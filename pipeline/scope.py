"""Stable segment and period objects shared by quality, metrics and evidence.

A segment ID hashes its dimension filters (plus the cohort when it is not the
full input); a period ID hashes its included ISO dates. Every module builds
scope through these helpers so the same group gets the same ID everywhere.
"""

from collections.abc import Iterable, Mapping
from datetime import date

from contracts.enums import CoverageStatus, Dimension
from contracts.ids import IdPrefix, make_id
from contracts.models import Period, Segment

FULL_INPUT_COHORT = "full_input"


def segment(
    namespace: str, filters: Mapping[Dimension, str], cohort_id: str = FULL_INPUT_COHORT
) -> Segment:
    labels = {Dimension(k).value: v for k, v in filters.items()}
    payload: object = labels if cohort_id == FULL_INPUT_COHORT else {
        "filters": labels, "cohort": cohort_id,
    }
    return Segment(
        segment_id=make_id(IdPrefix.SEGMENT, namespace, payload),
        filters={Dimension(k): v for k, v in labels.items()},
        cohort_id=cohort_id,
    )


def account_segment(namespace: str, cohort_id: str = FULL_INPUT_COHORT) -> Segment:
    return segment(namespace, {}, cohort_id)


def period(namespace: str, dates: Iterable[date], coverage: CoverageStatus) -> Period:
    included = sorted(set(dates))
    if not included:
        raise ValueError("a period needs at least one date")
    return Period(
        period_id=make_id(IdPrefix.PERIOD, namespace, [d.isoformat() for d in included]),
        start=included[0],
        end=included[-1],
        included_dates=included,
        coverage_status=coverage,
    )
