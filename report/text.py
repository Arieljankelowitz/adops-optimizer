"""Reviewed wording the renderer composes reports from.

Nothing here contains a digit: every quantity in a report comes from a typed
registry object and is recorded as an audited span. A unit test enforces the
no-digit rule so static prose can never carry an unaudited number.
"""

from dataclasses import dataclass

from contracts.enums import (
    AnalysisStatus,
    Confidence,
    CoverageStatus,
    DetectorFamily,
    Dimension,
    DisplacedCostTreatment,
    EligibilityStatus,
    InferenceRestriction,
    MetricKey,
    ProgressStage,
    QualityRuleId,
    Relation,
    RunStatus,
    ScenarioTemplate,
    Severity,
    ValidationErrorCode,
    ValidityStatus,
)
from contracts.templates import (
    HypothesisCode,
    PrerequisiteCode,
    RiskCode,
    SummaryThemeCode,
    TestTemplateId,
)

REPORT_TITLE = "Ad-performance optimization report"
CURRENCY_NOTE = (
    "Money is in export dollars ($, currency code not supplied); no conversion is applied."
)

METRIC_LABELS: dict[MetricKey, str] = {
    MetricKey.REQUESTS: "requests",
    MetricKey.RESPONSES: "responses",
    MetricKey.WINS: "wins",
    MetricKey.IMPRESSIONS: "impressions",
    MetricKey.REVENUE: "revenue",
    MetricKey.COST: "cost",
    MetricKey.PROFIT: "reported profit",
    MetricKey.RESPONSE_RATE: "response rate",
    MetricKey.WIN_RATE: "win rate",
    MetricKey.IMPRESSION_TO_WIN_RATE: "impressions per win",
    MetricKey.ECPM: "eCPM",
    MetricKey.REVENUE_PER_MILLION_REQUESTS: "revenue per million requests",
    MetricKey.PROFIT_PER_MILLION_REQUESTS: "profit per million requests",
    MetricKey.MARGIN: "margin",
    MetricKey.REQUEST_SHARE: "share of account requests",
    MetricKey.REVENUE_SHARE: "share of account revenue",
    MetricKey.PROFIT_SHARE: "share of account profit",
    MetricKey.REQUEST_WEIGHTED_FLOOR: "request-weighted average floor",
    MetricKey.RESPONSE_WEIGHTED_BID: "response-weighted average bid",
    MetricKey.RESPONSE_WEIGHTED_FLOOR: "response-weighted average floor",
    MetricKey.DAILY_CHANGE: "day-over-day change",
    MetricKey.PROFIT_RECONCILIATION_RESIDUAL: "profit reconciliation residual",
    MetricKey.ROW_COUNT: "rows",
    MetricKey.OBSERVED_DAYS: "observed days",
    MetricKey.RECONCILIATION_BOUND: "reconciliation bound",
    MetricKey.ROW_SHARE: "share of rows",
    MetricKey.IMPRESSION_SHARE: "share of account impressions",
    MetricKey.REVENUE_POSITIVE_DAYS: "revenue-positive days",
    MetricKey.MISSING_DAYS: "missing days",
    MetricKey.DAY_REQUEST_RATIO: "day requests versus the median of other days",
    MetricKey.DAY_REVENUE_RATIO: "day revenue versus the median of other days",
    MetricKey.SLICE_DROP_SHARE: "share of material slices that dropped",
    MetricKey.ADJACENT_DAY_YIELD_RATIO: "adjacent-day yield ratio",
    MetricKey.MAX_DAY_REVENUE_SHARE: "largest single-day share of positive revenue",
    MetricKey.TOP_TWO_DAY_REVENUE_SHARE: "largest two-day share of positive revenue",
    MetricKey.MAX_DAY_PROFIT_SHARE: "largest single-day share of positive profit",
    MetricKey.TOP_TWO_DAY_PROFIT_SHARE: "largest two-day share of positive profit",
    MetricKey.SRPM_MATCH_SHARE: "traffic share matching the per-million sRPM unit",
    MetricKey.SRPM_ERROR_RATIO: "sRPM reconstruction error ratio",
    MetricKey.ECPM_MISMATCH_SHARE: "impression share with eCPM mismatch",
    MetricKey.COUNTER_GRANULARITY: "common counter granularity",
    MetricKey.GRANULARITY_COVERAGE: "share of rows on the common granularity",
    MetricKey.ZERO_BID_RESPONSE_SHARE: "response share with zero bid price",
    MetricKey.BID_BELOW_FLOOR_RESPONSE_SHARE: "response share with average bid below floor",
    MetricKey.ZERO_FLOOR_REQUEST_SHARE: "request share with zero floor",
}

DIMENSION_LABELS: dict[Dimension, str] = {
    Dimension.SUPPLY_ID: "Supply",
    Dimension.COUNTRY: "Country",
    Dimension.SIZE: "Size",
    Dimension.PLACEMENT_TYPE: "Placement",
    Dimension.DEVICE_TYPE: "Device",
}

ACCOUNT_LABEL = "Whole account"

RELATION_WORDS: dict[Relation, str] = {
    Relation.GREATER: "higher than",
    Relation.LESS: "lower than",
    Relation.EQUAL: "equal to",
    Relation.UNDEFINED: "not comparable with",
}

COVERAGE_TEXT: dict[CoverageStatus, str] = {
    CoverageStatus.UNVERIFIED: "completeness unverified",
    CoverageStatus.POSSIBLY_INCOMPLETE: "possibly incomplete",
    CoverageStatus.INSUFFICIENT_DATA: "too few days to judge completeness",
    CoverageStatus.EXTERNALLY_VERIFIED: "completeness externally verified",
}

VALIDITY_TEXT: dict[ValidityStatus, str] = {
    ValidityStatus.VALID: "valid",
    ValidityStatus.UNDEFINED: "undefined",
    ValidityStatus.INVALID: "invalid",
    ValidityStatus.TAINTED: "tainted by anomalous contributors; no clean claim",
}

SEVERITY_LABELS: dict[Severity, str] = {
    Severity.FATAL: "Fatal",
    Severity.WARNING: "Warning",
    Severity.NOTICE: "Notice",
}


@dataclass(frozen=True)
class RuleText:
    title: str
    consequence: str


# Generic consequences from the quality specification; quantities are listed
# beside them from the issue's own facts rather than interpolated.
QUALITY_RULES: dict[QualityRuleId, RuleText] = {
    QualityRuleId.DQ01: RuleText(
        "Required fields could not be interpreted",
        "Analysis stopped for the affected input. No values were replaced with zero.",
    ),
    QualityRuleId.DQ02: RuleText(
        "Unknown or colliding dimension values",
        "Unknown dimension values are not a targetable market or format.",
    ),
    QualityRuleId.DQ03: RuleText(
        "Duplicate daily segment keys",
        "Totals may be double-counted. Resolve the export grain before recommendations.",
    ),
    QualityRuleId.DQ04: RuleText(
        "Inconsistent counter relationships",
        "Affected funnel rates are undefined or unreliable; monetary totals are retained.",
    ),
    QualityRuleId.DQ05: RuleText(
        "Money without a usable traffic denominator",
        "Yield estimates for those rows are unavailable; no requests or impressions were invented.",
    ),
    QualityRuleId.DQ06: RuleText(
        "Possible incomplete day or broad traffic change",
        "Daily data cannot distinguish extraction coverage from real traffic movement. Full "
        "totals are retained; trend and scale conclusions need a sensitivity view.",
    ),
    QualityRuleId.DQ07: RuleText(
        "Intermittent daily coverage",
        "Absence is not confirmed zero traffic. Treat scaling as exploratory.",
    ),
    QualityRuleId.DQ08: RuleText(
        "Concentrated earnings",
        "Period averages may reflect a one-off spike, not repeatable yield.",
    ),
    QualityRuleId.DQ09: RuleText(
        "Large daily change",
        "Check traffic mix, configuration and completeness before extrapolating.",
    ),
    QualityRuleId.DQ10: RuleText(
        "Placeholder or unverified country labels",
        "Do not interpret them as a country opportunity.",
    ),
    QualityRuleId.DQ11: RuleText(
        "Unusual or placeholder size",
        "Confirm the format mapping before a format-specific test.",
    ),
    QualityRuleId.DQ12: RuleText(
        "Profit reconciliation",
        "Reported profit is preserved; cent-level rounding is not a margin leak.",
    ),
    QualityRuleId.DQ13: RuleText(
        "sRPM unit check",
        "Revenue per million requests is recomputed from revenue and requests; the exported "
        "sRPM is trusted only when its unit is confirmed.",
    ),
    QualityRuleId.DQ14: RuleText(
        "Exported eCPM consistency",
        "Accounting totals use Revenue, never values reconstructed from rounded rates.",
    ),
    QualityRuleId.DQ15: RuleText(
        "Rows are not traffic",
        "Rates and opportunity sizing use traffic-weighted totals, not row counts.",
    ),
    QualityRuleId.DQ16: RuleText(
        "Common request granularity",
        "Export scaling or counter definitions need confirmation. Counts were not corrected.",
    ),
    QualityRuleId.DQ17: RuleText(
        "Bid and floor field semantics",
        "Average bid and floor fields are not auction-level distributions and cannot prove "
        "the optimal floor.",
    ),
    QualityRuleId.DQ18: RuleText(
        "Signed adjustments or cent-rounded zero revenue",
        "Separate adjustments and rounding from true monetization failure.",
    ),
    QualityRuleId.DQ19: RuleText(
        "Exploratory or threshold-sensitive candidate",
        "High rates alone do not establish a material, stable opportunity.",
    ),
    QualityRuleId.DQ20: RuleText(
        "Observational daily aggregates",
        "Floor, rendering and allocation effects are unproven; controlled tests and additional "
        "operational data are required.",
    ),
}

RESTRICTION_TEXT: dict[InferenceRestriction, str] = {
    InferenceRestriction.NO_RECOMMENDATIONS: "no recommendations",
    InferenceRestriction.NO_FUNNEL_INFERENCE: "no funnel inference",
    InferenceRestriction.NO_YIELD_ESTIMATE: "no yield estimate",
    InferenceRestriction.NO_STABLE_SCALE_CLAIM: "no stable-scale claim",
    InferenceRestriction.NO_TREND_CLAIM: "no trend claim",
    InferenceRestriction.NO_MATCHED_PEER_LABEL: "no matched-peer label",
    InferenceRestriction.NO_COUNTRY_OPPORTUNITY: "no country opportunity",
    InferenceRestriction.NO_FORMAT_SPECIFIC_TEST: "no format-specific test",
    InferenceRestriction.NO_FLOOR_CAUSAL_CLAIM: "no causal floor claim",
    InferenceRestriction.EXPORTED_RATE_UNTRUSTED: "exported rate not trusted",
}

FAMILY_LABELS: dict[DetectorFamily, str] = {
    DetectorFamily.SCALE_WINNER: "Scale candidate",
    DetectorFamily.WASTED_REQUESTS: "Low-yield requests",
    DetectorFamily.MARGIN_LEAK: "Margin review",
    DetectorFamily.FLOOR_HIGH_BID: "Floor investigation (bids well above floor)",
    DetectorFamily.FLOOR_LOW_BID: "Floor investigation (bids below floor)",
    DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: "Wins and impressions discrepancy",
    DetectorFamily.TREND: "Daily trend",
    DetectorFamily.COMBINATION_OUTLIER: "Combination outlier",
}

# Family-specific limits rendered under "What it does not prove".
FAMILY_LIMITS: dict[DetectorFamily, str] = {
    DetectorFamily.SCALE_WINNER: "A high observed yield does not show that more comparable "
    "inventory exists, that demand would absorb it, or that requests transfer across "
    "country, size or placement.",
    DetectorFamily.WASTED_REQUESTS: "Low revenue per request does not show that throttling "
    "saves money or that freed capacity can be redirected profitably.",
    DetectorFamily.MARGIN_LEAK: "A low or negative margin can come from rounding, adjustments "
    "or commercial terms; it does not show that margin can be raised.",
    DetectorFamily.FLOOR_HIGH_BID: "Daily average bids and floors are not auction-level "
    "distributions; a higher floor's effect is not identifiable from them.",
    DetectorFamily.FLOOR_LOW_BID: "Successful traffic with average bids below the average floor "
    "is counter-evidence, not proof of rejected bids.",
    DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: "A low impressions-per-win ratio may be a "
    "counting lag or definition difference; lost impressions are not recoverable revenue.",
    DetectorFamily.TREND: "A daily step change can reflect traffic mix, configuration or "
    "incomplete days; one period cannot establish seasonality.",
    DetectorFamily.COMBINATION_OUTLIER: "A combination that differs from its parent may differ "
    "in dimensions the partner cannot control.",
}

GENERAL_LIMIT = (
    "These are observational daily aggregates; a daily correlation does not establish cause."
)


@dataclass(frozen=True)
class TestText:
    __test__ = False  # not a pytest test class

    title: str
    design: str
    randomization_unit: str
    treatment_and_holdout: str
    rollout: str
    primary_metric: str
    guardrails: str


_PROFIT_JUDGEMENT = (
    "Incremental total account profit between treatment and holdout on equivalent "
    "eligible exposure, adjusted for unequal allocation."
)
_STANDARD_GUARDRAILS = (
    "Profit per million requests, revenue, impressions, response and request volume, and "
    "account-level displacement; stop early if the stop-loss assumption is breached. Do not "
    "declare success merely because the calendar duration elapsed."
)

TEST_TEMPLATES: dict[TestTemplateId, TestText] = {
    TestTemplateId.MATCHED_ALLOCATION_HOLDOUT: TestText(
        title="Matched allocation test with holdout",
        design="Allocate additional eligible, comparable inventory to the target slice for a "
        "treatment share while a concurrent matched holdout keeps current allocation.",
        randomization_unit="Eligible request slot or supply cluster, matched on country, size, "
        "device and placement; never arbitrary export rows.",
        treatment_and_holdout="Treatment share from the allocation assumption; the remainder is "
        "a concurrent holdout.",
        rollout="Reversible: revert allocation immediately if a guardrail is breached.",
        primary_metric=_PROFIT_JUDGEMENT,
        guardrails=_STANDARD_GUARDRAILS,
    ),
    TestTemplateId.TARGETED_THROTTLE_HOLDOUT: TestText(
        title="Targeted throttle test with holdout",
        design="Throttle a randomized share of the low-yield requests while a concurrent "
        "holdout keeps sending them.",
        randomization_unit="Eligible request slot within the target slice.",
        treatment_and_holdout="Throttled share from the throttle assumption; the remainder is "
        "a concurrent holdout.",
        rollout="Reversible: restore traffic immediately if revenue or profit at risk "
        "materializes or a contract constraint applies.",
        primary_metric=_PROFIT_JUDGEMENT,
        guardrails=_STANDARD_GUARDRAILS,
    ),
    TestTemplateId.FLOOR_INCREASE_RANDOMIZED: TestText(
        title="Randomized small floor increase",
        design="After feasibility checks, raise the floor slightly for a randomized share of "
        "eligible requests and compare with a concurrent holdout at the current floor.",
        randomization_unit="Eligible request slot within the target slice.",
        treatment_and_holdout="Treatment share from the allocation assumption; the remainder "
        "keeps the current floor.",
        rollout="Reversible: restore the previous floor immediately on a guardrail breach.",
        primary_metric=_PROFIT_JUDGEMENT,
        guardrails=_STANDARD_GUARDRAILS,
    ),
    TestTemplateId.FLOOR_DECREASE_RANDOMIZED: TestText(
        title="Randomized small floor decrease",
        design="After confirming field semantics, lower the floor slightly for a randomized "
        "share of eligible requests with profit guardrails and a concurrent holdout.",
        randomization_unit="Eligible request slot within the target slice.",
        treatment_and_holdout="Treatment share from the allocation assumption; the remainder "
        "keeps the current floor.",
        rollout="Reversible: restore the previous floor immediately on a guardrail breach.",
        primary_metric=_PROFIT_JUDGEMENT,
        guardrails=_STANDARD_GUARDRAILS,
    ),
    TestTemplateId.COMMERCIAL_TERMS_REVIEW: TestText(
        title="Commercial terms review",
        design="Check rounding, adjustments and contract terms for the slice before any "
        "negotiation; treat a margin change as a commercial hypothesis.",
        randomization_unit="Not randomized; a review of terms and adjustments.",
        treatment_and_holdout="No treatment until the review confirms a negotiable term.",
        rollout="No production change is made by the review itself.",
        primary_metric="Confirmed explanation of the margin difference.",
        guardrails="Partner relationship and contract obligations.",
    ),
    TestTemplateId.COUNTER_RECONCILIATION: TestText(
        title="Counter reconciliation",
        design="Reconcile wins, impressions and requests against delivery logs, including "
        "reporting lag, before considering any technical fix.",
        randomization_unit="Not randomized; a log reconciliation.",
        treatment_and_holdout="No treatment until a defect is verified.",
        rollout="No production change is made by the reconciliation itself.",
        primary_metric="Verified count definitions and any confirmed defect.",
        guardrails="Only a verified defect supports a later technical-fix test.",
    ),
    TestTemplateId.INSTRUMENTATION_FEASIBILITY: TestText(
        title="Instrumentation and feasibility check",
        design="Establish the operational data, comparable inventory and randomization "
        "mechanism a controlled test would need.",
        randomization_unit="Not randomized; defines the unit a later test would use.",
        treatment_and_holdout="No treatment yet.",
        rollout="No production change is made by the check itself.",
        primary_metric="Whether a controlled test is feasible and what it would measure.",
        guardrails="No causal success forecast before a feasible test exists.",
    ),
}

HYPOTHESES: dict[HypothesisCode, str] = {
    HypothesisCode.TRAFFIC_MIX_OR_CONFIGURATION_UNVERIFIED: "The pattern may come from traffic "
    "mix or configuration differences.",
    HypothesisCode.DEMAND_QUALITY_OR_MIX_UNVERIFIED: "The pattern may come from demand quality "
    "or demand mix.",
    HypothesisCode.FLOOR_SETTING_UNVERIFIED: "The floor setting may be constraining or "
    "under-pricing this traffic.",
    HypothesisCode.RENDERING_OR_COUNTING_DEFECT_UNVERIFIED: "A rendering or counting defect may "
    "separate wins from impressions.",
    HypothesisCode.COMMERCIAL_TERMS_UNVERIFIED: "Commercial terms may explain the margin.",
    HypothesisCode.EXPORT_SEMANTICS_UNVERIFIED: "Export field semantics may explain the pattern.",
    HypothesisCode.ONE_OFF_EVENT_UNVERIFIED: "A one-off event may explain the pattern.",
}

PREREQUISITES: dict[PrerequisiteCode, str] = {
    PrerequisiteCode.CONFIRM_ELIGIBLE_INVENTORY: "Confirm comparable eligible inventory exists.",
    PrerequisiteCode.CONFIRM_CAPACITY: "Confirm capacity and demand headroom.",
    PrerequisiteCode.CONFIRM_CONTRACT_TERMS: "Confirm contract terms allow the change.",
    PrerequisiteCode.RECONCILE_COUNTERS_WITH_LOGS: "Reconcile counters with delivery logs.",
    PrerequisiteCode.OBTAIN_BID_DISTRIBUTIONS: "Obtain auction-level bid distributions and "
    "loss reasons.",
    PrerequisiteCode.CONFIRM_EXPORT_SEMANTICS: "Confirm export field definitions.",
    PrerequisiteCode.ESTABLISH_CONCURRENT_HOLDOUT: "Establish a concurrent holdout mechanism.",
}

RISKS: dict[RiskCode, str] = {
    RiskCode.DEMAND_SATURATION: "Demand may saturate; extra requests may not monetize at the "
    "observed yield.",
    RiskCode.DISPLACED_PROFIT_UNKNOWN: "Profit displaced from other traffic is unknown.",
    RiskCode.REVENUE_AT_RISK: "Revenue on the treated traffic is at risk during the test.",
    RiskCode.PARTNER_RELATIONSHIP: "The change may affect the partner relationship.",
    RiskCode.MEASUREMENT_NOISE: "Daily aggregates are noisy; daily counters are not "
    "independent trials.",
    RiskCode.ONE_OFF_CONCENTRATION: "Earnings are concentrated on few days.",
    RiskCode.COUNTER_SEMANTICS_UNKNOWN: "Counter definitions are not confirmed.",
}

SUMMARY_THEMES: dict[SummaryThemeCode, str] = {
    SummaryThemeCode.DATA_QUALITY_LIMITS: "Data-quality limits constrain what can be concluded.",
    SummaryThemeCode.SCALE_OPPORTUNITIES: "Some slices show high observed yield worth a "
    "controlled allocation test.",
    SummaryThemeCode.WASTE_REDUCTION: "Some high-volume traffic earns very little per request.",
    SummaryThemeCode.MARGIN_REVIEW: "Some slices have low or negative margin worth a terms "
    "review.",
    SummaryThemeCode.FLOOR_INVESTIGATION: "Bid and floor averages suggest floor investigations, "
    "not proven floor changes.",
    SummaryThemeCode.DELIVERY_RECONCILIATION: "Counter discrepancies need reconciliation before "
    "any fix.",
    SummaryThemeCode.TREND_WATCH: "Daily changes deserve monitoring before extrapolation.",
}

SCENARIO_TEMPLATES: dict[ScenarioTemplate, str] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: "Additional eligible requests at the "
    "observed unit yield",
    ScenarioTemplate.THROTTLE: "Throttle share of the slice's requests",
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: "Margin sensitivity at unchanged revenue",
    ScenarioTemplate.RENDERING_SENSITIVITY: "Illustrative relative change in realized "
    "impressions",
    ScenarioTemplate.NOT_IDENTIFIABLE: "Impact not identifiable from this export",
}

DISPLACED_TEXT: dict[DisplacedCostTreatment, str] = {
    DisplacedCostTreatment.SUBTRACTED_MATCHED_SOURCE: "displaced profit from a matched source "
    "is subtracted",
    DisplacedCostTreatment.UNKNOWN_DISCLOSED: "displaced profit is unknown and not subtracted",
    DisplacedCostTreatment.NOT_APPLICABLE: "no displacement applies",
}

CONFIDENCE_TEXT: dict[Confidence, str] = {
    Confidence.HIGH: "High",
    Confidence.MEDIUM: "Medium",
    Confidence.LOW: "Low",
}

ELIGIBILITY_TEXT: dict[EligibilityStatus, str] = {
    EligibilityStatus.ELIGIBLE: "eligible",
    EligibilityStatus.EXPLORATORY: "exploratory (outside default gates)",
    EligibilityStatus.REJECTED: "rejected",
}

ANALYSIS_STATUS_TEXT: dict[AnalysisStatus, str] = {
    AnalysisStatus.COMPLETE: "analysis complete",
    AnalysisStatus.REJECTED_INVALID: "model output rejected by validation",
    AnalysisStatus.BUDGET_EXHAUSTED: "call budget exhausted before a valid answer",
    AnalysisStatus.TRANSPORT_FAILED: "model endpoint unavailable",
}

VALIDATION_CODE_TEXT: dict[ValidationErrorCode, str] = {
    ValidationErrorCode.SCHEMA_INVALID: "output did not match the schema",
    ValidationErrorCode.UNKNOWN_REFERENCE: "referenced an unknown ID",
    ValidationErrorCode.STALE_NAMESPACE: "referenced another run's evidence",
    ValidationErrorCode.WRONG_METRIC: "cited the wrong metric",
    ValidationErrorCode.WRONG_UNIT: "cited the wrong unit",
    ValidationErrorCode.WRONG_SEGMENT: "cited the wrong segment",
    ValidationErrorCode.WRONG_PERIOD: "cited the wrong period",
    ValidationErrorCode.WRONG_ROLE: "used a fact in the wrong role",
    ValidationErrorCode.WRONG_DIRECTION: "stated the wrong direction",
    ValidationErrorCode.INVALID_DENOMINATOR: "relied on an invalid denominator",
    ValidationErrorCode.QUALITY_GATE_VIOLATION: "ignored a data-quality restriction",
    ValidationErrorCode.MISSING_COUNTER_EVIDENCE: "omitted required counter-evidence",
    ValidationErrorCode.UNAPPROVED_TEMPLATE: "used an unapproved template",
    ValidationErrorCode.ASSUMPTION_OUT_OF_BOUNDS: "used an assumption outside its bounds",
    ValidationErrorCode.SCENARIO_MISMATCH: "cited a scenario that does not match",
    ValidationErrorCode.OVERLAP_CONFLICT: "conflicted with an overlapping recommendation",
    ValidationErrorCode.UNSUPPORTED_CLAIM: "made an unsupported claim",
}

STATUS_BANNERS: dict[RunStatus, str] = {
    RunStatus.FINAL: "Final validated report from a self-hosted model run.",
    RunStatus.PARTIAL: "PARTIAL RUN: some candidates or the final ranking did not complete "
    "validation. This is not a final report.",
    RunStatus.DETERMINISTIC_ONLY: "DETERMINISTIC-ONLY DRAFT: no validated model analysis is "
    "included. This is a diagnostic draft, not a model-generated report.",
    RunStatus.FAILED: "FAILED RUN: the pipeline did not complete; only diagnostics are shown.",
    RunStatus.CACHED_PREVIOUS: "CACHED PREVIOUS RUN — not fresh inference.",
}

STAGE_LABELS: dict[ProgressStage, str] = {
    ProgressStage.LOAD: "Load CSV",
    ProgressStage.INITIAL_QUALITY: "Initial data-quality checks",
    ProgressStage.AGGREGATE: "Aggregate metrics",
    ProgressStage.DETECT: "Detect candidates",
    ProgressStage.REFINED_QUALITY: "Refined data-quality checks",
    ProgressStage.EVIDENCE: "Build evidence",
    ProgressStage.MODEL_READINESS: "Model readiness (cold start)",
    ProgressStage.CANDIDATE_ANALYSIS: "Candidate analysis (warm inference)",
    ProgressStage.TOOL_CALL: "Drill-down tool call",
    ProgressStage.VALIDATION: "Validation and repair",
    ProgressStage.RANKING: "Final ranking",
    ProgressStage.RENDER: "Render report",
    ProgressStage.COMPLETE: "Complete",
}

INTERVENTION_UNPROVEN = "intervention effect is unproven."
CONDITIONAL_IMPACT = "Conditional on the test working and these assumptions holding."
NOT_ESTIMABLE = "Not estimable from this export."
HORIZON_TEXT = "Horizon: comparable exposure over the observed period; no annualization."
NO_PORTFOLIO_SUM = (
    "Scenarios are not summed: they may overlap or be mutually exclusive."
)


# --- Short report ------------------------------------------------------------

SHORT_SUBTITLE = "Ranked opportunities from the uploaded export"
READING_GUIDE_TITLE = "How to read this report"
READING_GUIDE = (
    "\u201cPossibly incomplete\u201d marks numbers whose period includes a day with far less "
    "traffic than a normal day, usually a partially exported day; those totals may be understated.",
    "Codes such as F_\u2026, A_\u2026 and S_\u2026 are reference IDs. Each points to one number "
    "calculated by code and stored with the run. The model cites IDs and code fills in the "
    "values, so every number can be traced to its calculation.",
    "Counter-evidence lists the facts that could make a recommendation wrong.",
    "Estimates hold only under the assumption stated next to them. Cards about the same "
    "traffic overlap, so their estimates must not be added together.",
)
FOUND_BY_AI = "found by AI"
FOUND_BY_AI_HINT = (
    "Proposed by the model while exploring the data, not raised by a detector rule. "
    "It passed the same evidence checks."
)

# Plain verb and one-line action per test template.
ACTIONS: dict[TestTemplateId, tuple[str, str]] = {
    TestTemplateId.MATCHED_ALLOCATION_HOLDOUT: (
        "Scale up",
        "Send more comparable traffic to this slice as a controlled test against a "
        "concurrent holdout.",
    ),
    TestTemplateId.TARGETED_THROTTLE_HOLDOUT: (
        "Throttle",
        "Throttle part of this slice's low-yield requests against a concurrent holdout.",
    ),
    TestTemplateId.FLOOR_INCREASE_RANDOMIZED: (
        "Raise floor",
        "Raise the floor slightly on a randomized share of this slice's requests, with a "
        "concurrent holdout at the current floor.",
    ),
    TestTemplateId.FLOOR_DECREASE_RANDOMIZED: (
        "Lower floor",
        "Lower the floor slightly on a randomized share of this slice's requests, with a "
        "concurrent holdout at the current floor.",
    ),
    TestTemplateId.COMMERCIAL_TERMS_REVIEW: (
        "Review margin terms",
        "Check rounding, adjustments and contract terms for this slice before any "
        "negotiation.",
    ),
    TestTemplateId.COUNTER_RECONCILIATION: (
        "Reconcile counters",
        "Reconcile wins, impressions and requests against delivery logs before any fix.",
    ),
    TestTemplateId.INSTRUMENTATION_FEASIBILITY: (
        "Investigate",
        "Check what data and mechanism a controlled test of this slice would need.",
    ),
}
INVESTIGATE_ACTION = (
    "Investigate this slice before changing anything: confirm the pattern is real, then "
    "design a controlled test if it holds."
)
INVESTIGATE_FIRST_PREFIX = "First clear the prerequisites under Details, then "
RANDOMIZED_TESTS = frozenset({
    TestTemplateId.MATCHED_ALLOCATION_HOLDOUT,
    TestTemplateId.TARGETED_THROTTLE_HOLDOUT,
    TestTemplateId.FLOOR_INCREASE_RANDOMIZED,
    TestTemplateId.FLOOR_DECREASE_RANDOMIZED,
})

# (before, after) wrapped around the scenario's key assumption value.
ASSUMPTION_CLAUSES: dict[ScenarioTemplate, tuple[str, str]] = {
    ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: (
        "Assumes ", " more eligible requests at the slice's observed yield."),
    ScenarioTemplate.THROTTLE: ("Assumes ", " of the slice's requests are throttled."),
    ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: (
        "Assumes margin rises by ", " at unchanged revenue."),
    ScenarioTemplate.RENDERING_SENSITIVITY: (
        "Assumes a ", " relative change in realized impressions."),
}
ESTIMATE_NOTE = "Estimate, conditional on the test working; not a forecast."
GAIN_LEAD = "About "
OVER_PERIOD = " over the observed period"
EXTRA_PROFIT = " extra profit"
EXTRA_REVENUE = " extra revenue"
THROTTLE_NO_GAIN = (
    "Avoidable processing cost is not provided; throttling puts about "
)
AT_RISK = " at risk"
FEW_RECOMMENDATIONS = (
    "Fewer recommendations are supportable from this evidence; none were invented to "
    "fill the list."
)
NO_CARDS_FATAL = (
    "A fatal data-quality issue blocks model recommendations. Only the caveats and "
    "appendix are shown."
)
NO_CARDS = "No validated model recommendations are included in this run."
