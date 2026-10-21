"""Final ranking and merge selection over validated recommendations."""

from contracts.config import RunBudget
from contracts.models import RankingIR, ValidatedRecommendation
from contracts.runtime import FactRegistry, ModelClient


def rank_recommendations(
    validated: list[ValidatedRecommendation],
    registry: FactRegistry,
    client: ModelClient,
    budget: RunBudget,
) -> RankingIR:
    """Select, order and merge up to ten validated recommendations."""
    raise NotImplementedError
