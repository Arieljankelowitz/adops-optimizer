"""Real analytic tool mini-eval, using independently invented rows only."""

from dataclasses import replace
from decimal import Decimal

import pytest

from contracts.config import AnalyticsConfig
from contracts.enums import Dimension as D
from contracts.enums import MetricKey as M
from contracts.enums import ValidityStatus
from contracts.ids import IdPrefix, make_id
from contracts.models import BreakdownArgs, Candidate, CandidatePack, DailyTrendArgs
from contracts.runtime import (
    AnalysisContext,
    AssumptionRegistry,
    EvidenceBundle,
    FactRegistry,
    ScenarioRegistry,
)
from llm.tools import (
    AnalyticToolRouter,
    breakdown,
    compare_to_peers,
    daily_trend,
    extend_bundle,
    peer_definition,
)
from pipeline.metrics import FIVE, aggregate, full_period, make_period, segment_id_for
from tests.support.analytics_data import (
    DAYS,
    loaded_from_rows,
    quality_for,
    row,
    slice_rows,
)


def make_tool_context():
    rows = []
    for key, requests, revenue in [
        (("alpha", "DE", "300x250", "Banner", "Desktop"), 100, "10"),
        (("beta", "DE", "300x250", "Banner", "Desktop"), 200, "20"),
        (("gamma", "DE", "300x250", "Banner", "Desktop"), 300, "30"),
        (("video", "DE", "300x250", "Video", "Desktop"), 900, "90"),
    ]:
        rows += slice_rows(key, requests, revenue)
    # alpha is absent on one interior date; present with an observed zero on another.
    rows = [
        r for r in rows if not (r["Supply ID"] == "alpha" and r["Day"] in DAYS[2:4])
    ]
    rows.append(
        row(
            DAYS[3],
            ("alpha", "DE", "300x250", "Banner", "Desktop"),
            0,
            0,
            0,
            0,
            "0",
            "0",
        )
    )
    data = loaded_from_rows(rows, "reasoning tools independent fixture")
    quality = quality_for(data)
    metrics = aggregate(data, quality, AnalyticsConfig())
    ns = metrics.run_namespace
    bundle = EvidenceBundle(
        ns,
        FactRegistry(ns),
        AssumptionRegistry(ns),
        ScenarioRegistry(ns),
        metrics.segments,
        metrics.periods,
        {},
        {},
        quality,
        {},
    )
    return AnalysisContext(ns, metrics, bundle)


@pytest.fixture
def ctx():
    return make_tool_context()


def target(ctx):
    return segment_id_for(
        ctx.run_namespace,
        dict(zip(FIVE, ("alpha", "DE", "300x250", "Banner", "Desktop"))),
    )


def fact_map(evidence):
    return {f.fact_id: f for f in evidence.appended_facts}


def test_breakdown_top_k_other_reconciles_full_coverage(ctx):
    account = segment_id_for(ctx.run_namespace, {})
    ev = breakdown(
        ctx, account, D.SUPPLY_ID, full_period(ctx.metrics).period_id, limit=2
    )
    facts = fact_map(ev)
    returned = [facts[r] for r in ev.returned_fact_ids if facts[r].metric is M.REQUESTS]
    coverage = [facts[r] for r in ev.coverage_fact_ids if facts[r].metric is M.REQUESTS]
    assert ev.truncated and len(ev.other_bucket_fact_ids) == 5
    assert sum(f.value for f in returned) == coverage[0].value
    assert {f.segment.filters.get(D.SUPPLY_ID) for f in returned} == {
        "video",
        "gamma",
        None,
    }
    other = facts[ev.other_bucket_fact_ids[0]]
    assert other.segment.cohort_id.startswith("other_")
    promoted = extend_bundle(ctx.bundle, ev)
    assert len(promoted.facts) == len(facts)
    assert not len(ctx.bundle.facts)  # immutable
    assert extend_bundle(promoted, ev).facts.values() == promoted.facts.values()


def test_breakdown_keeps_fixed_placement(ctx):
    banner = segment_id_for(ctx.run_namespace, {D.PLACEMENT_TYPE: "Banner"})
    ev = breakdown(ctx, banner, D.SUPPLY_ID, full_period(ctx.metrics).period_id, 25)
    facts = fact_map(ev)
    for ref in ev.returned_fact_ids:
        assert facts[ref].segment.filters[D.PLACEMENT_TYPE] == "Banner"
        assert facts[ref].segment.filters[D.SUPPLY_ID] != "video"
    assert not ev.truncated and not ev.other_bucket_fact_ids


def test_daily_absent_distinct_from_observed_zero(ctx):
    ev = daily_trend(ctx, target(ctx), full_period(ctx.metrics).period_id)
    facts = fact_map(ev)
    requests = {
        facts[r].period.start: facts[r]
        for r in ev.returned_fact_ids
        if facts[r].metric is M.REQUESTS
    }
    assert len(requests) == len(DAYS)
    assert requests[DAYS[2]].value is None
    assert requests[DAYS[2]].validity.reason == "absent_daily_observation"
    assert requests[DAYS[3]].value == 0
    assert requests[DAYS[3]].validity.status is ValidityStatus.VALID


def test_peer_definition_excludes_target_and_other_placement(ctx):
    definition = peer_definition(ctx.run_namespace)
    ev = compare_to_peers(
        ctx,
        target(ctx),
        full_period(ctx.metrics).period_id,
        definition.peer_definition_id,
    )
    facts = fact_map(ev)
    request_facts = [
        facts[r] for r in ev.returned_fact_ids if facts[r].metric is M.REQUESTS
    ]
    baseline = next(
        f for f in request_facts if f.segment.cohort_id.startswith("excluding_")
    )
    assert baseline.value == (200 + 300) * 7  # excludes Video and alpha
    comparisons = [f for f in facts.values() if f.comparison]
    assert comparisons
    assert all(D.PLACEMENT_TYPE in f.comparison.matching_keys for f in comparisons)
    assert all(
        f.comparison.left_fact_id in facts and f.comparison.right_fact_id in facts
        for f in comparisons
    )


def test_tool_namespace_scope_and_argument_adversaries(ctx):
    period = full_period(ctx.metrics).period_id
    with pytest.raises(ValueError, match="namespace"):
        daily_trend(replace(ctx, run_namespace="N_" + "0" * 16), target(ctx), period)
    with pytest.raises(ValueError):
        breakdown(ctx, target(ctx), "shell", period)
    with pytest.raises(ValueError):
        breakdown(ctx, target(ctx), D.COUNTRY, period, 100000)
    with pytest.raises(ValueError, match="fixed"):
        breakdown(ctx, target(ctx), D.COUNTRY, period)
    with pytest.raises(ValueError, match="unknown"):
        compare_to_peers(ctx, target(ctx), period, "D_" + "0" * 16)


def test_router_allows_followup_on_a_tool_returned_child_and_caches(ctx):
    account = segment_id_for(ctx.run_namespace, {})
    period = full_period(ctx.metrics)
    initial = breakdown(ctx, account, D.SUPPLY_ID, period.period_id, 1)
    root_facts = [f for f in initial.appended_facts if f.segment.segment_id == account]
    candidate = Candidate(
        candidate_id=make_id(IdPrefix.CANDIDATE, ctx.run_namespace, "root"),
        family="wasted_requests",
        segment_id=account,
        period_id=period.period_id,
        eligibility="eligible",
        trigger_fact_ids=[root_facts[0].fact_id],
        daily_fact_ids=[],
        peer_fact_ids=[],
        counter_evidence_fact_ids=[],
        quality_issue_ids=[],
        overlap_group_ids=[],
    )
    pack = CandidatePack(
        run_namespace=ctx.run_namespace,
        candidate=candidate,
        quality_issues=[],
        segments=[ctx.metrics.segments[account]],
        periods=[period],
        facts=root_facts,
        assumptions=[],
        scenarios=[],
        permitted_test_templates=[],
        tool_allowlist=["breakdown", "daily_trend"],
        token_budget=12288,
    )
    bundle = replace(
        ctx.bundle,
        facts=FactRegistry(ctx.run_namespace, root_facts),
        candidates={candidate.candidate_id: candidate},
        packs={candidate.candidate_id: pack},
    )
    router = AnalyticToolRouter(replace(ctx, bundle=bundle))
    call = BreakdownArgs(
        segment_id=account, period_id=period.period_id, by=D.SUPPLY_ID, limit=1
    )
    ev = router.dispatch(candidate.candidate_id, call)
    assert router.dispatch(candidate.candidate_id, call) is ev
    child = next(
        router.ctx.bundle.facts[r].segment.segment_id
        for r in ev.returned_fact_ids
        if router.ctx.bundle.facts[r].segment.cohort_id == "full_input"
    )
    trend = router.dispatch(
        candidate.candidate_id,
        DailyTrendArgs(segment_id=child, period_id=period.period_id),
    )
    assert trend.returned_fact_ids


def test_daily_sensitivity_never_reintroduces_excluded_observed_dates(ctx):
    original = full_period(ctx.metrics)
    subset = make_period(
        ctx.run_namespace,
        [d for d in original.included_dates if d != DAYS[4]],
        original.coverage_status,
    )
    metrics = replace(
        ctx.metrics, periods={**ctx.metrics.periods, subset.period_id: subset}
    )
    context = replace(ctx, metrics=metrics)
    ev = daily_trend(context, target(ctx), subset.period_id)
    facts = fact_map(ev)
    assert all(facts[r].period.start != DAYS[4] for r in ev.returned_fact_ids)
    assert any(
        facts[r].period.start == DAYS[2] and facts[r].value is None
        for r in ev.returned_fact_ids
    )  # truly absent, not intentionally excluded


def test_registry_rejects_contaminated_cache(ctx):
    ev = daily_trend(ctx, target(ctx), full_period(ctx.metrics).period_id)
    bundle = extend_bundle(ctx.bundle, ev)
    poisoned = ev.appended_facts[0].model_copy(update={"value": Decimal("999")})
    bad = ev.model_copy(update={"appended_facts": [poisoned]})
    with pytest.raises(ValueError, match="collision"):
        extend_bundle(bundle, bad)
    with pytest.raises(ValueError, match="namespace"):
        extend_bundle(
            ctx.bundle, ev.model_copy(update={"run_namespace": "N_" + "0" * 16})
        )
