"""Closed vocabularies used across contracts.

Every enum is a ``StrEnum`` so values serialize as plain strings in JSON and
JSON Schema ``enum`` lists. Adding a member is a contract change: update the
exported schemas with ``scripts/export_contracts.py``.
"""

from enum import StrEnum


class InputColumn(StrEnum):
    """The 17 required header names of the daily export, in file order."""

    DAY = "Day"
    SUPPLY_ID = "Supply ID"
    COUNTRY = "Country"
    SIZE = "Size"
    PLACEMENT_TYPE = "Placement Type"
    DEVICE_TYPE = "Device Type"
    DEMAND_REQUESTS = "Demand Requests"
    DEMAND_RESPONSES = "Demand Responses"
    OUR_BIDFLOOR = "Our Bidfloor"
    DEMAND_BIDPRICE = "Demand Bidprice"
    DEMAND_SRPM = "Demand sRPM $"
    DEMAND_WINS = "Demand Wins"
    SUPPLY_IMPRESSIONS = "Supply Impressions"
    REVENUE = "Revenue"
    COST = "Cost"
    DEMAND_ECPM = "Demand eCPM"
    PROFIT = "Profit $"


REQUIRED_COLUMNS: tuple[InputColumn, ...] = tuple(InputColumn)

COUNTER_COLUMNS: tuple[InputColumn, ...] = (
    InputColumn.DEMAND_REQUESTS,
    InputColumn.DEMAND_RESPONSES,
    InputColumn.DEMAND_WINS,
    InputColumn.SUPPLY_IMPRESSIONS,
)

MONEY_COLUMNS: tuple[InputColumn, ...] = (
    InputColumn.REVENUE,
    InputColumn.COST,
    InputColumn.PROFIT,
)

# Exported prices and rates: never summed, only used for reconciliation checks.
RATE_COLUMNS: tuple[InputColumn, ...] = (
    InputColumn.OUR_BIDFLOOR,
    InputColumn.DEMAND_BIDPRICE,
    InputColumn.DEMAND_SRPM,
    InputColumn.DEMAND_ECPM,
)


class Dimension(StrEnum):
    """The five approved segment dimensions; also the drill-down ``by`` values."""

    SUPPLY_ID = "supply_id"
    COUNTRY = "country"
    SIZE = "size"
    PLACEMENT_TYPE = "placement_type"
    DEVICE_TYPE = "device_type"


DIMENSION_COLUMNS: dict[Dimension, InputColumn] = {
    Dimension.SUPPLY_ID: InputColumn.SUPPLY_ID,
    Dimension.COUNTRY: InputColumn.COUNTRY,
    Dimension.SIZE: InputColumn.SIZE,
    Dimension.PLACEMENT_TYPE: InputColumn.PLACEMENT_TYPE,
    Dimension.DEVICE_TYPE: InputColumn.DEVICE_TYPE,
}


class MetricKey(StrEnum):
    """Metric identifiers carried by facts. Rates are always ratios of sums."""

    # Additive totals.
    REQUESTS = "requests"
    RESPONSES = "responses"
    WINS = "wins"
    IMPRESSIONS = "impressions"
    REVENUE = "revenue"
    COST = "cost"
    PROFIT = "profit"
    # Derived from additive sums.
    RESPONSE_RATE = "response_rate"
    WIN_RATE = "win_rate"
    IMPRESSION_TO_WIN_RATE = "impression_to_win_rate"
    ECPM = "ecpm"
    REVENUE_PER_MILLION_REQUESTS = "revenue_per_million_requests"
    PROFIT_PER_MILLION_REQUESTS = "profit_per_million_requests"
    MARGIN = "margin"
    REQUEST_SHARE = "request_share"
    REVENUE_SHARE = "revenue_share"
    PROFIT_SHARE = "profit_share"
    REQUEST_WEIGHTED_FLOOR = "request_weighted_floor"
    RESPONSE_WEIGHTED_BID = "response_weighted_bid"
    DAILY_CHANGE = "daily_change"
    # Reconciliation and coverage statistics used by quality rules.
    PROFIT_RECONCILIATION_RESIDUAL = "profit_reconciliation_residual"
    RECONCILIATION_BOUND = "reconciliation_bound"
    ROW_COUNT = "row_count"
    ROW_SHARE = "row_share"
    IMPRESSION_SHARE = "impression_share"
    OBSERVED_DAYS = "observed_days"
    REVENUE_POSITIVE_DAYS = "revenue_positive_days"
    MISSING_DAYS = "missing_days"
    # Day value divided by the median of the other observed days.
    DAY_REQUEST_RATIO = "day_request_ratio"
    DAY_REVENUE_RATIO = "day_revenue_ratio"
    SLICE_DROP_SHARE = "slice_drop_share"
    ADJACENT_DAY_YIELD_RATIO = "adjacent_day_yield_ratio"
    MAX_DAY_REVENUE_SHARE = "max_day_revenue_share"
    TOP_TWO_DAY_REVENUE_SHARE = "top_two_day_revenue_share"
    MAX_DAY_PROFIT_SHARE = "max_day_profit_share"
    TOP_TWO_DAY_PROFIT_SHARE = "top_two_day_profit_share"
    SRPM_MATCH_SHARE = "srpm_match_share"
    SRPM_ERROR_RATIO = "srpm_error_ratio"
    ECPM_MISMATCH_SHARE = "ecpm_mismatch_share"
    COUNTER_GRANULARITY = "counter_granularity"
    GRANULARITY_COVERAGE = "granularity_coverage"
    ZERO_BID_RESPONSE_SHARE = "zero_bid_response_share"
    BID_BELOW_FLOOR_RESPONSE_SHARE = "bid_below_floor_response_share"
    ZERO_FLOOR_REQUEST_SHARE = "zero_floor_request_share"


class Unit(StrEnum):
    """Units of fact and assumption values. No ISO currency is ever asserted."""

    COUNT = "count"
    FRACTION = "fraction"
    RATIO = "ratio"
    EXPORT_DOLLAR = "export_dollar"
    EXPORT_DOLLAR_PER_THOUSAND_IMPRESSIONS = "export_dollar_per_thousand_impressions"
    EXPORT_DOLLAR_PER_MILLION_REQUESTS = "export_dollar_per_million_requests"
    # Floor and bid averages keep the export's own price scale, unconfirmed.
    EXPORT_PRICE_AS_EXPORTED = "export_price_as_exported"
    PERCENTAGE_POINTS = "percentage_points"
    DAYS = "days"


class FactKind(StrEnum):
    MEASURED = "measured"
    CALCULATED = "calculated"
    COMPARISON = "comparison"
    QUALITY = "quality"
    SCENARIO = "scenario"


class ValidityStatus(StrEnum):
    VALID = "valid"
    # Zero or missing denominator: value is null with a reason.
    UNDEFINED = "undefined"
    # Inputs failed a quality rule: value is null with a reason.
    INVALID = "invalid"
    # Value computed but includes anomalous contributors; no clean claim.
    TAINTED = "tainted"


class CoverageStatus(StrEnum):
    """Daily exports cannot prove completeness, so nothing is ever ``complete``."""

    UNVERIFIED = "unverified"
    POSSIBLY_INCOMPLETE = "possibly_incomplete"
    INSUFFICIENT_DATA = "insufficient_data"
    EXTERNALLY_VERIFIED = "externally_verified"


class Weighting(StrEnum):
    SUM = "sum"
    RATIO_OF_SUMS = "ratio_of_sums"
    REQUEST_WEIGHTED = "request_weighted"
    RESPONSE_WEIGHTED = "response_weighted"
    NOT_APPLICABLE = "not_applicable"


class Relation(StrEnum):
    """Direction of a comparison fact, computed by code."""

    GREATER = "greater"
    LESS = "less"
    EQUAL = "equal"
    UNDEFINED = "undefined"


class Severity(StrEnum):
    """Quality severities: ``fatal`` blocks model recommendations."""

    FATAL = "fatal"
    WARNING = "warning"
    NOTICE = "notice"


class QualityRuleId(StrEnum):
    DQ01 = "DQ01"  # file / schema / parse
    DQ02 = "DQ02"  # blank dimensions and normalization collisions
    DQ03 = "DQ03"  # duplicate grain
    DQ04 = "DQ04"  # counter validity
    DQ05 = "DQ05"  # undefined rates / money without traffic
    DQ06 = "DQ06"  # date coverage and possible partial days
    DQ07 = "DQ07"  # per-segment intermittent coverage
    DQ08 = "DQ08"  # one/two-day money concentration
    DQ09 = "DQ09"  # daily spikes / step changes
    DQ10 = "DQ10"  # placeholder or unverified country
    DQ11 = "DQ11"  # placeholder or unusual size
    DQ12 = "DQ12"  # money / profit reconciliation
    DQ13 = "DQ13"  # sRPM unit verification
    DQ14 = "DQ14"  # eCPM consistency
    DQ15 = "DQ15"  # row vs traffic weighting
    DQ16 = "DQ16"  # request granularity / suspicious scaling
    DQ17 = "DQ17"  # bid / floor semantic coverage
    DQ18 = "DQ18"  # money sign / zero-money rounding
    DQ19 = "DQ19"  # materiality / threshold instability
    DQ20 = "DQ20"  # comparability and causal limits


class RuleOutcome(StrEnum):
    """Insufficient evidence is ``not_testable``, never a pass."""

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    NOT_TESTABLE = "not_testable"


class QualityPass(StrEnum):
    INITIAL = "initial"
    REFINED = "refined"


class InferenceRestriction(StrEnum):
    """Inferences a quality issue removes from the affected scope."""

    NO_RECOMMENDATIONS = "no_recommendations"
    NO_FUNNEL_INFERENCE = "no_funnel_inference"
    NO_YIELD_ESTIMATE = "no_yield_estimate"
    NO_STABLE_SCALE_CLAIM = "no_stable_scale_claim"
    NO_TREND_CLAIM = "no_trend_claim"
    NO_MATCHED_PEER_LABEL = "no_matched_peer_label"
    NO_COUNTRY_OPPORTUNITY = "no_country_opportunity"
    NO_FORMAT_SPECIFIC_TEST = "no_format_specific_test"
    NO_FLOOR_CAUSAL_CLAIM = "no_floor_causal_claim"
    EXPORTED_RATE_UNTRUSTED = "exported_rate_untrusted"


class DetectorFamily(StrEnum):
    SCALE_WINNER = "scale_winner"
    WASTED_REQUESTS = "wasted_requests"
    MARGIN_LEAK = "margin_leak"
    FLOOR_HIGH_BID = "floor_high_bid"
    FLOOR_LOW_BID = "floor_low_bid"
    WINS_IMPRESSIONS_DISCREPANCY = "wins_impressions_discrepancy"
    TREND = "trend"
    COMBINATION_OUTLIER = "combination_outlier"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    EXPLORATORY = "exploratory"
    REJECTED = "rejected"


class Decision(StrEnum):
    RECOMMEND_TEST = "recommend_test"
    INVESTIGATE_FIRST = "investigate_first"
    REJECT = "reject"
    MERGE = "merge"


class Confidence(StrEnum):
    """Confidence in the observed pattern, not in an intervention's effect."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AssumptionSelector(StrEnum):
    DEFAULT_TEST_TEMPLATE = "default_test_template"
    USER = "user"


class ScenarioTemplate(StrEnum):
    ADDITIONAL_ELIGIBLE_REQUESTS = "additional_eligible_requests"
    THROTTLE = "throttle"
    MARGIN_NEGOTIATION_SENSITIVITY = "margin_negotiation_sensitivity"
    RENDERING_SENSITIVITY = "rendering_sensitivity"
    NOT_IDENTIFIABLE = "not_identifiable"


class DisplacedCostTreatment(StrEnum):
    SUBTRACTED_MATCHED_SOURCE = "subtracted_matched_source"
    UNKNOWN_DISCLOSED = "unknown_disclosed"
    NOT_APPLICABLE = "not_applicable"


class ToolName(StrEnum):
    BREAKDOWN = "breakdown"
    DAILY_TREND = "daily_trend"
    COMPARE_TO_PEERS = "compare_to_peers"


class NextStepKind(StrEnum):
    CALL_TOOL = "call_tool"
    FINAL = "final"


class AnalysisStatus(StrEnum):
    COMPLETE = "complete"
    REJECTED_INVALID = "rejected_invalid"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TRANSPORT_FAILED = "transport_failed"


class ValidationErrorCode(StrEnum):
    SCHEMA_INVALID = "schema_invalid"
    UNKNOWN_REFERENCE = "unknown_reference"
    STALE_NAMESPACE = "stale_namespace"
    WRONG_METRIC = "wrong_metric"
    WRONG_UNIT = "wrong_unit"
    WRONG_SEGMENT = "wrong_segment"
    WRONG_PERIOD = "wrong_period"
    WRONG_ROLE = "wrong_role"
    WRONG_DIRECTION = "wrong_direction"
    INVALID_DENOMINATOR = "invalid_denominator"
    QUALITY_GATE_VIOLATION = "quality_gate_violation"
    MISSING_COUNTER_EVIDENCE = "missing_counter_evidence"
    UNAPPROVED_TEMPLATE = "unapproved_template"
    ASSUMPTION_OUT_OF_BOUNDS = "assumption_out_of_bounds"
    SCENARIO_MISMATCH = "scenario_mismatch"
    OVERLAP_CONFLICT = "overlap_conflict"
    UNSUPPORTED_CLAIM = "unsupported_claim"


class RunStatus(StrEnum):
    FINAL = "final"
    PARTIAL = "partial"
    DETERMINISTIC_ONLY = "deterministic_only"
    FAILED = "failed"
    CACHED_PREVIOUS = "cached_previous"


class ProgressStage(StrEnum):
    LOAD = "load"
    INITIAL_QUALITY = "initial_quality"
    AGGREGATE = "aggregate"
    DETECT = "detect"
    REFINED_QUALITY = "refined_quality"
    EVIDENCE = "evidence"
    MODEL_READINESS = "model_readiness"
    CANDIDATE_ANALYSIS = "candidate_analysis"
    TOOL_CALL = "tool_call"
    VALIDATION = "validation"
    RANKING = "ranking"
    RENDER = "render"
    COMPLETE = "complete"


class ProgressState(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RenderRole(StrEnum):
    """Role of a quantitative span in the rendered report audit map."""

    OBSERVATION = "observation"
    BASELINE = "baseline"
    ASSUMPTION = "assumption"
    SCENARIO = "scenario"
    WARNING = "warning"
    METADATA = "metadata"
