"""End-to-end orchestration used by the app and the CLI.

``run_pipeline`` is the frozen entry point. It calls each pipeline, model and
validation function through its contract signature, reports every stage as a
``ProgressEvent`` and returns the ``RunArtifact``. ``execute`` does the same
work but returns the richer ``RunOutcome`` the app needs (rendered report,
evidence bundle, metrics for drill-downs).

Failure handling follows the plan:

* a stage that is not implemented yet, or that raises, stops the run with a
  ``PipelineError`` carrying a message safe to show (no labels, keys or raw
  exception text);
* a fatal data-quality issue skips analytics and the model; the report shows
  the quality findings only;
* no model profile, an unavailable model layer or a refused client yields an
  explicitly labelled ``deterministic_only`` draft;
* budget exhaustion or incomplete validation yields ``partial``, never
  ``final``;
* cancellation stops before the next stage or model call and raises
  ``RunCancelled``; an in-flight call is not interrupted.

``RunJob`` runs ``execute`` on a background thread with a finite status, a
cancel flag and a per-session single-flight lock (``JobManager``), so a
Streamlit rerun reconnects to the current run instead of starting another.
``RunCache`` is a per-session, in-memory cache keyed by the complete cache key.
"""

import hashlib
import io
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

from contracts import SCHEMA_VERSION
from contracts.config import RunBudget, RunConfig, config_hash
from contracts.enums import (
    AnalysisStatus,
    BudgetReason,
    Decision,
    ProgressStage,
    ProgressState,
    RunStatus,
    ToolName,
)
from contracts.ids import make_namespace, stable_digest
from contracts.models import (
    BreakdownArgs,
    Candidate,
    CandidateAnalysis,
    CandidatePack,
    CompareToPeersArgs,
    DailyTrendArgs,
    ProgressEvent,
    QualityReport,
    RankingIR,
    RenderedReport,
    RunArtifact,
    RunManifest,
    ToolCall,
    ToolEvidence,
    ValidatedRecommendation,
    ValidationIssue,
    ValidationResult,
)
from contracts.runtime import (
    AnalysisContext,
    AssumptionRegistry,
    EvidenceBundle,
    FactRegistry,
    LoadedData,
    MetricsStore,
    ModelClient,
    ScenarioRegistry,
    ValidatedRun,
)
from llm.profiles import ModelProfile, load_profile
from report.bundle import RunBundle, RunOutcome, as_cached, assemble, from_bundle
from report.text import STAGE_LABELS

REPO_ROOT = Path(__file__).resolve().parent
MODEL_PROFILES_PATH = REPO_ROOT / "config" / "model_profiles.yaml"
PROMPTS_PATH = REPO_ROOT / "llm" / "prompts.py"
TOOLS_PATH = REPO_ROOT / "llm" / "tools.py"
DEMO_RUN_PATH = REPO_ROOT / "examples" / "synthetic" / "demo_run.json"


# --- Errors ----------------------------------------------------------------


class PipelineError(RuntimeError):
    """A run stopped. ``public_message`` is safe to show; it carries no private data."""

    def __init__(self, stage: ProgressStage, public_message: str) -> None:
        super().__init__(public_message)
        self.stage = stage
        self.public_message = public_message


class StageUnavailable(PipelineError):
    """A pipeline module still raises ``NotImplementedError`` in this build."""


class InputRejected(PipelineError):
    """The input failed a bound checked before parsing."""


class RunCancelled(PipelineError):
    """The run was cancelled; no further stage or model call was started."""


# --- Cache key and identity ------------------------------------------------


@dataclass(frozen=True)
class CacheKey:
    """The complete cache key; a cached result is reused only on an exact match."""

    input_sha256: str
    schema_version: str
    analytics_config_hash: str
    model_profile_hash: str | None
    prompt_hash: str | None
    tool_version: str
    assumption_hash: str

    @classmethod
    def from_manifest(cls, manifest: RunManifest) -> "CacheKey":
        return cls(
            input_sha256=manifest.input_sha256,
            schema_version=manifest.schema_version,
            analytics_config_hash=manifest.analytics_config_hash,
            model_profile_hash=manifest.model_profile_hash,
            prompt_hash=manifest.prompt_hash,
            tool_version=manifest.tool_version,
            assumption_hash=manifest.assumption_hash,
        )

    def digest(self) -> str:
        return stable_digest(list(self.__dict__.values()))


def _file_sha(path: Path) -> str:
    data = path.read_bytes() if path.is_file() else b""
    return hashlib.sha256(data).hexdigest()


def model_profile(profile_id: str) -> ModelProfile:
    """The named profile from ``config/model_profiles.yaml``; unknown names are refused."""
    try:
        return load_profile(profile_id, MODEL_PROFILES_PATH)
    except (KeyError, OSError, ValueError) as exc:
        raise ValueError("unknown or invalid model profile") from exc


def model_profile_hash(profile_id: str | None) -> str | None:
    """SHA-256 of every setting of the selected profile; None without a model."""
    return None if profile_id is None else model_profile(profile_id).sha256()


def prompt_hash(profile_id: str | None) -> str | None:
    """Hash of the versioned prompt module; None when no model is used."""
    return None if profile_id is None else _file_sha(PROMPTS_PATH)


def tool_version() -> str:
    return f"tools_{_file_sha(TOOLS_PATH)[:16]}"


def cache_key(input_sha256: str, config: RunConfig) -> CacheKey:
    return CacheKey(
        input_sha256=input_sha256,
        schema_version=SCHEMA_VERSION,
        analytics_config_hash=config_hash(config),
        model_profile_hash=model_profile_hash(config.model_profile_id),
        prompt_hash=prompt_hash(config.model_profile_id),
        tool_version=tool_version(),
        assumption_hash=config_hash(config.evidence),
    )


def input_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Stages ----------------------------------------------------------------


def _create_client(env: Mapping[str, str], budget: RunBudget,
                   profile: ModelProfile) -> ModelClient:
    from llm import client

    return client.create_client(env, budget, profile=profile)


@dataclass(frozen=True)
class PipelineStages:
    """The functions a run calls, by contract signature; tests substitute fakes."""

    load_csv: Callable
    assess_quality: Callable
    aggregate: Callable
    detect_opportunities: Callable
    refine_quality: Callable
    build_evidence: Callable
    create_client: Callable[[Mapping[str, str], RunBudget, ModelProfile], ModelClient]
    analyze_candidate: Callable
    validate_analysis: Callable[..., ValidationResult]
    rank_recommendations: Callable
    render_report: Callable[[ValidatedRun], RenderedReport]
    breakdown: Callable
    daily_trend: Callable
    compare_to_peers: Callable


def _build_evidence(metrics, quality, candidates, config):
    """Evidence with the calibrated token gate, before any server is reachable.

    The estimate errs high, so a pack that passes also passes the exact count at
    request time. A candidate whose mandatory evidence cannot fit is left
    unjudged with a reason instead of stopping the run.
    """
    from llm.tokens import estimate_pack_prompt_tokens
    from pipeline import evidence

    return evidence.build_evidence_bounded(
        metrics, quality, candidates, config,
        count_tokens=estimate_pack_prompt_tokens, skip_oversize=True)


def default_stages() -> PipelineStages:
    from llm import analysis, ranking, tools, validator
    from pipeline import detectors, load, metrics, quality
    from report import render

    return PipelineStages(
        load_csv=load.load_csv,
        assess_quality=quality.assess_quality,
        aggregate=metrics.aggregate,
        detect_opportunities=detectors.detect_opportunities,
        refine_quality=quality.refine_quality,
        build_evidence=_build_evidence,
        create_client=_create_client,
        analyze_candidate=analysis.analyze_candidate,
        validate_analysis=validator.validate_analysis,
        rank_recommendations=ranking.rank_recommendations,
        render_report=render.render_report,
        breakdown=tools.breakdown,
        daily_trend=tools.daily_trend,
        compare_to_peers=tools.compare_to_peers,
    )


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class _Progress:
    def __init__(self, run_id: str, sink: Callable[[ProgressEvent], None],
                 cancel: CancelToken, clock: Callable[[], datetime]) -> None:
        self.run_id = run_id
        self.sink = sink
        self.cancel = cancel
        self.clock = clock
        self.events: list[ProgressEvent] = []

    def emit(self, stage: ProgressStage, state: ProgressState, code: str,
             candidate_id: str | None = None) -> None:
        event = ProgressEvent(run_id=self.run_id, stage=stage, state=state,
                              candidate_id=candidate_id, message_code=code, at=self.clock())
        self.events.append(event)
        self.sink(event)

    def check_cancel(self, stage: ProgressStage, candidate_id: str | None = None) -> None:
        if self.cancel.cancelled:
            self.emit(stage, ProgressState.CANCELLED, "cancel_requested", candidate_id)
            raise RunCancelled(stage, "The run was cancelled before this stage started.")

    @contextmanager
    def stage(self, stage: ProgressStage, candidate_id: str | None = None) -> Iterator[None]:
        self.check_cancel(stage, candidate_id)
        self.emit(stage, ProgressState.STARTED, f"{stage.value}_started", candidate_id)
        try:
            yield
        except PipelineError:
            raise
        except NotImplementedError as exc:
            self.emit(stage, ProgressState.FAILED, "not_implemented", candidate_id)
            raise StageUnavailable(
                stage,
                f"{STAGE_LABELS[stage]} is not implemented in this build yet. "
                "Use the synthetic demo run to see the full flow.",
            ) from exc
        except Exception as exc:
            self.emit(stage, ProgressState.FAILED, "stage_error", candidate_id)
            raise PipelineError(
                stage, f"{STAGE_LABELS[stage]} failed ({type(exc).__name__})."
            ) from exc
        self.emit(stage, ProgressState.SUCCEEDED, f"{stage.value}_succeeded", candidate_id)

    def skip(self, stage: ProgressStage, code: str) -> None:
        self.emit(stage, ProgressState.SKIPPED, code)


class _ProgressToolRouter:
    """Dispatches model tool calls to the deterministic tools and reports each call."""

    def __init__(self, ctx: AnalysisContext, stages: PipelineStages, progress: _Progress,
                 budget: RunBudget) -> None:
        self.ctx = ctx
        self.stages = stages
        self.progress = progress
        self.budget = budget
        self.calls: dict[str | None, int] = {}
        self.cache: dict[str, ToolEvidence] = {}

    @property
    def allowed_tools(self) -> frozenset[ToolName]:
        return frozenset(ToolName)

    def validate_analysis(self, output: CandidateAnalysis) -> ValidationResult:
        return self.stages.validate_analysis(output, self.ctx.bundle)

    def dispatch_explore(self, call: ToolCall) -> ToolEvidence:
        # No candidate exists yet. Scope is checked by the deterministic tool,
        # and exploration owns a separate fixed execution budget.
        return self._dispatch(None, call)

    def dispatch(self, candidate_id: str, call: ToolCall) -> ToolEvidence:
        return self._dispatch(candidate_id, call)

    def _dispatch(self, candidate_id: str | None, call: ToolCall) -> ToolEvidence:
        from llm.tools import extend_bundle

        key = call.model_dump_json()
        if key in self.cache:
            return self.cache[key]
        count = self.calls.get(candidate_id, 0)
        limit = (min(self.budget.max_explore_tool_calls, 3) if candidate_id is None else
                 min(self.budget.max_tool_calls_per_candidate, 3))
        if count >= limit:
            raise PipelineError(ProgressStage.TOOL_CALL, "Tool call budget exceeded.")
        self.calls[candidate_id] = count + 1
        with self.progress.stage(ProgressStage.TOOL_CALL, candidate_id):
            match call:
                case BreakdownArgs():
                    ev = self.stages.breakdown(
                        self.ctx, call.segment_id, call.by, call.period_id, call.limit)
                case DailyTrendArgs():
                    ev = self.stages.daily_trend(self.ctx, call.segment_id, call.period_id)
                case CompareToPeersArgs():
                    ev = self.stages.compare_to_peers(
                        self.ctx, call.segment_id, call.period_id, call.peer_definition_id)
                case _:
                    raise PipelineError(ProgressStage.TOOL_CALL, "Unknown tool.")
            self.ctx = replace(self.ctx, bundle=extend_bundle(self.ctx.bundle, ev))
            self.cache[key] = ev
            return ev


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _empty_bundle(quality: QualityReport) -> EvidenceBundle:
    """No evidence was built; the quality facts are promoted so warnings keep their values."""
    ns = quality.run_namespace
    empty = MappingProxyType({})
    facts = FactRegistry(ns, (f for f in quality.facts if f.run_namespace == ns))
    return EvidenceBundle(
        run_namespace=ns, facts=facts, assumptions=AssumptionRegistry(ns),
        scenarios=ScenarioRegistry(ns), segments=empty, periods=empty, candidates=empty,
        overlap_groups=empty, quality=quality, packs=empty,
    )


@dataclass
class _ModelResult:
    status: RunStatus
    recommendations: list[ValidatedRecommendation] = field(default_factory=list)
    ranking: RankingIR | None = None
    analyses: list[CandidateAnalysis] = field(default_factory=list)
    failures: dict[str, tuple[ValidationIssue, ...]] = field(default_factory=dict)
    model_id: str | None = None
    model_revision: str | None = None
    bundle: EvidenceBundle | None = None
    model_call_counts: dict[str, int] = field(default_factory=dict)


def _run_model(bundle: EvidenceBundle, metrics: MetricsStore, config: RunConfig,
               stages: PipelineStages, progress: _Progress, env: Mapping[str, str],
               monotonic: Callable[[], float]) -> _ModelResult:
    budget = config.budget
    if config.model_profile_id is None:
        progress.skip(ProgressStage.MODEL_READINESS, "no_model_profile")
        return _ModelResult(RunStatus.DETERMINISTIC_ONLY)
    if not bundle.packs and not budget.explore_enabled:
        progress.skip(ProgressStage.MODEL_READINESS, "no_candidates")
        return _ModelResult(RunStatus.DETERMINISTIC_ONLY)

    progress.check_cancel(ProgressStage.MODEL_READINESS)
    progress.emit(ProgressStage.MODEL_READINESS, ProgressState.STARTED, "model_readiness_started")
    profile = model_profile(config.model_profile_id)
    try:
        client = stages.create_client(env, budget, profile)
    except NotImplementedError:
        progress.emit(ProgressStage.MODEL_READINESS, ProgressState.FAILED,
                      "model_layer_not_implemented")
        return _ModelResult(RunStatus.DETERMINISTIC_ONLY)
    except Exception:
        progress.emit(ProgressStage.MODEL_READINESS, ProgressState.FAILED, "model_client_refused")
        return _ModelResult(RunStatus.DETERMINISTIC_ONLY)
    # Cold start: poll readiness before any analysis call (bounded by the budget).
    wait = getattr(client, "wait_until_ready", None)
    if wait is not None:
        try:
            wait()
        except Exception:
            progress.emit(ProgressStage.MODEL_READINESS, ProgressState.FAILED,
                          "model_unavailable")
            return _ModelResult(RunStatus.DETERMINISTIC_ONLY)
    progress.emit(ProgressStage.MODEL_READINESS, ProgressState.SUCCEEDED,
                  "model_readiness_succeeded")

    result = _ModelResult(RunStatus.FINAL, model_id=profile.model_id,
                          model_revision=profile.model_revision)
    router = _ProgressToolRouter(
        AnalysisContext(run_namespace=bundle.run_namespace, metrics=metrics, bundle=bundle),
        stages, progress, budget,
    )
    started = monotonic()
    packs: list[CandidatePack] = list(bundle.packs.values())[: min(budget.max_candidates, 15)]
    complete = len(packs) == len(bundle.packs)
    for skipped in list(bundle.packs.values())[len(packs):]:
        result.analyses.append(CandidateAnalysis(
            candidate_id=skipped.candidate.candidate_id, status=AnalysisStatus.BUDGET_EXHAUSTED,
            recommendation=None, tool_evidence=[], generation_turns=0, repairs=0,
            budget_reason=BudgetReason.CANDIDATE_CAP))
    for cid, reason in bundle.unpacked.items():
        progress.emit(ProgressStage.CANDIDATE_ANALYSIS, ProgressState.SKIPPED, reason.value, cid)
        result.analyses.append(CandidateAnalysis(
            candidate_id=cid, status=AnalysisStatus.BUDGET_EXHAUSTED, recommendation=None,
            tool_evidence=[], generation_turns=0, repairs=0, budget_reason=reason))
        complete = False
    for pack in packs:
        cid = pack.candidate.candidate_id
        progress.check_cancel(ProgressStage.CANDIDATE_ANALYSIS, cid)
        if monotonic() - started > budget.warm_inference_seconds:
            progress.emit(ProgressStage.CANDIDATE_ANALYSIS, ProgressState.SKIPPED,
                          "wall_clock_budget_exhausted", cid)
            result.analyses.append(CandidateAnalysis(
                candidate_id=cid, status=AnalysisStatus.BUDGET_EXHAUSTED,
                recommendation=None, tool_evidence=[], generation_turns=0, repairs=0,
                budget_reason=BudgetReason.WARM_BUDGET))
            complete = False
            continue
        progress.emit(ProgressStage.CANDIDATE_ANALYSIS, ProgressState.STARTED,
                      "candidate_analysis_started", cid)
        try:
            analysis = stages.analyze_candidate(pack, router, client, budget)
        except NotImplementedError:
            progress.emit(ProgressStage.CANDIDATE_ANALYSIS, ProgressState.FAILED,
                          "model_layer_not_implemented", cid)
            return _ModelResult(RunStatus.DETERMINISTIC_ONLY)
        except RunCancelled:
            raise
        except Exception:
            progress.emit(ProgressStage.CANDIDATE_ANALYSIS, ProgressState.FAILED,
                          "candidate_analysis_error", cid)
            analysis = CandidateAnalysis(candidate_id=cid, status=AnalysisStatus.TRANSPORT_FAILED,
                                         recommendation=None, tool_evidence=[],
                                         generation_turns=0, repairs=0)
        else:
            state = (ProgressState.SUCCEEDED if analysis.status is AnalysisStatus.COMPLETE
                     else ProgressState.FAILED)
            code = analysis.status.value
            if analysis.budget_reason is not None:
                code = f"{code}_{analysis.budget_reason.value}"
            progress.emit(ProgressStage.CANDIDATE_ANALYSIS, state, code, cid)
        result.analyses.append(analysis)
        bundle = router.ctx.bundle
        if analysis.status is AnalysisStatus.REJECTED_INVALID:
            continue
        if analysis.status is not AnalysisStatus.COMPLETE or analysis.recommendation is None:
            complete = False
            continue
        progress.emit(ProgressStage.VALIDATION, ProgressState.STARTED, "validation_started", cid)
        try:
            verdict = stages.validate_analysis(analysis, bundle)
        except NotImplementedError:
            progress.emit(ProgressStage.VALIDATION, ProgressState.FAILED,
                          "validator_not_implemented", cid)
            return _ModelResult(RunStatus.DETERMINISTIC_ONLY)
        if verdict.accepted:
            result.recommendations.append(verdict.value)
            progress.emit(ProgressStage.VALIDATION, ProgressState.SUCCEEDED, "accepted", cid)
        else:
            result.failures[cid] = tuple(verdict.errors)
            progress.emit(ProgressStage.VALIDATION, ProgressState.FAILED, "rejected", cid)

    if budget.explore_enabled:
        from llm.analysis import explore_candidates

        exploration = explore_candidates(bundle, router, client, budget, judged=result.analyses)
        router.ctx = replace(router.ctx, bundle=exploration.bundle)
        bundle = exploration.bundle
        for pack in exploration.packs:
            cid = pack.candidate.candidate_id
            progress.check_cancel(ProgressStage.CANDIDATE_ANALYSIS, cid)
            try:
                analysis = stages.analyze_candidate(pack, router, client, budget)
                bundle = router.ctx.bundle
                result.analyses.append(analysis)
                if analysis.status is AnalysisStatus.COMPLETE and analysis.recommendation is not None:
                    verdict = stages.validate_analysis(analysis, bundle)
                    if verdict.accepted:
                        result.recommendations.append(verdict.value)
                    else:
                        result.failures[cid] = tuple(verdict.errors)
                # Optional discovery never makes otherwise complete rule results partial.
            except RunCancelled:
                raise
            except Exception:
                progress.emit(ProgressStage.CANDIDATE_ANALYSIS, ProgressState.FAILED,
                              "optional_explore_judgment_failed", cid)
        progress.check_cancel(ProgressStage.CANDIDATE_ANALYSIS)

    # Both the repair hook and the final ranking gate must resolve the same
    # already-accepted recommendations, including optional exploration.
    ranking_bundle = replace(bundle, validated_recommendations=MappingProxyType({
        r.recommendation_id: r for r in result.recommendations
    }))
    result.bundle = ranking_bundle
    # Ranking's sole repair also covers lane C grounding/overlap failures.
    setattr(client, "_validate_ranking", lambda output: stages.validate_analysis(output, ranking_bundle))
    actionable = [r for r in result.recommendations
                  if r.recommendation.decision in (Decision.RECOMMEND_TEST,
                                                   Decision.INVESTIGATE_FIRST)]
    if not actionable:
        progress.skip(ProgressStage.RANKING, "no_validated_recommendations")
    else:
        progress.check_cancel(ProgressStage.RANKING)
        progress.emit(ProgressStage.RANKING, ProgressState.STARTED, "ranking_started")
        try:
            ranking = stages.rank_recommendations(result.recommendations, bundle.facts, client,
                                                  budget)
            verdict = stages.validate_analysis(ranking, ranking_bundle)
        except NotImplementedError:
            progress.emit(ProgressStage.RANKING, ProgressState.FAILED, "ranking_not_implemented")
            complete = False
        except Exception:
            progress.emit(ProgressStage.RANKING, ProgressState.FAILED, "ranking_error")
            complete = False
        else:
            if verdict.accepted:
                result.ranking = verdict.value
                progress.emit(ProgressStage.RANKING, ProgressState.SUCCEEDED, "accepted")
            else:
                result.failures["ranking"] = tuple(verdict.errors)
                progress.emit(ProgressStage.RANKING, ProgressState.FAILED, "rejected")
                complete = False
    if not complete:
        result.status = RunStatus.PARTIAL
    elif not result.recommendations:
        result.status = RunStatus.DETERMINISTIC_ONLY
    from llm.analysis import reasoning_stats

    reasoning = reasoning_stats(client)
    result.model_call_counts = {"generations": reasoning.generations,
                                "ranking_calls": reasoning.ranking_calls,
                                "invalid_outputs": reasoning.invalid_outputs}
    if budget.explore_enabled:
        # Whether optional discovery ran to the end, and how many candidates it raised.
        result.model_call_counts.update(
            explore_completed=int(reasoning.explore_status == "complete"),
            explore_proposals=sum(1 for a in result.analyses
                                  if a.candidate_id in bundle.candidates
                                  and bundle.candidates[a.candidate_id].origin
                                  == "model_proposed"))
    transport = getattr(client, "stats", None)
    if transport is not None:
        result.model_call_counts.update({name: getattr(transport, name, 0) for name in (
            "network_attempts", "transport_retries", "prompt_tokens", "output_tokens",
            "token_counts_exact", "token_counts_estimated")})
    return result


def execute(
    source: BinaryIO,
    config: RunConfig,
    progress: Callable[[ProgressEvent], None],
    *,
    stages: PipelineStages | None = None,
    cancel: CancelToken | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
) -> RunOutcome:
    """Run every stage in plan order and return the sealed outcome."""
    stages = stages or default_stages()
    cancel = cancel or CancelToken()
    env = os.environ if env is None else env

    # Hash the bytes before any parsing; enforce the size bound first.
    data = source.read(config.input.max_bytes + 1)
    generated_at = clock()
    sha = input_sha256(data)
    try:
        key = cache_key(sha, config)
    except ValueError as exc:
        raise PipelineError(ProgressStage.MODEL_READINESS,
                            "The selected model profile is not defined.") from exc
    run_id = f"run_{stable_digest([key.digest(), generated_at.isoformat()])}"
    p = _Progress(run_id, progress, cancel, clock)
    if len(data) > config.input.max_bytes:
        p.emit(ProgressStage.LOAD, ProgressState.FAILED, "input_too_large")
        raise InputRejected(ProgressStage.LOAD, "The file exceeds the configured size limit.")

    with p.stage(ProgressStage.LOAD):
        loaded: LoadedData = stages.load_csv(io.BytesIO(data), config.input)
    if isinstance(loaded, LoadedData):
        # Bind every stage to one namespace over input, full config and schema.
        loaded = replace(loaded, run_namespace=make_namespace(
            sha, key.analytics_config_hash, SCHEMA_VERSION))
    with p.stage(ProgressStage.INITIAL_QUALITY):
        quality: QualityReport = stages.assess_quality(loaded, config.quality)

    metrics: MetricsStore | None = None
    if quality.has_fatal:
        for stage in (ProgressStage.AGGREGATE, ProgressStage.DETECT,
                      ProgressStage.REFINED_QUALITY, ProgressStage.EVIDENCE,
                      ProgressStage.MODEL_READINESS):
            p.skip(stage, "fatal_quality_issue")
        bundle = _empty_bundle(quality)
        model = _ModelResult(RunStatus.DETERMINISTIC_ONLY)
    else:
        with p.stage(ProgressStage.AGGREGATE):
            metrics = stages.aggregate(loaded, quality, config.analytics)
        with p.stage(ProgressStage.DETECT):
            candidates: list[Candidate] = stages.detect_opportunities(
                metrics, quality, config.detectors)
        with p.stage(ProgressStage.REFINED_QUALITY):
            quality = stages.refine_quality(loaded, metrics, candidates, quality, config.quality)
        with p.stage(ProgressStage.EVIDENCE):
            bundle = stages.build_evidence(metrics, quality, candidates, config.evidence)
        model = _run_model(bundle, metrics, config, stages, p, env, monotonic)
        bundle = model.bundle or bundle

    manifest = RunManifest(
        run_id=run_id,
        run_namespace=quality.run_namespace,
        input_sha256=sha,
        analytics_config_hash=key.analytics_config_hash,
        model_profile_hash=key.model_profile_hash,
        prompt_hash=key.prompt_hash,
        tool_version=key.tool_version,
        assumption_hash=key.assumption_hash,
        generated_at=generated_at,
        files={},
        model_id=model.model_id,
        model_revision=model.model_revision,
        model_call_counts=model.model_call_counts,
        incomplete_candidate_ids=[a.candidate_id for a in model.analyses
                                  if a.status not in (AnalysisStatus.COMPLETE,
                                                      AnalysisStatus.REJECTED_INVALID)],
    )
    run = ValidatedRun(
        config=config,
        manifest=manifest,
        bundle=bundle,
        recommendations=tuple(model.recommendations),
        ranking=model.ranking,
        status=model.status,
        analyses=tuple(model.analyses),
        validation_failures=MappingProxyType(dict(model.failures)),
    )
    with p.stage(ProgressStage.RENDER):
        stages.render_report(run)
    p.emit(ProgressStage.COMPLETE, ProgressState.SUCCEEDED, model.status.value)
    return assemble(run, p.events, metrics=metrics, renderer=stages.render_report)


def run_pipeline(
    source: BinaryIO, config: RunConfig, progress: Callable[[ProgressEvent], None]
) -> RunArtifact:
    """load -> initial quality -> aggregate -> candidates -> refined quality -> evidence -> model -> validate -> render."""
    return execute(source, config, progress).artifact


# --- Background jobs -------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


class RunJob:
    """One run on a background thread; the UI polls ``snapshot``."""

    def __init__(self, key: CacheKey, data: bytes, config: RunConfig,
                 stages: PipelineStages | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self.key = key
        self._data = data
        self._config = config
        self._stages = stages
        self._env = env
        self._lock = threading.Lock()
        self._cancel = CancelToken()
        self._events: list[ProgressEvent] = []
        self.status = JobStatus.QUEUED
        self.outcome: RunOutcome | None = None
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, name="adops-run", daemon=True)

    @property
    def run_id(self) -> str | None:
        with self._lock:
            return self._events[0].run_id if self._events else None

    def start(self) -> "RunJob":
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.cancel()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_JOB_STATUSES

    def snapshot(self) -> tuple[JobStatus, tuple[ProgressEvent, ...]]:
        with self._lock:
            return self.status, tuple(self._events)

    def _record(self, event: ProgressEvent) -> None:
        with self._lock:
            self._events.append(event)

    def _run(self) -> None:
        with self._lock:
            self.status = JobStatus.RUNNING
        try:
            outcome = execute(io.BytesIO(self._data), self._config, self._record,
                              stages=self._stages, cancel=self._cancel, env=self._env)
        except RunCancelled as exc:
            final, outcome, error = JobStatus.CANCELLED, None, exc.public_message
        except PipelineError as exc:
            final, outcome, error = JobStatus.FAILED, None, exc.public_message
        except Exception as exc:  # never surface raw text: it may hold private data
            final, outcome, error = JobStatus.FAILED, None, f"Run failed ({type(exc).__name__})."
        else:
            final, error = JobStatus.SUCCEEDED, None
        finally:
            self._data = b""
        with self._lock:
            self.status, self.outcome, self.error = final, outcome, error


class JobManager:
    """Per-session single flight: at most one active job; same key reconnects."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: RunJob | None = None

    def submit(self, key: CacheKey, data: bytes, config: RunConfig,
               stages: PipelineStages | None = None,
               env: Mapping[str, str] | None = None) -> RunJob:
        with self._lock:
            job = self.current
            if job is not None and job.active:
                if job.key == key:
                    return job
                raise RuntimeError("Another run is in progress; cancel it first.")
            self.current = RunJob(key, data, config, stages, env).start()
            return self.current


# --- Per-session cache -----------------------------------------------------


class RunCache:
    """In-memory, per-session cache; entries match only on the complete key."""

    def __init__(self, max_entries: int = 4) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[CacheKey, RunOutcome] = OrderedDict()

    def put(self, outcome: RunOutcome) -> None:
        key = CacheKey.from_manifest(outcome.artifact.manifest)
        self._entries[key] = outcome
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def get(self, key: CacheKey) -> RunOutcome | None:
        """The cached outcome relabelled as a previous run, or None."""
        outcome = self._entries.get(key)
        if outcome is None or CacheKey.from_manifest(outcome.artifact.manifest) != key:
            return None
        return as_cached(outcome)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def load_demo_run() -> RunOutcome:
    """The committed synthetic run, verified and labelled as a cached previous run."""
    bundle = RunBundle.model_validate_json(DEMO_RUN_PATH.read_text(encoding="utf-8"))
    return as_cached(from_bundle(bundle))
