"""Code-owned planning estimates, registered before either report is rendered.

These are sensitivities over the observed exposure, not causal forecasts. They
never modify validated model IR. Existing usable scenarios take precedence;
fallbacks retain their historical parents and an explicit planning assumption.
Export cost is not assumed to be avoidable request-processing cost.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from contracts.enums import (
    AssumptionSelector,
    Decision,
    DetectorFamily,
    FactKind,
    MetricKey,
    ScenarioTemplate,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import (
    Assumption,
    AssumptionBounds,
    CandidateRecommendation,
    Fact,
    ValidatedRecommendation,
    Validity,
)
from contracts.runtime import ValidatedRun

D = Decimal
M = MetricKey
F = DetectorFamily


@dataclass(frozen=True)
class Opportunity:
    family: DetectorFamily
    results: tuple[Fact, ...] = ()
    assumption: Assumption | None = None
    scenario_id: str | None = None
    mode: str = "missing"
    reason: str = "missing revenue and comparable yield inputs"

    @property
    def score(self) -> Decimal:
        # Revenue at risk is not a gain. Never rank a large risk as a large benefit.
        if self.mode == "risk":
            return D(0)
        profit = [
            f for f in self.results
            if f.metric is M.PROFIT and f.value is not None
        ]
        # Cards display weekly profit, so differing observed periods must rank
        # on that same basis. Revenue-only results are not profit opportunity.
        if not profit:
            return D(0)
        fact = profit[-1]
        return max(D(0), fact.value) * D(7) / D(len(fact.period.included_dates))


class Opportunities:
    def __init__(self, run: ValidatedRun):
        self.run = run
        self.b = run.bundle
        self.facts = self.b.facts.values()
        self.own = {
            (f.segment.segment_id, f.period.period_id, f.metric): f
            for f in self.facts
            if f.kind in (FactKind.MEASURED, FactKind.CALCULATED)
            and f.value is not None
            and f.validity.status in (ValidityStatus.VALID, ValidityStatus.TAINTED)
        }
        self.scenarios = {}
        for s in self.b.scenarios.values():
            self.scenarios.setdefault((s.segment_id, s.period_id), []).append(s)
        self.accounts = {
            (f.period.period_id, f.metric): f
            for f in self.own.values()
            if not f.segment.filters and f.segment.cohort_id == "full_input"
        }

    def metric(self, r: CandidateRecommendation, metric: MetricKey | None) -> Fact | None:
        return self.own.get((r.segment_id, r.period_id, metric))

    def family(self, r: CandidateRecommendation) -> DetectorFamily:
        cand = self.b.candidates.get(r.candidate_id)
        family = cand.family if cand else F.TREND
        # A floor flag on near-zero-yield traffic must not hide the waste pattern.
        rpm = self.metric(r, M.REVENUE_PER_MILLION_REQUESTS)
        account = self.accounts.get((r.period_id, M.REVENUE_PER_MILLION_REQUESTS))
        requests = self.metric(r, M.REQUESTS)
        if (
            family in (F.FLOOR_HIGH_BID, F.FLOOR_LOW_BID)
            and rpm
            and account
            and requests
            and requests.value >= self.run.config.analytics.waste_min_requests
            and account.value > 0
            and 0 <= rpm.value < min(
                account.value * D("0.1"),
                self.run.config.detectors.waste_max_revenue_per_million,
            )
        ):
            return F.WASTED_REQUESTS
        return family

    def plan(self, rec: ValidatedRecommendation) -> Opportunity:
        r = rec.recommendation
        family = self.family(r)
        templates = {
            F.SCALE_WINNER: ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS,
            F.COMBINATION_OUTLIER: ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS,
            F.WASTED_REQUESTS: ScenarioTemplate.THROTTLE,
            F.MARGIN_LEAK: ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY,
            F.WINS_IMPRESSIONS_DISCREPANCY: ScenarioTemplate.RENDERING_SENSITIVITY,
        }
        selected = [self.b.scenarios[i] for i in r.scenario_ids]
        selected += [
            s
            for s in self.scenarios.get((r.segment_id, r.period_id), [])
            if s.scenario_id not in r.scenario_ids
            and s.template == templates.get(family)
        ]
        for s in selected:
            # Fixed margin/rendering sensitivities are not identified upside.
            # Use observed account/peer comparisons below instead; floor effects
            # require experiment data, never an arbitrary percentage uplift.
            if s.template not in (
                ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS, ScenarioTemplate.THROTTLE,
            ) or s.template != templates.get(family):
                continue
            ids = (
                [s.revenue_at_risk_fact_id]
                if s.template is ScenarioTemplate.THROTTLE
                else s.result_fact_ids
            )
            results = tuple(
                self.b.facts[i]
                for i in ids
                if i
                and self.b.facts[i].unit is Unit.EXPORT_DOLLAR
                and self.b.facts[i].value is not None
            )
            if results:
                return Opportunity(
                    family,
                    results,
                    scenario_id=s.scenario_id,
                    mode="risk"
                    if s.template is ScenarioTemplate.THROTTLE
                    else "scenario",
                )
        return self.fallback(rec, family)

    def fallback(self, rec: ValidatedRecommendation, family: DetectorFamily) -> Opportunity:
        r = rec.recommendation
        req, rev, profit, ppm = (
            self.metric(r, m)
            for m in (M.REQUESTS, M.REVENUE, M.PROFIT, M.PROFIT_PER_MILLION_REQUESTS)
        )
        parents = []
        mode, metric, value, fraction = "missing", M.REVENUE, None, D("0.1")
        label = (
            "Additional eligible request fraction at unchanged observed profit yield"
        )
        reason = "missing requests or observed profit yield"
        if family in (F.SCALE_WINNER, F.COMBINATION_OUTLIER):
            if family is F.COMBINATION_OUTLIER:
                cand = self.b.candidates.get(r.candidate_id)
                comparisons = [self.b.facts[i] for i in cand.trigger_fact_ids
                               if self.b.facts[i].comparison] if cand else []
                if not comparisons or not all(f.comparison.ratio is not None and f.comparison.ratio > 1
                                               for f in comparisons):
                    return Opportunity(family, reason="a tested improvement for this low-yield combination")
            if req and ppm and ppm.value > 0:
                parents, value, metric, mode = (
                    [req, ppm],
                    req.value * fraction * ppm.value / D(1000000),
                    M.PROFIT,
                    "scale",
                )
        elif family is F.WASTED_REQUESTS:
            reason = "missing revenue for the throttled slice"
            if rev:
                fraction, label = (
                    D("0.5"),
                    "Throttled request share at unchanged traffic mix",
                )
                parents, value, mode = [rev], max(D(0), rev.value) * fraction, "risk"
        elif family is F.MARGIN_LEAK:
            reason = "missing revenue, profit or account margin"
            baseline = self.accounts.get((r.period_id, M.MARGIN))
            if rev and profit and baseline:
                fraction, label = (
                    baseline.value,
                    "Target margin equals the observed account margin at unchanged revenue",
                )
                parents = [rev, profit, baseline]
                value, metric, mode = (
                    max(D(0), rev.value * fraction - profit.value),
                    M.PROFIT,
                    "margin",
                )
        elif family in (F.FLOOR_HIGH_BID, F.FLOOR_LOW_BID):
            reason = "bid distributions and a controlled floor-test result"
        elif family is F.WINS_IMPRESSIONS_DISCREPANCY:
            rate = self.metric(r, M.IMPRESSION_TO_WIN_RATE)
            baseline = self.accounts.get((r.period_id, M.IMPRESSION_TO_WIN_RATE))
            cand = self.b.candidates.get(r.candidate_id)
            if cand:
                for fid in cand.peer_fact_ids:
                    peer = self.b.facts[fid]
                    if peer.comparison and peer.metric is M.IMPRESSION_TO_WIN_RATE:
                        baseline = self.b.facts[peer.comparison.right_fact_id]
                        break
            reason = "missing realized profit or comparable impressions-per-win rate"
            if (
                profit
                and rate
                and baseline
                and rate.value > 0
                and baseline.value is not None
            ):
                fraction = max(D(0), baseline.value / rate.value - 1)
                label = "Impressions per win reaches the comparison rate at unchanged profit per impression; counter check required"
                parents, value, metric, mode = (
                    [profit, rate, baseline],
                    max(D(0), profit.value) * fraction,
                    M.PROFIT,
                    "delivery",
                )
        else:
            reason = "a measured intervention result"
        if value is None:
            return Opportunity(family, reason=reason)
        ns = self.b.run_namespace
        identity = {"planning_recommendation": rec.recommendation_id, "mode": mode}
        if mode == "delivery":
            identity["arithmetic_version"] = 2  # old cached results sized revenue, not profit
        assumption = Assumption(
            assumption_id=make_id(IdPrefix.ASSUMPTION, ns, identity),
            run_namespace=ns,
            label=label,
            parameter_key=f"report_{mode}_fraction",
            value=fraction,
            unit=Unit.FRACTION,
            bounds=AssumptionBounds(
                minimum=fraction, maximum=fraction
            ),
            rationale="Code-owned planning sensitivity, not a forecast or a model claim.",
            selector=AssumptionSelector.DEFAULT_TEST_TEMPLATE,
            segment_id=r.segment_id,
            period_id=r.period_id,
            dependency_ids=[],
        )
        anchor = parents[0]
        tainted = any(p.validity.status is ValidityStatus.TAINTED for p in parents)
        result = Fact(
            fact_id=make_id(IdPrefix.FACT, ns, identity),
            run_namespace=ns,
            kind=FactKind.SCENARIO,
            segment=anchor.segment,
            period=anchor.period,
            metric=metric,
            value=value,
            unit=Unit.EXPORT_DOLLAR,
            validity=Validity(
                status=ValidityStatus.TAINTED if tainted else ValidityStatus.VALID,
                reason="includes flagged historical inputs" if tainted else None,
            ),
            provenance=anchor.provenance.model_copy(
                update={
                    "formula_id": f"report_{mode}_v2" if mode == "delivery" else f"report_{mode}_v1",
                    "parent_fact_ids": [p.fact_id for p in parents],
                    "weighting": Weighting.NOT_APPLICABLE,
                    "quality_issue_ids": sorted(
                        {i for p in parents for i in p.provenance.quality_issue_ids}
                    ),
                }
            ),
        )
        # Read back registered records on render; prepare_estimates owns registration.
        results = [self.b.facts.get(result.fact_id) or result]
        return Opportunity(
            family,
            tuple(results),
            self.b.assumptions.get(assumption.assumption_id) or assumption,
            mode=mode,
        )


def prepare_estimates(run: ValidatedRun) -> ValidatedRun:
    """Append planning facts/assumptions once; leave model recommendations untouched."""
    planner = Opportunities(run)
    facts, assumptions = [], []
    for rec in run.recommendations:
        if rec.recommendation.decision not in (
            Decision.RECOMMEND_TEST,
            Decision.INVESTIGATE_FIRST,
        ):
            continue
        plan = planner.plan(rec)
        facts.extend(f for f in plan.results if f.fact_id not in run.bundle.facts)
        if (
            plan.assumption
            and plan.assumption.assumption_id not in run.bundle.assumptions
        ):
            assumptions.append(plan.assumption)
    if not facts and not assumptions:
        return run
    return replace(
        run,
        bundle=replace(
            run.bundle,
            facts=run.bundle.facts.extended(facts),
            assumptions=run.bundle.assumptions.extended(assumptions),
        ),
    )
