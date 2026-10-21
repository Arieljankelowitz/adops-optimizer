"""One bounded model analysis per candidate, including the tool loop."""

from contracts.config import RunBudget
from contracts.models import CandidateAnalysis, CandidatePack
from contracts.runtime import ModelClient, ToolRouter


def analyze_candidate(
    pack: CandidatePack, tools: ToolRouter, client: ModelClient, budget: RunBudget
) -> CandidateAnalysis:
    """Ask the model to reject, drill down or recommend, within the per-candidate budget."""
    raise NotImplementedError
