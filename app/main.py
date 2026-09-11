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

Cold-chain quality assessment:

* ``POST /cold-chain-assessments`` registers one cold-chain record for a
  completed receipt: a unique assessment number, the allowed temperature
  zone and the strictly time-increasing sample points. Consecutive
  out-of-zone samples are merged into excursion segments and the deviation
  is integrated trapezoidally, yielding a reviewable transport
  temperature-control conclusion (persisted with the raw samples).
* ``GET /cold-chain-assessments/{assessment_id}`` returns the same
  deterministic document; an unknown assessment answers 404.
"""
from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Body, FastAPI, Header
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, StrictInt, model_validator

from .cold_chain import (
    MAX_SAMPLES,
    MAX_SPAN,
    MIN_SAMPLES,
    AssessmentSummary,
    ExcursionSegment,
    SamplePoint,
    assess_cold_chain,
)
from .gtin14 import STATUS_VALID, CodeResult, evaluate_code
from .storage import (
    AssessmentAlreadyExists,
    ColdChainAssessmentRecord,
    OrderAlreadyExists,
    PlannedLine,
    ReceiptState,
    Reconciliation,
    StorageUnavailable,
    create_cold_chain_assessment,
    create_receipt,
    get_cold_chain_assessment,
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
ColdChainConclusion = Literal["compliant", "excursion"]

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
    version="1.2.0",
    description=(
        "Receive scanned GTIN-14 package codes for pharmaceutical goods "
        "receipt and return an order-preserving, duplicate-preserving "
        "verdict for each code, with purchase-order reconciliation for "
        "created receipt orders and cold-chain temperature assessment for "
        "completed receipts."
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


def _sanitize_non_finite(value: object) -> object:
    """Replace non-finite floats with string tokens inside error details.

    A request body may legally carry ``NaN``/``Infinity`` tokens (Python's
    JSON parser accepts them); when such a value fails validation the stock
    FastAPI handler would embed the raw float in ``detail[].input`` and
    crash serialising the 422 response. Finite values pass through
    untouched, so the envelope is byte-identical to the default handler
    for every ordinary validation error.
    """
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
def _request_validation_handler(_, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": _sanitize_non_finite(jsonable_encoder(exc.errors()))
        },
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


# ---------------------------------------------------------------------------
# Cold-chain assessment models
# ---------------------------------------------------------------------------


class ColdChainSampleIn(BaseModel):
    """One raw temperature sample: a timezone-aware instant and a value."""

    recorded_at: AwareDatetime
    temperature: float = Field(allow_inf_nan=False)


class CreateColdChainAssessmentIn(BaseModel):
    """Body for ``POST /cold-chain-assessments``."""

    assessment_id: Annotated[str, Field(min_length=1, max_length=128)]
    order_no: Annotated[str, Field(min_length=1, max_length=128)]
    min_temp: float = Field(allow_inf_nan=False)
    max_temp: float = Field(allow_inf_nan=False)
    samples: Annotated[
        list[ColdChainSampleIn],
        Field(min_length=MIN_SAMPLES, max_length=MAX_SAMPLES),
    ]

    @model_validator(mode="after")
    def validate_business_rules(self) -> CreateColdChainAssessmentIn:
        if not self.assessment_id.strip():
            raise ValueError(
                "assessment_id must contain at least one non-blank char"
            )
        if "/" in self.assessment_id:
            # Like order_no, the assessment number is addressed as a single
            # URL path segment; a slash would make it unreachable.
            raise ValueError("assessment_id must not contain '/'")
        if not self.min_temp < self.max_temp:
            raise ValueError("min_temp must be strictly below max_temp")
        times = [sample.recorded_at for sample in self.samples]
        if any(later <= earlier for earlier, later in zip(times, times[1:])):
            raise ValueError("samples must be strictly increasing in recorded_at")
        if times[-1] - times[0] > MAX_SPAN:
            raise ValueError("samples must not span more than seven days")
        return self


class ColdChainSampleOut(BaseModel):
    """One raw sample as persisted."""

    recorded_at: datetime
    temperature: float


class ColdChainSegmentOut(BaseModel):
    """One merged excursion segment with its trapezoidal integrals."""

    start: datetime
    end: datetime
    duration_minutes: float
    degree_minutes: float
    sample_count: int
    peak_deviation: float


class ColdChainSummaryOut(BaseModel):
    """The reviewable transport temperature-control conclusion."""

    sample_count: int
    span_minutes: float
    out_of_range_samples: int
    segment_count: int
    total_duration_minutes: float
    total_degree_minutes: float
    conclusion: ColdChainConclusion
    segments: list[ColdChainSegmentOut]


class ColdChainAssessmentOut(BaseModel):
    """Full assessment document: zone, raw samples and persisted summary."""

    assessment_id: str
    order_no: str
    min_temp: float
    max_temp: float
    samples: list[ColdChainSampleOut]
    summary: ColdChainSummaryOut


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


def _segment_out(segment: ExcursionSegment) -> ColdChainSegmentOut:
    return ColdChainSegmentOut(
        start=segment.start,
        end=segment.end,
        duration_minutes=segment.duration_minutes,
        degree_minutes=segment.degree_minutes,
        sample_count=segment.sample_count,
        peak_deviation=segment.peak_deviation,
    )


def _summary_out(summary: AssessmentSummary) -> ColdChainSummaryOut:
    return ColdChainSummaryOut(
        sample_count=summary.sample_count,
        span_minutes=summary.span_minutes,
        out_of_range_samples=summary.out_of_range_samples,
        segment_count=summary.segment_count,
        total_duration_minutes=summary.total_duration_minutes,
        total_degree_minutes=summary.total_degree_minutes,
        conclusion=summary.conclusion,  # type: ignore[arg-type]
        segments=[_segment_out(segment) for segment in summary.segments],
    )


def _assessment_out(record: ColdChainAssessmentRecord) -> ColdChainAssessmentOut:
    return ColdChainAssessmentOut(
        assessment_id=record.assessment_id,
        order_no=record.order_no,
        min_temp=record.min_temp,
        max_temp=record.max_temp,
        samples=[
            ColdChainSampleOut(
                recorded_at=sample.recorded_at, temperature=sample.temperature
            )
            for sample in record.samples
        ],
        summary=_summary_out(record.summary),
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


# ---------------------------------------------------------------------------
# Cold-chain assessment endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/cold-chain-assessments",
    response_model=ColdChainAssessmentOut,
    status_code=201,
    tags=["cold-chain"],
    summary="Register a cold-chain record and assess temperature control",
)
def create_assessment(
    payload: CreateColdChainAssessmentIn,
) -> ColdChainAssessmentOut:
    """Assess one completed receipt's transport temperature curve.

    The receipt must exist (404 otherwise). A repeated assessment number
    answers 409; an unordered zone, fewer than two samples, non-strictly
    increasing timestamps or a span over seven days fail validation as a
    whole with 422. Failed requests persist nothing. On success the raw
    samples and the computed summary are stored atomically and returned.
    """
    # Resolve the receipt before computing anything so a wrong order number
    # is a clean 404 even for a structurally valid body.
    state = get_receipt(payload.order_no)
    if state is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"detail": f"unknown order_no {payload.order_no!r}"},
        )

    points = [
        SamplePoint(recorded_at=sample.recorded_at, temperature=sample.temperature)
        for sample in payload.samples
    ]
    summary = assess_cold_chain(payload.min_temp, payload.max_temp, points)
    assessment_id = payload.assessment_id.strip()
    try:
        create_cold_chain_assessment(
            assessment_id,
            state.order_no,
            payload.min_temp,
            payload.max_temp,
            points,
            summary,
        )
    except AssessmentAlreadyExists:
        # 409, not 422: the body itself is valid, only the number repeats.
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={"detail": f"assessment_id {assessment_id!r} already exists"},
        )
    record = get_cold_chain_assessment(assessment_id)
    assert record is not None  # just created above
    return _assessment_out(record)


@app.get(
    "/cold-chain-assessments/{assessment_id}",
    response_model=ColdChainAssessmentOut,
    tags=["cold-chain"],
    summary="Read the deterministic result of a cold-chain assessment",
)
def read_assessment(assessment_id: str) -> ColdChainAssessmentOut:
    """Return the persisted assessment (404 if the number is unknown)."""
    record = get_cold_chain_assessment(assessment_id)
    if record is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"detail": f"unknown assessment_id {assessment_id!r}"},
        )
    return _assessment_out(record)
