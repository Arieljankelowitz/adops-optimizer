"""In-process contract types that are not exchanged as JSON.

These hold dataframes or behaviour (registries, protocols) and are passed
between pipeline stages inside one run. Their serializable parts are the
models in ``contracts.models``.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Protocol

import pandas as pd
from pydantic import Field

from contracts.config import RunConfig
from contracts.enums import ToolName
from contracts.models import (
    Assumption,
    Candidate,
    CandidatePack,
    Contract,
    Fact,
    OverlapGroup,
    Period,
    QualityReport,
    RankingIR,
    RunManifest,
    Scenario,
    Segment,
    ToolCall,
    ToolEvidence,
    ValidatedRecommendation,
)


class _Registry[T: (Fact, Assumption, Scenario)]:
    """Append-only, namespace-bound mapping from ID to an immutable record.

    ``extended`` returns a new registry; existing records are never replaced
    and records from another run namespace are refused.
    """

    _id_field: str

    def __init__(self, namespace: str, items: Iterable[T] = ()) -> None:
        self.namespace = namespace
        self._items: dict[str, T] = {}
        self._add(items)

    def _add(self, items: Iterable[T]) -> None:
        for item in items:
            key = getattr(item, self._id_field)
            if item.run_namespace != self.namespace:
                raise ValueError(f"{key} belongs to another run namespace")
            if key in self._items:
                raise ValueError(f"{key} is already registered; records are immutable")
            self._items[key] = item

    def extended(self, items: Iterable[T]) -> "_Registry[T]":
        new = type(self)(self.namespace, self._items.values())
        new._add(items)
        return new

    def get(self, key: str) -> T | None:
        return self._items.get(key)

    def __getitem__(self, key: str) -> T:
        return self._items[key]

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def values(self) -> list[T]:
        return list(self._items.values())


class FactRegistry(_Registry[Fact]):
    _id_field = "fact_id"


class AssumptionRegistry(_Registry[Assumption]):
    _id_field = "assumption_id"


class ScenarioRegistry(_Registry[Scenario]):
    _id_field = "scenario_id"


@dataclass(frozen=True)
class PrecisionMetadata:
    money_quantum: Decimal
    rate_quantum: Decimal


@dataclass(frozen=True)
class LoadedData:
    """Parsed export. ``frame`` keeps one row per source line, labels as strings."""

    frame: pd.DataFrame
    input_sha256: str
    # Frame index -> 1-based source line number (header is line 1).
    source_lines: Mapping[int, int]
    # Normalized label -> raw labels, per dimension, for reversible auditing.
    normalization_audit: Mapping[str, Mapping[str, tuple[str, ...]]]
    precision: PrecisionMetadata
    dates: tuple[date, ...]


@dataclass(frozen=True)
class MetricsStore:
    """Immutable aggregates keyed by segment and period IDs; never sent to the model."""

    run_namespace: str
    segments: Mapping[str, Segment]
    periods: Mapping[str, Period]
    # Additive sums per (segment_id, period_id); daily rows per (segment_id, date).
    aggregates: pd.DataFrame
    daily: pd.DataFrame
    quality_flags: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class EvidenceBundle:
    run_namespace: str
    facts: FactRegistry
    assumptions: AssumptionRegistry
    scenarios: ScenarioRegistry
    segments: Mapping[str, Segment]
    periods: Mapping[str, Period]
    candidates: Mapping[str, Candidate]
    overlap_groups: Mapping[str, OverlapGroup]
    quality: QualityReport
    packs: Mapping[str, CandidatePack]


@dataclass(frozen=True)
class AnalysisContext:
    """What a drill-down tool may read: aggregates and the current registry."""

    run_namespace: str
    metrics: MetricsStore
    bundle: EvidenceBundle


@dataclass(frozen=True)
class ValidatedRun:
    """Everything the renderer needs; only validated model output is included."""

    config: RunConfig
    manifest: RunManifest
    bundle: EvidenceBundle
    recommendations: tuple[ValidatedRecommendation, ...]
    ranking: RankingIR | None


# --- Model transport -------------------------------------------------------


class ChatMessage(Contract):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelRequest(Contract):
    messages: list[ChatMessage] = Field(min_length=1)
    response_schema_name: str = Field(pattern=r"^[a-z0-9_]+$")
    response_schema: dict[str, object]
    max_output_tokens: int = Field(gt=0)
    # Thinking is disabled on every call; the type admits no other value.
    enable_thinking: Literal[False] = False


class ModelResponse(Contract):
    content: str
    prompt_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    finish_reason: str


class ModelClient(Protocol):
    """Transport to the approved self-hosted endpoint. No hosted fallback."""

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class ToolRouter(Protocol):
    """Dispatches validated tool calls to the deterministic drill-down tools."""

    @property
    def allowed_tools(self) -> frozenset[ToolName]: ...

    def dispatch(self, candidate_id: str, call: ToolCall) -> ToolEvidence: ...
