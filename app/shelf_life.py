"""Shelf-life review domain rules (货架期复核).

Once goods are booked into the warehouse, the warehouse keeper reviews every
received GTIN by production batch to arrange shelving. Each declared batch
carries an expiry date; the service counts the **remaining days** from the
review date to the expiry date in calendar days (自然日) and marks the batch
with a disposition:

* ``expired``     -- remaining days below zero (the expiry date has passed);
* ``short_dated`` -- remaining days zero or more but below the minimum
  sellable-days threshold;
* ``usable``      -- remaining days at or above the threshold.

A batch expiring exactly on the review date has zero remaining days and is
therefore ``short_dated`` (or ``usable`` when the threshold is zero), never
``expired``: only a negative count means the goods have expired. Within one
GTIN the reviewed batches are sorted by expiry date and then batch number;
the sort is stable, so batches with equal keys would keep their submission
order (batch numbers are unique per GTIN, making the order fully
deterministic).

The computation is a pure, deterministic function of ``(review_date,
min_sellable_days, batches)``: reading a stored review reproduces the very
same document that was created.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The minimum sellable-days threshold is bounded to 0..3650 days (ten
# years); anything outside that range fails request validation (422).
MAX_MIN_SELLABLE_DAYS: int = 3650

DISPOSITION_EXPIRED = "expired"
DISPOSITION_SHORT_DATED = "short_dated"
DISPOSITION_USABLE = "usable"


@dataclass(frozen=True)
class DeclaredBatch:
    """One batch as declared by the warehouse keeper."""

    batch_no: str
    quantity: int
    expiry_date: date


@dataclass(frozen=True)
class ReviewedBatch:
    """One declared batch with its computed remaining days and disposition."""

    batch_no: str
    quantity: int
    expiry_date: date
    remaining_days: int
    disposition: str  # "expired" | "short_dated" | "usable"


def remaining_days(expiry_date: date, review_date: date) -> int:
    """Return the calendar days from the review date to the expiry date.

    The result is negative when the expiry date lies before the review
    date and zero when both fall on the same calendar day.
    """
    return (expiry_date - review_date).days


def classify_remaining(remaining_days: int, min_sellable_days: int) -> str:
    """Map a remaining-days count to its shelf-life disposition."""
    if remaining_days < 0:
        return DISPOSITION_EXPIRED
    if remaining_days < min_sellable_days:
        return DISPOSITION_SHORT_DATED
    return DISPOSITION_USABLE


def review_batches(
    batches: list[DeclaredBatch],
    review_date: date,
    min_sellable_days: int,
) -> list[ReviewedBatch]:
    """Compute remaining days and dispositions and sort within one GTIN.

    ``batches`` arrives in submission order. The result is sorted by
    ``(expiry_date, batch_no)``; Python's sort is stable, so batches with
    identical keys (impossible once the caller has rejected duplicate batch
    numbers) would keep their submission order. Callers must have already
    validated the request boundary (positive quantities, unique batch
    numbers, threshold in range); this function performs no validation
    itself.
    """
    reviewed: list[ReviewedBatch] = []
    for batch in batches:
        days = remaining_days(batch.expiry_date, review_date)
        reviewed.append(
            ReviewedBatch(
                batch_no=batch.batch_no,
                quantity=batch.quantity,
                expiry_date=batch.expiry_date,
                remaining_days=days,
                disposition=classify_remaining(days, min_sellable_days),
            )
        )
    reviewed.sort(key=lambda batch: (batch.expiry_date, batch.batch_no))
    return reviewed
