"""GTIN-14 package-code domain rules.

A scanned package code is accepted **only** when it is exactly fourteen
ASCII digits. Whitespace, hyphens, full-width digits (U+FF10..U+FF19) and
any other character are never stripped, converted or normalised -- such an
item is reported as ``format_error``.
"""
from __future__ import annotations

import re
from typing import NamedTuple

CODE_LENGTH: int = 14

# ``[0-9]`` deliberately instead of ``\\d``: the latter also matches Unicode
# digit characters such as full-width digits, which must be rejected.
_ASCII_FOURTEEN_DIGITS = re.compile(r"[0-9]{14}")

# Left-to-right weights for positions 1..13: 3 on odd positions, 1 on even.
_WEIGHTS = (3, 1)

STATUS_VALID = "valid"
STATUS_FORMAT_ERROR = "format_error"
STATUS_CHECKSUM_MISMATCH = "checksum_mismatch"


class CodeResult(NamedTuple):
    """Verdict for a single code.

    ``calculated_check_digit`` is ``None`` whenever the format is invalid,
    because a check digit cannot be computed from non-conforming input.
    """

    code: str
    calculated_check_digit: int | None
    status: str


def calculate_check_digit(first_thirteen: str) -> int:
    """Compute the GTIN-14 check digit from the first 13 ASCII digits.

    Positions 1..13 are weighted 3, 1, 3, 1, ... from left to right::

        check = (10 - (weighted_sum % 10)) % 10

    The final ``% 10`` turns the modulo-zero case into ``0`` rather than
    ``10``. Callers are expected to have already confirmed the format;
    this function performs no conversion itself.
    """
    weighted_sum = sum(
        int(digit) * _WEIGHTS[index % 2]
        for index, digit in enumerate(first_thirteen)
    )
    return (10 - (weighted_sum % 10)) % 10


def evaluate_code(code: str) -> CodeResult:
    """Return the verdict for one raw, untransformed code string."""
    if not isinstance(code, str) or _ASCII_FOURTEEN_DIGITS.fullmatch(code) is None:
        return CodeResult(
            code=code,
            calculated_check_digit=None,
            status=STATUS_FORMAT_ERROR,
        )

    calculated = calculate_check_digit(code[: CODE_LENGTH - 1])
    actual = ord(code[CODE_LENGTH - 1]) - ord("0")
    status = STATUS_VALID if actual == calculated else STATUS_CHECKSUM_MISMATCH
    return CodeResult(
        code=code,
        calculated_check_digit=calculated,
        status=status,
    )
