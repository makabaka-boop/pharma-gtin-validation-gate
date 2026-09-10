"""FastAPI service entry point for pharmaceutical goods receipt.

Request boundary (fixed with Pydantic): the body must be a bare JSON array
of 1--100 strings. An empty array, an oversized array, a non-string member
or a non-array body all fail request validation with HTTP 422 and *no*
``results`` field. A structurally valid request always answers 200, even
when every code it carries is invalid.

Goods receipt (purchase-order reconciliation):

* ``POST /receipts`` creates a receipt from a unique business order number
  and planned ``(GTIN, positive quantity)`` lines.
* ``POST /receipts/{order_no}/scans`` performs the same per-code validation
  as ``/codes/verify`` and, in one SQLite transaction, books every valid
  code in input order, attaching a ``matched`` / ``excess`` / ``unplanned``
  conclusion. Invalid codes are neither booked nor concluded.
* ``GET /receipts/{order_no}`` returns the cumulative planned/received
  state used to prove rollback determinism.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator

from .gtin14 import STATUS_VALID, CodeResult, evaluate_code
from .storage import (
    OrderAlreadyExists,
    PlannedLine,
    ReceiptState,
    Reconciliation,
    StorageUnavailable,
    create_receipt,
    get_receipt,
    init_db,
    record_scan_batch,
)

MAX_CODES: int = 100
MAX_LINES: int = 100
# SQLite INTEGER is a signed 64-bit value; a planned quantity outside that
# range cannot be persisted, so it must fail request validation (422)
# instead of surfacing as an unhandled OverflowError (500) at insert time.
MAX_PLANNED_QTY: int = 2**63 - 1

Status = Literal["valid", "format_error", "checksum_mismatch"]
Conclusion = Literal["matched", "excess", "unplanned"]

# The storage-failure test seam is inert unless explicitly enabled, so a
# stray header in production can never destroy a batch. The acceptance
# stack enables it (see docker-compose.yml).
_FAILURE_INJECTION_ENABLED = (
    os.environ.get("RECEIPT_ENABLE_FAILURE_INJECTION", "").strip().lower()
    in {"1", "true", "yes", "on"}
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create required tables at startup; safe on a pre-existing database."""
    init_db()
    yield


app = FastAPI(
    title="Package Code Receiving API",
    version="1.1.0",
    description=(
        "Receive scanned GTIN-14 package codes for pharmaceutical goods "
        "receipt and return an order-preserving, duplicate-preserving "
        "verdict for each code, with purchase-order reconciliation for "
        "created receipt orders."
    ),
    lifespan=lifespan,
)


@app.exception_handler(StorageUnavailable)
def _storage_unavailable_handler(_, exc: StorageUnavailable) -> JSONResponse:
    # The whole batch was rolled back; the client may safely retry it.
    return JSONResponse(
        status_code=503,
        content={"detail": f"Storage temporarily unavailable: {exc}"},
        headers={"Retry-After": "1"},
    )


class CodeItem(BaseModel):
    """Verdict for one input item."""

    code: str
    calculated_check_digit: int | None
    status: Status


class VerifyResponse(BaseModel):
    """Wrapper object; present only for structurally valid requests."""

    results: list[CodeItem]


# ---------------------------------------------------------------------------
# Goods-receipt models
# ---------------------------------------------------------------------------


class PlannedLineIn(BaseModel):
    """One planned purchase-order line: a valid GTIN and a positive qty."""

    gtin: Annotated[str, Field(min_length=1, max_length=128)]
    planned_qty: Annotated[StrictInt, Field(gt=0, le=MAX_PLANNED_QTY)]


class CreateReceiptIn(BaseModel):
    """Body for ``POST /receipts``."""

    order_no: Annotated[str, Field(min_length=1, max_length=128)]
    items: Annotated[list[PlannedLineIn], Field(min_length=1, max_length=MAX_LINES)]

    @model_validator(mode="after")
    def validate_business_rules(self) -> CreateReceiptIn:
        if not self.order_no.strip():
            raise ValueError("order_no must contain at least one non-blank char")
        if "/" in self.order_no:
            # The order number is addressed as a single URL path segment;
            # with a slash the receipt would be created but unreachable
            # (every read/scan would 404), so reject it up front.
            raise ValueError("order_no must not contain '/'")
        gtins = [item.gtin for item in self.items]
        if len(set(gtins)) != len(gtins):
            raise ValueError("items must not contain duplicate GTINs")
        for index, item in enumerate(self.items):
            if evaluate_code(item.gtin).status != STATUS_VALID:
                raise ValueError(f"items[{index}].gtin is not a valid GTIN-14")
        return self


class PlannedLineOut(BaseModel):
    """One line in the cumulative receipt state."""

    gtin: str
    planned_qty: int | None
    received_qty: int


class ReceiptCreatedOut(BaseModel):
    """Acknowledgement returned after a receipt is created."""

    order_no: str
    items: list[PlannedLineOut]


class ReceiptStateOut(BaseModel):
    """Cumulative state of one receipt (planned lines plus unplanned ones)."""

    order_no: str
    items: list[PlannedLineOut]


class ReconciliationOut(BaseModel):
    """Booking conclusion attached to valid scans; null on invalid codes."""

    conclusion: Conclusion
    planned_qty: int | None
    received_qty: int


class ScanItem(BaseModel):
    """Per-code verdict for a receipt scan, with reconciliation appended."""

    code: str
    calculated_check_digit: int | None
    status: Status
    reconciliation: ReconciliationOut | None = None


class ScanResponse(BaseModel):
    """Same per-code results as /codes/verify, plus reconciliation."""

    results: list[ScanItem]


def _line_out(line: PlannedLine) -> PlannedLineOut:
    return PlannedLineOut(
        gtin=line.gtin,
        planned_qty=line.planned_qty,
        received_qty=line.received_qty,
    )


def _state_out(state: ReceiptState) -> ReceiptStateOut:
    return ReceiptStateOut(
        order_no=state.order_no,
        items=[_line_out(line) for line in state.items],
    )


def _failure_injection_requested(value: str | None) -> bool:
    return (
        _FAILURE_INJECTION_ENABLED
        and value is not None
        and value.strip().lower() in {"1", "true", "yes"}
    )


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


@app.post(
    "/receipts",
    response_model=ReceiptCreatedOut,
    status_code=201,
    tags=["receipts"],
    summary="Create a receipt order from planned GTIN quantities",
)
def create_receipt_order(payload: CreateReceiptIn) -> ReceiptCreatedOut:
    """Create a receipt with planned lines.

    A duplicate business order number (compared with surrounding whitespace
    stripped) answers 409; illegal GTINs, repeated GTINs, quantities outside
    the positive SQLite-integer range or an order number containing ``/``
    fail validation as a whole with 422.
    """
    try:
        create_receipt(
            payload.order_no,
            [(item.gtin, item.planned_qty) for item in payload.items],
        )
    except OrderAlreadyExists:
        # 409, not 422: the body itself is valid, only the number repeats.
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={"detail": f"order_no {payload.order_no!r} already exists"},
        )
    state = get_receipt(payload.order_no)
    assert state is not None  # just created above
    return ReceiptCreatedOut(
        order_no=state.order_no, items=[_line_out(line) for line in state.items]
    )


@app.post(
    "/receipts/{order_no}/scans",
    response_model=ScanResponse,
    tags=["receipts"],
    summary="Scan codes against a receipt and book received quantities",
)
def scan_for_receipt(
    order_no: str,
    codes: Annotated[
        list[str],
        Body(
            min_length=1,
            max_length=MAX_CODES,
            description="Bare JSON array of 1-100 raw package code strings.",
        ),
    ],
    x_simulate_storage_failure: Annotated[str | None, Header()] = None,
) -> ScanResponse:
    """Validate every code and book valid ones in one SQLite transaction.

    Format errors and checksum mismatches keep their per-code verdict, are
    not counted and carry ``reconciliation: null``. A storage failure rolls
    back every increment of the batch and answers 503; an unknown order
    answers 404.
    """
    # Resolve existence before evaluating codes so a wrong order number is a
    # clean 404 even for a structurally valid body.
    state = get_receipt(order_no)
    if state is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"detail": f"unknown order_no {order_no!r}"},
        )

    evaluated: list[CodeResult] = [evaluate_code(code) for code in codes]
    valid_gtins = [
        result.code for result in evaluated if result.status == STATUS_VALID
    ]

    booked: list[Reconciliation] = record_scan_batch(
        order_no,
        valid_gtins,
        simulate_storage_failure=_failure_injection_requested(
            x_simulate_storage_failure
        ),
    )

    items: list[ScanItem] = []
    booked_iter = iter(booked)
    for result in evaluated:  # full per-code order preserved
        reconciliation: ReconciliationOut | None = None
        if result.status == STATUS_VALID:
            outcome = next(booked_iter)
            reconciliation = ReconciliationOut(
                conclusion=outcome.conclusion,  # type: ignore[arg-type]
                planned_qty=outcome.planned_qty,
                received_qty=outcome.received_qty,
            )
        items.append(
            ScanItem(
                code=result.code,
                calculated_check_digit=result.calculated_check_digit,
                status=result.status,  # type: ignore[arg-type]
                reconciliation=reconciliation,
            )
        )
    return ScanResponse(results=items)


@app.get(
    "/receipts/{order_no}",
    response_model=ReceiptStateOut,
    tags=["receipts"],
    summary="Read cumulative planned and received quantities",
)
def read_receipt(order_no: str) -> ReceiptStateOut:
    """Return the receipt state (404 if the order was never created)."""
    state = get_receipt(order_no)
    if state is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"detail": f"unknown order_no {order_no!r}"},
        )
    return _state_out(state)
