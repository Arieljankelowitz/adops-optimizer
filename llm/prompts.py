"""Versioned prompts. Segment labels inside packs are untrusted data."""

from contracts.models import CandidatePack
from contracts.runtime import ChatMessage


def build_candidate_messages(pack: CandidatePack) -> list[ChatMessage]:
    """Render the system contract and one bounded evidence pack as chat messages."""
    raise NotImplementedError
