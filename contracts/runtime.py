"""In-process contract types that are not exchanged as JSON.

These hold dataframes or behaviour (registries, protocols) and are passed
between pipeline stages inside one run. Their serializable parts are the
models in ``contracts.models``.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Protocol

import pandas as pd
from pydantic import Field

from contracts.config import RunConfig
from contracts.enums import BudgetReason, RunStatus, ToolName
from contracts.models import (
    Assumption,
    Candidate,
    CandidateAnalysis,
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
    ValidationIssue,
)


class _Registry[T: (Fact, Assumption, Scenario)]:
    """Append-only, namespace-bound mapping from ID to an immutable record.

    ``extended`` returns a new registry; existing records are never replaced
    and records from another run namespace are refused.
    """

    _id_field: str

    def __init__(self, namespace: str, items: Iterable[T] = ()) -> None:
        self._namespace = namespace
        records: dict[str, T] = {}
        for item in items:
            key = getattr(item, self._id_field)
            if item.run_namespace != namespace:
                raise ValueError(f"{key} belongs to another run namespace")
            if key in records:
                raise ValueError(f"{key} is already registered; records are immutable")
            # Frozen pydantic models still contain mutable lists/dicts. Own a
            # snapshot and never hand its nested containers to a caller.
            records[key] = item.model_copy(deep=True)
        self._items = MappingProxyType(records)

    @property
    def namespace(self) -> str:
        return self._namespace

    def extended(self, items: Iterable[T]) -> "_Registry[T]":
        return type(self)(self.namespace, (*self._items.values(), *items))

    def get(self, key: str) -> T | None:
        item = self._items.get(key)
        return item.model_copy(deep=True) if item is not None else None

    def __getitem__(self, key: str) -> T:
        return self._items[key].model_copy(deep=True)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def values(self) -> list[T]:
        return [item.model_copy(deep=True) for item in self._items.values()]


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


class FileError(StrEnum):
    """File-level failures that leave no interpretable rows (all DQ01 fatal)."""

    EMPTY_FILE = "empty_file"
    UNDECODABLE = "undecodable"
    MALFORMED_CSV = "malformed_csv"
    NO_DATA_ROWS = "no_data_rows"
    MISSING_COLUMNS = "missing_columns"
    DUPLICATE_COLUMNS = "duplicate_columns"
    BYTE_LIMIT = "byte_limit"
    ROW_LIMIT = "row_limit"


@dataclass(frozen=True)
class ParseAudit:
    """What the loader could not interpret; quality rules turn it into issues."""

    file_errors: tuple[FileError, ...] = ()
    missing_columns: tuple[str, ...] = ()
    duplicate_columns: tuple[str, ...] = ()
    extra_columns: tuple[str, ...] = ()
    byte_count: int = 0
    # Source lines whose field count differs from the header; not in the frame.
    malformed_lines: tuple[int, ...] = ()
    # Column name -> frame indices whose cell failed strict parsing (value is None).
    invalid_cells: Mapping[str, tuple[int, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # Frame indices whose Day cell is blank.
    missing_dates: tuple[int, ...] = ()
    # Label the loader gave blank dimension cells.
    unknown_dimension_label: str = "UNKNOWN"

    @property
    def readable(self) -> bool:
        return not self.file_errors


@dataclass(frozen=True)
class LoadedData:
    """Parsed export. ``frame`` keeps one row per source line, labels as strings.

    Columns are the 17 required header names. ``Day`` holds ``datetime.date``,
    dimensions hold whitespace-normalized strings, numeric columns hold exact
    ``Decimal`` values; a cell that failed parsing is ``None``, never zero.
    """

    frame: pd.DataFrame
    input_sha256: str
    # Frame index -> 1-based source line number (header is line 1).
    source_lines: Mapping[int, int]
    # Normalized label -> raw labels, per dimension, for reversible auditing.
    normalization_audit: Mapping[str, Mapping[str, tuple[str, ...]]]
    precision: PrecisionMetadata
    dates: tuple[date, ...]
    parse_audit: ParseAudit = field(default_factory=ParseAudit)
    # Set by the runner to bind every stage to one run namespace; when None,
    # quality derives it from the input hash and the quality config hash.
    run_namespace: str | None = None


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
    # Input hash for fact provenance; ``aggregate`` always sets it.
    input_sha256: str | None = None


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
    # Ranking is reference-only and can resolve only previously accepted IR.
    validated_recommendations: Mapping[str, ValidatedRecommendation] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # Candidates that got no pack, and why; they stay in ``candidates``.
    unpacked: Mapping[str, BudgetReason] = field(
        default_factory=lambda: MappingProxyType({})
    )


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
    # Run outcome and audit trail for the report header and exclusions section.
    status: RunStatus = RunStatus.FINAL
    analyses: tuple[CandidateAnalysis, ...] = ()
    # Candidate ID (or "ranking") -> machine-readable reasons its output was rejected.
    validation_failures: Mapping[str, tuple[ValidationIssue, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )


# --- Model transport -------------------------------------------------------


class ChatMessage(Contract):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, object]] | None = None


class ModelRequest(Contract):
    messages: list[ChatMessage] = Field(min_length=1)
    response_schema_name: str = Field(pattern=r"^[a-z0-9_]+$")
    response_schema: dict[str, object]
    max_output_tokens: int = Field(gt=0)
    # Native dispatch is explicit and compatibility-gated; empty uses JSON fallback.
    tools: list[dict[str, object]] = Field(default_factory=list, max_length=3)
    # Thinking is disabled on every call; the type admits no other value.
    enable_thinking: Literal[False] = False


class ModelResponse(Contract):
    content: str
    prompt_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    finish_reason: str
    tool_calls: list[dict[str, object]] = Field(default_factory=list, max_length=1)


class ModelClient(Protocol):
    """Transport to the approved self-hosted endpoint. No hosted fallback."""

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class ToolRouter(Protocol):
    """Dispatches validated tool calls to the deterministic drill-down tools."""

    @property
    def allowed_tools(self) -> frozenset[ToolName]: ...

    def dispatch(self, candidate_id: str, call: ToolCall) -> ToolEvidence: ...
