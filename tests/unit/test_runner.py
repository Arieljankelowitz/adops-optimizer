"""Runner orchestration with mocked stages: order, statuses, failures, cancel, jobs."""

import importlib.util
import io
import itertools
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from contracts.config import InputConfig, RunConfig
from contracts.enums import (
    AnalysisStatus,
    ProgressStage,
    ProgressState,
    RunStatus,
    ValidationErrorCode,
)
from contracts.models import (
    CandidateAnalysis,
    CandidatePack,
    DailyTrendArgs,
    RankingIR,
    ValidationAccepted,
    ValidationIssue,
    ValidationRejected,
)
from llm.profiles import load_profiles
from report.render import render_report
from runner import (
    CacheKey,
    InputRejected,
    JobManager,
    JobStatus,
    PipelineError,
    PipelineStages,
    RunCancelled,
    StageUnavailable,
    cache_key,
    execute,
    input_sha256,
    run_pipeline,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CSV = b"synthetic,bytes\n"
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ad_performance.csv"
PROFILE = load_profiles().active_profile


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_demo_run", REPO_ROOT / "scripts" / "build_demo_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEMO = _builder().build_demo_outcome(RunStatus.FINAL)
FATAL = _builder().build_demo_outcome(RunStatus.DETERMINISTIC_ONLY, fatal=True)


def _bundle_with_packs():
    b = DEMO.run.bundle
    packs = {
        c.candidate_id: CandidatePack(
            run_namespace=b.run_namespace, candidate=c, quality_issues=[], segments=[],
            periods=[], facts=[], assumptions=[], scenarios=[], permitted_test_templates=[],
            tool_allowlist=[], token_budget=1000)
        for c in b.candidates.values() if c.eligibility.value == "eligible"
    }
    return replace(b, packs=MappingProxyType(packs))


BUNDLE = _bundle_with_packs()
ANALYSES = {a.candidate_id: a for a in DEMO.run.analyses}
RECS = {r.recommendation.candidate_id: r for r in DEMO.run.recommendations}


class FakeClient:
    def __init__(self) -> None:
        self.waited = False

    def wait_until_ready(self) -> float:
        self.waited = True
        return 0.0


def _analysis(pack, tools, client, budget):
    cid = pack.candidate.candidate_id
    if cid == next(iter(BUNDLE.packs)):
        tools.dispatch(cid, DailyTrendArgs(segment_id=pack.candidate.segment_id,
                                           period_id=pack.candidate.period_id))
    return ANALYSES.get(cid) or CandidateAnalysis(
        candidate_id=cid, status=AnalysisStatus.COMPLETE, recommendation=None, tool_evidence=[],
        generation_turns=1, repairs=0)


def _validate(output, bundle):
    if isinstance(output, RankingIR):
        return ValidationAccepted(value=output)
    return ValidationAccepted(value=RECS[output.candidate_id])


def _rank(validated, registry, client, budget):
    """The demo ranking restricted to what was validated in this run."""
    ids = {r.recommendation_id for r in validated}
    ranking = DEMO.run.ranking
    return RankingIR(
        ranked_recommendation_ids=[i for i in ranking.ranked_recommendation_ids if i in ids],
        merge_instructions=[m for m in ranking.merge_instructions
                            if m.target_recommendation_id in ids
                            and set(m.source_recommendation_ids) <= ids],
        summary_themes=[t.model_copy(update={"recommendation_ids": [
            i for i in t.recommendation_ids if i in ids]}) for t in ranking.summary_themes],
    )


def _not_implemented(*args, **kwargs):
    raise NotImplementedError


def _stages(**overrides) -> PipelineStages:
    base = dict(
        load_csv=lambda source, config: object(),
        assess_quality=lambda data, config: BUNDLE.quality,
        aggregate=lambda data, quality, config: object(),
        detect_opportunities=lambda metrics, quality, config: list(BUNDLE.candidates.values()),
        refine_quality=lambda data, metrics, candidates, initial, config: initial,
        build_evidence=lambda metrics, quality, candidates, config: BUNDLE,
        create_client=lambda env, budget, profile: FakeClient(),
        analyze_candidate=_analysis,
        validate_analysis=_validate,
        rank_recommendations=_rank,
        render_report=render_report,
        breakdown=_not_implemented,
        daily_trend=lambda ctx, segment_id, period_id: DEMO.run.analyses[0].tool_evidence[0],
        compare_to_peers=_not_implemented,
    )
    base.update(overrides)
    return PipelineStages(**base)


def _clock():
    start = datetime(2031, 3, 10, 9, tzinfo=UTC)
    counter = itertools.count()
    return lambda: start + timedelta(seconds=next(counter))


def _run(config: RunConfig | None = None, **kwargs):
    events = []
    outcome = execute(io.BytesIO(CSV), config or RunConfig(model_profile_id=PROFILE),
                      events.append, clock=_clock(), env={}, **kwargs)
    return outcome, events


def _stage_states(events):
    return [(e.stage, e.state) for e in events if e.candidate_id is None]


def test_full_run_emits_every_stage_in_plan_order_and_is_final() -> None:
    outcome, events = _run(stages=_stages())
    assert outcome.status is RunStatus.FINAL
    assert outcome.events == tuple(events)
    started = [s for s, st in _stage_states(events) if st is ProgressState.STARTED]
    assert started == [
        ProgressStage.LOAD, ProgressStage.INITIAL_QUALITY, ProgressStage.AGGREGATE,
        ProgressStage.DETECT, ProgressStage.REFINED_QUALITY, ProgressStage.EVIDENCE,
        ProgressStage.MODEL_READINESS, ProgressStage.RANKING, ProgressStage.RENDER,
    ]
    assert events[-1].stage is ProgressStage.COMPLETE
    per_candidate = {e.stage for e in events if e.candidate_id}
    assert per_candidate == {ProgressStage.CANDIDATE_ANALYSIS, ProgressStage.TOOL_CALL,
                             ProgressStage.VALIDATION}
    assert len({e.run_id for e in events}) == 1
    m = outcome.artifact.manifest
    profile = load_profiles().get(PROFILE)
    assert (m.model_id, m.model_revision) == (profile.model_id, profile.model_revision)
    assert m.model_profile_hash == profile.sha256()
    assert m.input_sha256 == input_sha256(CSV)
    assert set(m.files) == set(outcome.artifact.private_artifact_names) - {"manifest.json"}
    assert outcome.artifact.ranking == DEMO.run.ranking
    assert outcome.report.markdown.startswith("# Ad-performance optimization report")


def test_run_pipeline_returns_the_artifact() -> None:
    import runner

    original = runner.default_stages
    runner.default_stages = _stages
    try:
        artifact = run_pipeline(io.BytesIO(CSV), RunConfig(), lambda e: None)
    finally:
        runner.default_stages = original
    assert artifact.status is RunStatus.DETERMINISTIC_ONLY


def test_no_model_profile_is_deterministic_only() -> None:
    outcome, events = _run(RunConfig(), stages=_stages())
    assert outcome.status is RunStatus.DETERMINISTIC_ONLY
    assert (ProgressStage.MODEL_READINESS, ProgressState.SKIPPED) in _stage_states(events)
    assert outcome.artifact.recommendations == []
    assert outcome.artifact.manifest.model_id is None
    assert "DETERMINISTIC-ONLY DRAFT" in outcome.report.markdown


@pytest.mark.parametrize("stage", ["create_client", "analyze_candidate", "validate_analysis"])
def test_unimplemented_model_layer_degrades_to_labelled_deterministic_draft(stage: str) -> None:
    outcome, events = _run(stages=_stages(**{stage: _not_implemented}))
    assert outcome.status is RunStatus.DETERMINISTIC_ONLY
    assert any(e.state is ProgressState.FAILED and "not_implemented" in e.message_code
               for e in events)


def test_refused_client_degrades_without_exposing_details() -> None:
    def refuse(env, budget, profile):
        raise ValueError("LLM_API_KEY=sk-secret missing")

    outcome, events = _run(stages=_stages(create_client=refuse))
    assert outcome.status is RunStatus.DETERMINISTIC_ONLY
    assert all("secret" not in e.message_code for e in events)


def test_budget_exhausted_candidate_makes_the_run_partial() -> None:
    first = next(iter(BUNDLE.packs))

    def analysis(pack, tools, client, budget):
        if pack.candidate.candidate_id == first:
            return CandidateAnalysis(candidate_id=first, status=AnalysisStatus.BUDGET_EXHAUSTED,
                                     recommendation=None, tool_evidence=[], generation_turns=4,
                                     repairs=1)
        return _analysis(pack, tools, client, budget)

    outcome, _ = _run(stages=_stages(analyze_candidate=analysis))
    assert outcome.status is RunStatus.PARTIAL
    assert "PARTIAL RUN" in outcome.report.markdown


def test_rejected_ranking_makes_the_run_partial() -> None:
    def validate(output, bundle):
        if isinstance(output, RankingIR):
            return ValidationRejected(errors=[ValidationIssue(
                code=ValidationErrorCode.UNKNOWN_REFERENCE, path="ranked_recommendation_ids[0]",
                detail="unknown")])
        return _validate(output, bundle)

    outcome, _ = _run(stages=_stages(validate_analysis=validate))
    assert outcome.status is RunStatus.PARTIAL
    assert outcome.artifact.ranking is None
    assert "ranking" in outcome.run.validation_failures


def test_validation_rejection_is_recorded_and_excluded() -> None:
    target = next(iter(RECS))

    def validate(output, bundle):
        if not isinstance(output, RankingIR) and output.candidate_id == target:
            return ValidationRejected(errors=[ValidationIssue(
                code=ValidationErrorCode.WRONG_SEGMENT, path="observation_statements[0]",
                detail="wrong segment")])
        return _validate(output, bundle)

    outcome, events = _run(stages=_stages(validate_analysis=validate))
    assert target in outcome.run.validation_failures
    assert all(r.recommendation.candidate_id != target for r in outcome.artifact.recommendations)
    assert any(e.stage is ProgressStage.VALIDATION and e.state is ProgressState.FAILED
               for e in events)


def test_wall_clock_budget_skips_remaining_candidates() -> None:
    ticks = itertools.count(0, 1000)
    outcome, events = _run(stages=_stages(), monotonic=lambda: next(ticks))
    assert outcome.status is RunStatus.PARTIAL
    assert any(e.message_code == "wall_clock_budget_exhausted" for e in events)


def test_fatal_quality_skips_analytics_and_model() -> None:
    called = []
    stages = _stages(assess_quality=lambda data, config: FATAL.run.bundle.quality,
                     aggregate=lambda *a: called.append("aggregate"))
    outcome, events = _run(stages=stages)
    assert called == []
    assert outcome.status is RunStatus.DETERMINISTIC_ONLY
    skipped = [s for s, st in _stage_states(events) if st is ProgressState.SKIPPED]
    assert ProgressStage.AGGREGATE in skipped and ProgressStage.MODEL_READINESS in skipped
    assert "fatal data-quality issue blocks" in outcome.report.markdown


def test_unimplemented_pipeline_stage_fails_with_a_clear_message() -> None:
    events = []
    with pytest.raises(StageUnavailable) as err:
        execute(io.BytesIO(SAMPLE.read_bytes()), RunConfig(), events.append, env={})
    assert err.value.stage is ProgressStage.EVIDENCE
    assert "not implemented in this build yet" in err.value.public_message
    assert [(e.stage, e.state) for e in events][-2:] == [
        (ProgressStage.EVIDENCE, ProgressState.STARTED),
        (ProgressStage.EVIDENCE, ProgressState.FAILED)]
    succeeded = {e.stage for e in events if e.state is ProgressState.SUCCEEDED}
    assert {ProgressStage.LOAD, ProgressStage.INITIAL_QUALITY, ProgressStage.AGGREGATE,
            ProgressStage.DETECT, ProgressStage.REFINED_QUALITY} <= succeeded


def test_unreadable_file_gives_a_fatal_quality_report_through_the_real_modules() -> None:
    outcome, events = _run(RunConfig())  # default stages: the real loader and quality rules
    assert outcome.status is RunStatus.DETERMINISTIC_ONLY
    assert outcome.artifact.quality.has_fatal
    md = outcome.report.markdown
    assert "### Fatals" in md and "Analysis stopped" in md
    assert "fatal data-quality issue blocks" in md
    assert outcome.artifact.quality.run_namespace == outcome.artifact.manifest.run_namespace


def test_unknown_model_profile_is_refused_before_any_stage() -> None:
    events = []
    with pytest.raises(PipelineError, match="model profile"):
        execute(io.BytesIO(CSV), RunConfig(model_profile_id="no_such_profile"), events.append,
                stages=_stages(), env={})
    assert events == []


def test_readiness_is_polled_before_analysis() -> None:
    client = FakeClient()
    outcome, events = _run(stages=_stages(create_client=lambda env, budget, profile: client))
    assert client.waited
    ready = [i for i, e in enumerate(events) if e.stage is ProgressStage.MODEL_READINESS
             and e.state is ProgressState.SUCCEEDED]
    first = next(i for i, e in enumerate(events) if e.stage is ProgressStage.CANDIDATE_ANALYSIS)
    assert ready and ready[0] < first


def test_cold_start_failure_degrades_to_deterministic_draft() -> None:
    class Cold(FakeClient):
        def wait_until_ready(self) -> float:
            raise TimeoutError("endpoint did not become ready")

    outcome, events = _run(stages=_stages(create_client=lambda env, budget, profile: Cold()))
    assert outcome.status is RunStatus.DETERMINISTIC_ONLY
    assert any(e.message_code == "model_unavailable" for e in events)


def test_stage_errors_never_carry_exception_text() -> None:
    def boom(*args):
        raise ValueError("supply 41022 label leaked")

    with pytest.raises(PipelineError) as err:
        _run(stages=_stages(aggregate=boom))
    assert "41022" not in err.value.public_message
    assert "ValueError" in err.value.public_message


def test_input_over_the_size_bound_is_rejected_before_parsing() -> None:
    loaded = []
    config = RunConfig(input=InputConfig(max_bytes=4))
    with pytest.raises(InputRejected):
        _run(config, stages=_stages(load_csv=lambda s, c: loaded.append(1)))
    assert loaded == []


def test_cancel_before_start_runs_nothing() -> None:
    from runner import CancelToken

    token = CancelToken()
    token.cancel()
    events = []
    with pytest.raises(RunCancelled):
        execute(io.BytesIO(CSV), RunConfig(), events.append, stages=_stages(), cancel=token,
                env={})
    assert [(e.stage, e.state) for e in events] == [(ProgressStage.LOAD,
                                                     ProgressState.CANCELLED)]


def test_cancel_during_model_stage_prevents_further_calls() -> None:
    from runner import CancelToken

    token = CancelToken()
    calls = []

    def analysis(pack, tools, client, budget):
        calls.append(pack.candidate.candidate_id)
        token.cancel()
        return _analysis(pack, tools, client, budget)

    events = []
    with pytest.raises(RunCancelled):
        execute(io.BytesIO(CSV), RunConfig(model_profile_id=PROFILE), events.append,
                stages=_stages(analyze_candidate=analysis), cancel=token, env={})
    assert len(calls) == 1
    assert events[-1].state is ProgressState.CANCELLED


def test_tool_calls_are_bounded_per_candidate() -> None:
    def analysis(pack, tools, client, budget):
        # Distinct calls consume executions; identical calls now hit the cache.
        for i in range(budget.max_tool_calls_per_candidate + 1):
            args = DailyTrendArgs(segment_id=pack.candidate.segment_id,
                                  period_id=f"P_{i:016x}")
            tools.dispatch(pack.candidate.candidate_id, args)
        raise AssertionError("unreachable")

    outcome, events = _run(stages=_stages(analyze_candidate=analysis))
    tool_starts = [e for e in events if e.stage is ProgressStage.TOOL_CALL
                   and e.state is ProgressState.STARTED]
    assert len(tool_starts) == 3 * len(BUNDLE.packs)
    assert outcome.status is RunStatus.PARTIAL


def _wait(job, timeout=5.0):
    deadline = time.monotonic() + timeout
    while job.active and time.monotonic() < deadline:
        time.sleep(0.01)


def test_job_manager_is_single_flight_and_reconnects() -> None:
    import threading

    release = threading.Event()

    def slow_load(source, config):
        release.wait(5)
        return object()

    manager = JobManager()
    config = RunConfig()
    key = cache_key(input_sha256(CSV), config)
    job = manager.submit(key, CSV, config, stages=_stages(load_csv=slow_load), env={})
    assert manager.submit(key, CSV, config) is job  # rerun reconnects, no duplicate run
    other = replace(key, input_sha256="0" * 64)
    with pytest.raises(RuntimeError):
        manager.submit(other, CSV, config)
    release.set()
    _wait(job)
    assert job.status is JobStatus.SUCCEEDED
    assert job.outcome.status is RunStatus.DETERMINISTIC_ONLY
    status, events = job.snapshot()
    assert events[-1].stage is ProgressStage.COMPLETE
    assert job.run_id == job.outcome.artifact.manifest.run_id


def test_job_failure_message_is_redacted() -> None:
    def leak(*args):
        raise RuntimeError("LLM_API_KEY=sk-live-123 for supply 41022")

    manager = JobManager()
    config = RunConfig()
    job = manager.submit(cache_key(input_sha256(CSV), config), CSV, config,
                         stages=_stages(load_csv=leak), env={})
    _wait(job)
    assert job.status is JobStatus.FAILED
    assert "sk-live" not in job.error and "41022" not in job.error


def test_job_cancel() -> None:
    import threading

    gate = threading.Event()

    def slow_load(source, config):
        gate.wait(5)
        return object()

    manager = JobManager()
    config = RunConfig()
    job = manager.submit(cache_key(input_sha256(CSV), config), CSV, config,
                         stages=_stages(load_csv=slow_load), env={})
    job.cancel()
    gate.set()
    _wait(job)
    assert job.status is JobStatus.CANCELLED
    assert job.outcome is None


def test_cache_key_from_manifest_round_trips() -> None:
    outcome, _ = _run(stages=_stages())
    key = CacheKey.from_manifest(outcome.artifact.manifest)
    assert key == cache_key(input_sha256(CSV), RunConfig(model_profile_id=PROFILE))
