"""Finite libraries the model selects from instead of writing free text.

Observation templates are rendered by code from typed fact references; the
model never types a quantity. Hypothesis, prerequisite, risk and summary
codes are reviewed qualitative codes; the renderer adds uncertainty wording.
"""

from dataclasses import dataclass
from enum import StrEnum


class ObservationTemplateId(StrEnum):
    METRIC_VALUE = "metric_value"
    METRIC_VS_BASELINE = "metric_vs_baseline"
    TREND = "trend"
    COVERAGE = "coverage"
    CONCENTRATION = "concentration"


@dataclass(frozen=True)
class ObservationTemplate:
    """Ordered fact roles an observation statement must supply, one fact per role."""

    template_id: ObservationTemplateId
    fact_roles: tuple[str, ...]


OBSERVATION_TEMPLATES: dict[ObservationTemplateId, ObservationTemplate] = {
    t.template_id: t
    for t in (
        ObservationTemplate(ObservationTemplateId.METRIC_VALUE, ("target",)),
        ObservationTemplate(
            ObservationTemplateId.METRIC_VS_BASELINE, ("target", "baseline", "comparison")
        ),
        ObservationTemplate(ObservationTemplateId.TREND, ("earlier", "later", "change")),
        ObservationTemplate(ObservationTemplateId.COVERAGE, ("observed_days", "reference_days")),
        ObservationTemplate(ObservationTemplateId.CONCENTRATION, ("concentration_share",)),
    )
}


class TestTemplateId(StrEnum):
    __test__ = False  # not a pytest test class

    MATCHED_ALLOCATION_HOLDOUT = "matched_allocation_holdout"
    TARGETED_THROTTLE_HOLDOUT = "targeted_throttle_holdout"
    FLOOR_INCREASE_RANDOMIZED = "floor_increase_randomized"
    FLOOR_DECREASE_RANDOMIZED = "floor_decrease_randomized"
    COMMERCIAL_TERMS_REVIEW = "commercial_terms_review"
    COUNTER_RECONCILIATION = "counter_reconciliation"
    INSTRUMENTATION_FEASIBILITY = "instrumentation_feasibility"


class HypothesisCode(StrEnum):
    TRAFFIC_MIX_OR_CONFIGURATION_UNVERIFIED = "traffic_mix_or_configuration_unverified"
    DEMAND_QUALITY_OR_MIX_UNVERIFIED = "demand_quality_or_mix_unverified"
    FLOOR_SETTING_UNVERIFIED = "floor_setting_unverified"
    RENDERING_OR_COUNTING_DEFECT_UNVERIFIED = "rendering_or_counting_defect_unverified"
    COMMERCIAL_TERMS_UNVERIFIED = "commercial_terms_unverified"
    EXPORT_SEMANTICS_UNVERIFIED = "export_semantics_unverified"
    ONE_OFF_EVENT_UNVERIFIED = "one_off_event_unverified"


class PrerequisiteCode(StrEnum):
    CONFIRM_ELIGIBLE_INVENTORY = "confirm_eligible_inventory"
    CONFIRM_CAPACITY = "confirm_capacity"
    CONFIRM_CONTRACT_TERMS = "confirm_contract_terms"
    RECONCILE_COUNTERS_WITH_LOGS = "reconcile_counters_with_logs"
    OBTAIN_BID_DISTRIBUTIONS = "obtain_bid_distributions"
    CONFIRM_EXPORT_SEMANTICS = "confirm_export_semantics"
    ESTABLISH_CONCURRENT_HOLDOUT = "establish_concurrent_holdout"


class RiskCode(StrEnum):
    DEMAND_SATURATION = "demand_saturation"
    DISPLACED_PROFIT_UNKNOWN = "displaced_profit_unknown"
    REVENUE_AT_RISK = "revenue_at_risk"
    PARTNER_RELATIONSHIP = "partner_relationship"
    MEASUREMENT_NOISE = "measurement_noise"
    ONE_OFF_CONCENTRATION = "one_off_concentration"
    COUNTER_SEMANTICS_UNKNOWN = "counter_semantics_unknown"


class SummaryThemeCode(StrEnum):
    DATA_QUALITY_LIMITS = "data_quality_limits"
    SCALE_OPPORTUNITIES = "scale_opportunities"
    WASTE_REDUCTION = "waste_reduction"
    MARGIN_REVIEW = "margin_review"
    FLOOR_INVESTIGATION = "floor_investigation"
    DELIVERY_RECONCILIATION = "delivery_reconciliation"
    TREND_WATCH = "trend_watch"
