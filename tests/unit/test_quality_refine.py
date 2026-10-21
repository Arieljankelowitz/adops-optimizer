"""Second-pass rules against hand-built MetricsStore and candidate mocks."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from contracts.config import QualityConfig
from contracts.enums import (
    DetectorFamily,
    Dimension,
    EligibilityStatus,
    InferenceRestriction as IR,
    InputColumn as C,
    MetricKey as M,
    QualityPass,
    QualityRuleId as R,
    RuleOutcome as O,
)
from pipeline.quality import (
    PEER_KEYS_DIFFER_FLAG,
    RANK_SENSITIVE_FLAG,
    assess_quality,
    refine_quality,
    render_message,
)
from tests.quality_support import (
    SEGMENTS,
    START,
    candidate,
    clean_rows,
    issues,
    key_filters,
    load,
    make_row,
    metrics_for,
    outcome,
    scale,
)

TARGET = SEGMENTS[0]
CONFIG = QualityConfig()


def _refine(rows, keys=(TARGET,), flags=None, family=DetectorFamily.SCALE_WINNER,
            eligibility=EligibilityStatus.ELIGIBLE, extra_filters=()):
    data = load(rows)
    initial = assess_quality(data, CONFIG)
    filters = [key_filters(k) for k in keys] + list(extra_filters)
    store, segments, period_id = metrics_for(data, initial, filters, flags)
    by_filters = {tuple(sorted(s.filters.items())): s for s in segments.values()}
    candidates = [
        candidate(initial.run_namespace, by_filters[tuple(sorted(key_filters(k).items()))],
                  period_id, f"cand {i}", family, eligibility)
        for i, k in enumerate(keys)
    ]
    return initial, refine_quality(data, store, candidates, initial, CONFIG), candidates


def _facts(report):
    return {f.fact_id: f for f in report.facts}


def _target(row) -> bool:
    return tuple(row[c] for c in (C.SUPPLY_ID.value, C.COUNTRY.value, C.SIZE.value,
                                  C.PLACEMENT_TYPE.value, C.DEVICE_TYPE.value)) == TARGET


def test_stable_material_candidate_passes_every_refined_rule() -> None:
    initial, refined, _ = _refine(clean_rows())
    assert refined.quality_pass is QualityPass.REFINED
    for rule in (R.DQ07, R.DQ08, R.DQ09, R.DQ19, R.DQ20):
        assert outcome(refined, rule) is O.NOT_TRIGGERED, rule
    assert refined.issues == initial.issues


def test_refine_never_removes_first_pass_issues_or_facts() -> None:
    rows = clean_rows()
    rows[0][C.PROFIT.value] = str(Decimal(rows[0][C.PROFIT.value]) + Decimal("0.5"))
    initial, refined, _ = _refine(rows)
    assert refined.issues[: len(initial.issues)] == initial.issues
    assert {f.fact_id for f in initial.facts} <= {f.fact_id for f in refined.facts}
    assert outcome(refined, R.DQ12) is O.TRIGGERED


def test_no_candidates_leaves_candidate_rules_untriggered() -> None:
    data = load(clean_rows())
    initial = assess_quality(data, CONFIG)
    store, _, _ = metrics_for(data, initial, [])
    refined = refine_quality(data, store, [], initial, CONFIG)
    assert outcome(refined, R.DQ07) is O.NOT_TRIGGERED
    assert outcome(refined, R.DQ19) is O.NOT_TRIGGERED


def test_dq07_intermittent_slice_distinguishes_absent_days() -> None:
    rows = [r for r in clean_rows()
            if not (_target(r) and r[C.DAY.value] in {(START + timedelta(days=d)).isoformat()
                                                       for d in (1, 2, 4, 5)})]
    _, refined, (cand,) = _refine(rows)
    (issue,) = issues(refined, R.DQ07)
    facts = _facts(refined)
    observed, reference, positive, absent = (facts[f] for f in issue.measured_fact_ids)
    assert (observed.value, reference.value, positive.value, absent.value) == (3, 7, 3, 4)
    assert issue.affected_candidate_ids == [cand.candidate_id]
    assert IR.NO_STABLE_SCALE_CLAIM in issue.inference_restrictions
    assert "3/7 days observed" in render_message(issue, facts)


def test_dq07_observed_zero_revenue_days_are_not_revenue_positive() -> None:
    rows = clean_rows()
    for row in rows:
        if _target(row) and row[C.DAY.value] != START.isoformat():
            row[C.REVENUE.value] = row[C.COST.value] = row[C.PROFIT.value] = "0"
    _, refined, _ = _refine(rows)
    (issue,) = issues(refined, R.DQ07)
    facts = _facts(refined)
    assert facts[issue.measured_fact_ids[0]].value == 7
    assert facts[issue.measured_fact_ids[2]].value == 1


def test_dq08_candidate_concentration_and_dq19_dependence() -> None:
    rows = clean_rows()
    scale(rows, lambda r: _target(r) and r[C.DAY.value] == (START + timedelta(days=2)).isoformat(),
          C.DEMAND_ECPM, "30")
    _, refined, (cand,) = _refine(rows)
    revenue = issues(refined, R.DQ08, "DQ08_concentrated_revenue")
    assert len(revenue) == 1 and revenue[0].affected_candidate_ids == [cand.candidate_id]
    assert _facts(refined)[revenue[0].measured_fact_ids[0]].value >= Decimal("0.6")
    templates = {i.message_template_id for i in issues(refined, R.DQ19)}
    assert "DQ19_flagged_day_dependent" in templates


def test_dq08_parent_segment_is_checked_for_candidate() -> None:
    rows = clean_rows()
    supply = TARGET[0]
    scale(rows, lambda r: r[C.SUPPLY_ID.value] == supply
          and r[C.DAY.value] == (START + timedelta(days=2)).isoformat(), C.DEMAND_ECPM, "30")
    parent = {Dimension.SUPPLY_ID: supply}
    initial, refined, (cand,) = _refine(rows, extra_filters=[parent])
    added = [i for i in issues(refined, R.DQ08) if i not in initial.issues]
    scopes = {tuple(_facts(refined)[i.measured_fact_ids[0]].segment.filters.items())
              for i in added}
    assert ((Dimension.SUPPLY_ID, supply),) in scopes
    assert all(i.affected_candidate_ids == [cand.candidate_id] for i in added)


def test_dq08_short_candidate_is_not_testable() -> None:
    _, refined, _ = _refine(clean_rows(days=3))
    assert outcome(refined, R.DQ08) is O.NOT_TESTABLE


def test_dq09_candidate_yield_step() -> None:
    rows = clean_rows()
    scale(rows, lambda r: _target(r) and r[C.DAY.value] >= (START + timedelta(days=4)).isoformat(),
          C.DEMAND_ECPM, "2.5")
    _, refined, _ = _refine(rows)
    (issue,) = issues(refined, R.DQ09, "DQ09_yield_step")
    fact = _facts(refined)[issue.measured_fact_ids[0]]
    assert fact.metric is M.ADJACENT_DAY_YIELD_RATIO and fact.value >= 2
    assert [d.isoformat() for d in fact.period.included_dates] == [
        (START + timedelta(days=3)).isoformat(), (START + timedelta(days=4)).isoformat()]


def test_dq09_yield_step_needs_eligible_days() -> None:
    rows = clean_rows(base_requests=(200_001, 300_000))
    scale(rows, lambda r: _target(r) and r[C.DAY.value] >= (START + timedelta(days=4)).isoformat(),
          C.DEMAND_ECPM, "2.5")
    _, refined, _ = _refine(rows)
    assert not issues(refined, R.DQ09, "DQ09_yield_step")


def test_dq09_candidate_daily_spike_with_zero_median() -> None:
    rows = [r for r in clean_rows() if not _target(r)]
    for d in range(7):
        requests = 4_000_000 if d == 3 else 0
        rows.append(make_row(START + timedelta(days=d), TARGET, requests, Decimal(2)))
    _, refined, _ = _refine(rows)
    spikes = issues(refined, R.DQ09, "DQ09_request_spike")
    assert len(spikes) == 1
    fact = _facts(refined)[spikes[0].measured_fact_ids[0]]
    assert fact.value is None and fact.validity.reason


def test_dq19_below_materiality() -> None:
    rows = clean_rows(base_requests=(100_001, 200_000))
    _, refined, _ = _refine(rows)
    assert {i.message_template_id for i in issues(refined, R.DQ19)} == {"DQ19_below_materiality"}


def test_dq19_exploratory_eligibility_is_reported() -> None:
    _, refined, _ = _refine(clean_rows(), eligibility=EligibilityStatus.EXPLORATORY)
    assert {i.message_template_id for i in issues(refined, R.DQ19)} == {"DQ19_below_materiality"}


def test_dq19_threshold_sensitivity() -> None:
    rows = clean_rows(base_requests=(1_700_001, 2_000_000))
    _, refined, _ = _refine(rows)
    assert {i.message_template_id for i in issues(refined, R.DQ19)} == {"DQ19_threshold_sensitive"}


def test_dq19_family_specific_gates() -> None:
    _, refined, _ = _refine(clean_rows(), family=DetectorFamily.WASTED_REQUESTS)
    assert "DQ19_below_materiality" in {i.message_template_id for i in issues(refined, R.DQ19)}


def test_dq19_flagged_day_dependence() -> None:
    # Account traffic drops on the last day; the candidate's does not, and only
    # that possibly incomplete day lifts it over the request gate.
    rows = clean_rows(base_requests=(1_550_001, 1_650_000))
    last = (START + timedelta(days=6)).isoformat()
    scale(rows, lambda r: r[C.DAY.value] == last and not _target(r), C.DEMAND_REQUESTS, "0.3")
    _, refined, _ = _refine(rows)
    assert outcome(refined, R.DQ06) is O.TRIGGERED
    templates = {i.message_template_id for i in issues(refined, R.DQ19)}
    assert "DQ19_flagged_day_dependent" in templates
    assert "DQ19_below_materiality" not in templates


def test_dq19_rank_flag_from_metrics() -> None:
    data = load(clean_rows())
    initial = assess_quality(data, CONFIG)
    store, segments, period_id = metrics_for(data, initial, [key_filters(TARGET)])
    (seg,) = segments.values()
    store = replace(store, quality_flags={seg.segment_id: (RANK_SENSITIVE_FLAG,)})
    cand = candidate(initial.run_namespace, seg, period_id, "rank")
    refined = refine_quality(data, store, [cand], initial, CONFIG)
    assert {i.message_template_id for i in issues(refined, R.DQ19)} == {"DQ19_rank_sensitive"}


def test_dq20_unmatched_peer_flag_prohibits_matched_label() -> None:
    data = load(clean_rows())
    initial = assess_quality(data, CONFIG)
    store, segments, period_id = metrics_for(data, initial, [key_filters(TARGET)])
    (seg,) = segments.values()
    cand = candidate(initial.run_namespace, seg, period_id, "peer")
    store = replace(store, quality_flags={cand.candidate_id: (PEER_KEYS_DIFFER_FLAG,)})
    refined = refine_quality(data, store, [cand], initial, CONFIG)
    (issue,) = issues(refined, R.DQ20, "DQ20_unmatched_peer")
    assert IR.NO_MATCHED_PEER_LABEL in issue.inference_restrictions
    assert outcome(refined, R.DQ20) is O.TRIGGERED
    assert issues(refined, R.DQ20, "DQ20_observational_limits")


def test_refine_rejects_foreign_scope() -> None:
    data = load(clean_rows())
    initial = assess_quality(data, CONFIG)
    store, segments, period_id = metrics_for(data, initial, [key_filters(TARGET)])
    (seg,) = segments.values()
    cand = candidate(initial.run_namespace, seg, period_id, "x")
    with pytest.raises(ValueError, match="another run namespace"):
        refine_quality(data, replace(store, run_namespace="N_0000000000000000"), [cand],
                       initial, CONFIG)
    with pytest.raises(ValueError, match="unknown scope"):
        refine_quality(data, replace(store, segments={}), [cand], initial, CONFIG)
    with pytest.raises(ValueError, match="configuration"):
        refine_quality(data, store, [cand], initial, QualityConfig(min_stable_days=3))


def test_quality_materiality_defaults_match_analytics_defaults() -> None:
    from contracts.config import AnalyticsConfig

    analytics = AnalyticsConfig()
    assert CONFIG.materiality_min_requests == analytics.min_requests
    assert CONFIG.materiality_min_revenue == analytics.min_revenue
    assert CONFIG.materiality_waste_min_requests == analytics.waste_min_requests
    assert CONFIG.materiality_wins_min_wins == analytics.wins_min_wins
    assert CONFIG.sensitivity_multipliers == analytics.sensitivity_multipliers
