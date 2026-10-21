"""Every planted data-quality trap in the synthetic fixture is flagged by its rule.

Candidate-dependent rules run through ``refine_quality`` with a mock
MetricsStore whose segments are the planted slices and their supply parents.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pandas as pd
import pytest

from contracts.config import InputConfig, QualityConfig
from contracts.enums import (
    DIMENSION_COLUMNS,
    CoverageStatus,
    DetectorFamily,
    Dimension,
    InputColumn as C,
    MetricKey as M,
    QualityRuleId as R,
    Severity,
)
from contracts.models import QualityReport
from contracts.runtime import MetricsStore
from pipeline.load import load_csv
from pipeline.quality import assess_quality, refine_quality, render_message
from pipeline.scope import period, segment
from tests.quality_support import candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "ad_performance.csv"
TRAPS = json.loads(FIXTURE.with_name("ad_performance.traps.json").read_text())
CONFIG = QualityConfig()
COLUMN_DIMENSION = {c.value: d for d, c in DIMENSION_COLUMNS.items()}
# Planted slices aimed at detectors, not data quality: they must stay clean of
# slice-level coverage and concentration warnings.
DETECTOR_SLICES = ("wasted_requests", "scale_winner", "negative_margin", "high_bid_floor",
                   "wins_impressions_gap")


@pytest.fixture(scope="module")
def reports() -> tuple[QualityReport, QualityReport]:
    with FIXTURE.open("rb") as source:
        data = load_csv(source, InputConfig())
    initial = assess_quality(data, CONFIG)
    ns = initial.run_namespace
    full = period(ns, data.dates, CoverageStatus.POSSIBLY_INCOMPLETE)
    segments, candidates = {}, []
    for name, planted in TRAPS["planted_slices"].items():
        seg = segment(ns, _filters(planted["key"]))
        parent = segment(ns, {Dimension.SUPPLY_ID: planted["key"][C.SUPPLY_ID.value]})
        segments[seg.segment_id] = seg
        segments[parent.segment_id] = parent
        candidates.append(candidate(ns, seg, full.period_id, name, DetectorFamily.SCALE_WINNER))
    store = MetricsStore(
        run_namespace=ns, segments=MappingProxyType(segments),
        periods=MappingProxyType({full.period_id: full}), aggregates=pd.DataFrame(),
        daily=pd.DataFrame(),
    )
    return initial, refine_quality(data, store, candidates, initial, CONFIG)


def _filters(key: dict[str, str]) -> dict[Dimension, str]:
    return {COLUMN_DIMENSION[k]: v for k, v in key.items() if k in COLUMN_DIMENSION}


def _segment_id(report: QualityReport, key: dict[str, str]) -> str:
    return segment(report.run_namespace, _filters(key)).segment_id


def _issues(report: QualityReport, rule: R, template: str | None = None):
    return [i for i in report.issues if i.rule_id is rule
            and (template is None or i.message_template_id == template)]


def _facts(report: QualityReport):
    return {f.fact_id: f for f in report.facts}


def test_fixture_has_no_fatal_issue(reports) -> None:
    initial, refined = reports
    assert not initial.has_fatal and not refined.has_fatal


def test_every_message_renders(reports) -> None:
    _, refined = reports
    facts = _facts(refined)
    for issue in refined.issues:
        text = render_message(issue, facts)
        assert "{" not in text and "(not available)" not in text, issue.message_template_id


def test_partial_final_day_dq06(reports) -> None:
    initial, _ = reports
    trap = TRAPS["partial_final_day"]
    days = {_facts(initial)[i.measured_fact_ids[0]].period.start
            for i in _issues(initial, R.DQ06, "DQ06_possible_partial_day")}
    assert days == {date.fromisoformat(trap["day"])}


def test_request_granularity_dq16(reports) -> None:
    initial, _ = reports
    (issue,) = _issues(initial, R.DQ16)
    fact = _facts(initial)[issue.measured_fact_ids[0]]
    assert fact.metric is M.COUNTER_GRANULARITY
    assert fact.provenance.source_columns == [C.DEMAND_REQUESTS]
    assert fact.value == TRAPS["request_granularity"]


def test_srpm_per_million_dq13(reports) -> None:
    initial, _ = reports
    assert TRAPS["srpm_multiplier"] == 1_000_000
    (issue,) = _issues(initial, R.DQ13)
    assert issue.message_template_id == "DQ13_srpm_per_million_confirmed"


@pytest.mark.parametrize("name,rule,dimension", [
    ("placeholder_country", R.DQ10, Dimension.COUNTRY),
    ("placeholder_size", R.DQ11, Dimension.SIZE),
])
def test_placeholder_labels(reports, name: str, rule: R, dimension: Dimension) -> None:
    initial, _ = reports
    label = _filters(TRAPS["planted_slices"][name]["key"])[dimension]
    assert label in TRAPS["placeholders"][dimension.value]
    expected = segment(initial.run_namespace, {dimension: label}).segment_id
    assert any(expected in i.segment_ids for i in _issues(initial, rule))


def test_intermittent_slice_dq07(reports) -> None:
    _, refined = reports
    planted = TRAPS["planted_slices"]["intermittent"]
    flagged = {s for i in _issues(refined, R.DQ07) for s in i.segment_ids}
    assert flagged == {_segment_id(refined, planted["key"])}
    (issue,) = _issues(refined, R.DQ07)
    assert _facts(refined)[issue.measured_fact_ids[0]].value == len(planted["days"])


def test_two_day_spike_dq08_and_dq09(reports) -> None:
    _, refined = reports
    planted = TRAPS["planted_slices"]["two_day_spike"]
    target = _segment_id(refined, planted["key"])
    spike_days = sorted(date.fromisoformat(d) for d in planted["spike_days"])
    (concentration,) = [i for i in _issues(refined, R.DQ08, "DQ08_concentrated_revenue")
                        if target in i.segment_ids]
    top_two = _facts(refined)[concentration.measured_fact_ids[0]]
    assert top_two.metric is M.TOP_TWO_DAY_REVENUE_SHARE
    assert top_two.period.included_dates == spike_days
    assert top_two.value >= CONFIG.max_two_day_share
    spikes = {_facts(refined)[i.measured_fact_ids[0]].period.start
              for i in _issues(refined, R.DQ09, "DQ09_revenue_spike") if target in i.segment_ids}
    assert spikes == set(spike_days)
    dq19 = {i.message_template_id for i in _issues(refined, R.DQ19)
            if target in i.segment_ids}
    assert "DQ19_flagged_day_dependent" in dq19


def test_detector_slices_raise_no_slice_coverage_or_concentration_warnings(reports) -> None:
    _, refined = reports
    for name in DETECTOR_SLICES:
        target = _segment_id(refined, TRAPS["planted_slices"][name]["key"])
        for rule in (R.DQ07, R.DQ08):
            assert not [i for i in _issues(refined, rule) if target in i.segment_ids], (name, rule)


@pytest.mark.parametrize("trap,rule,template,severity", [
    ("responses_over_requests", R.DQ04, "DQ04_counter_relationship", Severity.WARNING),
    ("impressions_without_requests", R.DQ04, "DQ04_impressions_without_requests",
     Severity.WARNING),
    ("impressions_without_requests", R.DQ05, "DQ05_money_without_requests", Severity.WARNING),
    ("profit_cent_discrepancy", R.DQ12, "DQ12_reconciliation_within_bound", Severity.NOTICE),
    ("profit_over_rounding_bound", R.DQ12, "DQ12_reconciliation_exceeds_bound",
     Severity.WARNING),
    ("rounding_zero_revenue", R.DQ18, "DQ18_zero_revenue_rounding", Severity.NOTICE),
])
def test_row_traps(reports, trap: str, rule: R, template: str, severity: Severity) -> None:
    initial, _ = reports
    flagged = [i for i in _issues(initial, rule, template) if i.severity is severity]
    covered = {s for i in flagged for s in i.segment_ids}
    for row in TRAPS["row_traps"][trap]:
        assert _segment_id(initial, row) in covered, (trap, row)


def test_cent_discrepancy_is_not_called_a_bound_breach(reports) -> None:
    initial, _ = reports
    (exceeds,) = _issues(initial, R.DQ12, "DQ12_reconciliation_exceeds_bound")
    cent_rows = {_segment_id(initial, r) for r in TRAPS["row_traps"]["profit_cent_discrepancy"]}
    assert not cent_rows & set(exceeds.segment_ids)
    assert _facts(initial)[exceeds.measured_fact_ids[0]].value == Decimal("0.25")
