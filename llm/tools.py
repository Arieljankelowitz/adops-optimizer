"""Aggregate-only drill-downs. No SQL, rows, network or executable model input."""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType

from pydantic import TypeAdapter

from contracts.enums import (
    CoverageStatus,
    Dimension,
    FactKind,
    MetricKey,
    ToolName,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, canonical_json, make_id, stable_digest
from contracts.models import (
    BreakdownArgs,
    CompareToPeersArgs,
    DailyTrendArgs,
    Fact,
    Provenance,
    ToolCall,
    ToolEvidence,
    Validity,
)
from contracts.runtime import AnalysisContext, EvidenceBundle, ToolRouter
from pipeline.metrics import (
    FIVE,
    FULL_INPUT,
    SUM_COLUMNS,
    FactBuilder,
    combined_stats,
    full_period,
    level_name,
    make_period,
    make_segment,
    row_set_hash,
)

TOOL_VERSION = "drilldown_v1"
# A bounded, reviewed metric set; ratios are computed by the analytics engine.
TOTALS = (MetricKey.REQUESTS, MetricKey.REVENUE, MetricKey.PROFIT)
RATES = (MetricKey.REVENUE_PER_MILLION_REQUESTS, MetricKey.PROFIT_PER_MILLION_REQUESTS)


@dataclass(frozen=True)
class PeerDefinition:
    peer_definition_id: str
    fixed_dimensions: tuple[Dimension, ...]
    varying_dimension: Dimension
    # Target is always excluded. These are additional explicit exclusions.
    excluded_segment_ids: tuple[str, ...] = ()


def peer_definition(
    namespace: str, varying: Dimension = Dimension.SUPPLY_ID
) -> PeerDefinition:
    """Same country/size/device/placement, different supply (by default)."""
    varying = Dimension(varying)
    fixed = tuple(d for d in Dimension if d is not varying)
    pid = make_id(
        IdPrefix.PEER_DEFINITION,
        namespace,
        {"fixed": fixed, "varying": varying, "exclude_target": True},
    )
    return PeerDefinition(pid, fixed, varying)


def _scope(ctx, segment_id, period_id):
    if (
        ctx.run_namespace != ctx.metrics.run_namespace
        or ctx.run_namespace != ctx.bundle.run_namespace
    ):
        raise ValueError("tool_namespace_mismatch")
    segment = ctx.metrics.segments.get(segment_id) or ctx.bundle.segments.get(
        segment_id
    )
    period = ctx.metrics.periods.get(period_id) or ctx.bundle.periods.get(period_id)
    if segment is None or period is None:
        raise ValueError("unknown_tool_scope")
    # Arbitrary sensitivity-cohort recombination is deliberately not supported.
    if segment.cohort_id != FULL_INPUT:
        raise ValueError("unsupported_tool_cohort")
    return segment, period


def _rows(ctx, period):
    table = (
        ctx.metrics.daily if len(period.included_dates) == 1 else ctx.metrics.aggregates
    )
    return table[
        (table["level"] == level_name(FIVE))
        & (table["cohort_id"] == FULL_INPUT)
        & (table["period_id"] == period.period_id)
    ]


def _sums(rows):
    if rows.empty:
        return None
    values = {c: sum(rows[c].tolist(), Decimal(0)) for c in SUM_COLUMNS}
    values["row_set_hash"] = row_set_hash(n for lines in rows["lines"] for n in lines)
    return values


def _facts(builder, segment, period, stats):
    if stats is None:
        return []
    return [builder.additive(segment, period, stats, m) for m in TOTALS] + [
        builder.rate(segment, period, stats, m) for m in RATES
    ]


def _result(ctx, call, builder, coverage, returned, other=(), truncated=False):
    # Tools never overwrite facts; identical IDs must mean identical records.
    appended = []
    for fact in builder.facts.values():
        old = ctx.bundle.facts.get(fact.fact_id)
        if old is not None and old != fact:
            raise ValueError("immutable_fact_collision")
        if old is None:
            appended.append(fact)
    return ToolEvidence(
        tool_invocation_id=make_id(
            IdPrefix.TOOL_INVOCATION,
            ctx.run_namespace,
            {"version": TOOL_VERSION, "call": call.model_dump(mode="json")},
        ),
        run_namespace=ctx.run_namespace,
        tool_call=call,
        tool_version=TOOL_VERSION,
        coverage_fact_ids=[f.fact_id for f in coverage],
        returned_fact_ids=list(dict.fromkeys(f.fact_id for f in returned)),
        appended_facts=appended,
        other_bucket_fact_ids=[f.fact_id for f in other],
        truncated=truncated,
        quality_issue_ids=sorted(
            {q for f in builder.facts.values() for q in f.provenance.quality_issue_ids}
        ),
    )


def breakdown(
    ctx: AnalysisContext,
    segment_id: str,
    by: Dimension,
    period_id: str,
    limit: int = 10,
) -> ToolEvidence:
    """Traffic-ordered children and an exact residual, preserving every fixed key."""
    call = BreakdownArgs(segment_id=segment_id, by=by, period_id=period_id, limit=limit)
    segment, period = _scope(ctx, segment_id, period_id)
    if call.by in segment.filters:
        raise ValueError("breakdown_dimension_already_fixed")
    rows = _rows(ctx, period)
    for dim, label in segment.filters.items():
        rows = rows[rows[dim.value] == label]
    b = FactBuilder(ctx.metrics, ctx.bundle.quality)
    coverage = _facts(b, segment, period, _sums(rows))
    groups = [
        (str(label), group) for label, group in rows.groupby(call.by.value, sort=True)
    ]
    groups.sort(key=lambda pair: (-sum(pair[1]["requests"], Decimal(0)), pair[0]))
    returned = []
    for label, group in groups[:limit]:
        child = make_segment(ctx.run_namespace, {**segment.filters, call.by: label})
        returned.extend(_facts(b, child, period, _sums(group)))
    other = []
    if len(groups) > limit:
        labels = {label for label, _ in groups[limit:]}
        residual = rows[rows[call.by.value].isin(labels)]
        other_segment = make_segment(
            ctx.run_namespace,
            segment.filters,
            "other_" + stable_digest(call.model_dump(mode="json")),
        )
        other = _facts(b, other_segment, period, _sums(residual))
    return _result(ctx, call, b, coverage, returned + other, other, bool(other))


def daily_trend(ctx: AnalysisContext, segment_id: str, period_id: str) -> ToolEvidence:
    """Calendar series: absent requests are undefined, never fabricated zero."""
    call = DailyTrendArgs(segment_id=segment_id, period_id=period_id)
    segment, period = _scope(ctx, segment_id, period_id)
    if (period.end - period.start).days > 366:
        raise ValueError("daily_tool_period_too_long")
    b = FactBuilder(ctx.metrics, ctx.bundle.quality)
    coverage = _facts(
        b, segment, period, combined_stats(ctx.metrics, period, segment.filters)
    )
    returned = []
    day = period.start
    account_dates = set(full_period(ctx.metrics).included_dates)
    included = set(period.included_dates)
    while day <= period.end:
        if day in account_dates and day not in included:
            # Explicitly excluded sensitivity dates are not absent observations.
            day += timedelta(days=1)
            continue
        dp = make_period(ctx.run_namespace, [day], CoverageStatus.UNVERIFIED)
        if dp.period_id in ctx.metrics.periods:
            dp = ctx.metrics.periods[dp.period_id]
            stats = combined_stats(ctx.metrics, dp, segment.filters)
        else:
            stats = None
        if stats is not None:
            returned.extend(_facts(b, segment, dp, stats))
        else:
            empty_hash = row_set_hash([])
            fact = Fact(
                fact_id=make_id(
                    IdPrefix.FACT,
                    ctx.run_namespace,
                    {"tool_absent": segment_id, "period": dp.period_id},
                ),
                run_namespace=ctx.run_namespace,
                kind=FactKind.CALCULATED,
                segment=segment,
                period=dp,
                metric=MetricKey.REQUESTS,
                value=None,
                unit=Unit.COUNT,
                validity=Validity(
                    status=ValidityStatus.UNDEFINED, reason="absent_daily_observation"
                ),
                provenance=Provenance(
                    input_sha256=ctx.metrics.input_sha256,
                    formula_id="absent_daily_observation_v1",
                    source_columns=[],
                    source_row_set_hash=empty_hash,
                    source_line_ranges_ref="private_rows_"
                    + stable_digest(["rows", empty_hash]),
                    parent_fact_ids=[],
                    aggregation_keys=list(segment.filters),
                    weighting=Weighting.NOT_APPLICABLE,
                    quality_issue_ids=b.issues.issues_for(segment, dp),
                ),
            )
            b.facts[fact.fact_id] = fact
            returned.append(fact)
        day += timedelta(days=1)
    return _result(ctx, call, b, coverage, returned)


def compare_to_peers(
    ctx: AnalysisContext, segment_id: str, period_id: str, peer_definition_id: str
) -> ToolEvidence:
    """Exact residual sibling baseline; no cross-format replacement claim."""
    call = CompareToPeersArgs(
        segment_id=segment_id,
        period_id=period_id,
        peer_definition_id=peer_definition_id,
    )
    segment, period = _scope(ctx, segment_id, period_id)
    definitions = {
        peer_definition(ctx.run_namespace, d).peer_definition_id: peer_definition(
            ctx.run_namespace, d
        )
        for d in Dimension
    }
    definition = definitions.get(peer_definition_id)
    if definition is None or not all(d in segment.filters for d in Dimension):
        raise ValueError("unknown_or_unmatched_peer_definition")
    fixed = {d: segment.filters[d] for d in definition.fixed_dimensions}
    peer = make_segment(ctx.run_namespace, fixed, "excluding_" + segment.segment_id)
    b = FactBuilder(ctx.metrics, ctx.bundle.quality)
    own = _facts(
        b, segment, period, combined_stats(ctx.metrics, period, segment.filters)
    )
    peers = _facts(
        b, peer, period, combined_stats(ctx.metrics, period, fixed, segment.filters)
    )
    comparisons = [
        b.comparison(a, p, definition.fixed_dimensions) for a, p in zip(own, peers)
    ]
    return _result(ctx, call, b, own + peers, own + peers + comparisons)


def extend_bundle(bundle: EvidenceBundle, evidence: ToolEvidence) -> EvidenceBundle:
    """Promote tool facts before validation/rendering; preserve immutable lineage."""
    if evidence.run_namespace != bundle.run_namespace:
        raise ValueError("tool_namespace_mismatch")
    new = []
    segments, periods = dict(bundle.segments), dict(bundle.periods)
    for fact in evidence.appended_facts:
        old = bundle.facts.get(fact.fact_id)
        if old is not None and old != fact:
            raise ValueError("immutable_fact_collision")
        if old is None:
            new.append(fact)
        if (
            fact.segment.segment_id in segments
            and segments[fact.segment.segment_id] != fact.segment
        ):
            raise ValueError("immutable_segment_collision")
        if (
            fact.period.period_id in periods
            and periods[fact.period.period_id] != fact.period
        ):
            raise ValueError("immutable_period_collision")
        segments[fact.segment.segment_id] = fact.segment
        periods[fact.period.period_id] = fact.period
    facts = bundle.facts.extended(new)
    for ref in (
        *evidence.returned_fact_ids,
        *evidence.coverage_fact_ids,
        *evidence.other_bucket_fact_ids,
    ):
        if ref not in facts:
            raise ValueError("unresolved_tool_fact")
    for fact in new:
        if any(p not in facts for p in fact.provenance.parent_fact_ids):
            raise ValueError("unresolved_tool_parent")
    return replace(
        bundle,
        facts=facts,
        segments=MappingProxyType(segments),
        periods=MappingProxyType(periods),
    )


class AnalyticToolRouter(ToolRouter):
    """Namespace-local cache and candidate-scoped dispatch (also usable by UI)."""

    def __init__(self, ctx: AnalysisContext):
        self.ctx = ctx
        self._cache: dict[str, ToolEvidence] = {}
        self._scopes: dict[str, tuple[set[str], set[str]]] = {}

    @property
    def allowed_tools(self) -> frozenset[ToolName]:
        return frozenset(ToolName)

    def dispatch(self, candidate_id: str, call: ToolCall) -> ToolEvidence:
        call = TypeAdapter(ToolCall).validate_python(call)
        candidate = self.ctx.bundle.candidates.get(candidate_id)
        if candidate is None:
            raise ValueError("unknown_tool_candidate")
        # Candidate tools may drill within its evidence envelope, not another run.
        pack = self.ctx.bundle.packs.get(candidate_id)
        if candidate_id not in self._scopes:
            self._scopes[candidate_id] = (
                {s.segment_id for s in pack.segments}
                if pack
                else {candidate.segment_id},
                {p.period_id for p in pack.periods} if pack else {candidate.period_id},
            )
        segments, periods = self._scopes[candidate_id]
        if call.segment_id not in segments or call.period_id not in periods:
            raise ValueError("tool_outside_candidate_scope")
        key = canonical_json(call.model_dump(mode="json"))
        if key not in self._cache:
            if isinstance(call, BreakdownArgs):
                ev = breakdown(
                    self.ctx, call.segment_id, call.by, call.period_id, call.limit
                )
            elif isinstance(call, DailyTrendArgs):
                ev = daily_trend(self.ctx, call.segment_id, call.period_id)
            else:
                ev = compare_to_peers(
                    self.ctx, call.segment_id, call.period_id, call.peer_definition_id
                )
            self.ctx = replace(self.ctx, bundle=extend_bundle(self.ctx.bundle, ev))
            self._cache[key] = ev
        evidence = self._cache[key]
        for ref in evidence.returned_fact_ids:
            fact = self.ctx.bundle.facts[ref]
            segments.add(fact.segment.segment_id)
            periods.add(fact.period.period_id)
        return evidence
