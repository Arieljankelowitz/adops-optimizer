"""Immutable evidence registry and bounded per-candidate packs."""

from contracts.config import EvidenceConfig
from contracts.models import Candidate, QualityReport
from contracts.runtime import EvidenceBundle, MetricsStore


def build_evidence(
    metrics: MetricsStore,
    quality: QualityReport,
    candidates: list[Candidate],
    config: EvidenceConfig,
) -> EvidenceBundle:
    """Create historical facts and assumptions, call ``compute_scenarios``, then freeze the bundle."""
    raise NotImplementedError
