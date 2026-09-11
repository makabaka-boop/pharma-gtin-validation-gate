"""Cold-chain temperature assessment domain rules.

After a goods receipt is completed, quality personnel register one cold-chain
record per shipment: a unique assessment number, the allowed temperature
zone and the sampled temperature points (strictly increasing in time). The
service merges every maximal run of consecutive out-of-zone samples into an
**excursion segment** and integrates the deviation from the zone between
adjacent sample points with the trapezoidal rule, yielding two reviewable
quantities per segment and in total:

* ``duration_minutes`` -- how long the segment lasted (first to last
  out-of-zone sample of the run), and
* ``degree_minutes``   -- the area between the deviation curve and the zone
  boundary ("度分钟"), approximated trapezoidally over adjacent points.

A segment made of a single out-of-zone sample honestly reports zero
duration and zero degree-minutes: one isolated point spans no time. When
every sample stays inside the zone the assessment concludes ``compliant``
with empty segments and zero totals; otherwise it concludes ``excursion``.

All computed minute/degree-minute values are rounded to two decimals, and
totals are the sums of the already-rounded segment values, so the persisted
summary always adds up exactly as displayed. The computation is a pure,
deterministic function of ``(min_temp, max_temp, samples)``: reading a
stored assessment reproduces the very same document that was created.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# A trapezoidal integral needs at least two points; fewer cannot describe a
# transport curve at all, so such a submission fails validation (422).
MIN_SAMPLES: int = 2
MAX_SAMPLES: int = 10_000

# A cold-chain record may cover at most seven days end to end; a longer span
# is a data-entry error, not a shipment.
MAX_SPAN: timedelta = timedelta(days=7)

# Temperatures are stored as SQLite REAL (IEEE 754 double) and feed the
# deviation/trapezoidal arithmetic. Bounding every temperature input (zone
# bounds and samples alike) to 1e100 deg C keeps that arithmetic finite with
# enormous headroom -- the worst case is a deviation of 2e100 integrated
# over the maximal seven-day span, about 2e104 degree-minutes, nowhere near
# the float64 ceiling of ~1.8e308. A larger magnitude would overflow to
# infinity mid-computation, so it must fail request validation (422) instead
# of poisoning a persisted summary with non-finite numbers.
MAX_ABS_TEMP: float = 1e100

CONCLUSION_COMPLIANT = "compliant"
CONCLUSION_EXCURSION = "excursion"


@dataclass(frozen=True)
class SamplePoint:
    """One raw temperature sample as submitted (timezone-aware instant)."""

    recorded_at: datetime
    temperature: float


@dataclass(frozen=True)
class ExcursionSegment:
    """One merged run of consecutive out-of-zone samples.

    ``start``/``end`` are the timestamps of the first/last out-of-zone
    sample of the run; ``peak_deviation`` is the largest distance from the
    zone boundary observed inside the run (degrees, two decimals).
    """

    start: datetime
    end: datetime
    duration_minutes: float
    degree_minutes: float
    sample_count: int
    peak_deviation: float


@dataclass(frozen=True)
class AssessmentSummary:
    """The reviewable temperature-control conclusion of one assessment."""

    sample_count: int
    span_minutes: float
    out_of_range_samples: int
    segment_count: int
    total_duration_minutes: float
    total_degree_minutes: float
    conclusion: str  # "compliant" | "excursion"
    segments: list[ExcursionSegment]


def deviation_from_zone(temperature: float, min_temp: float, max_temp: float) -> float:
    """Return how far ``temperature`` lies outside ``[min_temp, max_temp]``.

    The deviation is zero on and inside the zone boundaries and otherwise
    the (positive) distance to the nearest boundary, so it can be
    integrated directly regardless of whether the excursion ran hot or
    cold.
    """
    if temperature < min_temp:
        return min_temp - temperature
    if temperature > max_temp:
        return temperature - max_temp
    return 0.0


def _minutes_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def _round2(value: float) -> float:
    """Round a computed minute/degree-minute value to two decimals."""
    return round(value, 2)


def assess_cold_chain(
    min_temp: float, max_temp: float, samples: list[SamplePoint]
) -> AssessmentSummary:
    """Compute the deterministic cold-chain summary for one shipment.

    Consecutive out-of-zone samples are merged into excursion segments;
    each segment integrates the deviation trapezoidally between its
    adjacent points. Totals are the sums of the rounded segment values.
    Callers must have already validated the request boundary (zone ordered,
    at least two samples, strictly increasing instants, span within seven
    days); this function performs no validation itself.
    """
    deviations = [
        deviation_from_zone(sample.temperature, min_temp, max_temp)
        for sample in samples
    ]

    segments: list[ExcursionSegment] = []
    index = 0
    while index < len(samples):
        if deviations[index] == 0.0:
            index += 1
            continue
        # Maximal run of consecutive out-of-zone samples: one segment.
        run_end = index
        while run_end + 1 < len(samples) and deviations[run_end + 1] > 0.0:
            run_end += 1
        run = samples[index : run_end + 1]
        run_deviations = deviations[index : run_end + 1]

        degree_minutes = 0.0
        for k in range(len(run) - 1):
            interval = _minutes_between(run[k].recorded_at, run[k + 1].recorded_at)
            degree_minutes += (
                (run_deviations[k] + run_deviations[k + 1]) / 2.0
            ) * interval

        segments.append(
            ExcursionSegment(
                start=run[0].recorded_at,
                end=run[-1].recorded_at,
                duration_minutes=_round2(
                    _minutes_between(run[0].recorded_at, run[-1].recorded_at)
                ),
                degree_minutes=_round2(degree_minutes),
                sample_count=len(run),
                peak_deviation=_round2(max(run_deviations)),
            )
        )
        index = run_end + 1

    return AssessmentSummary(
        sample_count=len(samples),
        span_minutes=_round2(
            _minutes_between(samples[0].recorded_at, samples[-1].recorded_at)
        ),
        out_of_range_samples=sum(1 for d in deviations if d > 0.0),
        segment_count=len(segments),
        total_duration_minutes=_round2(
            sum(segment.duration_minutes for segment in segments)
        ),
        total_degree_minutes=_round2(
            sum(segment.degree_minutes for segment in segments)
        ),
        conclusion=(
            CONCLUSION_EXCURSION if segments else CONCLUSION_COMPLIANT
        ),
        segments=segments,
    )
