"""Real evidence, tools, reasoning and strict validation with no model/network."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contracts.config import EvidenceConfig, RunBudget, RunConfig
from contracts.enums import RunStatus
from contracts.ids import IdPrefix, make_id
from contracts.models import CandidateRecommendation, DailyTrendArgs
from llm import analysis
from llm.profiles import load_profile
from runner import CancelToken, _Progress, _run_model, default_stages, execute
from tests.unit.test_reasoning import FakeClient
from tests.unit.test_reasoning_explore import ExplorerRouter, proposals, tool_turn
from tests.unit.test_reasoning_tools import make_tool_context

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/ad_performance.csv"


def recommendation(candidate, fact_id):
    return CandidateRecommendation(
        origin=candidate["origin"],
        candidate_id=candidate["candidate_id"],
        decision="investigate_first",
        segment_id=candidate["segment_id"],
        period_id=candidate["period_id"],
        observation_statements=[{"template_id": "metric_value", "fact_ids": [fact_id]}],
        counter_evidence_fact_ids=candidate["counter_evidence_fact_ids"],
        hypothesis_codes=[],
        test_template_id=None,
        assumption_ids=[],
        scenario_ids=[],
        prerequisite_codes=[],
        risk_codes=[],
        success_metric_keys=[],
        confidence_in_observation="low",
        overlap_group_ids=candidate["overlap_group_ids"],
    )


def rank_response(request):
    payload = json.loads(request.messages[1].content)
    return {
        "ranked_recommendation_ids": [
            r["recommendation_id"] for r in payload["validated_recommendations"]
        ],
        "merge_instructions": [],
        "summary_themes": [],
    }


@pytest.mark.parametrize("repair", [False, True])
def test_real_fixture_tools_and_ranking_repair_hook_use_strict_validator(repair):
    seen = {}

    def baseline_tool(request):
        payload = json.loads(request.messages[1].content)["untrusted_evidence"]
        seen["pack"] = payload
        account = next(
            s
            for s in payload["untrusted_segment_labels"]
            if not s["filters"] and s["cohort_id"] == "full_input"
        )
        call = DailyTrendArgs(
            segment_id=account["segment_id"],
            period_id=payload["candidate"]["period_id"],
        )
        return {"kind": "call_tool", "tool_call": call.model_dump(mode="json")}

    def final_response(request):
        payload = seen["pack"]
        candidate = payload["candidate"]
        for row in payload["facts"]:
            fact = dict(zip(payload["fact_fields"], row, strict=True))
            fact["segment_id"] = payload["untrusted_segment_labels"][
                fact.pop("segment_index")
            ]["segment_id"]
            fact["period_id"] = payload["periods"][fact.pop("period_index")]["period_id"]
            fact.update(
                zip(
                    payload["fact_type_fields"],
                    payload["fact_types"][fact.pop("type_index")],
                    strict=True,
                )
            )
            if (
                fact["metric"] == "requests"
                and fact["kind"] == "measured"
                and fact["segment_id"] == candidate["segment_id"]
                and fact["period_id"] == candidate["period_id"]
            ):
                rec = recommendation(candidate, fact["fact_id"])
                payload = rec.model_dump(mode="json")
                return (
                    payload
                    if request.response_schema_name == "candidate_final"
                    else {"kind": "final", "recommendation": payload}
                )
        raise AssertionError("target requests fact missing")

    def invalid_response(request):
        response = final_response(request)
        response["recommendation"]["observation_statements"][0]["fact_ids"] = [
            "F_" + "0" * 16
        ]
        return response

    outputs = [
        baseline_tool,
        *([invalid_response] if repair else []),
        final_response,
        rank_response,
    ]
    client = FakeClient(outputs)
    stages = replace(
        default_stages(), create_client=lambda env, budget, profile: client
    )
    config = RunConfig(
        model_profile_id=load_profile().profile_id,
        evidence=EvidenceConfig(max_candidates=1),
        budget=RunBudget(explore_enabled=False),
    )
    outcome = execute(
        io.BytesIO(SAMPLE.read_bytes()),
        config,
        lambda event: None,
        stages=stages,
        env={},
    )
    assert outcome.status is RunStatus.FINAL
    assert len(outcome.run.recommendations) == 1
    assert len(outcome.run.ranking.ranked_recommendation_ids) == 1
    assert len(client.requests) == 3 + int(repair)  # no spurious ranking repair
    assert outcome.run.analyses[0].repairs == int(repair)
    tool = outcome.run.analyses[0].tool_evidence[0]
    assert (
        tool.tool_call.segment_id
        != outcome.run.recommendations[0].recommendation.segment_id
    )
    assert all(ref in outcome.run.bundle.facts for ref in tool.returned_fact_ids)
    assert outcome.run.bundle.validated_recommendations


def test_optional_exploration_uses_real_strict_validation_and_keeps_origin():
    ctx = make_tool_context()
    budget = RunBudget(explore_enabled=True, max_explore_proposals=1)
    discovery = ExplorerRouter(ctx)
    expected = analysis.explore_candidates(
        ctx.bundle,
        discovery,
        FakeClient([tool_turn(ctx), lambda request: proposals(discovery, 1)]),
        budget,
    )
    pack = expected.packs[0]
    rec = recommendation(
        pack.candidate.model_dump(mode="json"), pack.candidate.trigger_fact_ids[0]
    )
    rid = make_id(
        IdPrefix.RECOMMENDATION, ctx.run_namespace, rec.model_dump(mode="json")
    )
    client = FakeClient(
        [
            tool_turn(ctx),
            proposals(discovery, 1),
            {"kind": "final", "recommendation": rec.model_dump(mode="json")},
            rank_response,
        ]
    )
    stages = replace(
        default_stages(), create_client=lambda env, budget, profile: client
    )
    progress = _Progress(
        "run_" + "0" * 16, lambda event: None, CancelToken(), lambda: datetime.now(UTC)
    )
    result = _run_model(
        ctx.bundle,
        ctx.metrics,
        RunConfig(model_profile_id=load_profile().profile_id, budget=budget),
        stages,
        progress,
        {},
        lambda: 0,
    )
    assert result.status is RunStatus.FINAL
    assert result.ranking.ranked_recommendation_ids == [rid]
    assert result.recommendations[0].recommendation.origin == "model_proposed"
    assert result.bundle.candidates[rec.candidate_id].origin == "model_proposed"
    assert len(client.requests) == 4
