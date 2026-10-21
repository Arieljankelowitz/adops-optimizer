"""Serializable contract models (pydantic v2).

Everything here round-trips through UTF-8 JSON and has an exported JSON
Schema under ``contracts/schemas/``. Models are frozen and reject unknown
fields, which also gives ``additionalProperties: false`` in the schemas the
model server is constrained by.

Quantities are ``Decimal`` in Python and decimal strings in JSON; floats are
rejected so no binary rounding enters a fact.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)

from contracts import SCHEMA_VERSION
from contracts.enums import (
    AnalysisStatus,
    AssumptionSelector,
    Confidence,
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
    ProgressStage,
    ProgressState,
    QualityPass,
    QualityRuleId,
    Relation,
    RenderRole,
    RuleOutcome,
    RunStatus,
    ScenarioTemplate,
    Severity,
    ToolName,
    Unit,
    ValidationErrorCode,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, id_pattern
from contracts.templates import (
    HypothesisCode,
    ObservationTemplateId,
    PrerequisiteCode,
    RiskCode,
    SummaryThemeCode,
    TestTemplateId,
)

DECIMAL_PATTERN = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("quantities must be decimal strings, not floats")
    return value


def _require_finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("quantities must be finite")
    return value


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    AfterValidator(_require_finite),
    PlainSerializer(lambda v: format(v, "f"), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_PATTERN}),
]


def _id(prefix: IdPrefix) -> Any:
    return Annotated[str, Field(pattern=id_pattern(prefix))]


NamespaceId = _id(IdPrefix.NAMESPACE)
FactId = _id(IdPrefix.FACT)
SegmentId = _id(IdPrefix.SEGMENT)
PeriodId = _id(IdPrefix.PERIOD)
CandidateId = _id(IdPrefix.CANDIDATE)
AssumptionId = _id(IdPrefix.ASSUMPTION)
ScenarioId = _id(IdPrefix.SCENARIO)
OverlapGroupId = _id(IdPrefix.OVERLAP_GROUP)
QualityIssueId = _id(IdPrefix.QUALITY_ISSUE)
RecommendationId = _id(IdPrefix.RECOMMENDATION)
ToolInvocationId = _id(IdPrefix.TOOL_INVOCATION)
PeerDefinitionId = _id(IdPrefix.PEER_DEFINITION)
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
SchemaVersion = Literal["1"]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Scope -----------------------------------------------------------------


class Segment(Contract):
    """An observed group. ``filters`` hold raw labels and are private data."""

    segment_id: SegmentId
    filters: dict[Dimension, str]
    cohort_id: str = Field(default="full_input", min_length=1, max_length=64)


class Period(Contract):
    period_id: PeriodId
    start: date
    end: date
    included_dates: list[date]
    coverage_status: CoverageStatus

    @model_validator(mode="after")
    def _dates_in_range(self) -> Self:
        if self.end < self.start:
            raise ValueError("period end precedes start")
        if any(d < self.start or d > self.end for d in self.included_dates):
            raise ValueError("included date outside period")
        return self


# --- Facts -----------------------------------------------------------------


class Validity(Contract):
    status: ValidityStatus
    reason: str | None = None

    @model_validator(mode="after")
    def _reason_when_not_valid(self) -> Self:
        if self.status is not ValidityStatus.VALID and not self.reason:
            raise ValueError("a non-valid fact needs a reason")
        return self


class Provenance(Contract):
    input_sha256: Sha256
    schema_version: SchemaVersion = SCHEMA_VERSION
    formula_id: str = Field(pattern=r"^[a-z0-9_]+_v[0-9]+$")
    source_columns: list[InputColumn]
    source_row_set_hash: Sha256
    # Opaque handle to private line ranges; never the ranges themselves.
    source_line_ranges_ref: str = Field(pattern=r"^private_rows_[0-9a-f]{16}$")
    parent_fact_ids: list[FactId]
    aggregation_keys: list[Dimension]
    weighting: Weighting
    quality_issue_ids: list[QualityIssueId]


class ComparisonDetail(Contract):
    """Required on ``comparison`` facts; computed by code, never the model."""

    left_fact_id: FactId
    right_fact_id: FactId
    matching_keys: list[Dimension]
    relation: Relation
    difference: DecimalValue | None
    ratio: DecimalValue | None
    difference_unit: Unit


class Fact(Contract):
    fact_id: FactId
    run_namespace: NamespaceId
    kind: FactKind
    segment: Segment
    period: Period
    metric: MetricKey
    value: DecimalValue | None
    unit: Unit
    validity: Validity
    provenance: Provenance
    comparison: ComparisonDetail | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.value is None and self.validity.status not in (
            ValidityStatus.UNDEFINED,
            ValidityStatus.INVALID,
        ):
            raise ValueError("null value is only allowed for undefined or invalid facts")
        if (self.kind is FactKind.COMPARISON) != (self.comparison is not None):
            raise ValueError("comparison detail is required exactly on comparison facts")
        return self


# --- Quality ---------------------------------------------------------------


class QualityIssue(Contract):
    issue_id: QualityIssueId
    rule_id: QualityRuleId
    rule_version: int = Field(ge=1)
    severity: Severity
    segment_ids: list[SegmentId]
    period_ids: list[PeriodId]
    measured_fact_ids: list[FactId]
    threshold_fact_ids: list[FactId]
    row_count: int = Field(ge=0)
    request_exposure_fact_id: FactId | None
    revenue_exposure_fact_id: FactId | None
    inference_restrictions: list[InferenceRestriction]
    message_template_id: str = Field(pattern=r"^DQ[0-9]{2}_[a-z0-9_]+$")
    affected_candidate_ids: list[CandidateId] = Field(default_factory=list)


class QualityRuleResult(Contract):
    rule_id: QualityRuleId
    rule_version: int = Field(ge=1)
    outcome: RuleOutcome
    issue_ids: list[QualityIssueId]


class QualityReport(Contract):
    schema_version: SchemaVersion = SCHEMA_VERSION
    run_namespace: NamespaceId
    quality_pass: QualityPass
    config_hash: Sha256
    rule_results: list[QualityRuleResult]
    issues: list[QualityIssue]
    # Provisional quality facts the issues reference; the evidence builder
    # promotes them into the immutable registry unchanged.
    facts: list[Fact] = Field(default_factory=list)

    @property
    def has_fatal(self) -> bool:
        return any(i.severity is Severity.FATAL for i in self.issues)


# --- Assumptions and scenarios ---------------------------------------------


class AssumptionBounds(Contract):
    minimum: DecimalValue
    maximum: DecimalValue

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("assumption bounds are inverted")
        return self


class Assumption(Contract):
    """A test parameter. Never historical evidence; rendered in its own list."""

    assumption_id: AssumptionId
    run_namespace: NamespaceId
    label: str = Field(min_length=1, max_length=120)
    parameter_key: str = Field(pattern=r"^[a-z0-9_]+$")
    value: DecimalValue
    unit: Unit
    bounds: AssumptionBounds
    rationale: str = Field(min_length=1, max_length=500)
    selector: AssumptionSelector
    segment_id: SegmentId
    period_id: PeriodId
    dependency_ids: list[AssumptionId]
    not_observed: Literal[True] = True

    @model_validator(mode="after")
    def _within_bounds(self) -> Self:
        if not self.bounds.minimum <= self.value <= self.bounds.maximum:
            raise ValueError("assumption value outside its approved bounds")
        return self


class Scenario(Contract):
    """Conditional impact computed by code; a null impact is a valid result."""

    scenario_id: ScenarioId
    run_namespace: NamespaceId
    template: ScenarioTemplate
    segment_id: SegmentId
    period_id: PeriodId
    formula_id: str = Field(pattern=r"^[a-z0-9_]+_v[0-9]+$")
    historical_fact_ids: list[FactId]
    assumption_ids: list[AssumptionId]
    result_fact_ids: list[FactId]
    horizon: Literal["observed_period_comparable_exposure"] = "observed_period_comparable_exposure"
    prerequisite_codes: list[PrerequisiteCode]
    displaced_cost_treatment: DisplacedCostTreatment
    overlap_group_ids: list[OverlapGroupId]
    revenue_at_risk_fact_id: FactId | None
    profit_at_risk_fact_id: FactId | None
    limitation_codes: list[RiskCode]
    conditional: Literal[True] = True


class ScenarioRequest(Contract):
    template: ScenarioTemplate
    segment_id: SegmentId
    period_id: PeriodId
    historical_fact_ids: list[FactId]
    assumption_ids: list[AssumptionId]


# --- Candidates and evidence packs -----------------------------------------


class OverlapGroup(Contract):
    overlap_group_id: OverlapGroupId
    candidate_ids: list[CandidateId] = Field(min_length=2)
    shared_request_fact_id: FactId


class Candidate(Contract):
    candidate_id: CandidateId
    family: DetectorFamily
    segment_id: SegmentId
    period_id: PeriodId
    eligibility: EligibilityStatus
    trigger_fact_ids: list[FactId] = Field(min_length=1)
    daily_fact_ids: list[FactId]
    peer_fact_ids: list[FactId]
    counter_evidence_fact_ids: list[FactId]
    quality_issue_ids: list[QualityIssueId]
    overlap_group_ids: list[OverlapGroupId]
    rejection_reason: str | None = None


class CandidatePack(Contract):
    """Bounded evidence sent to the model for one candidate. No raw rows."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    run_namespace: NamespaceId
    candidate: Candidate
    quality_issues: list[QualityIssue]
    segments: list[Segment]
    periods: list[Period]
    facts: list[Fact]
    assumptions: list[Assumption]
    scenarios: list[Scenario]
    permitted_test_templates: list[TestTemplateId]
    tool_allowlist: list[ToolName]
    token_budget: int = Field(gt=0)


# --- Model output IR -------------------------------------------------------


class ObservationStatement(Contract):
    template_id: ObservationTemplateId
    fact_ids: list[FactId] = Field(min_length=1, max_length=6)


class CandidateRecommendation(Contract):
    """The model's per-candidate output: references and codes only."""

    candidate_id: CandidateId
    decision: Decision
    segment_id: SegmentId
    period_id: PeriodId
    observation_statements: list[ObservationStatement] = Field(max_length=8)
    counter_evidence_fact_ids: list[FactId] = Field(max_length=12)
    hypothesis_codes: list[HypothesisCode] = Field(max_length=4)
    test_template_id: TestTemplateId | None
    assumption_ids: list[AssumptionId] = Field(max_length=8)
    scenario_ids: list[ScenarioId] = Field(max_length=4)
    prerequisite_codes: list[PrerequisiteCode] = Field(max_length=7)
    risk_codes: list[RiskCode] = Field(max_length=7)
    success_metric_keys: list[MetricKey] = Field(max_length=4)
    confidence_in_observation: Confidence
    overlap_group_ids: list[OverlapGroupId] = Field(max_length=8)
    merge_source_candidate_ids: list[CandidateId] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _decision_shape(self) -> Self:
        if (self.decision is Decision.MERGE) != bool(self.merge_source_candidate_ids):
            raise ValueError("merge source candidates are required exactly for a merge")
        if self.decision is Decision.RECOMMEND_TEST and self.test_template_id is None:
            raise ValueError("recommend_test needs a test template")
        return self


class BreakdownArgs(Contract):
    tool: Literal[ToolName.BREAKDOWN] = ToolName.BREAKDOWN
    segment_id: SegmentId
    by: Dimension
    period_id: PeriodId
    limit: int = Field(default=10, ge=1, le=25)


class DailyTrendArgs(Contract):
    tool: Literal[ToolName.DAILY_TREND] = ToolName.DAILY_TREND
    segment_id: SegmentId
    period_id: PeriodId


class CompareToPeersArgs(Contract):
    tool: Literal[ToolName.COMPARE_TO_PEERS] = ToolName.COMPARE_TO_PEERS
    segment_id: SegmentId
    period_id: PeriodId
    peer_definition_id: PeerDefinitionId


ToolCall = Annotated[
    BreakdownArgs | DailyTrendArgs | CompareToPeersArgs, Field(discriminator="tool")
]


class NextStep(Contract):
    """Parser-independent tool loop turn: call one tool or return a final answer."""

    kind: NextStepKind
    tool_call: ToolCall | None = None
    recommendation: CandidateRecommendation | None = None

    @model_validator(mode="after")
    def _one_payload(self) -> Self:
        if self.kind is NextStepKind.CALL_TOOL:
            ok = self.tool_call is not None and self.recommendation is None
        else:
            ok = self.recommendation is not None and self.tool_call is None
        if not ok:
            raise ValueError("next step must carry exactly the payload its kind names")
        return self


class MergeInstruction(Contract):
    target_recommendation_id: RecommendationId
    source_recommendation_ids: list[RecommendationId] = Field(min_length=1)


class SummaryTheme(Contract):
    theme_code: SummaryThemeCode
    fact_ids: list[FactId]
    recommendation_ids: list[RecommendationId]


class RankingIR(Contract):
    """Final ranking output; it cannot rewrite evidence."""

    ranked_recommendation_ids: list[RecommendationId] = Field(max_length=10)
    merge_instructions: list[MergeInstruction]
    summary_themes: list[SummaryTheme] = Field(max_length=7)


# --- Tools -----------------------------------------------------------------


class ToolEvidence(Contract):
    tool_invocation_id: ToolInvocationId
    run_namespace: NamespaceId
    tool_call: ToolCall
    tool_version: str = Field(min_length=1, max_length=32)
    coverage_fact_ids: list[FactId]
    returned_fact_ids: list[FactId]
    appended_facts: list[Fact]
    other_bucket_fact_ids: list[FactId]
    truncated: bool
    quality_issue_ids: list[QualityIssueId]


# --- Analysis and validation -----------------------------------------------


class CandidateAnalysis(Contract):
    candidate_id: CandidateId
    status: AnalysisStatus
    recommendation: CandidateRecommendation | None
    tool_evidence: list[ToolEvidence]
    generation_turns: int = Field(ge=0)
    repairs: int = Field(ge=0)


class ValidationIssue(Contract):
    code: ValidationErrorCode
    path: str = Field(max_length=200)
    ref: str | None = None
    detail: str = Field(max_length=300)


class ValidatedRecommendation(Contract):
    recommendation_id: RecommendationId
    run_namespace: NamespaceId
    recommendation: CandidateRecommendation


class ValidationAccepted(Contract):
    accepted: Literal[True] = True
    value: ValidatedRecommendation | RankingIR


class ValidationRejected(Contract):
    accepted: Literal[False] = False
    errors: list[ValidationIssue] = Field(min_length=1)


ValidationResult = Annotated[
    ValidationAccepted | ValidationRejected, Field(discriminator="accepted")
]


# --- Run, progress and report ----------------------------------------------


class RunManifest(Contract):
    """Links the private run bundle files by SHA-256. Contains no source path."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    run_id: str = Field(pattern=r"^run_[0-9a-f]{16}$")
    run_namespace: NamespaceId
    input_sha256: Sha256
    analytics_config_hash: Sha256
    model_profile_hash: Sha256 | None
    prompt_hash: Sha256 | None
    tool_version: str
    assumption_hash: Sha256
    generated_at: datetime
    files: dict[str, Sha256]


class ProgressEvent(Contract):
    run_id: str = Field(pattern=r"^run_[0-9a-f]{16}$")
    stage: ProgressStage
    state: ProgressState
    candidate_id: CandidateId | None = None
    message_code: str = Field(pattern=r"^[a-z0-9_]+$")
    at: datetime


class RenderSpan(Contract):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    source_id: str
    role: RenderRole


class RenderedReport(Contract):
    markdown: str
    spans: list[RenderSpan]


class RunArtifact(Contract):
    status: RunStatus
    manifest: RunManifest
    quality: QualityReport
    recommendations: list[ValidatedRecommendation]
    ranking: RankingIR | None
    # Names inside the private run directory, never absolute paths.
    private_artifact_names: list[str]
