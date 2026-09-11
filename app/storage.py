"""SQLite-backed storage for goods-receipt orders and scan reconciliation.

Two tables are created idempotently on application startup (see
``init_db``), so the same DDL works on an empty file or an existing
database:

* ``receipts``          -- one row per unique business order number.
* ``receipt_items``     -- one row per (order, GTIN). Rows created with the
  order carry a positive ``planned_qty``; rows created during a scan for a
  GTIN absent from the order carry ``planned_qty IS NULL`` and mark the
  merchandise as unplanned forever, even after repeat scans.

Every valid code in a scanned batch increments ``received_qty`` **in input
order inside a single SQLite transaction**. Format errors and check-digit
mismatches are never written. If any storage statement fails mid-batch the
whole transaction is rolled back and the caller receives
:class:`StorageUnavailable`, so a retried batch always starts from the same
counts -- the retry is deterministic.

A process-wide lock serialises write transactions: each batch is an atomic
"evaluate then increment" step, so concurrent requests can never produce an
interleaved or half-counted receipt.

Business order numbers are canonicalised (surrounding whitespace stripped)
before every read and write, so visually identical numbers -- ``"PO-1"``
versus ``"  PO-1  "`` -- always address the same receipt and can never
coexist as two separate orders. Rows written *before* this canonicalisation
may still carry the padded spelling in the database; they are resolved to
the same canonical number on every access (an exactly-canonical row wins
when both spellings exist), so the original receipt stays valid and a
repeated number is still rejected as a duplicate.

Cold-chain quality assessments add two more tables to the same startup
migration:

* ``cold_chain_assessments`` -- one row per unique assessment number,
  linked to its receipt order, carrying the allowed temperature zone and
  the persisted summary (segment list as JSON).
* ``cold_chain_samples``     -- the raw ``(recorded_at, temperature)``
  points of one assessment, in submission order.

An assessment is inserted together with all of its samples in **one
transaction**, so a failed request (duplicate assessment number, storage
error) leaves no partial record behind and the same number can be
resubmitted safely.

Idempotent scan batches add one more table to the same startup migration:

* ``scan_batches``        -- one row per (canonical order number,
  ``Idempotency-Key``). The row stores the raw scan array and the complete
  200 response body, and is inserted **in the same transaction** as the
  count increments it records.

A warehouse terminal that lost the response to a batch can resubmit it with
the same key: an identical array replays the stored response without
counting twice, while the same key carrying a different array is rejected
(:class:`IdempotencyConflict`) and the original record is kept. Because the
record commits together with the increments, a rolled-back batch neither
counts nor occupies its key -- the same key can be retried immediately.
Batches submitted without a key are never recorded and keep counting on
every request.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from .cold_chain import AssessmentSummary, ExcursionSegment, SamplePoint

# Default lives in the process working directory; override for tests or for
# mounting a volume in a container.
DEFAULT_DB_PATH = os.environ.get("RECEIPT_DB_PATH", "receipts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    order_no   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS receipt_items (
    order_no     TEXT NOT NULL,
    gtin         TEXT NOT NULL,
    planned_qty  INTEGER,           -- positive for planned lines, NULL = unplanned
    received_qty INTEGER NOT NULL DEFAULT 0,
    line_order   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (order_no, gtin),
    FOREIGN KEY (order_no) REFERENCES receipts(order_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_receipt_items_order
    ON receipt_items(order_no, line_order);

CREATE TABLE IF NOT EXISTS cold_chain_assessments (
    assessment_id        TEXT PRIMARY KEY,
    order_no             TEXT NOT NULL,
    min_temp             REAL NOT NULL,
    max_temp             REAL NOT NULL,
    sample_count         INTEGER NOT NULL,
    span_minutes         REAL NOT NULL,
    out_of_range_samples INTEGER NOT NULL,
    segment_count        INTEGER NOT NULL,
    total_duration_minutes REAL NOT NULL,
    total_degree_minutes   REAL NOT NULL,
    conclusion           TEXT NOT NULL,
    segments_json        TEXT NOT NULL,  -- persisted segment list (JSON)
    created_at           TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (order_no) REFERENCES receipts(order_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cold_chain_assessments_order
    ON cold_chain_assessments(order_no);

CREATE TABLE IF NOT EXISTS cold_chain_samples (
    assessment_id TEXT NOT NULL,
    sample_index  INTEGER NOT NULL,     -- 0-based, preserves submission order
    recorded_at   TEXT NOT NULL,        -- ISO-8601 with timezone offset
    temperature   REAL NOT NULL,
    PRIMARY KEY (assessment_id, sample_index),
    FOREIGN KEY (assessment_id)
        REFERENCES cold_chain_assessments(assessment_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scan_batches (
    order_no      TEXT NOT NULL,    -- canonical (whitespace-stripped) number
    batch_key     TEXT NOT NULL,    -- Idempotency-Key, whitespace-stripped
    codes_json    TEXT NOT NULL,    -- raw scan array, input order preserved
    response_json TEXT NOT NULL,    -- complete 200 response body to replay
    created_at    TEXT NOT NULL
                  DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (order_no, batch_key)
);
"""


class OrderAlreadyExists(Exception):
    """The unique business order number was already received."""


class OrderNotFound(Exception):
    """Scans were submitted against an order number that was never created."""


class StorageUnavailable(Exception):
    """A SQLite statement failed; the batch transaction was rolled back."""


class AssessmentAlreadyExists(Exception):
    """The unique cold-chain assessment number was already registered."""


class IdempotencyConflict(Exception):
    """An Idempotency-Key was reused with a different scan array.

    The first batch recorded under the key is kept untouched; nothing is
    booked for the conflicting request.
    """


@dataclass(frozen=True)
class PlannedLine:
    """A receipt line as persisted; ``planned_qty`` is None when unplanned."""

    gtin: str
    planned_qty: int | None
    received_qty: int = 0


@dataclass(frozen=True)
class ReceiptState:
    """Full snapshot of one receipt order (planned and unplanned lines)."""

    order_no: str
    items: list[PlannedLine]


@dataclass(frozen=True)
class Reconciliation:
    """Booking conclusion for one *valid* scanned code.

    ``planned_qty`` is ``None`` for unplanned merchandise. ``received_qty``
    is the cumulative count for this GTIN *after* the current scan was
    booked (1-based over the batch input order).
    """

    gtin: str
    conclusion: str  # "matched" | "excess" | "unplanned"
    planned_qty: int | None
    received_qty: int


@dataclass(frozen=True)
class ColdChainAssessmentRecord:
    """A persisted cold-chain assessment: raw samples plus the summary.

    ``order_no`` is the canonical (whitespace-stripped) business number,
    matching what the receipts API serves, even when the linked receipt row
    still carries a legacy padded spelling.
    """

    assessment_id: str
    order_no: str
    min_temp: float
    max_temp: float
    samples: list[SamplePoint]
    summary: AssessmentSummary


# A single connection is reused by the whole process. FastAPI runs sync
# endpoints on a thread pool; ``check_same_thread=False`` is therefore
# required, and the write lock makes concurrent access safe.
_connection: sqlite3.Connection | None = None
_db_path: str | None = None
_write_lock = threading.Lock()


@contextmanager
def _transaction() -> Iterator[sqlite3.Cursor]:
    """Yield a cursor inside one BEGIN/COMMIT (or ROLLBACK) transaction."""
    connection = _get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.cursor()
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _get_connection() -> sqlite3.Connection:
    global _connection, _db_path
    if _connection is None:
        path = _db_path or DEFAULT_DB_PATH
        connection = sqlite3.connect(
            path, check_same_thread=False, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        _connection = connection
    return _connection


def _init_db_locked() -> None:
    """Create the schema; the caller must hold ``_write_lock``."""
    connection = _get_connection()
    connection.executescript(SCHEMA)
    connection.commit()


def init_db(path: str | None = None) -> None:
    """Create the schema if it does not exist. Idempotent on restart."""
    global _connection, _db_path
    with _write_lock:
        if path is not None:
            if _connection is not None:
                _connection.close()
                _connection = None
            _db_path = path
        _init_db_locked()


def reset_for_tests(path: str = ":memory:") -> None:
    """Drop the cached connection and initialise a fresh database.

    Tests use this in a fixture so suite order can never leak counts between
    orders; the shared in-memory connection keeps the schema alive for the
    whole test session.
    """
    global _connection, _db_path
    with _write_lock:
        if _connection is not None:
            _connection.close()
        _connection = None
        _db_path = path
        _init_db_locked()


def _canonical_order_no(order_no: str) -> str:
    """Return the canonical form of a business order number.

    Surrounding whitespace carries no meaning: ``"PO-1"`` and ``" PO-1 "``
    are the *same* receipt. Canonicalising at this boundary makes create,
    read and scan all resolve one key, and turns a whitespace-padded
    variant of an existing number into a duplicate (409) instead of a
    second, visually identical order.
    """
    return order_no.strip()


def _resolve_stored_order_no(
    connection: sqlite3.Connection | sqlite3.Cursor, canonical: str
) -> str | None:
    """Return the stored ``receipts`` key for a canonical order number.

    Rows written since canonicalisation match exactly. Rows created
    *before* it may still store the same number with surrounding
    whitespace; the oldest such row is returned, so the original receipt
    stays addressable and counts as a duplicate -- its data is never
    rewritten. An exactly-canonical row always wins over a padded legacy
    spelling when both exist.
    """
    row = connection.execute(
        "SELECT order_no FROM receipts WHERE order_no = ?", (canonical,)
    ).fetchone()
    if row is not None:
        return row["order_no"]
    for legacy in connection.execute(
        "SELECT order_no FROM receipts ORDER BY rowid"
    ):
        stored = legacy["order_no"]
        if stored.strip() == canonical:
            return stored
    return None


def create_receipt(order_no: str, lines: list[tuple[str, int]]) -> None:
    """Insert one receipt order and its planned lines in a single transaction.

    Raises :class:`OrderAlreadyExists` on a duplicate business order number
    (including a padded spelling stored before canonicalisation).
    Any other SQLite failure becomes :class:`StorageUnavailable` and leaves
    no partial order behind.
    """
    order_no = _canonical_order_no(order_no)
    with _write_lock:
        try:
            with _transaction() as cursor:
                if _resolve_stored_order_no(cursor, order_no) is not None:
                    # A legacy row with a padded spelling is the same
                    # business number: reject as a duplicate, keep the
                    # original receipt untouched.
                    raise OrderAlreadyExists(order_no)
                cursor.execute(
                    "INSERT INTO receipts(order_no) VALUES (?)", (order_no,)
                )
                cursor.executemany(
                    "INSERT INTO receipt_items"
                    "(order_no, gtin, planned_qty, line_order) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (order_no, gtin, planned_qty, index)
                        for index, (gtin, planned_qty) in enumerate(lines)
                    ],
                )
        except sqlite3.IntegrityError as error:
            raise OrderAlreadyExists(order_no) from error
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error


def get_receipt(order_no: str) -> ReceiptState | None:
    """Return the full order snapshot, or ``None`` if it does not exist."""
    # All statements share one connection; the write lock also serialises
    # reads so two threads never touch the connection mid-transaction.
    order_no = _canonical_order_no(order_no)
    with _write_lock:
        try:
            connection = _get_connection()
            stored = _resolve_stored_order_no(connection, order_no)
            if stored is None:
                return None
            item_rows = connection.execute(
                "SELECT gtin, planned_qty, received_qty FROM receipt_items "
                "WHERE order_no = ? "
                "ORDER BY (planned_qty IS NULL), line_order, gtin",
                (stored,),
            ).fetchall()
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error
    return ReceiptState(
        order_no=order_no,
        items=[
            PlannedLine(
                gtin=item_row["gtin"],
                planned_qty=item_row["planned_qty"],
                received_qty=item_row["received_qty"],
            )
            for item_row in item_rows
        ],
    )


def _book_valid_gtins(
    cursor: sqlite3.Cursor,
    stored_order_no: str,
    valid_gtins: list[str],
    simulate_storage_failure: bool,
) -> list[Reconciliation]:
    """Increment ``received_qty`` for every valid GTIN, in input order.

    Runs inside the caller's transaction (the caller commits or rolls back)
    against the *stored* receipt key, so legacy padded rows are booked
    correctly. When ``simulate_storage_failure`` is set, a database error is
    raised after the first increment so the whole batch is rolled back.
    """
    results: list[Reconciliation] = []
    booked = 0
    for gtin in valid_gtins:
        row = cursor.execute(
            "SELECT planned_qty, received_qty FROM receipt_items "
            "WHERE order_no = ? AND gtin = ?",
            (stored_order_no, gtin),
        ).fetchone()

        if row is None:
            # First sighting of a GTIN absent from the purchase
            # plan: persist it as an unplanned line.
            cursor.execute(
                "INSERT INTO receipt_items"
                "(order_no, gtin, planned_qty, received_qty, "
                "line_order) VALUES (?, ?, NULL, 1, 0)",
                (stored_order_no, gtin),
            )
            planned_qty: int | None = None
            received = 1
            conclusion = "unplanned"
        else:
            planned_qty = row["planned_qty"]
            received = row["received_qty"] + 1
            cursor.execute(
                "UPDATE receipt_items SET received_qty = ? "
                "WHERE order_no = ? AND gtin = ?",
                (received, stored_order_no, gtin),
            )
            if planned_qty is None:
                conclusion = "unplanned"
            elif received <= planned_qty:
                conclusion = "matched"
            else:
                conclusion = "excess"

        results.append(
            Reconciliation(
                gtin=gtin,
                conclusion=conclusion,
                planned_qty=planned_qty,
                received_qty=received,
            )
        )
        booked += 1

        if simulate_storage_failure and booked == 1:
            raise sqlite3.DatabaseError(
                "injected storage failure before commit"
            )
    return results


def record_scan_batch(
    order_no: str,
    valid_gtins: list[str],
    simulate_storage_failure: bool = False,
) -> list[Reconciliation]:
    """Book a batch of already-validated GTINs in one transaction.

    ``valid_gtins`` is the ordered subsequence of codes whose status is
    ``valid``; invalid items are not passed at all, so they can neither be
    booked nor receive a reconciliation conclusion.

    Each GTIN is incremented strictly in input order. The returned list has
    one entry per valid GTIN, in the same order. When
    ``simulate_storage_failure`` is set, a storage error is injected after
    the first increment (and before commit): the transaction is rolled back,
    nothing is booked, and :class:`StorageUnavailable` is raised.
    """
    order_no = _canonical_order_no(order_no)
    with _write_lock:
        try:
            with _transaction() as cursor:
                stored = _resolve_stored_order_no(cursor, order_no)
                if stored is None:
                    # Raising inside the context manager triggers ROLLBACK.
                    raise OrderNotFound(order_no)
                results = _book_valid_gtins(
                    cursor, stored, valid_gtins, simulate_storage_failure
                )
        except OrderNotFound:
            raise
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error
    return results


def record_scan_batch_idempotent(
    order_no: str,
    batch_key: str,
    raw_codes: list[str],
    valid_gtins: list[str],
    response_factory: Callable[[list[Reconciliation]], str],
    simulate_storage_failure: bool = False,
) -> str:
    """Book one scan batch at most once under an idempotency key.

    The whole operation is a single SQLite transaction (serialised by the
    process-wide write lock), so the lookup, the count increments and the
    batch record commit or roll back together:

    * A committed record for ``(order_no, batch_key)`` carrying the
      **identical** raw array replays its stored 200 response body verbatim;
      nothing is counted again.
    * The same key carrying a different array (content or order) raises
      :class:`IdempotencyConflict`; the original record is kept and no
      quantity changes.
    * Otherwise the valid GTINs are booked in input order, the response
      produced by ``response_factory`` (the complete 200 body) is stored
      with the raw array under the canonical order number and the key, and
      that same body is returned.

    A storage failure (injected or real) rolls back the increments *and*
    the batch record, so the key stays free for an immediate retry.
    ``batch_key`` must already be canonicalised (whitespace-stripped) by
    the caller; ``order_no`` is canonicalised here like everywhere else.
    """
    order_no = _canonical_order_no(order_no)
    codes_json = json.dumps(raw_codes, ensure_ascii=False)
    with _write_lock:
        try:
            with _transaction() as cursor:
                row = cursor.execute(
                    "SELECT codes_json, response_json FROM scan_batches "
                    "WHERE order_no = ? AND batch_key = ?",
                    (order_no, batch_key),
                ).fetchone()
                if row is not None:
                    if row["codes_json"] == codes_json:
                        # Identical batch already committed: replay the
                        # first response without booking anything again.
                        return row["response_json"]
                    # Same key, different array: keep the original record.
                    raise IdempotencyConflict(batch_key)

                stored = _resolve_stored_order_no(cursor, order_no)
                if stored is None:
                    # Raising inside the context manager triggers ROLLBACK.
                    raise OrderNotFound(order_no)
                booked = _book_valid_gtins(
                    cursor, stored, valid_gtins, simulate_storage_failure
                )
                response_json = response_factory(booked)
                cursor.execute(
                    "INSERT INTO scan_batches"
                    "(order_no, batch_key, codes_json, response_json) "
                    "VALUES (?, ?, ?, ?)",
                    (order_no, batch_key, codes_json, response_json),
                )
                return response_json
        except (OrderNotFound, IdempotencyConflict):
            raise
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error


# ---------------------------------------------------------------------------
# Cold-chain assessments
# ---------------------------------------------------------------------------


def _canonical_assessment_id(assessment_id: str) -> str:
    """Canonicalise an assessment number exactly like a business order no."""
    return assessment_id.strip()


def create_cold_chain_assessment(
    assessment_id: str,
    order_no: str,
    min_temp: float,
    max_temp: float,
    samples: list[SamplePoint],
    summary: AssessmentSummary,
) -> None:
    """Persist one assessment, its summary and all raw samples atomically.

    The receipt is resolved to its stored key (legacy padded spellings
    included) so the foreign key always references the existing row.
    Raises :class:`OrderNotFound` if the receipt does not exist and
    :class:`AssessmentAlreadyExists` on a duplicate assessment number; any
    other SQLite failure becomes :class:`StorageUnavailable`. Every raise
    happens inside the transaction, so a failed request persists nothing.
    """
    assessment_id = _canonical_assessment_id(assessment_id)
    order_no = _canonical_order_no(order_no)
    segments_payload = json.dumps(
        [
            {
                "start": segment.start.isoformat(),
                "end": segment.end.isoformat(),
                "duration_minutes": segment.duration_minutes,
                "degree_minutes": segment.degree_minutes,
                "sample_count": segment.sample_count,
                "peak_deviation": segment.peak_deviation,
            }
            for segment in summary.segments
        ]
    )
    with _write_lock:
        try:
            with _transaction() as cursor:
                stored = _resolve_stored_order_no(cursor, order_no)
                if stored is None:
                    # Raising inside the context manager triggers ROLLBACK.
                    raise OrderNotFound(order_no)
                cursor.execute(
                    "SELECT 1 FROM cold_chain_assessments "
                    "WHERE assessment_id = ?",
                    (assessment_id,),
                )
                if cursor.fetchone() is not None:
                    raise AssessmentAlreadyExists(assessment_id)
                cursor.execute(
                    "INSERT INTO cold_chain_assessments"
                    "(assessment_id, order_no, min_temp, max_temp, "
                    " sample_count, span_minutes, out_of_range_samples, "
                    " segment_count, total_duration_minutes, "
                    " total_degree_minutes, conclusion, segments_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        assessment_id,
                        stored,
                        min_temp,
                        max_temp,
                        summary.sample_count,
                        summary.span_minutes,
                        summary.out_of_range_samples,
                        summary.segment_count,
                        summary.total_duration_minutes,
                        summary.total_degree_minutes,
                        summary.conclusion,
                        segments_payload,
                    ),
                )
                cursor.executemany(
                    "INSERT INTO cold_chain_samples"
                    "(assessment_id, sample_index, recorded_at, temperature) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            assessment_id,
                            index,
                            sample.recorded_at.isoformat(),
                            sample.temperature,
                        )
                        for index, sample in enumerate(samples)
                    ],
                )
        except (OrderNotFound, AssessmentAlreadyExists):
            raise
        except sqlite3.IntegrityError as error:
            # Backstop: a concurrent insert won the primary-key race.
            raise AssessmentAlreadyExists(assessment_id) from error
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error


def get_cold_chain_assessment(
    assessment_id: str,
) -> ColdChainAssessmentRecord | None:
    """Return the persisted assessment document, or ``None`` if unknown.

    The returned record is rebuilt from the stored summary and raw samples,
    so a read always serves the same deterministic document that the
    creating request returned.
    """
    assessment_id = _canonical_assessment_id(assessment_id)
    with _write_lock:
        try:
            connection = _get_connection()
            row = connection.execute(
                "SELECT order_no, min_temp, max_temp, sample_count, "
                "span_minutes, out_of_range_samples, segment_count, "
                "total_duration_minutes, total_degree_minutes, conclusion, "
                "segments_json "
                "FROM cold_chain_assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            if row is None:
                return None
            sample_rows = connection.execute(
                "SELECT recorded_at, temperature FROM cold_chain_samples "
                "WHERE assessment_id = ? ORDER BY sample_index",
                (assessment_id,),
            ).fetchall()
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error

    segments = [
        ExcursionSegment(
            start=datetime.fromisoformat(segment["start"]),
            end=datetime.fromisoformat(segment["end"]),
            duration_minutes=segment["duration_minutes"],
            degree_minutes=segment["degree_minutes"],
            sample_count=segment["sample_count"],
            peak_deviation=segment["peak_deviation"],
        )
        for segment in json.loads(row["segments_json"])
    ]
    return ColdChainAssessmentRecord(
        assessment_id=assessment_id,
        order_no=_canonical_order_no(row["order_no"]),
        min_temp=row["min_temp"],
        max_temp=row["max_temp"],
        samples=[
            SamplePoint(
                recorded_at=datetime.fromisoformat(sample["recorded_at"]),
                temperature=sample["temperature"],
            )
            for sample in sample_rows
        ],
        summary=AssessmentSummary(
            sample_count=row["sample_count"],
            span_minutes=row["span_minutes"],
            out_of_range_samples=row["out_of_range_samples"],
            segment_count=row["segment_count"],
            total_duration_minutes=row["total_duration_minutes"],
            total_degree_minutes=row["total_degree_minutes"],
            conclusion=row["conclusion"],
            segments=segments,
        ),
    )
