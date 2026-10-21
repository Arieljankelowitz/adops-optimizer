"""Hand-built synthetic exports for loader and quality-rule tests.

``clean_rows`` returns an internally consistent export that triggers no
data-dependent quality rule; tests mutate copies of it to cross one rule's
threshold at a time. All labels and numbers are invented.
"""

import io
import random
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType

import pandas as pd

from contracts.config import InputConfig, QualityConfig
from contracts.enums import (
    DIMENSION_COLUMNS,
    REQUIRED_COLUMNS,
    CoverageStatus,
    DetectorFamily,
    Dimension,
    EligibilityStatus,
    InputColumn as C,
    QualityRuleId,
    RuleOutcome,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import Candidate, QualityIssue, QualityReport, Segment
from contracts.runtime import LoadedData, MetricsStore
from pipeline.load import load_csv
from pipeline.quality import assess_quality
from pipeline.scope import period, segment

HEADER = [c.value for c in REQUIRED_COLUMNS]
START = date(2031, 5, 1)
CENT = Decimal("0.01")
Row = dict[str, str]

SEGMENTS = [
    (supply, country, size, placement, device)
    for supply, country in (("1001", "US"), ("1002", "DE"), ("1003", "FR"), ("1004", "JP"))
    for size, placement in (("300x250", "Banner"), ("728x90", "Video"))
    for device in ("Desktop", "Mobile")
]


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def make_row(day: date, key: tuple[str, ...], requests: int, ecpm: Decimal,
             response_rate: Decimal = Decimal("0.3"), floor: str = "0.50",
             bid: str = "0.80") -> Row:
    """One consistent row: rates reconstruct revenue, profit = revenue - cost."""
    responses = int(requests * response_rate)
    wins = int(responses * Decimal("0.3"))
    impressions = int(wins * Decimal("0.9"))
    revenue = _money(Decimal(impressions) * ecpm / 1000)
    cost = _money(revenue * Decimal("0.7"))
    srpm = _money(revenue / requests * 1_000_000) if requests else Decimal(0)
    exported_ecpm = _money(revenue / impressions * 1000) if impressions else Decimal(0)
    return dict(zip(HEADER, [
        day.isoformat(), *key, str(requests), str(responses), floor,
        bid if responses else "0", str(srpm), str(wins), str(impressions), str(revenue),
        str(cost), str(exported_ecpm), str(revenue - cost),
    ], strict=True))


def clean_rows(days: int = 7, seed: int = 11, segments: Iterable[tuple[str, ...]] = SEGMENTS,
               base_requests: tuple[int, int] = (3_500_001, 4_499_999)) -> list[Row]:
    rng = random.Random(seed)
    rows = []
    for key in segments:
        ecpm = Decimal(str(round(rng.uniform(1.5, 3.0), 2)))
        for d in range(days):
            requests = rng.randint(*base_requests)
            rows.append(make_row(START + timedelta(days=d), key, requests, ecpm))
    return rows


def to_csv(rows: Iterable[Row], header: list[str] = HEADER) -> bytes:
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for row in rows:
        buf.write(",".join(row[h] for h in header) + "\n")
    return buf.getvalue().encode()


def load(rows: Iterable[Row], config: InputConfig = InputConfig()) -> LoadedData:
    return load_csv(io.BytesIO(to_csv(rows)), config)


def assess(rows: Iterable[Row], config: QualityConfig = QualityConfig()) -> QualityReport:
    return assess_quality(load(rows), config)


def outcome(report: QualityReport, rule: QualityRuleId) -> RuleOutcome:
    return next(r.outcome for r in report.rule_results if r.rule_id is rule)


def issues(report: QualityReport, rule: QualityRuleId, template: str | None = None
           ) -> list[QualityIssue]:
    return [i for i in report.issues if i.rule_id is rule
            and (template is None or i.message_template_id == template)]


def scale(rows: list[Row], where: Callable[[Row], bool], column: C, factor: str) -> None:
    """Multiply a counter column (and dependent counters/money) on matching rows."""
    for i, row in enumerate(rows):
        if where(row):
            day = date.fromisoformat(row[C.DAY.value])
            key = tuple(row[c.value] for c in DIMENSION_COLUMNS.values())
            requests = int(Decimal(row[C.DEMAND_REQUESTS.value]) * Decimal(factor)) if (
                column is C.DEMAND_REQUESTS) else int(row[C.DEMAND_REQUESTS.value])
            ecpm = Decimal(row[C.DEMAND_ECPM.value]) * (
                Decimal(factor) if column is C.DEMAND_ECPM else 1)
            rows[i] = make_row(day, key, requests, ecpm)


def key_filters(key: tuple[str, ...]) -> dict[Dimension, str]:
    return dict(zip(DIMENSION_COLUMNS, key, strict=True))


def candidate(namespace: str, seg: Segment, period_id: str, name: str,
              family: DetectorFamily = DetectorFamily.SCALE_WINNER,
              eligibility: EligibilityStatus = EligibilityStatus.ELIGIBLE) -> Candidate:
    return Candidate(
        candidate_id=make_id(IdPrefix.CANDIDATE, namespace, name),
        family=family, segment_id=seg.segment_id, period_id=period_id, eligibility=eligibility,
        trigger_fact_ids=[make_id(IdPrefix.FACT, namespace, f"trigger {name}")],
        daily_fact_ids=[], peer_fact_ids=[], counter_evidence_fact_ids=[],
        quality_issue_ids=[], overlap_group_ids=[],
    )


def metrics_for(data: LoadedData, report: QualityReport, filters: Iterable[dict[Dimension, str]],
                flags: dict[str, tuple[str, ...]] | None = None
                ) -> tuple[MetricsStore, dict[str, Segment], str]:
    """A minimal MetricsStore mock: the given segments over the full observed period."""
    ns = report.run_namespace
    full = period(ns, data.dates, CoverageStatus.UNVERIFIED)
    segments = {}
    for f in filters:
        seg = segment(ns, f)
        segments[seg.segment_id] = seg
    store = MetricsStore(
        run_namespace=ns, segments=MappingProxyType(segments),
        periods=MappingProxyType({full.period_id: full}), aggregates=pd.DataFrame(),
        daily=pd.DataFrame(), quality_flags=MappingProxyType(flags or {}),
    )
    return store, segments, full.period_id
