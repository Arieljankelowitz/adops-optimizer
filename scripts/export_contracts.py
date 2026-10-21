"""Export versioned JSON Schemas and synthetic contract examples.

Schemas go to ``contracts/schemas/v<SCHEMA_VERSION>/`` and examples to
``examples/synthetic/``. Both are deterministic; a test fails when the
committed files drift from the models, so rerun this after a contract change:

    uv run python scripts/export_contracts.py
"""

import hashlib
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contracts import SCHEMA_VERSION  # noqa: E402
from contracts.config import RunConfig, config_hash  # noqa: E402
from contracts.enums import (  # noqa: E402
    AssumptionSelector,
    CoverageStatus,
    Decision,
    DetectorFamily,
    Dimension,
    DisplacedCostTreatment,
    EligibilityStatus,
    FactKind,
    InferenceRestriction,
    InputColumn,
    MetricKey,
    NextStepKind,
    QualityPass,
    QualityRuleId,
    Relation,
    RuleOutcome,
    ScenarioTemplate,
    Severity,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, make_id, make_namespace, stable_digest  # noqa: E402
from contracts.models import (  # noqa: E402
    Assumption,
    AssumptionBounds,
    Candidate,
    CandidatePack,
    CandidateRecommendation,
    ComparisonDetail,
    DailyTrendArgs,
    Fact,
    NextStep,
    ObservationStatement,
    Period,
    ProgressEvent,
    Provenance,
    QualityIssue,
    QualityReport,
    QualityRuleResult,
    RankingIR,
    RunManifest,
    Scenario,
    Segment,
    ToolEvidence,
    ValidationResult,
    Validity,
)
from contracts.templates import (  # noqa: E402
    HypothesisCode,
    ObservationTemplateId,
    PrerequisiteCode,
    RiskCode,
    TestTemplateId,
)

SCHEMA_DIR = REPO_ROOT / "contracts" / "schemas" / f"v{SCHEMA_VERSION}"
EXAMPLES_DIR = REPO_ROOT / "examples" / "synthetic"

SCHEMA_MODELS: dict[str, Any] = {
    "fact": Fact,
    "assumption": Assumption,
    "scenario": Scenario,
    "quality_report": QualityReport,
    "candidate": Candidate,
    "candidate_pack": CandidatePack,
    "candidate_recommendation": CandidateRecommendation,
    "next_step": NextStep,
    "ranking": RankingIR,
    "tool_evidence": ToolEvidence,
    "validation_result": ValidationResult,
    "run_manifest": RunManifest,
    "progress_event": ProgressEvent,
}


def schema_for(name: str) -> dict[str, Any]:
    target = SCHEMA_MODELS[name]
    adapter = TypeAdapter(target)
    schema = adapter.json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"adops-optimizer/contracts/v{SCHEMA_VERSION}/{name}.schema.json",
        "x-schema-version": SCHEMA_VERSION,
        **schema,
    }


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def build_examples() -> dict[str, BaseModel]:
    """A small, internally consistent example set with invented values."""
    input_sha = _sha("synthetic example input")
    cfg_hash = config_hash(RunConfig())
    ns = make_namespace(input_sha, cfg_hash, SCHEMA_VERSION)

    def fid(name: str) -> str:
        return make_id(IdPrefix.FACT, ns, name)

    account = Segment(segment_id=make_id(IdPrefix.SEGMENT, ns, {}), filters={})
    target = Segment(
        segment_id=make_id(IdPrefix.SEGMENT, ns, {"supply_id": "41022", "country": "DE"}),
        filters={Dimension.SUPPLY_ID: "41022", Dimension.COUNTRY: "DE"},
    )
    days = [date(2031, 3, d) for d in range(3, 10)]
    period = Period(
        period_id=make_id(IdPrefix.PERIOD, ns, [d.isoformat() for d in days]),
        start=days[0],
        end=days[-1],
        included_dates=days,
        coverage_status=CoverageStatus.POSSIBLY_INCOMPLETE,
    )
    rows_hash = _sha("synthetic example row set")
    rows_ref = f"private_rows_{stable_digest('synthetic example row set')}"

    def provenance(formula: str, columns: list[InputColumn], parents: list[str],
                   keys: list[Dimension], weighting: Weighting,
                   issues: list[str] | None = None) -> Provenance:
        return Provenance(
            input_sha256=input_sha,
            formula_id=formula,
            source_columns=columns,
            source_row_set_hash=rows_hash,
            source_line_ranges_ref=rows_ref,
            parent_fact_ids=parents,
            aggregation_keys=keys,
            weighting=weighting,
            quality_issue_ids=issues or [],
        )

    keys = [Dimension.SUPPLY_ID, Dimension.COUNTRY]
    partial_day_issue = make_id(IdPrefix.QUALITY_ISSUE, ns, "DQ06 example")
    valid = Validity(status=ValidityStatus.VALID)

    revenue = Fact(
        fact_id=fid("target revenue"), run_namespace=ns, kind=FactKind.MEASURED,
        segment=target, period=period, metric=MetricKey.REVENUE, value=Decimal("6250.40"),
        unit=Unit.EXPORT_DOLLAR, validity=valid,
        provenance=provenance("sum_revenue_v1", [InputColumn.REVENUE], [], keys, Weighting.SUM),
    )
    requests = Fact(
        fact_id=fid("target requests"), run_namespace=ns, kind=FactKind.MEASURED,
        segment=target, period=period, metric=MetricKey.REQUESTS, value=Decimal("11200000"),
        unit=Unit.COUNT, validity=valid,
        provenance=provenance("sum_requests_v1", [InputColumn.DEMAND_REQUESTS], [], keys,
                              Weighting.SUM),
    )
    target_yield = Fact(
        fact_id=fid("target revenue per million"), run_namespace=ns, kind=FactKind.CALCULATED,
        segment=target, period=period, metric=MetricKey.REVENUE_PER_MILLION_REQUESTS,
        value=Decimal("558.07"), unit=Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS, validity=valid,
        provenance=provenance(
            "revenue_per_million_requests_v1",
            [InputColumn.REVENUE, InputColumn.DEMAND_REQUESTS],
            [revenue.fact_id, requests.fact_id], keys, Weighting.RATIO_OF_SUMS,
            [partial_day_issue],
        ),
    )
    baseline_yield = Fact(
        fact_id=fid("account revenue per million"), run_namespace=ns, kind=FactKind.CALCULATED,
        segment=account, period=period, metric=MetricKey.REVENUE_PER_MILLION_REQUESTS,
        value=Decimal("61.30"), unit=Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS, validity=valid,
        provenance=provenance(
            "revenue_per_million_requests_v1",
            [InputColumn.REVENUE, InputColumn.DEMAND_REQUESTS],
            [fid("account revenue"), fid("account requests")], [], Weighting.RATIO_OF_SUMS,
        ),
    )
    comparison = Fact(
        fact_id=fid("target vs account yield"), run_namespace=ns, kind=FactKind.COMPARISON,
        segment=target, period=period, metric=MetricKey.REVENUE_PER_MILLION_REQUESTS,
        value=Decimal("9.10"), unit=Unit.RATIO, validity=valid,
        provenance=provenance(
            "ratio_comparison_v1", [], [target_yield.fact_id, baseline_yield.fact_id], keys,
            Weighting.NOT_APPLICABLE,
        ),
        comparison=ComparisonDetail(
            left_fact_id=target_yield.fact_id, right_fact_id=baseline_yield.fact_id,
            matching_keys=[], relation=Relation.GREATER, difference=Decimal("496.77"),
            ratio=Decimal("9.10"), difference_unit=Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS,
        ),
    )
    undefined_rate = Fact(
        fact_id=fid("target response rate"), run_namespace=ns, kind=FactKind.CALCULATED,
        segment=target, period=period, metric=MetricKey.RESPONSE_RATE, value=None,
        unit=Unit.FRACTION,
        validity=Validity(status=ValidityStatus.INVALID,
                          reason="counter relationships inconsistent in contributing rows"),
        provenance=provenance(
            "response_rate_v1", [InputColumn.DEMAND_RESPONSES, InputColumn.DEMAND_REQUESTS],
            [fid("target responses"), requests.fact_id], keys, Weighting.RATIO_OF_SUMS,
        ),
    )
    facts = [revenue, requests, target_yield, baseline_yield, comparison, undefined_rate]

    issue = QualityIssue(
        issue_id=partial_day_issue, rule_id=QualityRuleId.DQ06, rule_version=1,
        severity=Severity.WARNING, segment_ids=[account.segment_id],
        period_ids=[period.period_id], measured_fact_ids=[fid("final day request ratio")],
        threshold_fact_ids=[fid("edge day threshold")], row_count=38,
        request_exposure_fact_id=fid("final day request share"),
        revenue_exposure_fact_id=fid("final day revenue share"),
        inference_restrictions=[InferenceRestriction.NO_TREND_CLAIM],
        message_template_id="DQ06_possible_partial_day",
    )
    quality = QualityReport(
        run_namespace=ns, quality_pass=QualityPass.INITIAL, config_hash=cfg_hash,
        rule_results=[
            QualityRuleResult(rule_id=QualityRuleId.DQ06, rule_version=1,
                              outcome=RuleOutcome.TRIGGERED, issue_ids=[issue.issue_id]),
            QualityRuleResult(rule_id=QualityRuleId.DQ08, rule_version=1,
                              outcome=RuleOutcome.NOT_TESTABLE, issue_ids=[]),
        ],
        issues=[issue],
    )
    fraction = Assumption(
        assumption_id=make_id(IdPrefix.ASSUMPTION, ns, "additional fraction"),
        run_namespace=ns, label="Additional eligible request fraction",
        parameter_key="additional_request_fraction", value=Decimal("0.10"), unit=Unit.FRACTION,
        bounds=AssumptionBounds(minimum=Decimal("0.01"), maximum=Decimal("0.50")),
        rationale="Planning default for a small, reversible allocation test.",
        selector=AssumptionSelector.DEFAULT_TEST_TEMPLATE, segment_id=target.segment_id,
        period_id=period.period_id, dependency_ids=[],
    )
    scenario = Scenario(
        scenario_id=make_id(IdPrefix.SCENARIO, ns, "additional requests"), run_namespace=ns,
        template=ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS, segment_id=target.segment_id,
        period_id=period.period_id, formula_id="additional_eligible_requests_v1",
        historical_fact_ids=[requests.fact_id, target_yield.fact_id],
        assumption_ids=[fraction.assumption_id],
        result_fact_ids=[fid("scenario conditional revenue")],
        prerequisite_codes=[PrerequisiteCode.CONFIRM_ELIGIBLE_INVENTORY,
                            PrerequisiteCode.CONFIRM_CAPACITY],
        displaced_cost_treatment=DisplacedCostTreatment.UNKNOWN_DISCLOSED,
        overlap_group_ids=[], revenue_at_risk_fact_id=None, profit_at_risk_fact_id=None,
        limitation_codes=[RiskCode.DEMAND_SATURATION, RiskCode.DISPLACED_PROFIT_UNKNOWN],
    )
    candidate = Candidate(
        candidate_id=make_id(IdPrefix.CANDIDATE, ns, "scale example"),
        family=DetectorFamily.SCALE_WINNER, segment_id=target.segment_id,
        period_id=period.period_id, eligibility=EligibilityStatus.ELIGIBLE,
        trigger_fact_ids=[comparison.fact_id], daily_fact_ids=[], peer_fact_ids=[],
        counter_evidence_fact_ids=[], quality_issue_ids=[issue.issue_id], overlap_group_ids=[],
    )
    recommendation = CandidateRecommendation(
        candidate_id=candidate.candidate_id, decision=Decision.RECOMMEND_TEST,
        segment_id=target.segment_id, period_id=period.period_id,
        observation_statements=[ObservationStatement(
            template_id=ObservationTemplateId.METRIC_VS_BASELINE,
            fact_ids=[target_yield.fact_id, baseline_yield.fact_id, comparison.fact_id],
        )],
        counter_evidence_fact_ids=[], hypothesis_codes=[
            HypothesisCode.TRAFFIC_MIX_OR_CONFIGURATION_UNVERIFIED],
        test_template_id=TestTemplateId.MATCHED_ALLOCATION_HOLDOUT,
        assumption_ids=[fraction.assumption_id], scenario_ids=[scenario.scenario_id],
        prerequisite_codes=[PrerequisiteCode.CONFIRM_ELIGIBLE_INVENTORY],
        risk_codes=[RiskCode.DEMAND_SATURATION],
        success_metric_keys=[MetricKey.PROFIT_PER_MILLION_REQUESTS, MetricKey.PROFIT],
        confidence_in_observation="medium", overlap_group_ids=[],
    )
    tool_step = NextStep(
        kind=NextStepKind.CALL_TOOL,
        tool_call=DailyTrendArgs(segment_id=target.segment_id, period_id=period.period_id),
    )
    return {
        "facts": _Listing(items=facts),
        "quality_report": quality,
        "assumption": fraction,
        "scenario": scenario,
        "candidate": candidate,
        "candidate_recommendation": recommendation,
        "next_step_tool_call": tool_step,
    }


class _Listing(BaseModel):
    items: list[Fact]


def dump(model: BaseModel) -> str:
    payload = model.model_dump(mode="json")
    if isinstance(model, _Listing):
        payload = payload["items"]
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def rendered_files() -> dict[Path, str]:
    files = {
        SCHEMA_DIR / f"{name}.schema.json": json.dumps(schema_for(name), indent=2, sort_keys=True)
        + "\n"
        for name in SCHEMA_MODELS
    }
    for name, model in build_examples().items():
        files[EXAMPLES_DIR / f"{name}.example.json"] = dump(model)
    return files


def main() -> int:
    for path, text in rendered_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
