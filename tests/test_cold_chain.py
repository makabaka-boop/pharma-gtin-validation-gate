"""HTTP-level tests for cold-chain temperature assessments."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app

GTIN_A = "07300040109316"  # valid, check digit 6

BASE = datetime(2026, 9, 1, 8, 0, 0, tzinfo=UTC)


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


def _iso(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _samples(offsets_minutes: list[float], temps: list[float]) -> list[dict]:
    return [
        {
            "recorded_at": _iso(BASE + timedelta(minutes=offset)),
            "temperature": temp,
        }
        for offset, temp in zip(offsets_minutes, temps)
    ]


def _create_receipt(client: TestClient, order_no: str = "PO-CC") -> None:
    response = client.post(
        "/receipts",
        json={"order_no": order_no,
              "items": [{"gtin": GTIN_A, "planned_qty": 5}]},
    )
    assert response.status_code == 201


def _assess(
    client: TestClient,
    assessment_id: str = "CC-1",
    order_no: str = "PO-CC",
    min_temp: float = 2.0,
    max_temp: float = 8.0,
    samples: list[dict] | None = None,
):
    if samples is None:
        samples = _samples([0, 60], [5.0, 6.0])
    return client.post(
        "/cold-chain-assessments",
        json={
            "assessment_id": assessment_id,
            "order_no": order_no,
            "min_temp": min_temp,
            "max_temp": max_temp,
            "samples": samples,
        },
    )


# ---------------------------------------------------------------------------
# Happy path: compliant transport and multi-segment excursions
# ---------------------------------------------------------------------------


def test_compliant_assessment_has_no_segments_and_persists(
    client: TestClient,
) -> None:
    _create_receipt(client)
    samples = _samples([0, 60, 120], [4.5, 8.0, 2.0])  # edges are in-zone
    response = _assess(client, samples=samples)
    assert response.status_code == 201
    body = response.json()
    assert body["assessment_id"] == "CC-1"
    assert body["order_no"] == "PO-CC"
    assert body["min_temp"] == 2.0
    assert body["max_temp"] == 8.0
    assert body["samples"] == samples  # raw samples preserved as submitted
    assert body["summary"] == {
        "sample_count": 3,
        "span_minutes": 120.0,
        "out_of_range_samples": 0,
        "segment_count": 0,
        "total_duration_minutes": 0.0,
        "total_degree_minutes": 0.0,
        "conclusion": "compliant",
        "segments": [],
    }
    # The persisted document is served identically on read.
    reread = client.get("/cold-chain-assessments/CC-1")
    assert reread.status_code == 200
    assert reread.json() == body


def test_multiple_excursion_segments_are_merged_and_integrated(
    client: TestClient,
) -> None:
    _create_receipt(client)
    # Zone [2, 8]; two hot samples, one in-zone, two cold samples.
    samples = _samples(
        [0, 30, 60, 90, 120, 150, 180],
        [5.0, 9.0, 10.0, 6.0, 1.0, 0.5, 4.0],
    )
    response = _assess(client, samples=samples)
    assert response.status_code == 201
    summary = response.json()["summary"]

    # Segment 1: t=30..60, deviations 1 -> 2 above max.
    #   trapezoid (1 + 2) / 2 * 30 min = 45.0 degree-minutes.
    # Segment 2: t=120..150, deviations 1 -> 1.5 below min.
    #   trapezoid (1 + 1.5) / 2 * 30 min = 37.5 degree-minutes.
    assert summary["segments"] == [
        {
            "start": samples[1]["recorded_at"],
            "end": samples[2]["recorded_at"],
            "duration_minutes": 30.0,
            "degree_minutes": 45.0,
            "sample_count": 2,
            "peak_deviation": 2.0,
        },
        {
            "start": samples[4]["recorded_at"],
            "end": samples[5]["recorded_at"],
            "duration_minutes": 30.0,
            "degree_minutes": 37.5,
            "sample_count": 2,
            "peak_deviation": 1.5,
        },
    ]
    assert summary["sample_count"] == 7
    assert summary["span_minutes"] == 180.0
    assert summary["out_of_range_samples"] == 4
    assert summary["segment_count"] == 2
    # Totals are exactly the sums of the displayed segment values.
    assert summary["total_duration_minutes"] == 60.0
    assert summary["total_degree_minutes"] == 82.5
    assert summary["conclusion"] == "excursion"

    # Read consistency: GET returns the very same deterministic document.
    reread = client.get("/cold-chain-assessments/CC-1")
    assert reread.status_code == 200
    assert reread.json() == response.json()


def test_single_out_of_range_sample_forms_zero_duration_segment(
    client: TestClient,
) -> None:
    _create_receipt(client)
    # One isolated breach spans no time: duration and degree-minutes are 0,
    # but the excursion is still recorded and concluded.
    samples = _samples([0, 10, 20], [5.0, 9.0, 5.0])
    response = _assess(client, samples=samples)
    assert response.status_code == 201
    summary = response.json()["summary"]
    assert summary["conclusion"] == "excursion"
    assert summary["segments"] == [
        {
            "start": samples[1]["recorded_at"],
            "end": samples[1]["recorded_at"],
            "duration_minutes": 0.0,
            "degree_minutes": 0.0,
            "sample_count": 1,
            "peak_deviation": 1.0,
        }
    ]
    assert summary["total_duration_minutes"] == 0.0
    assert summary["total_degree_minutes"] == 0.0


def test_computed_values_are_rounded_to_two_decimals(
    client: TestClient,
) -> None:
    _create_receipt(client)
    # One second of span: 1/60 minute = 0.01666... -> 0.02.
    samples = [
        {"recorded_at": _iso(BASE), "temperature": 9.0},
        {"recorded_at": _iso(BASE + timedelta(seconds=1)), "temperature": 9.0},
    ]
    response = _assess(client, samples=samples)
    assert response.status_code == 201
    summary = response.json()["summary"]
    assert summary["span_minutes"] == 0.02
    assert summary["segments"][0]["duration_minutes"] == 0.02
    assert summary["segments"][0]["degree_minutes"] == 0.02
    assert summary["total_degree_minutes"] == 0.02


def test_timestamps_with_different_offsets_compare_by_instant(
    client: TestClient,
) -> None:
    _create_receipt(client)
    # 10:00+02:00 is 08:00Z; the series advances by one hour per sample.
    samples = [
        {"recorded_at": "2026-09-01T10:00:00+02:00", "temperature": 5.0},
        {"recorded_at": "2026-09-01T09:00:00Z", "temperature": 9.0},
        {"recorded_at": "2026-09-01T10:00:00Z", "temperature": 9.0},
    ]
    response = _assess(client, samples=samples)
    assert response.status_code == 201
    summary = response.json()["summary"]
    assert summary["span_minutes"] == 120.0
    assert summary["segments"][0]["duration_minutes"] == 60.0
    assert summary["segments"][0]["degree_minutes"] == 60.0  # (1+1)/2 * 60


def test_assessment_links_legacy_padded_receipt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Receipts stored before order-number canonicalisation still accept
    # assessments; the response carries the canonical order number.
    original = storage._canonical_order_no
    storage._canonical_order_no = lambda value: value
    try:
        _create_receipt(client, "  PO-LEGACY-CC  ")
    finally:
        storage._canonical_order_no = original

    response = _assess(client, order_no="PO-LEGACY-CC")
    assert response.status_code == 201
    assert response.json()["order_no"] == "PO-LEGACY-CC"


# ---------------------------------------------------------------------------
# Error envelope: 409 / 404 / 422
# ---------------------------------------------------------------------------


def test_duplicate_assessment_id_is_409(client: TestClient) -> None:
    _create_receipt(client)
    assert _assess(client, "CC-DUP").status_code == 201
    response = _assess(client, "CC-DUP")
    assert response.status_code == 409
    body = response.json()
    assert "detail" in body
    assert "results" not in body
    # The first assessment is untouched by the rejected duplicate.
    assert client.get("/cold-chain-assessments/CC-DUP").status_code == 200


def test_whitespace_padded_assessment_id_is_a_duplicate_409(
    client: TestClient,
) -> None:
    _create_receipt(client)
    assert _assess(client, "CC-WS").status_code == 201
    assert _assess(client, "  CC-WS  ").status_code == 409
    # The canonical number addresses the stored assessment.
    assert client.get("/cold-chain-assessments/CC-WS").status_code == 200
    assert client.get("/cold-chain-assessments/%20CC-WS%20").status_code == 200


def test_unknown_order_no_is_404(client: TestClient) -> None:
    response = _assess(client, order_no="NO-SUCH-ORDER")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_get_unknown_assessment_is_404(client: TestClient) -> None:
    response = client.get("/cold-chain-assessments/NO-SUCH-ASSESSMENT")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_invalid_temperature_zone_is_422(client: TestClient) -> None:
    _create_receipt(client)
    for zone in ((8.0, 2.0), (5.0, 5.0)):
        response = _assess(client, min_temp=zone[0], max_temp=zone[1])
        assert response.status_code == 422, zone
        assert isinstance(response.json()["detail"], list)


def test_non_finite_zone_or_temperature_is_422(client: TestClient) -> None:
    _create_receipt(client)
    samples = _samples([0, 60], [5.0, 6.0])

    def post_raw(assessment_id: str, body: str):
        return client.post(
            "/cold-chain-assessments",
            content=body.replace("__ID__", assessment_id),
            headers={"content-type": "application/json"},
        )

    template = (
        '{"assessment_id": "__ID__", "order_no": "PO-CC", '
        f'"min_temp": 2.0, "max_temp": 8.0, "samples": {json.dumps(samples)}}}'
    )
    for bad_value in ("NaN", "Infinity", "-Infinity"):
        for field, original in (("min_temp", "2.0"), ("max_temp", "8.0")):
            response = post_raw(
                "CC-NAN",
                template.replace(f'"{field}": {original}',
                                 f'"{field}": {bad_value}'),
            )
            assert response.status_code == 422, (field, bad_value)
    # A NaN sample temperature is rejected the same way.
    response = post_raw("CC-NAN", template.replace("5.0", "NaN"))
    assert response.status_code == 422


def test_insufficient_samples_are_422(client: TestClient) -> None:
    _create_receipt(client)
    assert _assess(client, samples=[]).status_code == 422
    assert _assess(client, samples=_samples([0], [5.0])).status_code == 422


def test_non_strictly_increasing_times_are_422(client: TestClient) -> None:
    _create_receipt(client)
    # Equal instants are not strictly increasing.
    assert _assess(client, samples=_samples([0, 0], [5.0, 6.0])).status_code == 422
    # Going backwards is rejected too.
    assert _assess(client, samples=_samples([60, 0], [5.0, 6.0])).status_code == 422
    # Same instant expressed with a different offset is still equal.
    samples = [
        {"recorded_at": "2026-09-01T08:00:00Z", "temperature": 5.0},
        {"recorded_at": "2026-09-01T10:00:00+02:00", "temperature": 6.0},
    ]
    assert _assess(client, samples=samples).status_code == 422


def test_span_over_seven_days_is_422_exactly_seven_days_ok(
    client: TestClient,
) -> None:
    _create_receipt(client)
    seven_days = 7 * 24 * 60
    over = _assess(client, "CC-OVER", samples=_samples([0, seven_days + 1], [5.0, 6.0]))
    assert over.status_code == 422
    exact = _assess(client, "CC-EXACT", samples=_samples([0, seven_days], [5.0, 6.0]))
    assert exact.status_code == 201
    assert exact.json()["summary"]["span_minutes"] == float(seven_days)


def test_naive_timestamp_is_422(client: TestClient) -> None:
    _create_receipt(client)
    samples = [
        {"recorded_at": "2026-09-01T08:00:00", "temperature": 5.0},
        {"recorded_at": "2026-09-01T09:00:00", "temperature": 6.0},
    ]
    assert _assess(client, samples=samples).status_code == 422


def test_assessment_id_with_slash_or_blank_is_422(client: TestClient) -> None:
    _create_receipt(client)
    assert _assess(client, "CC/2026/0001").status_code == 422
    assert _assess(client, "   ").status_code == 422


def test_structural_422_precedes_order_lookup(client: TestClient) -> None:
    # Pydantic request validation runs before the endpoint, so an invalid
    # body answers 422 even when the order does not exist.
    response = _assess(client, order_no="GONE", samples=_samples([0], [5.0]))
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Failed requests leave no records behind
# ---------------------------------------------------------------------------


def test_failed_requests_leave_no_residual_records(client: TestClient) -> None:
    _create_receipt(client)
    failing_bodies = [
        # invalid zone
        {"assessment_id": "CC-F1", "order_no": "PO-CC", "min_temp": 9.0,
         "max_temp": 2.0, "samples": _samples([0, 60], [5.0, 6.0])},
        # insufficient samples
        {"assessment_id": "CC-F2", "order_no": "PO-CC", "min_temp": 2.0,
         "max_temp": 8.0, "samples": _samples([0], [5.0])},
        # non-strictly increasing time
        {"assessment_id": "CC-F3", "order_no": "PO-CC", "min_temp": 2.0,
         "max_temp": 8.0, "samples": _samples([0, 0], [5.0, 6.0])},
        # span over seven days
        {"assessment_id": "CC-F4", "order_no": "PO-CC", "min_temp": 2.0,
         "max_temp": 8.0, "samples": _samples([0, 8 * 24 * 60], [5.0, 6.0])},
        # unknown receipt
        {"assessment_id": "CC-F5", "order_no": "PO-GONE", "min_temp": 2.0,
         "max_temp": 8.0, "samples": _samples([0, 60], [5.0, 6.0])},
    ]
    for body in failing_bodies:
        response = client.post("/cold-chain-assessments", json=body)
        assert response.status_code in (404, 422), body["assessment_id"]

    # A duplicate attempt fails too, after one assessment succeeded.
    assert _assess(client, "CC-KEEP").status_code == 201
    assert _assess(client, "CC-KEEP").status_code == 409

    # No failed request left anything behind: every failed number is still
    # unknown, and reusing one of them succeeds cleanly and completely.
    for body in failing_bodies:
        fetched = client.get(f"/cold-chain-assessments/{body['assessment_id']}")
        assert fetched.status_code == 404, body["assessment_id"]

    reused = client.post(
        "/cold-chain-assessments",
        json={**failing_bodies[2], "samples": _samples([0, 30, 60], [9.0, 9.0, 5.0])},
    )
    assert reused.status_code == 201
    summary = reused.json()["summary"]
    assert summary["sample_count"] == 3
    assert summary["segments"][0]["degree_minutes"] == 30.0  # (1+1)/2 * 30
    # The stored record contains exactly the reused request's samples --
    # no orphaned rows from the earlier failed attempts.
    assert client.get(
        "/cold-chain-assessments/CC-F3"
    ).json() == reused.json()

    # Only the successful assessments were ever persisted: the kept one and
    # the reused number. No failed request left an assessment or sample row.
    stored = storage._get_connection().execute(
        "SELECT assessment_id FROM cold_chain_assessments"
    ).fetchall()
    assert sorted(row["assessment_id"] for row in stored) == ["CC-F3", "CC-KEEP"]
    sample_rows = storage._get_connection().execute(
        "SELECT assessment_id, COUNT(*) AS n FROM cold_chain_samples "
        "GROUP BY assessment_id"
    ).fetchall()
    assert {row["assessment_id"]: row["n"] for row in sample_rows} == {
        "CC-F3": 3,
        "CC-KEEP": 2,
    }
