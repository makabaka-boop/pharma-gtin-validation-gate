"""HTTP-level tests for goods-receipt orders and scan reconciliation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main, storage
from app.main import app

GTIN_A = "07300040109316"  # valid, check digit 6
GTIN_B = "00000000000000"  # valid, check digit 0
GTIN_BAD_CHECKSUM = "07300040109310"  # 14 digits but wrong check digit
GTIN_MALFORMED = "0730004010-9316"    # hyphen: format error

FAILURE_HEADER = {"X-Simulate-Storage-Failure": "1"}


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch: pytest.MonkeyPatch):
    """Every test gets an empty in-memory database and injection enabled."""
    monkeypatch.setattr(main, "_FAILURE_INJECTION_ENABLED", True)
    storage.reset_for_tests(":memory:")
    yield


@pytest.fixture
def client():
    # Context-manager use triggers the lifespan (init_db), which must stay
    # harmless against the fixture's already-created tables.
    with TestClient(app) as test_client:
        yield test_client


def _create(client: TestClient, order_no: str, lines: list[list] | None = None):
    lines = lines or [[GTIN_A, 2], [GTIN_B, 1]]
    return client.post(
        "/receipts", json={"order_no": order_no, "items": [
            {"gtin": gtin, "planned_qty": qty} for gtin, qty in lines
        ]}
    )


def _state(client: TestClient, order_no: str) -> dict:
    return client.get(f"/receipts/{order_no}").json()


def _received_by_gtin(state: dict) -> dict[str, int]:
    return {item["gtin"]: item["received_qty"] for item in state["items"]}


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


def test_create_receipt_persists_planned_lines_at_zero(client: TestClient) -> None:
    response = _create(client, "PO-1")
    assert response.status_code == 201
    body = response.json()
    assert body["order_no"] == "PO-1"
    assert body["items"] == [
        {"gtin": GTIN_A, "planned_qty": 2, "received_qty": 0},
        {"gtin": GTIN_B, "planned_qty": 1, "received_qty": 0},
    ]


def test_duplicate_order_number_is_409(client: TestClient) -> None:
    assert _create(client, "PO-DUP").status_code == 201
    response = _create(client, "PO-DUP")
    assert response.status_code == 409
    assert "results" not in response.json()
    assert "detail" in response.json()


def test_create_rejects_illegal_gtin_with_422(client: TestClient) -> None:
    for bad_gtin in (GTIN_BAD_CHECKSUM, GTIN_MALFORMED, "abc", "0730004010931"):
        response = client.post(
            "/receipts",
            json={"order_no": "PO-X",
                  "items": [{"gtin": bad_gtin, "planned_qty": 1}]},
        )
        assert response.status_code == 422, bad_gtin


def test_create_rejects_duplicate_gtins_with_422(client: TestClient) -> None:
    response = client.post(
        "/receipts",
        json={"order_no": "PO-X", "items": [
            {"gtin": GTIN_A, "planned_qty": 1},
            {"gtin": GTIN_A, "planned_qty": 2},
        ]},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("qty", [0, -1])
def test_create_rejects_non_positive_integer_quantity(
    client: TestClient, qty: int
) -> None:
    response = client.post(
        "/receipts",
        json={"order_no": "PO-X",
              "items": [{"gtin": GTIN_A, "planned_qty": qty}]},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("qty", [True, 1.0, 1.5, "2", None])
def test_create_rejects_non_integer_quantity(
    client: TestClient, qty: object
) -> None:
    response = client.post(
        "/receipts",
        json={"order_no": "PO-X",
              "items": [{"gtin": GTIN_A, "planned_qty": qty}]},
    )
    assert response.status_code == 422


def test_create_rejects_empty_items_and_blank_order_no(
    client: TestClient,
) -> None:
    assert client.post(
        "/receipts", json={"order_no": "PO-X", "items": []}
    ).status_code == 422
    assert client.post(
        "/receipts",
        json={"order_no": "   ",
              "items": [{"gtin": GTIN_A, "planned_qty": 1}]},
    ).status_code == 422


def test_rejected_create_is_whole_order_with_nothing_persisted(
    client: TestClient,
) -> None:
    # Invalid GTIN line must not leave a partial order behind.
    response = client.post(
        "/receipts",
        json={"order_no": "PO-PARTIAL", "items": [
            {"gtin": GTIN_A, "planned_qty": 1},
            {"gtin": GTIN_BAD_CHECKSUM, "planned_qty": 1},
        ]},
    )
    assert response.status_code == 422
    assert client.get("/receipts/PO-PARTIAL").status_code == 404
    # The order number can be reused after a failed create (no ghost row).
    assert _create(client, "PO-PARTIAL").status_code == 201


# ---------------------------------------------------------------------------
# Order-number and quantity boundaries (regression)
# ---------------------------------------------------------------------------


def test_order_no_containing_slash_is_rejected_with_422(
    client: TestClient,
) -> None:
    # A slash can never be addressed as one URL path segment: the receipt
    # would be created but every read/scan would answer 404. Reject up front.
    response = client.post(
        "/receipts",
        json={"order_no": "PO/2026/0001",
              "items": [{"gtin": GTIN_A, "planned_qty": 1}]},
    )
    assert response.status_code == 422
    body = response.json()
    assert "results" not in body
    assert isinstance(body["detail"], list) and body["detail"]


def test_whitespace_padded_order_no_is_a_duplicate_409(
    client: TestClient,
) -> None:
    assert _create(client, "PO-WS").status_code == 201
    # Visually identical number with surrounding whitespace: same receipt.
    response = _create(client, "  PO-WS  ")
    assert response.status_code == 409
    assert "results" not in response.json()
    # Symmetrically, a number created padded blocks the plain variant.
    assert _create(client, "  PO-WS2  ").status_code == 201
    assert _create(client, "PO-WS2").status_code == 409


def test_whitespace_variant_addresses_the_same_receipt(
    client: TestClient,
) -> None:
    _create(client, "PO-WS3", [[GTIN_A, 2]])
    client.post("/receipts/PO-WS3/scans", json=[GTIN_A])

    padded_read = client.get("/receipts/%20PO-WS3%20")
    assert padded_read.status_code == 200
    assert padded_read.json()["order_no"] == "PO-WS3"
    assert _received_by_gtin(padded_read.json()) == {GTIN_A: 1}

    padded_scan = client.post("/receipts/%20PO-WS3%20/scans", json=[GTIN_A])
    assert padded_scan.status_code == 200
    assert padded_scan.json()["results"][0]["reconciliation"] == {
        "conclusion": "matched", "planned_qty": 2, "received_qty": 2
    }


@pytest.mark.parametrize("qty", [2**63, 2**63 + 1, 10**30])
def test_create_rejects_quantity_above_sqlite_integer_range(
    client: TestClient, qty: int
) -> None:
    # Beyond the SQLite INTEGER range the insert would explode as an
    # unhandled OverflowError (500); it must be a structured 422 instead.
    response = client.post(
        "/receipts",
        json={"order_no": "PO-HUGE",
              "items": [{"gtin": GTIN_A, "planned_qty": qty}]},
    )
    assert response.status_code == 422
    body = response.json()
    assert "results" not in body
    assert isinstance(body["detail"], list) and body["detail"]
    # Nothing persisted: the order number stays free.
    assert client.get("/receipts/PO-HUGE").status_code == 404


def test_create_accepts_sqlite_integer_max_quantity(
    client: TestClient,
) -> None:
    response = client.post(
        "/receipts",
        json={"order_no": "PO-MAXQTY",
              "items": [{"gtin": GTIN_A, "planned_qty": 2**63 - 1}]},
    )
    assert response.status_code == 201
    assert response.json()["items"][0]["planned_qty"] == 2**63 - 1


# ---------------------------------------------------------------------------
# Legacy rows: receipts stored with padded keys before canonicalisation
# ---------------------------------------------------------------------------


def _create_legacy(client: TestClient, order_no: str, lines: list[list]) -> None:
    """Persist a receipt exactly as pre-canonicalisation code did.

    The old service stored the business number raw, so a database from
    before the upgrade can hold padded keys like ``"  PO-1  "``.
    """
    original = storage._canonical_order_no
    storage._canonical_order_no = lambda value: value
    try:
        assert _create(client, order_no, lines).status_code == 201
    finally:
        storage._canonical_order_no = original


def test_legacy_padded_receipt_stays_readable(client: TestClient) -> None:
    _create_legacy(client, "  PO-LEGACY  ", [[GTIN_A, 2]])

    canonical = client.get("/receipts/PO-LEGACY")
    assert canonical.status_code == 200
    assert canonical.json()["order_no"] == "PO-LEGACY"
    assert canonical.json()["items"] == [
        {"gtin": GTIN_A, "planned_qty": 2, "received_qty": 0}
    ]
    # The padded spelling resolves to the same original receipt as well.
    assert client.get("/receipts/%20%20PO-LEGACY%20%20").status_code == 200


def test_legacy_padded_receipt_blocks_recreation_with_409(
    client: TestClient,
) -> None:
    _create_legacy(client, "  PO-LEGACY2  ", [[GTIN_A, 1]])

    assert _create(client, "PO-LEGACY2").status_code == 409
    assert _create(client, " PO-LEGACY2 ").status_code == 409
    # The original receipt is preserved and still the one being served.
    state = _state(client, "PO-LEGACY2")
    assert state["items"] == [
        {"gtin": GTIN_A, "planned_qty": 1, "received_qty": 0}
    ]


def test_scans_book_onto_legacy_padded_receipt(client: TestClient) -> None:
    _create_legacy(client, "  PO-LEGACY3  ", [[GTIN_A, 2]])

    scan = client.post("/receipts/PO-LEGACY3/scans", json=[GTIN_A])
    assert scan.status_code == 200
    assert scan.json()["results"][0]["reconciliation"] == {
        "conclusion": "matched", "planned_qty": 2, "received_qty": 1
    }
    padded_scan = client.post("/receipts/%20PO-LEGACY3%20/scans", json=[GTIN_A])
    assert padded_scan.json()["results"][0]["reconciliation"][
        "received_qty"
    ] == 2
    assert _received_by_gtin(_state(client, "PO-LEGACY3")) == {GTIN_A: 2}


def test_exact_row_wins_over_legacy_padded_and_both_are_kept(
    client: TestClient,
) -> None:
    # Pre-upgrade databases could hold both spellings as separate orders.
    _create_legacy(client, "PO-SHADOW", [[GTIN_A, 1]])
    _create_legacy(client, " PO-SHADOW ", [[GTIN_B, 5]])

    # The exactly-canonical row is served; the padded one is shadowed.
    state = _state(client, "PO-SHADOW")
    assert state["items"] == [
        {"gtin": GTIN_A, "planned_qty": 1, "received_qty": 0}
    ]
    assert _create(client, "PO-SHADOW").status_code == 409
    # Both original rows remain stored; nothing is deleted or rewritten.
    stored = storage._get_connection().execute(
        "SELECT order_no FROM receipts"
    ).fetchall()
    assert sorted(row["order_no"] for row in stored) == [
        " PO-SHADOW ", "PO-SHADOW"
    ]


# ---------------------------------------------------------------------------
# Scanning: planned increment up to and beyond the plan
# ---------------------------------------------------------------------------


def test_planned_scans_increment_from_matched_to_excess(
    client: TestClient,
) -> None:
    _create(client, "PO-INC", [[GTIN_A, 2]])

    # First scan: matched, cumulative received 1.
    first = client.post("/receipts/PO-INC/scans", json=[GTIN_A])
    assert first.status_code == 200
    item = first.json()["results"][0]
    assert item["status"] == "valid"
    assert item["reconciliation"] == {
        "conclusion": "matched", "planned_qty": 2, "received_qty": 1
    }

    # Second scan: still within plan.
    second = client.post("/receipts/PO-INC/scans", json=[GTIN_A])
    assert second.json()["results"][0]["reconciliation"]["conclusion"] == "matched"
    assert second.json()["results"][0]["reconciliation"]["received_qty"] == 2

    # Third scan: over-receipt.
    third = client.post("/receipts/PO-INC/scans", json=[GTIN_A])
    recon = third.json()["results"][0]["reconciliation"]
    assert recon == {"conclusion": "excess", "planned_qty": 2,
                     "received_qty": 3}


def test_batch_increments_in_input_order_within_one_request(
    client: TestClient,
) -> None:
    _create(client, "PO-BATCH", [[GTIN_A, 2]])
    response = client.post(
        "/receipts/PO-BATCH/scans", json=[GTIN_A, GTIN_A, GTIN_A]
    )
    conclusions = [
        item["reconciliation"]["conclusion"]
        for item in response.json()["results"]
    ]
    received = [
        item["reconciliation"]["received_qty"]
        for item in response.json()["results"]
    ]
    assert conclusions == ["matched", "matched", "excess"]
    assert received == [1, 2, 3]


def test_unplanned_gtin_is_recognised_and_then_stays_unplanned(
    client: TestClient,
) -> None:
    _create(client, "PO-UNPLAN", [[GTIN_A, 5]])

    first = client.post("/receipts/PO-UNPLAN/scans", json=[GTIN_B])
    recon = first.json()["results"][0]["reconciliation"]
    assert recon == {"conclusion": "unplanned", "planned_qty": None,
                     "received_qty": 1}

    # Repeat scans of the same unplanned GTIN stay unplanned and accumulate.
    second = client.post("/receipts/PO-UNPLAN/scans", json=[GTIN_B])
    recon = second.json()["results"][0]["reconciliation"]
    assert recon["conclusion"] == "unplanned"
    assert recon["planned_qty"] is None
    assert recon["received_qty"] == 2

    state = _state(client, "PO-UNPLAN")
    assert _received_by_gtin(state) == {GTIN_A: 0, GTIN_B: 2}


def test_invalid_codes_are_not_booked_and_have_no_conclusion(
    client: TestClient,
) -> None:
    _create(client, "PO-INVALID", [[GTIN_A, 2]])
    codes = [GTIN_BAD_CHECKSUM, GTIN_MALFORMED, "abc", " 07300040109316"]
    response = client.post("/receipts/PO-INVALID/scans", json=codes)
    assert response.status_code == 200
    results = response.json()["results"]
    # Original per-code verdicts preserved in order.
    assert [item["code"] for item in results] == codes
    assert [item["status"] for item in results] == [
        "checksum_mismatch", "format_error", "format_error", "format_error"
    ]
    for item in results:
        assert item["reconciliation"] is None

    # Nothing booked: a later valid scan starts at received 1 / matched.
    after = client.post("/receipts/PO-INVALID/scans", json=[GTIN_A])
    assert after.json()["results"][0]["reconciliation"] == {
        "conclusion": "matched", "planned_qty": 2, "received_qty": 1
    }


def test_mixed_batch_preserves_order_and_books_only_valid_codes(
    client: TestClient,
) -> None:
    _create(client, "PO-MIX", [[GTIN_A, 1]])
    codes = [
        GTIN_A,              # matched, received 1
        GTIN_B,              # unplanned, received 1
        GTIN_BAD_CHECKSUM,   # not booked, no conclusion
        GTIN_A,              # excess, received 2
        GTIN_MALFORMED,      # not booked, no conclusion
    ]
    response = client.post("/receipts/PO-MIX/scans", json=codes)
    results = response.json()["results"]
    assert [item["code"] for item in results] == codes
    assert [
        None if item["reconciliation"] is None
        else item["reconciliation"]["conclusion"]
        for item in results
    ] == ["matched", "unplanned", None, "excess", None]
    state = _state(client, "PO-MIX")
    assert _received_by_gtin(state) == {GTIN_A: 2, GTIN_B: 1}


def test_scanning_unknown_order_is_404(client: TestClient) -> None:
    response = client.post("/receipts/NO-SUCH/scans", json=[GTIN_A])
    assert response.status_code == 404
    assert "detail" in response.json()


def test_get_unknown_order_is_404(client: TestClient) -> None:
    assert client.get("/receipts/NO-SUCH").status_code == 404


def test_scan_structural_boundaries_are_422_without_results(
    client: TestClient,
) -> None:
    _create(client, "PO-BOUND")
    for payload in ([], ["x"] * 101, [123], {}):
        response = client.post("/receipts/PO-BOUND/scans", json=payload)
        assert response.status_code == 422, payload
        assert "results" not in response.json()


def test_scan_unknown_order_404_takes_precedence_over_valid_body(
    client: TestClient,
) -> None:
    # Even an all-valid batch cannot create booking state on a missing order.
    response = client.post("/receipts/GONE/scans", json=[GTIN_A])
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Transactional rollback and deterministic retry
# ---------------------------------------------------------------------------


def test_storage_failure_rolls_back_whole_batch(client: TestClient) -> None:
    _create(client, "PO-ROLLBACK", [[GTIN_A, 5], [GTIN_B, 5]])
    codes = [GTIN_A, GTIN_B, GTIN_A]

    failed = client.post(
        "/receipts/PO-ROLLBACK/scans", json=codes, headers=FAILURE_HEADER
    )
    assert failed.status_code == 503
    assert "results" not in failed.json()

    # The injected failure happened after the first increment; rollback must
    # have erased it so every line is back to zero.
    state = _state(client, "PO-ROLLBACK")
    assert _received_by_gtin(state) == {GTIN_A: 0, GTIN_B: 0}


def test_retry_after_rollback_is_deterministic(client: TestClient) -> None:
    _create(client, "PO-RETRY", [[GTIN_A, 2]])
    codes = [GTIN_A, GTIN_A, GTIN_A]

    failed = client.post(
        "/receipts/PO-RETRY/scans", json=codes, headers=FAILURE_HEADER
    )
    assert failed.status_code == 503

    # Same batch retried without the fault: counts look exactly as if the
    # failed request had never happened.
    retried = client.post("/receipts/PO-RETRY/scans", json=codes)
    assert retried.status_code == 200
    conclusions = [
        item["reconciliation"]["conclusion"]
        for item in retried.json()["results"]
    ]
    received = [
        item["reconciliation"]["received_qty"]
        for item in retried.json()["results"]
    ]
    assert conclusions == ["matched", "matched", "excess"]
    assert received == [1, 2, 3]

    # Retrying the whole batch once more counts forward from 3 (the failed
    # attempt contributed nothing in between).
    again = client.post("/receipts/PO-RETRY/scans", json=codes)
    assert [
        item["reconciliation"]["received_qty"]
        for item in again.json()["results"]
    ] == [4, 5, 6]
    assert all(
        item["reconciliation"]["conclusion"] == "excess"
        for item in again.json()["results"]
    )


def test_failure_injection_does_not_fire_without_enabling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "_FAILURE_INJECTION_ENABLED", False)
    _create(client, "PO-NOINJECT", [[GTIN_A, 5]])
    response = client.post(
        "/receipts/PO-NOINJECT/scans", json=[GTIN_A], headers=FAILURE_HEADER
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["reconciliation"]["received_qty"] == 1


def test_failure_injection_without_valid_codes_does_not_fire(
    client: TestClient,
) -> None:
    # Injection only happens after a booked increment; a batch of invalid
    # codes commits (books nothing) normally and returns 200.
    _create(client, "PO-NOVALID", [[GTIN_A, 5]])
    response = client.post(
        "/receipts/PO-NOVALID/scans",
        json=[GTIN_BAD_CHECKSUM, GTIN_MALFORMED],
        headers=FAILURE_HEADER,
    )
    assert response.status_code == 200
    assert all(
        item["reconciliation"] is None for item in response.json()["results"]
    )


def test_failure_rollback_preserves_counts_from_earlier_commits(
    client: TestClient,
) -> None:
    _create(client, "PO-PRIOR", [[GTIN_A, 5]])
    # A prior committed batch counts 2.
    client.post("/receipts/PO-PRIOR/scans", json=[GTIN_A, GTIN_A])
    # Next batch fails after its first increment; prior counts must survive.
    failed = client.post(
        "/receipts/PO-PRIOR/scans",
        json=[GTIN_A, GTIN_A],
        headers=FAILURE_HEADER,
    )
    assert failed.status_code == 503
    state = _state(client, "PO-PRIOR")
    assert _received_by_gtin(state) == {GTIN_A: 2}


# ---------------------------------------------------------------------------
# Received-count ceiling (SQLite INTEGER max): storage-failure semantics
# ---------------------------------------------------------------------------

SQLITE_INT64_MAX = 2**63 - 1


def _set_received(order_no: str, gtin: str, qty: int) -> None:
    """Drive a line's cumulative count directly, as long-running
    accumulation would (scanning 2**63 - 1 times is not an option)."""
    connection = storage._get_connection()
    connection.execute(
        "UPDATE receipt_items SET received_qty = ? "
        "WHERE order_no = ? AND gtin = ?",
        (qty, order_no, gtin),
    )
    connection.commit()


def test_planned_line_at_integer_ceiling_scan_is_structured_503(
    client: TestClient,
) -> None:
    # The increment cannot be persisted beyond the SQLite INTEGER range;
    # it must fail like any storage failure (503), not as an unhandled 500.
    _create(client, "PO-CEIL", [[GTIN_A, SQLITE_INT64_MAX]])
    _set_received("PO-CEIL", GTIN_A, SQLITE_INT64_MAX)

    response = client.post("/receipts/PO-CEIL/scans", json=[GTIN_A])
    assert response.status_code == 503
    body = response.json()
    assert "results" not in body
    assert "detail" in body
    assert response.headers["Retry-After"] == "1"
    # The saturated count is preserved, not destroyed.
    assert _received_by_gtin(_state(client, "PO-CEIL")) == {
        GTIN_A: SQLITE_INT64_MAX
    }


def test_ceiling_failure_rolls_back_the_whole_batch(client: TestClient) -> None:
    _create(client, "PO-CEILRB", [[GTIN_A, SQLITE_INT64_MAX], [GTIN_B, 5]])
    _set_received("PO-CEILRB", GTIN_A, SQLITE_INT64_MAX)

    # GTIN_B books fine, then GTIN_A hits the ceiling: the whole batch must
    # roll back, so GTIN_B's increment is undone as well.
    failed = client.post("/receipts/PO-CEILRB/scans", json=[GTIN_B, GTIN_A])
    assert failed.status_code == 503
    assert _received_by_gtin(_state(client, "PO-CEILRB")) == {
        GTIN_A: SQLITE_INT64_MAX,
        GTIN_B: 0,
    }

    # Retrying only the healthy line books it deterministically.
    retried = client.post("/receipts/PO-CEILRB/scans", json=[GTIN_B])
    assert retried.status_code == 200
    assert retried.json()["results"][0]["reconciliation"] == {
        "conclusion": "matched", "planned_qty": 5, "received_qty": 1
    }


def test_unplanned_line_at_integer_ceiling_keeps_count_and_503(
    client: TestClient,
) -> None:
    _create(client, "PO-CEILUNPLAN", [[GTIN_A, 5]])
    # GTIN_B is not on the plan: the first scan registers the unplanned line.
    client.post("/receipts/PO-CEILUNPLAN/scans", json=[GTIN_B])
    _set_received("PO-CEILUNPLAN", GTIN_B, SQLITE_INT64_MAX)

    response = client.post("/receipts/PO-CEILUNPLAN/scans", json=[GTIN_B])
    assert response.status_code == 503
    assert "results" not in response.json()
    # The original accumulated count is kept exactly as it was.
    assert _received_by_gtin(_state(client, "PO-CEILUNPLAN")) == {
        GTIN_A: 0,
        GTIN_B: SQLITE_INT64_MAX,
    }
