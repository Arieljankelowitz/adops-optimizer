"""The eight opportunity detector families; they produce candidates, not conclusions.

Each family has a *pattern* (its DetectorConfig thresholds, evaluated on the
full-period sums of every observed segment) and an eligibility *gate* (the
AnalyticsConfig gates that ``aggregate`` stored as ``gate_*@<role>`` columns):

* pattern and base gate (and stability, for scale winners) -> ``eligible``;
* pattern and only the exploratory gate, or a quality restriction / tainted
  funnel / stability failure -> ``exploratory`` (the labelled exploratory table);
* a same-family parent explained by one narrower candidate -> ``rejected``
  with the reason retained for audit.

Evidence facts are built by code for every candidate before any truncation:
triggers with their denominators, daily series for the segment and the
account, matched peers, coverage, concentration and contrary evidence. Fact
creation never depends on thresholds, so ``candidate_evidence`` can rebuild
exactly the facts and overlap groups a candidate list references.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from statistics import median
from types import MappingProxyType
from typing import Any

import numpy as np

from contracts.config import DetectorConfig
from contracts.enums import (
    CoverageStatus,
    DetectorFamily,
    Dimension,
    EligibilityStatus,
    InferenceRestriction,
    MetricKey,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import Candidate, Fact, OverlapGroup, Period, QualityReport, Segment
from contracts.runtime import FactRegistry, MetricsStore
from pipeline.metrics import (
    BASE,
    EXCLUDING_PREFIX,
    EXPLORATORY,
    FIVE,
    FULL_INPUT,
    FUNNEL_INVALID_ROWS,
    GATE_VOLUME,
    GATE_WASTE,
    GATE_WINS,
    MILLION,
    SUM_COLUMNS,
    FactBuilder,
    as_decimal,
    combined_stats,
    daily_rows,
    excluding_cohort,
    full_period,
    gate_column,
    level_name,
    make_segment,
    ratio_value,
    segment_id_for,
    segments_overlap,
    sensitivity_roles,
    stability_reasons,
    stats_row,
)
from pipeline.quality import PEER_KEYS_DIFFER_FLAG, RANK_SENSITIVE_FLAG

Row = Mapping[str, Any]

FAMILY_GATE: dict[DetectorFamily, str] = {
    DetectorFamily.SCALE_WINNER: GATE_VOLUME,
    DetectorFamily.WASTED_REQUESTS: GATE_WASTE,
    DetectorFamily.MARGIN_LEAK: GATE_VOLUME,
    DetectorFamily.FLOOR_HIGH_BID: GATE_VOLUME,
    DetectorFamily.FLOOR_LOW_BID: GATE_VOLUME,
    DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: GATE_WINS,
    DetectorFamily.TREND: GATE_VOLUME,
    DetectorFamily.COMBINATION_OUTLIER: GATE_VOLUME,
}

# Quality restrictions that keep a family's candidates exploratory.
FAMILY_BLOCKERS: dict[DetectorFamily, frozenset[InferenceRestriction]] = {
    family: frozenset({InferenceRestriction.NO_RECOMMENDATIONS, *extra})
    for family, extra in {
        DetectorFamily.SCALE_WINNER: (
            InferenceRestriction.NO_STABLE_SCALE_CLAIM, InferenceRestriction.NO_YIELD_ESTIMATE,
        ),
        DetectorFamily.WASTED_REQUESTS: (InferenceRestriction.NO_YIELD_ESTIMATE,),
        DetectorFamily.MARGIN_LEAK: (),
        DetectorFamily.FLOOR_HIGH_BID: (),
        DetectorFamily.FLOOR_LOW_BID: (
            InferenceRestriction.NO_FUNNEL_INFERENCE, InferenceRestriction.NO_MATCHED_PEER_LABEL,
        ),
        DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: (InferenceRestriction.NO_FUNNEL_INFERENCE,),
        DetectorFamily.TREND: (InferenceRestriction.NO_TREND_CLAIM,),
        DetectorFamily.COMBINATION_OUTLIER: (InferenceRestriction.NO_YIELD_ESTIMATE,),
    }.items()
}
# Restrictions that block any candidate fixing the given dimension.
DIMENSION_BLOCKERS: dict[InferenceRestriction, Dimension] = {
    InferenceRestriction.NO_COUNTRY_OPPORTUNITY: Dimension.COUNTRY,
    InferenceRestriction.NO_FORMAT_SPECIFIC_TEST: Dimension.SIZE,
}
FUNNEL_FAMILIES = frozenset({DetectorFamily.FLOOR_LOW_BID, DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY})
FIVE_ONLY_FAMILIES = frozenset({DetectorFamily.FLOOR_LOW_BID, DetectorFamily.COMBINATION_OUTLIER})
# Fixed matching dimensions of a five-dimension slice's matched sibling peer.
PEER_KEYS: tuple[Dimension, ...] = tuple(d for d in FIVE if d is not Dimension.SUPPLY_ID)


def parent_dims(dims: Sequence[Dimension]) -> tuple[Dimension, ...]:
    """Immediate coarser observed level used for residual-parent contrasts."""
    if len(dims) <= 1:
        return ()
    if tuple(dims) == FIVE:
        return (Dimension.SUPPLY_ID, Dimension.COUNTRY)
    return (Dimension.SUPPLY_ID,)


def _num(row: Row, column: str) -> Decimal:
    return as_decimal(row[column])


def _rate(row: Row | None, num: str, den: str, scale: Decimal = Decimal(1)) -> Decimal | None:
    if row is None:
        return None
    return ratio_value(_num(row, num), _num(row, den), scale)[0]


def _subtract(a: Row, b: Row) -> dict[str, Any]:
    return {c: as_decimal(a[c]) - as_decimal(b[c]) for c in SUM_COLUMNS}


R = MetricKey


# --- Results -------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    trigger: tuple[str, ...]
    daily: tuple[str, ...]
    peer: tuple[str, ...]
    counter: tuple[str, ...]


@dataclass(frozen=True)
class SensitivityResult:
    """Eligible (family, segment_id) pairs per gate role and whether the shortlist moved."""

    eligible_by_role: Mapping[str, frozenset[tuple[DetectorFamily, str]]]
    shortlist_changed: Mapping[str, bool]
    sensitive_candidate_ids: frozenset[str]


@dataclass(frozen=True)
class Detection:
    candidates: tuple[Candidate, ...]
    facts: FactRegistry
    overlap_groups: Mapping[str, OverlapGroup]
    sensitivity: SensitivityResult


@dataclass(frozen=True)
class CandidateEvidence:
    facts: FactRegistry
    overlap_groups: Mapping[str, OverlapGroup]


@dataclass
class _Draft:
    family: DetectorFamily
    segment: Segment
    row: Row
    eligibility: EligibilityStatus
    evidence: Evidence
    candidate_id: str
    rejection_reason: str | None = None
    overlap_group_ids: list[str] = field(default_factory=list)


# --- Context -------------------------------------------------------------------


class _Context:
    def __init__(self, store: MetricsStore, quality: QualityReport, config: DetectorConfig) -> None:
        self.store = store
        self.config = config
        self.trend_cache: dict[str, list[Any]] = {}
        self.ns = store.run_namespace
        self.builder = FactBuilder(store, quality)
        self.period = full_period(store)
        agg = store.aggregates
        rows = agg[(agg["period_id"] == self.period.period_id) & (agg["cohort_id"] == FULL_INPUT)]
        self.rows: dict[str, Row] = {r["segment_id"]: r for r in rows.to_dict("records")}
        self.account_segment = store.segments[segment_id_for(self.ns, {})]
        self.account_row = self.rows[self.account_segment.segment_id]
        self.flagged_days = {
            p.included_dates[0]
            for p in store.periods.values()
            if len(p.included_dates) == 1 and p.coverage_status is CoverageStatus.POSSIBLY_INCOMPLETE
        }
        self.sensitivity_period = next(
            (
                p
                for p in store.periods.values()
                if len(p.included_dates) > 1 and p.period_id != self.period.period_id
            ),
            None,
        )
        five = rows[rows["level"] == level_name(FIVE)]
        self.five = five
        peer_groups: dict[tuple[str, ...], list[Row]] = {}
        for r in five.to_dict("records"):
            peer_groups.setdefault(tuple(r[d.value] for d in PEER_KEYS), []).append(r)
        self._peer_totals = {
            k: ({c: sum((as_decimal(r[c]) for r in v), Decimal(0)) for c in SUM_COLUMNS}, len(v))
            for k, v in peer_groups.items()
        }

    def dims(self, segment: Segment) -> tuple[Dimension, ...]:
        return tuple(segment.filters)

    def account(self) -> tuple[Segment, Row]:
        return self.account_segment, self.account_row

    # Sums for pattern checks (no facts are created here).

    def peer_sums(self, segment: Segment, row: Row) -> dict[str, Any] | None:
        if self.dims(segment) != FIVE:
            return None
        total, members = self._peer_totals[tuple(segment.filters[d] for d in PEER_KEYS)]
        return _subtract(total, row) if members > 1 else None

    def residual_sums(self, segment: Segment, row: Row) -> dict[str, Any] | None:
        parent = {d: segment.filters[d] for d in parent_dims(self.dims(segment))}
        parent_row = self.rows[segment_id_for(self.ns, parent)]
        if parent_row["row_count"] == row["row_count"]:
            return None
        return _subtract(parent_row, row)

    # Segments and stats for evidence facts.

    def peer(self, segment: Segment) -> tuple[Segment, dict[str, Any]] | None:
        if self.dims(segment) != FIVE:
            return None
        include = {d: segment.filters[d] for d in PEER_KEYS}
        stats = combined_stats(self.store, self.period, include, exclude=segment.filters)
        if stats is None:
            return None
        return make_segment(self.ns, include, excluding_cohort(segment.segment_id)), stats

    def residual_parent(self, segment: Segment) -> tuple[Segment, dict[str, Any]] | None:
        include = {d: segment.filters[d] for d in parent_dims(self.dims(segment))}
        stats = combined_stats(self.store, self.period, include, exclude=segment.filters)
        if stats is None:
            return None
        return make_segment(self.ns, include, excluding_cohort(segment.segment_id)), stats


# --- Shared evidence -------------------------------------------------------------


@dataclass
class _Lists:
    trigger: list[Fact] = field(default_factory=list)
    daily: list[Fact] = field(default_factory=list)
    peer: list[Fact] = field(default_factory=list)
    counter: list[Fact] = field(default_factory=list)

    def freeze(self) -> Evidence:
        def ids(facts: Iterable[Fact]) -> tuple[str, ...]:
            return tuple(dict.fromkeys(f.fact_id for f in facts))

        return Evidence(ids(self.trigger), ids(self.daily), ids(self.peer), ids(self.counter))


def _common(ctx: _Context, segment: Segment, row: Row, daily_metric: MetricKey) -> _Lists:
    """Daily series, coverage and concentration facts every candidate carries."""
    b, p = ctx.builder, ctx.period
    a_seg, a_row = ctx.account()
    out = _Lists()
    for metric in (R.REQUESTS, R.REVENUE, R.PROFIT):
        out.daily.extend(b.daily_facts(segment, p, metric))
    if daily_metric not in (R.REQUESTS, R.REVENUE, R.PROFIT):
        out.daily.extend(b.daily_facts(segment, p, daily_metric))
    out.daily.extend(b.daily_facts(a_seg, p, daily_metric))

    observed = b.days(segment, p, row, R.OBSERVED_DAYS)
    reference = b.days(a_seg, p, a_row, R.OBSERVED_DAYS)
    positive = b.days(segment, p, row, R.REVENUE_POSITIVE_DAYS)
    concentration = {
        m: b.rate(segment, p, row, m)
        for m in (
            R.MAX_DAY_REVENUE_SHARE, R.TOP_TWO_DAY_REVENUE_SHARE,
            R.MAX_DAY_PROFIT_SHARE, R.TOP_TWO_DAY_PROFIT_SHARE,
        )
    }
    reasons = stability_reasons(row)
    if (
        observed.value < reference.value
        or {"few_observed_days", "few_revenue_positive_days"} & reasons
    ):
        out.counter.extend((observed, reference, positive))
    if "revenue_concentrated" in reasons:
        out.counter.extend((concentration[R.MAX_DAY_REVENUE_SHARE], concentration[R.TOP_TWO_DAY_REVENUE_SHARE]))
    if "profit_concentrated" in reasons:
        out.counter.extend((concentration[R.MAX_DAY_PROFIT_SHARE], concentration[R.TOP_TWO_DAY_PROFIT_SHARE]))
    return out


def _vs_account(ctx: _Context, segment: Segment, row: Row, metric: MetricKey) -> tuple[Fact, Fact, Fact]:
    b, p = ctx.builder, ctx.period
    a_seg, a_row = ctx.account()
    left = b.rate(segment, p, row, metric)
    right = b.rate(a_seg, p, a_row, metric)
    return left, right, b.comparison(left, right, [])


def _vs_other(
    ctx: _Context, left: Fact, other: tuple[Segment, Row] | None, metric: MetricKey, keys: Sequence[Dimension]
) -> tuple[Fact, Fact] | None:
    if other is None:
        return None
    right = ctx.builder.rate(other[0], ctx.period, other[1], metric)
    return right, ctx.builder.comparison(left, right, keys)


def _shares(ctx: _Context, segment: Segment, row: Row) -> dict[MetricKey, Fact]:
    return {
        m: ctx.builder.rate(segment, ctx.period, row, m, account=ctx.account())
        for m in (R.REQUEST_SHARE, R.REVENUE_SHARE, R.PROFIT_SHARE)
    }


def _ratio_at_least(fact: Fact, k: Decimal) -> bool:
    return fact.value is not None and fact.value >= k


def _ratio_at_most(fact: Fact, k: Decimal) -> bool:
    return fact.value is not None and fact.value <= k


# --- Families --------------------------------------------------------------------
# Each family has ``pattern(ctx, segment, row) -> bool`` over sums and
# ``evidence(ctx, segment, row) -> Evidence`` that always creates the same facts.


def _scale_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    cfg = ctx.config
    pm = _rate(row, R.PROFIT.value, R.REQUESTS.value, MILLION)
    share = ratio_value(_num(row, R.REQUESTS.value), _num(ctx.account_row, R.REQUESTS.value))[0]
    if pm is None or _num(row, R.PROFIT.value) <= 0 or share is None or share >= cfg.scale_max_request_share:
        return False
    account_pm = _rate(ctx.account_row, R.PROFIT.value, R.REQUESTS.value, MILLION)
    peer_pm = _rate(ctx.peer_sums(segment, row), R.PROFIT.value, R.REQUESTS.value, MILLION)
    return any(
        base is not None and base > 0 and pm >= cfg.scale_yield_multiple * base
        for base in (account_pm, peer_pm)
    )


def _scale_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    cfg, b, p = ctx.config, ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.PROFIT_PER_MILLION_REQUESTS)
    pm, a_pm, vs_account = _vs_account(ctx, segment, row, R.PROFIT_PER_MILLION_REQUESTS)
    shares = _shares(ctx, segment, row)
    margin, a_margin, margin_cmp = _vs_account(ctx, segment, row, R.MARGIN)
    out.trigger.extend((pm, a_pm, vs_account, shares[R.REQUEST_SHARE], margin, b.rate(segment, p, row, R.REVENUE_PER_MILLION_REQUESTS)))
    peer = _vs_other(ctx, pm, ctx.peer(segment), R.PROFIT_PER_MILLION_REQUESTS, PEER_KEYS)
    if peer:
        out.peer.extend(peer)
        if not _ratio_at_least(peer[1], cfg.scale_yield_multiple):
            out.counter.extend(peer)
    if margin_cmp.value is not None and margin_cmp.value < 1:
        out.counter.extend((margin, a_margin, margin_cmp))
    return out.freeze()


def _waste_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    rm = _rate(row, R.REVENUE.value, R.REQUESTS.value, MILLION)
    return rm is not None and rm < ctx.config.waste_max_revenue_per_million


def _waste_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    b, p = ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.REVENUE_PER_MILLION_REQUESTS)
    rm, a_rm, cmp = _vs_account(ctx, segment, row, R.REVENUE_PER_MILLION_REQUESTS)
    shares = _shares(ctx, segment, row)
    requests = b.additive(segment, p, row, R.REQUESTS)
    profit = b.additive(segment, p, row, R.PROFIT)
    out.trigger.extend((rm, a_rm, cmp, requests, shares[R.REQUEST_SHARE]))
    if profit.value > 0:
        out.counter.extend((profit, shares[R.PROFIT_SHARE], shares[R.REVENUE_SHARE]))
    return out.freeze()


def _margin_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    cfg = ctx.config
    margin = _rate(row, R.PROFIT.value, R.REVENUE.value)
    if margin is None:
        return False
    if margin < cfg.margin_floor or _num(row, R.PROFIT.value) < 0:
        return True
    parent = _rate(ctx.residual_sums(segment, row), R.PROFIT.value, R.REVENUE.value)
    return parent is not None and margin <= parent - cfg.margin_disparity_points


def _margin_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    cfg, b, p = ctx.config, ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.MARGIN)
    margin, a_margin, cmp = _vs_account(ctx, segment, row, R.MARGIN)
    out.trigger.extend((margin, a_margin, cmp, b.additive(segment, p, row, R.PROFIT), b.additive(segment, p, row, R.REVENUE)))
    parent = _vs_other(ctx, margin, ctx.residual_parent(segment), R.MARGIN, parent_dims(ctx.dims(segment)))
    if parent:
        out.peer.extend(parent)
        right = parent[0]
        if right.value is not None and margin.value is not None and margin.value > right.value - cfg.margin_disparity_points:
            out.counter.extend(parent)
    return out.freeze()


def _bid_floor_ratio(row: Row) -> Decimal | None:
    return ratio_value(_num(row, "bf_bid_x_responses"), _num(row, "bf_floor_x_responses"))[0]


def _floor_high_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    ratio = _bid_floor_ratio(row)
    return ratio is not None and ratio >= ctx.config.high_bid_floor_ratio


def _floor_high_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    b, p = ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.REVENUE_PER_MILLION_REQUESTS)
    bid, floor, cmp = b.bid_floor(segment, p, row)
    a_bid, a_floor, a_cmp = b.bid_floor(ctx.account_segment, p, ctx.account_row)
    zero_bid = b.rate(segment, p, row, R.ZERO_BID_RESPONSE_SHARE)
    out.trigger.extend((bid, floor, cmp, b.additive(segment, p, row, R.REVENUE)))
    out.peer.extend((a_bid, a_floor, a_cmp))
    if _ratio_at_least(a_cmp, ctx.config.high_bid_floor_ratio):
        out.counter.append(a_cmp)
    if zero_bid.value:
        out.counter.append(zero_bid)
    return out.freeze()


def _floor_low_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    cfg = ctx.config
    ratio = _bid_floor_ratio(row)
    peer = ctx.peer_sums(segment, row)
    if ratio is None or ratio > cfg.low_bid_floor_ratio or peer is None:
        return False
    for num, den in ((R.RESPONSES.value, R.REQUESTS.value), (R.WINS.value, R.RESPONSES.value)):
        own, other = _rate(row, num, den), _rate(peer, num, den)
        if own is not None and other is not None and other > 0 and own <= cfg.low_bid_peer_rate_ratio * other:
            return True
    return False


def _floor_low_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    b, p = ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.RESPONSE_RATE)
    bid, floor, cmp = b.bid_floor(segment, p, row)
    out.trigger.extend((bid, floor, cmp))
    peer = ctx.peer(segment)
    for metric in (R.RESPONSE_RATE, R.WIN_RATE):
        own = b.rate(segment, p, row, metric)
        out.trigger.append(own)
        pair = _vs_other(ctx, own, peer, metric, PEER_KEYS)
        if pair:
            out.peer.extend(pair)
    wins = b.additive(segment, p, row, R.WINS)
    if wins.value > 0:
        out.counter.extend((wins, b.rate(segment, p, row, R.WIN_RATE)))
    zero_bid = b.rate(segment, p, row, R.ZERO_BID_RESPONSE_SHARE)
    if zero_bid.value:
        out.counter.append(zero_bid)
    return out.freeze()


def _wins_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    cfg = ctx.config
    own = _rate(row, R.IMPRESSIONS.value, R.WINS.value)
    if own is None:
        return False
    if own < cfg.impressions_per_win_floor:
        return True
    peer = _rate(ctx.peer_sums(segment, row), R.IMPRESSIONS.value, R.WINS.value)
    return peer is not None and peer > 0 and own < cfg.impressions_per_win_peer_ratio * peer


def _wins_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    b, p = ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.IMPRESSION_TO_WIN_RATE)
    own, a_own, cmp = _vs_account(ctx, segment, row, R.IMPRESSION_TO_WIN_RATE)
    out.trigger.extend((own, a_own, cmp, b.additive(segment, p, row, R.WINS), b.additive(segment, p, row, R.IMPRESSIONS)))
    peer = _vs_other(ctx, own, ctx.peer(segment), R.IMPRESSION_TO_WIN_RATE, PEER_KEYS)
    if peer:
        out.peer.extend(peer)
    revenue = b.additive(segment, p, row, R.REVENUE)
    if revenue.value > 0:
        out.counter.extend((revenue, b.rate(segment, p, row, R.ECPM)))
    if _ratio_at_most(a_own, ctx.config.impressions_per_win_floor):
        out.counter.append(a_own)
    return out.freeze()


@dataclass(frozen=True)
class _Step:
    earlier: Any
    later: Any
    kind: str  # "yield" or "volume"


def _trend_steps(ctx: _Context, segment: Segment) -> list[_Step]:
    """Qualifying adjacent-day yield steps and request/revenue spikes, from daily sums."""
    if segment.segment_id not in ctx.trend_cache:
        ctx.trend_cache[segment.segment_id] = _find_trend_steps(ctx, segment)
    return ctx.trend_cache[segment.segment_id]


def _find_trend_steps(ctx: _Context, segment: Segment) -> list[_Step]:
    cfg = ctx.config
    days = daily_rows(ctx.store, segment.segment_id)
    by_day = {r["day"]: r for r in days.to_dict("records")}
    dates = ctx.period.included_dates
    steps: list[_Step] = []
    for earlier_day, later_day in zip(dates, dates[1:]):
        e, l = by_day.get(earlier_day), by_day.get(later_day)
        if e is None or l is None:
            continue
        if min(_num(e, R.REQUESTS.value), _num(l, R.REQUESTS.value)) < cfg.trend_min_day_requests:
            continue
        if max(_num(e, R.REVENUE.value), _num(l, R.REVENUE.value)) < cfg.trend_min_day_revenue:
            continue
        before = _rate(e, R.REVENUE.value, R.REQUESTS.value, MILLION)
        after = _rate(l, R.REVENUE.value, R.REQUESTS.value, MILLION)
        if before is None or after is None:
            continue
        if before <= 0:
            qualifies = after > 0
        else:
            qualifies = after >= cfg.trend_step_up * before or after <= cfg.trend_step_down * before
        if qualifies:
            steps.append(_Step(earlier_day, later_day, "yield"))
    if len(by_day) >= cfg.trend_min_usable_days:
        for metric in (R.REQUESTS.value, R.REVENUE.value):
            values = {d: max(_num(r, metric), Decimal(0)) for d, r in by_day.items()}
            for day, value in values.items():
                others = [v for d, v in values.items() if d != day]
                mid = Decimal(median(others))
                if value > 0 and (value >= cfg.trend_volume_multiple * mid if mid > 0 else True):
                    steps.append(_Step(None, day, "volume"))
    return list(dict.fromkeys(steps))


def _trend_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    return bool(_trend_steps(ctx, segment))


def _trend_flagged_only(ctx: _Context, segment: Segment) -> bool:
    steps = _trend_steps(ctx, segment)
    return bool(steps) and all(
        {s.earlier, s.later} & ctx.flagged_days for s in steps
    )


def _trend_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    b, p = ctx.builder, ctx.period
    out = _common(ctx, segment, row, R.REVENUE_PER_MILLION_REQUESTS)
    a_seg, _ = ctx.account()
    own = {f.period.included_dates[0]: f for f in b.daily_facts(segment, p, R.REVENUE_PER_MILLION_REQUESTS)}
    acc = {f.period.included_dates[0]: f for f in b.daily_facts(a_seg, p, R.REVENUE_PER_MILLION_REQUESTS)}
    requests = {f.period.included_dates[0]: f for f in b.daily_facts(segment, p, R.REQUESTS)}
    revenue = {f.period.included_dates[0]: f for f in b.daily_facts(segment, p, R.REVENUE)}
    changes: dict[tuple[Any, Any], tuple[Fact, Fact | None]] = {}
    dates = p.included_dates
    for earlier, later in zip(dates, dates[1:]):
        if earlier in own and later in own:
            account_change = b.daily_change(acc[earlier], acc[later]) if earlier in acc and later in acc else None
            changes[(earlier, later)] = (b.daily_change(own[earlier], own[later]), account_change)
    sensitivity = None
    if ctx.sensitivity_period is not None:
        sens_row = stats_row(ctx.store, segment.segment_id, ctx.sensitivity_period.period_id)
        if sens_row is not None:
            sensitivity = b.rate(segment, ctx.sensitivity_period, sens_row, R.REVENUE_PER_MILLION_REQUESTS)

    steps = _trend_steps(ctx, segment)
    for step in steps:
        if step.kind == "yield":
            change, account_change = changes[(step.earlier, step.later)]
            out.trigger.extend((own[step.earlier], own[step.later], change))
            if account_change is not None and account_change.value is not None and change.value is not None and (
                account_change.value * change.value > 0
            ):
                out.counter.append(account_change)
        else:
            out.trigger.extend((requests[step.later], revenue[step.later]))
        if {step.earlier, step.later} & ctx.flagged_days and sensitivity is not None:
            out.counter.append(sensitivity)
    return out.freeze()


def _combination_pattern(ctx: _Context, segment: Segment, row: Row) -> bool:
    cfg = ctx.config
    own = _rate(row, R.PROFIT.value, R.REQUESTS.value, MILLION)
    if own is None:
        return False
    for other in (ctx.residual_sums(segment, row), ctx.peer_sums(segment, row)):
        base = _rate(other, R.PROFIT.value, R.REQUESTS.value, MILLION)
        if base is not None and base > 0 and (
            own >= cfg.combination_multiple_up * base or own <= cfg.combination_multiple_down * base
        ):
            return True
    return False


def _combination_evidence(ctx: _Context, segment: Segment, row: Row) -> Evidence:
    cfg = ctx.config
    out = _common(ctx, segment, row, R.PROFIT_PER_MILLION_REQUESTS)
    own = ctx.builder.rate(segment, ctx.period, row, R.PROFIT_PER_MILLION_REQUESTS)
    out.trigger.append(own)
    out.trigger.append(_shares(ctx, segment, row)[R.REQUEST_SHARE])
    for other, keys in (
        (ctx.residual_parent(segment), parent_dims(FIVE)),
        (ctx.peer(segment), PEER_KEYS),
    ):
        pair = _vs_other(ctx, own, other, R.PROFIT_PER_MILLION_REQUESTS, keys)
        if pair is None:
            continue
        ratio = pair[1]
        if _ratio_at_least(ratio, cfg.combination_multiple_up) or (
            ratio.value is not None and ratio.value <= cfg.combination_multiple_down
        ):
            out.trigger.extend(pair)
        else:
            out.peer.extend(pair)
    return out.freeze()


_FAMILIES: dict[
    DetectorFamily,
    tuple[Callable[[_Context, Segment, Row], bool], Callable[[_Context, Segment, Row], Evidence]],
] = {
    DetectorFamily.SCALE_WINNER: (_scale_pattern, _scale_evidence),
    DetectorFamily.WASTED_REQUESTS: (_waste_pattern, _waste_evidence),
    DetectorFamily.MARGIN_LEAK: (_margin_pattern, _margin_evidence),
    DetectorFamily.FLOOR_HIGH_BID: (_floor_high_pattern, _floor_high_evidence),
    DetectorFamily.FLOOR_LOW_BID: (_floor_low_pattern, _floor_low_evidence),
    DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: (_wins_pattern, _wins_evidence),
    DetectorFamily.TREND: (_trend_pattern, _trend_evidence),
    DetectorFamily.COMBINATION_OUTLIER: (_combination_pattern, _combination_evidence),
}


# --- Eligibility -----------------------------------------------------------------


def _blocked(ctx: _Context, family: DetectorFamily, segment: Segment) -> bool:
    restrictions = ctx.builder.issues.restrictions_for(segment, ctx.period)
    if restrictions & FAMILY_BLOCKERS[family]:
        return True
    return any(r in restrictions and d in segment.filters for r, d in DIMENSION_BLOCKERS.items())


def _stable_without_flagged_days(ctx: _Context, segment: Segment, row: Row, role: str) -> bool:
    """Stable on the full period and, when days are flagged, on the period without them."""
    if not row["stable"]:
        return False
    if ctx.sensitivity_period is None:
        return True
    other = stats_row(ctx.store, segment.segment_id, ctx.sensitivity_period.period_id)
    return other is not None and bool(other["stable"]) and bool(other[gate_column(GATE_VOLUME, role)])


def _passes(ctx: _Context, family: DetectorFamily, segment: Segment, row: Row, role: str) -> bool:
    """Gate (and stability for scale) at ``role``, with no restriction or taint."""
    if not row[gate_column(FAMILY_GATE[family], role)]:
        return False
    if family is DetectorFamily.SCALE_WINNER and not _stable_without_flagged_days(ctx, segment, row, role):
        return False
    if family in FUNNEL_FAMILIES and row[FUNNEL_INVALID_ROWS] > 0:
        return False
    if family is DetectorFamily.TREND and _trend_flagged_only(ctx, segment):
        return False
    return not _blocked(ctx, family, segment)


def _candidate_id(ctx: _Context, family: DetectorFamily, segment: Segment) -> str:
    return make_id(
        IdPrefix.CANDIDATE,
        ctx.ns,
        {"family": family.value, "segment_id": segment.segment_id, "period_id": ctx.period.period_id},
    )


def _scan(ctx: _Context) -> tuple[list[tuple[DetectorFamily, Segment, Row]], dict[str, set[tuple[DetectorFamily, str]]]]:
    """Pattern hits above the exploratory gate, and eligible sets per gate role."""
    roles = [BASE, *sensitivity_roles(ctx.store)]
    eligible: dict[str, set[tuple[DetectorFamily, str]]] = {r: set() for r in roles}
    hits = []
    for segment_id, row in ctx.rows.items():
        segment = ctx.store.segments[segment_id]
        if not segment.filters:
            continue
        for family, (pattern, _) in _FAMILIES.items():
            if family in FIVE_ONLY_FAMILIES and ctx.dims(segment) != FIVE:
                continue
            if not pattern(ctx, segment, row):
                continue
            for role in roles:
                if _passes(ctx, family, segment, row, role):
                    eligible[role].add((family, segment_id))
            if row[gate_column(FAMILY_GATE[family], EXPLORATORY)] or row[gate_column(FAMILY_GATE[family], BASE)]:
                hits.append((family, segment, row))
    return hits, eligible


def _dedupe(drafts: list[_Draft], share: Decimal) -> None:
    """Reject a same-family parent when one narrower candidate carries ``share`` of its requests."""
    by_family: dict[DetectorFamily, list[_Draft]] = {}
    for draft in drafts:
        by_family.setdefault(draft.family, []).append(draft)
    for group in by_family.values():
        for parent in group:
            parent_requests = as_decimal(parent.row["requests"])
            children = [
                c
                for c in group
                if len(c.segment.filters) > len(parent.segment.filters)
                and parent.segment.filters.items() <= c.segment.filters.items()
            ]
            covering = [
                c for c in children
                if parent_requests > 0 and as_decimal(c.row["requests"]) >= share * parent_requests
            ]
            if covering:
                child = max(covering, key=lambda c: (as_decimal(c.row["requests"]), c.candidate_id))
                parent.eligibility = EligibilityStatus.REJECTED
                parent.rejection_reason = (
                    f"duplicate of narrower candidate {child.candidate_id}, which carries at least "
                    f"{format(share, 'f')} of this segment's requests"
                )


def _overlaps(ctx: _Context, drafts: Sequence[_Draft]) -> dict[str, OverlapGroup]:
    """Pairwise exact shared-request exposure between candidates whose traffic intersects."""
    five = ctx.five
    requests = np.array([int(as_decimal(v)) for v in five["requests"]], dtype=object)
    masks = {}
    for draft in drafts:
        mask = np.ones(len(five), dtype=bool)
        for dim, label in draft.segment.filters.items():
            mask &= (five[dim.value] == label).to_numpy()
        masks[draft.candidate_id] = mask
    # Two slices overlap unless they fix a shared dimension differently: index
    # drafts by the labels they fix, so each draft meets only the drafts that
    # agree with it (a full-size export has thousands of candidates).
    by_dims: dict[tuple[Dimension, ...], list[int]] = {}
    for i, draft in enumerate(drafts):
        by_dims.setdefault(ctx.dims(draft.segment), []).append(i)
    projections: dict[tuple[tuple[Dimension, ...], tuple[Dimension, ...]], dict] = {}

    def agreeing(a: _Draft) -> list[int]:
        out = []
        for dims, members in by_dims.items():
            shared = tuple(d for d in dims if d in a.segment.filters)
            index = projections.get((dims, shared))
            if index is None:
                index = {}
                for j in members:
                    key = tuple(drafts[j].segment.filters[d] for d in shared)
                    index.setdefault(key, []).append(j)
                projections[(dims, shared)] = index
            out.extend(index.get(tuple(a.segment.filters[d] for d in shared), ()))
        return sorted(out)

    groups: dict[str, OverlapGroup] = {}
    for i, a in enumerate(drafts):
        for b in (drafts[j] for j in agreeing(a) if j > i):
            if not segments_overlap(a.segment.filters, b.segment.filters):
                continue
            shared = masks[a.candidate_id] & masks[b.candidate_id]
            if not shared.any() or sum(requests[shared]) <= 0:
                continue
            union = {**a.segment.filters, **b.segment.filters}
            stats = combined_stats(ctx.store, ctx.period, union)
            if stats is None:
                continue
            fact = ctx.builder.additive(make_segment(ctx.ns, union), ctx.period, stats, R.REQUESTS)
            ids = sorted((a.candidate_id, b.candidate_id))
            group = OverlapGroup(
                overlap_group_id=make_id(IdPrefix.OVERLAP_GROUP, ctx.ns, ids),
                candidate_ids=ids,
                shared_request_fact_id=fact.fact_id,
            )
            groups[group.overlap_group_id] = group
            a.overlap_group_ids.append(group.overlap_group_id)
            b.overlap_group_ids.append(group.overlap_group_id)
    return groups


_FAMILY_ORDER = {f: i for i, f in enumerate(DetectorFamily)}
_ELIGIBILITY_ORDER = {
    EligibilityStatus.ELIGIBLE: 0, EligibilityStatus.EXPLORATORY: 1, EligibilityStatus.REJECTED: 2,
}


def run_detection(metrics: MetricsStore, quality: QualityReport, config: DetectorConfig) -> Detection:
    """Every detector family, deduplication, overlap graph, evidence facts and sensitivity."""
    ctx = _Context(metrics, quality, config)
    hits, eligible = _scan(ctx)
    drafts = []
    for family, segment, row in hits:
        status = (
            EligibilityStatus.ELIGIBLE if (family, segment.segment_id) in eligible[BASE]
            else EligibilityStatus.EXPLORATORY
        )
        drafts.append(
            _Draft(
                family=family, segment=segment, row=row, eligibility=status,
                evidence=_FAMILIES[family][1](ctx, segment, row),
                candidate_id=_candidate_id(ctx, family, segment),
            )
        )
    _dedupe(drafts, config.dedup_child_request_share)
    drafts.sort(
        key=lambda d: (
            _ELIGIBILITY_ORDER[d.eligibility], _FAMILY_ORDER[d.family],
            -as_decimal(d.row["requests"]), d.segment.segment_id,
        )
    )
    groups = _overlaps(ctx, [d for d in drafts if d.eligibility is not EligibilityStatus.REJECTED])
    candidates = tuple(_to_candidate(ctx, d) for d in drafts)

    base = frozenset(eligible[BASE])
    by_role = {role: frozenset(pairs) for role, pairs in eligible.items()}
    sensitive = frozenset(
        d.candidate_id
        for d in drafts
        if any(((d.family, d.segment.segment_id) in pairs) != ((d.family, d.segment.segment_id) in base)
               for pairs in by_role.values())
    )
    sensitivity = SensitivityResult(
        eligible_by_role=by_role,
        shortlist_changed={role: pairs != base for role, pairs in by_role.items() if role != BASE},
        sensitive_candidate_ids=sensitive,
    )
    return Detection(
        candidates=candidates,
        facts=FactRegistry(ctx.ns, ctx.builder.facts.values()),
        overlap_groups=groups,
        sensitivity=sensitivity,
    )


def _to_candidate(ctx: _Context, draft: _Draft) -> Candidate:
    evidence = draft.evidence
    return Candidate(
        candidate_id=draft.candidate_id,
        family=draft.family,
        segment_id=draft.segment.segment_id,
        period_id=ctx.period.period_id,
        eligibility=draft.eligibility,
        trigger_fact_ids=list(evidence.trigger),
        daily_fact_ids=list(evidence.daily),
        peer_fact_ids=list(evidence.peer),
        counter_evidence_fact_ids=list(evidence.counter),
        quality_issue_ids=ctx.builder.issues.issues_for(draft.segment, ctx.period),
        overlap_group_ids=sorted(draft.overlap_group_ids),
        rejection_reason=draft.rejection_reason,
    )


def detect_opportunities(
    metrics: MetricsStore, quality: QualityReport, config: DetectorConfig
) -> list[Candidate]:
    """Run every detector family over observed groups and attach supporting and contrary evidence."""
    return list(run_detection(metrics, quality, config).candidates)


def candidate_evidence(
    metrics: MetricsStore, quality: QualityReport, candidates: Sequence[Candidate]
) -> CandidateEvidence:
    """Rebuild the facts and overlap groups ``candidates`` reference, without detector thresholds.

    For consumers (evidence builder, refined quality) that receive candidates
    but not the detector configuration. Facts are closed under parents.
    """
    ctx = _Context(metrics, quality, DetectorConfig())
    drafts = []
    for c in candidates:
        segment = metrics.segments[c.segment_id]
        row = ctx.rows[c.segment_id]
        _FAMILIES[c.family][1](ctx, segment, row)
        drafts.append(
            _Draft(
                family=c.family, segment=segment, row=row, eligibility=c.eligibility,
                evidence=Evidence((), (), (), ()), candidate_id=c.candidate_id,
            )
        )
    referenced = {g for c in candidates for g in c.overlap_group_ids}
    groups = {k: v for k, v in _overlaps(ctx, drafts).items() if k in referenced}
    return CandidateEvidence(facts=FactRegistry(ctx.ns, ctx.builder.facts.values()), overlap_groups=groups)


def threshold_sensitive(metrics: MetricsStore, candidate: Candidate) -> bool:
    """Whether the candidate's eligibility gate flips at any 0.5x/2x-style sensitivity role."""
    period: Period = metrics.periods[candidate.period_id]
    row = stats_row(metrics, candidate.segment_id, period.period_id)
    if row is None:
        return False
    gate = FAMILY_GATE[candidate.family]
    base = bool(row[gate_column(gate, BASE)])
    return any(bool(row[gate_column(gate, role)]) != base for role in sensitivity_roles(metrics))


def _peer_keys_differ(candidate: Candidate, facts: FactRegistry, metrics: MetricsStore) -> bool:
    """A peer or residual-parent contrast leaves a fixed dimension (other than supply) unmatched."""
    dims = set(metrics.segments[candidate.segment_id].filters) - {Dimension.SUPPLY_ID}
    for fact_id in (*candidate.trigger_fact_ids, *candidate.peer_fact_ids):
        fact = facts.get(fact_id)
        if fact is None or fact.comparison is None:
            continue
        right = facts.get(fact.comparison.right_fact_id)
        if right is None or not right.segment.cohort_id.startswith(EXCLUDING_PREFIX):
            continue
        if dims - set(fact.comparison.matching_keys):
            return True
    return False


def with_detection_flags(metrics: MetricsStore, detection: Detection) -> MetricsStore:
    """A copy of ``metrics`` whose ``quality_flags`` carry detector-side signals for ``refine_quality``.

    ``RANK_SENSITIVE_FLAG`` marks candidates whose eligibility changes at a
    sensitivity multiple; ``PEER_KEYS_DIFFER_FLAG`` marks candidates whose
    contrast does not hold every fixed dimension, so no matched-peer label.
    """
    flags: dict[str, list[str]] = {k: list(v) for k, v in metrics.quality_flags.items()}

    def add(key: str, flag: str) -> None:
        if flag not in flags.setdefault(key, []):
            flags[key].append(flag)

    for candidate in detection.candidates:
        if candidate.candidate_id in detection.sensitivity.sensitive_candidate_ids:
            add(candidate.candidate_id, RANK_SENSITIVE_FLAG)
        if _peer_keys_differ(candidate, detection.facts, metrics):
            add(candidate.candidate_id, PEER_KEYS_DIFFER_FLAG)
    return replace(metrics, quality_flags=MappingProxyType({k: tuple(v) for k, v in flags.items()}))
