"""Configuration contracts with the plan's default thresholds.

All thresholds are heuristics, not significance tests. Every value is part of
the configuration hash and therefore of the run namespace. Lanes may add
fields; changing a default changes the namespace of every later run.
"""

import hashlib
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from contracts.ids import canonical_json
from contracts.models import DecimalValue


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InputConfig(Config):
    max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_rows: int = Field(default=2_000_000, gt=0)
    # Source quantum for money and exported rates; overridable per export.
    money_quantum: DecimalValue = Decimal("0.01")
    rate_quantum: DecimalValue = Decimal("0.01")
    date_format: str = "%Y-%m-%d"
    # Tagged bucket for blank dimension cells; the raw blank stays in the audit map.
    unknown_dimension_label: str = "UNKNOWN"


class QualityConfig(Config):
    rules_version: int = Field(default=1, ge=1)
    partial_day_ratio: DecimalValue = Decimal("0.5")
    partial_edge_day_ratio: DecimalValue = Decimal("0.7")
    min_days_for_completeness: int = 3
    material_slice_request_share: DecimalValue = Decimal("0.01")
    broad_drop_slice_share: DecimalValue = Decimal("0.5")
    min_stable_days: int = 4
    concentration_min_revenue: DecimalValue = Decimal("100")
    max_day_share: DecimalValue = Decimal("0.6")
    max_two_day_share: DecimalValue = Decimal("0.8")
    spike_multiple: DecimalValue = Decimal("3")
    spike_min_request_share: DecimalValue = Decimal("0.01")
    spike_min_revenue: DecimalValue = Decimal("100")
    yield_step_up: DecimalValue = Decimal("2")
    yield_step_down: DecimalValue = Decimal("0.5")
    yield_step_min_requests: int = 1_000_000
    yield_step_min_revenue: DecimalValue = Decimal("25")
    country_placeholders: tuple[str, ...] = ("", "-", "--", "unknown", "null", "n/a")
    size_sentinel: int = 9999
    size_max_pixels: int = 4096
    # Non-WxH size labels accepted as documented special formats.
    allowed_special_sizes: tuple[str, ...] = ()
    # Versioned accepted country reference (pipeline/countries.py) plus documented extras.
    country_reference: str = "iso3166_alpha2_v1"
    extra_accepted_countries: tuple[str, ...] = ()
    reconciliation_epsilon: DecimalValue = Decimal("0.000001")
    srpm_multiplier: int = 1_000_000
    srpm_alternative_multiplier: int = 1_000
    srpm_confirm_share: DecimalValue = Decimal("0.95")
    srpm_error_ratio: DecimalValue = Decimal("10")
    ecpm_mismatch_share: DecimalValue = Decimal("0.01")
    weighting_gap_points: DecimalValue = Decimal("0.10")
    granularity_min_rows: int = 100
    granularity_coverage: DecimalValue = Decimal("0.95")
    zero_bid_response_share: DecimalValue = Decimal("0.01")
    bid_below_floor_response_share: DecimalValue = Decimal("0.01")
    # DQ19 materiality gates; keep aligned with AnalyticsConfig eligibility defaults.
    materiality_min_requests: int = 10_000_000
    materiality_min_revenue: DecimalValue = Decimal("100")
    materiality_waste_min_requests: int = 100_000_000
    materiality_wins_min_wins: int = 1_000_000
    sensitivity_multipliers: tuple[DecimalValue, ...] = (Decimal("0.5"), Decimal("2"))
    # Affected segments listed per issue (largest by requests); totals cover all rows.
    max_issue_segments: int = Field(default=100, ge=1)


class AnalyticsConfig(Config):
    analytics_version: int = Field(default=1, ge=1)
    min_requests: int = 10_000_000
    min_revenue: DecimalValue = Decimal("100")
    waste_min_requests: int = 100_000_000
    wins_min_wins: int = 1_000_000
    sensitivity_multipliers: tuple[DecimalValue, ...] = (Decimal("0.5"), Decimal("2"))
    # Material candidates outside the gates, but above this fraction of them,
    # are kept in a labelled exploratory table instead of being discarded.
    exploratory_gate_fraction: DecimalValue = Decimal("0.1")
    # Stability gate: observed and revenue-positive days, and no one/two-day
    # concentration of positive revenue or profit.
    min_stable_days: int = 4
    concentration_min_positive: DecimalValue = Decimal("100")
    concentration_max_day_share: DecimalValue = Decimal("0.6")
    concentration_max_two_day_share: DecimalValue = Decimal("0.8")


class DetectorConfig(Config):
    detectors_version: int = Field(default=1, ge=1)
    scale_yield_multiple: DecimalValue = Decimal("2")
    scale_max_request_share: DecimalValue = Decimal("0.02")
    waste_max_revenue_per_million: DecimalValue = Decimal("0.05")
    margin_floor: DecimalValue = Decimal("0.25")
    # Child margin this many fraction points below its parent excluding itself.
    margin_disparity_points: DecimalValue = Decimal("0.15")
    high_bid_floor_ratio: DecimalValue = Decimal("2")
    low_bid_floor_ratio: DecimalValue = Decimal("0.9")
    # Response or win rate at most this multiple of the matched peer's.
    low_bid_peer_rate_ratio: DecimalValue = Decimal("0.5")
    impressions_per_win_floor: DecimalValue = Decimal("0.10")
    impressions_per_win_peer_ratio: DecimalValue = Decimal("0.5")
    trend_step_up: DecimalValue = Decimal("2")
    trend_step_down: DecimalValue = Decimal("0.5")
    trend_min_day_requests: int = 1_000_000
    trend_min_day_revenue: DecimalValue = Decimal("25")
    trend_volume_multiple: DecimalValue = Decimal("3")
    trend_min_usable_days: int = 4
    combination_multiple_up: DecimalValue = Decimal("2")
    combination_multiple_down: DecimalValue = Decimal("0.5")
    # A same-family parent is rejected as a duplicate when one child candidate
    # carries at least this share of its requests.
    dedup_child_request_share: DecimalValue = Decimal("0.8")


class EvidenceConfig(Config):
    max_candidates: int = 15
    default_additional_request_fraction: DecimalValue = Decimal("0.10")
    default_throttle_fraction: DecimalValue = Decimal("0.50")
    margin_sensitivity_points: DecimalValue = Decimal("0.05")
    rendering_sensitivity_relative: DecimalValue = Decimal("0.05")
    test_allocation: DecimalValue = Decimal("0.10")
    test_min_days: int = 7
    test_profit_stop_loss: DecimalValue = Decimal("0.05")


class RunBudget(Config):
    max_candidates: int = 15
    max_tool_calls_per_candidate: int = 3
    max_turns_per_candidate: int = 4
    max_repairs_per_candidate: int = 1
    max_ranking_calls: int = 1
    max_ranking_repairs: int = 1
    max_network_attempts: int = 100
    max_transport_retries: int = 1
    warm_inference_seconds: int = 1800
    readiness_seconds: int = 600
    request_timeout_seconds: int = 180


class RunConfig(Config):
    input: InputConfig = InputConfig()
    quality: QualityConfig = QualityConfig()
    analytics: AnalyticsConfig = AnalyticsConfig()
    detectors: DetectorConfig = DetectorConfig()
    evidence: EvidenceConfig = EvidenceConfig()
    budget: RunBudget = RunBudget()
    # Selects an entry in config/model_profiles.yaml; None means deterministic only.
    model_profile_id: str | None = None


def config_hash(config: Config) -> str:
    """Full SHA-256 of the canonical JSON of ``config``; feeds the run namespace."""
    payload = canonical_json(config.model_dump(mode="json"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
