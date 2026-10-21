"""The private run bundle: one in-memory outcome, its files and their hashes.

A finished run is a ``RunOutcome``. ``assemble`` renders it, serializes the
bundle files named in the plan (``quality.json``, ``facts.json`` ...,
``report.md``, ``render_audit.json``) and records each file's SHA-256 in the
manifest. ``read_bundle`` verifies those hashes before trusting a saved run,
so an edited or mixed-up bundle is refused rather than displayed.

Bundles contain private derived data. They are written only to a directory
the caller names explicitly, or offered as a download after an explicit
private action; nothing here picks a default location.
"""

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from contracts.config import RunConfig
from contracts.enums import RunStatus
from contracts.models import (
    Assumption,
    Candidate,
    CandidateAnalysis,
    Fact,
    OverlapGroup,
    Period,
    ProgressEvent,
    QualityReport,
    RankingIR,
    RenderedReport,
    RenderSpan,
    RunArtifact,
    RunManifest,
    Scenario,
    Segment,
    ValidatedRecommendation,
    ValidationIssue,
)
from contracts.runtime import (
    AssumptionRegistry,
    EvidenceBundle,
    FactRegistry,
    MetricsStore,
    ScenarioRegistry,
    ValidatedRun,
)
from report.render import audit_report, render_report

MANIFEST_NAME = "manifest.json"
# Every file except the manifest, in the order they are written.
BUNDLE_FILES = (
    "run.json",
    "quality.json",
    "facts.json",
    "assumptions.json",
    "scenarios.json",
    "candidates.json",
    "analyses.json",
    "validation.json",
    "report.json",
    "report.md",
    "render_audit.json",
)


class BundleError(ValueError):
    """A saved bundle is incomplete, tampered with or inconsistent."""


class _File(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunFile(_File):
    status: RunStatus
    config: RunConfig
    events: list[ProgressEvent]


class CandidatesFile(_File):
    segments: list[Segment]
    periods: list[Period]
    candidates: list[Candidate]
    overlap_groups: list[OverlapGroup]


class ValidationFailure(_File):
    key: str
    errors: list[ValidationIssue]


class ValidationFile(_File):
    recommendations: list[ValidatedRecommendation]
    ranking: RankingIR | None
    failures: list[ValidationFailure]


class RenderAuditFile(_File):
    spans: list[RenderSpan]
    uncovered: list[str]
    unresolved: list[str]


class RunBundle(_File):
    """Everything in a private run bundle, as one validated object."""

    manifest: RunManifest
    run: RunFile
    quality: QualityReport
    facts: list[Fact]
    assumptions: list[Assumption]
    scenarios: list[Scenario]
    candidates: CandidatesFile
    analyses: list[CandidateAnalysis]
    validation: ValidationFile
    report: RenderedReport


@dataclass(frozen=True)
class RunOutcome:
    """A finished run: the contract artifact plus what the app and renderer need."""

    artifact: RunArtifact
    run: ValidatedRun
    report: RenderedReport
    events: tuple[ProgressEvent, ...]
    # Present only for a run computed in this process; drill-down tools need it.
    metrics: MetricsStore | None = None

    @property
    def status(self) -> RunStatus:
        return self.artifact.status


def _dump(model: BaseModel | list[BaseModel]) -> str:
    if isinstance(model, list):
        payload = [m.model_dump(mode="json") for m in model]
    else:
        payload = model.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _files(
    run: ValidatedRun, report: RenderedReport, events: Iterable[ProgressEvent]
) -> dict[str, str]:
    b = run.bundle
    audit = audit_report(report, run)
    return {
        "run.json": _dump(RunFile(status=run.status, config=run.config, events=list(events))),
        "quality.json": _dump(b.quality),
        "facts.json": _dump(b.facts.values()),
        "assumptions.json": _dump(b.assumptions.values()),
        "scenarios.json": _dump(b.scenarios.values()),
        "candidates.json": _dump(CandidatesFile(
            segments=list(b.segments.values()),
            periods=list(b.periods.values()),
            candidates=list(b.candidates.values()),
            overlap_groups=list(b.overlap_groups.values()),
        )),
        "analyses.json": _dump(list(run.analyses)),
        "validation.json": _dump(ValidationFile(
            recommendations=list(run.recommendations),
            ranking=run.ranking,
            failures=[ValidationFailure(key=k, errors=list(v))
                      for k, v in sorted(run.validation_failures.items())],
        )),
        "report.json": _dump(report),
        "report.md": report.markdown,
        "render_audit.json": _dump(RenderAuditFile(
            spans=report.spans, uncovered=list(audit.uncovered), unresolved=list(audit.unresolved)
        )),
    }


def assemble(
    run: ValidatedRun,
    events: Iterable[ProgressEvent],
    metrics: MetricsStore | None = None,
    renderer: Callable[[ValidatedRun], RenderedReport] = render_report,
) -> RunOutcome:
    """Render ``run`` and seal its bundle: file hashes go into the manifest.

    The report header shows manifest identity but not the file table, so the
    report rendered before sealing is identical to one rendered after.
    """
    events = tuple(events)
    report = renderer(run)
    files = _files(run, report, events)
    manifest = run.manifest.model_copy(update={"files": {k: _sha256(v) for k, v in files.items()}})
    run = replace(run, manifest=manifest)
    artifact = RunArtifact(
        status=run.status,
        manifest=manifest,
        quality=run.bundle.quality,
        recommendations=list(run.recommendations),
        ranking=run.ranking,
        private_artifact_names=[MANIFEST_NAME, *BUNDLE_FILES],
    )
    return RunOutcome(artifact=artifact, run=run, report=report, events=events, metrics=metrics)


def bundle_files(outcome: RunOutcome) -> dict[str, str]:
    """All bundle files, manifest included, as UTF-8 text.

    The manifest's file table is recomputed so a relabelled (cached) outcome
    still yields a self-consistent bundle; its identity fields are unchanged.
    """
    files = _files(outcome.run, outcome.report, outcome.events)
    manifest = outcome.run.manifest.model_copy(
        update={"files": {k: _sha256(v) for k, v in files.items()}}
    )
    return {MANIFEST_NAME: _dump(manifest), **files}


def to_bundle(outcome: RunOutcome) -> RunBundle:
    files = bundle_files(outcome)
    return _from_files(files)


def bundle_zip(outcome: RunOutcome) -> bytes:
    """A zip of the bundle for an explicit private download."""
    buffer = io.BytesIO()
    folder = outcome.run.manifest.run_id
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in bundle_files(outcome).items():
            info = zipfile.ZipInfo(f"{folder}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(info, text)
    return buffer.getvalue()


def write_bundle(outcome: RunOutcome, directory: Path) -> Path:
    """Write the bundle to ``directory/<run_id>/``; refuses to overwrite."""
    target = Path(directory) / outcome.run.manifest.run_id
    target.mkdir(parents=True, exist_ok=False)
    for name, text in bundle_files(outcome).items():
        (target / name).write_text(text, encoding="utf-8")
    return target


def _from_files(files: Mapping[str, str]) -> RunBundle:
    missing = [n for n in (MANIFEST_NAME, *BUNDLE_FILES) if n not in files]
    if missing:
        raise BundleError(f"bundle is missing {', '.join(missing)}")
    manifest = RunManifest.model_validate_json(files[MANIFEST_NAME])
    for name in BUNDLE_FILES:
        if manifest.files.get(name) != _sha256(files[name]):
            raise BundleError(f"{name} does not match the manifest hash")
    return RunBundle(
        manifest=manifest,
        run=RunFile.model_validate_json(files["run.json"]),
        quality=QualityReport.model_validate_json(files["quality.json"]),
        facts=[Fact.model_validate(x) for x in json.loads(files["facts.json"])],
        assumptions=[Assumption.model_validate(x) for x in json.loads(files["assumptions.json"])],
        scenarios=[Scenario.model_validate(x) for x in json.loads(files["scenarios.json"])],
        candidates=CandidatesFile.model_validate_json(files["candidates.json"]),
        analyses=[CandidateAnalysis.model_validate(x) for x in json.loads(files["analyses.json"])],
        validation=ValidationFile.model_validate_json(files["validation.json"]),
        report=RenderedReport.model_validate_json(files["report.json"]),
    )


def read_bundle(directory: Path) -> RunOutcome:
    """Load and verify a bundle written by ``write_bundle``."""
    directory = Path(directory)
    files = {}
    for name in (MANIFEST_NAME, *BUNDLE_FILES):
        path = directory / name
        if path.is_file():
            files[name] = path.read_text(encoding="utf-8")
    return from_bundle(_from_files(files))


def from_bundle(bundle: RunBundle) -> RunOutcome:
    """Rebuild an outcome from a verified bundle object; no metrics store is restored."""
    ns = bundle.manifest.run_namespace
    if bundle.quality.run_namespace != ns:
        raise BundleError("quality report belongs to another run namespace")
    try:
        evidence = EvidenceBundle(
            run_namespace=ns,
            facts=FactRegistry(ns, bundle.facts),
            assumptions=AssumptionRegistry(ns, bundle.assumptions),
            scenarios=ScenarioRegistry(ns, bundle.scenarios),
            segments=MappingProxyType({s.segment_id: s for s in bundle.candidates.segments}),
            periods=MappingProxyType({p.period_id: p for p in bundle.candidates.periods}),
            candidates=MappingProxyType(
                {c.candidate_id: c for c in bundle.candidates.candidates}
            ),
            overlap_groups=MappingProxyType(
                {o.overlap_group_id: o for o in bundle.candidates.overlap_groups}
            ),
            quality=bundle.quality,
            packs=MappingProxyType({}),
        )
    except ValueError as exc:
        raise BundleError(str(exc)) from exc
    if any(r.run_namespace != ns for r in bundle.validation.recommendations):
        raise BundleError("a recommendation belongs to another run namespace")
    run = ValidatedRun(
        config=bundle.run.config,
        manifest=bundle.manifest,
        bundle=evidence,
        recommendations=tuple(bundle.validation.recommendations),
        ranking=bundle.validation.ranking,
        status=bundle.run.status,
        analyses=tuple(bundle.analyses),
        validation_failures=MappingProxyType(
            {f.key: tuple(f.errors) for f in bundle.validation.failures}
        ),
    )
    artifact = RunArtifact(
        status=bundle.run.status,
        manifest=bundle.manifest,
        quality=bundle.quality,
        recommendations=list(bundle.validation.recommendations),
        ranking=bundle.validation.ranking,
        private_artifact_names=[MANIFEST_NAME, *BUNDLE_FILES],
    )
    return RunOutcome(
        artifact=artifact, run=run, report=bundle.report, events=tuple(bundle.run.events)
    )


def as_cached(outcome: RunOutcome) -> RunOutcome:
    """Relabel a stored outcome as a previous run and re-render its report.

    The manifest, and so the inference generation time, input hash and model
    identity, is unchanged; only the status and its banner change.
    """
    run = replace(outcome.run, status=RunStatus.CACHED_PREVIOUS)
    report = render_report(run)
    artifact = outcome.artifact.model_copy(update={"status": RunStatus.CACHED_PREVIOUS})
    return replace(outcome, artifact=artifact, run=run, report=report)
