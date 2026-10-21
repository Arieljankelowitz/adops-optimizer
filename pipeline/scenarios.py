"""Pure conditional-impact formulas over historical facts and test assumptions.

Every scenario output is a ``scenario`` fact computed here, never typed by the
model, over the observed period's comparable exposure (no annualization):

* additional eligible requests: ``increment = requests x fraction``; revenue and
  profit at observed unit yields; a matched displaced source (a profit-per-
  million fact on another segment) is subtracted, otherwise opportunity cost
  is disclosed as unknown;
* throttle: requests freed, and proportional revenue and profit at risk;
  savings, redirect gains and net uplift stay unknown;
* margin negotiation sensitivity: ``revenue x points`` at unchanged revenue,
  where 0.05 means five percentage points, not a 5% relative change;
* rendering sensitivity: ``+r`` relative realized impressions at unchanged
  mix and unit economics, an illustrative sensitivity only;
* not identifiable: a null impact (floor and other causal investigations).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from contracts.enums import (
    DisplacedCostTreatment,
    FactKind,
    MetricKey,
    ScenarioTemplate,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import Assumption, Fact, Provenance, Scenario, ScenarioRequest, Validity
from contracts.runtime import AssumptionRegistry, FactRegistry, ScenarioRegistry
from contracts.templates import PrerequisiteCode as Pre
from contracts.templates import RiskCode as Risk

MILLION = Decimal(1_000_000)
R = MetricKey

# Assumption parameter key and unit each template requires.
PARAMETERS: dict[ScenarioTemplate, tuple[str, Unit]] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: ("additional_request_fraction", Unit.FRACTION),
    ScenarioTemplate.THROTTLE: ("throttle_fraction", Unit.FRACTION),
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: ("margin_sensitivity_points", Unit.PERCENTAGE_POINTS),
    ScenarioTemplate.RENDERING_SENSITIVITY: ("rendering_sensitivity_relative", Unit.FRACTION),
}

# Historical metrics each template reads on the request's own segment and period.
REQUIRED_METRICS: dict[ScenarioTemplate, tuple[MetricKey, ...]] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: (
        R.REQUESTS, R.REVENUE_PER_MILLION_REQUESTS, R.PROFIT_PER_MILLION_REQUESTS,
    ),
    ScenarioTemplate.THROTTLE: (R.REQUESTS, R.REVENUE, R.PROFIT),
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: (R.REVENUE,),
    ScenarioTemplate.RENDERING_SENSITIVITY: (R.IMPRESSIONS, R.REVENUE, R.PROFIT),
    ScenarioTemplate.NOT_IDENTIFIABLE: (),
}

PREREQUISITES: dict[ScenarioTemplate, list[Pre]] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: [Pre.CONFIRM_ELIGIBLE_INVENTORY, Pre.CONFIRM_CAPACITY],
    ScenarioTemplate.THROTTLE: [Pre.CONFIRM_CONTRACT_TERMS, Pre.ESTABLISH_CONCURRENT_HOLDOUT],
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: [Pre.CONFIRM_CONTRACT_TERMS],
    ScenarioTemplate.RENDERING_SENSITIVITY: [Pre.RECONCILE_COUNTERS_WITH_LOGS],
    ScenarioTemplate.NOT_IDENTIFIABLE: [Pre.OBTAIN_BID_DISTRIBUTIONS, Pre.ESTABLISH_CONCURRENT_HOLDOUT],
}

LIMITATIONS: dict[ScenarioTemplate, list[Risk]] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: [Risk.DEMAND_SATURATION],
    ScenarioTemplate.THROTTLE: [Risk.REVENUE_AT_RISK, Risk.PARTNER_RELATIONSHIP],
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: [Risk.PARTNER_RELATIONSHIP, Risk.REVENUE_AT_RISK],
    ScenarioTemplate.RENDERING_SENSITIVITY: [Risk.COUNTER_SEMANTICS_UNKNOWN, Risk.MEASUREMENT_NOISE],
    ScenarioTemplate.NOT_IDENTIFIABLE: [Risk.MEASUREMENT_NOISE],
}

# The output a portfolio may sum, for templates that can ever be summed.
PRIMARY_OUTPUT: dict[ScenarioTemplate, str] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: "conditional_profit",
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: "profit_change",
    ScenarioTemplate.RENDERING_SENSITIVITY: "conditional_profit",
}

NOT_IDENTIFIABLE_REASON = "impact is not identifiable from this export without a controlled result"


@dataclass(frozen=True)
class ScenarioResult:
    """Scenarios plus the ``scenario`` facts they reference, for the fact registry."""

    scenarios: ScenarioRegistry
    facts: tuple[Fact, ...]
    # scenario_id -> output name -> result fact_id
    outputs: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class _Value:
    value: Decimal | None
    reason: str | None
    tainted: str | None


def _value(fact: Fact) -> _Value:
    if fact.value is None:
        return _Value(None, f"historical {fact.metric.value} is undefined", None)
    tainted = fact.validity.reason if fact.validity.status is ValidityStatus.TAINTED else None
    return _Value(fact.value, None, tainted)


def _times(*parts: _Value | Decimal) -> _Value:
    result = Decimal(1)
    tainted = None
    for part in parts:
        if isinstance(part, Decimal):
            result *= part
            continue
        if part.value is None:
            return _Value(None, part.reason, None)
        result *= part.value
        tainted = tainted or part.tainted
    return _Value(result, None, tainted)


def _minus(a: _Value, b: _Value) -> _Value:
    if a.value is None or b.value is None:
        return _Value(None, a.reason or b.reason, None)
    return _Value(a.value - b.value, None, a.tainted or b.tainted)


def _resolve(registry: FactRegistry, ids: Iterable[str]) -> list[Fact]:
    facts = []
    for fact_id in ids:
        fact = registry.get(fact_id)
        if fact is None:
            raise ValueError(f"unknown historical fact {fact_id}")
        if fact.kind is FactKind.SCENARIO:
            raise ValueError(f"{fact_id} is a scenario output, not a historical fact")
        facts.append(fact)
    return facts


def _own_metrics(request: ScenarioRequest, facts: Sequence[Fact]) -> dict[MetricKey, Fact]:
    own: dict[MetricKey, Fact] = {}
    for fact in facts:
        if fact.segment.segment_id != request.segment_id or fact.period.period_id != request.period_id:
            continue
        if fact.metric in own:
            raise ValueError(f"two historical {fact.metric.value} facts for the scenario segment")
        own[fact.metric] = fact
    missing = [m.value for m in REQUIRED_METRICS[request.template] if m not in own]
    if missing:
        raise ValueError(f"{request.template.value} needs historical facts for {missing}")
    return own


def _assumption(
    assumptions: AssumptionRegistry, request: ScenarioRequest
) -> Assumption | None:
    if request.template not in PARAMETERS:
        if request.assumption_ids:
            raise ValueError(f"{request.template.value} takes no assumptions")
        return None
    key, unit = PARAMETERS[request.template]
    chosen = []
    for assumption_id in request.assumption_ids:
        assumption = assumptions.get(assumption_id)
        if assumption is None:
            raise ValueError(f"unknown assumption {assumption_id}")
        if assumption.segment_id != request.segment_id or assumption.period_id != request.period_id:
            raise ValueError(f"{assumption_id} applies to another segment or period")
        if assumption.parameter_key == key:
            chosen.append(assumption)
    if len(chosen) != 1:
        raise ValueError(f"{request.template.value} needs exactly one {key!r} assumption")
    if chosen[0].unit is not unit:
        raise ValueError(f"{key!r} must be expressed in {unit.value}")
    return chosen[0]


def _scenario_fact(
    namespace: str,
    scenario_id: str,
    output: str,
    metric: MetricKey,
    unit: Unit,
    result: _Value,
    anchor: Fact,
    parents: Sequence[Fact],
    formula_id: str,
) -> Fact:
    if result.value is None:
        validity = Validity(status=ValidityStatus.UNDEFINED, reason=result.reason)
    elif result.tainted:
        validity = Validity(status=ValidityStatus.TAINTED, reason=result.tainted)
    else:
        validity = Validity(status=ValidityStatus.VALID)
    columns = list(dict.fromkeys(c for p in parents for c in p.provenance.source_columns))
    issues = sorted({i for p in parents for i in p.provenance.quality_issue_ids})
    return Fact(
        fact_id=make_id(IdPrefix.FACT, namespace, {"scenario_id": scenario_id, "output": output}),
        run_namespace=namespace,
        kind=FactKind.SCENARIO,
        segment=anchor.segment,
        period=anchor.period,
        metric=metric,
        value=result.value,
        unit=unit,
        validity=validity,
        provenance=Provenance(
            input_sha256=anchor.provenance.input_sha256,
            formula_id=formula_id,
            source_columns=columns,
            source_row_set_hash=anchor.provenance.source_row_set_hash,
            source_line_ranges_ref=anchor.provenance.source_line_ranges_ref,
            parent_fact_ids=[p.fact_id for p in parents],
            aggregation_keys=list(anchor.segment.filters),
            weighting=Weighting.NOT_APPLICABLE,
            quality_issue_ids=issues,
        ),
    )


def _compute_one(
    namespace: str,
    request: ScenarioRequest,
    facts: Sequence[Fact],
    assumption: Assumption | None,
) -> tuple[Scenario, list[Fact], dict[str, str]]:
    template = request.template
    own = _own_metrics(request, facts)
    scenario_id = make_id(
        IdPrefix.SCENARIO,
        namespace,
        {
            "template": template.value,
            "segment_id": request.segment_id,
            "period_id": request.period_id,
            "historical_fact_ids": sorted(request.historical_fact_ids),
            "assumption_ids": sorted(request.assumption_ids),
        },
    )
    anchor = own.get(R.REQUESTS) or own.get(R.REVENUE) or own.get(R.IMPRESSIONS)
    if anchor is None:
        anchor = next(
            (f for f in facts if f.segment.segment_id == request.segment_id and f.period.period_id == request.period_id),
            None,
        )
    if anchor is None:
        raise ValueError("a scenario needs at least one historical fact on its own segment and period")
    a = Decimal(assumption.value) if assumption else Decimal(0)
    outputs: list[tuple[str, MetricKey, Unit, _Value, list[Fact]]] = []
    displaced = DisplacedCostTreatment.NOT_APPLICABLE
    limitations = list(LIMITATIONS[template])
    at_risk: dict[str, str] = {}

    if template is ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS:
        requests, rpm, ppm = own[R.REQUESTS], own[R.REVENUE_PER_MILLION_REQUESTS], own[R.PROFIT_PER_MILLION_REQUESTS]
        increment = _times(_value(requests), a)
        revenue = _times(increment, _value(rpm), 1 / MILLION)
        profit = _times(increment, _value(ppm), 1 / MILLION)
        outputs += [
            ("increment_requests", R.REQUESTS, Unit.COUNT, increment, [requests]),
            ("conditional_revenue", R.REVENUE, Unit.EXPORT_DOLLAR, revenue, [requests, rpm]),
            ("conditional_profit", R.PROFIT, Unit.EXPORT_DOLLAR, profit, [requests, ppm]),
        ]
        sources = [
            f for f in facts
            if f.metric is R.PROFIT_PER_MILLION_REQUESTS
            and f.segment.segment_id != request.segment_id
            and f.period.period_id == request.period_id
        ]
        if len(sources) > 1:
            raise ValueError("at most one matched displaced source is allowed")
        if sources:
            displaced = DisplacedCostTreatment.SUBTRACTED_MATCHED_SOURCE
            lost = _times(increment, _value(sources[0]), 1 / MILLION)
            outputs += [
                ("displaced_profit", R.PROFIT, Unit.EXPORT_DOLLAR, lost, [requests, sources[0]]),
                ("net_profit", R.PROFIT, Unit.EXPORT_DOLLAR, _minus(profit, lost), [requests, ppm, sources[0]]),
            ]
        else:
            displaced = DisplacedCostTreatment.UNKNOWN_DISCLOSED
            limitations.append(Risk.DISPLACED_PROFIT_UNKNOWN)
    elif template is ScenarioTemplate.THROTTLE:
        requests, revenue, profit = own[R.REQUESTS], own[R.REVENUE], own[R.PROFIT]
        outputs += [
            ("requests_freed", R.REQUESTS, Unit.COUNT, _times(_value(requests), a), [requests]),
            ("revenue_at_risk", R.REVENUE, Unit.EXPORT_DOLLAR, _times(_value(revenue), a), [revenue]),
            ("profit_at_risk", R.PROFIT, Unit.EXPORT_DOLLAR, _times(_value(profit), a), [profit]),
        ]
    elif template is ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY:
        revenue = own[R.REVENUE]
        outputs.append(("profit_change", R.PROFIT, Unit.EXPORT_DOLLAR, _times(_value(revenue), a), [revenue]))
    elif template is ScenarioTemplate.RENDERING_SENSITIVITY:
        impressions, revenue, profit = own[R.IMPRESSIONS], own[R.REVENUE], own[R.PROFIT]
        outputs += [
            ("additional_impressions", R.IMPRESSIONS, Unit.COUNT, _times(_value(impressions), a), [impressions]),
            ("conditional_revenue", R.REVENUE, Unit.EXPORT_DOLLAR, _times(_value(revenue), a), [revenue]),
            ("conditional_profit", R.PROFIT, Unit.EXPORT_DOLLAR, _times(_value(profit), a), [profit]),
        ]
    else:
        outputs.append(
            ("impact", R.PROFIT, Unit.EXPORT_DOLLAR, _Value(None, NOT_IDENTIFIABLE_REASON, None), [anchor])
        )

    result_facts = []
    ids: dict[str, str] = {}
    for name, metric, unit, value, parents in outputs:
        fact = _scenario_fact(
            namespace, scenario_id, name, metric, unit, value, anchor, parents,
            f"{template.value}_{name}_v1",
        )
        result_facts.append(fact)
        ids[name] = fact.fact_id
    if template is ScenarioTemplate.THROTTLE:
        at_risk = {"revenue": ids["revenue_at_risk"], "profit": ids["profit_at_risk"]}
    scenario = Scenario(
        scenario_id=scenario_id,
        run_namespace=namespace,
        template=template,
        segment_id=request.segment_id,
        period_id=request.period_id,
        formula_id=f"{template.value}_v1",
        historical_fact_ids=list(request.historical_fact_ids),
        assumption_ids=list(request.assumption_ids),
        result_fact_ids=[f.fact_id for f in result_facts],
        prerequisite_codes=list(PREREQUISITES[template]),
        displaced_cost_treatment=displaced,
        overlap_group_ids=[],
        revenue_at_risk_fact_id=at_risk.get("revenue"),
        profit_at_risk_fact_id=at_risk.get("profit"),
        limitation_codes=list(dict.fromkeys(limitations)),
    )
    return scenario, result_facts, ids


def evaluate_scenarios(
    registry: FactRegistry,
    assumptions: AssumptionRegistry,
    requests: list[ScenarioRequest],
) -> ScenarioResult:
    """Compute every requested scenario; raises on references a code generator should never emit."""
    if assumptions.namespace != registry.namespace:
        raise ValueError("assumptions belong to another run namespace")
    scenarios, facts, outputs = [], [], {}
    for request in requests:
        historical = _resolve(registry, request.historical_fact_ids)
        scenario, result_facts, ids = _compute_one(
            registry.namespace, request, historical, _assumption(assumptions, request)
        )
        if scenario.scenario_id in outputs:
            continue
        scenarios.append(scenario)
        facts.extend(result_facts)
        outputs[scenario.scenario_id] = ids
    return ScenarioResult(
        scenarios=ScenarioRegistry(registry.namespace, scenarios), facts=tuple(facts), outputs=outputs
    )


def compute_scenarios(
    registry: FactRegistry,
    assumptions: AssumptionRegistry,
    requests: list[ScenarioRequest],
) -> ScenarioRegistry:
    """Compute scenario facts (kind ``scenario``) from code formulas; unknown impact stays null.

    The result facts themselves come from ``evaluate_scenarios``, which the
    evidence builder uses to add them to the fact registry.
    """
    return evaluate_scenarios(registry, assumptions, requests).scenarios


@dataclass(frozen=True)
class PortfolioTotal:
    value: Decimal | None
    reason: str | None


def portfolio_total(
    result: ScenarioResult, facts: FactRegistry, scenario_ids: Sequence[str]
) -> PortfolioTotal:
    """Sum of primary profit outputs, only with proof of non-overlap and compatibility.

    Omitted (null with a reason) unless every scenario uses the same summable
    template and period and no two segments can share traffic.
    """
    chosen = [result.scenarios[s] for s in scenario_ids]
    if len(chosen) < 2:
        return PortfolioTotal(None, "a portfolio needs at least two scenarios")
    templates = {s.template for s in chosen}
    if len(templates) != 1 or next(iter(templates)) not in PRIMARY_OUTPUT:
        return PortfolioTotal(None, "scenarios are not the same summable template")
    if len({s.period_id for s in chosen}) != 1:
        return PortfolioTotal(None, "scenarios cover different periods")
    template = next(iter(templates))
    values, filters = [], []
    for s in chosen:
        fact = facts.get(result.outputs[s.scenario_id][PRIMARY_OUTPUT[template]])
        if fact is None:
            raise ValueError(f"scenario output for {s.scenario_id} is not in the fact registry")
        if fact.value is None:
            return PortfolioTotal(None, "a scenario impact is undefined")
        values.append(fact.value)
        filters.append(fact.segment.filters)
    for i, a in enumerate(filters):
        for b in filters[i + 1:]:
            if all(a[d] == b[d] for d in a.keys() & b.keys()):
                return PortfolioTotal(None, "scenario segments may share traffic")
    return PortfolioTotal(sum(values, Decimal(0)), None)
