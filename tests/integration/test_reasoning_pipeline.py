"""Reasoning integration against real analytics/client contracts and a mocked lane C."""

from dataclasses import replace
from datetime import UTC, datetime

from contracts.config import RunBudget, RunConfig
from contracts.enums import RunStatus
from contracts.ids import IdPrefix, make_id
from contracts.models import (
    CandidateAnalysis,
    CandidateRecommendation,
    RankingIR,
    ValidatedRecommendation,
    ValidationAccepted,
)
from llm import analysis
from llm.profiles import load_profile
from runner import CancelToken, _Progress, _run_model, default_stages
from tests.unit.test_reasoning import FakeClient
from tests.unit.test_reasoning_explore import ExplorerRouter, proposals, tool_turn
from tests.unit.test_reasoning_tools import make_tool_context


def test_explore_flows_through_identical_judgment_validator_and_ranking():
    ctx = make_tool_context()
    budget = RunBudget(explore_enabled=True, max_explore_proposals=1)
    # Derive expected proposal from the deterministic tool, not copied constants.
    discovery = ExplorerRouter(ctx)
    expected = analysis.explore_candidates(
        ctx.bundle,
        discovery,
        FakeClient([tool_turn(ctx), lambda request: proposals(discovery, 1)]),
        budget,
    )
    pack = expected.packs[0]
    rec = CandidateRecommendation(
        origin="model_proposed",
        candidate_id=pack.candidate.candidate_id,
        decision="investigate_first",
        segment_id=pack.candidate.segment_id,
        period_id=pack.candidate.period_id,
        observation_statements=[],
        counter_evidence_fact_ids=pack.candidate.counter_evidence_fact_ids,
        hypothesis_codes=[],
        test_template_id=None,
        assumption_ids=[],
        scenario_ids=[],
        prerequisite_codes=[],
        risk_codes=[],
        success_metric_keys=[],
        confidence_in_observation="low",
        overlap_group_ids=[],
    )
    rid = make_id(IdPrefix.RECOMMENDATION, ctx.run_namespace, rec.candidate_id)
    client = FakeClient(
        [
            tool_turn(ctx),
            proposals(discovery, 1),
            {"kind": "final", "recommendation": rec.model_dump(mode="json")},
            {
                "ranked_recommendation_ids": [rid],
                "merge_instructions": [],
                "summary_themes": [],
            },
        ]
    )
    seen = []

    def validator(output, bundle):
        seen.append(output)
        if isinstance(output, CandidateAnalysis):
            assert bundle.candidates[output.candidate_id].origin == "model_proposed"
            assert output.recommendation.origin == "model_proposed"
            assert all(f.fact_id in bundle.facts for f in pack.facts)
            return ValidationAccepted(
                value=ValidatedRecommendation(
                    recommendation_id=rid,
                    run_namespace=bundle.run_namespace,
                    recommendation=output.recommendation,
                )
            )
        assert isinstance(output, RankingIR)
        return ValidationAccepted(value=output)

    stages = default_stages()
    stages = replace(
        stages, validate_analysis=validator, create_client=lambda env, b, p: client
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
    assert (
        len(result.recommendations) == 1
        and result.recommendations[0].recommendation.origin == "model_proposed"
    )
    assert result.bundle.candidates[rec.candidate_id].origin == "model_proposed"
    assert (
        sum(isinstance(o, CandidateAnalysis) for o in seen) == 2
    )  # repair hook + runner gate
    assert sum(isinstance(o, RankingIR) for o in seen) == 2
    assert len(client.requests) == 4
    assert result.model_call_counts == {
        "generations": 4,
        "ranking_calls": 1,
        "invalid_outputs": 0,
    }
