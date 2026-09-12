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
  conclusion. Invalid codes are neither booked nor concluded. An optional
  ``Idempotency-Key`` header (1-128 characters, at least one non-blank)
  makes the batch exactly-once: the canonical order number, the raw scan
  array and the complete 200 response are stored in the same transaction as
  the count increments, so a retry after a lost response replays the first
  response without counting twice, a conflicting reuse of the key answers
  409, and a rolled-back batch neither counts nor occupies its key.
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

Shelf-life review (货架期复核):

* ``POST /shelf-life-reviews`` registers one shelf-life review for an
  existing receipt: a unique review number, the review date, the minimum
  sellable-days threshold and the declared batches (batch number, quantity,
  expiry date) of every booked GTIN. Remaining days are counted in calendar
  days; batches below zero, below the threshold and at or above it are
  marked ``expired`` / ``short_dated`` / ``usable`` and stably sorted by
  expiry date and batch number within each GTIN. The review, its GTIN items
  and the batch details are persisted atomically.
* ``GET /shelf-life-reviews/{review_id}`` returns the same deterministic
  document; an unknown review number answers 404.
"""
from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import Body, FastAPI, Header, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, StrictInt, model_validator

from .cold_chain import (
    MAX_ABS_TEMP,
    MAX_SAMPLES,
    MAX_SPAN,
    MIN_SAMPLES,
    AssessmentSummary,
    ExcursionSegment,
    SamplePoint,
    assess_cold_chain,
)
from .gtin14 import STATUS_VALID, CodeResult, evaluate_code
from .shelf_life import (
    DISPOSITION_EXPIRED,
    DISPOSITION_SHORT_DATED,
    DISPOSITION_USABLE,
    MAX_MIN_SELLABLE_DAYS,
    DeclaredBatch,
    review_batches,
)
from .storage import (
    AssessmentAlreadyExists,
    ColdChainAssessmentRecord,
    IdempotencyConflict,
    OrderAlreadyExists,
    PlannedLine,
    ReceiptState,
    Reconciliation,
    ReviewAlreadyExists,
    ShelfLifeReviewItem,
    ShelfLifeReviewRecord,
    StorageUnavailable,
    create_cold_chain_assessment,
    create_receipt,
    create_shelf_life_review,
    get_cold_chain_assessment,
    get_receipt,
    get_shelf_life_review,
    init_db,
    record_scan_batch,
    record_scan_batch_idempotent,
)

MAX_CODES: int = 100
MAX_LINES: int = 100
# An Idempotency-Key is 1-128 characters with at least one non-blank
# character; anything else fails request validation with 422.
MAX_IDEMPOTENCY_KEY_LENGTH: int = 128
# SQLite INTEGER is a signed 64-bit value; a planned quantity outside that
# range cannot be persisted, so it must fail request validation (422)
# instead of surfacing as an unhandled OverflowError (500) at insert time.
MAX_PLANNED_QTY: int = 2**63 - 1

Status = Literal["valid", "format_error", "checksum_mismatch"]
Conclusion = Literal["matched", "excess", "unplanned"]
ColdChainConclusion = Literal["compliant", "excursion"]
Disposition = Literal["expired", "short_dated", "usable"]
Disposition = Literal["expired", "short_dated", "usable"]

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
    version="1.4.0",
    description=(
        "Receive scanned GTIN-14 package codes for pharmaceutical goods "
        "receipt and return an order-preserving, duplicate-preserving "
        "verdict for each code, with purchase-order reconciliation for "
        "created receipt orders, cold-chain temperature assessment for "
        "completed receipts and shelf-life reviews for booked goods."
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
    temperature: Annotated[
        float, Field(allow_inf_nan=False, ge=-MAX_ABS_TEMP, le=MAX_ABS_TEMP)
    ]


class CreateColdChainAssessmentIn(BaseModel):
    """Body for ``POST /cold-chain-assessments``."""

    assessment_id: Annotated[str, Field(min_length=1, max_length=128)]
    order_no: Annotated[str, Field(min_length=1, max_length=128)]
    min_temp: Annotated[
        float, Field(allow_inf_nan=False, ge=-MAX_ABS_TEMP, le=MAX_ABS_TEMP)
    ]
    max_temp: Annotated[
        float, Field(allow_inf_nan=False, ge=-MAX_ABS_TEMP, le=MAX_ABS_TEMP)
    ]
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


# ---------------------------------------------------------------------------
# Shelf-life review models
# ---------------------------------------------------------------------------


class ShelfLifeBatchIn(BaseModel):
    """One declared batch: production batch number, quantity, expiry date."""

    batch_no: Annotated[str, Field(min_length=1, max_length=128)]
    quantity: Annotated[StrictInt, Field(gt=0, le=MAX_PLANNED_QTY)]
    expiry_date: date


class ShelfLifeItemIn(BaseModel):
    """All batches declared for one GTIN already booked on the receipt."""

    gtin: Annotated[str, Field(min_length=1, max_length=128)]
    batches: Annotated[
        list[ShelfLifeBatchIn], Field(min_length=1, max_length=MAX_LINES)
    ]


class CreateShelfLifeReviewIn(BaseModel):
    """Body for ``POST /shelf-life-reviews``."""

    review_id: Annotated[str, Field(min_length=1, max_length=128)]
    order_no: Annotated[str, Field(min_length=1, max_length=128)]
    review_date: date
    min_sellable_days: Annotated[
        StrictInt, Field(ge=0, le=MAX_MIN_SELLABLE_DAYS)
    ]
    items: Annotated[
        list[ShelfLifeItemIn], Field(min_length=1, max_length=MAX_LINES)
    ]

    @model_validator(mode="after")
    def validate_business_rules(self) -> CreateShelfLifeReviewIn:
        if not self.review_id.strip():
            raise ValueError(
                "review_id must contain at least one non-blank char"
            )
        if "/" in self.review_id:
            # Like order_no, the review number is addressed as a single
            # URL path segment; a slash would make it unreachable.
            raise ValueError("review_id must not contain '/'")
        gtins = [item.gtin for item in self.items]
        if len(set(gtins)) != len(gtins):
            raise ValueError("items must not contain duplicate GTINs")
        for item in self.items:
            batch_nos: list[str] = []
            for batch in item.batches:
                if not batch.batch_no.strip():
                    raise ValueError(
                        "batch_no must contain at least one non-blank char"
                    )
                batch_nos.append(batch.batch_no)
            if len(set(batch_nos)) != len(batch_nos):
                raise ValueError(
                    f"batches of GTIN {item.gtin!r} must not contain "
                    "duplicate batch numbers"
                )
        return self


class ShelfLifeBatchOut(BaseModel):
    """One reviewed batch with its remaining days and disposition."""

    batch_no: str
    quantity: int
    expiry_date: date
    remaining_days: int
    disposition: Disposition


class ShelfLifeItemOut(BaseModel):
    """One reviewed GTIN: booked snapshot plus its sorted batches."""

    gtin: str
    received_qty: int
    declared_qty: int
    batches: list[ShelfLifeBatchOut]


class ShelfLifeSummaryOut(BaseModel):
    """Disposition counts over the whole review."""

    gtin_count: int
    batch_count: int
    declared_qty: int
    expired_batches: int
    short_dated_batches: int
    usable_batches: int


class ShelfLifeReviewOut(BaseModel):
    """Full review document: header, sorted items and the summary."""

    review_id: str
    order_no: str
    review_date: date
    min_sellable_days: int
    items: list[ShelfLifeItemOut]
    summary: ShelfLifeSummaryOut


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


def _review_out(record: ShelfLifeReviewRecord) -> ShelfLifeReviewOut:
    items: list[ShelfLifeItemOut] = []
    for item in record.items:
        items.append(
            ShelfLifeItemOut(
                gtin=item.gtin,
                received_qty=item.received_qty,
                declared_qty=sum(batch.quantity for batch in item.batches),
                batches=[
                    ShelfLifeBatchOut(
                        batch_no=batch.batch_no,
                        quantity=batch.quantity,
                        expiry_date=batch.expiry_date,
                        remaining_days=batch.remaining_days,
                        disposition=batch.disposition,  # type: ignore[arg-type]
                    )
                    for batch in item.batches
                ],
            )
        )
    all_batches = [batch for item in record.items for batch in item.batches]
    return ShelfLifeReviewOut(
        review_id=record.review_id,
        order_no=record.order_no,
        review_date=record.review_date,
        min_sellable_days=record.min_sellable_days,
        items=items,
        summary=ShelfLifeSummaryOut(
            gtin_count=len(record.items),
            batch_count=len(all_batches),
            declared_qty=sum(batch.quantity for batch in all_batches),
            expired_batches=sum(
                1
                for batch in all_batches
                if batch.disposition == DISPOSITION_EXPIRED
            ),
            short_dated_batches=sum(
                1
                for batch in all_batches
                if batch.disposition == DISPOSITION_SHORT_DATED
            ),
            usable_batches=sum(
                1
                for batch in all_batches
                if batch.disposition == DISPOSITION_USABLE
            ),
        ),
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


def _build_scan_items(
    evaluated: list[CodeResult], booked: list[Reconciliation]
) -> list[ScanItem]:
    """Zip per-code verdicts with booking outcomes, preserving input order.

    ``booked`` has exactly one entry per *valid* code, in input order;
    invalid codes keep their verdict and carry ``reconciliation: null``.
    """
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
    return items


def _blank_idempotency_key_response() -> JSONResponse:
    """422 envelope for a key that holds no non-blank character."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": "value_error",
                    "loc": ["header", "idempotency-key"],
                    "msg": "Idempotency-Key must contain at least one "
                    "non-blank character",
                }
            ]
        },
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
    idempotency_key: Annotated[
        str | None,
        Header(
            min_length=1,
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
            description="Optional idempotency batch key (1-128 characters).",
        ),
    ] = None,
) -> ScanResponse:
    """Validate every code and book valid ones in one SQLite transaction.

    Format errors and checksum mismatches keep their per-code verdict, are
    not counted and carry ``reconciliation: null``. A storage failure rolls
    back every increment of the batch and answers 503; an unknown order
    answers 404.

    With an ``Idempotency-Key`` header the batch is booked at most once:
    the key, the raw array and the complete response commit together with
    the increments. Repeating the identical array with the same key replays
    the first 200 response without counting again; reusing the key with a
    different array answers 409 and keeps the original record. An empty,
    blank or overlong key answers 422. Without the header every request is
    booked as it arrives.
    """
    # An illegal key is a malformed request: reject it before touching any
    # resource state, exactly like the structural body validation does.
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            return _blank_idempotency_key_response()  # type: ignore[return-value]

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
    simulate_failure = _failure_injection_requested(x_simulate_storage_failure)

    if idempotency_key is None:
        # No batch key: book every request as it arrives (unchanged path).
        booked = record_scan_batch(
            order_no,
            valid_gtins,
            simulate_storage_failure=simulate_failure,
        )
        return ScanResponse(results=_build_scan_items(evaluated, booked))

    try:
        response_json = record_scan_batch_idempotent(
            state.order_no,
            idempotency_key,
            codes,
            valid_gtins,
            response_factory=lambda booked: ScanResponse(
                results=_build_scan_items(evaluated, booked)
            ).model_dump_json(),
            simulate_storage_failure=simulate_failure,
        )
    except IdempotencyConflict:
        # 409, not 422: the request itself is valid, only the key was
        # already spent on a different array. The original record is kept.
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={
                "detail": f"Idempotency-Key {idempotency_key!r} was already "
                f"used with a different scan batch for "
                f"order_no {state.order_no!r}"
            },
        )
    # Fresh and replayed batches return the very same stored body, so a
    # retried request is byte-identical to the response that was lost.
    return Response(
        content=response_json, status_code=200, media_type="application/json"
    )


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


# ---------------------------------------------------------------------------
# Shelf-life review endpoints
# ---------------------------------------------------------------------------


def _over_declared_response(
    index: int, gtin: str, declared_qty: int, received_qty: int
) -> JSONResponse:
    """422 envelope for a declared batch total above the booked quantity."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": "value_error",
                    "loc": ["body", "items", index, "batches"],
                    "msg": f"declared batch quantities total {declared_qty}, "
                    f"exceeding the received quantity {received_qty} "
                    f"of GTIN {gtin!r}",
                }
            ]
        },
    )


@app.post(
    "/shelf-life-reviews",
    response_model=ShelfLifeReviewOut,
    status_code=201,
    tags=["shelf-life"],
    summary="Review booked batches and produce the shelf-life disposition list",
)
def create_review(payload: CreateShelfLifeReviewIn) -> ShelfLifeReviewOut:
    """Review the booked batches of one receipt and mark their dispositions.

    The receipt must exist and every submitted GTIN must already be booked
    on it -- a planned line counts only once at least one unit was scanned
    in, and unplanned-but-booked merchandise qualifies as well (404
    otherwise). The declared batch quantities of one GTIN must not total
    more than its received quantity; an illegal date, a repeated
    batch number, a non-positive quantity, a threshold outside 0-3650 days
    or an over-declared total fails validation as a whole with 422. A
    repeated review number answers 409. Failed requests persist nothing.
    On success the review, its items and the batch details are stored
    atomically and the disposition list is returned.
    """
    # Resolve the receipt before computing anything so a wrong order number
    # is a clean 404 even for a structurally valid body.
    state = get_receipt(payload.order_no)
    if state is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"detail": f"unknown order_no {payload.order_no!r}"},
        )

    received_by_gtin = {line.gtin: line.received_qty for line in state.items}
    review_items: list[ShelfLifeReviewItem] = []
    for index, item in enumerate(payload.items):
        received_qty = received_by_gtin.get(item.gtin)
        if received_qty is None or received_qty == 0:
            # Absent from the receipt, or planned but nothing scanned yet:
            # either way the merchandise is not booked and cannot be
            # reviewed (a planned line only becomes booked goods once its
            # first unit is scanned in).
            return JSONResponse(  # type: ignore[return-value]
                status_code=404,
                content={
                    "detail": f"GTIN {item.gtin!r} is not booked on "
                    f"order {state.order_no!r}"
                },
            )
        declared_qty = sum(batch.quantity for batch in item.batches)
        if declared_qty > received_qty:
            return _over_declared_response(  # type: ignore[return-value]
                index, item.gtin, declared_qty, received_qty
            )
        review_items.append(
            ShelfLifeReviewItem(
                gtin=item.gtin,
                received_qty=received_qty,
                batches=review_batches(
                    [
                        DeclaredBatch(
                            batch_no=batch.batch_no,
                            quantity=batch.quantity,
                            expiry_date=batch.expiry_date,
                        )
                        for batch in item.batches
                    ],
                    payload.review_date,
                    payload.min_sellable_days,
                ),
            )
        )

    review_id = payload.review_id.strip()
    try:
        create_shelf_life_review(
            review_id,
            state.order_no,
            payload.review_date,
            payload.min_sellable_days,
            review_items,
        )
    except ReviewAlreadyExists:
        # 409, not 422: the body itself is valid, only the number repeats.
        return JSONResponse(  # type: ignore[return-value]
            status_code=409,
            content={"detail": f"review_id {review_id!r} already exists"},
        )
    record = get_shelf_life_review(review_id)
    assert record is not None  # just created above
    return _review_out(record)


@app.get(
    "/shelf-life-reviews/{review_id}",
    response_model=ShelfLifeReviewOut,
    tags=["shelf-life"],
    summary="Read the deterministic document of a shelf-life review",
)
def read_review(review_id: str) -> ShelfLifeReviewOut:
    """Return the persisted review (404 if the number is unknown)."""
    record = get_shelf_life_review(review_id)
    if record is None:
        return JSONResponse(  # type: ignore[return-value]
            status_code=404,
            content={"detail": f"unknown review_id {review_id!r}"},
        )
    return _review_out(record)
