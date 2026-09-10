#!/usr/bin/env python3
"""One-shot acceptance check for the package-code receiving service.

Runs inside the ``verify`` Compose service once the API container is
healthy. It talks to the real HTTP service (stdlib only -- the verify
image does not need the test dependencies), independently recomputes the
expected check digit for every returned item, and exits non-zero on the
first discrepancy.

Usage (from the repository root):

    docker compose run --rm verify
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://api:8000")
VERIFY_URL = f"{API_BASE}/codes/verify"
HEALTH_URL = f"{API_BASE}/health"

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

failures: list[str] = []


def expected_check_digit(first_thirteen: str) -> int:
    """Independent re-implementation used to audit the service output."""
    weighted_sum = sum(
        int(digit) * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(first_thirteen)
    )
    return (10 - (weighted_sum % 10)) % 10


def wait_for_health(attempts: int = 30, delay_seconds: float = 1.0) -> None:
    import time

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


def post_json(payload: object) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        VERIFY_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)
        print(f"  FAIL: {message}")
    else:
        print(f"  ok:   {message}")


def audit_mixed_batch() -> None:
    print("== mixed batch: order, duplicates and per-item verdicts ==")
    status_code, payload = post_json(MIXED_BATCH)
    check(status_code == 200, f"HTTP 200 for structurally valid batch (got {status_code})")
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
        check(digit == expected,
              f"item {index}: check digit {digit} independently recomputes to {expected}")
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


def main() -> int:
    wait_for_health()
    audit_mixed_batch()
    audit_rejections()

    print()
    if failures:
        print(f"ACCEPTANCE FAILED with {len(failures)} failure(s)")
        return 1
    print("ACCEPTANCE PASSED: every package code has a unique, order-preserving,")
    print("recomputable release verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
