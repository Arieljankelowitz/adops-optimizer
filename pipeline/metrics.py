"""Observed-group aggregation with ratios of additive sums.

``aggregate`` turns loaded rows into immutable tables of additive sums:

* ``MetricsStore.aggregates``: one row per observed (segment, multi-day period),
  with coverage, concentration, stability and eligibility-gate columns;
* ``MetricsStore.daily``: one row per observed (segment, day). A missing row
  means the slice was absent that day, never an observed zero.

Rates are never stored as averages: ``FactBuilder`` derives every rate from
the sums as a typed fact, with a null value and a reason whenever the
denominator is zero or otherwise unusable.

Segment and period IDs come from ``pipeline.scope`` (shared with quality and
evidence). A fact ID is ``make_id(F, ns, spec)`` where ``spec`` names kind,
metric, formula, segment and period (plus left/right facts for comparisons).
"""

import hashlib
import weakref
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import numpy as np
import pandas as pd

from contracts import SCHEMA_VERSION
from contracts.config import AnalyticsConfig
from contracts.enums import (
    CoverageStatus,
    Dimension,
    DIMENSION_COLUMNS,
    FactKind,
    InferenceRestriction,
    InputColumn,
    MetricKey,
    QualityRuleId,
    Relation,
    RuleOutcome,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, make_id, stable_digest
from contracts.models import (
    ComparisonDetail,
    Fact,
    Period,
    Provenance,
    QualityReport,
    Segment,
    Validity,
)
from contracts.runtime import LoadedData, MetricsStore
from pipeline import scope

ZERO = Decimal(0)
THOUSAND = Decimal(1_000)
MILLION = Decimal(1_000_000)

FULL_INPUT = scope.FULL_INPUT_COHORT
# Rows whose counters respect requests >= responses >= wins >= impressions.
VALID_COUNTERS = "valid_counters"
# Rows with positive responses and a positive floor: the bid/floor population.
BID_FLOOR_COHORT = "positive_response_positive_floor"
EXCLUDING_PREFIX = "excluding_"

FIVE: tuple[Dimension, ...] = tuple(Dimension)
LEVELS: tuple[tuple[Dimension, ...], ...] = (
    (),
    *((d,) for d in Dimension),
    (Dimension.SUPPLY_ID, Dimension.COUNTRY),
    (Dimension.SUPPLY_ID, Dimension.DEVICE_TYPE),
    (Dimension.SUPPLY_ID, Dimension.PLACEMENT_TYPE),
    FIVE,
)
# Levels also computed for the valid-counter sensitivity cohort.
VALID_COHORT_LEVELS: tuple[tuple[Dimension, ...], ...] = LEVELS[:6]

ADDITIVE: tuple[MetricKey, ...] = (
    MetricKey.REQUESTS,
    MetricKey.RESPONSES,
    MetricKey.WINS,
    MetricKey.IMPRESSIONS,
    MetricKey.REVENUE,
    MetricKey.COST,
    MetricKey.PROFIT,
)
COUNTERS = ADDITIVE[:4]
MONEY = ADDITIVE[4:]
ADDITIVE_SOURCE: dict[MetricKey, InputColumn] = {
    MetricKey.REQUESTS: InputColumn.DEMAND_REQUESTS,
    MetricKey.RESPONSES: InputColumn.DEMAND_RESPONSES,
    MetricKey.WINS: InputColumn.DEMAND_WINS,
    MetricKey.IMPRESSIONS: InputColumn.SUPPLY_IMPRESSIONS,
    MetricKey.REVENUE: InputColumn.REVENUE,
    MetricKey.COST: InputColumn.COST,
    MetricKey.PROFIT: InputColumn.PROFIT,
}

# Helper sums behind weighted averages and validity flags; not metrics.
FLOOR_X_REQUESTS = "floor_x_requests"
BID_X_RESPONSES = "bid_x_responses"
ZERO_BID_RESPONSES = "zero_bid_responses"
BF_RESPONSES = "bf_responses"
BF_BID_X_RESPONSES = "bf_bid_x_responses"
BF_FLOOR_X_RESPONSES = "bf_floor_x_responses"
FUNNEL_INVALID_ROWS = "funnel_invalid_rows"
FUNNEL_INVALID_REQUESTS = "funnel_invalid_requests"
NEGATIVE_MONEY_ROWS = "negative_money_rows"
ROW_COUNT = "row_count"
SUM_COLUMNS: tuple[str, ...] = (
    *(m.value for m in ADDITIVE),
    FLOOR_X_REQUESTS,
    BID_X_RESPONSES,
    ZERO_BID_RESPONSES,
    BF_RESPONSES,
    BF_BID_X_RESPONSES,
    BF_FLOOR_X_RESPONSES,
    FUNNEL_INVALID_ROWS,
    FUNNEL_INVALID_REQUESTS,
    NEGATIVE_MONEY_ROWS,
    ROW_COUNT,
)

# Eligibility gates (plan 5.2). Column ``gate_<name>@<role>`` per role:
# ``base`` (configured thresholds), ``x<m>`` (sensitivity multiple m) and
# ``exploratory`` (the labelled exploratory floor).
GATE_VOLUME = "volume"  # scale, margin, floor, trend and combination candidates
GATE_WASTE = "waste"  # wasted-request candidates (requests only)
GATE_WINS = "wins"  # wins/impressions discrepancy candidates
GATES = (GATE_VOLUME, GATE_WASTE, GATE_WINS)
BASE = "base"
EXPLORATORY = "exploratory"


def gate_column(gate: str, role: str) -> str:
    return f"gate_{gate}@{role}"


def sensitivity_role(multiplier: Decimal) -> str:
    return f"x{format(multiplier.normalize(), 'f')}"


def sensitivity_roles(store: MetricsStore) -> list[str]:
    """Sensitivity roles present in ``store``, e.g. ``["x0.5", "x2"]``."""
    prefix = f"gate_{GATE_VOLUME}@x"
    return [c.split("@", 1)[1] for c in store.aggregates.columns if c.startswith(prefix)]


def level_name(dims: Sequence[Dimension]) -> str:
    return "+".join(d.value for d in dims) if dims else "account"


# --- IDs ---------------------------------------------------------------------


def make_segment(namespace: str, filters: Mapping[Dimension, str], cohort_id: str = FULL_INPUT) -> Segment:
    """``pipeline.scope.segment`` with filters in canonical dimension order."""
    return scope.segment(namespace, {d: filters[d] for d in Dimension if d in filters}, cohort_id)


def segment_id_for(namespace: str, filters: Mapping[Dimension, str], cohort_id: str = FULL_INPUT) -> str:
    return scope.segment(namespace, filters, cohort_id).segment_id


def make_period(namespace: str, dates: Iterable[date], status: CoverageStatus) -> Period:
    return scope.period(namespace, dates, status)


def period_id_for(namespace: str, dates: Iterable[date]) -> str:
    return scope.period(namespace, dates, CoverageStatus.UNVERIFIED).period_id


def excluding_cohort(segment_id: str) -> str:
    return f"{EXCLUDING_PREFIX}{segment_id}"


def segments_overlap(a: Mapping[Dimension, str], b: Mapping[Dimension, str]) -> bool:
    """Two filter sets can share traffic unless they fix a dimension differently."""
    return all(a[d] == b[d] for d in a.keys() & b.keys())


def row_set_hash(lines: Iterable[int]) -> str:
    payload = ",".join(str(n) for n in sorted(lines))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def combined_row_set_hash(*hashes: str) -> str:
    """Row-set hash for the union of disjoint row sets that already have hashes."""
    return hashlib.sha256("+".join(sorted(hashes)).encode("ascii")).hexdigest()


# --- Aggregation -------------------------------------------------------------


def as_decimal(value: Any) -> Decimal:
    """Exact Decimal for a stored sum (Python or numpy integer, or Decimal)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return Decimal(int(value))
    raise TypeError(f"not an exact numeric sum: {value!r}")


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return date.fromisoformat(str(value).strip())


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("counter is boolean")
    if isinstance(value, int):
        return value
    as_decimal = _to_decimal(value)
    if as_decimal != as_decimal.to_integral_value():
        raise ValueError(f"counter is not an integer: {value!r}")
    return int(as_decimal)


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int) and not isinstance(value, bool):
        result = Decimal(value)
    else:
        # Floats go through their shortest repr, never their binary expansion.
        result = Decimal(str(value).strip())
    if not result.is_finite():
        raise ValueError(f"non-finite numeric value: {value!r}")
    return result


def prepare_rows(data: LoadedData) -> pd.DataFrame:
    """Typed per-row frame: dimension labels, day, integer counters, Decimal money/rates.

    Adds the helper products and validity flags that make every rate a ratio
    of sums. Raises on values the loader should already have rejected.
    """
    frame = data.frame
    numeric = [c.value for c in InputColumn if c not in DIMENSION_COLUMNS.values()]
    unparsed = [c for c in numeric if frame[c].isna().any()]
    if unparsed:
        raise ValueError(
            f"cannot aggregate cells that failed parsing in {unparsed}; the quality report is fatal (DQ01)"
        )
    out = pd.DataFrame(index=frame.index)
    out["line"] = [int(data.source_lines.get(i, -1)) for i in frame.index]
    if (out["line"] < 0).any():
        missing = [i for i in frame.index if i not in data.source_lines]
        raise ValueError(f"rows without a source line: {missing[:5]}")
    out["day"] = [_to_date(v) for v in frame[InputColumn.DAY.value]]
    for dim, column in DIMENSION_COLUMNS.items():
        out[dim.value] = [str(v) for v in frame[column.value]]
    for metric in COUNTERS:
        out[metric.value] = pd.Series(
            [_to_int(v) for v in frame[ADDITIVE_SOURCE[metric].value]], index=frame.index, dtype=object
        )
    for metric in MONEY:
        out[metric.value] = [_to_decimal(v) for v in frame[ADDITIVE_SOURCE[metric].value]]
    floor = [_to_decimal(v) for v in frame[InputColumn.OUR_BIDFLOOR.value]]
    bid = [_to_decimal(v) for v in frame[InputColumn.DEMAND_BIDPRICE.value]]

    requests = out[MetricKey.REQUESTS.value].tolist()
    responses = out[MetricKey.RESPONSES.value].tolist()
    wins = out[MetricKey.WINS.value].tolist()
    impressions = out[MetricKey.IMPRESSIONS.value].tolist()
    revenue = out[MetricKey.REVENUE.value].tolist()
    cost = out[MetricKey.COST.value].tolist()

    bf = [r > 0 and f > 0 for r, f in zip(responses, floor, strict=True)]
    invalid = [
        min(q, s, w, i) < 0
        or s > q
        or w > s
        or i > w
        for q, s, w, i in zip(requests, responses, wins, impressions, strict=True)
    ]
    out[FLOOR_X_REQUESTS] = [q * f for q, f in zip(requests, floor, strict=True)]
    out[BID_X_RESPONSES] = [s * b for s, b in zip(responses, bid, strict=True)]
    out[ZERO_BID_RESPONSES] = pd.Series(
        [s if b == 0 else 0 for s, b in zip(responses, bid, strict=True)], index=frame.index, dtype=object
    )
    out[BF_RESPONSES] = pd.Series(
        [s if ok else 0 for s, ok in zip(responses, bf, strict=True)], index=frame.index, dtype=object
    )
    out[BF_BID_X_RESPONSES] = [
        s * b if ok else ZERO for s, b, ok in zip(responses, bid, bf, strict=True)
    ]
    out[BF_FLOOR_X_RESPONSES] = [
        s * f if ok else ZERO for s, f, ok in zip(responses, floor, bf, strict=True)
    ]
    out[FUNNEL_INVALID_ROWS] = pd.Series([int(x) for x in invalid], index=frame.index, dtype=object)
    out[FUNNEL_INVALID_REQUESTS] = pd.Series(
        [q if x else 0 for q, x in zip(requests, invalid, strict=True)], index=frame.index, dtype=object
    )
    out[NEGATIVE_MONEY_ROWS] = pd.Series(
        [int(r < 0 or c < 0) for r, c in zip(revenue, cost, strict=True)], index=frame.index, dtype=object
    )
    out[ROW_COUNT] = pd.Series([1] * len(frame), index=frame.index, dtype=object)
    return out


def _group(
    rows: pd.DataFrame, dims: Sequence[Dimension], by_day: bool, keep_lines: bool
) -> pd.DataFrame:
    keys = [d.value for d in dims] + (["day"] if by_day else [])
    work = rows.assign(_all=0)
    grouped = work.groupby(["_all", *keys], sort=True, dropna=False)
    sums = grouped[list(SUM_COLUMNS)].agg(lambda s: sum(s.tolist(), start=type(s.iloc[0])(0)))
    sums["row_set_hash"] = grouped["line"].agg(row_set_hash)
    if keep_lines:
        sums["lines"] = grouped["line"].agg(lambda s: tuple(sorted(s.tolist())))
    return sums.reset_index().drop(columns="_all")


def _with_ids(
    frame: pd.DataFrame,
    namespace: str,
    dims: Sequence[Dimension],
    cohort_id: str,
    segments: dict[str, Segment],
) -> pd.DataFrame:
    ids = []
    for labels in zip(*(frame[d.value] for d in dims), strict=True) if dims else ((),) * len(frame):
        segment = make_segment(namespace, dict(zip(dims, labels, strict=True)), cohort_id)
        segments.setdefault(segment.segment_id, segment)
        ids.append(segment.segment_id)
    frame.insert(0, "segment_id", ids)
    frame.insert(1, "level", level_name(dims))
    frame.insert(2, "cohort_id", cohort_id)
    for dim in Dimension:
        if dim not in dims:
            frame[dim.value] = None
    return frame


def _top_shares(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    positive = sorted((v for v in values if v > 0), reverse=True)
    gross = sum(positive, ZERO)
    return gross, (positive[0] if positive else ZERO), sum(positive[:2], ZERO)


def _day_stats(daily: pd.DataFrame, dates: set[date]) -> pd.DataFrame:
    rows = []
    in_period = daily[daily["day"].isin(dates)]
    for segment_id, group in in_period.groupby("segment_id", sort=False):
        revenue = group[MetricKey.REVENUE.value].tolist()
        profit = group[MetricKey.PROFIT.value].tolist()
        gross_rev, max_rev, top_rev = _top_shares(revenue)
        gross_pro, max_pro, top_pro = _top_shares(profit)
        rows.append(
            {
                "segment_id": segment_id,
                "observed_days": len(group),
                "revenue_positive_days": sum(1 for v in revenue if v > 0),
                "gross_positive_revenue": gross_rev,
                "max_day_revenue": max_rev,
                "top_two_days_revenue": top_rev,
                "gross_positive_profit": gross_pro,
                "max_day_profit": max_pro,
                "top_two_days_profit": top_pro,
            }
        )
    return pd.DataFrame(rows)


def _stability(row: Mapping[str, Any], config: AnalyticsConfig) -> tuple[bool, str | None]:
    reasons = []
    if row["observed_days"] < config.min_stable_days:
        reasons.append("few_observed_days")
    if row["revenue_positive_days"] < config.min_stable_days:
        reasons.append("few_revenue_positive_days")
    for metric in ("revenue", "profit"):
        gross = row[f"gross_positive_{metric}"]
        if gross < config.concentration_min_positive or row["observed_days"] < config.min_stable_days:
            continue
        if (
            row[f"max_day_{metric}"] / gross >= config.concentration_max_day_share
            or row[f"top_two_days_{metric}"] / gross >= config.concentration_max_two_day_share
        ):
            reasons.append(f"{metric}_concentrated")
    return (not reasons), (",".join(reasons) or None)


def _gate_roles(config: AnalyticsConfig) -> dict[str, Decimal]:
    roles = {BASE: Decimal(1), EXPLORATORY: config.exploratory_gate_fraction}
    for m in config.sensitivity_multipliers:
        roles[sensitivity_role(m)] = m
    return roles


def _add_gates(frame: pd.DataFrame, config: AnalyticsConfig) -> None:
    requests = frame[MetricKey.REQUESTS.value]
    revenue = frame[MetricKey.REVENUE.value]
    wins = frame[MetricKey.WINS.value]
    for role, m in _gate_roles(config).items():
        frame[gate_column(GATE_VOLUME, role)] = [
            q >= config.min_requests * m and r >= config.min_revenue * m
            for q, r in zip(requests, revenue, strict=True)
        ]
        frame[gate_column(GATE_WASTE, role)] = [q >= config.waste_min_requests * m for q in requests]
        frame[gate_column(GATE_WINS, role)] = [
            w >= config.wins_min_wins * m and r >= config.min_revenue * m
            for w, r in zip(wins, revenue, strict=True)
        ]


def _flagged_dates(quality: QualityReport, namespace: str, dates: Sequence[date]) -> set[date]:
    """Days a DQ06 issue marks as possibly incomplete (single-day period refs)."""
    by_id = {period_id_for(namespace, [d]): d for d in dates}
    flagged: set[date] = set()
    for issue in quality.issues:
        if issue.rule_id is QualityRuleId.DQ06:
            flagged.update(by_id[p] for p in issue.period_ids if p in by_id)
    return flagged


def _period_status(quality: QualityReport, flagged: bool) -> CoverageStatus:
    if flagged or any(i.rule_id is QualityRuleId.DQ06 for i in quality.issues):
        return CoverageStatus.POSSIBLY_INCOMPLETE
    for result in quality.rule_results:
        if result.rule_id is QualityRuleId.DQ06 and result.outcome is RuleOutcome.NOT_TESTABLE:
            return CoverageStatus.INSUFFICIENT_DATA
    return CoverageStatus.UNVERIFIED


def aggregate(data: LoadedData, quality: QualityReport, config: AnalyticsConfig) -> MetricsStore:
    """Build account, single-dimension, pairwise and five-dimension aggregates plus daily series.

    Only observed groups are produced. Periods are the full input period, an
    ``excluding possibly incomplete days`` sensitivity period when quality
    flags such days, and one period per observed day (daily table).
    """
    namespace = quality.run_namespace
    rows = prepare_rows(data)
    dates = sorted(set(data.dates) | set(rows["day"]))
    if not dates:
        raise ValueError("no observed dates to aggregate")
    flagged = _flagged_dates(quality, namespace, dates)

    full = make_period(namespace, dates, _period_status(quality, bool(flagged)))
    multi_day = [full]
    if flagged and len(flagged) < len(dates):
        kept = [d for d in dates if d not in flagged]
        multi_day.append(make_period(namespace, kept, CoverageStatus.UNVERIFIED))
    single_day = {
        d: make_period(
            namespace,
            [d],
            CoverageStatus.POSSIBLY_INCOMPLETE if d in flagged else CoverageStatus.UNVERIFIED,
        )
        for d in dates
    }
    periods = {p.period_id: p for p in (*multi_day, *single_day.values())}

    cohorts = [(FULL_INPUT, rows, LEVELS)]
    invalid = rows[FUNNEL_INVALID_ROWS].astype(bool)
    if invalid.any() and not invalid.all():
        cohorts.append((VALID_COUNTERS, rows[~invalid], VALID_COHORT_LEVELS))

    segments: dict[str, Segment] = {}
    daily_parts, aggregate_parts = [], []
    for cohort_id, cohort_rows, levels in cohorts:
        for dims in levels:
            keep_lines = dims == FIVE
            daily = _with_ids(_group(cohort_rows, dims, True, keep_lines), namespace, dims, cohort_id, segments)
            daily.insert(3, "period_id", [single_day[d].period_id for d in daily["day"]])
            daily_parts.append(daily)
            for period in multi_day:
                in_period = cohort_rows[cohort_rows["day"].isin(set(period.included_dates))]
                if in_period.empty:
                    continue
                agg = _with_ids(_group(in_period, dims, False, keep_lines), namespace, dims, cohort_id, segments)
                agg.insert(3, "period_id", period.period_id)
                stats = _day_stats(daily, set(period.included_dates))
                aggregate_parts.append(agg.merge(stats, on="segment_id", how="left", validate="1:1"))

    daily_frame = pd.concat(daily_parts, ignore_index=True)
    agg_frame = pd.concat(aggregate_parts, ignore_index=True)
    stability = [_stability(r, config) for r in agg_frame.to_dict("records")]
    agg_frame["stable"] = [s for s, _ in stability]
    agg_frame["stability_reason"] = [r for _, r in stability]
    _add_gates(agg_frame, config)

    flags: dict[str, list[str]] = {}
    for issue in quality.issues:
        for segment_id in issue.segment_ids:
            flags.setdefault(segment_id, []).append(issue.issue_id)

    return MetricsStore(
        run_namespace=namespace,
        segments=MappingProxyType(segments),
        periods=MappingProxyType(periods),
        aggregates=agg_frame,
        daily=daily_frame,
        quality_flags=MappingProxyType({k: tuple(v) for k, v in flags.items()}),
        input_sha256=data.input_sha256,
    )


# --- Lookups -----------------------------------------------------------------


def full_period(store: MetricsStore) -> Period:
    """The period covering every observed date."""
    return max(store.periods.values(), key=lambda p: (len(p.included_dates), p.period_id))


def day_period(store: MetricsStore, day: date) -> Period:
    return store.periods[period_id_for(store.run_namespace, [day])]


def stability_reasons(row: Mapping[str, Any]) -> set[str]:
    reason = row.get("stability_reason")
    return set(reason.split(",")) if isinstance(reason, str) else set()


# Lookup indexes over a store's frames, built on first use. ``aggregate`` never
# mutates a frame after building it, so an index stays valid for the frame's
# life; it is dropped with the frame. At full size (100k+ rows) repeated
# whole-frame boolean filters dominated detection and evidence building.
_INDEXES: dict[tuple[int, str], tuple[weakref.ref, Any]] = {}


def _frame_index(frame: pd.DataFrame, kind: str, build) -> Any:
    key = (id(frame), kind)
    hit = _INDEXES.get(key)
    if hit is None or hit[0]() is not frame:
        ref = weakref.ref(frame, lambda _, key=key: _INDEXES.pop(key, None))
        hit = (ref, build(frame))
        _INDEXES[key] = hit
    return hit[1]


def _first_rows(frame: pd.DataFrame) -> dict[tuple[str, str], int]:
    positions: dict[tuple[str, str], int] = {}
    for n, key in enumerate(zip(frame["segment_id"], frame["period_id"], strict=True)):
        positions.setdefault(key, n)
    return positions


def stats_row(store: MetricsStore, segment_id: str, period_id: str) -> dict[str, Any] | None:
    """Sums and coverage for an observed (segment, period), or ``None`` when unobserved."""
    period = store.periods[period_id]
    if len(period.included_dates) == 1:
        frame = store.daily
    else:
        frame = store.aggregates
    n = _frame_index(frame, "first_rows", _first_rows).get((segment_id, period_id))
    if n is None:
        return None
    return frame.iloc[n].to_dict()


def daily_rows(store: MetricsStore, segment_id: str) -> pd.DataFrame:
    """Observed daily rows for a segment, sorted by day."""
    frame = store.daily
    groups = _frame_index(
        frame, "segment_rows", lambda f: f.groupby("segment_id", sort=False).indices
    )
    positions = groups.get(segment_id)
    if positions is None:
        return frame.iloc[0:0].sort_values("day")
    return frame.iloc[positions].sort_values("day")


class _FiveRows:
    """One period's five-dimension rows as arrays, with per-label masks built on demand."""

    def __init__(self, rows: pd.DataFrame) -> None:
        self.size = len(rows)
        self.labels = {d: rows[d.value].to_numpy(dtype=object) for d in Dimension}
        self.columns = {c: rows[c].to_numpy(dtype=object) for c in SUM_COLUMNS}
        self.lines = rows["lines"].to_numpy(dtype=object)
        self._masks: dict[tuple[Dimension, str], np.ndarray] = {}

    def mask(self, filters: Mapping[Dimension, str]) -> np.ndarray:
        out = np.ones(self.size, dtype=bool)
        for dim, label in filters.items():
            key = (dim, label)
            if key not in self._masks:
                self._masks[key] = self.labels[dim] == label
            out &= self._masks[key]
        return out


def _five_rows(store: MetricsStore, period: Period) -> _FiveRows | None:
    frame = store.daily if len(period.included_dates) == 1 else store.aggregates

    def build(f: pd.DataFrame) -> dict[str, _FiveRows]:
        five = f[(f["level"] == level_name(FIVE)) & (f["cohort_id"] == FULL_INPUT)]
        return {pid: _FiveRows(rows) for pid, rows in five.groupby("period_id", sort=False)}

    return _frame_index(frame, "five_rows", build).get(period.period_id)


def combined_stats(
    store: MetricsStore,
    period: Period,
    include: Mapping[Dimension, str],
    exclude: Mapping[Dimension, str] | None = None,
) -> dict[str, Any] | None:
    """Exact sums for any filter set (optionally minus a sub-slice) from five-dimension rows."""
    five = _five_rows(store, period)
    if five is None:
        return None
    mask = five.mask(include)
    if exclude is not None:
        mask &= ~five.mask(exclude)
    chosen = np.flatnonzero(mask)
    if not len(chosen):
        return None
    stats: dict[str, Any] = {}
    for c in SUM_COLUMNS:
        values = five.columns[c][chosen].tolist()
        stats[c] = sum(values, start=type(values[0])(0))
    lines = [n for group in five.lines[chosen] for n in group]
    stats["row_set_hash"] = row_set_hash(lines)
    return stats


# --- Facts -------------------------------------------------------------------


_COUNT_SOURCES = {m: [ADDITIVE_SOURCE[m]] for m in ADDITIVE}

_RATIOS: dict[MetricKey, tuple[MetricKey, MetricKey, Decimal, Unit]] = {
    MetricKey.RESPONSE_RATE: (MetricKey.RESPONSES, MetricKey.REQUESTS, Decimal(1), Unit.FRACTION),
    MetricKey.WIN_RATE: (MetricKey.WINS, MetricKey.RESPONSES, Decimal(1), Unit.FRACTION),
    MetricKey.IMPRESSION_TO_WIN_RATE: (MetricKey.IMPRESSIONS, MetricKey.WINS, Decimal(1), Unit.RATIO),
    MetricKey.ECPM: (
        MetricKey.REVENUE, MetricKey.IMPRESSIONS, THOUSAND, Unit.EXPORT_DOLLAR_PER_THOUSAND_IMPRESSIONS,
    ),
    MetricKey.REVENUE_PER_MILLION_REQUESTS: (
        MetricKey.REVENUE, MetricKey.REQUESTS, MILLION, Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS,
    ),
    MetricKey.PROFIT_PER_MILLION_REQUESTS: (
        MetricKey.PROFIT, MetricKey.REQUESTS, MILLION, Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS,
    ),
    MetricKey.MARGIN: (MetricKey.PROFIT, MetricKey.REVENUE, Decimal(1), Unit.FRACTION),
}
_SHARES: dict[MetricKey, MetricKey] = {
    MetricKey.REQUEST_SHARE: MetricKey.REQUESTS,
    MetricKey.REVENUE_SHARE: MetricKey.REVENUE,
    MetricKey.PROFIT_SHARE: MetricKey.PROFIT,
}
FUNNEL_METRICS = frozenset(
    {MetricKey.RESPONSE_RATE, MetricKey.WIN_RATE, MetricKey.IMPRESSION_TO_WIN_RATE}
)
MONEY_RATE_METRICS = frozenset(
    {
        MetricKey.ECPM,
        MetricKey.REVENUE_PER_MILLION_REQUESTS,
        MetricKey.PROFIT_PER_MILLION_REQUESTS,
        MetricKey.MARGIN,
        MetricKey.REVENUE_SHARE,
        MetricKey.PROFIT_SHARE,
    }
)
_CONCENTRATION: dict[MetricKey, tuple[str, str]] = {
    MetricKey.MAX_DAY_REVENUE_SHARE: ("max_day_revenue", "gross_positive_revenue"),
    MetricKey.TOP_TWO_DAY_REVENUE_SHARE: ("top_two_days_revenue", "gross_positive_revenue"),
    MetricKey.MAX_DAY_PROFIT_SHARE: ("max_day_profit", "gross_positive_profit"),
    MetricKey.TOP_TWO_DAY_PROFIT_SHARE: ("top_two_days_profit", "gross_positive_profit"),
}


def ratio_value(numerator: Decimal, denominator: Decimal, scale: Decimal = Decimal(1)) -> tuple[Decimal | None, str | None]:
    """``numerator / denominator * scale``; a zero or negative denominator is undefined, never infinite."""
    denominator = as_decimal(denominator)
    if denominator == 0:
        return None, "denominator is zero"
    if denominator < 0:
        return None, "denominator is negative"
    return as_decimal(numerator) / as_decimal(denominator) * scale, None


class IssueIndex:
    """Which quality issues touch a (segment, period); unknown references count as touching.

    Two slices share traffic unless they fix a dimension differently, so an
    issue slice fixing dimensions ``D`` touches a slice fixing ``E`` exactly
    when their labels agree on ``D & E``. Issue slices are indexed by those
    labels: a full-size export has thousands of issues over hundreds of slices
    each, and pairwise checks dominated the run.
    """

    def __init__(self, store: MetricsStore, quality: QualityReport) -> None:
        self._store = store
        self._issues = list(quality.issues)
        # Issues touching every slice: no scope, or a scope not in the store.
        self._everywhere: set[int] = set()
        self._by_dims: dict[tuple[Dimension, ...], list[tuple[Mapping[Dimension, str], int]]] = {}
        for n, issue in enumerate(self._issues):
            if not issue.segment_ids:
                self._everywhere.add(n)
                continue
            for segment_id in issue.segment_ids:
                other = store.segments.get(segment_id)
                if other is None:
                    self._everywhere.add(n)
                    break
                dims = tuple(d for d in Dimension if d in other.filters)
                self._by_dims.setdefault(dims, []).append((other.filters, n))
        self._projections: dict[tuple[tuple[Dimension, ...], tuple[Dimension, ...]], dict] = {}
        self._segments: dict[str, frozenset[int]] = {}
        self._periods: dict[tuple[str, bool], frozenset[int]] = {}
        self._memo: dict[tuple[str, str], list[str]] = {}
        self._restrictions: dict[tuple[str, str], frozenset[InferenceRestriction]] = {}

    def _touching_segment(self, segment: Segment) -> frozenset[int]:
        hit = self._segments.get(segment.segment_id)
        if hit is not None:
            return hit
        out = set(self._everywhere)
        for dims, entries in self._by_dims.items():
            shared = tuple(d for d in dims if d in segment.filters)
            index = self._projections.get((dims, shared))
            if index is None:
                index = {}
                for filters, n in entries:
                    index.setdefault(tuple(filters[d] for d in shared), set()).add(n)
                self._projections[(dims, shared)] = index
            out |= index.get(tuple(segment.filters[d] for d in shared), set())
        hit = self._segments[segment.segment_id] = frozenset(out)
        return hit

    def _dated(self, period: Period, covers: bool) -> frozenset[int]:
        """Issues on dates that intersect ``period`` (or, ``covers``, contain all of it)."""
        key = (period.period_id, covers)
        hit = self._periods.get(key)
        if hit is None:
            test = self._covers_period if covers else self._touches_period
            hit = frozenset(
                n for n, issue in enumerate(self._issues) if test(issue.period_ids, period)
            )
            self._periods[key] = hit
        return hit

    def _touches_period(self, issue_periods: Sequence[str], period: Period) -> bool:
        if not issue_periods:
            return True
        dates = set(period.included_dates)
        for period_id in issue_periods:
            other = self._store.periods.get(period_id)
            if other is None or dates & set(other.included_dates):
                return True
        return False

    def issues_for(self, segment: Segment, period: Period) -> list[str]:
        key = (segment.segment_id, period.period_id)
        if key not in self._memo:
            chosen = self._touching_segment(segment) & self._dated(period, False)
            self._memo[key] = sorted(self._issues[n].issue_id for n in chosen)
        return list(self._memo[key])

    def _covers_period(self, issue_periods: Sequence[str], period: Period) -> bool:
        if not issue_periods:
            return True
        dates = set(period.included_dates)
        for period_id in issue_periods:
            other = self._store.periods.get(period_id)
            if other is None or dates <= set(other.included_dates):
                return True
        return False

    def restrictions_for(self, segment: Segment, period: Period) -> set[InferenceRestriction]:
        """Restrictions from issues on overlapping traffic whose period covers all of ``period``.

        An issue on a strict sub-period (e.g. one possibly incomplete day)
        does not block a whole-period inference; detectors handle that with
        the sensitivity period instead.
        """
        key = (segment.segment_id, period.period_id)
        if key not in self._restrictions:
            chosen = self._touching_segment(segment) & self._dated(period, True)
            self._restrictions[key] = frozenset(
                r for n in chosen for r in self._issues[n].inference_restrictions
            )
        return set(self._restrictions[key])


class FactBuilder:
    """Creates typed facts with deterministic IDs and records each one it makes.

    Ratios create their numerator and denominator facts as parents, so the
    collected ``facts`` are closed under ``parent_fact_ids``.
    """

    def __init__(self, store: MetricsStore, quality: QualityReport) -> None:
        if not store.input_sha256:
            raise ValueError("MetricsStore has no input_sha256; build it with aggregate()")
        self.store = store
        self.namespace = store.run_namespace
        self.input_sha256 = store.input_sha256
        self.issues = IssueIndex(store, quality)
        self.facts: dict[str, Fact] = {}
        # Daily rows per segment as records; every candidate reads the account's.
        self._daily_records: dict[str, list[dict[str, Any]]] = {}

    def _fact(
        self,
        *,
        kind: FactKind,
        segment: Segment,
        period: Period,
        metric: MetricKey,
        formula_id: str,
        value: Decimal | None,
        unit: Unit,
        validity: Validity,
        source_columns: Sequence[InputColumn],
        row_hash: str,
        parents: Sequence[Fact],
        weighting: Weighting,
        comparison: ComparisonDetail | None = None,
        extra_spec: Mapping[str, Any] | None = None,
    ) -> Fact:
        spec = {
            "kind": kind.value,
            "metric": metric.value,
            "formula_id": formula_id,
            "segment_id": segment.segment_id,
            "period_id": period.period_id,
            **(extra_spec or {}),
        }
        fact_id = make_id(IdPrefix.FACT, self.namespace, spec)
        if fact_id in self.facts:
            return self.facts[fact_id]
        fact = Fact(
            fact_id=fact_id,
            run_namespace=self.namespace,
            kind=kind,
            segment=segment,
            period=period,
            metric=metric,
            value=value,
            unit=unit,
            validity=validity,
            provenance=Provenance(
                input_sha256=self.input_sha256,
                schema_version=SCHEMA_VERSION,
                formula_id=formula_id,
                source_columns=sorted(set(source_columns), key=list(InputColumn).index),
                source_row_set_hash=row_hash,
                source_line_ranges_ref=f"private_rows_{stable_digest(['rows', row_hash])}",
                parent_fact_ids=[p.fact_id for p in parents],
                aggregation_keys=list(segment.filters),
                weighting=weighting,
                quality_issue_ids=self.issues.issues_for(segment, period),
            ),
            comparison=comparison,
        )
        self.facts[fact_id] = fact
        return fact

    # Measured sums and counts.

    def additive(self, segment: Segment, period: Period, stats: Mapping[str, Any], metric: MetricKey) -> Fact:
        value = as_decimal(stats[metric.value])
        unit = Unit.EXPORT_DOLLAR if metric in MONEY else Unit.COUNT
        return self._fact(
            kind=FactKind.MEASURED, segment=segment, period=period, metric=metric,
            formula_id=f"{metric.value}_v1", value=value, unit=unit,
            validity=Validity(status=ValidityStatus.VALID), source_columns=_COUNT_SOURCES[metric],
            row_hash=stats["row_set_hash"], parents=(), weighting=Weighting.SUM,
        )

    def days(self, segment: Segment, period: Period, stats: Mapping[str, Any], metric: MetricKey) -> Fact:
        """``observed_days``, ``revenue_positive_days`` or ``row_count`` of an observed aggregate."""
        column = {
            MetricKey.OBSERVED_DAYS: "observed_days",
            MetricKey.REVENUE_POSITIVE_DAYS: "revenue_positive_days",
            MetricKey.ROW_COUNT: ROW_COUNT,
        }[metric]
        sources = [InputColumn.DAY] + ([InputColumn.REVENUE] if metric is MetricKey.REVENUE_POSITIVE_DAYS else [])
        return self._fact(
            kind=FactKind.MEASURED, segment=segment, period=period, metric=metric,
            formula_id=f"{metric.value}_v1", value=as_decimal(stats[column]),
            unit=Unit.COUNT if metric is MetricKey.ROW_COUNT else Unit.DAYS,
            validity=Validity(status=ValidityStatus.VALID), source_columns=sources,
            row_hash=stats["row_set_hash"], parents=(), weighting=Weighting.NOT_APPLICABLE,
        )

    # Calculated rates.

    def _taint(self, metric: MetricKey, stats: Mapping[str, Any]) -> str | None:
        if metric in FUNNEL_METRICS and stats[FUNNEL_INVALID_ROWS] > 0:
            return f"includes {stats[FUNNEL_INVALID_ROWS]} rows with inconsistent counter relationships"
        if metric in MONEY_RATE_METRICS and stats[NEGATIVE_MONEY_ROWS] > 0:
            return f"includes {stats[NEGATIVE_MONEY_ROWS]} rows with negative revenue or cost"
        return None

    @staticmethod
    def _validity(value: Decimal | None, undefined: str | None, taint: str | None) -> Validity:
        if value is None:
            return Validity(status=ValidityStatus.UNDEFINED, reason=undefined)
        if taint:
            return Validity(status=ValidityStatus.TAINTED, reason=taint)
        return Validity(status=ValidityStatus.VALID)

    def rate(
        self,
        segment: Segment,
        period: Period,
        stats: Mapping[str, Any],
        metric: MetricKey,
        account: tuple[Segment, Mapping[str, Any]] | None = None,
    ) -> Fact:
        """A ratio-of-sums metric; shares need ``account`` (same period and cohort)."""
        if metric in _RATIOS:
            num_key, den_key, scale, unit = _RATIOS[metric]
            num = self.additive(segment, period, stats, num_key)
            den = self.additive(segment, period, stats, den_key)
            value, reason = ratio_value(num.value, den.value, scale)
            if reason:
                reason = f"{den_key.value} {reason}"
            return self._fact(
                kind=FactKind.CALCULATED, segment=segment, period=period, metric=metric,
                formula_id=f"{metric.value}_v1", value=value, unit=unit,
                validity=self._validity(value, reason, self._taint(metric, stats)),
                source_columns=[*_COUNT_SOURCES[num_key], *_COUNT_SOURCES[den_key]],
                row_hash=stats["row_set_hash"], parents=(num, den), weighting=Weighting.RATIO_OF_SUMS,
            )
        if metric in _SHARES:
            if account is None:
                raise ValueError(f"{metric} needs the account baseline")
            base = _SHARES[metric]
            account_segment, account_stats = account
            num = self.additive(segment, period, stats, base)
            den = self.additive(account_segment, period, account_stats, base)
            value, reason = ratio_value(num.value, den.value)
            if reason:
                reason = f"account {base.value} total {reason}"
            return self._fact(
                kind=FactKind.CALCULATED, segment=segment, period=period, metric=metric,
                formula_id=f"{metric.value}_v1", value=value, unit=Unit.FRACTION,
                validity=self._validity(value, reason, self._taint(metric, stats)),
                source_columns=_COUNT_SOURCES[base], row_hash=stats["row_set_hash"],
                parents=(num, den), weighting=Weighting.RATIO_OF_SUMS,
            )
        if metric is MetricKey.REQUEST_WEIGHTED_FLOOR:
            den = self.additive(segment, period, stats, MetricKey.REQUESTS)
            value, reason = ratio_value(stats[FLOOR_X_REQUESTS], den.value)
            return self._fact(
                kind=FactKind.CALCULATED, segment=segment, period=period, metric=metric,
                formula_id=f"{metric.value}_v1", value=value, unit=Unit.EXPORT_PRICE_AS_EXPORTED,
                validity=self._validity(value, reason and f"requests {reason}", None),
                source_columns=[InputColumn.OUR_BIDFLOOR, InputColumn.DEMAND_REQUESTS],
                row_hash=stats["row_set_hash"], parents=(den,), weighting=Weighting.REQUEST_WEIGHTED,
            )
        if metric is MetricKey.RESPONSE_WEIGHTED_BID:
            den = self.additive(segment, period, stats, MetricKey.RESPONSES)
            value, reason = ratio_value(stats[BID_X_RESPONSES], den.value)
            return self._fact(
                kind=FactKind.CALCULATED, segment=segment, period=period, metric=metric,
                formula_id=f"{metric.value}_v1", value=value, unit=Unit.EXPORT_PRICE_AS_EXPORTED,
                validity=self._validity(value, reason and f"responses {reason}", None),
                source_columns=[InputColumn.DEMAND_BIDPRICE, InputColumn.DEMAND_RESPONSES],
                row_hash=stats["row_set_hash"], parents=(den,), weighting=Weighting.RESPONSE_WEIGHTED,
            )
        if metric is MetricKey.ZERO_BID_RESPONSE_SHARE:
            den = self.additive(segment, period, stats, MetricKey.RESPONSES)
            value, reason = ratio_value(stats[ZERO_BID_RESPONSES], den.value)
            return self._fact(
                kind=FactKind.CALCULATED, segment=segment, period=period, metric=metric,
                formula_id=f"{metric.value}_v1", value=value, unit=Unit.FRACTION,
                validity=self._validity(value, reason and f"responses {reason}", None),
                source_columns=[InputColumn.DEMAND_BIDPRICE, InputColumn.DEMAND_RESPONSES],
                row_hash=stats["row_set_hash"], parents=(den,), weighting=Weighting.RESPONSE_WEIGHTED,
            )
        if metric in _CONCENTRATION:
            top, gross = _CONCENTRATION[metric]
            base = MetricKey.REVENUE if gross.endswith("revenue") else MetricKey.PROFIT
            parents = self.daily_facts(segment, period, base)
            value, reason = ratio_value(stats[top], stats[gross])
            if reason:
                reason = f"gross positive daily {base.value} is zero"
            return self._fact(
                kind=FactKind.CALCULATED, segment=segment, period=period, metric=metric,
                formula_id=f"{metric.value}_v1", value=value, unit=Unit.FRACTION,
                validity=self._validity(value, reason, None),
                source_columns=[InputColumn.DAY, ADDITIVE_SOURCE[base]],
                row_hash=stats["row_set_hash"], parents=parents, weighting=Weighting.RATIO_OF_SUMS,
            )
        raise ValueError(f"no rate formula for {metric}")

    def bid_floor(self, segment: Segment, period: Period, stats: Mapping[str, Any]) -> tuple[Fact, Fact, Fact]:
        """Response-weighted bid and floor on positive-response, positive-floor rows, and their ratio."""
        cohort = make_segment(self.namespace, segment.filters, BID_FLOOR_COHORT)
        responses = self._fact(
            kind=FactKind.MEASURED, segment=cohort, period=period, metric=MetricKey.RESPONSES,
            formula_id="responses_v1", value=as_decimal(stats[BF_RESPONSES]), unit=Unit.COUNT,
            validity=Validity(status=ValidityStatus.VALID),
            source_columns=[InputColumn.DEMAND_RESPONSES, InputColumn.OUR_BIDFLOOR],
            row_hash=stats["row_set_hash"], parents=(), weighting=Weighting.SUM,
        )
        out = []
        for metric, column, source in (
            (MetricKey.RESPONSE_WEIGHTED_BID, BF_BID_X_RESPONSES, InputColumn.DEMAND_BIDPRICE),
            (MetricKey.RESPONSE_WEIGHTED_FLOOR, BF_FLOOR_X_RESPONSES, InputColumn.OUR_BIDFLOOR),
        ):
            value, reason = ratio_value(stats[column], responses.value)
            out.append(
                self._fact(
                    kind=FactKind.CALCULATED, segment=cohort, period=period, metric=metric,
                    formula_id=f"{metric.value}_v1", value=value, unit=Unit.EXPORT_PRICE_AS_EXPORTED,
                    validity=self._validity(value, reason and f"positive-floor responses {reason}", None),
                    source_columns=[source, InputColumn.DEMAND_RESPONSES, InputColumn.OUR_BIDFLOOR],
                    row_hash=stats["row_set_hash"], parents=(responses,),
                    weighting=Weighting.RESPONSE_WEIGHTED,
                )
            )
        bid, floor = out
        return bid, floor, self.comparison(bid, floor, list(segment.filters))

    # Daily evidence.

    def daily_facts(self, segment: Segment, period: Period, metric: MetricKey) -> list[Fact]:
        """One fact per observed day of ``segment`` inside ``period`` (full-input or valid cohort)."""
        records = self._daily_records.get(segment.segment_id)
        if records is None:
            records = daily_rows(self.store, segment.segment_id).to_dict("records")
            self._daily_records[segment.segment_id] = records
        dates = set(period.included_dates)
        facts = []
        for row in (r for r in records if r["day"] in dates):
            day = self.store.periods[row["period_id"]]
            if metric in ADDITIVE:
                facts.append(self.additive(segment, day, row, metric))
            else:
                facts.append(self.rate(segment, day, row, metric))
        return facts

    # Comparisons and changes.

    def comparison(self, left: Fact, right: Fact, matching_keys: Sequence[Dimension]) -> Fact:
        """Left versus right in the same unit; value is ``left / right`` when right is positive.

        The fact carries the left metric (e.g. bid versus floor is a
        ``response_weighted_bid`` comparison against a floor fact).
        """
        if left.unit is not right.unit:
            raise ValueError("comparison needs facts in the same unit")
        if left.value is None or right.value is None:
            relation, difference, ratio = Relation.UNDEFINED, None, None
            reason = "one side of the comparison is undefined"
        else:
            difference = left.value - right.value
            relation = Relation.GREATER if difference > 0 else Relation.LESS if difference < 0 else Relation.EQUAL
            ratio, reason = ratio_value(left.value, right.value)
            if reason:
                reason = f"baseline {reason}"
        tainted = [f for f in (left, right) if f.validity.status is ValidityStatus.TAINTED]
        if ratio is None:
            validity = Validity(status=ValidityStatus.UNDEFINED, reason=reason)
        elif tainted:
            validity = Validity(status=ValidityStatus.TAINTED, reason=tainted[0].validity.reason)
        else:
            validity = Validity(status=ValidityStatus.VALID)
        detail = ComparisonDetail(
            left_fact_id=left.fact_id, right_fact_id=right.fact_id,
            matching_keys=[d for d in Dimension if d in set(matching_keys)],
            relation=relation, difference=difference, ratio=ratio, difference_unit=left.unit,
        )
        return self._fact(
            kind=FactKind.COMPARISON, segment=left.segment, period=left.period, metric=left.metric,
            formula_id=f"comparison_{left.metric.value}_v1", value=ratio, unit=Unit.RATIO,
            validity=validity,
            source_columns=[*left.provenance.source_columns, *right.provenance.source_columns],
            row_hash=left.provenance.source_row_set_hash, parents=(left, right),
            weighting=left.provenance.weighting, comparison=detail,
            extra_spec={"left": left.fact_id, "right": right.fact_id},
        )

    def daily_change(self, earlier: Fact, later: Fact) -> Fact:
        """``(later - earlier) / earlier`` over a two-day period; null when the earlier value is not positive."""
        if earlier.metric is not later.metric or earlier.segment != later.segment:
            raise ValueError("daily change needs the same metric and segment")
        period = make_period(
            self.namespace,
            [*earlier.period.included_dates, *later.period.included_dates],
            CoverageStatus.POSSIBLY_INCOMPLETE
            if CoverageStatus.POSSIBLY_INCOMPLETE in (earlier.period.coverage_status, later.period.coverage_status)
            else CoverageStatus.UNVERIFIED,
        )
        if earlier.value is None or later.value is None:
            value, reason = None, "a day value is undefined"
        else:
            value, reason = ratio_value(later.value - earlier.value, earlier.value)
            if reason:
                reason = f"prior day value {reason.split(' ', 1)[1]}"
        tainted = [f for f in (earlier, later) if f.validity.status is ValidityStatus.TAINTED]
        return self._fact(
            kind=FactKind.CALCULATED, segment=later.segment, period=period, metric=MetricKey.DAILY_CHANGE,
            formula_id=f"daily_change_{later.metric.value}_v1", value=value, unit=Unit.FRACTION,
            validity=self._validity(value, reason, tainted[0].validity.reason if tainted else None),
            source_columns=[InputColumn.DAY, *later.provenance.source_columns],
            row_hash=combined_row_set_hash(
                earlier.provenance.source_row_set_hash, later.provenance.source_row_set_hash
            ),
            parents=(earlier, later), weighting=Weighting.RATIO_OF_SUMS,
        )
