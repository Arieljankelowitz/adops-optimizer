"""Opt-in check against a running endpoint: one synthetic, schema-constrained request.

Deselected by default. Needs LLM_BASE_URL, LLM_API_KEY and LLM_ALLOWED_BASE_URLS
in the environment and a served model; a cold start can take several minutes
and the request is billed as GPU time. See deploy/README.md.
"""

import json
import os

import pytest

from contracts.config import RunBudget
from contracts.runtime import ChatMessage, ModelRequest
from llm.client import SelfHostedModelClient, create_client

pytestmark = pytest.mark.live

FACT_IDS = ["F_00000000000000a1", "F_00000000000000b2", "F_00000000000000c3"]
SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["investigate", "reject"]},
        "supporting_fact_ids": {
            "type": "array",
            "items": {"type": "string", "enum": FACT_IDS},
            "minItems": 1,
            "maxItems": 3,
        },
        "reason_code": {"type": "string", "enum": ["material_gap", "immaterial", "unstable"]},
    },
    "required": ["decision", "supporting_fact_ids", "reason_code"],
    "additionalProperties": False,
}
PACK = {
    "candidate": "C_0000000000000001",
    "facts": [
        {"fact_id": FACT_IDS[0], "statement": "segment fill rate", "value": "0.12"},
        {"fact_id": FACT_IDS[1], "statement": "matched baseline fill rate", "value": "0.31"},
        {"fact_id": FACT_IDS[2], "statement": "segment share of requests", "value": "0.18"},
    ],
}


def test_synthetic_schema_constrained_request() -> None:
    client = create_client(os.environ, RunBudget())
    assert isinstance(client, SelfHostedModelClient)
    waited = client.wait_until_ready()

    response = client.generate(ModelRequest(
        messages=[
            ChatMessage(role="system", content=(
                "You review precomputed synthetic evidence. Do not calculate. "
                "Return only the supplied JSON Schema.")),
            ChatMessage(role="user", content=(
                "Should this candidate be investigated? Cite the facts that support "
                "your decision.\n" + json.dumps(PACK))),
        ],
        response_schema_name="live_check",
        response_schema=SCHEMA,
        max_output_tokens=256,
    ))

    answer = json.loads(response.content)
    assert answer["decision"] in {"investigate", "reject"}
    assert set(answer["supporting_fact_ids"]) <= set(FACT_IDS)
    assert response.finish_reason == "stop"
    assert response.prompt_tokens > 0 and response.output_tokens > 0
    print(f"\nready after {waited:.0f} s; answer {answer}; stats {client.stats}")
