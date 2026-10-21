"""The frozen core signatures: names, parameters and annotations, all still stubs."""

import inspect
import typing
from collections.abc import Callable
from typing import BinaryIO

import pytest

import runner
from contracts.config import (
    AnalyticsConfig,
    DetectorConfig,
    EvidenceConfig,
    InputConfig,
    QualityConfig,
    RunBudget,
    RunConfig,
)
from contracts.enums import Dimension
from contracts.models import (
    Candidate,
    CandidateAnalysis,
    CandidatePack,
    ProgressEvent,
    QualityReport,
    RankingIR,
    RenderedReport,
    RunArtifact,
    ScenarioRequest,
    ToolEvidence,
    ValidatedRecommendation,
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
    ToolRouter,
    ValidatedRun,
)
from llm import analysis, ranking, tools, validator
from pipeline import detectors, evidence, load, metrics, quality, scenarios
from report import render

EXPECTED: list[tuple[Callable[..., object], dict[str, object]]] = [
    (load.load_csv, {"source": BinaryIO, "config": InputConfig, "return": LoadedData}),
    (quality.assess_quality,
     {"data": LoadedData, "config": QualityConfig, "return": QualityReport}),
    (quality.refine_quality, {
        "data": LoadedData, "metrics": MetricsStore, "candidates": list[Candidate],
        "initial": QualityReport, "config": QualityConfig, "return": QualityReport,
    }),
    (metrics.aggregate, {
        "data": LoadedData, "quality": QualityReport, "config": AnalyticsConfig,
        "return": MetricsStore,
    }),
    (detectors.detect_opportunities, {
        "metrics": MetricsStore, "quality": QualityReport, "config": DetectorConfig,
        "return": list[Candidate],
    }),
    (evidence.build_evidence, {
        "metrics": MetricsStore, "quality": QualityReport, "candidates": list[Candidate],
        "config": EvidenceConfig, "return": EvidenceBundle,
    }),
    (scenarios.compute_scenarios, {
        "registry": FactRegistry, "assumptions": AssumptionRegistry,
        "requests": list[ScenarioRequest], "return": ScenarioRegistry,
    }),
    (analysis.analyze_candidate, {
        "pack": CandidatePack, "tools": ToolRouter, "client": ModelClient,
        "budget": RunBudget, "return": CandidateAnalysis,
    }),
    (ranking.rank_recommendations, {
        "validated": list[ValidatedRecommendation], "registry": FactRegistry,
        "client": ModelClient, "budget": RunBudget, "return": RankingIR,
    }),
    (validator.validate_analysis, {
        "output": CandidateAnalysis | RankingIR, "bundle": EvidenceBundle,
        "return": ValidationResult,
    }),
    (render.render_report, {"run": ValidatedRun, "return": RenderedReport}),
    (runner.run_pipeline, {
        "source": BinaryIO, "config": RunConfig,
        "progress": Callable[[ProgressEvent], None], "return": RunArtifact,
    }),
    (tools.breakdown, {
        "ctx": AnalysisContext, "segment_id": str, "by": Dimension, "period_id": str,
        "limit": int, "return": ToolEvidence,
    }),
    (tools.daily_trend,
     {"ctx": AnalysisContext, "segment_id": str, "period_id": str, "return": ToolEvidence}),
    (tools.compare_to_peers, {
        "ctx": AnalysisContext, "segment_id": str, "period_id": str,
        "peer_definition_id": str, "return": ToolEvidence,
    }),
]


@pytest.mark.parametrize("func,expected", EXPECTED, ids=lambda v: getattr(v, "__name__", ""))
def test_signature_is_frozen(func: Callable[..., object], expected: dict[str, object]) -> None:
    hints = typing.get_type_hints(func, include_extras=True)
    assert hints == expected
    params = list(inspect.signature(func).parameters)
    assert params == [k for k in expected if k != "return"]


def test_breakdown_default_limit() -> None:
    assert inspect.signature(tools.breakdown).parameters["limit"].default == 10


@pytest.mark.parametrize("func,expected", EXPECTED, ids=lambda v: getattr(v, "__name__", ""))
def test_stubs_are_not_implemented(
    func: Callable[..., object], expected: dict[str, object]
) -> None:
    args = [object() for k in expected if k not in ("return", "limit")]
    with pytest.raises(NotImplementedError):
        func(*args)
