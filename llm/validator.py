"""Fail-closed reference/role validation; no number-set or prose matching.

Only code-owned registries are authoritative. In particular, ToolEvidence in a
model analysis cannot register its own facts. Ranking resolves an explicit map
of previously validated recommendations, never guessed recommendation IDs.
"""

import re
from collections.abc import Mapping

from pydantic import ValidationError

from contracts.enums import (
    AnalysisStatus,
    Decision,
    EligibilityStatus,
    FactKind,
    Relation,
    RenderRole,
    ScenarioTemplate,
    Unit,
    ValidityStatus,
)
from contracts.enums import (
    InferenceRestriction as Restriction,
)
from contracts.enums import (
    MetricKey as Metric,
)
from contracts.enums import (
    ValidationErrorCode as Code,
)
from contracts.ids import IdPrefix, canonical_json, make_id
from contracts.models import (
    CandidateAnalysis,
    Fact,
    RankingIR,
    RenderedReport,
    ScenarioRequest,
    ValidatedRecommendation,
    ValidationAccepted,
    ValidationIssue,
    ValidationRejected,
    ValidationResult,
)
from contracts.runtime import EvidenceBundle
from contracts.templates import OBSERVATION_TEMPLATES
from contracts.templates import ObservationTemplateId as Template
from pipeline.detectors import DIMENSION_BLOCKERS, FAMILY_BLOCKERS
from pipeline.evidence import SCENARIOS
from pipeline.metrics import (
    _RATIOS,
    _SHARES,
    ADDITIVE,
    FUNNEL_METRICS,
    MONEY_RATE_METRICS,
    segments_overlap,
)
from pipeline.scenarios import evaluate_scenarios

CONCENTRATION = {
    Metric.MAX_DAY_REVENUE_SHARE,
    Metric.TOP_TWO_DAY_REVENUE_SHARE,
    Metric.MAX_DAY_PROFIT_SHARE,
    Metric.TOP_TWO_DAY_PROFIT_SHARE,
}
UNITS = {m: Unit.COUNT for m in ADDITIVE}
UNITS.update(
    {m: Unit.EXPORT_DOLLAR for m in (Metric.REVENUE, Metric.COST, Metric.PROFIT)}
)
UNITS.update(
    {
        m: Unit.FRACTION
        for m in (
            Metric.RESPONSE_RATE,
            Metric.WIN_RATE,
            Metric.IMPRESSION_TO_WIN_RATE,
            Metric.MARGIN,
            Metric.REQUEST_SHARE,
            Metric.REVENUE_SHARE,
            Metric.PROFIT_SHARE,
            Metric.DAILY_CHANGE,
            *CONCENTRATION,
        )
    }
)
UNITS.update(
    {
        m: Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS
        for m in (
            Metric.REVENUE_PER_MILLION_REQUESTS,
            Metric.PROFIT_PER_MILLION_REQUESTS,
        )
    }
)
UNITS[Metric.IMPRESSION_TO_WIN_RATE] = Unit.RATIO
UNITS[Metric.ECPM] = Unit.EXPORT_DOLLAR_PER_THOUSAND_IMPRESSIONS
UNITS.update(
    {
        m: Unit.EXPORT_PRICE_AS_EXPORTED
        for m in (
            Metric.REQUEST_WEIGHTED_FLOOR,
            Metric.RESPONSE_WEIGHTED_BID,
            Metric.RESPONSE_WEIGHTED_FLOOR,
        )
    }
)
UNITS.update(
    {
        m: Unit.DAYS
        for m in (
            Metric.OBSERVED_DAYS,
            Metric.REVENUE_POSITIVE_DAYS,
            Metric.MISSING_DAYS,
        )
    }
)


class _Validator:
    def __init__(self, bundle: EvidenceBundle):
        self.b = bundle
        self.errors: list[ValidationIssue] = []
        self.facts = {f.fact_id: f for f in bundle.facts.values()}
        anchors = bundle.quality.facts or list(self.facts.values())[:1]
        self.input_sha256 = anchors[0].provenance.input_sha256 if anchors else None
        self.checked: set[str] = set()
        self.active: set[str] = set()
        self.threshold_ids = {
            r for issue in bundle.quality.issues for r in issue.threshold_fact_ids
        }

    def error(
        self,
        code: Code,
        path: str,
        ref: str | None = None,
        detail: str = "reference is incompatible",
    ):
        # Error text is static; never echo data labels or the rejected payload.
        self.errors.append(
            ValidationIssue(code=code, path=path[:200], ref=ref, detail=detail)
        )

    def namespace(self, record, path):
        if record.run_namespace != self.b.run_namespace:
            self.error(Code.STALE_NAMESPACE, path)

    def fact(self, ref: str, path: str) -> Fact | None:
        f = self.facts.get(ref)
        if f is None:
            self.error(Code.UNKNOWN_REFERENCE, path, ref)
            return None
        self.namespace(f, path)
        if f.provenance.input_sha256 != self.input_sha256:
            self.error(
                Code.STALE_NAMESPACE,
                path,
                ref,
                "fact provenance belongs to another input",
            )
        if ref in self.active:
            self.error(Code.WRONG_ROLE, path, ref, "cyclic provenance lineage")
            return f
        if ref in self.checked:
            return f
        self.checked.add(ref)
        self.active.add(ref)
        if self.b.segments.get(f.segment.segment_id) != f.segment:
            self.error(Code.WRONG_SEGMENT, path, ref)
        if self.b.periods.get(f.period.period_id) != f.period:
            self.error(Code.WRONG_PERIOD, path, ref)
        expected = Unit.RATIO if f.kind is FactKind.COMPARISON else UNITS.get(f.metric)
        if expected is not None and f.unit is not expected:
            self.error(Code.WRONG_UNIT, path, ref)
        parents = [
            self.fact(parent, path + ".parents")
            for parent in f.provenance.parent_fact_ids
        ]
        if f.kind is FactKind.CALCULATED and (
            f.metric in _RATIOS or f.metric in _SHARES
        ):
            if f.metric in _RATIOS:
                numerator, denominator, scale, _unit = _RATIOS[f.metric]
            else:
                numerator = denominator = _SHARES[f.metric]
                scale = 1
            if len(parents) != 2 or any(p is None for p in parents):
                self.error(
                    Code.WRONG_ROLE,
                    path,
                    ref,
                    "ratio lacks numerator/denominator lineage",
                )
            else:
                num, den = parents
                if num.metric is not numerator or den.metric is not denominator:
                    self.error(Code.WRONG_METRIC, path, ref)
                if (
                    num.segment != f.segment
                    or num.period != f.period
                    or den.period != f.period
                ):
                    self.error(Code.WRONG_ROLE, path, ref)
                if f.metric in _RATIOS and den.segment != f.segment:
                    self.error(Code.WRONG_SEGMENT, path, ref)
                if f.metric in _SHARES and (
                    den.segment.filters or den.segment.cohort_id != f.segment.cohort_id
                ):
                    self.error(Code.WRONG_SEGMENT, path, ref)
                expected_value = (
                    num.value / den.value * scale
                    if num.value is not None and den.value is not None and den.value > 0
                    else None
                )
                if expected_value != f.value:
                    self.error(
                        Code.WRONG_DIRECTION,
                        path,
                        ref,
                        "ratio differs from registered additive parents",
                    )
        if f.comparison:
            left = self.fact(f.comparison.left_fact_id, path + ".left")
            right = self.fact(f.comparison.right_fact_id, path + ".right")
            if left and right:
                self.comparison(f, left, right, path)
        self.active.remove(ref)
        return f

    def comparison(self, f, left, right, path):
        c = f.comparison
        if c is None:
            self.error(Code.WRONG_ROLE, path, f.fact_id)
            return
        if f.provenance.parent_fact_ids != [left.fact_id, right.fact_id]:
            self.error(
                Code.WRONG_ROLE, path, f.fact_id, "comparison parent lineage differs"
            )
        if f.segment != left.segment or f.period != left.period:
            self.error(Code.WRONG_ROLE, path, f.fact_id)
        if left.unit is not right.unit or c.difference_unit is not left.unit:
            self.error(Code.WRONG_UNIT, path, f.fact_id)
        if f.metric is not left.metric:
            self.error(Code.WRONG_METRIC, path, f.fact_id)
        # Bid/floor is the only approved cross-metric comparison.
        if left.metric is not right.metric and (left.metric, right.metric) != (
            Metric.RESPONSE_WEIGHTED_BID,
            Metric.RESPONSE_WEIGHTED_FLOOR,
        ):
            self.error(Code.WRONG_METRIC, path, f.fact_id)
        for key in c.matching_keys:
            if (
                key not in left.segment.filters
                or key not in right.segment.filters
                or left.segment.filters[key] != right.segment.filters[key]
            ):
                self.error(
                    Code.WRONG_SEGMENT, path, f.fact_id, "fixed matching key differs"
                )
        if left.value is None or right.value is None:
            relation, difference, ratio = Relation.UNDEFINED, None, None
        else:
            difference = left.value - right.value
            relation = (
                Relation.GREATER
                if difference > 0
                else Relation.LESS
                if difference < 0
                else Relation.EQUAL
            )
            ratio = left.value / right.value if right.value > 0 else None
        if (
            c.relation is not relation
            or c.difference != difference
            or c.ratio != ratio
            or f.value != ratio
        ):
            self.error(
                Code.WRONG_DIRECTION,
                path,
                f.fact_id,
                "computed comparison does not match its parents",
            )

    def usable(self, fact, path):
        if fact.kind is FactKind.SCENARIO:
            self.error(
                Code.WRONG_ROLE,
                path,
                fact.fact_id,
                "conditional scenario is not historical evidence",
            )
        if fact.fact_id in self.threshold_ids:
            self.error(
                Code.WRONG_ROLE,
                path,
                fact.fact_id,
                "configured quality threshold is not a historical measurement",
            )
        if fact.value is None or fact.validity.status is not ValidityStatus.VALID:
            self.error(
                Code.INVALID_DENOMINATOR,
                path,
                fact.fact_id,
                "undefined, invalid or tainted evidence cannot support a clean observation",
            )

    def scope(self, fact, candidate, path, *, daily=False):
        if fact.segment.segment_id != candidate.segment_id:
            self.error(Code.WRONG_SEGMENT, path, fact.fact_id)
        period = self.b.periods.get(candidate.period_id)
        wrong_period = (
            fact.period.period_id != candidate.period_id
            if not daily
            else period is None
            or not set(fact.period.included_dates) <= set(period.included_dates)
        )
        if wrong_period:
            self.error(Code.WRONG_PERIOD, path, fact.fact_id)

    def restrictions(self, candidate):
        out = set()
        segment = self.b.segments.get(candidate.segment_id)
        period = self.b.periods.get(candidate.period_id)
        for issue in self.b.quality.issues:
            # Unknown quality scopes conservatively apply, just as in B.
            touches = not issue.segment_ids or any(
                sid not in self.b.segments
                or segment is None
                or segments_overlap(segment.filters, self.b.segments[sid].filters)
                for sid in issue.segment_ids
            )
            dates = not issue.period_ids or any(
                pid not in self.b.periods
                or period is None
                or bool(
                    set(period.included_dates) & set(self.b.periods[pid].included_dates)
                )
                for pid in issue.period_ids
            )
            if touches and dates:
                out.update(issue.inference_restrictions)
        return out

    def observation(self, statement, candidate, allowed, restrictions, path):
        roles = OBSERVATION_TEMPLATES[statement.template_id].fact_roles
        if len(statement.fact_ids) != len(roles):
            self.error(
                Code.WRONG_ROLE, path, detail="wrong number of ordered fact roles"
            )
            return
        fs = [self.fact(ref, path + ".fact_ids") for ref in statement.fact_ids]
        for ref, fact in zip(statement.fact_ids, fs, strict=True):
            if ref not in allowed:
                self.error(
                    Code.WRONG_ROLE,
                    path,
                    ref,
                    "fact was not supplied as candidate evidence",
                )
            if fact:
                self.usable(fact, path)
                if (
                    Restriction.NO_FUNNEL_INFERENCE in restrictions
                    and fact.metric in FUNNEL_METRICS
                ):
                    self.error(Code.QUALITY_GATE_VIOLATION, path, ref)
                if (
                    Restriction.NO_YIELD_ESTIMATE in restrictions
                    and fact.metric in MONEY_RATE_METRICS
                ):
                    self.error(Code.QUALITY_GATE_VIOLATION, path, ref)
        if any(f is None for f in fs):
            return
        template = statement.template_id
        if template is Template.METRIC_VALUE:
            self.scope(fs[0], candidate, path)
            if fs[0].kind is FactKind.COMPARISON:
                self.error(
                    Code.WRONG_ROLE,
                    path,
                    fs[0].fact_id,
                    "comparison needs explicit baseline roles",
                )
        elif template is Template.METRIC_VS_BASELINE:
            target, baseline, comp = fs
            self.scope(target, candidate, path)
            if target.period != baseline.period:
                self.error(Code.WRONG_PERIOD, path, baseline.fact_id)
            # B declares residual-parent / peer cohorts explicitly.
            if (
                target.segment.cohort_id != baseline.segment.cohort_id
                and baseline.segment.cohort_id != "excluding_" + candidate.segment_id
            ):
                self.error(Code.WRONG_SEGMENT, path, baseline.fact_id)
            if not comp.comparison or (
                comp.comparison.left_fact_id,
                comp.comparison.right_fact_id,
            ) != (target.fact_id, baseline.fact_id):
                self.error(
                    Code.WRONG_ROLE,
                    path,
                    comp.fact_id,
                    "comparison roles were swapped or substituted",
                )
            else:
                if (
                    Restriction.NO_MATCHED_PEER_LABEL in restrictions
                    and comp.comparison.matching_keys
                ):
                    self.error(Code.QUALITY_GATE_VIOLATION, path, comp.fact_id)
                self.comparison(comp, target, baseline, path)
        elif template is Template.TREND:
            earlier, later, change = fs
            for f in fs:
                self.scope(f, candidate, path, daily=True)
            if (
                earlier.metric is not later.metric
                or earlier.unit is not later.unit
                or change.metric is not Metric.DAILY_CHANGE
            ):
                self.error(Code.WRONG_METRIC, path)
            if earlier.period.end >= later.period.start:
                self.error(
                    Code.WRONG_PERIOD, path, detail="trend periods are not ordered"
                )
            if set(change.period.included_dates) != set(
                earlier.period.included_dates + later.period.included_dates
            ):
                self.error(Code.WRONG_PERIOD, path, change.fact_id)
            if change.provenance.parent_fact_ids != [earlier.fact_id, later.fact_id]:
                self.error(Code.WRONG_ROLE, path, change.fact_id)
            if earlier.value is None or earlier.value <= 0 or later.value is None:
                self.error(Code.INVALID_DENOMINATOR, path)
            elif change.value != (later.value - earlier.value) / earlier.value:
                self.error(Code.WRONG_DIRECTION, path, change.fact_id)
            if Restriction.NO_TREND_CLAIM in restrictions:
                self.error(Code.QUALITY_GATE_VIOLATION, path)
        elif template is Template.COVERAGE:
            observed, reference = fs
            self.scope(observed, candidate, path)
            if any(f.metric is not Metric.OBSERVED_DAYS for f in fs):
                self.error(Code.WRONG_METRIC, path)
            if reference.period.period_id != candidate.period_id:
                self.error(Code.WRONG_PERIOD, path, reference.fact_id)
            if (
                reference.segment.filters
                or reference.segment.cohort_id != observed.segment.cohort_id
            ):
                self.error(Code.WRONG_SEGMENT, path, reference.fact_id)
        elif template is Template.CONCENTRATION:
            self.scope(fs[0], candidate, path)
            if fs[0].metric not in CONCENTRATION:
                self.error(Code.WRONG_METRIC, path, fs[0].fact_id)

    def scenario(self, scenario, candidate, rec, path):
        self.namespace(scenario, path)
        if (
            scenario.segment_id != candidate.segment_id
            or scenario.period_id != candidate.period_id
        ):
            self.error(Code.SCENARIO_MISMATCH, path, scenario.scenario_id)
        if scenario.template is not SCENARIOS.get(
            candidate.family, ScenarioTemplate.NOT_IDENTIFIABLE
        ):
            self.error(Code.SCENARIO_MISMATCH, path, scenario.scenario_id)
        if not set(scenario.assumption_ids) <= set(rec.assumption_ids):
            self.error(
                Code.WRONG_ROLE,
                path,
                detail="scenario assumptions must be explicitly labelled",
            )
        if not set(scenario.prerequisite_codes) <= set(rec.prerequisite_codes):
            self.error(
                Code.SCENARIO_MISMATCH, path, detail="scenario prerequisites omitted"
            )
        if not set(scenario.limitation_codes) <= set(rec.risk_codes):
            self.error(
                Code.SCENARIO_MISMATCH, path, detail="scenario limitations omitted"
            )
        if not set(scenario.overlap_group_ids) <= set(rec.overlap_group_ids):
            self.error(Code.OVERLAP_CONFLICT, path)
        for ref in (*scenario.historical_fact_ids, *scenario.result_fact_ids):
            self.fact(ref, path)
        try:
            recomputed = evaluate_scenarios(
                self.b.facts,
                self.b.assumptions,
                [
                    ScenarioRequest(
                        template=scenario.template,
                        segment_id=scenario.segment_id,
                        period_id=scenario.period_id,
                        historical_fact_ids=scenario.historical_fact_ids,
                        assumption_ids=scenario.assumption_ids,
                    )
                ],
            )
            expected = recomputed.scenarios[scenario.scenario_id].model_copy(
                update={"overlap_group_ids": scenario.overlap_group_ids}
            )
            if expected != scenario or any(
                self.facts.get(f.fact_id) != f for f in recomputed.facts
            ):
                self.error(
                    Code.SCENARIO_MISMATCH,
                    path,
                    scenario.scenario_id,
                    "scenario differs from code-recomputed formula or safeguards",
                )
        except (KeyError, ValueError, ArithmeticError):
            self.error(Code.SCENARIO_MISMATCH, path, scenario.scenario_id)

    def analysis(self, output):
        candidate = self.b.candidates.get(output.candidate_id)
        rec = output.recommendation
        if candidate is None:
            self.error(Code.UNKNOWN_REFERENCE, "candidate_id", output.candidate_id)
            return None
        if output.status is not AnalysisStatus.COMPLETE or rec is None:
            self.error(Code.SCHEMA_INVALID, "status", detail="analysis is not complete")
            return None
        if (
            not 1 <= output.generation_turns <= 4
            or output.repairs > 1
            or len(output.tool_evidence) > 3
        ):
            self.error(
                Code.SCHEMA_INVALID,
                "budget",
                detail="analysis exceeds finite turn/repair/tool limits",
            )
        if rec.candidate_id != candidate.candidate_id:
            self.error(Code.WRONG_ROLE, "recommendation.candidate_id", rec.candidate_id)
        if rec.origin != candidate.origin:
            self.error(
                Code.WRONG_ROLE,
                "recommendation.origin",
                detail="candidate origin cannot be relabelled",
            )
        if (
            rec.segment_id != candidate.segment_id
            or rec.segment_id not in self.b.segments
        ):
            self.error(Code.WRONG_SEGMENT, "recommendation.segment_id", rec.segment_id)
        if rec.period_id != candidate.period_id or rec.period_id not in self.b.periods:
            self.error(Code.WRONG_PERIOD, "recommendation.period_id", rec.period_id)
        pack = self.b.packs.get(candidate.candidate_id)
        allowed = {f.fact_id for f in pack.facts} if pack else set()
        tool_segments = (
            {s.segment_id for s in pack.segments} if pack else {candidate.segment_id}
        )
        tool_periods = (
            {p.period_id for p in pack.periods} if pack else {candidate.period_id}
        )
        for tool in output.tool_evidence:
            self.namespace(tool, "tool_evidence")
            if (
                tool.tool_call.segment_id not in tool_segments
                or tool.tool_call.period_id not in tool_periods
                or pack is None
                or tool.tool_call.tool not in pack.tool_allowlist
            ):
                self.error(Code.WRONG_ROLE, "tool_evidence", tool.tool_invocation_id)
            for fact in tool.appended_facts:
                if self.facts.get(fact.fact_id) != fact:
                    self.error(
                        Code.UNKNOWN_REFERENCE,
                        "tool_evidence.appended_facts",
                        fact.fact_id,
                        "tool facts must be registered by deterministic code before validation",
                    )
            for ref in (
                *tool.returned_fact_ids,
                *tool.coverage_fact_ids,
                *tool.other_bucket_fact_ids,
            ):
                fact = self.fact(ref, "tool_evidence")
                allowed.add(ref)
                if fact is not None:
                    tool_segments.add(fact.segment.segment_id)
                    tool_periods.add(fact.period.period_id)
        restrictions = self.restrictions(candidate)
        if rec.decision is not Decision.REJECT:
            if (
                self.b.quality.has_fatal
                or Restriction.NO_RECOMMENDATIONS in restrictions
            ):
                self.error(Code.QUALITY_GATE_VIOLATION, "decision")
            if candidate.eligibility is EligibilityStatus.REJECTED:
                self.error(
                    Code.QUALITY_GATE_VIOLATION,
                    "decision",
                    detail="detector rejected this duplicate candidate",
                )
            if not rec.observation_statements:
                self.error(Code.UNSUPPORTED_CLAIM, "observation_statements")
            if not set(candidate.counter_evidence_fact_ids) <= set(
                rec.counter_evidence_fact_ids
            ):
                self.error(Code.MISSING_COUNTER_EVIDENCE, "counter_evidence_fact_ids")
            if rec.decision is Decision.RECOMMEND_TEST:
                blocked = restrictions & FAMILY_BLOCKERS[candidate.family]
                segment = self.b.segments.get(candidate.segment_id)
                blocked |= {
                    r
                    for r, d in DIMENSION_BLOCKERS.items()
                    if r in restrictions
                    and segment is not None
                    and d in segment.filters
                }
                if blocked or candidate.eligibility is EligibilityStatus.EXPLORATORY:
                    self.error(
                        Code.QUALITY_GATE_VIOLATION,
                        "decision",
                        detail="exploratory or restricted evidence permits investigation, not a scale/test claim",
                    )
        if rec.test_template_id is not None and (
            pack is None or rec.test_template_id not in pack.permitted_test_templates
        ):
            self.error(Code.UNAPPROVED_TEMPLATE, "test_template_id")
        for i, statement in enumerate(rec.observation_statements):
            self.observation(
                statement,
                candidate,
                allowed,
                restrictions,
                f"observation_statements[{i}]",
            )
        for ref in rec.counter_evidence_fact_ids:
            self.fact(ref, "counter_evidence_fact_ids")
            if ref not in allowed:
                self.error(Code.WRONG_ROLE, "counter_evidence_fact_ids", ref)
        if set(rec.overlap_group_ids) != set(candidate.overlap_group_ids):
            self.error(Code.OVERLAP_CONFLICT, "overlap_group_ids")
        for ref in rec.overlap_group_ids:
            group = self.b.overlap_groups.get(ref)
            if group is None or candidate.candidate_id not in group.candidate_ids:
                self.error(Code.UNKNOWN_REFERENCE, "overlap_group_ids", ref)
        permitted_assumptions = (
            {a.assumption_id for a in pack.assumptions} if pack else set()
        )
        for ref in rec.assumption_ids:
            a = self.b.assumptions.get(ref)
            if a is None:
                self.error(Code.UNKNOWN_REFERENCE, "assumption_ids", ref)
                continue
            self.namespace(a, "assumption_ids")
            approved = (
                next((p for p in pack.assumptions if p.assumption_id == ref), None)
                if pack
                else None
            )
            if approved != a:
                self.error(
                    Code.WRONG_UNIT,
                    "assumption_ids",
                    ref,
                    "assumption differs from its approved typed parameter",
                )
            if (
                ref not in permitted_assumptions
                or a.segment_id != candidate.segment_id
                or a.period_id != candidate.period_id
            ):
                self.error(Code.WRONG_ROLE, "assumption_ids", ref)
            if not a.bounds.minimum <= a.value <= a.bounds.maximum:
                self.error(Code.ASSUMPTION_OUT_OF_BOUNDS, "assumption_ids", ref)
            for dependency in a.dependency_ids:
                if dependency not in rec.assumption_ids:
                    self.error(
                        Code.UNKNOWN_REFERENCE,
                        "assumption_ids.dependencies",
                        dependency,
                    )
        permitted_scenarios = {s.scenario_id for s in pack.scenarios} if pack else set()
        selected_scenarios = []
        for ref in rec.scenario_ids:
            scenario = self.b.scenarios.get(ref)
            if scenario is None:
                self.error(Code.UNKNOWN_REFERENCE, "scenario_ids", ref)
            else:
                if ref not in permitted_scenarios:
                    self.error(Code.SCENARIO_MISMATCH, "scenario_ids", ref)
                self.scenario(scenario, candidate, rec, "scenario_ids")
                selected_scenarios.append(scenario)
        # One scoped experiment cannot add alternative/overlapping impacts.
        if len(selected_scenarios) > 1 or len(set(rec.scenario_ids)) != len(
            rec.scenario_ids
        ):
            self.error(Code.OVERLAP_CONFLICT, "scenario_ids")
        if rec.decision is Decision.RECOMMEND_TEST:
            required = {"test_allocation", "test_min_days", "test_profit_stop_loss"}
            actual = {
                a.parameter_key
                for ref in rec.assumption_ids
                if (a := self.b.assumptions.get(ref)) is not None
            }
            if not required <= actual:
                self.error(
                    Code.WRONG_ROLE,
                    "assumption_ids",
                    detail="test design parameters must be labelled assumptions",
                )
            if not {Metric.PROFIT, Metric.PROFIT_PER_MILLION_REQUESTS} <= set(
                rec.success_metric_keys
            ):
                self.error(
                    Code.UNSUPPORTED_CLAIM,
                    "success_metric_keys",
                    detail="account profit and unit-profit guardrails required",
                )
        if rec.decision is Decision.MERGE:
            # No new union scope exists in this IR: only exact-scope merges can
            # be proved; parent/child union and portfolio impacts are refused.
            if candidate.candidate_id in rec.merge_source_candidate_ids or len(
                set(rec.merge_source_candidate_ids)
            ) != len(rec.merge_source_candidate_ids):
                self.error(
                    Code.OVERLAP_CONFLICT,
                    "merge_source_candidate_ids",
                    detail="self/duplicate candidate merge",
                )
            for ref in rec.merge_source_candidate_ids:
                source = self.b.candidates.get(ref)
                if source is None:
                    self.error(
                        Code.UNKNOWN_REFERENCE, "merge_source_candidate_ids", ref
                    )
                elif (source.segment_id, source.period_id) != (
                    candidate.segment_id,
                    candidate.period_id,
                ):
                    self.error(Code.OVERLAP_CONFLICT, "merge_source_candidate_ids", ref)
                elif not set(source.counter_evidence_fact_ids) <= set(
                    rec.counter_evidence_fact_ids
                ):
                    self.error(
                        Code.MISSING_COUNTER_EVIDENCE, "merge_source_candidate_ids", ref
                    )
        return ValidatedRecommendation(
            recommendation_id=make_id(
                IdPrefix.RECOMMENDATION,
                self.b.run_namespace,
                rec.model_dump(mode="json"),
            ),
            run_namespace=self.b.run_namespace,
            recommendation=rec,
        )

    def ranking(self, output):
        # Reuse lane E's selection/merge policy as well as the typed registry
        # checks below. Direct validator callers cannot bypass non-overlap or
        # caveat preservation by skipping the ranking orchestrator.
        from llm.ranking import RankingError, check_ranking

        try:
            check_ranking(
                output, list(self.b.validated_recommendations.values()), self.b.facts
            )
        except RankingError:
            self.error(
                Code.OVERLAP_CONFLICT,
                "ranking",
                detail="selection violates validated ranking/merge policy",
            )
        ids = output.ranked_recommendation_ids
        if len(set(ids)) != len(ids):
            self.error(Code.OVERLAP_CONFLICT, "ranked_recommendation_ids")
        chosen = {}
        for ref in ids:
            validated = self.b.validated_recommendations.get(ref)
            if validated is None:
                self.error(Code.UNKNOWN_REFERENCE, "ranked_recommendation_ids", ref)
                continue
            self.namespace(validated, "ranked_recommendation_ids")
            if validated.recommendation.decision not in (
                Decision.RECOMMEND_TEST,
                Decision.INVESTIGATE_FIRST,
            ):
                self.error(Code.WRONG_ROLE, "ranked_recommendation_ids", ref)
            chosen[ref] = validated
        used = set()
        for validated in chosen.values():
            for ref in validated.recommendation.scenario_ids:
                if ref in used:
                    self.error(
                        Code.OVERLAP_CONFLICT,
                        "ranked_recommendation_ids",
                        ref,
                        "same conditional impact selected more than once",
                    )
                used.add(ref)
        for merge in output.merge_instructions:
            target = chosen.get(merge.target_recommendation_id)
            if target is None:
                self.error(
                    Code.UNKNOWN_REFERENCE,
                    "merge_instructions",
                    merge.target_recommendation_id,
                )
                continue
            t = target.recommendation
            if merge.target_recommendation_id in merge.source_recommendation_ids or len(
                set(merge.source_recommendation_ids)
            ) != len(merge.source_recommendation_ids):
                self.error(
                    Code.OVERLAP_CONFLICT,
                    "merge_instructions",
                    detail="self/duplicate merge",
                )
            for ref in merge.source_recommendation_ids:
                source = self.b.validated_recommendations.get(ref)
                if source is None:
                    self.error(Code.UNKNOWN_REFERENCE, "merge_instructions", ref)
                    continue
                s = source.recommendation
                if (
                    (s.segment_id, s.period_id) != (t.segment_id, t.period_id)
                    or s.scenario_ids
                    or t.scenario_ids
                ):
                    self.error(Code.OVERLAP_CONFLICT, "merge_instructions", ref)
                if not (
                    set(s.counter_evidence_fact_ids) <= set(t.counter_evidence_fact_ids)
                    and set(s.risk_codes) <= set(t.risk_codes)
                    and set(s.prerequisite_codes) <= set(t.prerequisite_codes)
                    and set(s.assumption_ids) <= set(t.assumption_ids)
                    and {
                        f
                        for statement in s.observation_statements
                        for f in statement.fact_ids
                    }
                    <= {
                        f
                        for statement in t.observation_statements
                        for f in statement.fact_ids
                    }
                ):
                    self.error(Code.MISSING_COUNTER_EVIDENCE, "merge_instructions", ref)
        for theme in output.summary_themes:
            supporting = set()
            for ref in theme.recommendation_ids:
                rec = chosen.get(ref)
                if rec is None:
                    self.error(
                        Code.UNKNOWN_REFERENCE, "summary_themes.recommendation_ids", ref
                    )
                else:
                    supporting.update(
                        f
                        for s in rec.recommendation.observation_statements
                        for f in s.fact_ids
                    )
                    supporting.update(rec.recommendation.counter_evidence_fact_ids)
            quality_ids = {f.fact_id for f in self.b.quality.facts}
            for ref in theme.fact_ids:
                fact = self.fact(ref, "summary_themes.fact_ids")
                if fact and ref not in supporting | quality_ids:
                    self.error(Code.WRONG_ROLE, "summary_themes.fact_ids", ref)


def validate_analysis(
    output: CandidateAnalysis | RankingIR, bundle: EvidenceBundle
) -> ValidationResult:
    """Reparse strict schemas, resolve references, verify roles, gates and formulas.

    Also accepts a JSON-like mapping for defensive callers; unknown fields,
    invented quantities, prose and invalid enum values are schema failures.
    """
    v = _Validator(bundle)
    try:
        raw = (
            output.model_dump(mode="json") if hasattr(output, "model_dump") else output
        )
        if len(canonical_json(raw).encode("utf-8")) > 1_048_576:
            raise ValueError("output exceeds validation size bound")
        is_ranking = isinstance(output, RankingIR) or (
            isinstance(raw, Mapping) and "ranked_recommendation_ids" in raw
        )
        parsed = (
            RankingIR.model_validate(raw)
            if is_ranking
            else CandidateAnalysis.model_validate(raw)
        )
    except (ValidationError, TypeError, ValueError, RecursionError):
        return ValidationRejected(
            errors=[
                ValidationIssue(
                    code=Code.SCHEMA_INVALID,
                    path="$",
                    detail="output violates the strict reference-only schema",
                )
            ]
        )
    if any(
        r.namespace != bundle.run_namespace
        for r in (bundle.facts, bundle.assumptions, bundle.scenarios)
    ):
        v.error(Code.STALE_NAMESPACE, "bundle")
    v.namespace(bundle.quality, "quality")
    if isinstance(parsed, RankingIR):
        v.ranking(parsed)
        accepted = parsed
    else:
        accepted = v.analysis(parsed)
    if v.errors:
        return ValidationRejected(errors=v.errors)
    if accepted is None:
        return ValidationRejected(
            errors=[
                ValidationIssue(
                    code=Code.SCHEMA_INVALID, path="$", detail="no accepted output"
                )
            ]
        )
    return ValidationAccepted(value=accepted)


def validate_rendered_report(
    report: RenderedReport,
    bundle: EvidenceBundle,
    *,
    metadata: Mapping[str, set[str]] | None = None,
) -> list[ValidationIssue]:
    """Strict post-render audit contract for factual/test/impact spans.

    Metadata exemptions are exact (source ID, rendered text) pairs supplied by
    trusted code, never whole-section or numeric-looking-label exemptions.
    Values must match code formatting, not merely sit under a known fact ID.
    The existing renderer's audit additionally covers its warning templates.
    """
    from report import text as wording
    from report.render import clean_label, format_value, period_label

    errors = []
    covered = bytearray(len(report.markdown))
    metadata = metadata or {}
    for span in report.spans:
        path = "spans"
        if not (0 <= span.start < span.end <= len(report.markdown)):
            errors.append(
                ValidationIssue(
                    code=Code.UNSUPPORTED_CLAIM, path=path, detail="invalid span bounds"
                )
            )
            continue
        text = report.markdown[span.start : span.end]
        allowed = set()
        f = bundle.facts.get(span.source_id)
        a = bundle.assumptions.get(span.source_id)
        if (
            f
            and span.role
            in (
                RenderRole.OBSERVATION,
                RenderRole.BASELINE,
                RenderRole.WARNING,
                RenderRole.SCENARIO,
            )
            and (f.kind is FactKind.SCENARIO) == (span.role is RenderRole.SCENARIO)
        ):
            allowed.update((f.fact_id, format_value(f.value, f.unit)))
            if f.comparison:
                allowed.add(format_value(f.comparison.ratio, Unit.RATIO))
                allowed.add(
                    format_value(f.comparison.difference, f.comparison.difference_unit)
                )
        if a and span.role is RenderRole.ASSUMPTION:
            allowed.update(
                (
                    a.assumption_id,
                    *(
                        format_value(x, a.unit)
                        for x in (a.value, a.bounds.minimum, a.bounds.maximum)
                    ),
                )
            )
        if span.role is RenderRole.METADATA:
            if (s := bundle.segments.get(span.source_id)) is not None:
                label = (
                    " · ".join(
                        f"{wording.DIMENSION_LABELS[d]} {clean_label(s.filters[d])}"
                        for d in wording.DIMENSION_LABELS
                        if d in s.filters
                    )
                    if s.filters
                    else wording.ACCOUNT_LABEL
                )
                if s.cohort_id != "full_input":
                    label += f" (cohort {clean_label(s.cohort_id)})"
                allowed.update((s.segment_id, label))
            if (p := bundle.periods.get(span.source_id)) is not None:
                allowed.update((p.period_id, period_label(p)))
            allowed.update(metadata.get(span.source_id, set()))
        if text not in allowed:
            errors.append(
                ValidationIssue(
                    code=Code.UNSUPPORTED_CLAIM,
                    path=path,
                    ref=span.source_id,
                    detail="span value or source role is not code-derived",
                )
            )
        else:
            covered[span.start : span.end] = b"\x01" * (span.end - span.start)
    if any(c.isdigit() and not covered[i] for i, c in enumerate(report.markdown)):
        errors.append(
            ValidationIssue(
                code=Code.UNSUPPORTED_CLAIM,
                path="markdown",
                detail="quantitative token lacks an exact sourced span",
            )
        )
    # Number words are not a loophole for future optional prose.
    unspanned = "".join(" " if covered[i] else c for i, c in enumerate(report.markdown))
    if re.search(
        r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million)\b",
        unspanned,
        re.IGNORECASE,
    ):
        errors.append(
            ValidationIssue(
                code=Code.UNSUPPORTED_CLAIM,
                path="markdown",
                detail="unregistered number word in prose",
            )
        )
    return errors
