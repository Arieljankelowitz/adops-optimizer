"""Fail-closed validation of model output against the immutable evidence bundle."""

from contracts.models import CandidateAnalysis, RankingIR, ValidationResult
from contracts.runtime import EvidenceBundle


def validate_analysis(
    output: CandidateAnalysis | RankingIR, bundle: EvidenceBundle
) -> ValidationResult:
    """Resolve every reference in the current namespace and check metric, unit, scope and role."""
    raise NotImplementedError
