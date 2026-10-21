"""Versioned data-quality rules DQ01-DQ20 in two passes."""

from contracts.config import QualityConfig
from contracts.models import Candidate, QualityReport
from contracts.runtime import LoadedData, MetricsStore


def assess_quality(data: LoadedData, config: QualityConfig) -> QualityReport:
    """First pass: source, schema, counter and account-day checks."""
    raise NotImplementedError


def refine_quality(
    data: LoadedData,
    metrics: MetricsStore,
    candidates: list[Candidate],
    initial: QualityReport,
    config: QualityConfig,
) -> QualityReport:
    """Second pass over every preliminary candidate before truncation.

    Adds concentration, coverage, peer and threshold-sensitivity issues; it
    never removes a first-pass issue.
    """
    raise NotImplementedError
