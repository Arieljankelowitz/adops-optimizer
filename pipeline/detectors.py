"""The eight opportunity detector families; they produce candidates, not conclusions."""

from contracts.config import DetectorConfig
from contracts.models import Candidate, QualityReport
from contracts.runtime import MetricsStore


def detect_opportunities(
    metrics: MetricsStore, quality: QualityReport, config: DetectorConfig
) -> list[Candidate]:
    """Run every detector family over observed groups and attach supporting and contrary evidence."""
    raise NotImplementedError
