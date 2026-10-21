"""Deterministic drill-down tools exposed to the model and the UI."""

from contracts.enums import Dimension
from contracts.models import ToolEvidence
from contracts.runtime import AnalysisContext


def breakdown(
    ctx: AnalysisContext, segment_id: str, by: Dimension, period_id: str, limit: int = 10
) -> ToolEvidence:
    """Top-``limit`` children of a segment along ``by``, with an ``other`` aggregate."""
    raise NotImplementedError


def daily_trend(ctx: AnalysisContext, segment_id: str, period_id: str) -> ToolEvidence:
    """Daily series that distinguishes absent days from observed zeros."""
    raise NotImplementedError


def compare_to_peers(
    ctx: AnalysisContext, segment_id: str, period_id: str, peer_definition_id: str
) -> ToolEvidence:
    """Comparison against peers that match on the definition's fixed dimensions."""
    raise NotImplementedError
