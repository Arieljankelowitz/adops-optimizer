"""Conditional scenarios: code-only arithmetic, labelling, null impacts and portfolio refusal."""

from decimal import Decimal

import pytest

from contracts.config import AnalyticsConfig
from contracts.enums import (
    AssumptionSelector,
    DisplacedCostTreatment,
    FactKind,
    MetricKey as M,
    ScenarioTemplate as T,
    Unit,
    ValidityStatus,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import Assumption, AssumptionBounds, ScenarioRequest
from contracts.runtime import AssumptionRegistry, FactRegistry
from contracts.templates import RiskCode
from pipeline.metrics import FIVE, FactBuilder, aggregate, full_period, segment_id_for, stats_row
from pipeline.scenarios import compute_scenarios, evaluate_scenarios, portfolio_total
from tests.support.analytics_data import DAYS, loaded_from_rows, quality_for, row

A = ("1", "US", "300x250", "Banner", "Desktop")
B = ("2", "DE", "728x90", "Video", "Mobile")
EMPTY = ("3", "GB", "320x50", "Banner", "Mobile")


@pytest.fixture(scope="module")
def world():
    rows = [
        row(DAYS[0], A, 20_000_000, 2_000_000, 500_000, 400_000, "50.00", "30.00"),
        row(DAYS[0], B, 10_000_000, 1_000_000, 200_000, 150_000, "8.00", "6.00"),
        row(DAYS[0], EMPTY, 0, 0, 0, 0, "0", "0"),
    ]
    data = loaded_from_rows(rows)
    quality = quality_for(data)
    store = aggregate(data, quality, AnalyticsConfig())
    builder = FactBuilder(store, quality)
    period = full_period(store)
    facts = {}
    for name, key in (("a", A), ("b", B), ("empty", EMPTY)):
        seg = store.segments[segment_id_for(store.run_namespace, dict(zip(FIVE, key)))]
        stats = stats_row(store, seg.segment_id, period.period_id)
        facts[name] = {
            m: builder.rate(seg, period, stats, m) if m not in (M.REQUESTS, M.REVENUE, M.PROFIT, M.IMPRESSIONS)
            else builder.additive(seg, period, stats, m)
            for m in (M.REQUESTS, M.REVENUE, M.PROFIT, M.IMPRESSIONS,
                      M.REVENUE_PER_MILLION_REQUESTS, M.PROFIT_PER_MILLION_REQUESTS)
        }
    registry = FactRegistry(store.run_namespace, builder.facts.values())
    return store.run_namespace, period, facts, registry


def _assumption(ns, fact, key, value, unit=Unit.FRACTION, maximum="1"):
    return Assumption(
        assumption_id=make_id(IdPrefix.ASSUMPTION, ns, [key, fact.segment.segment_id, value]),
        run_namespace=ns, label=key.replace("_", " "), parameter_key=key, value=Decimal(value), unit=unit,
        bounds=AssumptionBounds(minimum=Decimal("0"), maximum=Decimal(maximum)),
        rationale="Synthetic test default.", selector=AssumptionSelector.DEFAULT_TEST_TEMPLATE,
        segment_id=fact.segment.segment_id, period_id=fact.period.period_id, dependency_ids=[],
    )


def _request(template, facts, assumptions):
    anchor = facts[0]
    return ScenarioRequest(
        template=template, segment_id=anchor.segment.segment_id, period_id=anchor.period.period_id,
        historical_fact_ids=[f.fact_id for f in facts], assumption_ids=[a.assumption_id for a in assumptions],
    )


def _outputs(result, scenario_id, registry_facts):
    by_id = {f.fact_id: f for f in registry_facts}
    return {name: by_id[fid] for name, fid in result.outputs[scenario_id].items()}


def test_additional_requests_uses_observed_unit_yields(world) -> None:
    ns, _, facts, registry = world
    a = facts["a"]
    fraction = _assumption(ns, a[M.REQUESTS], "additional_request_fraction", "0.10")
    req = _request(T.ADDITIONAL_ELIGIBLE_REQUESTS,
                   [a[M.REQUESTS], a[M.REVENUE_PER_MILLION_REQUESTS], a[M.PROFIT_PER_MILLION_REQUESTS]], [fraction])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [fraction]), [req])
    scenario = result.scenarios.values()[0]
    out = _outputs(result, scenario.scenario_id, result.facts)
    assert out["increment_requests"].value == Decimal("2000000.00")
    assert out["conditional_revenue"].value == Decimal("5.00")  # 2M x 2.50 per million
    assert out["conditional_profit"].value == Decimal("2.00")  # 2M x 1.00 per million
    assert scenario.conditional is True
    assert scenario.horizon == "observed_period_comparable_exposure"
    assert scenario.displaced_cost_treatment is DisplacedCostTreatment.UNKNOWN_DISCLOSED
    assert RiskCode.DISPLACED_PROFIT_UNKNOWN in scenario.limitation_codes
    assert all(f.kind is FactKind.SCENARIO for f in result.facts)
    assert set(scenario.result_fact_ids) == {f.fact_id for f in result.facts}
    assert compute_scenarios(registry, AssumptionRegistry(ns, [fraction]), [req]).values() == [scenario]


def test_additional_requests_subtracts_a_matched_displaced_source(world) -> None:
    ns, _, facts, registry = world
    a, b = facts["a"], facts["b"]
    fraction = _assumption(ns, a[M.REQUESTS], "additional_request_fraction", "0.10")
    req = _request(T.ADDITIONAL_ELIGIBLE_REQUESTS,
                   [a[M.REQUESTS], a[M.REVENUE_PER_MILLION_REQUESTS], a[M.PROFIT_PER_MILLION_REQUESTS],
                    b[M.PROFIT_PER_MILLION_REQUESTS]], [fraction])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [fraction]), [req])
    scenario = result.scenarios.values()[0]
    out = _outputs(result, scenario.scenario_id, result.facts)
    assert out["displaced_profit"].value == Decimal("0.40")  # 2M x 0.20 per million
    assert out["net_profit"].value == Decimal("1.60")
    assert scenario.displaced_cost_treatment is DisplacedCostTreatment.SUBTRACTED_MATCHED_SOURCE


def test_throttle_reports_freed_requests_and_proportional_risk(world) -> None:
    ns, _, facts, registry = world
    a = facts["a"]
    fraction = _assumption(ns, a[M.REQUESTS], "throttle_fraction", "0.50")
    req = _request(T.THROTTLE, [a[M.REQUESTS], a[M.REVENUE], a[M.PROFIT]], [fraction])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [fraction]), [req])
    scenario = result.scenarios.values()[0]
    out = _outputs(result, scenario.scenario_id, result.facts)
    assert out["requests_freed"].value == Decimal("10000000.00")
    assert out["revenue_at_risk"].value == Decimal("25.0000")
    assert out["profit_at_risk"].value == Decimal("10.0000")
    assert scenario.revenue_at_risk_fact_id == out["revenue_at_risk"].fact_id
    assert scenario.profit_at_risk_fact_id == out["profit_at_risk"].fact_id


def test_margin_sensitivity_is_points_of_revenue_not_relative(world) -> None:
    ns, _, facts, registry = world
    a = facts["a"]
    points = _assumption(ns, a[M.REVENUE], "margin_sensitivity_points", "0.05", Unit.PERCENTAGE_POINTS)
    req = _request(T.MARGIN_NEGOTIATION_SENSITIVITY, [a[M.REVENUE]], [points])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [points]), [req])
    out = _outputs(result, result.scenarios.values()[0].scenario_id, result.facts)
    # 5 points of $50 revenue is $2.50 (a 5% relative profit rise would be $1.00).
    assert out["profit_change"].value == Decimal("2.5000")


def test_rendering_sensitivity_is_relative_impressions(world) -> None:
    ns, _, facts, registry = world
    a = facts["a"]
    rel = _assumption(ns, a[M.IMPRESSIONS], "rendering_sensitivity_relative", "0.05")
    req = _request(T.RENDERING_SENSITIVITY, [a[M.IMPRESSIONS], a[M.REVENUE], a[M.PROFIT]], [rel])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [rel]), [req])
    out = _outputs(result, result.scenarios.values()[0].scenario_id, result.facts)
    assert out["additional_impressions"].value == Decimal("20000.00")
    assert out["conditional_revenue"].value == Decimal("2.5000")
    assert out["conditional_profit"].value == Decimal("1.0000")


def test_not_identifiable_is_a_null_impact(world) -> None:
    ns, _, facts, registry = world
    req = _request(T.NOT_IDENTIFIABLE, [facts["a"][M.REVENUE]], [])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, []), [req])
    (impact,) = result.facts
    assert impact.value is None
    assert impact.validity.status is ValidityStatus.UNDEFINED
    assert "not identifiable" in impact.validity.reason


def test_undefined_unit_yield_gives_null_scenario(world) -> None:
    ns, _, facts, registry = world
    e = facts["empty"]
    fraction = _assumption(ns, e[M.REQUESTS], "additional_request_fraction", "0.10")
    req = _request(T.ADDITIONAL_ELIGIBLE_REQUESTS,
                   [e[M.REQUESTS], e[M.REVENUE_PER_MILLION_REQUESTS], e[M.PROFIT_PER_MILLION_REQUESTS]], [fraction])
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [fraction]), [req])
    out = _outputs(result, result.scenarios.values()[0].scenario_id, result.facts)
    assert out["conditional_revenue"].value is None
    assert out["conditional_revenue"].validity.status is ValidityStatus.UNDEFINED


def test_bad_requests_are_refused(world) -> None:
    ns, _, facts, registry = world
    a = facts["a"]
    wrong_key = _assumption(ns, a[M.REQUESTS], "throttle_fraction", "0.10")
    req = _request(T.ADDITIONAL_ELIGIBLE_REQUESTS,
                   [a[M.REQUESTS], a[M.REVENUE_PER_MILLION_REQUESTS], a[M.PROFIT_PER_MILLION_REQUESTS]], [wrong_key])
    with pytest.raises(ValueError, match="additional_request_fraction"):
        evaluate_scenarios(registry, AssumptionRegistry(ns, [wrong_key]), [req])
    fraction = _assumption(ns, a[M.REQUESTS], "throttle_fraction", "0.50")
    missing = _request(T.THROTTLE, [a[M.REQUESTS], a[M.REVENUE]], [fraction])
    with pytest.raises(ValueError, match="profit"):
        evaluate_scenarios(registry, AssumptionRegistry(ns, [fraction]), [missing])
    unknown = ScenarioRequest(
        template=T.NOT_IDENTIFIABLE, segment_id=a[M.REVENUE].segment.segment_id,
        period_id=a[M.REVENUE].period.period_id, historical_fact_ids=["F_0000000000000000"], assumption_ids=[],
    )
    with pytest.raises(ValueError, match="unknown historical fact"):
        evaluate_scenarios(registry, AssumptionRegistry(ns, []), [unknown])


def test_portfolio_total_requires_disjoint_compatible_scenarios(world) -> None:
    ns, _, facts, registry = world
    a, b = facts["a"], facts["b"]
    pa = _assumption(ns, a[M.REVENUE], "margin_sensitivity_points", "0.05", Unit.PERCENTAGE_POINTS)
    pb = _assumption(ns, b[M.REVENUE], "margin_sensitivity_points", "0.05", Unit.PERCENTAGE_POINTS)
    reqs = [_request(T.MARGIN_NEGOTIATION_SENSITIVITY, [a[M.REVENUE]], [pa]),
            _request(T.MARGIN_NEGOTIATION_SENSITIVITY, [b[M.REVENUE]], [pb])]
    result = evaluate_scenarios(registry, AssumptionRegistry(ns, [pa, pb]), reqs)
    all_facts = registry.extended(result.facts)
    ids = [s.scenario_id for s in result.scenarios.values()]
    total = portfolio_total(result, all_facts, ids)
    assert total.value == Decimal("2.9000")  # 2.50 + 0.40
    # A throttle and an expansion are never summed.
    fa = _assumption(ns, a[M.REQUESTS], "throttle_fraction", "0.50")
    mixed = evaluate_scenarios(
        registry, AssumptionRegistry(ns, [pa, fa]),
        [reqs[0], _request(T.THROTTLE, [a[M.REQUESTS], a[M.REVENUE], a[M.PROFIT]], [fa])],
    )
    refused = portfolio_total(mixed, registry.extended(mixed.facts), [s.scenario_id for s in mixed.scenarios.values()])
    assert refused.value is None and "template" in refused.reason


def test_portfolio_refuses_overlapping_traffic(world) -> None:
    ns, _, facts, registry = world
    a = facts["a"]
    p1 = _assumption(ns, a[M.REVENUE], "margin_sensitivity_points", "0.05", Unit.PERCENTAGE_POINTS)
    p2 = _assumption(ns, a[M.REVENUE], "margin_sensitivity_points", "0.03", Unit.PERCENTAGE_POINTS)
    result = evaluate_scenarios(
        registry, AssumptionRegistry(ns, [p1, p2]),
        [_request(T.MARGIN_NEGOTIATION_SENSITIVITY, [a[M.REVENUE]], [p]) for p in (p1, p2)],
    )
    total = portfolio_total(result, registry.extended(result.facts), [s.scenario_id for s in result.scenarios.values()])
    assert total.value is None and "share traffic" in total.reason
