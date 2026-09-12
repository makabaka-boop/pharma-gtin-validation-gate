"""HTTP-level tests for shelf-life reviews (货架期复核)."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app
from app.shelf_life import classify_remaining, remaining_days

GTIN_A = "07300040109316"  # valid, check digit 6
GTIN_B = "00000000000000"  # valid, check digit 0
GTIN_C = "00000000000017"  # valid, check digit 7 (unplanned in tests)

REVIEW_DATE = "2026-09-12"


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test gets an empty in-memory database."""
    storage.reset_for_tests(":memory:")
    yield


@pytest.fixture
def client():
    # Context-manager use triggers the lifespan (init_db), which must stay
    # harmless against the fixture's already-created tables.
    with TestClient(app) as test_client:
        yield test_client


def _create(
    client: TestClient, order_no: str = "PO-SL", lines: list[list] | None = None
):
    lines = lines if lines is not None else [[GTIN_A, 10], [GTIN_B, 3]]
    return client.post(
        "/receipts",
        json={
            "order_no": order_no,
            "items": [{"gtin": gtin, "planned_qty": qty} for gtin, qty in lines],
        },
    )


def _scan(client: TestClient, order_no: str, codes: list[str]):
    return client.post(f"/receipts/{order_no}/scans", json=codes)


def _batch(batch_no: str, quantity: int, expiry_date: str) -> dict:
    return {"batch_no": batch_no, "quantity": quantity, "expiry_date": expiry_date}


def _review(
    client: TestClient,
    review_id: str = "SLR-1",
    order_no: str = "PO-SL",
    review_date: str = REVIEW_DATE,
    min_sellable_days: object = 30,
    items: list[dict] | None = None,
):
    if items is None:
        items = [{"gtin": GTIN_A, "batches": [_batch("B1", 1, "2027-01-01")]}]
    return client.post(
        "/shelf-life-reviews",
        json={
            "review_id": review_id,
            "order_no": order_no,
            "review_date": review_date,
            "min_sellable_days": min_sellable_days,
            "items": items,
        },
    )


# ---------------------------------------------------------------------------
# Domain rules: calendar-day arithmetic and the three dispositions
# ---------------------------------------------------------------------------


def test_remaining_days_counts_calendar_days() -> None:
    assert remaining_days(date(2026, 9, 12), date(2026, 9, 12)) == 0
    assert remaining_days(date(2026, 9, 1), date(2026, 9, 12)) == -11
    assert remaining_days(date(2026, 10, 12), date(2026, 9, 12)) == 30
    assert remaining_days(date(2027, 1, 1), date(2026, 9, 12)) == 111


def test_classify_remaining_thresholds() -> None:
    assert classify_remaining(-1, 30) == "expired"
    assert classify_remaining(0, 30) == "short_dated"
    assert classify_remaining(29, 30) == "short_dated"
    assert classify_remaining(30, 30) == "usable"
    assert classify_remaining(0, 0) == "usable"  # zero threshold: never short


# ---------------------------------------------------------------------------
# Happy path: three dispositions, stable sorting, deterministic read-back
# ---------------------------------------------------------------------------


def test_review_marks_three_dispositions_and_sorts_batches(
    client: TestClient,
) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A] * 8)
    items = [
        {
            "gtin": GTIN_A,
            "batches": [
                _batch("B-USABLE", 3, "2027-01-01"),    # 111 days: usable
                _batch("B-EXPIRED", 2, "2026-09-01"),   # -11 days: expired
                _batch("B-SHORT", 2, "2026-10-01"),     # 19 days: short_dated
                _batch("B-EDGE", 1, "2026-10-12"),      # 30 == threshold: usable
            ],
        }
    ]
    response = _review(client, items=items)
    assert response.status_code == 201
    body = response.json()
    assert body["review_id"] == "SLR-1"
    assert body["order_no"] == "PO-SL"
    assert body["review_date"] == REVIEW_DATE
    assert body["min_sellable_days"] == 30
    (item,) = body["items"]
    assert item["gtin"] == GTIN_A
    assert item["received_qty"] == 8
    assert item["declared_qty"] == 8
    # Batches come back sorted by expiry date, not in submission order.
    assert item["batches"] == [
        {"batch_no": "B-EXPIRED", "quantity": 2, "expiry_date": "2026-09-01",
         "remaining_days": -11, "disposition": "expired"},
        {"batch_no": "B-SHORT", "quantity": 2, "expiry_date": "2026-10-01",
         "remaining_days": 19, "disposition": "short_dated"},
        {"batch_no": "B-EDGE", "quantity": 1, "expiry_date": "2026-10-12",
         "remaining_days": 30, "disposition": "usable"},
        {"batch_no": "B-USABLE", "quantity": 3, "expiry_date": "2027-01-01",
         "remaining_days": 111, "disposition": "usable"},
    ]
    assert body["summary"] == {
        "gtin_count": 1,
        "batch_count": 4,
        "declared_qty": 8,
        "expired_batches": 1,
        "short_dated_batches": 1,
        "usable_batches": 2,
    }
    # The persisted document is served identically on read.
    reread = client.get("/shelf-life-reviews/SLR-1")
    assert reread.status_code == 200
    assert reread.json() == body


def test_same_expiry_date_sorts_by_batch_no(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A] * 3)
    items = [
        {
            "gtin": GTIN_A,
            "batches": [
                _batch("B2", 1, "2027-01-01"),
                _batch("B10", 1, "2027-01-01"),
                _batch("B1", 1, "2027-01-01"),
            ],
        }
    ]
    response = _review(client, items=items)
    assert response.status_code == 201
    batches = response.json()["items"][0]["batches"]
    # Equal expiry dates fall back to (lexicographic) batch-number order.
    assert [batch["batch_no"] for batch in batches] == ["B1", "B10", "B2"]


def test_gtin_items_keep_submission_order(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_B, GTIN_A])
    items = [
        {"gtin": GTIN_B, "batches": [_batch("B1", 1, "2027-01-01")]},
        {"gtin": GTIN_A, "batches": [_batch("A1", 1, "2027-01-01")]},
    ]
    response = _review(client, items=items)
    assert response.status_code == 201
    # Only the batches inside a GTIN are sorted; the GTIN entries themselves
    # stay in submission order.
    assert [item["gtin"] for item in response.json()["items"]] == [GTIN_B, GTIN_A]


def test_unplanned_booked_gtin_can_be_reviewed(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_C, GTIN_C])  # unplanned merchandise, booked 2
    items = [{"gtin": GTIN_C, "batches": [_batch("C-1", 2, "2026-09-12")]}]
    response = _review(client, items=items)
    assert response.status_code == 201
    (item,) = response.json()["items"]
    assert item["gtin"] == GTIN_C
    assert item["received_qty"] == 2
    assert item["declared_qty"] == 2
    # Expiring on the review date: zero remaining days, not yet expired.
    assert item["batches"][0]["remaining_days"] == 0
    assert item["batches"][0]["disposition"] == "short_dated"


def test_received_qty_snapshot_survives_later_scans(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_A])
    items = [{"gtin": GTIN_A, "batches": [_batch("B1", 2, "2027-01-01")]}]
    assert _review(client, "SLR-SNAP", items=items).status_code == 201
    _scan(client, "PO-SL", [GTIN_A] * 5)  # received grows 2 -> 7 afterwards
    body = client.get("/shelf-life-reviews/SLR-SNAP").json()
    assert body["items"][0]["received_qty"] == 2  # frozen at review time


def test_review_links_legacy_padded_receipt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Receipts stored before order-number canonicalisation still accept
    # reviews; the response carries the canonical order number.
    original = storage._canonical_order_no
    storage._canonical_order_no = lambda value: value
    try:
        assert _create(client, "  PO-LEGACY-SL  ", [[GTIN_A, 2]]).status_code == 201
    finally:
        storage._canonical_order_no = original
    _scan(client, "PO-LEGACY-SL", [GTIN_A])

    response = _review(client, order_no="PO-LEGACY-SL")
    assert response.status_code == 201
    assert response.json()["order_no"] == "PO-LEGACY-SL"


# ---------------------------------------------------------------------------
# Threshold and boundary rules
# ---------------------------------------------------------------------------


def test_min_sellable_days_zero_and_3650_are_accepted(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    # Threshold 0: every non-expired batch is usable, even expiring today.
    items = [{"gtin": GTIN_A, "batches": [_batch("B1", 1, "2026-09-12")]}]
    response = _review(client, "SLR-ZERO", min_sellable_days=0, items=items)
    assert response.status_code == 201
    assert response.json()["items"][0]["batches"][0]["disposition"] == "usable"
    # The upper bound 3650 is inclusive.
    assert _review(client, "SLR-MAX", min_sellable_days=3650).status_code == 201


@pytest.mark.parametrize("threshold", [-1, 3651, 10**6, True, 1.5, "30", None])
def test_min_sellable_days_out_of_range_is_422(
    client: TestClient, threshold: object
) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    response = _review(client, min_sellable_days=threshold)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_declared_total_exactly_received_is_created(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_A])
    items = [{"gtin": GTIN_A, "batches": [_batch("B1", 2, "2027-01-01")]}]
    response = _review(client, items=items)
    assert response.status_code == 201
    assert response.json()["items"][0]["declared_qty"] == 2


# ---------------------------------------------------------------------------
# Error envelope: 409 / 404 / 422
# ---------------------------------------------------------------------------


def test_duplicate_review_id_is_409(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    assert _review(client, "SLR-DUP").status_code == 201
    response = _review(client, "SLR-DUP")
    assert response.status_code == 409
    body = response.json()
    assert "detail" in body
    assert "results" not in body
    # The first review is untouched by the rejected duplicate.
    assert client.get("/shelf-life-reviews/SLR-DUP").status_code == 200


def test_whitespace_padded_review_id_is_a_duplicate_409(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    assert _review(client, "SLR-WS").status_code == 201
    assert _review(client, "  SLR-WS  ").status_code == 409
    # The canonical number addresses the stored review.
    assert client.get("/shelf-life-reviews/SLR-WS").status_code == 200
    assert client.get("/shelf-life-reviews/%20SLR-WS%20").status_code == 200


def test_unknown_order_no_is_404(client: TestClient) -> None:
    response = _review(client, order_no="NO-SUCH-ORDER")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_unbooked_gtin_is_404(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    # GTIN_C was neither planned nor scanned on this receipt.
    items = [{"gtin": GTIN_C, "batches": [_batch("B1", 1, "2027-01-01")]}]
    response = _review(client, items=items)
    assert response.status_code == 404
    assert "detail" in response.json()


def test_planned_but_unreceived_gtin_is_422_not_404(client: TestClient) -> None:
    _create(client)  # GTIN_B is planned (3) but never scanned
    _scan(client, "PO-SL", [GTIN_A])
    items = [{"gtin": GTIN_B, "batches": [_batch("B1", 1, "2027-01-01")]}]
    # The GTIN is on the books, but declared 1 exceeds received 0.
    response = _review(client, items=items)
    assert response.status_code == 422


def test_get_unknown_review_is_404(client: TestClient) -> None:
    response = client.get("/shelf-life-reviews/NO-SUCH-REVIEW")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_declared_total_exceeding_received_is_422_and_leaves_no_record(
    client: TestClient,
) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_A])  # received 2
    items = [
        {
            "gtin": GTIN_A,
            "batches": [
                _batch("B1", 2, "2027-01-01"),
                _batch("B2", 1, "2027-02-01"),  # declared 3 > received 2
            ],
        }
    ]
    response = _review(client, "SLR-OVER", items=items)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    # No residue: the number stays unknown and is immediately reusable.
    assert client.get("/shelf-life-reviews/SLR-OVER").status_code == 404
    items[0]["batches"] = items[0]["batches"][:1]  # declared 2 == received 2
    assert _review(client, "SLR-OVER", items=items).status_code == 201


def test_invalid_date_formats_are_422(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    for bad_date in ("12/09/2026", "2026-13-01", "not-a-date",
                     "2026-09-12T08:00:00", "2026-9-2"):
        assert _review(client, review_date=bad_date).status_code == 422
        items = [{"gtin": GTIN_A, "batches": [_batch("B1", 1, bad_date)]}]
        assert _review(client, items=items).status_code == 422


def test_duplicate_batch_no_within_gtin_is_422(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_A])
    items = [
        {
            "gtin": GTIN_A,
            "batches": [
                _batch("B1", 1, "2027-01-01"),
                _batch("B1", 1, "2027-02-01"),
            ],
        }
    ]
    assert _review(client, items=items).status_code == 422


def test_same_batch_no_on_different_gtins_is_allowed(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_B])
    items = [
        {"gtin": GTIN_A, "batches": [_batch("B1", 1, "2027-01-01")]},
        {"gtin": GTIN_B, "batches": [_batch("B1", 1, "2027-01-01")]},
    ]
    assert _review(client, items=items).status_code == 201


def test_duplicate_gtin_items_are_422(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_A])
    items = [
        {"gtin": GTIN_A, "batches": [_batch("B1", 1, "2027-01-01")]},
        {"gtin": GTIN_A, "batches": [_batch("B2", 1, "2027-01-01")]},
    ]
    assert _review(client, items=items).status_code == 422


@pytest.mark.parametrize("qty", [0, -1, True, 1.5, "2", None])
def test_non_positive_or_non_integer_quantity_is_422(
    client: TestClient, qty: object
) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    items = [{"gtin": GTIN_A, "batches": [_batch("B1", qty, "2027-01-01")]}]
    assert _review(client, items=items).status_code == 422


def test_quantity_above_sqlite_integer_range_is_422(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    items = [{"gtin": GTIN_A, "batches": [_batch("B1", 2**63, "2027-01-01")]}]
    assert _review(client, items=items).status_code == 422


def test_review_id_with_slash_or_blank_is_422(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    assert _review(client, "SLR/2026/0001").status_code == 422
    assert _review(client, "   ").status_code == 422


def test_empty_or_blank_structures_are_422(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A])
    assert _review(client, items=[]).status_code == 422
    assert _review(client, items=[{"gtin": GTIN_A, "batches": []}]).status_code == 422
    # A batch number holding only whitespace is no batch number.
    items = [{"gtin": GTIN_A, "batches": [_batch("   ", 1, "2027-01-01")]}]
    assert _review(client, items=items).status_code == 422


def test_structural_422_precedes_order_lookup(client: TestClient) -> None:
    # Pydantic request validation runs before the endpoint, so an invalid
    # body answers 422 even when the order does not exist.
    response = _review(client, order_no="GONE", min_sellable_days=-1)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Failed requests leave no records behind
# ---------------------------------------------------------------------------


def test_failed_requests_leave_no_residual_records(client: TestClient) -> None:
    _create(client)
    _scan(client, "PO-SL", [GTIN_A, GTIN_A])

    def body(review_id: str, **overrides: object) -> dict:
        base: dict = {
            "review_id": review_id,
            "order_no": "PO-SL",
            "review_date": REVIEW_DATE,
            "min_sellable_days": 30,
            "items": [{"gtin": GTIN_A, "batches": [_batch("B1", 1, "2027-01-01")]}],
        }
        base.update(overrides)
        return base

    failing_bodies = [
        # invalid review date
        body("SLR-F1", review_date="not-a-date"),
        # duplicate batch number within the GTIN
        body("SLR-F2", items=[{"gtin": GTIN_A, "batches": [
            _batch("B1", 1, "2027-01-01"), _batch("B1", 1, "2027-02-01")]}]),
        # non-positive quantity
        body("SLR-F3", items=[{"gtin": GTIN_A, "batches": [
            _batch("B1", 0, "2027-01-01")]}]),
        # threshold out of range
        body("SLR-F4", min_sellable_days=3651),
        # declared total exceeds the received quantity (2)
        body("SLR-F5", items=[{"gtin": GTIN_A, "batches": [
            _batch("B1", 3, "2027-01-01")]}]),
        # GTIN never booked on the receipt
        body("SLR-F6", items=[{"gtin": GTIN_C, "batches": [
            _batch("B1", 1, "2027-01-01")]}]),
        # unknown receipt
        body("SLR-F7", order_no="PO-GONE"),
    ]
    for failing in failing_bodies:
        response = client.post("/shelf-life-reviews", json=failing)
        assert response.status_code in (404, 422), failing["review_id"]

    # A duplicate attempt fails too, after one review succeeded.
    assert _review(client, "SLR-KEEP").status_code == 201
    assert _review(client, "SLR-KEEP").status_code == 409

    # No failed request left anything behind: every failed number is still
    # unknown, and reusing one of them succeeds cleanly and completely.
    for failing in failing_bodies:
        fetched = client.get(f"/shelf-life-reviews/{failing['review_id']}")
        assert fetched.status_code == 404, failing["review_id"]

    reused = client.post(
        "/shelf-life-reviews",
        json=body("SLR-F5", items=[{"gtin": GTIN_A, "batches": [
            _batch("B1", 2, "2027-01-01")]}]),
    )
    assert reused.status_code == 201
    assert client.get("/shelf-life-reviews/SLR-F5").json() == reused.json()

    # Only the successful reviews were ever persisted: the kept one and the
    # reused number. No failed request left a review, item or batch row.
    stored_reviews = storage._get_connection().execute(
        "SELECT review_id FROM shelf_life_reviews"
    ).fetchall()
    assert sorted(row["review_id"] for row in stored_reviews) == [
        "SLR-F5",
        "SLR-KEEP",
    ]
    stored_items = storage._get_connection().execute(
        "SELECT review_id, COUNT(*) AS n FROM shelf_life_review_items "
        "GROUP BY review_id"
    ).fetchall()
    assert {row["review_id"]: row["n"] for row in stored_items} == {
        "SLR-F5": 1,
        "SLR-KEEP": 1,
    }
    stored_batches = storage._get_connection().execute(
        "SELECT review_id, COUNT(*) AS n FROM shelf_life_batches "
        "GROUP BY review_id"
    ).fetchall()
    assert {row["review_id"]: row["n"] for row in stored_batches} == {
        "SLR-F5": 1,
        "SLR-KEEP": 1,
    }
