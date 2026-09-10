#!/usr/bin/env python3
"""One-shot acceptance check for the package-code receiving service.

Runs inside the ``verify`` Compose service once the API container is
healthy. It talks to the real HTTP service (stdlib only -- the verify
image does not need the test dependencies), independently recomputes the
expected check digit for every returned item, and exits non-zero on the
first discrepancy.

It audits both feature areas:

1. ``POST /codes/verify`` -- order/duplicate-preserving per-code verdicts
   and the 422 request boundary (unchanged contract).
2. Goods receipt -- create order (201/409/422), scan batches that count
   planned goods up to over-receipt (``matched`` -> ``excess``), recognise
   unplanned GTINs, never book malformed/checksum-bad codes, return 404 for
   unknown orders, and roll back the whole batch on storage failure so a
   retry produces exactly the same counts as if the failure never happened.

Usage (from the repository root):

    docker compose run --rm verify
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://api:8000")
VERIFY_URL = f"{API_BASE}/codes/verify"
RECEIPTS_URL = f"{API_BASE}/receipts"
HEALTH_URL = f"{API_BASE}/health"

GTIN_A = "07300040109316"   # valid, check digit 6
GTIN_B = "00000000000000"   # valid, check digit 0
GTIN_C = "00000000000017"   # valid, check digit 7 (unplanned in audits)
GTIN_BAD_CHECKSUM = "07300040109310"  # 14 digits, check digit should be 6
GTIN_MALFORMED = "0730004010-9316"    # hyphen: format error, never converted

MIXED_BATCH = [
    "07300040109316",   # valid (check digit 6)
    "07300040109316",   # valid duplicate, must be retained
    "07300040109310",   # checksum mismatch: computed digit 6, trailing 0
    "00000000000000",   # valid: zero weighted sum yields check digit 0
    "0730004010-9316",  # format error: hyphen, never converted
    " 07300040109316",  # format error: leading whitespace
    "０７３０００４０１０９３１６",  # format error: full-width digits
    "0730004010931",    # format error: 13 digits
    "073000401093166",  # format error: 15 digits
]

FAILURE_HEADER = {"X-Simulate-Storage-Failure": "1"}

failures: list[str] = []


def expected_check_digit(first_thirteen: str) -> int:
    """Independent re-implementation used to audit the service output."""
    weighted_sum = sum(
        int(digit) * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(first_thirteen)
    )
    return (10 - (weighted_sum % 10)) % 10


def wait_for_health(attempts: int = 30, delay_seconds: float = 1.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    print(f"api healthy after {attempt} attempt(s)")
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(delay_seconds)
    raise SystemExit(f"API at {API_BASE} did not become healthy")


def request_json(
    url: str,
    payload: object,
    method: str = "POST",
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def get_json(url: str) -> tuple[int, object]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def post_json(payload: object) -> tuple[int, object]:
    return request_json(VERIFY_URL, payload)


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)
        print(f"  FAIL: {message}")
    else:
        print(f"  ok:   {message}")


def audit_mixed_batch() -> None:
    print("== mixed batch: order, duplicates and per-item verdicts ==")
    status_code, payload = post_json(MIXED_BATCH)
    check(
        status_code == 200,
        f"HTTP 200 for structurally valid batch (got {status_code})",
    )
    if status_code != 200:
        return

    results = payload["results"]
    check(len(results) == len(MIXED_BATCH),
          f"{len(MIXED_BATCH)} items returned (got {len(results)})")
    check(
        [item["code"] for item in results] == MIXED_BATCH,
        "original codes preserved in input order, duplicates retained",
    )

    print()
    print(f"  {'#':>2}  {'code':<22} {'computed':>8}  status")
    for index, item in enumerate(results):
        code = item["code"]
        printed = code if code.isascii() else code.encode("unicode_escape").decode()
        print(f"  {index:>2}  {printed:<22} {str(item['calculated_check_digit']):>8}  "
              f"{item['status']}")

    for index, item in enumerate(results):
        code = item["code"]
        status = item["status"]
        digit = item["calculated_check_digit"]

        well_formed = len(code) == 14 and all("0" <= ch <= "9" for ch in code)
        if not well_formed:
            check(status == "format_error",
                  f"item {index}: malformed code reported as format_error")
            check(digit is None,
                  f"item {index}: format error has null calculated check digit")
            continue

        expected = expected_check_digit(code[:13])
        check(
            digit == expected,
            f"item {index}: check digit {digit} independently "
            f"recomputes to {expected}",
        )
        actual = ord(code[13]) - ord("0")
        expected_status = "valid" if actual == expected else "checksum_mismatch"
        check(status == expected_status,
              f"item {index}: status {status!r} matches recomputation "
              f"({expected_status!r})")


def audit_rejections() -> None:
    print()
    print("== request boundary: every malformed request is 422 with no results ==")
    cases = {
        "empty array": [],
        "101 codes": ["07300040109316"] * 101,
        "integer member": ["07300040109316", 123],
        "null member": ["07300040109316", None],
        "object body": {"codes": ["07300040109316"]},
        "string body": "07300040109316",
        "null body": None,
    }
    for label, payload in cases.items():
        status_code, body = post_json(payload)
        check(status_code == 422, f"{label}: HTTP 422 (got {status_code})")
        check(isinstance(body, dict) and "results" not in body,
              f"{label}: no partial 'results' field")
        check(isinstance(body, dict) and isinstance(body.get("detail"), list)
              and body["detail"],
              f"{label}: structured Pydantic 'detail' list present")

    print()
    print("== size boundary: exactly 100 codes is accepted ==")
    status_code, body = post_json(["07300040109316"] * 100)
    check(status_code == 200, f"100 codes: HTTP 200 (got {status_code})")
    check(status_code != 200 or len(body["results"]) == 100,
          "100 codes: all 100 results returned")


# ---------------------------------------------------------------------------
# Goods-receipt (purchase-order reconciliation) audits
# ---------------------------------------------------------------------------


def _create_order(
    order_no: str, lines: list[list]
) -> tuple[int, object]:
    return request_json(
        RECEIPTS_URL,
        {
            "order_no": order_no,
            "items": [
                {"gtin": gtin, "planned_qty": qty} for gtin, qty in lines
            ],
        },
    )


def _scan(
    order_no: str, codes: list, headers: dict[str, str] | None = None
) -> tuple[int, object]:
    return request_json(
        f"{RECEIPTS_URL}/{order_no}/scans", codes, headers=headers
    )


def _received_map(body: dict) -> dict[str, int]:
    return {item["gtin"]: item["received_qty"] for item in body["items"]}


def _summary(results: list[dict]) -> list:
    return [
        None if item.get("reconciliation") is None
        else (
            item["status"],
            item["reconciliation"]["conclusion"],
            item["reconciliation"]["planned_qty"],
            item["reconciliation"]["received_qty"],
        )
        for item in results
    ]


def audit_receipt_lifecycle() -> None:
    print()
    print("== goods receipt: create, duplicate 409 and 422 creation rules ==")
    order_no = f"ACC-{int(time.time())}"

    status_code, body = _create_order(order_no, [[GTIN_A, 2], [GTIN_B, 1]])
    check(status_code == 201, f"create order: HTTP 201 (got {status_code})")
    if status_code == 201:
        check(body["order_no"] == order_no, "created order echoes order_no")
        check(
            body["items"] == [
                {"gtin": GTIN_A, "planned_qty": 2, "received_qty": 0},
                {"gtin": GTIN_B, "planned_qty": 1, "received_qty": 0},
            ],
            "planned lines persisted with received_qty 0",
        )

    status_code, body = _create_order(order_no, [[GTIN_A, 2]])
    check(status_code == 409, f"duplicate order_no: HTTP 409 (got {status_code})")
    check("results" not in body, "409 body carries no 'results' field")

    invalid_creates = {
        "illegal GTIN (bad checksum)": {
            "order_no": order_no + "-bad1",
            "items": [{"gtin": GTIN_BAD_CHECKSUM, "planned_qty": 1}],
        },
        "illegal GTIN (malformed)": {
            "order_no": order_no + "-bad2",
            "items": [{"gtin": GTIN_MALFORMED, "planned_qty": 1}],
        },
        "duplicate GTIN lines": {
            "order_no": order_no + "-bad3",
            "items": [
                {"gtin": GTIN_A, "planned_qty": 1},
                {"gtin": GTIN_A, "planned_qty": 2},
            ],
        },
        "zero quantity": {
            "order_no": order_no + "-bad4",
            "items": [{"gtin": GTIN_A, "planned_qty": 0}],
        },
        "negative quantity": {
            "order_no": order_no + "-bad5",
            "items": [{"gtin": GTIN_A, "planned_qty": -3}],
        },
        "boolean quantity": {
            "order_no": order_no + "-bad6",
            "items": [{"gtin": GTIN_A, "planned_qty": True}],
        },
        "fractional quantity": {
            "order_no": order_no + "-bad7",
            "items": [{"gtin": GTIN_A, "planned_qty": 1.5}],
        },
        "empty items": {"order_no": order_no + "-bad8", "items": []},
    }
    for label, payload in invalid_creates.items():
        status_code, body = request_json(RECEIPTS_URL, payload)
        check(status_code == 422, f"{label}: HTTP 422 (got {status_code})")
        check(isinstance(body, dict) and "results" not in body,
              f"{label}: rejected whole, no partial order")

    print()
    print("== scans: matched increments, over-receipt, unplanned, invalid codes ==")
    status_code, body = _scan("NO-SUCH-ORDER", [GTIN_A])
    check(
        status_code == 404,
        f"scan against unknown order: HTTP 404 (got {status_code})",
    )
    status_code, _ = get_json(f"{RECEIPTS_URL}/NO-SUCH-ORDER")
    check(status_code == 404, "read unknown order: HTTP 404")

    for label, payload in (
        ("empty scan array", []),
        ("101 scan codes", [GTIN_A] * 101),
        ("non-string scan member", [123]),
    ):
        status_code, body = _scan(order_no, payload)
        check(status_code == 422, f"{label}: HTTP 422 (got {status_code})")
        check(isinstance(body, dict) and "results" not in body,
              f"{label}: no partial 'results'")

    # First batch: planned lines fill up to plan, an unplanned GTIN appears,
    # and two invalid codes must be ignored entirely.
    batch_one = [GTIN_A, GTIN_B, GTIN_BAD_CHECKSUM, GTIN_MALFORMED,
                 GTIN_C, GTIN_A]
    status_code, body = _scan(order_no, batch_one)
    check(status_code == 200, f"scan batch one: HTTP 200 (got {status_code})")
    if status_code == 200:
        results = body["results"]
        check([item["code"] for item in results] == batch_one,
              "scan results preserve full input order")
        check(_summary(results) == [
            ("valid", "matched", 2, 1),    # A within plan
            ("valid", "matched", 1, 1),    # B exactly at plan
            None,                           # checksum mismatch: not booked
            None,                           # format error: not booked
            ("valid", "unplanned", None, 1),  # C not on the purchase order
            ("valid", "matched", 2, 2),    # A reaches plan exactly
        ], "batch one reconciliation: matched/matched/null/null/unplanned/matched")
        check(all(item["reconciliation"] is None
                  for item in results if item["status"] != "valid"),
              "invalid codes carry no reconciliation conclusion")

    # Second batch: A now over-receives; the unplanned GTIN keeps
    # accumulating and stays unplanned; an invalid code still books nothing.
    batch_two = [GTIN_A, GTIN_A, GTIN_C, "abc"]
    status_code, body = _scan(order_no, batch_two)
    check(status_code == 200, f"scan batch two: HTTP 200 (got {status_code})")
    if status_code == 200:
        check(_summary(body["results"]) == [
            ("valid", "excess", 2, 3),
            ("valid", "excess", 2, 4),
            ("valid", "unplanned", None, 2),
            None,  # format error: not booked, no conclusion
        ], "batch two reconciliation: excess/excess/unplanned/null")

    status_code, state = get_json(f"{RECEIPTS_URL}/{order_no}")
    check(status_code == 200, "read cumulative receipt state: HTTP 200")
    if status_code == 200:
        check(
            _received_map(state) == {GTIN_A: 4, GTIN_B: 1, GTIN_C: 2},
            "cumulative counts: A=4 (2 over plan), B=1 (exact), C=2 (unplanned); "
            "invalid codes contributed 0",
        )
        planned = {item["gtin"]: item["planned_qty"] for item in state["items"]}
        check(planned == {GTIN_A: 2, GTIN_B: 1, GTIN_C: None},
              "unplanned line is stored with planned_qty null")


def audit_rollback_and_deterministic_retry() -> None:
    print()
    print("== storage failure: whole-batch rollback and deterministic retry ==")
    order_no = f"ACC-RB-{int(time.time())}"
    status_code, _ = _create_order(order_no, [[GTIN_A, 2]])
    check(status_code == 201, f"rollback order created: HTTP 201 (got {status_code})")

    batch = [GTIN_A, GTIN_A, GTIN_A]

    status_code, body = _scan(order_no, batch, headers=FAILURE_HEADER)
    check(status_code == 503,
          f"storage failure during scan: HTTP 503 (got {status_code})")
    check(isinstance(body, dict) and "results" not in body,
          "503 returns no partial results")

    _, state = get_json(f"{RECEIPTS_URL}/{order_no}")
    check(_received_map(state) == {GTIN_A: 0},
          "rolled back batch left received_qty at 0 (first increment undone)")

    # Retrying the identical batch without the fault must count exactly as if
    # the failed attempt had never happened: 1 matched, 2 matched, 3 excess.
    status_code, body = _scan(order_no, batch)
    check(status_code == 200, f"retried batch: HTTP 200 (got {status_code})")
    if status_code == 200:
        check(_summary(body["results"]) == [
            ("valid", "matched", 2, 1),
            ("valid", "matched", 2, 2),
            ("valid", "excess", 2, 3),
        ], "retry result is deterministic: matched/matched/excess, received 1/2/3")

    # A later failure must undo only its own batch, preserving the committed
    # count of 3; the retry then counts forward 4 and 5.
    status_code, _ = _scan(order_no, [GTIN_A, GTIN_A], headers=FAILURE_HEADER)
    check(status_code == 503, "second storage failure: HTTP 503")
    _, state = get_json(f"{RECEIPTS_URL}/{order_no}")
    check(_received_map(state) == {GTIN_A: 3},
          "earlier committed counts survive a later batch rollback")

    status_code, body = _scan(order_no, [GTIN_A, GTIN_A])
    check(
        status_code == 200,
        f"retry after second failure: HTTP 200 (got {status_code})",
    )
    if status_code == 200:
        check(_summary(body["results"]) == [
            ("valid", "excess", 2, 4),
            ("valid", "excess", 2, 5),
        ], "retry counts forward from the preserved baseline: received 4/5")
    _, state = get_json(f"{RECEIPTS_URL}/{order_no}")
    check(_received_map(state) == {GTIN_A: 5}, "final cumulative received_qty is 5")


def main() -> int:
    wait_for_health()
    audit_mixed_batch()
    audit_rejections()
    audit_receipt_lifecycle()
    audit_rollback_and_deterministic_retry()

    print()
    if failures:
        print(f"ACCEPTANCE FAILED with {len(failures)} failure(s)")
        return 1
    print("ACCEPTANCE PASSED: per-code verdicts are order-preserving and")
    print("recomputable; planned receipts increment to matched/excess, unplanned")
    print("goods are identified, invalid codes never book, and rolled-back")
    print("batches retry with deterministic counts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
