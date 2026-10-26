"""A scripted stand-in for the served model, for offline end-to-end runs.

It behaves like the live model under guided decoding: every answer must match
the request's response schema (checked here with jsonschema, as the server
enforces it), it explores with a tool before answering when the schema lets
it, and it answers from the code-computed answer guide. It never reads rows;
it sees exactly the request the real client would send.
"""

import json
from types import SimpleNamespace

import jsonschema

from contracts.enums import DetectorFamily
from contracts.runtime import ModelResponse
from llm.profiles import load_profile
from llm.tokens import estimate_prompt_tokens


def _evidence(request) -> dict:
    return json.loads(request.messages[1].content)["untrusted_evidence"]


def _tool_results(request) -> list[dict]:
    out = []
    for message in request.messages[2:]:
        if message.role in ("user", "tool") and "untrusted_tool_evidence" in message.content:
            out.append(json.loads(message.content))
    return out


def _variants(definition: dict | None) -> list[dict]:
    if definition is None:
        return []
    return definition.get("anyOf", [definition])


class ScriptedModel:
    """Answers only what the response schema admits; counts what it was asked."""

    def __init__(self, explore_proposals: int = 1) -> None:
        # The deployed profile's limits (prompt window, output reserve).
        self.profile = load_profile()
        self.requests = []
        self.explore_proposals = explore_proposals
        self.stats = SimpleNamespace(
            network_attempts=0, transport_retries=0, prompt_tokens=0, output_tokens=0,
            token_counts_exact=0, token_counts_estimated=0,
        )

    def count_prompt_tokens(self, messages, tools) -> int:
        return estimate_prompt_tokens(messages, tools)

    def generate(self, request):
        self.requests.append(request)
        name = request.response_schema_name
        if name == "recommendation_ranking":
            document = self._rank(request)
        elif name == "explore_next_step":
            document = self._explore(request)
        elif name == "candidate_next_step":
            document = self._step(request)
        else:
            document = self._final(request)
        # Guided decoding: the server cannot emit anything else.
        jsonschema.Draft202012Validator(request.response_schema).validate(document)
        content = json.dumps(document)
        self.stats.network_attempts += 1
        prompt = estimate_prompt_tokens(request.messages, request.tools)
        self.stats.prompt_tokens += prompt
        self.stats.output_tokens += len(content) // 3
        return ModelResponse(
            content=content, prompt_tokens=prompt, output_tokens=len(content) // 3,
            finish_reason="stop",
        )

    # Candidate judgment.

    def _tool_call(self, request) -> dict | None:
        """A breakdown of the candidate's slice, else a peer comparison, if offered."""
        schema = request.response_schema
        if schema["properties"]["kind"].get("const") == "final":
            return None
        c = _evidence(request)["candidate"]
        defs = schema["$defs"]
        for variant in _variants(defs.get("BreakdownArgs")):
            props = variant["properties"]
            if c["segment_id"] in props["segment_id"]["enum"]:
                return {
                    "tool": "breakdown", "segment_id": c["segment_id"],
                    "by": props["by"]["enum"][0], "period_id": c["period_id"], "limit": 5,
                }
        peers = defs.get("CompareToPeersArgs")
        if peers and c["segment_id"] in peers["properties"]["segment_id"]["enum"]:
            return {
                "tool": "compare_to_peers", "segment_id": c["segment_id"],
                "period_id": c["period_id"],
                "peer_definition_id": peers["properties"]["peer_definition_id"]["enum"][0],
            }
        return None

    def _step(self, request) -> dict:
        if not _tool_results(request) and "repair" not in request.messages[-2].content:
            call = self._tool_call(request)
            if call is not None:
                return {"kind": "call_tool", "tool_call": call}
        return {"kind": "final", "recommendation": self._final(request)}

    def _final(self, request) -> dict:
        evidence = _evidence(request)
        c, g = evidence["candidate"], evidence["answer_guide"]
        test = g["recommend_test"]
        decisions = g["allowed_decisions"]
        decision = (
            "recommend_test" if "recommend_test" in decisions and test
            else "investigate_first" if "investigate_first" in decisions
            else decisions[0]
        )
        options = list(g["observation_options"])
        for result in _tool_results(request):
            options.extend(result.get("new_observation_options", []))
        return {
            "origin": c["origin"],
            "candidate_id": c["candidate_id"],
            "decision": decision,
            "segment_id": c["segment_id"],
            "period_id": c["period_id"],
            "observation_statements": options[:2],
            "counter_evidence_fact_ids": c["counter_evidence_fact_ids"],
            "hypothesis_codes": ["traffic_mix_or_configuration_unverified"],
            "test_template_id": test["test_template_ids"][0]
            if decision == "recommend_test" else None,
            "assumption_ids": test["assumption_ids"] if decision == "recommend_test" else [],
            "scenario_ids": [],
            "prerequisite_codes": [],
            "risk_codes": [],
            "success_metric_keys": test["success_metric_keys"]
            if decision == "recommend_test" else ["profit_per_million_requests"],
            "confidence_in_observation": "medium",
            "overlap_group_ids": [],
            "merge_source_candidate_ids": [],
        }

    # Ranking and discovery.

    @staticmethod
    def _rank(request) -> dict:
        payload = json.loads(request.messages[1].content)
        chosen, banned = [], set()
        for r in payload["validated_recommendations"]:
            rid = r["recommendation_id"]
            if rid not in banned and len(chosen) < 10:
                chosen.append(rid)
                banned |= {x for pair in payload["conflicting_pairs"] if rid in pair for x in pair}
        return {"ranked_recommendation_ids": chosen, "merge_instructions": [], "summary_themes": []}

    def _explore(self, request) -> dict:
        schema = request.response_schema
        final = schema["properties"]["kind"].get("const") == "final"
        results = [
            json.loads(m.content) for m in request.messages[2:]
            if m.role == "user" and "untrusted_tool_evidence" in m.content
        ]
        if not results and not final:
            for variant in _variants(schema["$defs"].get("BreakdownArgs")):
                props = variant["properties"]
                if "supply_id" in props["by"]["enum"]:
                    return {"kind": "call_tool", "tool_call": {
                        "tool": "breakdown", "segment_id": props["segment_id"]["enum"][0],
                        "by": "supply_id", "period_id": props["period_id"]["enum"][0],
                        "limit": 5,
                    }}
        proposals = []
        fact_ids = schema["$defs"]["ExploreProposal"]["properties"].get("fact_ids", {})
        if results and "items" in fact_ids:
            facts = results[-1]["untrusted_tool_evidence"]["facts"]
            types = facts["types"]
            allowed = set(fact_ids["items"]["enum"])
            for row in facts["rows"]:
                fact_id, segment_id, period_id, type_index = row[:4]
                if (
                    fact_id in allowed and types[type_index][1] == "requests"
                    and len(proposals) < self.explore_proposals
                ):
                    proposals.append({
                        "family": DetectorFamily.WASTED_REQUESTS.value,
                        "segment_id": segment_id, "period_id": period_id,
                        "fact_ids": [fact_id],
                    })
        return {"kind": "final", "proposals": proposals}
