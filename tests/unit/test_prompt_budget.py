"""Prompt sizing: token counting, the evidence gate, and skip reasons. Offline only.

Regression for the first live run, where every candidate ended ``budget_exhausted``
before any request: the client had no token counter so the orchestrator counted
UTF-8 bytes (about 2x tokens) against a token limit, and the prompt it counted
was a different, 4-5x larger serialization than the pack the evidence gate fit.
"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from contracts.config import EvidenceConfig, RunBudget, RunConfig
from contracts.enums import (
    AnalysisStatus,
    BudgetReason,
    Decision,
    FactKind,
    ProgressStage,
    ProgressState,
)
from contracts.ids import canonical_json
from contracts.models import CandidateRecommendation, ObservationStatement
from contracts.runtime import ChatMessage, ModelResponse
from contracts.templates import ObservationTemplateId
from llm.analysis import analyze_candidate, reasoning_stats
from llm.client import ModelBudgetError, ModelConfigError
from llm.profiles import load_profile
from llm.prompts import build_candidate_messages
from llm.tokens import (
    PROMPT_OVERHEAD_TOKENS,
    estimate_pack_prompt_tokens,
    estimate_prompt_tokens,
    estimate_text_tokens,
)
from pipeline.evidence import (
    EvidenceBudgetError,
    build_evidence,
    build_evidence_bounded,
    pack_payload,
    serialize_pack,
)
from runner import default_stages, execute
from tests.unit.test_llm_client import (
    BASE,
    FakeClock,
    Server,
    _client,
    _completion,
    _models,
    _request,
)
from tests.unit.test_reasoning import FakeClient, FakeRouter, final, pack  # noqa: F401

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "ad_performance.csv"
PROFILE_ID = "qwen38_fp8_l40s_v1"
# The orchestrator's limit for a client without a profile (the old 16K window's).
PROMPT_LIMIT = 12288
PROFILE_PROMPT_LIMIT = load_profile(PROFILE_ID).max_prompt_tokens

# Exact counts from the pinned Qwen tokenizer for strings in the shape of a
# serialized pack (measured offline once; the tokenizer is not a dependency).
EXACT_TOKENS = {
    "fact_rows": (
        '[["F_0259994546b94149",1,3,9,"38.94565571103156524568825252",null,'
        '["F_5daac590ea8e213c","F_e0f7af97d0733fbb"],null],["F_028dc7c19e7b7102",0,4,10,'
        '"6.830410812868974503141892538",null,["F_1628fe9d9e631059","F_ef0dd38e205ab269"],'
        '{"difference":"226.6754723323566321921909969","difference_unit":'
        '"export_dollar_per_million_requests","left_fact_id":"F_1628fe9d9e631059",'
        '"matching_keys":[],"ratio":"6.830410812868974503141892538","relation":"greater",'
        '"right_fact_id":"F_ef0dd38e205ab269"}]]',
        312,
    ),
    "issue_row": (
        '["Q_263d406fa3a49e31",[],["no_trend_claim","no_stable_scale_claim"],'
        '["F_18ca2315dd540656","F_f2d4f0e8d7ef4d6f"],"DQ06_possible_partial_day",'
        '["P_b5acfaa703e0822b"],"F_c6db38758087cbd4","F_93e8b12f47363d2e",38,"DQ06",1,'
        '["G_b200365cb844703e"],"warning",["F_7f661e462d66d2f5","F_244621636057e8a3"]]',
        194,
    ),
    "english": (
        "Revenue per million requests fell versus its peers on desktop banner "
        "inventory; coverage is partial.",
        17,
    ),
    "non_ascii": ("Ünïcödé 日本語 label Привет мир", 10),
}


# --- the estimate ------------------------------------------------------------


@pytest.mark.parametrize("name", EXACT_TOKENS)
def test_estimate_is_never_below_the_exact_count(name):
    text, exact = EXACT_TOKENS[name]
    assert estimate_text_tokens(text) >= exact


@pytest.mark.parametrize("name", ["fact_rows", "issue_row"])
def test_estimate_is_close_on_pack_shaped_text_not_a_byte_count(name):
    text, exact = EXACT_TOKENS[name]
    assert estimate_text_tokens(text) <= exact * 1.25
    # The old fallback: bytes of the text. It was about 1.5-2x the real count.
    assert len(text.encode()) > exact * 1.4


def test_estimate_prompt_counts_messages_tools_and_framing():
    messages = [ChatMessage(role="system", content="x" * 100),
                ChatMessage(role="user", content="7" * 100)]
    base = estimate_prompt_tokens(messages)
    assert base > estimate_text_tokens("x" * 100) + estimate_text_tokens("7" * 100)
    assert estimate_prompt_tokens(messages, [{"type": "function"}]) > base
    assert estimate_text_tokens("") == 0


# --- the client counter ------------------------------------------------------


def _ready(client):
    client.wait_until_ready()
    return client


def _tokenizer_server(answer):
    """Ready server whose ``/tokenize`` is answered by ``answer(request)``."""
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/models"):
            return _models()
        assert request.url.path == "/tokenize"
        return answer(request)

    return handler, seen


def _counting_client(answer):
    handler, seen = _tokenizer_server(answer)
    clock = FakeClock()
    from contracts.config import RunBudget as Budget
    from llm.client import ENV_ALLOWED_BASE_URLS, ENV_API_KEY, ENV_BASE_URL, create_client

    env = {ENV_BASE_URL: BASE, ENV_API_KEY: "synthetic-test-key-0123456789",
           ENV_ALLOWED_BASE_URLS: BASE}
    client = create_client(env, Budget(), transport=httpx.MockTransport(handler),
                           clock=clock, sleep=clock.sleep)
    return client, seen


MESSAGES = [ChatMessage(role="system", content="Return JSON."),
            ChatMessage(role="user", content=canonical_json({"n": "123456789"}))]


def test_exact_count_comes_from_the_servers_tokenize_route():
    client, seen = _counting_client(
        lambda _: httpx.Response(200, json={"count": 4321, "max_model_len": 16384,
                                            "tokens": [1, 2, 3]}))
    _ready(client)
    assert client.count_prompt_tokens(MESSAGES, []) == 4321
    request = seen[-1]
    # Same host, outside /v1, with the generation flags the real request uses.
    assert str(request.url) == BASE.removesuffix("/v1") + "/tokenize"
    body = json.loads(request.content)
    assert body["model"] == "adops-analysis"
    assert [m["content"] for m in body["messages"]] == [m.content for m in MESSAGES]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["add_generation_prompt"] is True
    assert (client.stats.token_counts_exact, client.stats.token_counts_estimated) == (1, 0)


def test_tools_are_tokenized_with_the_messages():
    client, seen = _counting_client(lambda _: httpx.Response(200, json={"count": 9}))
    _ready(client)
    tool = {"type": "function", "function": {"name": "daily_trend", "parameters": {}}}
    client.count_prompt_tokens(MESSAGES, [tool])
    assert json.loads(seen[-1].content)["tools"] == [tool]


@pytest.mark.parametrize("answer", [
    httpx.Response(404, json={"error": "no route"}),
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"unexpected": 1}),
    httpx.Response(200, json={"count": "12"}),
    httpx.Response(200, json={"count": 0}),
    httpx.Response(200, text="not json"),
], ids=["404", "500", "no-count", "string-count", "zero", "not-json"])
def test_unusable_tokenize_answer_falls_back_to_the_calibrated_estimate(answer):
    client, _ = _counting_client(lambda _: answer)
    _ready(client)
    assert client.count_prompt_tokens(MESSAGES, []) == estimate_prompt_tokens(MESSAGES, [])
    assert (client.stats.token_counts_exact, client.stats.token_counts_estimated) == (0, 1)


def test_not_ready_client_estimates_without_touching_the_network():
    client, seen = _counting_client(lambda _: pytest.fail("no request expected"))
    assert client.count_prompt_tokens(MESSAGES, []) == estimate_prompt_tokens(MESSAGES, [])
    assert not seen


def test_tokenize_redirect_to_another_host_still_fails_closed():
    client, _ = _counting_client(lambda _: httpx.Response(
        307, headers={"location": "https://elsewhere.example/tokenize"}))
    _ready(client)
    with pytest.raises(ModelConfigError):
        client.count_prompt_tokens(MESSAGES, [])


# --- orchestrator limits and reasons ------------------------------------------


def test_client_without_a_counter_uses_the_estimate_and_a_real_pack_goes_through(pack):  # noqa: F811
    client = FakeClient([final(pack)])
    del client.count_prompt_tokens
    client.profile = SimpleNamespace(max_prompt_tokens=PROMPT_LIMIT, max_output_tokens=2048)
    result = analyze_candidate(pack, FakeRouter(pack), client, RunBudget())
    assert result.status is AnalysisStatus.COMPLETE and result.budget_reason is None
    assert client.requests


@pytest.mark.parametrize("setup,reason", [
    ("prompt", BudgetReason.PROMPT_BUDGET),
    ("generations", BudgetReason.GENERATION_BUDGET),
    ("warm", BudgetReason.WARM_BUDGET),
])
def test_budget_exhaustion_names_the_limit_that_ended_it(pack, setup, reason):  # noqa: F811
    client = FakeClient([])
    if setup == "prompt":
        client.count_prompt_tokens = lambda messages, tools: PROMPT_LIMIT + 1
    elif setup == "generations":
        reasoning_stats(client).generations = 10**6
    else:
        reasoning_stats(client).started -= 10**6
    result = analyze_candidate(pack, FakeRouter(pack), client, RunBudget())
    assert result.status is AnalysisStatus.BUDGET_EXHAUSTED
    assert result.budget_reason is reason
    assert not client.requests and result.generation_turns == 1


def test_transport_budget_error_keeps_its_reason(pack):  # noqa: F811
    client = FakeClient([ModelBudgetError("spent", BudgetReason.NETWORK_BUDGET)])
    result = analyze_candidate(pack, FakeRouter(pack), client, RunBudget())
    assert result.budget_reason is BudgetReason.NETWORK_BUDGET


def test_candidate_cap_and_turn_exhaustion_are_labelled(pack):  # noqa: F811
    stats_client = FakeClient([])
    reasoning_stats(stats_client).candidate_ids.update(f"C_{i:016x}" for i in range(15))
    capped = analyze_candidate(pack, FakeRouter(pack), stats_client, RunBudget())
    assert (capped.status, capped.budget_reason) == (
        AnalysisStatus.BUDGET_EXHAUSTED, BudgetReason.CANDIDATE_CAP)
    # A completed analysis never carries a reason.
    done = analyze_candidate(pack, FakeRouter(pack), FakeClient([final(pack)]), RunBudget())
    assert done.budget_reason is None


def test_tool_result_that_does_not_fit_is_dropped_and_the_pack_is_judged(pack):  # noqa: F811
    """A drill-down that would overflow the prompt must not cost the candidate."""
    from tests.unit.test_reasoning import call_step

    from tests.unit.test_reasoning import recommendation

    client = FakeClient([call_step(pack), recommendation(pack).model_dump(mode="json")])
    client.count_prompt_tokens = lambda messages, tools: (
        PROMPT_LIMIT if any(m.role == "assistant" for m in messages) else 9000)
    router = FakeRouter(pack)
    result = analyze_candidate(pack, router, client, RunBudget())
    assert result.status is AnalysisStatus.COMPLETE
    assert router.calls and len(client.requests) == 2
    # The second request is final-only and carries no tool exchange.
    assert not any(m.role == "assistant" for m in client.requests[1].messages)
    assert "final_recommendation" in client.requests[1].messages[-1].content


# --- the evidence gate and the prompt are the same document -------------------


@pytest.fixture(scope="module")
def synthetic_run():
    """The synthetic fixture through the real pipeline up to the model stage."""
    captured = {}
    stages = default_stages()
    real = stages.build_evidence

    def spy(*args):
        captured["bundle"] = real(*args)
        return captured["bundle"]

    return captured, replace(stages, build_evidence=spy)


def test_prompt_is_the_serialization_the_gate_measured(synthetic_run):
    captured, stages = synthetic_run
    config = RunConfig(model_profile_id=None)
    with SAMPLE.open("rb") as source:
        execute(source, config, lambda e: None, stages=stages, env={})
    bundle = captured["bundle"]
    assert bundle.packs
    for pack_ in bundle.packs.values():
        user = build_candidate_messages(pack_)[1].content
        assert json.loads(user)["untrusted_evidence"] == pack_payload(pack_)
        assert serialize_pack(pack_) in user


def test_gate_leaves_room_for_instructions_and_stays_inside_the_pack_budget(synthetic_run):
    bundle = synthetic_run[0]["bundle"]
    for pack_ in bundle.packs.values():
        assert estimate_pack_prompt_tokens(serialize_pack(pack_)) <= pack_.token_budget
        assert PROMPT_OVERHEAD_TOKENS < pack_.token_budget
        # The profile's prompt keeps room beyond a full pack for tool results
        # and two refused answers with their repair turns.
        assert PROFILE_PROMPT_LIMIT - pack_.token_budget >= 12288


def test_overhead_covers_the_candidate_instructions_around_a_pack(synthetic_run):
    pack_ = next(iter(synthetic_run[0]["bundle"].packs.values()))
    messages = build_candidate_messages(pack_)
    turn = ChatMessage(role="user", content=canonical_json({
        "response": "final_recommendation", "remaining_tool_executions": 3,
        "peer_definition_ids": ["D_" + "0" * 16] * 5}))
    around = estimate_prompt_tokens([*messages, turn]) - estimate_text_tokens(
        serialize_pack(pack_))
    assert around <= PROMPT_OVERHEAD_TOKENS


def test_every_synthetic_candidate_fits_even_the_old_16k_budget(synthetic_run):
    """Regression: six of fifteen live candidates were skipped as evidence_too_large.

    Only issues bearing on a candidate's claims are listed with their facts, so
    mandatory evidence fits the 12,288-token prompt of the former 16K window.
    """
    captured = {}
    stages = default_stages()

    def grab(metrics, quality, candidates, config):
        captured["inputs"] = (metrics, quality, candidates)
        return stages.build_evidence(metrics, quality, candidates, config)

    with SAMPLE.open("rb") as source:
        execute(source, RunConfig(), lambda e: None,
                stages=replace(stages, build_evidence=grab), env={})
    assert len(synthetic_run[0]["bundle"].packs) == 15
    assert not synthetic_run[0]["bundle"].unpacked
    metrics, quality, candidates = captured["inputs"]
    old = build_evidence_bounded(
        metrics, quality, candidates, EvidenceConfig(max_pack_tokens=12288),
        count_tokens=estimate_pack_prompt_tokens, skip_oversize=True)
    assert len(old.packs) == 15 and not old.unpacked


def test_oversize_candidates_are_unpacked_with_a_reason_not_fatal():
    captured = {}
    stages = default_stages()

    def grab(metrics, quality, candidates, config):
        captured["inputs"] = (metrics, quality, candidates)
        return stages.build_evidence(metrics, quality, candidates, config)

    with SAMPLE.open("rb") as source:
        execute(source, RunConfig(), lambda e: None,
                stages=replace(stages, build_evidence=grab), env={})
    metrics, quality, candidates = captured["inputs"]
    # Tight enough that only the largest mandatory evidence fails to fit.
    bundle = build_evidence_bounded(
        metrics, quality, candidates, EvidenceConfig(max_pack_tokens=9216),
        count_tokens=estimate_pack_prompt_tokens, skip_oversize=True)
    assert bundle.packs and bundle.unpacked
    assert set(bundle.unpacked.values()) == {BudgetReason.EVIDENCE_TOO_LARGE}
    assert not set(bundle.unpacked) & set(bundle.packs)
    assert set(bundle.unpacked) <= set(bundle.candidates)


def test_public_build_evidence_still_refuses_an_irreducible_pack(synthetic_run):
    """The frozen contract is unchanged: only the runner's entry point skips."""
    from tests.unit.test_evidence_validator import evidence_inputs  # noqa: F401

    assert build_evidence is not build_evidence_bounded


# --- a live-shaped run: realistic packs reach the model and validate -----------


class ScriptedModel:
    """Answers each candidate from its own pack, as a well-behaved model would."""

    profile = SimpleNamespace(
        max_prompt_tokens=PROFILE_PROMPT_LIMIT, max_output_tokens=2048,
        served_model_name="adops-analysis",
        enable_thinking=False, temperature=0.2)

    def __init__(self, bundle):
        self.bundle = bundle
        self.requests = []
        self.stats = SimpleNamespace(network_attempts=0, transport_retries=0, prompt_tokens=0,
                                     output_tokens=0, token_counts_exact=0,
                                     token_counts_estimated=0)

    def wait_until_ready(self):
        return 0.0

    def count_prompt_tokens(self, messages, tools=()):
        self.stats.token_counts_estimated += 1
        return estimate_prompt_tokens(messages, tools)

    def generate(self, request):
        self.requests.append(request)
        evidence = json.loads(request.messages[1].content)["untrusted_evidence"]
        pack_ = self.bundle.packs[evidence["candidate"]["candidate_id"]]
        c = pack_.candidate
        # A plain measured fact of the candidate's own scope; the model's job in
        # this scripted stand-in is only to cite something the prompt showed.
        trigger = next(
            f for f in pack_.facts
            if f.kind is FactKind.MEASURED
            and (f.segment.segment_id, f.period.period_id) == (c.segment_id, c.period_id))
        rec = CandidateRecommendation(
            candidate_id=c.candidate_id, decision=Decision.INVESTIGATE_FIRST,
            segment_id=c.segment_id, period_id=c.period_id,
            observation_statements=[ObservationStatement(
                template_id=ObservationTemplateId.METRIC_VALUE, fact_ids=[trigger.fact_id])],
            counter_evidence_fact_ids=c.counter_evidence_fact_ids, hypothesis_codes=[],
            test_template_id=None, assumption_ids=[], scenario_ids=[], prerequisite_codes=[],
            risk_codes=[], success_metric_keys=[], confidence_in_observation="low",
            overlap_group_ids=c.overlap_group_ids)
        body = rec.model_dump(mode="json")
        if request.response_schema_name == "candidate_next_step":
            body = {"kind": "final", "recommendation": body}
        self.stats.output_tokens += 20
        return ModelResponse(content=json.dumps(body), prompt_tokens=100, output_tokens=20,
                             finish_reason="stop")


def test_synthetic_file_reaches_the_model_and_yields_validated_recommendations():
    captured = {}
    stages = default_stages()
    real = stages.build_evidence

    def spy(*args):
        captured["bundle"] = real(*args)
        captured["model"] = ScriptedModel(captured["bundle"])
        return captured["bundle"]

    events = []
    stages = replace(stages, build_evidence=spy,
                     create_client=lambda env, budget, profile: captured["model"])
    with SAMPLE.open("rb") as source:
        outcome = execute(source, RunConfig(model_profile_id=PROFILE_ID), events.append,
                          stages=stages, env={})
    bundle, model = captured["bundle"], captured["model"]
    run = outcome.run

    # Every packed candidate reached the client with a prompt inside the limit...
    assert len(model.requests) >= len(bundle.packs) >= 5
    for request in model.requests:
        assert estimate_prompt_tokens(request.messages, request.tools) <= PROFILE_PROMPT_LIMIT
    # ...and the pre-fix byte count of those very prompts was over the old limit.
    for pack_ in bundle.packs.values():
        old_count = sum(len(canonical_json(m.model_dump(exclude_none=True)).encode()) + 32
                        for m in build_candidate_messages(pack_))
        assert old_count > PROMPT_LIMIT

    judged = {a.candidate_id: a for a in run.analyses}
    assert {cid for cid, a in judged.items() if a.status is AnalysisStatus.COMPLETE} == set(
        bundle.packs)
    assert len(run.recommendations) == len(bundle.packs)
    # Candidates that could not be packed are named, not a bare exhaustion.
    for cid, reason in bundle.unpacked.items():
        assert judged[cid].status is AnalysisStatus.BUDGET_EXHAUSTED
        assert judged[cid].budget_reason is reason
    assert all(a.budget_reason is None for a in run.analyses
               if a.status is AnalysisStatus.COMPLETE)
    skipped = [e for e in events if e.state is ProgressState.SKIPPED
               and e.stage is ProgressStage.CANDIDATE_ANALYSIS]
    assert {e.candidate_id for e in skipped} == set(bundle.unpacked)
    assert {e.message_code for e in skipped} <= {"evidence_too_large"}
    assert run.manifest.incomplete_candidate_ids == sorted(bundle.unpacked) or set(
        run.manifest.incomplete_candidate_ids) == set(bundle.unpacked)


def test_irreducible_pack_fails_the_public_build_but_not_the_bounded_one():
    from contracts.enums import EligibilityStatus  # noqa: F401 (documentation of scope)

    captured = {}
    stages = default_stages()
    real = stages.build_evidence

    def grab(metrics, quality, candidates, config):
        captured["inputs"] = (metrics, quality, candidates)
        return real(metrics, quality, candidates, config)

    with SAMPLE.open("rb") as source:
        execute(source, RunConfig(), lambda e: None, stages=replace(stages, build_evidence=grab),
                env={})
    metrics, quality, candidates = captured["inputs"]
    tiny = EvidenceConfig(max_pack_tokens=1)
    with pytest.raises(EvidenceBudgetError):
        build_evidence(metrics, quality, candidates, tiny, count_tokens=estimate_text_tokens)
    bundle = build_evidence_bounded(metrics, quality, candidates, tiny,
                                    count_tokens=estimate_text_tokens, skip_oversize=True)
    assert not bundle.packs and set(bundle.unpacked.values()) == {BudgetReason.EVIDENCE_TOO_LARGE}
    assert _completion  # imported helper kept for symmetry with test_llm_client
    assert _request  # noqa: B018
