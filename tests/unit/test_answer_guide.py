"""Answer guide, narrowed schema and named repairs, offline on the synthetic file.

Regression for the live run after the prompt-budget fix: 8 of 15 candidates
were answered but refused after the one repair (their codes were not stored),
and 6 were skipped as ``evidence_too_large``. Replaying plausible answers
against the strict validator showed the refusals came from rules the model was
never shown: a cited scenario's prerequisite/limitation labels, the ordered
fact roles of a statement, copying overlap groups and counter-evidence
exactly, and decisions a quality restriction blocks. One rule was a plain
false positive: a scenario shared by other same-scope candidates could never
be cited.
"""

import io
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from pydantic import Field, ValidationError, create_model

from contracts.config import (
    AnalyticsConfig,
    DetectorConfig,
    EvidenceConfig,
    InputConfig,
    QualityConfig,
    RunBudget,
    RunConfig,
)
from contracts.enums import (
    AnalysisStatus,
    Confidence,
    Decision,
    DetectorFamily,
    Dimension,
    MetricKey,
    Relation,
    RunStatus,
)
from contracts.enums import ValidationErrorCode as Code
from contracts.models import (
    CandidateAnalysis,
    CandidateRecommendation,
    CompareToPeersArgs,
    ObservationStatement,
    RankingIR,
)
from contracts.templates import HypothesisCode
from contracts.templates import ObservationTemplateId as Template
from llm.analysis import explore_candidates
from llm.answer import attach_code_fields, final_schema, repair_errors, step_schema
from llm.profiles import load_profile
from llm.ranking import RankingError, check_ranking
from llm.tokens import estimate_pack_prompt_tokens
from llm.validator import _Validator, validate_analysis
from pipeline.detectors import run_detection
from pipeline.evidence import DETAILED_ISSUES_PER_RULE, build_evidence_bounded
from pipeline.load import load_csv
from pipeline.metrics import aggregate
from pipeline.quality import assess_quality, refine_quality
from report.bundle import read_bundle, write_bundle
from runner import default_stages, execute
from tests.unit.test_reasoning import FakeClient

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures/synthetic/ad_performance.csv"


@pytest.fixture(scope="module")
def bundle():
    data = load_csv(io.BytesIO(SAMPLE.read_bytes()), InputConfig())
    initial = assess_quality(data, QualityConfig())
    metrics = aggregate(data, initial, AnalyticsConfig())
    candidates = list(run_detection(metrics, initial, DetectorConfig()).candidates)
    quality = refine_quality(data, metrics, candidates, initial, QualityConfig())
    # The runner's own gate: the calibrated estimate, oversize skipped not fatal.
    return build_evidence_bounded(
        metrics,
        quality,
        candidates,
        EvidenceConfig(),
        count_tokens=estimate_pack_prompt_tokens,
        skip_oversize=True,
    )


def guided(pack, decision=None, *, options=2, scenario=True):
    """The answer of a model that follows the pack's answer guide."""
    g, c = pack.answer_guide, pack.candidate
    decision = decision or g.allowed_decisions[0]
    labels = g.scenario_labels[0] if scenario and g.scenario_labels else None
    test = g.recommend_test if decision is Decision.RECOMMEND_TEST else None
    assumptions = [
        *(labels.assumption_ids if labels else []),
        *(test.assumption_ids if test else []),
    ]
    return CandidateRecommendation(
        origin=c.origin,
        candidate_id=c.candidate_id,
        decision=decision,
        segment_id=c.segment_id,
        period_id=c.period_id,
        observation_statements=g.observation_options[:options],
        counter_evidence_fact_ids=c.counter_evidence_fact_ids,
        hypothesis_codes=[HypothesisCode.TRAFFIC_MIX_OR_CONFIGURATION_UNVERIFIED],
        test_template_id=test.test_template_ids[0] if test else None,
        assumption_ids=list(dict.fromkeys(assumptions)),
        scenario_ids=[labels.scenario_id] if labels else [],
        prerequisite_codes=labels.prerequisite_codes if labels else [],
        risk_codes=labels.risk_codes if labels else [],
        success_metric_keys=test.success_metric_keys
        if test
        else [MetricKey.PROFIT_PER_MILLION_REQUESTS],
        confidence_in_observation=Confidence.MEDIUM,
        overlap_group_ids=c.overlap_group_ids,
    )


def edit(rec, **fields):
    """A changed answer, re-validated as the contract would parse it."""
    return CandidateRecommendation.model_validate({**rec.model_dump(), **fields})


def verdict(bundle, rec):
    return validate_analysis(
        CandidateAnalysis(
            candidate_id=rec.candidate_id,
            status=AnalysisStatus.COMPLETE,
            recommendation=rec,
            tool_evidence=[],
            generation_turns=1,
            repairs=0,
        ),
        bundle,
    )


def schemas(pack):
    options = list(pack.answer_guide.observation_options)
    periods = {p.period_id for p in pack.periods}
    return final_schema(pack, options), step_schema(pack, options, pack.segments, periods, True)


def admitted(schema, document):
    return jsonschema.Draft202012Validator(schema).is_valid(document)


def test_every_candidate_is_packed_with_a_guide_matching_the_validator(bundle):
    assert len(bundle.packs) == 15 and not bundle.unpacked
    for pack in bundle.packs.values():
        g = pack.answer_guide
        assert g is not None and g.observation_options
        # The guide states exactly the restrictions the validator applies.
        assert set(g.effective_restrictions) == _Validator(bundle).restrictions(
            pack.candidate
        )
        listed = Counter(i.rule_id for i in pack.quality_issues)
        assert max(listed.values(), default=0) <= DETAILED_ISSUES_PER_RULE
        for summary in g.other_quality_issues:
            assert set(summary.inference_restrictions) <= set(g.effective_restrictions)


def test_every_observation_option_is_accepted_by_the_strict_validator(bundle):
    checked = 0
    for pack in bundle.packs.values():
        for option in pack.answer_guide.observation_options:
            rec = edit(guided(pack), observation_statements=[option])
            result = verdict(bundle, rec)
            assert result.accepted, (option, [e.code for e in result.errors])
            checked += 1
    assert checked >= 15 * 5


def test_guide_following_answers_validate_for_every_candidate_and_decision(bundle):
    offered = Counter()
    for pack in bundle.packs.values():
        final, step = schemas(pack)
        for decision in pack.answer_guide.allowed_decisions:
            offered[decision] += 1
            rec = guided(pack, decision)
            result = verdict(bundle, rec)
            assert result.accepted, (decision, [(e.code, e.path) for e in result.errors])
            # The model leaves overlap groups to code, which attaches them.
            document = rec.model_dump(mode="json") | {"overlap_group_ids": []}
            assert admitted(final, document)
            assert admitted(step, {"kind": "final", "recommendation": document})
            assert attach_code_fields(
                CandidateRecommendation.model_validate(document), pack
            ) == rec
    # Some candidates may be tested; restricted ones (scale winners here) may not.
    assert offered[Decision.RECOMMEND_TEST] and offered[Decision.RECOMMEND_TEST] < 15
    assert offered[Decision.INVESTIGATE_FIRST] == offered[Decision.REJECT] == 15


def test_live_failure_classes_are_refused_and_now_prevented_or_named(bundle):
    packs = list(bundle.packs.values())
    pack = next(p for p in packs if p.scenarios and p.candidate.overlap_group_ids)
    final, _ = schemas(pack)
    base = guided(pack)
    assert verdict(bundle, base).accepted

    def refused(rec, code):
        result = verdict(bundle, rec)
        assert not result.accepted and code in {e.code for e in result.errors}
        return result.errors

    # Prevented by the narrowed schema: the grammar cannot emit these.
    comparison = next(
        o
        for o in pack.answer_guide.observation_options
        if o.template_id is Template.METRIC_VS_BASELINE
    )
    swapped = ObservationStatement(
        template_id=comparison.template_id,
        fact_ids=[comparison.fact_ids[1], comparison.fact_ids[0], comparison.fact_ids[2]],
    )
    rec = edit(base, observation_statements=[swapped])
    refused(rec, Code.WRONG_ROLE)
    assert not admitted(final, rec.model_dump(mode="json") | {"overlap_group_ids": []})
    # Overlap groups are code-attached; the validator still refuses an answer
    # without them, so the non-additivity protection does not rest on the model.
    dropped = edit(base, overlap_group_ids=[])
    refused(dropped, Code.OVERLAP_CONFLICT)
    assert admitted(final, dropped.model_dump(mode="json"))
    assert not admitted(final, base.model_dump(mode="json"))
    assert verdict(bundle, attach_code_fields(dropped, pack)).accepted
    with_counter = next(p for p in packs if p.candidate.counter_evidence_fact_ids)
    rec = edit(guided(with_counter), counter_evidence_fact_ids=[])
    refused(rec, Code.MISSING_COUNTER_EVIDENCE)
    assert not admitted(schemas(with_counter)[0], rec.model_dump(mode="json"))
    scale = next(
        p for p in packs if p.candidate.family is DetectorFamily.SCALE_WINNER
    )
    assert Decision.RECOMMEND_TEST not in scale.answer_guide.allowed_decisions
    blocked = edit(
        guided(scale),
        decision=Decision.RECOMMEND_TEST,
        test_template_id=scale.permitted_test_templates[0],
    )
    refused(blocked, Code.QUALITY_GATE_VIOLATION)
    assert not admitted(schemas(scale)[0], blocked.model_dump(mode="json"))

    # Admitted by the schema but named exactly by the repair turn: applying the
    # named fix gives an accepted answer.
    unlabelled = edit(base, prerequisite_codes=[], risk_codes=[])
    errors = refused(unlabelled, Code.SCENARIO_MISMATCH)
    labels = next(
        e["fix"]["use"] for e in repair_errors(errors, pack) if e["path"] == "scenario_ids"
    )
    fixed = edit(
        unlabelled,
        prerequisite_codes=labels[0]["prerequisite_codes"],
        risk_codes=labels[0]["risk_codes"],
    )
    assert verdict(bundle, fixed).accepted
    testable = next(
        p for p in packs if p.answer_guide.recommend_test is not None
    )
    bare = edit(
        guided(testable, Decision.RECOMMEND_TEST, scenario=False),
        assumption_ids=[],
        success_metric_keys=[],
    )
    errors = refused(bare, Code.WRONG_ROLE)
    use = next(
        e["fix"]["use"] for e in repair_errors(errors, testable) if e["path"] == "assumption_ids"
    )
    fixed = edit(
        bare,
        assumption_ids=use["assumption_ids"],
        success_metric_keys=use["success_metric_keys"],
    )
    assert verdict(bundle, fixed).accepted


def test_scenario_shared_with_other_candidates_can_be_cited(bundle):
    """False positive: a scenario's groups include other same-scope candidates' groups.

    The recommendation must declare exactly its candidate's groups, and may not
    declare a group without its candidate; the old rule also demanded every
    scenario group, so no answer could cite such a scenario.
    """
    pack = next(
        p
        for p in bundle.packs.values()
        for s in p.scenarios
        if set(s.overlap_group_ids) - set(p.candidate.overlap_group_ids)
    )
    scenario, c = pack.scenarios[0], pack.candidate
    foreign = sorted(set(scenario.overlap_group_ids) - set(c.overlap_group_ids))
    assert all(c.candidate_id not in bundle.overlap_groups[g].candidate_ids for g in foreign)
    rec = guided(pack)
    assert rec.scenario_ids == [scenario.scenario_id]
    assert verdict(bundle, rec).accepted
    # Declaring the foreign group, as the old rule required, is still refused.
    declared = edit(rec, overlap_group_ids=[*c.overlap_group_ids, *foreign])
    assert not verdict(bundle, declared).accepted
    # Its own groups are still required.
    if c.overlap_group_ids:
        dropped = edit(rec, overlap_group_ids=c.overlap_group_ids[1:])
        assert Code.OVERLAP_CONFLICT in {e.code for e in verdict(bundle, dropped).errors}


def _run(outputs, budget=RunBudget(explore_enabled=False), max_candidates=1):
    client = FakeClient(outputs)
    stages = replace(default_stages(), create_client=lambda env, budget, profile: client)
    config = RunConfig(
        model_profile_id=load_profile().profile_id,
        evidence=EvidenceConfig(max_candidates=max_candidates),
        budget=budget,
    )
    outcome = execute(
        io.BytesIO(SAMPLE.read_bytes()), config, lambda e: None, stages=stages, env={}
    )
    return outcome, client


def _evidence(request):
    return json.loads(request.messages[1].content)["untrusted_evidence"]


def _final_from_guide(request, extra=None):
    evidence = _evidence(request)
    c, g = evidence["candidate"], evidence["answer_guide"]
    rec = {
        "origin": c["origin"],
        "candidate_id": c["candidate_id"],
        "decision": "investigate_first",
        "segment_id": c["segment_id"],
        "period_id": c["period_id"],
        "observation_statements": [extra or g["observation_options"][0]],
        "counter_evidence_fact_ids": c["counter_evidence_fact_ids"],
        "hypothesis_codes": [],
        "test_template_id": None,
        "assumption_ids": [],
        "scenario_ids": [],
        "prerequisite_codes": [],
        "risk_codes": [],
        "success_metric_keys": [],
        "confidence_in_observation": "low",
        "overlap_group_ids": [],
        "merge_source_candidate_ids": [],
    }
    if request.response_schema_name == "candidate_final":
        return rec
    return {"kind": "final", "recommendation": rec}


def _rank(request):
    """A ranking that keeps the first of each conflicting pair, as instructed."""
    payload = json.loads(request.messages[1].content)
    chosen, banned = [], set()
    for r in payload["validated_recommendations"]:
        rid = r["recommendation_id"]
        if rid not in banned and len(chosen) < 10:
            chosen.append(rid)
            banned |= {x for pair in payload["conflicting_pairs"] if rid in pair for x in pair}
    return {"ranked_recommendation_ids": chosen, "merge_instructions": [], "summary_themes": []}


def test_refusals_are_stored_in_the_bundle_and_repair_shows_the_answer(tmp_path):
    def unknown_fact(request):
        answer = _final_from_guide(request)
        rec = answer.get("recommendation", answer)
        rec["observation_statements"] = [
            {"template_id": "metric_value", "fact_ids": ["F_" + "0" * 16]}
        ]
        return answer

    outcome, client = _run([unknown_fact, _final_from_guide, _rank])
    assert outcome.status is RunStatus.FINAL
    (analysis,) = outcome.run.analyses
    assert analysis.status is AnalysisStatus.COMPLETE and analysis.repairs == 1
    # The repair turn carries the refused answer and the named error.
    repair = client.requests[1].messages
    assert repair[-3].role == "assistant"
    errors = json.loads(repair[-2].content)["errors"]
    assert any(
        e["code"] == "unknown_reference"
        and e["path"].startswith("observation_statements")
        and e["fix"]["instruction"]
        for e in errors
    )
    saved = read_bundle(write_bundle(outcome, tmp_path))
    (stored,) = saved.run.analyses
    (attempt,) = stored.rejected_attempts
    assert attempt.attempt == 1
    assert Code.UNKNOWN_REFERENCE in {e.code for e in attempt.errors}
    assert {e.ref for e in attempt.errors} >= {"F_" + "0" * 16}


def test_rejected_candidates_keep_every_refused_attempt():
    def wrong(request):
        answer = _final_from_guide(request)
        rec = answer.get("recommendation", answer)
        rec["observation_statements"] = [
            {"template_id": "metric_value", "fact_ids": ["F_" + "0" * 16]}
        ]
        return answer

    outcome, client = _run([wrong, wrong, wrong])
    (analysis,) = outcome.run.analyses
    assert analysis.status is AnalysisStatus.REJECTED_INVALID
    # The correction repeated the named error, so no third generation is spent.
    assert [a.attempt for a in analysis.rejected_attempts] == [1, 2]
    assert len(client.requests) == 2


def test_a_peer_comparison_adds_observation_options_the_model_may_cite():
    seen = {}

    def respond(request):
        if request.response_schema_name == "recommendation_ranking":
            return _rank(request)
        evidence = _evidence(request)
        c = evidence["candidate"]
        last = json.loads(request.messages[-2].content)
        if "untrusted_tool_evidence" in last:
            new = last.get("new_observation_options", [])
            seen.setdefault("new", []).extend(new)
            if not new:
                return _final_from_guide(request)
            items = request.response_schema["$defs"]["CandidateRecommendation"][
                "properties"
            ]["observation_statements"]["items"]["enum"]
            assert new[0] in items
            seen["cited"] = new[0]
            seen["options"] = evidence["answer_guide"]["observation_options"]
            return _final_from_guide(request, extra=new[0])
        if c["family"] == "combination_outlier" and "cited" not in seen:
            turn = json.loads(request.messages[-1].content)
            call = CompareToPeersArgs(
                segment_id=c["segment_id"],
                period_id=c["period_id"],
                peer_definition_id=turn["peer_definition_ids"][0],
            )
            return {"kind": "call_tool", "tool_call": call.model_dump(mode="json")}
        return _final_from_guide(request)

    outcome, _ = _run([respond] * 40, max_candidates=15)
    assert outcome.run.ranking is not None
    assert "cited" in seen and seen["cited"] not in seen["options"]
    assert seen["cited"]["template_id"] == "metric_vs_baseline"
    cited = [
        r.recommendation
        for r in outcome.run.recommendations
        if seen["cited"]
        in [o.model_dump(mode="json") for o in r.recommendation.observation_statements]
    ]
    assert cited, "the tool-raised option was cited and passed strict validation"


def test_exploration_packs_are_shrunk_and_guided_like_rule_packs():
    from tests.unit.test_reasoning_explore import ExplorerRouter, proposals, tool_turn
    from tests.unit.test_reasoning_tools import make_tool_context

    ctx = make_tool_context()
    router = ExplorerRouter(ctx)
    client = FakeClient([tool_turn(ctx), lambda request: proposals(router, 2)])
    result = explore_candidates(ctx.bundle, router, client, RunBudget(explore_enabled=True))
    assert result.status == "complete" and result.packs
    # Before any tool result nothing can be proposed; afterwards only the
    # full-input facts the tool returned can be cited.
    first, second = (r.response_schema for r in client.requests)
    assert first["properties"]["proposals"]["maxItems"] == 0
    cited = second["$defs"]["ExploreProposal"]["properties"]["fact_ids"]["items"]["enum"]
    assert cited and set(cited) <= set(router.ev.returned_fact_ids)
    for pack in result.packs:
        g = pack.answer_guide
        assert g is not None and g.observation_options
        # Exploratory evidence permits investigation, never a test claim.
        assert Decision.RECOMMEND_TEST not in g.allowed_decisions
        assert set(g.effective_restrictions) == _Validator(result.bundle).restrictions(
            pack.candidate
        )
        ids = {f.fact_id for f in pack.facts}
        assert {i.issue_id for i in pack.quality_issues} <= set(
            pack.candidate.quality_issue_ids
        )
        for issue in pack.quality_issues:
            assert {*issue.measured_fact_ids, *issue.threshold_fact_ids} <= ids
        for option in g.observation_options:
            rec = edit(guided(pack), observation_statements=[option])
            assert verdict(result.bundle, rec).accepted


def test_ranking_is_shown_exactly_the_pairs_its_check_refuses():
    seen = {}

    def respond(request):
        if request.response_schema_name != "recommendation_ranking":
            return _final_from_guide(request)
        seen["pairs"] = json.loads(request.messages[1].content)["conflicting_pairs"]
        seen["schema"] = request.response_schema
        return _rank(request)

    outcome, _ = _run([respond] * 40, max_candidates=15)
    recs, facts = list(outcome.run.recommendations), outcome.run.bundle.facts
    assert len(recs) == 15 and seen["pairs"]
    assert outcome.run.ranking is not None and outcome.status is RunStatus.FINAL
    listed = {frozenset(p) for p in seen["pairs"]}
    for i, a in enumerate(recs):
        for b in recs[i + 1 :]:
            pair = RankingIR(
                ranked_recommendation_ids=[a.recommendation_id, b.recommendation_id],
                merge_instructions=[],
                summary_themes=[],
            )
            try:
                check_ranking(pair, recs, facts)
                refused = False
            except RankingError:
                refused = True
            assert refused == (frozenset((a.recommendation_id, b.recommendation_id)) in listed)
    ids = seen["schema"]["properties"]["ranked_recommendation_ids"]["items"]["enum"]
    assert set(ids) == {r.recommendation_id for r in recs}


@pytest.mark.parametrize("fixed_breakdown", [False, True])
def test_a_refused_tool_call_is_reported_to_the_model_not_fatal(fixed_breakdown):
    """Live regression: 7 of 15 candidates were lost when the model asked for
    peers of a slice that does not fix every dimension; the tool's refusal
    ended the whole candidate as a pipeline error."""
    events, seen = [], {}

    def respond(request):
        if request.response_schema_name == "recommendation_ranking":
            return _rank(request)
        evidence = _evidence(request)
        c = evidence["candidate"]
        last = json.loads(request.messages[-2].content)
        if "tool_refused" in last:
            seen["refused"], seen["candidate"] = last["tool_refused"], c["candidate_id"]
            return _final_from_guide(request)
        own = next(
            s for s in evidence["untrusted_segment_labels"]
            if s["segment_id"] == c["segment_id"]
        )
        if (
            "refused" not in seen
            and len(own["filters"]) < 5
            and request.response_schema_name == "candidate_next_step"
        ):
            turn = json.loads(request.messages[-1].content)
            seen["schema"] = request.response_schema
            if fixed_breakdown:
                from contracts.models import BreakdownArgs

                call = BreakdownArgs(
                    segment_id=c["segment_id"], period_id=c["period_id"],
                    by=next(iter(own["filters"])), limit=5,
                )
            else:
                call = CompareToPeersArgs(
                    segment_id=c["segment_id"],
                    period_id=c["period_id"],
                    peer_definition_id=turn["peer_definition_ids"][0],
                )
            seen["bad_call"] = call.model_dump(mode="json")
            return {"kind": "call_tool", "tool_call": seen["bad_call"]}
        return _final_from_guide(request)

    client = FakeClient([respond] * 40)
    stages = replace(default_stages(), create_client=lambda env, budget, profile: client)
    config = RunConfig(
        model_profile_id=load_profile().profile_id,
        budget=RunBudget(explore_enabled=False),
    )
    outcome = execute(
        io.BytesIO(SAMPLE.read_bytes()), config, events.append, stages=stages, env={}
    )
    expected = ("breakdown_dimension_already_fixed" if fixed_breakdown
                else "unknown_or_unmatched_peer_definition")
    assert seen["refused"] == expected
    analysis = next(a for a in outcome.run.analyses if a.candidate_id == seen["candidate"])
    assert analysis.status is AnalysisStatus.COMPLETE
    assert all(a.status is AnalysisStatus.COMPLETE for a in outcome.run.analyses)
    assert len(outcome.run.recommendations) == 15
    (attempt,) = analysis.rejected_attempts
    assert attempt.errors[0].path == "/tool_call"
    assert attempt.errors[0].code is Code.SCHEMA_INVALID
    assert attempt.errors[0].detail == f"tool refused: {expected}"
    assert [e.message_code for e in events if e.stage.value == "tool_call"] == [
        "tool_call_started",
        "tool_refused",
    ]
    # The narrowed schema would not have offered that call for this slice.
    assert not admitted(seen["schema"], {"kind": "call_tool", "tool_call": seen["bad_call"]})


def test_peer_comparison_is_offered_only_for_slices_fixing_every_dimension(bundle):
    from llm.answer import peer_segments

    for pack in bundle.packs.values():
        segments = {s.segment_id: s for s in pack.segments}
        peers = peer_segments(segments.values())
        schema = step_schema(
            pack, list(pack.answer_guide.observation_options), segments.values(),
            {p.period_id for p in pack.periods}, True,
        )
        union = schema["properties"]["tool_call"]["anyOf"][0]["oneOf"]
        offered = "#/$defs/CompareToPeersArgs" in [r["$ref"] for r in union]
        assert offered == bool(peers)
        if peers:
            enum = schema["$defs"]["CompareToPeersArgs"]["properties"]["segment_id"]["enum"]
            assert all(len(segments[s].filters) == 5 for s in enum)


def test_a_slice_below_its_baseline_is_never_offered_more_traffic(bundle):
    """Live report regression: a combination outlier earning 0.16x its matched
    peers was recommended as "Scale up" through the family's allocation test."""
    directions = set()
    for pack in bundle.packs.values():
        c = pack.candidate
        if c.family is not DetectorFamily.COMBINATION_OUTLIER:
            continue
        relations = {
            bundle.facts[r].comparison.relation
            for r in c.trigger_fact_ids
            if bundle.facts[r].comparison is not None
        }
        directions |= relations
        allocation = "matched_allocation_holdout" in pack.permitted_test_templates
        throttle = "targeted_throttle_holdout" in pack.permitted_test_templates
        assert allocation == (relations == {Relation.GREATER})
        assert throttle == (relations == {Relation.LESS})
    assert directions == {Relation.GREATER, Relation.LESS}


def test_many_overlap_groups_reproduce_legacy_too_long_and_are_code_attached(bundle, monkeypatch):
    """Replay the stored failure with synthetic IDs; no private data required."""
    from llm.answer import exception_issues

    pack = next(iter(bundle.packs.values()))
    groups = [f"O_{i:016x}" for i in range(100)]
    candidate = pack.candidate.model_copy(update={"overlap_group_ids": groups})
    pack = pack.model_copy(update={"candidate": candidate})
    rec = guided(pack).model_dump(mode="json")
    legacy = create_model(
        "LegacyRecommendation", __base__=CandidateRecommendation,
        overlap_group_ids=(list[str], Field(max_length=8)),
    )
    with pytest.raises(ValidationError) as caught:
        legacy.model_validate(rec)
    errors = exception_issues(caught.value)
    assert any(e.code is Code.SCHEMA_INVALID and e.path == "/overlap_group_ids"
               and e.detail == "contract check failed: too_long" for e in errors)
    # Replay the legacy parser inside the real analysis loop with a scripted
    # model echoing the candidate's groups, exactly as the old prompt asked.
    from llm import analysis
    from tests.unit.test_reasoning import FakeRouter

    with monkeypatch.context() as patch:
        patch.setattr(analysis, "CandidateRecommendation", legacy)
        result = analysis.analyze_candidate(
            pack, FakeRouter(pack), FakeClient([rec, rec, rec]),
            RunBudget(max_turns_per_candidate=1),
        )
    assert result.status is AnalysisStatus.REJECTED_INVALID
    assert all(any(e.path == "/overlap_group_ids" and e.detail == "contract check failed: too_long"
                   for e in attempt.errors) for attempt in result.rejected_attempts)
    assert len(CandidateRecommendation.model_validate(rec).overlap_group_ids) == 100
    # The prompt and model answer do not grow with bookkeeping lists.
    wire = rec | {"overlap_group_ids": []}
    assert admitted(final_schema(pack, pack.answer_guide.observation_options), wire)
    assert attach_code_fields(CandidateRecommendation.model_validate(wire), pack).overlap_group_ids == groups


def test_bookkeeping_lists_do_not_expand_the_evidence_prompt(bundle):
    from pipeline.evidence import pack_payload

    pack = next(iter(bundle.packs.values()))
    candidate = pack.candidate.model_copy(update={
        "overlap_group_ids": [f"O_{i:016x}" for i in range(1000)],
        "quality_issue_ids": [f"Q_{i:016x}" for i in range(1000)],
    })
    view = pack_payload(pack.model_copy(update={"candidate": candidate}))
    assert view["candidate"]["overlap_group_count"] == 1000
    assert view["candidate"]["quality_issue_count"] == 1000
    assert "overlap_group_ids" not in view["candidate"]
    assert "quality_issue_ids" not in view["candidate"]


def test_breakdown_is_offered_only_by_dimensions_the_slice_does_not_fix(bundle):
    """Live regression: 13 refusals of breakdown_dimension_already_fixed on a
    full-size run, each spending a turn and a tool execution."""
    offered = 0
    for pack in bundle.packs.values():
        _, step = schemas(pack)
        for segment in pack.segments:
            for dim in Dimension:
                call = {
                    "tool": "breakdown", "segment_id": segment.segment_id, "by": dim.value,
                    "period_id": pack.candidate.period_id, "limit": 5,
                }
                expected = segment.cohort_id == "full_input" and dim not in segment.filters
                assert admitted(step, {"kind": "call_tool", "tool_call": call}) == expected
                offered += expected
    assert offered
