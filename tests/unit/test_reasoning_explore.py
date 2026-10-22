"""Optional discovery is capped, tool-grounded, labelled and validated normally."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from contracts.config import RunBudget
from contracts.enums import AnalysisStatus, DetectorFamily, ToolName
from contracts.models import BreakdownArgs, CandidateRecommendation
from llm.analysis import analyze_candidate, explore_candidates
from llm.tools import breakdown, extend_bundle
from pipeline.metrics import full_period, segment_id_for
from tests.unit.test_reasoning import FakeClient
from tests.unit.test_reasoning_tools import make_tool_context


@pytest.fixture
def ctx():
    return make_tool_context()


class ExplorerRouter:
    allowed_tools = frozenset(ToolName)

    def __init__(self, ctx):
        self.ctx, self.calls = ctx, []
        self.ev = None

    def dispatch_explore(self, call):
        self.calls.append(call)
        self.ev = breakdown(
            self.ctx, call.segment_id, call.by, call.period_id, call.limit
        )
        self.ctx = replace(self.ctx, bundle=extend_bundle(self.ctx.bundle, self.ev))
        return self.ev

    def dispatch(self, cid, call):
        raise AssertionError("judgment in this test does not need another tool")


def tool_turn(ctx):
    return {
        "kind": "call_tool",
        "tool_call": BreakdownArgs(
            segment_id=segment_id_for(ctx.run_namespace, {}),
            by="supply_id",
            period_id=full_period(ctx.metrics).period_id,
            limit=3,
        ).model_dump(mode="json"),
    }


def proposals(router, n=3):
    facts = [
        router.ctx.bundle.facts[r]
        for r in router.ev.returned_fact_ids
        if router.ctx.bundle.facts[r].metric.value == "requests"
        and router.ctx.bundle.facts[r].segment.cohort_id == "full_input"
    ][:n]
    return {
        "kind": "final",
        "proposals": [
            {
                "family": DetectorFamily.WASTED_REQUESTS,
                "segment_id": f.segment.segment_id,
                "period_id": f.period.period_id,
                "fact_ids": [f.fact_id],
            }
            for f in facts
        ],
    }


def test_explore_disabled_no_calls(ctx):
    client, router = FakeClient([]), ExplorerRouter(ctx)
    result = explore_candidates(
        ctx.bundle, router, client, RunBudget(explore_enabled=False)
    )
    assert result.status == "disabled" and not result.packs
    assert not client.requests and not router.calls


def test_explore_cap_tool_facts_origin_and_same_validation_path(ctx):
    router = ExplorerRouter(ctx)
    client = FakeClient([tool_turn(ctx), lambda r: proposals(router)])
    result = explore_candidates(
        ctx.bundle,
        router,
        client,
        RunBudget(explore_enabled=True, max_explore_proposals=2),
    )
    assert result.status == "complete" and len(result.packs) == 2
    assert len(client.requests) == 2 and len(router.calls) == 1
    assert all(p.candidate.origin == "model_proposed" for p in result.packs)
    assert all(
        set(p.candidate.trigger_fact_ids) <= set(router.ev.returned_fact_ids)
        for p in result.packs
    )
    assert not ctx.bundle.candidates
    pack = result.packs[0]
    router.seen = []
    router.validate_analysis = lambda output: (
        router.seen.append(output) or SimpleNamespace(accepted=True)
    )
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
    judged = analyze_candidate(
        pack,
        router,
        FakeClient([{"kind": "final", "recommendation": rec.model_dump(mode="json")}]),
        RunBudget(explore_enabled=True),
    )
    assert judged.status is AnalysisStatus.COMPLETE
    assert router.seen[0] == judged and judged.recommendation.origin == "model_proposed"


def test_explore_more_than_three_is_rejected_not_bypassed(ctx):
    router = ExplorerRouter(ctx)

    def too_many(request):
        result = proposals(router, 1)
        result["proposals"] *= 4
        return result

    client = FakeClient([tool_turn(ctx), too_many])
    result = explore_candidates(
        ctx.bundle, router, client, RunBudget(explore_enabled=True)
    )
    assert result.status == "failed_optional" and not result.packs
    assert result.bundle is ctx.bundle
    with pytest.raises(ValueError):
        RunBudget(max_explore_proposals=4)


def test_explore_cannot_use_existing_but_unreturned_fact(ctx):
    router = ExplorerRouter(ctx)

    def invented(request):
        answer = proposals(router, 1)
        answer["proposals"][0]["fact_ids"] = ["F_" + "f" * 16]
        return answer

    result = explore_candidates(
        ctx.bundle,
        router,
        FakeClient([tool_turn(ctx), invented]),
        RunBudget(explore_enabled=True),
    )
    assert result.status == "failed_optional" and not result.packs


def test_explore_tools_have_separate_fixed_budget_and_cache(ctx):
    router = ExplorerRouter(ctx)
    result = explore_candidates(
        ctx.bundle,
        router,
        FakeClient([tool_turn(ctx)]),
        RunBudget(explore_enabled=True, max_explore_tool_calls=0),
    )
    assert result.status == "failed_optional" and not router.calls
    router = ExplorerRouter(ctx)
    client = FakeClient(
        [tool_turn(ctx), tool_turn(ctx), lambda r: proposals(router, 1)]
    )
    result = explore_candidates(
        ctx.bundle, router, client, RunBudget(explore_enabled=True)
    )
    assert result.status == "complete" and len(router.calls) == 1


def test_explore_final_turn_cannot_request_another_tool(ctx):
    router = ExplorerRouter(ctx)
    client = FakeClient([tool_turn(ctx)] * 4)
    result = explore_candidates(
        ctx.bundle, router, client, RunBudget(explore_enabled=True)
    )
    assert result.status == "failed_optional" and len(client.requests) == 4


@pytest.mark.parametrize("bad", ["null", "{}", "not json", RuntimeError("offline")])
def test_explore_failure_is_optional_and_does_not_change_rule_results(ctx, bad):
    result = explore_candidates(
        ctx.bundle,
        ExplorerRouter(ctx),
        FakeClient([bad]),
        RunBudget(explore_enabled=True),
    )
    assert result.status == "failed_optional" and result.bundle is ctx.bundle
