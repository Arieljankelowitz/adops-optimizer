"""First-pass quality rules: each rule's pass, trigger and not-testable side."""

import io
from datetime import timedelta
from decimal import Decimal

import pytest

from contracts.config import InputConfig, QualityConfig
from contracts.enums import (
    CoverageStatus,
    InferenceRestriction as IR,
    InputColumn as C,
    MetricKey as M,
    QualityRuleId as R,
    RuleOutcome as O,
    Severity,
)
from pipeline.load import load_csv
from pipeline.quality import assess_quality, possibly_incomplete_days, render_message
from tests.quality_support import (
    HEADER,
    START,
    assess,
    clean_rows,
    issues,
    load,
    make_row,
    outcome,
    scale,
    to_csv,
)

ALL = [r for r in R]
STATIC_NOTICES = {"DQ13_srpm_per_million_confirmed", "DQ15_weighting_notice",
                  "DQ17_causal_limit", "DQ20_observational_limits"}


def _facts(report):
    return {f.fact_id: f for f in report.facts}


def _measured(report, issue, index=0):
    return _facts(report)[issue.measured_fact_ids[index]]


def _day(i: int) -> str:
    return (START + timedelta(days=i)).isoformat()


@pytest.fixture(scope="module")
def clean():
    return assess(clean_rows())


def test_clean_export_triggers_nothing_but_static_notices(clean) -> None:
    for rule in ALL:
        expected = O.NOT_TESTABLE if rule in (R.DQ07, R.DQ19) else O.NOT_TRIGGERED
        assert outcome(clean, rule) is expected, rule
    assert {i.message_template_id for i in clean.issues} == STATIC_NOTICES
    assert not clean.has_fatal


def test_report_is_deterministic_and_self_contained(clean) -> None:
    again = assess(clean_rows())
    assert again == clean
    facts = _facts(clean)
    for issue in clean.issues:
        for ref in [*issue.measured_fact_ids, *issue.threshold_fact_ids,
                    issue.request_exposure_fact_id, issue.revenue_exposure_fact_id]:
            assert ref is None or ref in facts
        assert "{" not in render_message(issue, facts)
    assert all(f.run_namespace == clean.run_namespace for f in clean.facts)


def test_runner_namespace_is_respected() -> None:
    from dataclasses import replace

    data = replace(load(clean_rows(days=3)), run_namespace="N_0123456789abcdef")
    report = assess_quality(data, QualityConfig())
    assert report.run_namespace == "N_0123456789abcdef"


# --- DQ01 ------------------------------------------------------------------


def test_dq01_unparseable_numeric_is_fatal_and_names_fields() -> None:
    rows = clean_rows()
    rows[5][C.COST.value] = "n/a"
    rows[9][C.DEMAND_ECPM.value] = "inf"
    report = assess(rows)
    (issue,) = issues(report, R.DQ01, "DQ01_parse_failure")
    assert issue.severity is Severity.FATAL and issue.row_count == 2
    assert IR.NO_RECOMMENDATIONS in issue.inference_restrictions
    message = render_message(issue, _facts(report))
    assert "Cost" in message and "Demand eCPM" in message and "2 affected rows" in message
    assert report.has_fatal


@pytest.mark.parametrize("raw,template", [
    (b"", "DQ01_unreadable_file"),
    ((",".join(HEADER[:-1]) + "\n").encode(), "DQ01_schema_failure"),
])
def test_dq01_file_failures_stop_analysis(raw: bytes, template: str) -> None:
    report = assess_quality(load_csv(io.BytesIO(raw), InputConfig()), QualityConfig())
    (issue,) = issues(report, R.DQ01)
    assert issue.message_template_id == template and issue.severity is Severity.FATAL
    for rule in ALL:
        if rule not in (R.DQ01, R.DQ20):
            assert outcome(report, rule) is O.NOT_TESTABLE, rule


def test_dq01_bounds_get_specific_messages() -> None:
    raw = to_csv(clean_rows(days=1))
    report = assess_quality(load_csv(io.BytesIO(raw), InputConfig(max_rows=3)), QualityConfig())
    assert [i.message_template_id for i in issues(report, R.DQ01)] == ["DQ01_row_limit"]
    report = assess_quality(load_csv(io.BytesIO(raw), InputConfig(max_bytes=10)), QualityConfig())
    assert [i.message_template_id for i in issues(report, R.DQ01)] == ["DQ01_byte_limit"]


def test_dq01_extra_columns_are_a_notice() -> None:
    rows = clean_rows(days=3)
    for row in rows:
        row["Extra"] = "1"
    report = assess_quality(load_csv(io.BytesIO(to_csv(rows, [*HEADER, "Extra"])),
                                     InputConfig()), QualityConfig())
    (issue,) = issues(report, R.DQ01)
    assert issue.severity is Severity.NOTICE
    assert outcome(report, R.DQ01) is O.NOT_TRIGGERED


# --- DQ02 ------------------------------------------------------------------


def test_dq02_blank_dimension_is_unknown_bucket_with_exposure() -> None:
    rows = clean_rows()
    for row in rows[:7]:
        row[C.COUNTRY.value] = ""
    report = assess(rows)
    (issue,) = issues(report, R.DQ02)
    assert issue.severity is Severity.WARNING and issue.row_count == 7
    assert IR.NO_COUNTRY_OPPORTUNITY in issue.inference_restrictions
    share = _facts(report)[issue.request_exposure_fact_id]
    assert share.metric is M.REQUEST_SHARE and Decimal("0.04") < share.value < Decimal("0.09")
    assert "not a targetable market" in render_message(issue, _facts(report))


@pytest.mark.parametrize("variant", [" US", "us"])
def test_dq02_normalization_collision_is_fatal(variant: str) -> None:
    rows = clean_rows()
    rows[0][C.COUNTRY.value] = variant
    report = assess(rows)
    (issue,) = issues(report, R.DQ02, "DQ02_normalization_collision")
    assert issue.severity is Severity.FATAL


def test_dq02_consistent_whitespace_is_not_a_collision() -> None:
    rows = clean_rows()
    for row in rows:
        row[C.SUPPLY_ID.value] = f"{row[C.SUPPLY_ID.value]} "
    assert outcome(assess(rows), R.DQ02) is O.NOT_TRIGGERED


def test_dq02_missing_date_is_fatal() -> None:
    rows = clean_rows()
    rows[4][C.DAY.value] = ""
    (issue,) = issues(assess(rows), R.DQ02, "DQ02_missing_date")
    assert issue.severity is Severity.FATAL and issue.row_count == 1


def test_dq02_not_testable_on_unreadable_file() -> None:
    report = assess_quality(load_csv(io.BytesIO(b""), InputConfig()), QualityConfig())
    assert outcome(report, R.DQ02) is O.NOT_TESTABLE


# --- DQ03 ------------------------------------------------------------------


def test_dq03_duplicate_grain_is_fatal_without_dropping_rows() -> None:
    rows = clean_rows()
    rows.append(dict(rows[0]))
    report = assess(rows)
    (issue,) = issues(report, R.DQ03)
    assert issue.severity is Severity.FATAL and issue.row_count == 2
    exact = _measured(report, issue, 1)
    assert exact.segment.cohort_id == "dq03_exact_duplicate" and exact.value == 2


def test_dq03_same_key_on_other_day_is_fine(clean) -> None:
    assert outcome(clean, R.DQ03) is O.NOT_TRIGGERED


# --- DQ04 ------------------------------------------------------------------


def test_dq04_counter_relationship_warns_with_request_share() -> None:
    rows = clean_rows()
    rows[3][C.DEMAND_RESPONSES.value] = str(int(rows[3][C.DEMAND_REQUESTS.value]) + 5)
    report = assess(rows)
    (issue,) = issues(report, R.DQ04)
    assert issue.message_template_id == "DQ04_counter_relationship"
    assert issue.severity is Severity.WARNING and issue.row_count == 1
    assert IR.NO_FUNNEL_INFERENCE in issue.inference_restrictions
    assert len(issue.segment_ids) == 1


def test_dq04_impressions_without_requests_listed_explicitly() -> None:
    rows = clean_rows()
    rows.append(make_row(START, ("1009", "US", "300x250", "Banner", "Desktop"), 0, Decimal(1)))
    rows[-1][C.SUPPLY_IMPRESSIONS.value] = "40"
    rows[-1][C.DEMAND_WINS.value] = "40"
    templates = {i.message_template_id for i in issues(assess(rows), R.DQ04)}
    assert "DQ04_impressions_without_requests" in templates


@pytest.mark.parametrize("value", ["-5", "10.5"])
def test_dq04_negative_or_fractional_counter_is_fatal(value: str) -> None:
    rows = clean_rows()
    rows[2][C.DEMAND_WINS.value] = value
    fatal = [i for i in issues(assess(rows), R.DQ04) if i.severity is Severity.FATAL]
    assert [i.message_template_id for i in fatal] == ["DQ04_invalid_counter"]


def test_dq04_not_testable_without_any_valid_date() -> None:
    rows = clean_rows(days=1)
    for row in rows:
        row[C.DAY.value] = "bad"
    report = assess(rows)
    assert outcome(report, R.DQ04) is O.NOT_TESTABLE
    assert outcome(report, R.DQ01) is O.TRIGGERED


# --- DQ05 ------------------------------------------------------------------


def test_dq05_money_without_requests_warns_with_amount() -> None:
    rows = clean_rows()
    row = make_row(START, ("1009", "US", "300x250", "Banner", "Desktop"), 0, Decimal(1))
    row[C.REVENUE.value], row[C.PROFIT.value] = "3.50", "3.50"
    rows.append(row)
    report = assess(rows)
    templates = [i.message_template_id for i in issues(report, R.DQ05)]
    assert "DQ05_money_without_requests" in templates
    issue = issues(report, R.DQ05, "DQ05_money_without_requests")[0]
    assert _measured(report, issue).value == Decimal("3.50")
    assert IR.NO_YIELD_ESTIMATE in issue.inference_restrictions


def test_dq05_zero_money_with_zero_traffic_is_fine() -> None:
    rows = clean_rows()
    rows.append(make_row(START, ("1009", "US", "300x250", "Banner", "Desktop"), 0, Decimal(1)))
    assert outcome(assess(rows), R.DQ05) is O.NOT_TRIGGERED


# --- DQ06 ------------------------------------------------------------------


def _scale_day(rows, day: int, factor: str):
    scale(rows, lambda r: r[C.DAY.value] == _day(day), C.DEMAND_REQUESTS, factor)
    return rows


def test_dq06_low_final_day_is_flagged_as_possible_partial_day() -> None:
    report = assess(_scale_day(clean_rows(), 6, "0.6"))
    (issue,) = issues(report, R.DQ06)
    assert issue.message_template_id == "DQ06_possible_partial_day"
    fact = _measured(report, issue)
    assert fact.period.start == START + timedelta(days=6)
    assert fact.period.coverage_status is CoverageStatus.POSSIBLY_INCOMPLETE
    assert Decimal("0.55") < fact.value < Decimal("0.65")
    threshold = _facts(report)[issue.threshold_fact_ids[0]]
    assert threshold.value == Decimal("0.7")
    message = render_message(issue, _facts(report))
    assert "Possible incomplete day" in message and "cannot distinguish" in message


def test_dq06_interior_day_uses_the_stricter_ratio() -> None:
    assert outcome(assess(_scale_day(clean_rows(), 3, "0.6")), R.DQ06) is O.NOT_TRIGGERED
    assert outcome(assess(_scale_day(clean_rows(), 3, "0.4")), R.DQ06) is O.TRIGGERED


def test_dq06_missing_interior_date_is_a_gap_not_zero_traffic() -> None:
    rows = [r for r in clean_rows() if r[C.DAY.value] != _day(3)]
    report = assess(rows)
    (issue,) = issues(report, R.DQ06, "DQ06_missing_date")
    assert _measured(report, issue).value == 1
    assert issue.severity is Severity.WARNING


def test_dq06_fewer_than_three_days_is_unknown_not_passed() -> None:
    report = assess(clean_rows(days=2))
    assert outcome(report, R.DQ06) is O.NOT_TESTABLE
    assert [i.message_template_id for i in issues(report, R.DQ06)] == [
        "DQ06_completeness_unknown"]


def test_dq06_broad_slice_drop_flags_day_the_account_ratio_misses() -> None:
    big = ("2001", "US", "300x250", "Banner", "Desktop")
    small = [("2002", "DE", "728x90", "Video", "Mobile"), ("2003", "FR", "160x600", "Video",
             "Mobile"), ("2004", "JP", "468x60", "Video", "Mobile")]
    rows = []
    for d in range(7):
        day = START + timedelta(days=d)
        rows.append(make_row(day, big, 6_000_001 + d, Decimal(2)))
        for key in small:
            factor = Decimal("0.3") if d == 3 else Decimal(1)
            rows.append(make_row(day, key, int(1_333_337 * factor) + d, Decimal(2)))
    report = assess(rows)
    (issue,) = issues(report, R.DQ06)
    ratio, slices = (_measured(report, issue, i) for i in (0, 1))
    assert ratio.value > Decimal("0.5")
    assert slices.metric is M.SLICE_DROP_SHARE and slices.value >= Decimal("0.5")
    assert possibly_incomplete_days(load(rows), QualityConfig()) == {START + timedelta(days=3)}


# --- DQ08 / DQ09 (account level) -------------------------------------------


def test_dq08_account_concentration_triggers() -> None:
    rows = clean_rows()
    scale(rows, lambda r: r[C.DAY.value] == _day(2), C.DEMAND_ECPM, "20")
    report = assess(rows)
    templates = {i.message_template_id for i in issues(report, R.DQ08)}
    assert templates == {"DQ08_concentrated_revenue", "DQ08_concentrated_profit"}
    issue = issues(report, R.DQ08, "DQ08_concentrated_revenue")[0]
    fact = _measured(report, issue)
    assert fact.metric is M.MAX_DAY_REVENUE_SHARE and fact.period.included_dates == [
        START + timedelta(days=2)]
    assert IR.NO_STABLE_SCALE_CLAIM in issue.inference_restrictions


def test_dq08_short_period_is_not_testable() -> None:
    assert outcome(assess(clean_rows(days=3)), R.DQ08) is O.NOT_TESTABLE


def test_dq08_low_revenue_is_not_testable() -> None:
    rows = clean_rows(base_requests=(1_001, 1_999))
    assert outcome(assess(rows), R.DQ08) is O.NOT_TESTABLE


def test_dq09_account_request_spike() -> None:
    report = assess(_scale_day(clean_rows(), 4, "4"))
    (issue,) = issues(report, R.DQ09, "DQ09_request_spike")
    fact = _measured(report, issue)
    assert fact.metric is M.DAY_REQUEST_RATIO and fact.value >= 3
    assert IR.NO_TREND_CLAIM in issue.inference_restrictions


def test_dq09_short_period_is_not_testable() -> None:
    assert outcome(assess(clean_rows(days=3)), R.DQ09) is O.NOT_TESTABLE


# --- DQ10 / DQ11 -----------------------------------------------------------


@pytest.mark.parametrize("label,template", [
    ("-", "DQ10_placeholder_country"),
    ("N/A", "DQ10_placeholder_country"),
    ("ZZ", "DQ10_placeholder_country"),
    ("XXX", "DQ10_placeholder_country"),
    ("undefined", "DQ10_placeholder_country"),
    ("NotACountry", "DQ10_unverified_country"),
])
def test_dq10_placeholder_and_unverified_countries(label: str, template: str) -> None:
    rows = clean_rows()
    for row in rows[:7]:
        row[C.COUNTRY.value] = label
    report = assess(rows)
    (issue,) = issues(report, R.DQ10)
    assert issue.message_template_id == template
    assert IR.NO_COUNTRY_OPPORTUNITY in issue.inference_restrictions
    assert _measured(report, issue).segment.filters == {"country": label}


@pytest.fixture
def alpha3_rows():
    """Invented rows using the export's three-letter country convention."""
    rows = clean_rows()
    countries = ("USA", "GBR", "DEU", "FRA", "usa", "gbr", "US", "DE")
    for n, row in enumerate(rows):
        row[C.COUNTRY.value] = countries[n % len(countries)]
    return rows


def test_alpha3_and_alpha2_countries_survive_loader_and_quality(alpha3_rows) -> None:
    data = load(alpha3_rows)
    assert len(data.frame) == len(alpha3_rows)
    assert not data.parse_audit.invalid_cells
    report = assess_quality(data, QualityConfig())
    assert outcome(report, R.DQ10) is O.NOT_TRIGGERED
    assert not any(IR.NO_COUNTRY_OPPORTUNITY in i.inference_restrictions for i in issues(report, R.DQ10))


def test_country_reference_contains_all_iso_alpha3_codes():
    from pipeline.countries import ISO3166_ALPHA3_V1, accepted_countries
    assert len(ISO3166_ALPHA3_V1) == 249
    assert {"USA", "GBR", "DEU", "FRA", "US", "GB", "DE", "FR"} <= accepted_countries(QualityConfig().country_reference)
    assert not {"XX", "XXX", "undefined"} & accepted_countries(QualityConfig().country_reference)


def test_dq10_documented_extra_country_is_accepted() -> None:
    rows = clean_rows()
    rows[0][C.COUNTRY.value] = "CustomMarket"
    config = QualityConfig(extra_accepted_countries=("custommarket",))
    assert outcome(assess(rows, config), R.DQ10) is O.NOT_TRIGGERED


@pytest.mark.parametrize("label", ["9999x9999", "300x9999", "0x250", "5000x250", "native"])
def test_dq11_unusual_sizes(label: str) -> None:
    rows = clean_rows()
    rows[0][C.SIZE.value] = label
    report = assess(rows)
    (issue,) = issues(report, R.DQ11)
    assert _measured(report, issue).segment.filters == {"size": label}
    assert IR.NO_FORMAT_SPECIFIC_TEST in issue.inference_restrictions
    assert label in render_message(issue, _facts(report))


def test_dq11_allowed_special_format() -> None:
    rows = clean_rows()
    rows[0][C.SIZE.value] = "native"
    assert outcome(assess(rows, QualityConfig(allowed_special_sizes=("native",))),
                   R.DQ11) is O.NOT_TRIGGERED


# --- DQ12 ------------------------------------------------------------------


def test_dq12_cent_residual_is_a_notice_within_bound() -> None:
    rows = clean_rows()
    rows[0][C.PROFIT.value] = str(Decimal(rows[0][C.PROFIT.value]) + Decimal("0.01"))
    report = assess(rows)
    (issue,) = issues(report, R.DQ12)
    assert issue.message_template_id == "DQ12_reconciliation_within_bound"
    assert issue.severity is Severity.NOTICE
    assert outcome(report, R.DQ12) is O.NOT_TRIGGERED


def test_dq12_residual_above_rounding_bound_warns() -> None:
    rows = clean_rows()
    rows[0][C.PROFIT.value] = str(Decimal(rows[0][C.PROFIT.value]) + Decimal("0.02"))
    report = assess(rows)
    (issue,) = issues(report, R.DQ12)
    assert issue.message_template_id == "DQ12_reconciliation_exceeds_bound"
    assert _measured(report, issue).value == Decimal("0.02")
    bound = _facts(report)[issue.threshold_fact_ids[0]]
    assert Decimal("0.015") < bound.value < Decimal("0.0151")
    assert "Reported profit is preserved" in render_message(issue, _facts(report))


# --- DQ13 / DQ14 -----------------------------------------------------------


def test_dq13_per_million_srpm_is_confirmed(clean) -> None:
    (issue,) = issues(clean, R.DQ13)
    assert issue.message_template_id == "DQ13_srpm_per_million_confirmed"
    assert _measured(clean, issue).value >= Decimal("0.95")


def test_dq13_per_thousand_export_is_not_confirmed() -> None:
    rows = clean_rows()
    for row in rows:
        revenue, requests = Decimal(row[C.REVENUE.value]), int(row[C.DEMAND_REQUESTS.value])
        row[C.DEMAND_SRPM.value] = str((revenue / requests * 1000).quantize(Decimal("0.01")))
    report = assess(rows)
    (issue,) = issues(report, R.DQ13)
    assert issue.message_template_id == "DQ13_srpm_unconfirmed"
    assert IR.EXPORTED_RATE_UNTRUSTED in issue.inference_restrictions
    assert outcome(report, R.DQ13) is O.TRIGGERED


def test_dq13_indistinguishable_units_are_not_testable() -> None:
    rows = clean_rows(base_requests=(1_001, 1_999))
    for row in rows:
        row[C.REVENUE.value] = row[C.COST.value] = row[C.PROFIT.value] = "0"
        row[C.DEMAND_SRPM.value] = row[C.DEMAND_ECPM.value] = "0"
    report = assess(rows)
    assert outcome(report, R.DQ13) is O.NOT_TESTABLE
    assert issues(report, R.DQ13)[0].message_template_id == "DQ13_srpm_unconfirmed"


def test_dq14_ecpm_mismatch_warns_above_share() -> None:
    rows = clean_rows()
    for row in rows[:20]:
        row[C.DEMAND_ECPM.value] = str(Decimal(row[C.DEMAND_ECPM.value]) * 2)
    (issue,) = issues(assess(rows), R.DQ14)
    assert issue.message_template_id == "DQ14_ecpm_mismatch"
    assert issue.severity is Severity.WARNING


def test_dq14_small_mismatch_is_a_notice() -> None:
    rows = clean_rows()
    rows.append(make_row(START, ("1009", "US", "300x250", "Banner", "Desktop"), 100_000,
                         Decimal(2)))
    rows[-1][C.DEMAND_ECPM.value] = "9.99"
    report = assess(rows)
    (issue,) = issues(report, R.DQ14)
    assert issue.severity is Severity.NOTICE
    assert outcome(report, R.DQ14) is O.NOT_TRIGGERED


def test_dq14_without_impressions_is_not_testable() -> None:
    rows = clean_rows()
    for row in rows:
        row[C.SUPPLY_IMPRESSIONS.value] = "0"
    assert outcome(assess(rows), R.DQ14) is O.NOT_TESTABLE


# --- DQ15 ------------------------------------------------------------------


def test_dq15_row_share_far_from_request_share_warns() -> None:
    rows = clean_rows()
    for i in range(80):
        key = (f"3{i:03d}", "US", "300x250", "Banner", "Desktop")
        rows.append(make_row(START, key, 10, Decimal(1), response_rate=Decimal(0)))
    report = assess(rows)
    (issue,) = issues(report, R.DQ15, "DQ15_weighting_gap_zero_responses")
    row_share, request_share = (_measured(report, issue, i).value for i in (0, 1))
    assert row_share > Decimal("0.4") and request_share < Decimal("0.001")
    assert issues(report, R.DQ15, "DQ15_weighting_notice")


# --- DQ16 ------------------------------------------------------------------


def test_dq16_common_request_granularity_is_reported_not_corrected() -> None:
    rows = clean_rows()
    for row in rows:
        row[C.DEMAND_REQUESTS.value] = str(int(row[C.DEMAND_REQUESTS.value]) // 1000 * 1000)
    report = assess(rows)
    (issue,) = issues(report, R.DQ16)
    assert _measured(report, issue).value == 1000
    assert "have not been divided" in render_message(issue, _facts(report))


def test_dq16_dominant_granularity_tolerates_a_few_exceptions() -> None:
    rows = clean_rows()
    for i, row in enumerate(rows):
        if i % 50:
            row[C.DEMAND_REQUESTS.value] = str(int(row[C.DEMAND_REQUESTS.value]) // 500 * 500)
    report = assess(rows)
    (issue,) = issues(report, R.DQ16)
    assert _measured(report, issue).value == 500
    assert Decimal("0.95") <= _measured(report, issue, 1).value < 1


def test_dq16_needs_enough_rows() -> None:
    assert outcome(assess(clean_rows(days=3)), R.DQ16) is O.NOT_TESTABLE


# --- DQ17 ------------------------------------------------------------------


def test_dq17_bid_below_floor_warns() -> None:
    rows = clean_rows()
    for row in rows[:10]:
        row[C.DEMAND_BIDPRICE.value] = "0.40"
    report = assess(rows)
    (issue,) = issues(report, R.DQ17, "DQ17_bid_below_floor")
    assert issue.severity is Severity.WARNING
    assert IR.NO_FLOOR_CAUSAL_CLAIM in issue.inference_restrictions
    assert issues(report, R.DQ17, "DQ17_causal_limit")


def test_dq17_rare_zero_bid_with_responses_is_a_notice() -> None:
    rows = clean_rows()
    rows[0][C.DEMAND_BIDPRICE.value] = "0"
    (issue,) = issues(assess(rows), R.DQ17, "DQ17_zero_bid_responses")
    assert issue.severity is Severity.NOTICE


def test_dq17_without_responses_is_not_testable_but_keeps_causal_notice() -> None:
    rows = clean_rows()
    for row in rows:
        row[C.DEMAND_RESPONSES.value] = "0"
    report = assess(rows)
    assert outcome(report, R.DQ17) is O.NOT_TESTABLE
    assert issues(report, R.DQ17, "DQ17_causal_limit")


# --- DQ18 ------------------------------------------------------------------


def test_dq18_negative_revenue_warns_and_negative_profit_is_allowed() -> None:
    rows = clean_rows()
    rows[0][C.REVENUE.value] = "-5.00"
    rows[0][C.PROFIT.value] = str(Decimal("-5.00") - Decimal(rows[0][C.COST.value]))
    report = assess(rows)
    (issue,) = issues(report, R.DQ18)
    assert issue.message_template_id == "DQ18_negative_money"
    assert _measured(report, issue).value == Decimal("-5.00")
    only_profit = clean_rows()
    only_profit[0][C.COST.value] = str(Decimal(only_profit[0][C.REVENUE.value]) * 2)
    only_profit[0][C.PROFIT.value] = str(-Decimal(only_profit[0][C.REVENUE.value]))
    assert outcome(assess(only_profit), R.DQ18) is O.NOT_TRIGGERED


def test_dq18_rounded_zero_revenue_is_a_notice() -> None:
    rows = clean_rows()
    rows.append(make_row(START, ("1009", "US", "300x250", "Banner", "Desktop"), 200,
                         Decimal("0.2")))
    assert rows[-1][C.REVENUE.value] == "0.00"
    report = assess(rows)
    (issue,) = issues(report, R.DQ18)
    assert issue.message_template_id == "DQ18_zero_revenue_rounding"
    assert issue.severity is Severity.NOTICE


# --- DQ20 ------------------------------------------------------------------


def test_dq20_static_notice_is_always_present(clean) -> None:
    (issue,) = issues(clean, R.DQ20)
    assert issue.severity is Severity.NOTICE
    assert "observational daily aggregates" in render_message(issue, _facts(clean))


def test_thresholds_come_from_config() -> None:
    rows = _scale_day(clean_rows(), 6, "0.6")
    assert outcome(assess(rows), R.DQ06) is O.TRIGGERED
    relaxed = QualityConfig(partial_edge_day_ratio=Decimal("0.5"))
    assert outcome(assess(rows, relaxed), R.DQ06) is O.NOT_TRIGGERED
