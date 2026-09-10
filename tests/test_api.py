"""HTTP-level tests for the package-code receiving API."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import MAX_CODES, app

client = TestClient(app)


def _post(body: object) -> object:
    return client.post("/codes/verify", json=body)


def test_mixed_batch_is_order_and_duplicate_preserving() -> None:
    codes = [
        "07300040109316",  # valid
        "07300040109316",  # valid duplicate, retained
        "07300040109310",  # checksum mismatch (computed digit 6)
        "0730004010-9316",  # format error: hyphen, not stripped
        " 07300040109316",  # format error: leading whitespace
        "０７３０００４０１０９３１６",  # format error: full-width digits
        "0730004010931",  # format error: only 13 digits
    ]
    response = _post(codes)

    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["code"] for item in results] == codes  # order & duplicates

    expected = [
        ("valid", 6),
        ("valid", 6),
        ("checksum_mismatch", 6),
        ("format_error", None),
        ("format_error", None),
        ("format_error", None),
        ("format_error", None),
    ]
    assert [
        (item["status"], item["calculated_check_digit"]) for item in results
    ] == expected


def test_single_valid_and_single_invalid_code() -> None:
    response = _post(["00000000000000"])
    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"code": "00000000000000",
             "calculated_check_digit": 0,
             "status": "valid"}
        ]
    }

    response = _post(["abc"])
    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"code": "abc",
             "calculated_check_digit": None,
             "status": "format_error"}
        ]
    }


def test_lower_bound_exactly_one_code_is_accepted() -> None:
    response = _post(["07300040109316"])
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


def test_upper_bound_exactly_max_codes_are_accepted() -> None:
    response = _post(["07300040109316"] * MAX_CODES)
    assert response.status_code == 200
    assert len(response.json()["results"]) == MAX_CODES


def test_empty_array_is_rejected_with_422_and_no_results() -> None:
    response = _post([])
    assert response.status_code == 422
    body = response.json()
    assert "results" not in body
    # Pydantic structured error details
    assert "detail" in body
    assert body["detail"][0]["type"] == "too_short"


def test_more_than_max_codes_is_rejected_with_422() -> None:
    response = _post(["07300040109316"] * (MAX_CODES + 1))
    assert response.status_code == 422
    body = response.json()
    assert "results" not in body
    assert body["detail"][0]["type"] == "too_long"


def test_non_string_members_are_rejected_with_422() -> None:
    for bad_member in (123, 1.5, True, None, ["07300040109316"], {}):
        response = _post(["07300040109316", bad_member])
        assert response.status_code == 422, bad_member
        assert "results" not in response.json()


def test_non_array_body_is_rejected_with_422() -> None:
    for body in ({}, {"codes": ["07300040109316"]}, "07300040109316", None, 42):
        response = _post(body)
        assert response.status_code == 422, body
        assert "results" not in response.json()


def test_malformed_json_is_rejected_with_422() -> None:
    response = client.post(
        "/codes/verify",
        content='["07300040109316",',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert "results" not in response.json()


def test_missing_body_is_rejected_with_422() -> None:
    response = client.post("/codes/verify")
    assert response.status_code == 422
    assert "results" not in response.json()


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
