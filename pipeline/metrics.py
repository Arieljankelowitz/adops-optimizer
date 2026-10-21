"""Observed-group aggregation with ratios of additive sums."""

from contracts.config import AnalyticsConfig
from contracts.models import QualityReport
from contracts.runtime import LoadedData, MetricsStore


def aggregate(data: LoadedData, quality: QualityReport, config: AnalyticsConfig) -> MetricsStore:
    """Build account, single-dimension, pairwise and five-dimension aggregates plus daily series."""
    raise NotImplementedError
