"""Unit tests for the GTIN-14 check digit formula and per-code rules."""
from __future__ import annotations

import pytest

from app.gtin14 import (
    STATUS_CHECKSUM_MISMATCH,
    STATUS_FORMAT_ERROR,
    STATUS_VALID,
    calculate_check_digit,
    evaluate_code,
)

# (first 13 digits, expected check digit)
# Weights from the left: 3,1,3,1,...,3 on positions 1..13.
FORMULA_CASES = [
    ("0730004010931", 6),  # full valid code 07300040109316 (README example)
    ("0000000000000", 0),  # zero weighted sum -> (10-0)%10 == 0, not 10
    ("1111111111111", 3),  # 7*3 + 6*1 = 27 -> (10-7)%10
    ("1234567890123", 1),  # weighted sum 109
    ("0000000000001", 7),  # last position is weighted 3: 1*3 = 3
    ("9999999999999", 7),  # 7*9*3 + 6*9 = 243
]


@pytest.mark.parametrize("first_thirteen,expected", FORMULA_CASES)
def test_formula(first_thirteen: str, expected: int) -> None:
    assert calculate_check_digit(first_thirteen) == expected


def test_weights_alternate_3_and_1_from_the_left() -> None:
    # digits in odd positions (1,3,...,13) are multiplied by 3,
    # digits in even positions (2,4,...,12) by 1.
    assert calculate_check_digit("1000000000000") == 7   # 1*3 = 3
    assert calculate_check_digit("0100000000000") == 9   # 1*1 = 1


def test_evaluate_valid_code() -> None:
    result = evaluate_code("07300040109316")
    assert result.status == STATUS_VALID
    assert result.calculated_check_digit == 6
    assert result.code == "07300040109316"


def test_evaluate_checksum_mismatch() -> None:
    # format is fine; trailing digit 0 does not equal computed 6
    result = evaluate_code("07300040109310")
    assert result.status == STATUS_CHECKSUM_MISMATCH
    assert result.calculated_check_digit == 6


def test_all_zero_code_is_valid() -> None:
    result = evaluate_code("00000000000000")
    assert result.status == STATUS_VALID
    assert result.calculated_check_digit == 0


@pytest.mark.parametrize(
    "raw",
    [
        " 07300040109316",   # leading whitespace
        "07300040109316 ",   # trailing whitespace
        "07300040109 16",    # internal whitespace
        "0730004010-9316",   # hyphen
        "０７３０００４０１０９３１６",  # full-width digits
        "0730004010931",     # 13 digits
        "073000401093166",   # 15 digits
        "0730004010931a",    # trailing letter
        "abcdefghijklmn",    # all letters
        "0730004010931.6",   # punctuation
        "",                   # empty string
    ],
)
def test_format_errors_are_not_normalised(raw: str) -> None:
    result = evaluate_code(raw)
    assert result.status == STATUS_FORMAT_ERROR
    assert result.calculated_check_digit is None
    # original value is echoed back untouched
    assert result.code == raw


def test_non_string_member_is_format_error() -> None:
    # Pydantic normally rejects these at the boundary, but the domain
    # function must remain defensive on its own.
    result = evaluate_code(12345678901234)  # type: ignore[arg-type]
    assert result.status == STATUS_FORMAT_ERROR
    assert result.calculated_check_digit is None
