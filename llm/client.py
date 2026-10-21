"""Fail-closed transport to the approved self-hosted, OpenAI-compatible endpoint."""

from collections.abc import Mapping

from contracts.config import RunBudget
from contracts.runtime import ModelClient


def create_client(env: Mapping[str, str], budget: RunBudget) -> ModelClient:
    """Build a client from explicit ``LLM_BASE_URL`` and ``LLM_API_KEY``.

    Refuses missing configuration, non-allowlisted endpoints and hosted model
    APIs; never falls back to an SDK default endpoint.
    """
    raise NotImplementedError
