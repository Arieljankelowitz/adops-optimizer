"""Prompt-size estimates in model tokens, for the offline path and the evidence gate.

The deployed server counts exactly (see ``SelfHostedModelClient.count_prompt_tokens``).
This estimate is the fallback when that count is unavailable and the gate
``build_evidence`` applies before any server is reachable. It must not be
optimistic: bytes are NOT a usable unit (the serialized packs run at about 2.0
UTF-8 bytes per token), and neither is bytes / 4.

Calibrated against the pinned Qwen tokenizer on the serialized synthetic packs:
a character-class model (digits, ASCII letters, everything else) fits whole
packs within 1 %. The weights below round that fit up and ``MARGIN`` adds more,
so whole packs come out 4-9 % above the exact count, never below.
"""

import math
from collections.abc import Sequence

from contracts.ids import canonical_json
from contracts.runtime import ChatMessage

# Qwen splits numbers into single digits and spends several tokens on hex IDs.
DIGIT_TOKENS = 1.5
LETTER_TOKENS = 0.32
# JSON punctuation mostly merges into neighbouring tokens; a little is kept.
OTHER_ASCII_TOKENS = 0.05
# A non-ASCII byte is at most one token, so this stays an upper bound.
NON_ASCII_BYTE_TOKENS = 1.0
MARGIN = 1.02
# Chat-template framing per message and for the generation prompt.
MESSAGE_OVERHEAD_TOKENS = 8
GENERATION_PROMPT_TOKENS = 8
# System prompt, prompt wrapper and the turn instruction around a serialized
# pack. The evidence gate adds this to the pack text it counts.
PROMPT_OVERHEAD_TOKENS = 512


def estimate_text_tokens(text: str) -> int:
    """Conservative token estimate for ``text``."""
    total = 0.0
    for char in text:
        if char.isascii():
            if char.isdigit():
                total += DIGIT_TOKENS
            elif char.isalpha():
                total += LETTER_TOKENS
            else:
                total += OTHER_ASCII_TOKENS
        else:
            total += NON_ASCII_BYTE_TOKENS * len(char.encode("utf-8"))
    return math.ceil(total * MARGIN)


def estimate_pack_prompt_tokens(pack_text: str) -> int:
    """What the evidence gate counts for one serialized pack."""
    return estimate_text_tokens(pack_text) + PROMPT_OVERHEAD_TOKENS


def estimate_prompt_tokens(messages: Sequence[ChatMessage], tools: Sequence[object] = ()) -> int:
    """Estimate for a complete chat request: messages, tool definitions and framing."""
    total = GENERATION_PROMPT_TOKENS
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(
            canonical_json(message.model_dump(exclude_none=True, exclude={"role"}))
        )
    if tools:
        total += estimate_text_tokens(canonical_json(list(tools)))
    return total
