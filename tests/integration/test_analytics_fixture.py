"""Analytics over the committed synthetic fixture: planted slices become candidates."""

import json
from decimal import Decimal

import pytest

from contracts.config import AnalyticsConfig, DetectorConfig, QualityConfig
from contracts.enums import DIMENSION_COLUMNS, REQUIRED_COLUMNS, CoverageStatus
from contracts.enums import DetectorFamily as F
from contracts.enums import EligibilityStatus as E
from contracts.enums import QualityPass, QualityRuleId
from pipeline.detectors import run_detection, with_detection_flags
from pipeline.metrics import FULL_INPUT, aggregate, full_period, segment_id_for
from pipeline.quality import RANK_SENSITIVE_FLAG, assess_quality, refine_quality
from tests.support.analytics_data import FIXTURE, load_fixture

PLANTED = json.loads(FIXTURE.with_name("ad_performance.traps.json").read_text())["planted_slices"]


@pytest.fixture(scope="module")
def run():
    data = load_fixture()
    quality = assess_quality(data, QualityConfig())
    store = aggregate(data, quality, AnalyticsConfig())
    return data, store, run_detection(store, quality, DetectorConfig()), quality


def _families(store, detection, name):
    key = PLANTED[name]["key"]
    sid = segment_id_for(store.run_namespace, {d: key[c.value] for d, c in DIMENSION_COLUMNS.items()})
    return {(c.family, c.eligibility) for c in detection.candidates if c.segment_id == sid}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("scale_winner", (F.SCALE_WINNER, E.ELIGIBLE)),
        ("wasted_requests", (F.WASTED_REQUESTS, E.ELIGIBLE)),
        ("high_bid_floor", (F.FLOOR_HIGH_BID, E.ELIGIBLE)),
        ("wins_impressions_gap", (F.WINS_IMPRESSIONS_DISCREPANCY, E.ELIGIBLE)),
        # Below the volume gates: kept in the labelled exploratory table.
        ("negative_margin", (F.MARGIN_LEAK, E.EXPLORATORY)),
        # High yield earned on two days is never a stable scale winner.
        ("two_day_spike", (F.SCALE_WINNER, E.EXPLORATORY)),
    ],
)
def test_planted_slice_is_a_candidate(run, name, expected) -> None:
    _, store, detection, _ = run
    assert expected in _families(store, detection, name)


def test_two_day_spike_is_not_eligible_anywhere(run) -> None:
    _, store, detection, _ = run
    assert all(e is not E.ELIGIBLE for _, e in _families(store, detection, "two_day_spike"))


def test_account_totals_match_the_raw_file(run) -> None:
    data, store, _, _ = run
    frame = data.frame
    agg = store.aggregates
    account = agg[(agg["level"] == "account") & (agg["cohort_id"] == FULL_INPUT)
                  & (agg["period_id"] == full_period(store).period_id)].iloc[0]
    assert account["requests"] == sum(frame["Demand Requests"], Decimal(0))
    for column, key in (("Revenue", "revenue"), ("Cost", "cost"), ("Profit $", "profit")):
        assert account[key] == sum(frame[column], Decimal(0))
    assert account["row_count"] == len(frame)
    assert len(REQUIRED_COLUMNS) == 17


def test_every_candidate_has_daily_and_coverage_evidence(run) -> None:
    _, _, detection, _ = run
    for candidate in detection.candidates:
        metrics = {detection.facts[i].metric.value for i in candidate.daily_fact_ids}
        assert {"requests", "revenue", "profit"} <= metrics
        for fid in candidate.trigger_fact_ids:
            assert fid in detection.facts


def test_partial_final_day_is_carried_from_the_quality_report(run) -> None:
    _, store, _, quality = run
    assert any(i.rule_id is QualityRuleId.DQ06 for i in quality.issues)
    assert full_period(store).coverage_status is CoverageStatus.POSSIBLY_INCOMPLETE
    assert len([p for p in store.periods.values() if len(p.included_dates) > 1]) == 2


def test_refined_quality_consumes_detector_flags(run) -> None:
    data, store, detection, quality = run
    flagged = with_detection_flags(store, detection)
    assert store.quality_flags is not flagged.quality_flags
    sensitive = detection.sensitivity.sensitive_candidate_ids
    assert all(RANK_SENSITIVE_FLAG in flagged.quality_flags[c] for c in sensitive)
    refined = refine_quality(data, flagged, list(detection.candidates), quality, QualityConfig())
    assert refined.quality_pass is QualityPass.REFINED
    assert {i.issue_id for i in quality.issues} <= {i.issue_id for i in refined.issues}
    rank_issues = [i for i in refined.issues if i.message_template_id == "DQ19_rank_sensitive"]
    assert {c for i in rank_issues for c in i.affected_candidate_ids} == set(sensitive)
