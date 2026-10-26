"""Arithmetic and provenance for code-owned report estimates (synthetic only)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from contracts.enums import DetectorFamily, FactKind, MetricKey, RunStatus
from contracts.runtime import FactRegistry, ScenarioRegistry
from report.opportunities import Opportunities, prepare_estimates
from scripts.build_demo_run import build_demo_outcome

D = Decimal


@pytest.fixture(scope="module")
def fallback_run():
    run = build_demo_outcome(RunStatus.FINAL).run
    recs = tuple(
        r.model_copy(
            update={
                "recommendation": r.recommendation.model_copy(
                    update={"scenario_ids": []}
                )
            }
        )
        for r in run.recommendations
    )
    # Remove illustrative scenarios so each arithmetic fallback is exercised.
    run = replace(
        run,
        recommendations=recs,
        bundle=replace(
            run.bundle, scenarios=ScenarioRegistry(run.bundle.run_namespace)
        ),
    )
    return prepare_estimates(run)


def test_all_families_have_registered_estimates_with_historical_parents(fallback_run):
    run = fallback_run
    planner = Opportunities(run)
    modes = set()
    for rec in run.recommendations:
        if rec.recommendation.decision.value not in (
            "recommend_test",
            "investigate_first",
        ):
            continue
        plan = planner.plan(rec)
        modes.add(plan.mode)
        assert plan.results
        assert plan.assumption.assumption_id in run.bundle.assumptions
        for f in plan.results:
            assert f.fact_id in run.bundle.facts and f.kind is FactKind.SCENARIO
            assert f.provenance.parent_fact_ids
            assert all(i in run.bundle.facts for i in f.provenance.parent_fact_ids)
    assert modes == {"scale", "risk", "margin", "floor", "delivery", "sensitivity"}
    # Registration is append-only and idempotent.
    assert prepare_estimates(run) is run


def test_fallback_arithmetic(fallback_run):
    planner = Opportunities(fallback_run)
    for rec in fallback_run.recommendations:
        if rec.recommendation.decision.value not in (
            "recommend_test",
            "investigate_first",
        ):
            continue
        r = rec.recommendation
        plan = planner.plan(rec)
        value = plan.results[0].value
        rev = planner.metric(r, MetricKey.REVENUE).value
        if plan.mode == "scale":
            req = planner.metric(r, MetricKey.REQUESTS).value
            ppm = planner.metric(r, MetricKey.PROFIT_PER_MILLION_REQUESTS).value
            assert value == req * D("0.1") * ppm / D(1000000)
        elif plan.mode == "risk":
            assert value == rev * D("0.5")
            assert plan.score == 0  # risk is never ranked as gain
        elif plan.mode == "margin":
            profit = planner.metric(r, MetricKey.PROFIT).value
            margin = planner.accounts[(r.period_id, MetricKey.MARGIN)].value
            assert value == max(D(0), rev * margin - profit)
        elif plan.mode == "floor":
            wins = planner.metric(r, MetricKey.WINS).value
            imp = planner.metric(r, MetricKey.IMPRESSIONS).value
            assert value == rev / imp * min(wins, imp) * D("0.05")
            assert plan.results[1].value == value * 2
        elif plan.mode == "delivery":
            rate = planner.metric(r, MetricKey.IMPRESSION_TO_WIN_RATE).value
            baseline = planner.accounts[
                (r.period_id, MetricKey.IMPRESSION_TO_WIN_RATE)
            ].value
            assert value == rev * max(D(0), baseline / rate - 1)
        else:
            assert value == rev * D("0.05")


def test_existing_bundle_scenarios_used_even_when_model_does_not_select_them():
    run = build_demo_outcome(RunStatus.FINAL).run
    rec = run.recommendations[0]
    no_selection = rec.model_copy(
        update={
            "recommendation": rec.recommendation.model_copy(update={"scenario_ids": []})
        }
    )
    plan = Opportunities(run).plan(no_selection)
    assert plan.scenario_id == rec.recommendation.scenario_ids[0]
    assert plan.assumption is None


def test_not_estimable_only_when_required_inputs_missing(fallback_run):
    run = replace(
        fallback_run,
        bundle=replace(
            fallback_run.bundle, facts=FactRegistry(fallback_run.bundle.run_namespace)
        ),
    )
    planner = Opportunities(run)
    rec = run.recommendations[0]
    plan = planner.plan(rec)
    assert not plan.results and "missing requests" in plan.reason


def test_floor_flag_on_near_zero_yield_becomes_throttle(fallback_run):
    planner = Opportunities(fallback_run)
    waste = next(
        r
        for r in fallback_run.recommendations
        if planner.family(r.recommendation) is DetectorFamily.WASTED_REQUESTS
    )
    candidate = fallback_run.bundle.candidates[waste.recommendation.candidate_id]
    candidates = dict(fallback_run.bundle.candidates)
    candidates[candidate.candidate_id] = candidate.model_copy(
        update={"family": DetectorFamily.FLOOR_HIGH_BID}
    )
    run = replace(
        fallback_run, bundle=replace(fallback_run.bundle, candidates=candidates)
    )
    assert Opportunities(run).plan(waste).family is DetectorFamily.WASTED_REQUESTS
