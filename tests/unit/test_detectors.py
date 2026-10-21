"""Each detector family fires on a purpose-built case and stays quiet on a near miss.

Every case sits on the same background: four large ordinary slices earning
1.00 revenue and 0.30 profit per million requests.
"""

from decimal import Decimal

import pytest

from contracts.config import AnalyticsConfig, DetectorConfig
from contracts.enums import (
    DetectorFamily as F,
    EligibilityStatus as E,
    FactKind,
    InferenceRestriction,
    QualityRuleId,
    Severity,
    ValidityStatus,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import QualityIssue
from pipeline.detectors import candidate_evidence, detect_opportunities, run_detection, threshold_sensitive
from pipeline.metrics import FIVE, aggregate, combined_stats, full_period, period_id_for, segment_id_for
from tests.support.analytics_data import DAYS, background, loaded_from_rows, quality_for, slice_rows

T = ("777", "DE", "300x600", "Banner", "Tablet")


def _detect(rows, issues=(), config=DetectorConfig(), analytics=AnalyticsConfig()):
    data = loaded_from_rows(rows)
    quality = quality_for(data, issues)
    store = aggregate(data, quality, analytics)
    return store, quality, run_detection(store, quality, config)


def _found(store, detection, family, key=T):
    sid = segment_id_for(store.run_namespace, dict(zip(FIVE, key)))
    return {c.eligibility for c in detection.candidates if c.family is family and c.segment_id == sid}


def _issue(ns, restrictions, segment_ids=(), rule=QualityRuleId.DQ19, period_ids=()):
    return QualityIssue(
        issue_id=make_id(IdPrefix.QUALITY_ISSUE, ns, [rule.value, list(segment_ids), list(period_ids)]),
        rule_id=rule, rule_version=1, severity=Severity.WARNING, segment_ids=list(segment_ids),
        period_ids=list(period_ids), measured_fact_ids=[], threshold_fact_ids=[], row_count=0,
        request_exposure_fact_id=None, revenue_exposure_fact_id=None,
        inference_restrictions=list(restrictions), message_template_id=f"{rule.value}_test",
    )


# --- Scale winners -------------------------------------------------------------


def test_scale_winner_fires_on_stable_high_yield_small_slice() -> None:
    # 21M requests, $210 revenue, 4.0 profit per million (account ~0.3), <2% of requests.
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "30.00", margin="0.40"))
    assert _found(store, det, F.SCALE_WINNER) == {E.ELIGIBLE}


def test_scale_winner_near_miss_below_yield_multiple() -> None:
    # 0.55 profit per million is under twice the account's ~0.30.
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "5.50", margin="0.30"))
    assert _found(store, det, F.SCALE_WINNER) == set()


def test_scale_winner_with_concentrated_money_is_only_exploratory() -> None:
    spike = {DAYS[2]: "200.00", DAYS[3]: "200.00"}
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "2.00", margin="0.40", revenue_by_day=spike))
    assert _found(store, det, F.SCALE_WINNER) == {E.EXPLORATORY}
    candidate = next(c for c in det.candidates if c.family is F.SCALE_WINNER
                     and c.segment_id == segment_id_for(store.run_namespace, dict(zip(FIVE, T))))
    counter = [det.facts[i].metric.value for i in candidate.counter_evidence_fact_ids]
    assert "top_two_day_revenue_share" in counter


def test_scale_winner_with_too_few_days_is_only_exploratory() -> None:
    store, _, det = _detect(background() + slice_rows(T, 7_000_000, "70.00", margin="0.40", days=DAYS[:3]))
    assert _found(store, det, F.SCALE_WINNER) == {E.EXPLORATORY}


# --- Wasted requests -----------------------------------------------------------


def test_wasted_requests_fires_on_large_unmonetized_slice() -> None:
    # 140M requests at 0.04 revenue per million.
    store, _, det = _detect(background() + slice_rows(T, 20_000_000, "0.80"))
    assert _found(store, det, F.WASTED_REQUESTS) == {E.ELIGIBLE}


def test_wasted_requests_keeps_zero_revenue_slices() -> None:
    store, _, det = _detect(background() + slice_rows(T, 20_000_000, "0"))
    assert _found(store, det, F.WASTED_REQUESTS) == {E.ELIGIBLE}


def test_wasted_requests_near_miss_above_yield_floor() -> None:
    store, _, det = _detect(background() + slice_rows(T, 20_000_000, "1.20"))  # 0.06 per million
    assert _found(store, det, F.WASTED_REQUESTS) == set()


# --- Margin leaks --------------------------------------------------------------


@pytest.mark.parametrize("margin", ["0.10", "-0.10"])
def test_margin_leak_fires_on_low_or_negative_margin(margin: str) -> None:
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "30.00", margin=margin))
    assert _found(store, det, F.MARGIN_LEAK) == {E.ELIGIBLE}


def test_margin_leak_near_miss_at_ordinary_margin() -> None:
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "30.00", margin="0.30"))
    assert _found(store, det, F.MARGIN_LEAK) == set()


def test_margin_leak_fires_on_parent_child_disparity() -> None:
    sibling = ("777", "DE", "728x90", "Video", "Mobile")
    rows = background() + slice_rows(T, 3_000_000, "30.00", margin="0.30")
    rows += slice_rows(sibling, 3_000_000, "30.00", margin="0.60")
    store, _, det = _detect(rows)
    assert _found(store, det, F.MARGIN_LEAK) == {E.ELIGIBLE}
    candidate = next(c for c in det.candidates if c.family is F.MARGIN_LEAK
                     and c.segment_id == segment_id_for(store.run_namespace, dict(zip(FIVE, T))))
    assert candidate.peer_fact_ids  # residual parent margin and its comparison


# --- Floor investigations ------------------------------------------------------


def test_high_bid_floor_fires() -> None:
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "30.00", floor="0.50", bid="1.20"))
    assert _found(store, det, F.FLOOR_HIGH_BID) == {E.ELIGIBLE}
    candidate = next(c for c in det.candidates if c.family is F.FLOOR_HIGH_BID
                     and c.segment_id == segment_id_for(store.run_namespace, dict(zip(FIVE, T))))
    ratio = next(det.facts[i] for i in candidate.trigger_fact_ids if det.facts[i].kind is FactKind.COMPARISON)
    assert ratio.value == Decimal("2.4")
    assert ratio.segment.cohort_id == "positive_response_positive_floor"


def test_high_bid_floor_near_miss() -> None:
    store, _, det = _detect(background() + slice_rows(T, 3_000_000, "30.00", floor="0.50", bid="0.90"))
    assert _found(store, det, F.FLOOR_HIGH_BID) == set()


PEER = ("778", "DE", "300x600", "Banner", "Tablet")


def test_low_bid_floor_fires_against_matched_peer() -> None:
    rows = background() + slice_rows(PEER, 3_000_000, "30.00", response_rate="0.30", floor="0.50", bid="0.60")
    rows += slice_rows(T, 3_000_000, "30.00", response_rate="0.05", floor="0.50", bid="0.40")
    store, _, det = _detect(rows)
    assert _found(store, det, F.FLOOR_LOW_BID) == {E.ELIGIBLE}
    candidate = next(c for c in det.candidates if c.family is F.FLOOR_LOW_BID)
    peer_keys = {d.value for i in candidate.peer_fact_ids if det.facts[i].comparison
                 for d in det.facts[i].comparison.matching_keys}
    assert peer_keys == {"country", "size", "placement_type", "device_type"}


def test_low_bid_floor_near_miss_when_response_behaviour_is_comparable() -> None:
    rows = background() + slice_rows(PEER, 3_000_000, "30.00", response_rate="0.30", floor="0.50", bid="0.60")
    rows += slice_rows(T, 3_000_000, "30.00", response_rate="0.20", floor="0.50", bid="0.40")
    store, _, det = _detect(rows)
    assert _found(store, det, F.FLOOR_LOW_BID) == set()


def test_low_bid_floor_needs_a_matched_peer() -> None:
    rows = background() + slice_rows(T, 3_000_000, "30.00", response_rate="0.05", floor="0.50", bid="0.40")
    store, _, det = _detect(rows)
    assert _found(store, det, F.FLOOR_LOW_BID) == set()


# --- Wins / impressions --------------------------------------------------------


def test_wins_impressions_discrepancy_fires() -> None:
    rows = background() + slice_rows(
        T, 10_000_000, "30.00", response_rate="0.50", win_rate="0.50", impressions_per_win="0.05"
    )
    store, _, det = _detect(rows)
    assert _found(store, det, F.WINS_IMPRESSIONS_DISCREPANCY) == {E.ELIGIBLE}


def test_wins_impressions_near_miss() -> None:
    rows = background() + slice_rows(
        T, 10_000_000, "30.00", response_rate="0.50", win_rate="0.50", impressions_per_win="0.12"
    )
    store, _, det = _detect(rows)
    assert _found(store, det, F.WINS_IMPRESSIONS_DISCREPANCY) == set()


def test_wins_discrepancy_on_tainted_funnel_is_exploratory() -> None:
    rows = background() + slice_rows(
        T, 10_000_000, "30.00", response_rate="0.50", win_rate="0.50", impressions_per_win="0.05"
    )
    bad = dict(rows[-1])
    bad["Demand Responses"] = bad["Demand Requests"] + 1
    rows[-1] = bad
    store, _, det = _detect(rows)
    assert _found(store, det, F.WINS_IMPRESSIONS_DISCREPANCY) == {E.EXPLORATORY}
    candidate = next(c for c in det.candidates if c.family is F.WINS_IMPRESSIONS_DISCREPANCY
                     and c.segment_id == segment_id_for(store.run_namespace, dict(zip(FIVE, T))))
    assert det.facts[candidate.trigger_fact_ids[0]].validity.status is ValidityStatus.TAINTED


# --- Trends --------------------------------------------------------------------


def test_trend_fires_on_adjacent_day_yield_step() -> None:
    rows = background() + slice_rows(T, 10_000_000, "30.00", revenue_by_day={DAYS[3]: "70.00"})
    store, _, det = _detect(rows)
    assert _found(store, det, F.TREND) == {E.ELIGIBLE}


def test_trend_near_miss_below_step_multiple() -> None:
    rows = background() + slice_rows(T, 10_000_000, "30.00", revenue_by_day={DAYS[3]: "55.00"})
    store, _, det = _detect(rows)
    assert _found(store, det, F.TREND) == set()


def test_trend_on_possibly_incomplete_day_is_exploratory() -> None:
    rows = background() + slice_rows(T, 10_000_000, "30.00", revenue_by_day={DAYS[-1]: "5.00"})
    data = loaded_from_rows(rows)
    ns = quality_for(data).run_namespace
    issue = _issue(ns, [], rule=QualityRuleId.DQ06, period_ids=[period_id_for(ns, [DAYS[-1]])])
    store, _, det = _detect(rows, [issue])
    assert _found(store, det, F.TREND) == {E.EXPLORATORY}


# --- Combination outliers ------------------------------------------------------


def test_combination_outlier_fires_against_residual_parent() -> None:
    other = ("777", "DE", "728x90", "Video", "Mobile")
    rows = background() + slice_rows(T, 3_000_000, "60.00", margin="0.50")
    rows += slice_rows(other, 3_000_000, "30.00", margin="0.30")
    store, _, det = _detect(rows)
    assert _found(store, det, F.COMBINATION_OUTLIER) == {E.ELIGIBLE}
    candidate = next(c for c in det.candidates if c.family is F.COMBINATION_OUTLIER
                     and c.segment_id == segment_id_for(store.run_namespace, dict(zip(FIVE, T))))
    residual = [det.facts[i] for i in candidate.trigger_fact_ids if det.facts[i].segment.cohort_id.startswith("excluding_")]
    assert residual and residual[0].segment.filters == {k: T[i] for i, k in enumerate(FIVE[:2])}


def test_combination_outlier_near_miss() -> None:
    other = ("777", "DE", "728x90", "Video", "Mobile")
    rows = background() + slice_rows(T, 3_000_000, "45.00", margin="0.30")
    rows += slice_rows(other, 3_000_000, "30.00", margin="0.30")
    store, _, det = _detect(rows)
    assert _found(store, det, F.COMBINATION_OUTLIER) == set()


# --- Gates, sensitivity, quality, overlap ----------------------------------------


def test_below_gate_but_material_is_exploratory_not_dropped() -> None:
    # 7M requests: under the 10M gate, above the 1M exploratory floor.
    store, _, det = _detect(background() + slice_rows(T, 1_000_000, "30.00", margin="0.10"))
    assert _found(store, det, F.MARGIN_LEAK) == {E.EXPLORATORY}


def test_threshold_sensitivity_records_shortlist_change() -> None:
    # 14M requests passes the 10M gate but not 2x; $140 revenue also fails 2x.
    store, _, det = _detect(background() + slice_rows(T, 2_000_000, "20.00", margin="0.05"))
    sid = segment_id_for(store.run_namespace, dict(zip(FIVE, T)))
    candidate = next(c for c in det.candidates if c.family is F.MARGIN_LEAK and c.segment_id == sid)
    assert candidate.eligibility is E.ELIGIBLE
    assert det.sensitivity.shortlist_changed["x2"]
    assert (F.MARGIN_LEAK, sid) not in det.sensitivity.eligible_by_role["x2"]
    assert (F.MARGIN_LEAK, sid) in det.sensitivity.eligible_by_role["x0.5"]
    assert candidate.candidate_id in det.sensitivity.sensitive_candidate_ids
    assert threshold_sensitive(store, candidate)


def test_detector_thresholds_come_from_config() -> None:
    rows = background() + slice_rows(T, 3_000_000, "30.00", margin="0.30")
    store, _, det = _detect(rows, config=DetectorConfig(margin_floor=Decimal("0.35")))
    assert _found(store, det, F.MARGIN_LEAK) == {E.ELIGIBLE}


def test_quality_restriction_downgrades_and_is_attached() -> None:
    rows = background() + slice_rows(T, 3_000_000, "30.00", margin="0.40")
    data = loaded_from_rows(rows)
    ns = quality_for(data).run_namespace
    issue = _issue(ns, [InferenceRestriction.NO_STABLE_SCALE_CLAIM])
    store, _, det = _detect(rows, [issue])
    assert _found(store, det, F.SCALE_WINNER) == {E.EXPLORATORY}
    assert all(issue.issue_id in c.quality_issue_ids for c in det.candidates)


def test_country_restriction_only_hits_matching_country() -> None:
    rows = background() + slice_rows(T, 3_000_000, "30.00", margin="0.40")
    data = loaded_from_rows(rows)
    ns = quality_for(data).run_namespace
    us = segment_id_for(ns, {FIVE[1]: "US"})
    store, _, det = _detect(rows, [_issue(ns, [InferenceRestriction.NO_COUNTRY_OPPORTUNITY], [us])])
    assert _found(store, det, F.SCALE_WINNER) == {E.ELIGIBLE}  # DE slice untouched


def test_parent_duplicates_are_rejected_with_reason_and_overlaps_are_exact() -> None:
    store, quality, det = _detect(background() + slice_rows(T, 3_000_000, "30.00", margin="0.40"))
    scale = [c for c in det.candidates if c.family is F.SCALE_WINNER]
    rejected = [c for c in scale if c.eligibility is E.REJECTED]
    assert rejected and all(c.rejection_reason and "C_" in c.rejection_reason for c in rejected)
    kept = [c for c in det.candidates if c.eligibility is not E.REJECTED]
    for group in det.overlap_groups.values():
        a, b = (next(c for c in kept if c.candidate_id == i) for i in group.candidate_ids)
        shared = det.facts[group.shared_request_fact_id]
        union = {**store.segments[a.segment_id].filters, **store.segments[b.segment_id].filters}
        expected = combined_stats(store, full_period(store), union)["requests"]
        assert shared.value == expected
        assert group.overlap_group_id in a.overlap_group_ids and group.overlap_group_id in b.overlap_group_ids


def test_evidence_is_closed_complete_and_rebuildable() -> None:
    rows = background() + slice_rows(T, 3_000_000, "30.00", margin="0.40", floor="0.50", bid="1.20")
    store, quality, det = _detect(rows)
    referenced = {
        i for c in det.candidates
        for i in (*c.trigger_fact_ids, *c.daily_fact_ids, *c.peer_fact_ids, *c.counter_evidence_fact_ids)
    }
    assert referenced <= set(det.facts)
    for fact in det.facts.values():
        assert set(fact.provenance.parent_fact_ids) <= set(det.facts)
    rebuilt = candidate_evidence(store, quality, list(det.candidates))
    assert referenced <= set(rebuilt.facts)
    assert all(rebuilt.facts[i] == det.facts[i] for i in referenced)
    assert set(rebuilt.overlap_groups) == set(det.overlap_groups)
    for c in det.candidates:
        assert c.daily_fact_ids, c.family
    assert detect_opportunities(store, quality, DetectorConfig()) == list(det.candidates)


def test_detection_flags_mark_unmatched_contrasts_and_sensitivity() -> None:
    from pipeline.detectors import with_detection_flags
    from pipeline.quality import PEER_KEYS_DIFFER_FLAG, RANK_SENSITIVE_FLAG

    other = ("777", "DE", "728x90", "Video", "Mobile")
    rows = background() + slice_rows(T, 3_000_000, "60.00", margin="0.50")
    rows += slice_rows(other, 3_000_000, "30.00", margin="0.30")
    store, _, det = _detect(rows)
    flagged = with_detection_flags(store, det)
    sid = segment_id_for(store.run_namespace, dict(zip(FIVE, T)))
    combo = next(c for c in det.candidates if c.family is F.COMBINATION_OUTLIER and c.segment_id == sid)
    # The residual parent fixes supply and country only: size, placement and device differ.
    assert PEER_KEYS_DIFFER_FLAG in flagged.quality_flags[combo.candidate_id]
    for cid in det.sensitivity.sensitive_candidate_ids:
        assert RANK_SENSITIVE_FLAG in flagged.quality_flags[cid]
    assert store.quality_flags == {} or store.quality_flags is not flagged.quality_flags


def test_single_day_restriction_does_not_block_whole_period_scale_claim() -> None:
    rows = background() + slice_rows(T, 3_000_000, "30.00", margin="0.40")
    data = loaded_from_rows(rows)
    ns = quality_for(data).run_namespace
    day = _issue(ns, [InferenceRestriction.NO_STABLE_SCALE_CLAIM], rule=QualityRuleId.DQ06,
                 period_ids=[period_id_for(ns, [DAYS[-1]])])
    store, _, det = _detect(rows, [day])
    # Stable on the full period and on the six days without the flagged one.
    assert _found(store, det, F.SCALE_WINNER) == {E.ELIGIBLE}
    candidate = next(c for c in det.candidates if c.family is F.SCALE_WINNER
                     and c.segment_id == segment_id_for(store.run_namespace, dict(zip(FIVE, T))))
    assert day.issue_id in candidate.quality_issue_ids
