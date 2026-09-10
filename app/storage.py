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
coexist as two separate orders.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

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
"""


class OrderAlreadyExists(Exception):
    """The unique business order number was already received."""


class OrderNotFound(Exception):
    """Scans were submitted against an order number that was never created."""


class StorageUnavailable(Exception):
    """A SQLite statement failed; the batch transaction was rolled back."""


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


def create_receipt(order_no: str, lines: list[tuple[str, int]]) -> None:
    """Insert one receipt order and its planned lines in a single transaction.

    Raises :class:`OrderAlreadyExists` on a duplicate business order number.
    Any other SQLite failure becomes :class:`StorageUnavailable` and leaves
    no partial order behind.
    """
    order_no = _canonical_order_no(order_no)
    with _write_lock:
        try:
            with _transaction() as cursor:
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
            row = connection.execute(
                "SELECT 1 FROM receipts WHERE order_no = ?", (order_no,)
            ).fetchone()
            if row is None:
                return None
            item_rows = connection.execute(
                "SELECT gtin, planned_qty, received_qty FROM receipt_items "
                "WHERE order_no = ? "
                "ORDER BY (planned_qty IS NULL), line_order, gtin",
                (order_no,),
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
    results: list[Reconciliation] = []
    with _write_lock:
        try:
            with _transaction() as cursor:
                exists = cursor.execute(
                    "SELECT 1 FROM receipts WHERE order_no = ?", (order_no,)
                ).fetchone()
                if exists is None:
                    # Raising inside the context manager triggers ROLLBACK.
                    raise OrderNotFound(order_no)

                booked = 0
                for gtin in valid_gtins:
                    row = cursor.execute(
                        "SELECT planned_qty, received_qty FROM receipt_items "
                        "WHERE order_no = ? AND gtin = ?",
                        (order_no, gtin),
                    ).fetchone()

                    if row is None:
                        # First sighting of a GTIN absent from the purchase
                        # plan: persist it as an unplanned line.
                        cursor.execute(
                            "INSERT INTO receipt_items"
                            "(order_no, gtin, planned_qty, received_qty, "
                            "line_order) VALUES (?, ?, NULL, 1, 0)",
                            (order_no, gtin),
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
                            (received, order_no, gtin),
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
        except OrderNotFound:
            raise
        except sqlite3.Error as error:
            raise StorageUnavailable(str(error)) from error
    return results
