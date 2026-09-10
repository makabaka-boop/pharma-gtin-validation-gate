"""FastAPI service entry point for pharmaceutical goods receipt.

Request boundary (fixed with Pydantic): the body must be a bare JSON array
of 1--100 strings. An empty array, an oversized array, a non-string member
or a non-array body all fail request validation with HTTP 422 and *no*
``results`` field. A structurally valid request always answers 200, even
when every code it carries is invalid.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Body, FastAPI
from pydantic import BaseModel

from .gtin14 import CodeResult, evaluate_code

MAX_CODES: int = 100

Status = Literal["valid", "format_error", "checksum_mismatch"]

app = FastAPI(
    title="Package Code Receiving API",
    version="1.0.0",
    description=(
        "Receive scanned GTIN-14 package codes for pharmaceutical goods "
        "receipt and return an order-preserving, duplicate-preserving "
        "verdict for each code."
    ),
)


class CodeItem(BaseModel):
    """Verdict for one input item."""

    code: str
    calculated_check_digit: int | None
    status: Status


class VerifyResponse(BaseModel):
    """Wrapper object; present only for structurally valid requests."""

    results: list[CodeItem]


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe used by the Docker Compose healthcheck."""
    return {"status": "ok"}


@app.post(
    "/codes/verify",
    response_model=VerifyResponse,
    tags=["codes"],
    summary="Verify a batch of 1 to 100 raw package codes",
)
def verify_codes(
    codes: Annotated[
        list[str],
        Body(
            min_length=1,
            max_length=MAX_CODES,
            description="Bare JSON array of 1-100 raw package code strings.",
        ),
    ],
) -> VerifyResponse:
    items: list[CodeItem] = []
    for code in codes:  # input order and duplicates are preserved as-is
        result: CodeResult = evaluate_code(code)
        items.append(
            CodeItem(
                code=result.code,
                calculated_check_digit=result.calculated_check_digit,
                status=result.status,  # type: ignore[arg-type]
            )
        )
    return VerifyResponse(results=items)
