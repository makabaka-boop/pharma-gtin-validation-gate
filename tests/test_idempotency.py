"""HTTP-level tests for idempotent scan batches (``Idempotency-Key``).

A warehouse terminal can lose the response to a committed scan batch when
the network drops; resubmitting the batch with the same idempotency key
must replay the first response instead of counting the goods twice.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main, storage
from app.main import app

GTIN_A = "07300040109316"  # valid, check digit 6
GTIN_B = "00000000000000"  # valid, check digit 0
GTIN_BAD_CHECKSUM = "07300040109310"  # 14 digits but wrong check digit
GTIN_MALFORMED = "0730004010-9316"    # hyphen: format error
GTIN_FULLWIDTH = "０７３０００４０１０９３１６"  # full-width digits: format error

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
    lines = lines or [[GTIN_A, 2]]
    return client.post(
        "/receipts", json={"order_no": order_no, "items": [
            {"gtin": gtin, "planned_qty": qty} for gtin, qty in lines
        ]}
    )


def _scan(
    client: TestClient,
    order_no: str,
    codes: list,
    key: str | None = None,
    headers: dict[str, str] | None = None,
):
    request_headers = dict(headers or {})
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post(
        f"/receipts/{order_no}/scans", json=codes, headers=request_headers
    )


def _received_by_gtin(state: dict) -> dict[str, int]:
    return {item["gtin"]: item["received_qty"] for item in state["items"]}


def _state(client: TestClient, order_no: str) -> dict:
    return client.get(f"/receipts/{order_no}").json()


# ---------------------------------------------------------------------------
# Replay after a lost response
# ---------------------------------------------------------------------------


def test_replay_returns_first_response_without_recounting(
    client: TestClient,
) -> None:
    _create(client, "PO-IDEM")
    batch = [GTIN_A, GTIN_BAD_CHECKSUM, GTIN_A]

    first = _scan(client, "PO-IDEM", batch, key="batch-1")
    assert first.status_code == 200
    assert [
        None if item["reconciliation"] is None
        else item["reconciliation"]["received_qty"]
        for item in first.json()["results"]
    ] == [1, None, 2]
    assert _received_by_gtin(_state(client, "PO-IDEM")) == {GTIN_A: 2}

    # The terminal never saw the response and retries the identical batch:
    # the stored 200 body comes back byte-for-byte and nothing is recounted.
    for _ in range(3):
        replay = _scan(client, "PO-IDEM", batch, key="batch-1")
        assert replay.status_code == 200
        assert replay.content == first.content
        assert _received_by_gtin(_state(client, "PO-IDEM")) == {GTIN_A: 2}


def test_keyed_response_shape_matches_keyless_response(
    client: TestClient,
) -> None:
    _create(client, "PO-SHAPE-KEYED")
    _create(client, "PO-SHAPE-PLAIN")
    batch = [GTIN_A, GTIN_B, GTIN_MALFORMED]

    keyed = _scan(client, "PO-SHAPE-KEYED", batch, key="k")
    plain = _scan(client, "PO-SHAPE-PLAIN", batch)
    assert keyed.status_code == plain.status_code == 200
    # Same per-code verdicts and reconciliation content in both shapes.
    assert keyed.json() == plain.json()


def test_replay_preserves_non_ascii_codes_byte_for_byte(
    client: TestClient,
) -> None:
    _create(client, "PO-UNICODE")
    batch = [GTIN_FULLWIDTH, GTIN_A]
    first = _scan(client, "PO-UNICODE", batch, key="k")
    assert first.status_code == 200
    assert first.json()["results"][0]["code"] == GTIN_FULLWIDTH

    replay = _scan(client, "PO-UNICODE", batch, key="k")
    assert replay.content == first.content


def test_whitespace_padded_key_addresses_the_same_batch(
    client: TestClient,
) -> None:
    # Keys are canonicalised like order numbers: surrounding whitespace
    # carries no meaning, so a padded retry replays the original batch.
    _create(client, "PO-KEYWS")
    first = _scan(client, "PO-KEYWS", [GTIN_A], key="  batch-7  ")
    assert first.status_code == 200

    replay = _scan(client, "PO-KEYWS", [GTIN_A], key="batch-7")
    assert replay.status_code == 200
    assert replay.content == first.content
    assert _received_by_gtin(_state(client, "PO-KEYWS")) == {GTIN_A: 1}


def test_whitespace_variant_order_no_shares_the_batch_record(
    client: TestClient,
) -> None:
    # The record is stored under the canonical order number, so a retry
    # addressed to a padded spelling of the same receipt still replays.
    _create(client, "PO-ORDWS")
    first = _scan(client, "PO-ORDWS", [GTIN_A], key="k")
    assert first.status_code == 200

    replay = client.post(
        "/receipts/%20PO-ORDWS%20/scans",
        json=[GTIN_A],
        headers={"Idempotency-Key": "k"},
    )
    assert replay.status_code == 200
    assert replay.content == first.content
    assert _received_by_gtin(_state(client, "PO-ORDWS")) == {GTIN_A: 1}


# ---------------------------------------------------------------------------
# Conflicting reuse of a key
# ---------------------------------------------------------------------------


def test_conflicting_key_reuse_is_409_and_changes_nothing(
    client: TestClient,
) -> None:
    _create(client, "PO-CONFLICT")
    batch = [GTIN_A, GTIN_A]
    first = _scan(client, "PO-CONFLICT", batch, key="k")
    assert first.status_code == 200
    assert _received_by_gtin(_state(client, "PO-CONFLICT")) == {GTIN_A: 2}

    # Same key, different content.
    conflict = _scan(client, "PO-CONFLICT", [GTIN_A], key="k")
    assert conflict.status_code == 409
    assert "detail" in conflict.json()
    assert "results" not in conflict.json()

    # Same key, same codes in a different order.
    reordered = _scan(client, "PO-CONFLICT", [GTIN_A, GTIN_A, GTIN_B], key="k")
    assert reordered.status_code == 409

    # Counts untouched and the original record kept: the first batch still
    # replays exactly.
    assert _received_by_gtin(_state(client, "PO-CONFLICT")) == {GTIN_A: 2}
    replay = _scan(client, "PO-CONFLICT", batch, key="k")
    assert replay.status_code == 200
    assert replay.content == first.content
    assert _received_by_gtin(_state(client, "PO-CONFLICT")) == {GTIN_A: 2}


def test_same_codes_different_order_is_a_conflict(client: TestClient) -> None:
    _create(client, "PO-ORDER", [[GTIN_A, 5], [GTIN_B, 5]])
    assert _scan(client, "PO-ORDER", [GTIN_A, GTIN_B], key="k").status_code == 200
    response = _scan(client, "PO-ORDER", [GTIN_B, GTIN_A], key="k")
    assert response.status_code == 409
    assert _received_by_gtin(_state(client, "PO-ORDER")) == {
        GTIN_A: 1,
        GTIN_B: 1,
    }


def test_same_key_on_different_orders_is_independent(client: TestClient) -> None:
    _create(client, "PO-ONE")
    _create(client, "PO-TWO")

    first = _scan(client, "PO-ONE", [GTIN_A], key="shared")
    second = _scan(client, "PO-TWO", [GTIN_A], key="shared")
    assert first.status_code == second.status_code == 200
    # Both orders booked their own batch under the same key.
    assert _received_by_gtin(_state(client, "PO-ONE")) == {GTIN_A: 1}
    assert _received_by_gtin(_state(client, "PO-TWO")) == {GTIN_A: 1}
    # ... and each replays against its own order only.
    assert _scan(client, "PO-ONE", [GTIN_A], key="shared").content == first.content
    assert _scan(client, "PO-TWO", [GTIN_A], key="shared").content == second.content


# ---------------------------------------------------------------------------
# Key validation (422)
# ---------------------------------------------------------------------------


def test_invalid_keys_are_422_and_book_nothing(client: TestClient) -> None:
    _create(client, "PO-BADKEY")
    for bad_key in ("", "   ", "k" * 129):
        response = _scan(client, "PO-BADKEY", [GTIN_A], key=bad_key)
        assert response.status_code == 422, repr(bad_key)
        body = response.json()
        assert "results" not in body
        assert isinstance(body["detail"], list) and body["detail"]
    # Nothing was booked while rejecting the keys.
    assert _received_by_gtin(_state(client, "PO-BADKEY")) == {GTIN_A: 0}


def test_key_length_boundaries(client: TestClient) -> None:
    _create(client, "PO-KEYLEN")
    assert _scan(client, "PO-KEYLEN", [GTIN_A], key="k").status_code == 200
    assert _scan(client, "PO-KEYLEN", [GTIN_A], key="k" * 128).status_code == 200
    assert _scan(client, "PO-KEYLEN", [GTIN_A], key="k" * 129).status_code == 422


def test_blank_key_is_422_even_for_an_unknown_order(client: TestClient) -> None:
    # A malformed request is rejected before any resource lookup, exactly
    # like the structural body validation.
    response = _scan(client, "NO-SUCH-ORDER", [GTIN_A], key="  ")
    assert response.status_code == 422


def test_unknown_order_with_valid_key_is_still_404(client: TestClient) -> None:
    response = _scan(client, "NO-SUCH-ORDER", [GTIN_A], key="k")
    assert response.status_code == 404
    assert "detail" in response.json()


# ---------------------------------------------------------------------------
# Failure rollback: neither counts nor key occupation
# ---------------------------------------------------------------------------


def test_failed_batch_neither_counts_nor_occupies_the_key(
    client: TestClient,
) -> None:
    _create(client, "PO-FAIL", [[GTIN_A, 5]])
    batch = [GTIN_A, GTIN_A, GTIN_A]

    failed = _scan(client, "PO-FAIL", batch, key="k", headers=FAILURE_HEADER)
    assert failed.status_code == 503
    assert "results" not in failed.json()
    assert _received_by_gtin(_state(client, "PO-FAIL")) == {GTIN_A: 0}

    # The rolled-back attempt left no record behind: the same key accepts
    # the retried batch and books it exactly once.
    retried = _scan(client, "PO-FAIL", batch, key="k")
    assert retried.status_code == 200
    assert [
        item["reconciliation"]["received_qty"]
        for item in retried.json()["results"]
    ] == [1, 2, 3]

    replay = _scan(client, "PO-FAIL", batch, key="k")
    assert replay.status_code == 200
    assert replay.content == retried.content
    assert _received_by_gtin(_state(client, "PO-FAIL")) == {GTIN_A: 3}


def test_failed_attempt_does_not_block_a_different_array(
    client: TestClient,
) -> None:
    # Because the failed attempt never occupied the key, resubmitting the
    # key with a corrected array is a fresh batch, not a 409 conflict.
    _create(client, "PO-FAIL2", [[GTIN_A, 5]])
    failed = _scan(client, "PO-FAIL2", [GTIN_A], key="k", headers=FAILURE_HEADER)
    assert failed.status_code == 503

    corrected = _scan(client, "PO-FAIL2", [GTIN_A, GTIN_A], key="k")
    assert corrected.status_code == 200
    assert [
        item["reconciliation"]["received_qty"]
        for item in corrected.json()["results"]
    ] == [1, 2]


def test_keyed_failure_preserves_earlier_committed_counts(
    client: TestClient,
) -> None:
    _create(client, "PO-PRIOR", [[GTIN_A, 5]])
    committed = _scan(client, "PO-PRIOR", [GTIN_A, GTIN_A], key="first")
    assert committed.status_code == 200

    failed = _scan(
        client, "PO-PRIOR", [GTIN_A, GTIN_A], key="second",
        headers=FAILURE_HEADER,
    )
    assert failed.status_code == 503
    assert _received_by_gtin(_state(client, "PO-PRIOR")) == {GTIN_A: 2}

    # The first key still replays its committed response after the failure.
    replay = _scan(client, "PO-PRIOR", [GTIN_A, GTIN_A], key="first")
    assert replay.status_code == 200
    assert replay.content == committed.content


def test_keyed_scan_at_integer_ceiling_is_503_and_frees_key(
    client: TestClient,
) -> None:
    # A cumulative count at the SQLite INTEGER ceiling cannot be incremented;
    # the keyed batch must fail as a retryable 503 (not an unhandled 500),
    # keep the saturated count and leave the key unoccupied.
    ceiling = 2**63 - 1
    _create(client, "PO-CEILKEY", [[GTIN_A, ceiling]])
    connection = storage._get_connection()
    connection.execute(
        "UPDATE receipt_items SET received_qty = ? "
        "WHERE order_no = 'PO-CEILKEY' AND gtin = ?",
        (ceiling, GTIN_A),
    )
    connection.commit()

    failed = _scan(client, "PO-CEILKEY", [GTIN_A], key="ceil")
    assert failed.status_code == 503
    assert "results" not in failed.json()
    assert "detail" in failed.json()
    assert _received_by_gtin(_state(client, "PO-CEILKEY")) == {GTIN_A: ceiling}

    # The rolled-back batch did not occupy its key: resubmitting the same
    # key is a fresh attempt (another 503 while saturated), never a 409,
    # and a different array under the key is not a conflict either.
    retried = _scan(client, "PO-CEILKEY", [GTIN_A], key="ceil")
    assert retried.status_code == 503
    other = _scan(client, "PO-CEILKEY", [GTIN_A, GTIN_A], key="ceil")
    assert other.status_code == 503
    assert _received_by_gtin(_state(client, "PO-CEILKEY")) == {GTIN_A: ceiling}


# ---------------------------------------------------------------------------
# Keyless batches are unaffected
# ---------------------------------------------------------------------------


def test_keyless_scans_keep_accumulating(client: TestClient) -> None:
    _create(client, "PO-PLAIN", [[GTIN_A, 5]])
    for expected in (1, 2, 3):
        response = _scan(client, "PO-PLAIN", [GTIN_A])
        assert response.status_code == 200
        assert response.json()["results"][0]["reconciliation"][
            "received_qty"
        ] == expected
    assert _received_by_gtin(_state(client, "PO-PLAIN")) == {GTIN_A: 3}


def test_keyless_and_keyed_batches_interleave_correctly(
    client: TestClient,
) -> None:
    _create(client, "PO-INTERLEAVE", [[GTIN_A, 5]])
    keyed = _scan(client, "PO-INTERLEAVE", [GTIN_A], key="k")
    assert keyed.json()["results"][0]["reconciliation"]["received_qty"] == 1

    plain = _scan(client, "PO-INTERLEAVE", [GTIN_A])
    assert plain.json()["results"][0]["reconciliation"]["received_qty"] == 2

    # Replaying the keyed batch does not undo the keyless increment.
    replay = _scan(client, "PO-INTERLEAVE", [GTIN_A], key="k")
    assert replay.content == keyed.content
    assert _received_by_gtin(_state(client, "PO-INTERLEAVE")) == {GTIN_A: 2}


def test_keyed_batch_of_invalid_codes_replays_without_booking(
    client: TestClient,
) -> None:
    _create(client, "PO-ALLINVALID", [[GTIN_A, 5]])
    batch = [GTIN_BAD_CHECKSUM, GTIN_MALFORMED]
    first = _scan(client, "PO-ALLINVALID", batch, key="k")
    assert first.status_code == 200
    assert all(
        item["reconciliation"] is None for item in first.json()["results"]
    )

    replay = _scan(client, "PO-ALLINVALID", batch, key="k")
    assert replay.content == first.content
    assert _received_by_gtin(_state(client, "PO-ALLINVALID")) == {GTIN_A: 0}

    # A later valid scan still starts from zero.
    after = _scan(client, "PO-ALLINVALID", [GTIN_A])
    assert after.json()["results"][0]["reconciliation"]["received_qty"] == 1
