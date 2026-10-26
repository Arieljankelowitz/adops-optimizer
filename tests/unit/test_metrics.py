"""Aggregation: additive totals, ratios of sums, null denominators, gates and stability."""

from decimal import Decimal

import pytest

from contracts.config import AnalyticsConfig
from contracts.enums import (
    CoverageStatus,
    Dimension,
    FactKind,
    MetricKey as M,
    QualityRuleId,
    Severity,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, make_id
from contracts.models import QualityIssue
from pipeline.metrics import (
    ADDITIVE,
    FIVE,
    FULL_INPUT,
    LEVELS,
    VALID_COUNTERS,
    FactBuilder,
    aggregate,
    full_period,
    gate_column,
    level_name,
    make_segment,
    period_id_for,
    segment_id_for,
    stats_row,
)
from tests.support.analytics_data import DAYS, loaded_from_rows, quality_for, row, slice_rows

A = ("1", "US", "300x250", "Banner", "Desktop")
B = ("2", "DE", "728x90", "Video", "Mobile")


def _store(rows, issues=(), config=AnalyticsConfig()):
    data = loaded_from_rows(rows)
    quality = quality_for(data, issues)
    return aggregate(data, quality, config), quality


def _facts(store, quality):
    return FactBuilder(store, quality)


def _seg(store, filters, cohort=FULL_INPUT):
    return store.segments[segment_id_for(store.run_namespace, filters, cohort)]


def test_rates_are_ratios_of_sums_not_row_averages() -> None:
    day = DAYS[0]
    rows = [
        row(day, A, 1_000_000, 0, 0, 0, "10.00", "5.00"),  # 10 per million
        row(day, B, 9_000_000, 0, 0, 0, "9.00", "3.00"),  # 1 per million
    ]
    store, quality = _store(rows)
    b = _facts(store, quality)
    account = _seg(store, {})
    period = full_period(store)
    fact = b.rate(account, period, stats_row(store, account.segment_id, period.period_id), M.REVENUE_PER_MILLION_REQUESTS)
    # (10 + 9) / 10M * 1M = 1.9, not the row average 5.5.
    assert fact.value == Decimal("1.9")
    assert fact.unit is Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS
    assert fact.provenance.weighting is Weighting.RATIO_OF_SUMS
    assert len(fact.provenance.parent_fact_ids) == 2
    margin = b.rate(account, period, stats_row(store, account.segment_id, period.period_id), M.MARGIN)
    assert margin.value == Decimal("11") / Decimal("19")


def test_srpm_scale_is_per_million_requests_and_ecpm_per_thousand() -> None:
    rows = [row(DAYS[0], A, 4_000_000, 2_000_000, 1_000_000, 500_000, "10.00", "6.00")]
    store, quality = _store(rows)
    b = _facts(store, quality)
    seg, period = _seg(store, dict(zip(FIVE, A))), full_period(store)
    stats = stats_row(store, seg.segment_id, period.period_id)
    assert b.rate(seg, period, stats, M.REVENUE_PER_MILLION_REQUESTS).value == Decimal("2.5")
    assert b.rate(seg, period, stats, M.PROFIT_PER_MILLION_REQUESTS).value == Decimal("1")
    assert b.rate(seg, period, stats, M.ECPM).value == Decimal("0.02")
    assert b.rate(seg, period, stats, M.RESPONSE_RATE).value == Decimal("0.5")
    assert b.rate(seg, period, stats, M.WIN_RATE).value == Decimal("0.5")
    assert b.rate(seg, period, stats, M.IMPRESSION_TO_WIN_RATE).value == Decimal("0.5")


@pytest.mark.parametrize(
    "metric",
    [M.RESPONSE_RATE, M.REVENUE_PER_MILLION_REQUESTS, M.PROFIT_PER_MILLION_REQUESTS, M.WIN_RATE,
     M.IMPRESSION_TO_WIN_RATE, M.ECPM, M.MARGIN, M.REQUEST_WEIGHTED_FLOOR, M.RESPONSE_WEIGHTED_BID],
)
def test_zero_denominators_are_null_with_reason(metric: M) -> None:
    rows = [row(DAYS[0], A, 0, 0, 0, 0, "0", "0")]
    store, quality = _store(rows)
    b = _facts(store, quality)
    seg, period = _seg(store, dict(zip(FIVE, A))), full_period(store)
    fact = b.rate(seg, period, stats_row(store, seg.segment_id, period.period_id), metric)
    assert fact.value is None
    assert fact.validity.status is ValidityStatus.UNDEFINED
    assert "zero" in fact.validity.reason


def test_negative_revenue_margin_is_undefined_and_shares_need_positive_totals() -> None:
    rows = [
        row(DAYS[0], A, 1_000_000, 0, 0, 0, "-5.00", "1.00"),
        row(DAYS[0], B, 1_000_000, 0, 0, 0, "2.00", "1.00"),
    ]
    store, quality = _store(rows)
    b = _facts(store, quality)
    seg, period = _seg(store, dict(zip(FIVE, A))), full_period(store)
    stats = stats_row(store, seg.segment_id, period.period_id)
    assert b.rate(seg, period, stats, M.MARGIN).validity.status is ValidityStatus.UNDEFINED
    account = _seg(store, {})
    acc = (account, stats_row(store, account.segment_id, period.period_id))
    # Account revenue is -3 and account profit is -5: shares are undefined, not negative.
    assert b.rate(seg, period, stats, M.REVENUE_SHARE, account=acc).value is None
    assert b.rate(seg, period, stats, M.PROFIT_SHARE, account=acc).value is None
    # Rate metrics over rows with signed adjustments are tainted, not dropped.
    rpm = b.rate(seg, period, stats, M.REVENUE_PER_MILLION_REQUESTS)
    assert rpm.value == Decimal("-5")
    assert rpm.validity.status is ValidityStatus.TAINTED


def test_invalid_funnel_rows_taint_rates_and_get_a_valid_cohort() -> None:
    rows = [
        row(DAYS[0], A, 1_000, 2_000, 10, 10, "1.00", "0.50"),  # responses > requests
        row(DAYS[0], B, 1_000_000, 100_000, 10_000, 9_000, "3.00", "1.00"),
    ]
    store, quality = _store(rows)
    b = _facts(store, quality)
    period = full_period(store)
    account = _seg(store, {})
    stats = stats_row(store, account.segment_id, period.period_id)
    fact = b.rate(account, period, stats, M.RESPONSE_RATE)
    assert fact.validity.status is ValidityStatus.TAINTED
    # Money totals stay intact on the full input.
    assert b.additive(account, period, stats, M.REVENUE).value == Decimal("4.00")
    valid = _seg(store, {}, VALID_COUNTERS)
    valid_stats = stats_row(store, valid.segment_id, period.period_id)
    assert b.rate(valid, period, valid_stats, M.RESPONSE_RATE).validity.status is ValidityStatus.VALID
    assert valid_stats["requests"] == 1_000_000


def test_additive_totals_reconcile_across_every_level_and_day() -> None:
    rows = slice_rows(A, 1_000_000, "3.21") + slice_rows(B, 2_000_000, "4.56", days=DAYS[:4])
    rows += slice_rows(("2", "DE", "728x90", "Banner", "Mobile"), 3_000_000, "1.07", days=DAYS[2:])
    store, _ = _store(rows)
    period = full_period(store)
    agg = store.aggregates[
        (store.aggregates["period_id"] == period.period_id) & (store.aggregates["cohort_id"] == FULL_INPUT)
    ]
    account = agg[agg["level"] == "account"].iloc[0]
    for dims in LEVELS:
        level = agg[agg["level"] == level_name(dims)]
        assert not level.empty
        for metric in ADDITIVE:
            assert sum(level[metric.value].tolist()) == account[metric.value], (dims, metric)
    daily = store.daily[(store.daily["level"] == "account") & (store.daily["cohort_id"] == FULL_INPUT)]
    for metric in ADDITIVE:
        assert sum(daily[metric.value].tolist()) == account[metric.value]
    assert account["revenue"] == sum(Decimal(str(r["Revenue"])) for r in rows)


def test_only_observed_groups_and_placement_is_preserved() -> None:
    video = ("1", "US", "300x250", "Video", "Desktop")
    store, _ = _store(slice_rows(A, 1_000_000, "1.00") + slice_rows(video, 1_000_000, "1.00"))
    five = store.aggregates[store.aggregates["level"] == level_name(FIVE)]
    assert sorted(five["placement_type"].unique()) == ["Banner", "Video"]
    pairs = store.aggregates[store.aggregates["level"] == "supply_id+country"]
    assert len(pairs) == 1  # no Cartesian product of unseen combinations


def test_unobserved_days_are_absent_not_zero() -> None:
    store, _ = _store(slice_rows(A, 1_000_000, "1.00") + slice_rows(B, 1_000_000, "1.00", days=DAYS[:3]))
    seg = _seg(store, dict(zip(FIVE, B)))
    daily = store.daily[store.daily["segment_id"] == seg.segment_id]
    assert len(daily) == 3
    row_ = stats_row(store, seg.segment_id, full_period(store).period_id)
    assert row_["observed_days"] == 3
    assert not row_["stable"] and "few_observed_days" in row_["stability_reason"]


def test_concentrated_revenue_fails_stability() -> None:
    spiky = {DAYS[2]: "900.00", DAYS[3]: "900.00"}
    store, quality = _store(slice_rows(A, 1_000_000, "10.00", revenue_by_day=spiky))
    seg = _seg(store, dict(zip(FIVE, A)))
    period = full_period(store)
    stats = stats_row(store, seg.segment_id, period.period_id)
    assert "revenue_concentrated" in stats["stability_reason"]
    fact = _facts(store, quality).rate(seg, period, stats, M.TOP_TWO_DAY_REVENUE_SHARE)
    assert fact.value == Decimal("1800") / Decimal("1850")
    assert len(fact.provenance.parent_fact_ids) == 7  # the daily revenue facts


def test_gates_follow_config_and_sensitivity_multipliers() -> None:
    store, _ = _store(slice_rows(A, 2_000_000, "20.00"))  # 14M requests, $140 revenue
    stats = stats_row(store, _seg(store, {}).segment_id, full_period(store).period_id)
    assert stats[gate_column("volume", "base")]
    assert stats[gate_column("volume", "x0.5")]
    assert not stats[gate_column("volume", "x2")]
    assert stats[gate_column("volume", "exploratory")]
    assert not stats[gate_column("waste", "base")]
    tight, _ = _store(slice_rows(A, 2_000_000, "20.00"), config=AnalyticsConfig(min_requests=20_000_000))
    t = stats_row(tight, _seg(tight, {}).segment_id, full_period(tight).period_id)
    assert not t[gate_column("volume", "base")]


def test_possibly_incomplete_day_gets_a_sensitivity_period() -> None:
    data = loaded_from_rows(slice_rows(A, 1_000_000, "1.00"))
    ns = quality_for(data).run_namespace
    issue = QualityIssue(
        issue_id=make_id(IdPrefix.QUALITY_ISSUE, ns, "dq06"), rule_id=QualityRuleId.DQ06, rule_version=1,
        severity=Severity.WARNING, segment_ids=[], period_ids=[period_id_for(ns, [DAYS[-1]])],
        measured_fact_ids=[], threshold_fact_ids=[], row_count=1, request_exposure_fact_id=None,
        revenue_exposure_fact_id=None, inference_restrictions=[], message_template_id="DQ06_partial_day",
    )
    store = aggregate(data, quality_for(data, [issue]), AnalyticsConfig())
    full = full_period(store)
    assert full.coverage_status is CoverageStatus.POSSIBLY_INCOMPLETE
    multi = [p for p in store.periods.values() if len(p.included_dates) > 1]
    assert sorted(len(p.included_dates) for p in multi) == [6, 7]
    assert store.periods[period_id_for(ns, [DAYS[-1]])].coverage_status is CoverageStatus.POSSIBLY_INCOMPLETE
    # Facts touching that day carry the issue in provenance.
    b = FactBuilder(store, quality_for(data, [issue]))
    seg = _seg(store, {})
    fact = b.additive(seg, full, stats_row(store, seg.segment_id, full.period_id), M.REQUESTS)
    assert fact.provenance.quality_issue_ids == [issue.issue_id]


def test_ids_are_deterministic_and_label_free() -> None:
    rows = slice_rows(A, 1_000_000, "1.00")
    s1, q1 = _store(rows)
    s2, q2 = _store(rows)
    assert list(s1.segments) == list(s2.segments)
    seg = make_segment(s1.run_namespace, {Dimension.COUNTRY: "US"})
    assert "US" not in seg.segment_id
    period = full_period(s1)
    f1 = _facts(s1, q1).rate(seg, period, stats_row(s1, seg.segment_id, period.period_id), M.ECPM)
    f2 = _facts(s2, q2).rate(seg, period, stats_row(s2, seg.segment_id, period.period_id), M.ECPM)
    assert f1 == f2
    assert f1.kind is FactKind.CALCULATED


def test_comparison_and_daily_change_facts() -> None:
    rows = slice_rows(A, 1_000_000, "2.00", revenue_by_day={DAYS[1]: "6.00"}) + slice_rows(B, 1_000_000, "1.00")
    store, quality = _store(rows)
    b = _facts(store, quality)
    period = full_period(store)
    a_seg, acc = _seg(store, dict(zip(FIVE, A))), _seg(store, {})
    left = b.rate(a_seg, period, stats_row(store, a_seg.segment_id, period.period_id), M.REVENUE_PER_MILLION_REQUESTS)
    right = b.rate(acc, period, stats_row(store, acc.segment_id, period.period_id), M.REVENUE_PER_MILLION_REQUESTS)
    cmp = b.comparison(left, right, [])
    assert cmp.kind is FactKind.COMPARISON
    assert cmp.comparison.ratio == left.value / right.value
    assert cmp.comparison.relation.value == "greater"
    days = b.daily_facts(a_seg, period, M.REVENUE_PER_MILLION_REQUESTS)
    change = b.daily_change(days[0], days[1])
    assert change.value == Decimal("2")  # 2 -> 6 per million is +200%
    assert change.period.included_dates == [DAYS[0], DAYS[1]]


def test_issue_index_matches_pairwise_overlap_on_every_scope() -> None:
    """The label index answers exactly what checking every issue slice would."""
    import random

    from contracts.enums import InferenceRestriction
    from pipeline.metrics import IssueIndex, segments_overlap
    from tests.support.analytics_data import load_fixture

    data = load_fixture()
    ns = quality_for(data).run_namespace
    store = aggregate(data, quality_for(data), AnalyticsConfig())
    rng = random.Random(7)
    segment_ids, period_ids = sorted(store.segments), sorted(store.periods)
    restrictions = list(InferenceRestriction)
    issues = []
    for n in range(40):
        scope = rng.sample(segment_ids, rng.choice([0, 1, 3, 12]))
        if n % 9 == 0:
            scope.append("G_" + "f" * 16)  # an unknown slice touches everything
        issues.append(QualityIssue(
            issue_id=make_id(IdPrefix.QUALITY_ISSUE, ns, ["index", n]), rule_id=QualityRuleId.DQ07,
            rule_version=1, severity=Severity.WARNING, segment_ids=scope,
            period_ids=rng.sample(period_ids, rng.choice([0, 1, 2])), measured_fact_ids=[],
            threshold_fact_ids=[], row_count=1, request_exposure_fact_id=None,
            revenue_exposure_fact_id=None,
            inference_restrictions=rng.sample(restrictions, 2), message_template_id="DQ07_coverage",
        ))
    quality = quality_for(data, issues)
    index = IssueIndex(store, quality)

    def touches(issue, segment):
        return not issue.segment_ids or any(
            sid not in store.segments or segments_overlap(store.segments[sid].filters, segment.filters)
            for sid in issue.segment_ids
        )

    def dated(issue, period, covers):
        dates = set(period.included_dates)
        return not issue.period_ids or any(
            pid not in store.periods
            or (dates <= set(store.periods[pid].included_dates) if covers
                else bool(dates & set(store.periods[pid].included_dates)))
            for pid in issue.period_ids
        )

    for sid in rng.sample(segment_ids, 150):
        segment = store.segments[sid]
        for pid in period_ids:
            period = store.periods[pid]
            assert index.issues_for(segment, period) == sorted(
                i.issue_id for i in issues if touches(i, segment) and dated(i, period, False)
            )
            assert index.restrictions_for(segment, period) == {
                r for i in issues if touches(i, segment) and dated(i, period, True)
                for r in i.inference_restrictions
            }
