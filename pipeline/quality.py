"""Versioned data-quality rules DQ01-DQ20 in two passes.

``assess_quality`` runs source, schema, counter and account-day checks on the
loaded rows. ``refine_quality`` adds slice coverage, concentration, spike,
materiality and peer checks for every preliminary candidate; it only appends.

Every rule is generic: thresholds come from ``QualityConfig`` and no rule
names a date, supply ID or label. A rule reports ``not_testable`` when the
evidence is insufficient, never a pass. Measured and threshold statistics are
provisional quality facts carried in ``QualityReport.facts``; issue messages
are rendered from those facts by ``render_message``.
"""

import hashlib
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

import numpy as np
import pandas as pd

from contracts import SCHEMA_VERSION
from contracts.config import QualityConfig, config_hash
from contracts.enums import (
    COUNTER_COLUMNS,
    DIMENSION_COLUMNS,
    CoverageStatus,
    DetectorFamily,
    Dimension,
    EligibilityStatus,
    FactKind,
    InferenceRestriction,
    InputColumn,
    MetricKey,
    QualityPass,
    QualityRuleId,
    RuleOutcome,
    Severity,
    Unit,
    ValidityStatus,
    Weighting,
)
from contracts.ids import IdPrefix, make_id, make_namespace
from contracts.models import (
    Candidate,
    Fact,
    Period,
    Provenance,
    QualityIssue,
    QualityReport,
    QualityRuleResult,
    Segment,
    Validity,
)
from contracts.runtime import FileError, LoadedData, MetricsStore
from pipeline.countries import accepted_countries
from pipeline.scope import account_segment, period, segment

C = InputColumn
R = QualityRuleId
M = MetricKey
IR = InferenceRestriction
_DAY = C.DAY
_KEY_DIMENSIONS = tuple(DIMENSION_COLUMNS)

# Flags a later stage may set in ``MetricsStore.quality_flags`` (keyed by
# candidate or segment ID) for checks that need detector-side information.
RANK_SENSITIVE_FLAG = "rank_changes_under_threshold_sensitivity"
PEER_KEYS_DIFFER_FLAG = "peer_keys_differ_on_fixed_dimension"

# Rules evaluated only once candidates exist.
_REFINE_ONLY = (R.DQ07, R.DQ19)

MESSAGE_TEMPLATES: dict[str, str] = {
    "DQ01_parse_failure": (
        "Analysis stopped: required fields could not be interpreted ({fields}, {rows} affected "
        "rows). No values were replaced with zero."
    ),
    "DQ01_schema_failure": (
        "Analysis stopped: required columns are missing or duplicated in the header. No values "
        "were replaced with zero."
    ),
    "DQ01_unreadable_file": (
        "Analysis stopped: the file is empty or could not be read as a CSV export. No values "
        "were replaced with zero."
    ),
    "DQ01_byte_limit": "Analysis stopped: the file exceeds the configured size limit.",
    "DQ01_row_limit": "Analysis stopped: the file exceeds the configured row limit.",
    "DQ01_extra_columns_ignored": (
        "Columns outside the required export contract were ignored explicitly."
    ),
    "DQ02_unknown_dimension": (
        "Unknown dimension values affect {request_share} of requests and {revenue_share} of "
        "revenue; these are not a targetable market/format."
    ),
    "DQ02_normalization_collision": (
        "Analysis stopped: distinct labels become identical after whitespace or case "
        "normalization ({rows} rows). A documented mapping is required; labels were not merged."
    ),
    "DQ02_missing_date": (
        "Analysis stopped: {rows} rows have no date. No rows were dropped or filled."
    ),
    "DQ03_duplicate_grain": (
        "Duplicate daily segment keys found ({rows} rows); totals may be double-counted. "
        "Resolve export grain before recommendations."
    ),
    "DQ04_invalid_counter": (
        "Analysis stopped: negative or fractional counter values in {rows} rows. No values were "
        "replaced."
    ),
    "DQ04_counter_relationship": (
        "Counter relationships are inconsistent in {rows} rows ({request_share} of requests; "
        "{m0_fields}). Affected funnel rates are undefined/unreliable; monetary totals are "
        "retained."
    ),
    "DQ04_impressions_without_requests": (
        "Counter relationships are inconsistent: {rows} rows report positive impressions with "
        "zero requests ({m1} of impressions). Affected funnel rates are undefined/unreliable; "
        "monetary totals are retained."
    ),
    "DQ05_money_without_requests": (
        "Some money has no usable traffic denominator ({m0} revenue in {rows} rows with zero "
        "requests); yield estimates for those rows are unavailable."
    ),
    "DQ05_money_without_impressions": (
        "Some money has no usable traffic denominator ({m0} revenue in {rows} rows with zero "
        "impressions); yield estimates for those rows are unavailable."
    ),
    "DQ06_possible_partial_day": (
        "Possible incomplete day or broad traffic change: {m0_day} has {m0:pct} of "
        "comparable-day request volume. Daily data cannot distinguish extraction coverage from "
        "real traffic movement. Full totals are retained; trend/scale conclusions need a "
        "sensitivity view."
    ),
    "DQ06_missing_date": (
        "Date coverage has gaps: {m0} calendar day(s) between the first and last date have no "
        "rows. Absence is not confirmed zero traffic; no observations were invented."
    ),
    "DQ06_completeness_unknown": (
        "Fewer than {t0} observed days: day completeness cannot be assessed (unknown, not "
        "passed)."
    ),
    "DQ07_intermittent_coverage": (
        "This slice lacks stable daily coverage ({m0}/{m1} days observed, {m2} revenue-positive); "
        "absence is not confirmed zero traffic. Treat scaling as exploratory."
    ),
    "DQ08_concentrated_revenue": (
        "Concentrated earnings: {m0_segment} earned {m0:pct} of its positive revenue on "
        "{m0_days}. Weekly averages may reflect a one-off spike, not repeatable yield."
    ),
    "DQ08_concentrated_profit": (
        "Concentrated earnings: {m0_segment} earned {m0:pct} of its positive profit on "
        "{m0_days}. Weekly averages may reflect a one-off spike, not repeatable yield."
    ),
    "DQ09_request_spike": (
        "Large daily change in requests on {m0_day} for {m0_segment}; check traffic mix, "
        "configuration and completeness before extrapolating."
    ),
    "DQ09_revenue_spike": (
        "Large daily change in revenue on {m0_day} for {m0_segment}; check traffic mix, "
        "configuration and completeness before extrapolating."
    ),
    "DQ09_yield_step": (
        "Large daily change in revenue per million requests between {m0_days} for "
        "{m0_segment}; check traffic mix, configuration and completeness before extrapolating. "
        "A possibly incomplete day cannot establish a performance collapse."
    ),
    "DQ10_placeholder_country": (
        "Placeholder country labels account for {request_share} of requests. Do not interpret "
        "them as a country opportunity."
    ),
    "DQ10_unverified_country": (
        "Unverified country labels account for {request_share} of requests. Do not interpret "
        "them as a country opportunity."
    ),
    "DQ11_unusual_size": (
        "Unusual or placeholder size {m0_segment} affects {request_share} of requests; confirm "
        "the format mapping before a format-specific test."
    ),
    "DQ12_reconciliation_exceeds_bound": (
        "Reported profit differs from revenue minus cost by {m0} in {rows} rows; this exceeds "
        "the declared source-rounding bound of {t0} per row. Reported profit is preserved."
    ),
    "DQ12_reconciliation_within_bound": (
        "Reported profit differs from revenue minus cost by {m0} in {rows} rows; this is within "
        "declared source-rounding bounds. Reported profit is preserved."
    ),
    "DQ13_srpm_per_million_confirmed": (
        "Unit check: sRPM means revenue per million requests, not per thousand."
    ),
    "DQ13_srpm_unconfirmed": (
        "Exported sRPM units could not be confirmed; recomputed revenue per million requests is "
        "used and exported sRPM is not trusted."
    ),
    "DQ14_ecpm_mismatch": (
        "Exported eCPM differs from recomputed revenue/impressions beyond source rounding for "
        "{m0:pct} of impressions; accounting totals use Revenue."
    ),
    "DQ14_ecpm_reconciliation": (
        "Exported eCPM differs from recomputed revenue/impressions beyond source rounding for "
        "{m0:pct} of impressions (below the warning share); accounting totals use Revenue."
    ),
    "DQ15_weighting_gap_zero_responses": (
        "Rows are not traffic: {m0:pct} of rows with zero responses represent {m1:pct} of "
        "requests. Rates and opportunity sizing use traffic-weighted totals."
    ),
    "DQ15_weighting_gap_zero_revenue": (
        "Rows are not traffic: {m0:pct} of rows with zero revenue represent {m1:pct} of "
        "requests. Rates and opportunity sizing use traffic-weighted totals."
    ),
    "DQ15_weighting_notice": (
        "Rows are not traffic: {m0:pct} of rows have zero responses ({m1:pct} of requests) and "
        "{m2:pct} have zero revenue ({m3:pct} of requests). Rates and opportunity sizing use "
        "traffic-weighted totals."
    ),
    "DQ16_counter_granularity": (
        "Request counts show a common granularity of {m0}; export scaling/counter definitions "
        "need confirmation. Counts have not been divided or otherwise corrected."
    ),
    "DQ16_mixed_granularity": (
        "Counters show different dominant granularities ({m0_fields}: {m0}); export "
        "scaling/counter definitions need confirmation. Counts have not been corrected."
    ),
    "DQ17_zero_bid_responses": (
        "Some responses carry a zero average bid price ({m0:pct} of responses); bid fields do "
        "not cover all successful traffic."
    ),
    "DQ17_bid_below_floor": (
        "Average bid and floor fields are not auction-level distributions; some successful "
        "traffic has average bid below average floor ({m0:pct} of responses). These fields "
        "cannot prove the optimal floor."
    ),
    "DQ17_causal_limit": (
        "Average bid and floor fields are not auction-level distributions and cannot prove the "
        "optimal floor."
    ),
    "DQ18_negative_money": (
        "Signed adjustments may affect apparent yield: negative revenue {m0} and negative cost "
        "{m1} in {rows} rows. Separate adjustments and rounding from true monetization failure."
    ),
    "DQ18_zero_revenue_rounding": (
        "Cent-rounded zero revenue may affect apparent yield: {rows} rows with positive "
        "impressions report zero revenue ({m0:pct} of impressions, {request_share} of "
        "requests). Separate rounding from true monetization failure."
    ),
    "DQ19_below_materiality": (
        "Exploratory or threshold-sensitive candidate: below its configured request, revenue or "
        "day thresholds. High rates alone do not establish a material, stable opportunity."
    ),
    "DQ19_threshold_sensitive": (
        "Exploratory or threshold-sensitive candidate: eligibility changes when thresholds are "
        "scaled. High rates alone do not establish a material, stable opportunity."
    ),
    "DQ19_flagged_day_dependent": (
        "Exploratory or threshold-sensitive candidate: depends on flagged or concentrated days. "
        "High rates alone do not establish a material, stable opportunity."
    ),
    "DQ19_rank_sensitive": (
        "Exploratory or threshold-sensitive candidate: its rank changes when thresholds are "
        "scaled. High rates alone do not establish a material, stable opportunity."
    ),
    "DQ20_observational_limits": (
        "These are observational daily aggregates. Floor, rendering and allocation effects are "
        "unproven; controlled tests and additional operational data are required. The export "
        "has no per-auction distributions, hourly completeness, request-processing costs, "
        "alternative demand, capacity or experiment outcomes."
    ),
    "DQ20_unmatched_peer": (
        "Peer keys differ on a fixed matching dimension; this comparison cannot be labelled a "
        "matched peer."
    ),
}


# --- Public helpers --------------------------------------------------------


def possibly_incomplete_days(data: LoadedData, config: QualityConfig) -> frozenset[date]:
    """Days DQ06 flags as possibly incomplete; shared with sensitivity views."""
    return _DayCoverage.compute(_View(data), config).flagged


def render_message(issue: QualityIssue, facts: Mapping[str, Fact]) -> str:
    """Fill the issue's template from its facts; quantities are code-rendered."""
    template = MESSAGE_TEMPLATES[issue.message_template_id]
    measured = [facts.get(f) for f in issue.measured_fact_ids]
    thresholds = [facts.get(f) for f in issue.threshold_fact_ids]

    def lookup(match: re.Match[str]) -> str:
        name, fmt = match.group(1), match.group(2)
        if name == "rows":
            return f"{issue.row_count:,}"
        if name in ("request_share", "revenue_share"):
            ref = issue.request_exposure_fact_id if name == "request_share" else (
                issue.revenue_exposure_fact_id)
            return _format(facts.get(ref) if ref else None, "pct")
        if name == "fields":
            columns = sorted({c.value for f in measured if f for c in f.provenance.source_columns})
            return ", ".join(columns) or "(not available)"
        ref = re.fullmatch(r"([mt])(\d+)(?:_(day|days|segment|fields))?", name)
        if not ref:
            raise KeyError(name)
        pool = measured if ref.group(1) == "m" else thresholds
        index = int(ref.group(2))
        fact = pool[index] if index < len(pool) else None
        part = ref.group(3)
        if fact is None:
            return "(not available)"
        if part == "day":
            return fact.period.start.isoformat()
        if part == "days":
            return ", ".join(d.isoformat() for d in fact.period.included_dates)
        if part == "segment":
            return _format_segment(fact.segment)
        if part == "fields":
            return ", ".join(c.value for c in fact.provenance.source_columns)
        return _format(fact, fmt)

    return re.sub(r"\{([a-z0-9_]+)(?::([a-z]+))?\}", lookup, template)


def _format(fact: Fact | None, fmt: str | None = None) -> str:
    if fact is None:
        return "(not available)"
    if fact.value is None:
        return "undefined"
    value = fact.value
    if fmt == "pct" or (fmt is None and fact.unit is Unit.FRACTION):
        if 0 < abs(value) < Decimal("0.0005"):
            return "<0.1%"
        return f"{value * 100:.1f}%"
    if fact.unit is Unit.RATIO:
        return f"{value:.2f}x"
    if fact.unit is Unit.EXPORT_DOLLAR:
        places = 2 if value == value.quantize(Decimal("0.01")) else 4
        return f"${value:,.{places}f} (currency code not supplied)"
    if fact.unit in (Unit.COUNT, Unit.DAYS) and value == value.to_integral_value():
        return f"{int(value):,}"
    return format(value, "f")


def _format_segment(seg: Segment) -> str:
    if not seg.filters:
        return "the account"
    return ", ".join(f"{d.value}={label}" for d, label in sorted(seg.filters.items()))


# --- Row view --------------------------------------------------------------


class _View:
    """Vector view of the frame: float arrays for masks, Decimals for reported sums."""

    def __init__(self, data: LoadedData) -> None:
        frame = data.frame
        self.data = data
        self.n = len(frame)
        self.lines = np.array([data.source_lines.get(i, 0) for i in range(self.n)], dtype=np.int64)
        self.day = frame[_DAY.value].to_numpy(dtype=object)
        self.has_day = np.array([d is not None for d in self.day], dtype=bool)
        self.ordinal = np.array([d.toordinal() if d is not None else -1 for d in self.day],
                                dtype=np.int64)
        self.label = {
            d: frame[c.value].to_numpy(dtype=object) for d, c in DIMENSION_COLUMNS.items()
        }
        self.dec: dict[InputColumn, np.ndarray] = {}
        self.num: dict[InputColumn, np.ndarray] = {}
        self.ok: dict[InputColumn, np.ndarray] = {}
        for column in C:
            if column is _DAY or column in DIMENSION_COLUMNS.values():
                continue
            values = frame[column.value].to_numpy(dtype=object)
            self.dec[column] = values
            self.num[column] = pd.to_numeric(pd.Series(values), errors="raise").to_numpy(
                dtype=float, na_value=np.nan)
            self.ok[column] = ~np.isnan(self.num[column])
        self.all = np.ones(self.n, dtype=bool)

    def dsum(self, column: InputColumn, mask: np.ndarray) -> Decimal:
        return sum(self.dec[column][mask & self.ok[column]], Decimal(0))

    def positive_dsum(self, column: InputColumn, mask: np.ndarray) -> Decimal:
        return self.dsum(column, mask & (np.nan_to_num(self.num[column]) > 0))

    def matching(self, filters: Mapping[Dimension, str],
                 dates: Iterable[date] | None) -> np.ndarray:
        mask = self.all.copy()
        for dimension, label in filters.items():
            mask &= self.label[Dimension(dimension)] == label
        if dates is not None:
            mask &= np.isin(self.ordinal, [d.toordinal() for d in dates])
        return mask

    def days_of(self, mask: np.ndarray) -> list[date]:
        return sorted({d for d in self.day[mask & self.has_day]})

    def daily(self, column: InputColumn, mask: np.ndarray,
              positive: bool = False) -> dict[date, Decimal]:
        """Exact per-day sums over rows with a valid day and value."""
        keep = mask & self.has_day & self.ok[column]
        if positive:
            keep &= self.num[column] > 0
        if not keep.any():
            return {}
        series = pd.Series(self.dec[column][keep], index=self.day[keep], dtype=object)
        return {d: sum(v, Decimal(0)) for d, v in series.groupby(level=0)}

    def key_frame(self, mask: np.ndarray) -> pd.DataFrame:
        cols = {d.value: self.label[d][mask] for d in _KEY_DIMENSIONS}
        frame = pd.DataFrame(cols)
        frame["_row"] = np.flatnonzero(mask)
        frame["_requests"] = np.nan_to_num(self.num[C.DEMAND_REQUESTS][mask])
        return frame


def _share(part: Decimal, whole: Decimal) -> Decimal | None:
    return part / whole if whole > 0 else None


def _fd(value: float) -> Decimal:
    """Float statistic to Decimal at 12 significant digits (never an accounting total)."""
    return Decimal(f"{value:.12g}")


# --- Day coverage (DQ06) ---------------------------------------------------


@dataclass(frozen=True)
class _DayCheck:
    day: date
    ratio: Decimal | None
    threshold: Decimal
    slice_share: Decimal | None
    flagged: bool


@dataclass(frozen=True)
class _DayCoverage:
    observed: tuple[date, ...]
    missing: tuple[date, ...]
    checks: tuple[_DayCheck, ...]
    material_slices: int
    testable: bool

    @property
    def flagged(self) -> frozenset[date]:
        return frozenset(c.day for c in self.checks if c.flagged)

    @classmethod
    def compute(cls, view: _View, config: QualityConfig) -> "_DayCoverage":
        observed = tuple(view.days_of(view.all))
        if not observed:
            return cls((), (), (), 0, False)
        span = (observed[-1] - observed[0]).days + 1
        calendar = {observed[0] + timedelta(days=i) for i in range(span)}
        missing = tuple(sorted(calendar - set(observed)))
        if len(observed) < config.min_days_for_completeness:
            return cls(observed, missing, (), 0, False)

        requests = view.daily(C.DEMAND_REQUESTS, view.all)
        totals = {d: requests.get(d, Decimal(0)) for d in observed}
        slices = cls._material_slices(view, observed, config)
        checks = []
        for day in observed:
            others = [totals[d] for d in observed if d != day]
            median = statistics.median(others)
            ratio = totals[day] / median if median > 0 else None
            edge = day in (observed[0], observed[-1])
            threshold = config.partial_edge_day_ratio if edge else config.partial_day_ratio
            slice_share = None
            if len(slices) >= config.min_days_for_completeness:
                below = 0
                for series in slices:
                    own = statistics.median([series[d] for d in observed if d != day])
                    below += own > 0 and series[day] < float(config.partial_day_ratio) * own
                slice_share = Decimal(below) / Decimal(len(slices))
            flagged = (ratio is not None and ratio < threshold) or (
                slice_share is not None and slice_share >= config.broad_drop_slice_share
            )
            checks.append(_DayCheck(day, ratio, threshold, slice_share, flagged))
        return cls(observed, missing, tuple(checks), len(slices), True)

    @staticmethod
    def _material_slices(
        view: _View, observed: Sequence[date], config: QualityConfig
    ) -> list[dict[date, float]]:
        """Single-dimension slices with a material, established request share."""
        keep = view.has_day & view.ok[C.DEMAND_REQUESTS]
        requests = view.num[C.DEMAND_REQUESTS][keep]
        total = requests.sum()
        if total <= 0:
            return []
        days = view.day[keep]
        result = []
        for dimension in _KEY_DIMENSIONS:
            frame = pd.DataFrame({"label": view.label[dimension][keep], "day": days, "r": requests})
            per_label = frame.groupby("label")["r"].sum()
            material = per_label / total >= float(config.material_slice_request_share)
            for label in per_label.index[material]:
                daily = frame[frame["label"] == label].groupby("day")["r"].sum()
                established = int((daily > 0).sum())
                if established >= config.min_days_for_completeness:
                    result.append({d: float(daily.get(d, 0.0)) for d in observed})
        return result


# --- Fact and issue builder ------------------------------------------------


class _Builder:
    def __init__(self, namespace: str, view: _View, config: QualityConfig,
                 coverage: _DayCoverage) -> None:
        self.ns = namespace
        self.v = view
        self.config = config
        self.version = config.rules_version
        self.coverage = coverage
        self.facts: dict[str, Fact] = {}
        self.account = account_segment(namespace)
        self.full = self.period(coverage.observed) if coverage.observed else None
        self.total_requests = view.dsum(C.DEMAND_REQUESTS, view.all)
        self.total_revenue = view.dsum(C.REVENUE, view.all)
        self.total_impressions = view.dsum(C.SUPPLY_IMPRESSIONS, view.all)

    # Scope

    def period(self, dates: Iterable[date]) -> Period:
        included = sorted(set(dates))
        if len(self.coverage.observed) < self.config.min_days_for_completeness:
            status = CoverageStatus.INSUFFICIENT_DATA
        elif set(included) & self.coverage.flagged or (
            self.coverage.missing and included and
            any(included[0] < d < included[-1] for d in self.coverage.missing)
        ):
            status = CoverageStatus.POSSIBLY_INCOMPLETE
        else:
            status = CoverageStatus.UNVERIFIED
        return period(self.ns, included, status)

    def cohort(self, tag: str, filters: Mapping[Dimension, str] | None = None) -> Segment:
        return segment(self.ns, filters or {}, cohort_id=tag)

    # Facts

    def fact(
        self,
        metric: MetricKey,
        value: Decimal | None,
        unit: Unit,
        seg: Segment,
        per: Period,
        columns: Sequence[InputColumn] = (),
        rows: np.ndarray | None = None,
        weighting: Weighting = Weighting.SUM,
        reason: str = "denominator is zero or not positive",
        formula: str | None = None,
        tag: str = "",
    ) -> str:
        formula_id = formula or f"{metric.value}_v{self.version}"
        lines = self.v.lines[rows] if rows is not None else np.array([], dtype=np.int64)
        row_digest = hashlib.sha256(
            self.v.data.input_sha256.encode() + np.sort(lines).astype("<i8").tobytes()
        ).hexdigest()
        fact_id = make_id(IdPrefix.FACT, self.ns, [
            "quality", formula_id, metric.value, seg.segment_id, per.period_id,
            [c.value for c in columns], tag,
        ])
        validity = Validity(status=ValidityStatus.VALID) if value is not None else Validity(
            status=ValidityStatus.UNDEFINED, reason=reason)
        fact = Fact(
            fact_id=fact_id,
            run_namespace=self.ns,
            kind=FactKind.QUALITY,
            segment=seg,
            period=per,
            metric=metric,
            value=value,
            unit=unit,
            validity=validity,
            provenance=Provenance(
                input_sha256=self.v.data.input_sha256,
                schema_version=SCHEMA_VERSION,
                formula_id=formula_id,
                source_columns=list(columns),
                source_row_set_hash=row_digest,
                source_line_ranges_ref=f"private_rows_{row_digest[:16]}",
                parent_fact_ids=[],
                aggregation_keys=list(seg.filters),
                weighting=weighting,
                quality_issue_ids=[],
            ),
        )
        existing = self.facts.get(fact_id)
        if existing is not None and existing != fact:
            raise RuntimeError(f"conflicting quality fact {fact_id}")
        self.facts[fact_id] = fact
        return fact_id

    def threshold(self, name: str, metric: MetricKey, unit: Unit) -> str:
        value = getattr(self.config, name)
        assert self.full is not None
        return self.fact(metric, Decimal(value), unit, self.account, self.full,
                         weighting=Weighting.NOT_APPLICABLE,
                         formula=f"threshold_{name}_v{self.version}")

    def count(self, mask: np.ndarray, seg: Segment, per: Period,
              columns: Sequence[InputColumn] = (), tag: str = "") -> str:
        return self.fact(M.ROW_COUNT, Decimal(int(mask.sum())), Unit.COUNT, seg, per, columns,
                         mask, tag=tag)

    def money(self, column: InputColumn, mask: np.ndarray, seg: Segment, per: Period,
              tag: str = "") -> str:
        metric = {C.REVENUE: M.REVENUE, C.COST: M.COST, C.PROFIT: M.PROFIT}[column]
        return self.fact(metric, self.v.dsum(column, mask), Unit.EXPORT_DOLLAR, seg, per,
                         [column], mask, tag=tag)

    def exposure(self, mask: np.ndarray, seg: Segment, per: Period) -> tuple[str, str]:
        requests = self.v.dsum(C.DEMAND_REQUESTS, mask)
        revenue = self.v.dsum(C.REVENUE, mask)
        req = self.fact(M.REQUEST_SHARE, _share(requests, self.total_requests), Unit.FRACTION,
                        seg, per, [C.DEMAND_REQUESTS], mask, Weighting.RATIO_OF_SUMS,
                        "account requests are not positive")
        rev = self.fact(M.REVENUE_SHARE, _share(revenue, self.total_revenue), Unit.FRACTION,
                        seg, per, [C.REVENUE], mask, Weighting.RATIO_OF_SUMS,
                        "account revenue is not positive")
        return req, rev

    def affected(self, mask: np.ndarray, tag: str) -> tuple[list[str], list[str]]:
        """Five-dimension segments touched by ``mask`` (largest first) and their row counts."""
        if not mask.any() or self.full is None:
            return [], []
        keys = [d.value for d in _KEY_DIMENSIONS]
        frame = self.v.key_frame(mask)
        groups = frame.groupby(keys, sort=True).agg(r=("_requests", "sum"), n=("_row", "size"))
        groups = groups.reset_index().sort_values(["r", "n", *keys], ascending=[False, False]
                                                  + [True] * len(keys))
        segment_ids, fact_ids = [], []
        for _, row in groups.head(self.config.max_issue_segments).iterrows():
            filters = {d: row[d.value] for d in _KEY_DIMENSIONS}
            full_segment = segment(self.ns, filters)
            rows = mask & self.v.matching(filters, None)
            segment_ids.append(full_segment.segment_id)
            fact_ids.append(self.count(rows, self.cohort(tag, filters), self.full, tag=tag))
        return segment_ids, fact_ids

    # Issues

    def issue(
        self,
        rule: QualityRuleId,
        severity: Severity,
        template: str,
        segments: Sequence[str],
        periods: Sequence[Period | None],
        measured: Sequence[str] = (),
        thresholds: Sequence[str] = (),
        rows: int = 0,
        exposure: tuple[str, str] | None = None,
        restrictions: Sequence[InferenceRestriction] = (),
        candidates: Iterable[str] = (),
    ) -> QualityIssue:
        assert template in MESSAGE_TEMPLATES, template
        period_ids = [p.period_id for p in periods if p is not None]
        candidate_ids = sorted(set(candidates))
        return QualityIssue(
            issue_id=make_id(IdPrefix.QUALITY_ISSUE, self.ns, [
                rule.value, template, list(segments), period_ids, list(measured), candidate_ids,
            ]),
            rule_id=rule,
            rule_version=self.version,
            severity=severity,
            segment_ids=list(segments),
            period_ids=period_ids,
            measured_fact_ids=list(measured),
            threshold_fact_ids=list(thresholds),
            row_count=rows,
            request_exposure_fact_id=exposure[0] if exposure else None,
            revenue_exposure_fact_id=exposure[1] if exposure else None,
            inference_restrictions=list(restrictions),
            message_template_id=template,
            affected_candidate_ids=candidate_ids,
        )


type _Result = tuple[RuleOutcome, list[QualityIssue]]

_PRIORITY = {RuleOutcome.TRIGGERED: 2, RuleOutcome.NOT_TESTABLE: 1, RuleOutcome.NOT_TRIGGERED: 0}


def _worst(outcomes: Iterable[RuleOutcome], empty: RuleOutcome) -> RuleOutcome:
    outcomes = list(outcomes)
    return max(outcomes, key=_PRIORITY.__getitem__) if outcomes else empty


def _triggered(issues: list[QualityIssue]) -> RuleOutcome:
    return RuleOutcome.TRIGGERED if issues else RuleOutcome.NOT_TRIGGERED


# --- First pass ------------------------------------------------------------


def assess_quality(data: LoadedData, config: QualityConfig) -> QualityReport:
    """First pass: source, schema, counter and account-day checks."""
    cfg_hash = config_hash(config)
    namespace = data.run_namespace or make_namespace(data.input_sha256, cfg_hash, SCHEMA_VERSION)
    view = _View(data)
    coverage = _DayCoverage.compute(view, config)
    b = _Builder(namespace, view, config, coverage)

    results: dict[QualityRuleId, _Result] = {
        R.DQ01: _dq01(b),
        R.DQ02: _dq02(b),
    }
    if b.full is None:
        for rule in R:
            results.setdefault(rule, (RuleOutcome.NOT_TESTABLE, []))
        results[R.DQ20] = _dq20_notice(b)
    else:
        results.update({
            R.DQ03: _dq03(b),
            R.DQ04: _dq04(b),
            R.DQ05: _dq05(b),
            R.DQ06: _dq06(b),
            R.DQ07: (RuleOutcome.NOT_TESTABLE, []),
            R.DQ08: _dq08(b, b.account, b.full, view.all, ()),
            R.DQ09: _dq09(b, b.account, b.full, view.all, (), yield_steps=False),
            R.DQ10: _dq10(b),
            R.DQ11: _dq11(b),
            R.DQ12: _dq12(b),
            R.DQ13: _dq13(b),
            R.DQ14: _dq14(b),
            R.DQ15: _dq15(b),
            R.DQ16: _dq16(b),
            R.DQ17: _dq17(b),
            R.DQ18: _dq18(b),
            R.DQ19: (RuleOutcome.NOT_TESTABLE, []),
            R.DQ20: _dq20_notice(b),
        })
    return _report(b, cfg_hash, QualityPass.INITIAL, results)


def _report(b: _Builder, cfg_hash: str, quality_pass: QualityPass,
            results: Mapping[QualityRuleId, _Result],
            prior: QualityReport | None = None) -> QualityReport:
    issues = list(prior.issues) if prior else []
    seen = {i.issue_id for i in issues}
    rule_results = []
    for rule in R:
        outcome, rule_issues = results[rule]
        new = [i for i in rule_issues if i.issue_id not in seen]
        seen.update(i.issue_id for i in new)
        issues.extend(new)
        rule_results.append(QualityRuleResult(
            rule_id=rule, rule_version=b.version, outcome=outcome,
            issue_ids=list(dict.fromkeys(i.issue_id for i in rule_issues)),
        ))
    facts = {f.fact_id: f for f in prior.facts} if prior else {}
    facts.update(b.facts)
    return QualityReport(
        run_namespace=b.ns,
        quality_pass=quality_pass,
        config_hash=cfg_hash,
        rule_results=rule_results,
        issues=issues,
        facts=list(facts.values()),
    )


_FILE_ERROR_TEMPLATES = {
    FileError.BYTE_LIMIT: "DQ01_byte_limit",
    FileError.ROW_LIMIT: "DQ01_row_limit",
    FileError.MISSING_COLUMNS: "DQ01_schema_failure",
    FileError.DUPLICATE_COLUMNS: "DQ01_schema_failure",
}


def _dq01(b: _Builder) -> _Result:
    audit = b.v.data.parse_audit
    issues = []
    for error in dict.fromkeys(audit.file_errors):
        template = _FILE_ERROR_TEMPLATES.get(error, "DQ01_unreadable_file")
        issues.append(b.issue(R.DQ01, Severity.FATAL, template, [b.account.segment_id], [],
                              restrictions=[IR.NO_RECOMMENDATIONS]))
    if audit.invalid_cells or audit.malformed_lines:
        bad = np.zeros(b.v.n, dtype=bool)
        measured = []
        for column in C:
            indices = audit.invalid_cells.get(column.value, ())
            if not indices:
                continue
            mask = np.zeros(b.v.n, dtype=bool)
            mask[list(indices)] = True
            bad |= mask
            if b.full is not None:
                measured.append(b.count(mask, b.cohort("dq01_unparseable"), b.full, [column]))
        if audit.malformed_lines and b.full is not None:
            measured.append(b.fact(M.ROW_COUNT, Decimal(len(audit.malformed_lines)), Unit.COUNT,
                                   b.cohort("dq01_malformed_lines"), b.full, tag="malformed"))
        segments, _ = b.affected(bad, "dq01_unparseable")
        issues.append(b.issue(
            R.DQ01, Severity.FATAL, "DQ01_parse_failure", [b.account.segment_id, *segments],
            [b.full], measured, rows=int(bad.sum()) + len(audit.malformed_lines),
            restrictions=[IR.NO_RECOMMENDATIONS],
        ))
    outcome = RuleOutcome.TRIGGERED if issues else RuleOutcome.NOT_TRIGGERED
    if audit.extra_columns:
        issues.append(b.issue(R.DQ01, Severity.NOTICE, "DQ01_extra_columns_ignored",
                              [b.account.segment_id], [b.full]))
    return outcome, issues


def _dq02(b: _Builder) -> _Result:
    data = b.v.data
    if not data.parse_audit.readable:
        return RuleOutcome.NOT_TESTABLE, []
    unknown = data.parse_audit.unknown_dimension_label
    issues = []
    for dimension in _KEY_DIMENSIONS:
        variants = data.normalization_audit.get(dimension.value, {})
        labels = b.v.label[dimension]
        if unknown in variants and any(not v.strip() for v in variants[unknown]) and b.full:
            mask = labels == unknown
            seg = segment(b.ns, {dimension: unknown})
            restriction = {
                Dimension.COUNTRY: IR.NO_COUNTRY_OPPORTUNITY,
                Dimension.SUPPLY_ID: IR.NO_RECOMMENDATIONS,
            }.get(dimension, IR.NO_FORMAT_SPECIFIC_TEST)
            issues.append(b.issue(
                R.DQ02, Severity.WARNING, "DQ02_unknown_dimension", [seg.segment_id], [b.full],
                [b.count(mask, seg, b.full, [DIMENSION_COLUMNS[dimension]])],
                rows=int(mask.sum()), exposure=b.exposure(mask, seg, b.full),
                restrictions=[restriction],
            ))
        collided = {
            label for label, raw in variants.items()
            if len(raw) > 1 and any(v.strip() for v in raw)
        }
        by_case: dict[str, list[str]] = {}
        for label in variants:
            by_case.setdefault(label.casefold(), []).append(label)
        collided |= {label for group in by_case.values() if len(group) > 1 for label in group}
        if collided:
            mask = np.isin(labels, sorted(collided))
            measured = [b.count(mask, b.cohort(f"dq02_collision_{dimension.value}"), b.full,
                                [DIMENSION_COLUMNS[dimension]])] if b.full else []
            segments = [segment(b.ns, {dimension: label}).segment_id for label in sorted(collided)]
            issues.append(b.issue(
                R.DQ02, Severity.FATAL, "DQ02_normalization_collision", segments, [b.full],
                measured, rows=int(mask.sum()), restrictions=[IR.NO_RECOMMENDATIONS],
            ))
    missing = data.parse_audit.missing_dates
    if missing:
        mask = np.zeros(b.v.n, dtype=bool)
        mask[list(missing)] = True
        measured = [b.count(mask, b.cohort("dq02_missing_date"), b.full, [_DAY])] if b.full else []
        issues.append(b.issue(
            R.DQ02, Severity.FATAL, "DQ02_missing_date", [b.account.segment_id], [b.full],
            measured, rows=len(missing), restrictions=[IR.NO_RECOMMENDATIONS],
        ))
    return _triggered(issues), issues


def _dq03(b: _Builder) -> _Result:
    frame = b.v.data.frame
    keys = [_DAY.value, *(c.value for c in DIMENSION_COLUMNS.values())]
    in_dup_key = frame.duplicated(subset=keys, keep=False).to_numpy() & b.v.has_day
    if not in_dup_key.any():
        return RuleOutcome.NOT_TRIGGERED, []
    exact = frame.duplicated(keep=False).to_numpy() & in_dup_key
    measured = [b.count(in_dup_key, b.cohort("dq03_duplicate_key"), b.full, [_DAY])]
    if exact.any():
        measured.append(b.count(exact, b.cohort("dq03_exact_duplicate"), b.full))
    segments, _ = b.affected(in_dup_key, "dq03_duplicate_key")
    return RuleOutcome.TRIGGERED, [b.issue(
        R.DQ03, Severity.FATAL, "DQ03_duplicate_grain", segments, [b.full], measured,
        rows=int(in_dup_key.sum()), exposure=b.exposure(in_dup_key, b.account, b.full),
        restrictions=[IR.NO_RECOMMENDATIONS],
    )]


def _dq04(b: _Builder) -> _Result:
    v, issues = b.v, []
    invalid = np.zeros(v.n, dtype=bool)
    measured = []
    for column in COUNTER_COLUMNS:
        values = np.nan_to_num(v.num[column])
        bad = v.ok[column] & ((values < 0) | (values != np.floor(values)))
        if bad.any():
            invalid |= bad
            measured.append(b.count(bad, b.cohort("dq04_invalid_counter"), b.full, [column]))
    if invalid.any():
        segments, _ = b.affected(invalid, "dq04_invalid_counter")
        issues.append(b.issue(
            R.DQ04, Severity.FATAL, "DQ04_invalid_counter", segments, [b.full], measured,
            rows=int(invalid.sum()), exposure=b.exposure(invalid, b.account, b.full),
            restrictions=[IR.NO_RECOMMENDATIONS],
        ))

    def counter(column: InputColumn) -> np.ndarray:
        return np.nan_to_num(v.num[column])

    pairs = (
        (C.DEMAND_RESPONSES, C.DEMAND_REQUESTS),
        (C.DEMAND_WINS, C.DEMAND_RESPONSES),
        (C.SUPPLY_IMPRESSIONS, C.DEMAND_WINS),
    )
    for down, up in pairs:
        mask = v.ok[down] & v.ok[up] & (counter(down) > counter(up))
        if not mask.any():
            continue
        tag = f"dq04_{down.name.lower()}_over_{up.name.lower()}"
        segments, per_segment = b.affected(mask, tag)
        cohort = b.cohort(tag)
        issues.append(b.issue(
            R.DQ04, Severity.WARNING, "DQ04_counter_relationship", segments, [b.full],
            [b.count(mask, cohort, b.full, [down, up]), *per_segment], rows=int(mask.sum()),
            exposure=b.exposure(mask, cohort, b.full), restrictions=[IR.NO_FUNNEL_INFERENCE],
        ))
    mask = (v.ok[C.SUPPLY_IMPRESSIONS] & v.ok[C.DEMAND_REQUESTS]
            & (counter(C.SUPPLY_IMPRESSIONS) > 0) & (counter(C.DEMAND_REQUESTS) == 0))
    if mask.any():
        tag = "dq04_impressions_without_requests"
        cohort = b.cohort(tag)
        segments, per_segment = b.affected(mask, tag)
        impressions = b.fact(
            M.IMPRESSION_SHARE, _share(v.dsum(C.SUPPLY_IMPRESSIONS, mask), b.total_impressions),
            Unit.FRACTION, cohort, b.full, [C.SUPPLY_IMPRESSIONS], mask, Weighting.RATIO_OF_SUMS)
        issues.append(b.issue(
            R.DQ04, Severity.WARNING, "DQ04_impressions_without_requests", segments, [b.full],
            [b.count(mask, cohort, b.full, [C.SUPPLY_IMPRESSIONS, C.DEMAND_REQUESTS]),
             impressions, *per_segment],
            rows=int(mask.sum()), exposure=b.exposure(mask, cohort, b.full),
            restrictions=[IR.NO_FUNNEL_INFERENCE],
        ))
    return _triggered(issues), issues


def _dq05(b: _Builder) -> _Result:
    v, issues = b.v, []
    money = np.zeros(v.n, dtype=bool)
    for column in (C.REVENUE, C.COST, C.PROFIT):
        money |= v.ok[column] & (np.nan_to_num(v.num[column]) != 0)
    for denominator, template in ((C.DEMAND_REQUESTS, "DQ05_money_without_requests"),
                                  (C.SUPPLY_IMPRESSIONS, "DQ05_money_without_impressions")):
        mask = money & v.ok[denominator] & (np.nan_to_num(v.num[denominator]) == 0)
        if not mask.any():
            continue
        tag = f"dq05_money_without_{denominator.name.lower()}"
        cohort = b.cohort(tag)
        segments, per_segment = b.affected(mask, tag)
        measured = [b.money(c, mask, cohort, b.full) for c in (C.REVENUE, C.COST, C.PROFIT)]
        measured.append(b.count(mask, cohort, b.full, [denominator]))
        issues.append(b.issue(
            R.DQ05, Severity.WARNING, template, segments, [b.full], [*measured, *per_segment],
            rows=int(mask.sum()), exposure=b.exposure(mask, cohort, b.full),
            restrictions=[IR.NO_YIELD_ESTIMATE],
        ))
    return _triggered(issues), issues


def _dq06(b: _Builder) -> _Result:
    coverage, issues = b.coverage, []
    if coverage.missing:
        issues.append(b.issue(
            R.DQ06, Severity.WARNING, "DQ06_missing_date", [b.account.segment_id], [b.full],
            [b.fact(M.MISSING_DAYS, Decimal(len(coverage.missing)), Unit.DAYS, b.account, b.full,
                    [_DAY], b.v.all)],
            restrictions=[IR.NO_TREND_CLAIM, IR.NO_STABLE_SCALE_CLAIM],
        ))
    if not coverage.testable:
        issues.append(b.issue(
            R.DQ06, Severity.NOTICE, "DQ06_completeness_unknown", [b.account.segment_id],
            [b.full], thresholds=[b.threshold("min_days_for_completeness", M.OBSERVED_DAYS,
                                              Unit.DAYS)],
            restrictions=[IR.NO_TREND_CLAIM, IR.NO_STABLE_SCALE_CLAIM],
        ))
        return (RuleOutcome.TRIGGERED if coverage.missing else RuleOutcome.NOT_TESTABLE), issues
    for check in coverage.checks:
        if not check.flagged:
            continue
        day = b.period([check.day])
        mask = b.v.matching({}, [check.day])
        edge = check.threshold == b.config.partial_edge_day_ratio
        measured = [_day_ratio(b, M.DAY_REQUEST_RATIO, b.account, day, mask, check.ratio)]
        thresholds = [b.threshold("partial_edge_day_ratio" if edge else "partial_day_ratio",
                                  M.DAY_REQUEST_RATIO, Unit.RATIO)]
        if check.slice_share is not None:
            measured.append(b.fact(M.SLICE_DROP_SHARE, check.slice_share, Unit.FRACTION,
                                   b.account, day, [C.DEMAND_REQUESTS], mask,
                                   Weighting.NOT_APPLICABLE))
            thresholds.append(b.threshold("broad_drop_slice_share", M.SLICE_DROP_SHARE,
                                          Unit.FRACTION))
        issues.append(b.issue(
            R.DQ06, Severity.WARNING, "DQ06_possible_partial_day", [b.account.segment_id], [day],
            measured, thresholds, rows=int(mask.sum()),
            exposure=b.exposure(mask, b.account, day),
            restrictions=[IR.NO_TREND_CLAIM, IR.NO_STABLE_SCALE_CLAIM],
        ))
    return _triggered(issues), issues


def _day_ratio(b: _Builder, metric: MetricKey, seg: Segment, day: Period, rows: np.ndarray,
               ratio: Decimal | None) -> str:
    column = C.DEMAND_REQUESTS if metric is M.DAY_REQUEST_RATIO else C.REVENUE
    return b.fact(metric, ratio, Unit.RATIO, seg, day, [column, _DAY], rows,
                  Weighting.NOT_APPLICABLE, "median of the other observed days is zero")


def _dq08(b: _Builder, seg: Segment, per: Period, mask: np.ndarray,
          candidates: Sequence[str]) -> _Result:
    """One/two-day concentration of positive revenue and positive profit."""
    days = b.v.days_of(mask)
    if len(days) < b.config.min_stable_days:
        return RuleOutcome.NOT_TESTABLE, []
    issues = []
    outcomes = []
    metrics = (
        (C.REVENUE, M.MAX_DAY_REVENUE_SHARE, M.TOP_TWO_DAY_REVENUE_SHARE,
         "DQ08_concentrated_revenue"),
        (C.PROFIT, M.MAX_DAY_PROFIT_SHARE, M.TOP_TWO_DAY_PROFIT_SHARE,
         "DQ08_concentrated_profit"),
    )
    for column, max_metric, two_metric, template in metrics:
        daily = b.v.daily(column, mask, positive=True)
        gross = sum(daily.values(), Decimal(0))
        if gross < b.config.concentration_min_revenue:
            if column is C.REVENUE:
                outcomes.append(RuleOutcome.NOT_TESTABLE)
            continue
        ranked = sorted(daily.items(), key=lambda kv: (-kv[1], kv[0]))
        top = ranked[0]
        two = ranked[:2]
        max_share = top[1] / gross
        two_share = sum((x for _, x in two), Decimal(0)) / gross
        hit_max = max_share >= b.config.max_day_share
        hit_two = two_share >= b.config.max_two_day_share
        if not (hit_max or hit_two):
            outcomes.append(RuleOutcome.NOT_TRIGGERED)
            continue
        outcomes.append(RuleOutcome.TRIGGERED)
        top_period = b.period([top[0]])
        two_period = b.period([d for d, _ in two])
        positive = mask & (np.nan_to_num(b.v.num[column]) > 0)
        max_fact = b.fact(max_metric, max_share, Unit.FRACTION, seg, top_period, [column, _DAY],
                          positive & b.v.matching({}, [top[0]]), Weighting.RATIO_OF_SUMS)
        two_fact = b.fact(two_metric, two_share, Unit.FRACTION, seg, two_period, [column, _DAY],
                          positive & b.v.matching({}, [d for d, _ in two]),
                          Weighting.RATIO_OF_SUMS)
        measured = [max_fact, two_fact] if hit_max else [two_fact, max_fact]
        thresholds = [b.threshold("max_day_share", max_metric, Unit.FRACTION),
                      b.threshold("max_two_day_share", two_metric, Unit.FRACTION)]
        if not hit_max:
            thresholds.reverse()
        issues.append(b.issue(
            R.DQ08, Severity.WARNING, template, [seg.segment_id], [per], measured, thresholds,
            rows=int(mask.sum()), exposure=b.exposure(mask, seg, per),
            restrictions=[IR.NO_STABLE_SCALE_CLAIM], candidates=candidates,
        ))
    return _worst(outcomes, RuleOutcome.NOT_TESTABLE), issues


def _dq09(b: _Builder, seg: Segment, per: Period, mask: np.ndarray, candidates: Sequence[str],
          yield_steps: bool) -> _Result:
    """Daily request/revenue spikes and, for candidates, adjacent-day yield steps."""
    days = b.v.days_of(mask)
    if len(days) < b.config.min_stable_days:
        return RuleOutcome.NOT_TESTABLE, []
    issues = []
    requests = b.v.daily(C.DEMAND_REQUESTS, mask)
    revenue = b.v.daily(C.REVENUE, mask, positive=True)
    material_requests = b.config.spike_min_request_share * b.total_requests
    for metric, series, template in ((M.DAY_REQUEST_RATIO, requests, "DQ09_request_spike"),
                                     (M.DAY_REVENUE_RATIO, revenue, "DQ09_revenue_spike")):
        values = {d: series.get(d, Decimal(0)) for d in days}
        for day in days:
            median = statistics.median([values[d] for d in days if d != day])
            value = values[day]
            spike = (value >= b.config.spike_multiple * median) if median > 0 else value > 0
            material = (requests.get(day, Decimal(0)) >= material_requests
                        or revenue.get(day, Decimal(0)) >= b.config.spike_min_revenue)
            if not (spike and material):
                continue
            day_period = b.period([day])
            rows = mask & b.v.matching({}, [day])
            ratio = value / median if median > 0 else None
            issues.append(b.issue(
                R.DQ09, Severity.WARNING, template, [seg.segment_id], [day_period],
                [_day_ratio(b, metric, seg, day_period, rows, ratio)],
                [b.threshold("spike_multiple", metric, Unit.RATIO)], rows=int(rows.sum()),
                exposure=b.exposure(rows, seg, day_period), restrictions=[IR.NO_TREND_CLAIM],
                candidates=candidates,
            ))
    if yield_steps:
        signed = b.v.daily(C.REVENUE, mask)
        for first, second in zip(days, days[1:]):
            if (second - first).days != 1:
                continue
            r1, r2 = requests.get(first, Decimal(0)), requests.get(second, Decimal(0))
            v1, v2 = signed.get(first, Decimal(0)), signed.get(second, Decimal(0))
            if min(r1, r2) < b.config.yield_step_min_requests or (
                    max(v1, v2) < b.config.yield_step_min_revenue):
                continue
            y1, y2 = v1 / r1, v2 / r2
            if y1 > 0:
                ratio = y2 / y1
                step = ratio >= b.config.yield_step_up or ratio <= b.config.yield_step_down
            else:
                ratio, step = None, y2 > 0
            if not step:
                continue
            pair = b.period([first, second])
            rows = mask & b.v.matching({}, [first, second])
            issues.append(b.issue(
                R.DQ09, Severity.WARNING, "DQ09_yield_step", [seg.segment_id], [pair],
                [b.fact(M.ADJACENT_DAY_YIELD_RATIO, ratio, Unit.RATIO, seg, pair,
                        [C.REVENUE, C.DEMAND_REQUESTS, _DAY], rows, Weighting.RATIO_OF_SUMS,
                        "earlier day has no positive yield")],
                [b.threshold("yield_step_up", M.ADJACENT_DAY_YIELD_RATIO, Unit.RATIO),
                 b.threshold("yield_step_down", M.ADJACENT_DAY_YIELD_RATIO, Unit.RATIO)],
                rows=int(rows.sum()), exposure=b.exposure(rows, seg, pair),
                restrictions=[IR.NO_TREND_CLAIM], candidates=candidates,
            ))
    return _triggered(issues), issues


def _label_issues(b: _Builder, rule: QualityRuleId, dimension: Dimension, labels: Sequence[str],
                  template: str, restrictions: Sequence[InferenceRestriction],
                  tag: str) -> list[QualityIssue]:
    """One issue covering ``labels`` of ``dimension`` with per-label request shares."""
    if not labels:
        return []
    column = DIMENSION_COLUMNS[dimension]
    mask = np.isin(b.v.label[dimension], list(labels))
    segments, measured = [], []
    for label in labels:
        seg = segment(b.ns, {dimension: label})
        rows = b.v.label[dimension] == label
        segments.append(seg.segment_id)
        measured.append(b.exposure(rows, seg, b.full)[0])
    cohort = b.cohort(tag)
    return [b.issue(
        rule, Severity.WARNING, template, segments, [b.full],
        [*measured, b.count(mask, cohort, b.full, [column])], rows=int(mask.sum()),
        exposure=b.exposure(mask, cohort, b.full), restrictions=restrictions,
    )]


def _dq10(b: _Builder) -> _Result:
    markers = {p.strip().casefold() for p in b.config.country_placeholders}
    markers.add(b.v.data.parse_audit.unknown_dimension_label.casefold())
    accepted = accepted_countries(b.config.country_reference, b.config.extra_accepted_countries)
    labels = sorted(set(b.v.label[Dimension.COUNTRY]))
    placeholders = [x for x in labels if x.strip().casefold() in markers]
    unverified = [x for x in labels if x not in placeholders and x.strip().upper() not in accepted]
    restrictions = [IR.NO_COUNTRY_OPPORTUNITY]
    issues = [
        *_label_issues(b, R.DQ10, Dimension.COUNTRY, placeholders, "DQ10_placeholder_country",
                       restrictions, "dq10_placeholder_country"),
        *_label_issues(b, R.DQ10, Dimension.COUNTRY, unverified, "DQ10_unverified_country",
                       restrictions, "dq10_unverified_country"),
    ]
    return _triggered(issues), issues


_SIZE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")


def _unusual_size(label: str, config: QualityConfig) -> bool:
    match = _SIZE.match(label)
    if not match:
        return True
    dims = (int(match.group(1)), int(match.group(2)))
    return any(x <= 0 or x == config.size_sentinel or x > config.size_max_pixels for x in dims)


def _dq11(b: _Builder) -> _Result:
    unknown = b.v.data.parse_audit.unknown_dimension_label
    allowed = set(b.config.allowed_special_sizes)
    labels = sorted(set(b.v.label[Dimension.SIZE]))
    issues = []
    for label in labels:
        if label == unknown or label in allowed or not _unusual_size(label, b.config):
            continue
        issues += _label_issues(b, R.DQ11, Dimension.SIZE, [label], "DQ11_unusual_size",
                                [IR.NO_FORMAT_SPECIFIC_TEST, IR.NO_STABLE_SCALE_CLAIM],
                                f"dq11_unusual_size_{len(issues)}")
    return _triggered(issues), issues


def _dq12(b: _Builder) -> _Result:
    v = b.v
    ok = v.ok[C.REVENUE] & v.ok[C.COST] & v.ok[C.PROFIT]
    if not ok.any():
        return RuleOutcome.NOT_TESTABLE, []
    quantum = v.data.precision.money_quantum
    bound = Decimal("0.5") * 3 * quantum + b.config.reconciliation_epsilon
    approx = np.nan_to_num(v.num[C.PROFIT] - (v.num[C.REVENUE] - v.num[C.COST]))
    nonzero = ok & (np.abs(approx) > 1e-9)
    residual: dict[int, Decimal] = {}
    for i in np.flatnonzero(nonzero):
        value = v.dec[C.PROFIT][i] - (v.dec[C.REVENUE][i] - v.dec[C.COST][i])
        if value != 0:
            residual[int(i)] = value
    exceed = np.zeros(v.n, dtype=bool)
    within = np.zeros(v.n, dtype=bool)
    for i, value in residual.items():
        (exceed if abs(value) > bound else within)[i] = True
    aggregate = sum(residual.values(), Decimal(0))
    aggregate_bound = bound * int(ok.sum())
    issues = []
    columns = [C.PROFIT, C.REVENUE, C.COST]
    if exceed.any() or abs(aggregate) > aggregate_bound:
        cohort = b.cohort("dq12_exceeds_bound")
        rows = exceed if exceed.any() else ok
        segments, per_segment = b.affected(rows, "dq12_exceeds_bound")
        measured = [
            b.fact(M.PROFIT_RECONCILIATION_RESIDUAL,
                   sum((residual[i] for i in np.flatnonzero(rows) if i in residual), Decimal(0)),
                   Unit.EXPORT_DOLLAR, cohort, b.full, columns, rows),
            b.fact(M.PROFIT_RECONCILIATION_RESIDUAL, aggregate, Unit.EXPORT_DOLLAR, b.account,
                   b.full, columns, ok),
            *per_segment,
        ]
        thresholds = [
            b.fact(M.RECONCILIATION_BOUND, bound, Unit.EXPORT_DOLLAR, b.account, b.full,
                   weighting=Weighting.NOT_APPLICABLE, formula=f"row_rounding_bound_v{b.version}"),
            b.fact(M.RECONCILIATION_BOUND, aggregate_bound, Unit.EXPORT_DOLLAR, b.account,
                   b.full, columns, ok, Weighting.SUM,
                   formula=f"aggregate_rounding_bound_v{b.version}"),
        ]
        issues.append(b.issue(
            R.DQ12, Severity.WARNING, "DQ12_reconciliation_exceeds_bound", segments, [b.full],
            measured, thresholds, rows=int(rows.sum()),
            exposure=b.exposure(rows, cohort, b.full),
        ))
    outcome = _triggered(issues)
    if within.any():
        cohort = b.cohort("dq12_within_bound")
        segments, per_segment = b.affected(within, "dq12_within_bound")
        issues.append(b.issue(
            R.DQ12, Severity.NOTICE, "DQ12_reconciliation_within_bound", segments, [b.full],
            [b.fact(M.PROFIT_RECONCILIATION_RESIDUAL,
                    sum((residual[i] for i in np.flatnonzero(within)), Decimal(0)),
                    Unit.EXPORT_DOLLAR, cohort, b.full, columns, within), *per_segment],
            [b.fact(M.RECONCILIATION_BOUND, bound, Unit.EXPORT_DOLLAR, b.account, b.full,
                    weighting=Weighting.NOT_APPLICABLE,
                    formula=f"row_rounding_bound_v{b.version}")],
            rows=int(within.sum()), exposure=b.exposure(within, cohort, b.full),
        ))
    return outcome, issues


def _reconstruction(b: _Builder, rate: InputColumn, base: InputColumn, multiplier: int,
                    mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Absolute revenue reconstruction error and its source-rounding tolerance."""
    precision = b.v.data.precision
    base_values = b.v.num[base][mask]
    recon = b.v.num[rate][mask] * base_values / multiplier
    error = np.abs(b.v.num[C.REVENUE][mask] - recon)
    tolerance = (float(precision.money_quantum) / 2
                 + float(precision.rate_quantum) / 2 * base_values / multiplier
                 + float(b.config.reconciliation_epsilon))
    return error, tolerance


def _dq13(b: _Builder) -> _Result:
    v = b.v
    rows = (v.ok[C.DEMAND_REQUESTS] & v.ok[C.DEMAND_SRPM] & v.ok[C.REVENUE]
            & (np.nan_to_num(v.num[C.DEMAND_REQUESTS]) > 0))
    unconfirmed = [b.issue(R.DQ13, Severity.WARNING, "DQ13_srpm_unconfirmed",
                           [b.account.segment_id], [b.full],
                           restrictions=[IR.EXPORTED_RATE_UNTRUSTED])]
    if not rows.any():
        return RuleOutcome.NOT_TESTABLE, unconfirmed
    err_m, tol_m = _reconstruction(b, C.DEMAND_SRPM, C.DEMAND_REQUESTS, b.config.srpm_multiplier,
                                   rows)
    err_k, tol_k = _reconstruction(b, C.DEMAND_SRPM, C.DEMAND_REQUESTS,
                                   b.config.srpm_alternative_multiplier, rows)
    requests = v.num[C.DEMAND_REQUESTS][rows]
    recon_gap = np.abs(
        v.num[C.DEMAND_SRPM][rows] * requests
        * (1 / b.config.srpm_multiplier - 1 / b.config.srpm_alternative_multiplier))
    distinct = recon_gap > tol_m + tol_k
    if not distinct.any():
        return RuleOutcome.NOT_TESTABLE, unconfirmed
    distinct_rows = rows.copy()
    distinct_rows[rows] = distinct
    matched_rows = rows.copy()
    matched_rows[rows] = distinct & (err_m <= tol_m)
    share = v.dsum(C.DEMAND_REQUESTS, matched_rows) / v.dsum(C.DEMAND_REQUESTS, distinct_rows)
    weights = requests[distinct]
    weighted_m = float(np.sum(weights * err_m[distinct]) / np.sum(weights))
    weighted_k = float(np.sum(weights * err_k[distinct]) / np.sum(weights))
    ratio = _fd(weighted_k / weighted_m) if weighted_m > 0 else None
    confirmed = share >= b.config.srpm_confirm_share and weighted_k > 0 and (
        weighted_m == 0 or weighted_k / weighted_m >= float(b.config.srpm_error_ratio))
    columns = [C.DEMAND_SRPM, C.DEMAND_REQUESTS, C.REVENUE]
    measured = [
        b.fact(M.SRPM_MATCH_SHARE, share, Unit.FRACTION, b.account, b.full, columns,
               distinct_rows, Weighting.REQUEST_WEIGHTED),
        b.fact(M.SRPM_ERROR_RATIO, ratio, Unit.RATIO, b.account, b.full, columns, distinct_rows,
               Weighting.REQUEST_WEIGHTED, "per-million reconstruction error is zero"),
    ]
    thresholds = [b.threshold("srpm_confirm_share", M.SRPM_MATCH_SHARE, Unit.FRACTION),
                  b.threshold("srpm_error_ratio", M.SRPM_ERROR_RATIO, Unit.RATIO)]
    if confirmed:
        return RuleOutcome.NOT_TRIGGERED, [b.issue(
            R.DQ13, Severity.NOTICE, "DQ13_srpm_per_million_confirmed", [b.account.segment_id],
            [b.full], measured, thresholds, rows=int(distinct_rows.sum()))]
    return RuleOutcome.TRIGGERED, [b.issue(
        R.DQ13, Severity.WARNING, "DQ13_srpm_unconfirmed", [b.account.segment_id], [b.full],
        measured, thresholds, rows=int(distinct_rows.sum()),
        restrictions=[IR.EXPORTED_RATE_UNTRUSTED])]


def _dq14(b: _Builder) -> _Result:
    v = b.v
    rows = (v.ok[C.SUPPLY_IMPRESSIONS] & v.ok[C.DEMAND_ECPM] & v.ok[C.REVENUE]
            & (np.nan_to_num(v.num[C.SUPPLY_IMPRESSIONS]) > 0))
    if not rows.any():
        return RuleOutcome.NOT_TESTABLE, []
    error, tolerance = _reconstruction(b, C.DEMAND_ECPM, C.SUPPLY_IMPRESSIONS, 1000, rows)
    mismatch = rows.copy()
    mismatch[rows] = error > tolerance
    if not mismatch.any():
        return RuleOutcome.NOT_TRIGGERED, []
    share = v.dsum(C.SUPPLY_IMPRESSIONS, mismatch) / v.dsum(C.SUPPLY_IMPRESSIONS, rows)
    cohort = b.cohort("dq14_ecpm_mismatch")
    segments, per_segment = b.affected(mismatch, "dq14_ecpm_mismatch")
    measured = [b.fact(M.ECPM_MISMATCH_SHARE, share, Unit.FRACTION, b.account, b.full,
                       [C.DEMAND_ECPM, C.SUPPLY_IMPRESSIONS, C.REVENUE], rows,
                       Weighting.RATIO_OF_SUMS), *per_segment]
    thresholds = [b.threshold("ecpm_mismatch_share", M.ECPM_MISMATCH_SHARE, Unit.FRACTION)]
    warn = share > b.config.ecpm_mismatch_share
    return (RuleOutcome.TRIGGERED if warn else RuleOutcome.NOT_TRIGGERED), [b.issue(
        R.DQ14, Severity.WARNING if warn else Severity.NOTICE,
        "DQ14_ecpm_mismatch" if warn else "DQ14_ecpm_reconciliation", segments, [b.full],
        measured, thresholds, rows=int(mismatch.sum()),
        exposure=b.exposure(mismatch, cohort, b.full),
        restrictions=[IR.EXPORTED_RATE_UNTRUSTED] if warn else [],
    )]


def _dq15(b: _Builder) -> _Result:
    v = b.v
    base = v.ok[C.DEMAND_REQUESTS]
    if not base.any() or b.total_requests <= 0:
        return RuleOutcome.NOT_TESTABLE, []
    issues, notice_facts = [], []
    gap_threshold = b.threshold("weighting_gap_points", M.ROW_SHARE, Unit.FRACTION)
    for column, template, tag in (
        (C.DEMAND_RESPONSES, "DQ15_weighting_gap_zero_responses", "dq15_zero_responses"),
        (C.REVENUE, "DQ15_weighting_gap_zero_revenue", "dq15_zero_revenue"),
    ):
        mask = base & v.ok[column] & (np.nan_to_num(v.num[column]) == 0)
        cohort = b.cohort(tag)
        row_share = Decimal(int(mask.sum())) / Decimal(int(base.sum()))
        request_share = v.dsum(C.DEMAND_REQUESTS, mask) / b.total_requests
        facts = [
            b.fact(M.ROW_SHARE, row_share, Unit.FRACTION, cohort, b.full, [column], mask,
                   Weighting.NOT_APPLICABLE),
            b.fact(M.REQUEST_SHARE, request_share, Unit.FRACTION, cohort, b.full,
                   [column, C.DEMAND_REQUESTS], mask, Weighting.RATIO_OF_SUMS),
        ]
        notice_facts += facts
        if abs(row_share - request_share) >= b.config.weighting_gap_points:
            issues.append(b.issue(
                R.DQ15, Severity.WARNING, template, [b.account.segment_id], [b.full], facts,
                [gap_threshold], rows=int(mask.sum()),
                exposure=b.exposure(mask, cohort, b.full),
            ))
    outcome = _triggered(issues)
    issues.append(b.issue(R.DQ15, Severity.NOTICE, "DQ15_weighting_notice",
                          [b.account.segment_id], [b.full], notice_facts))
    return outcome, issues


def _granularity(values: np.ndarray, coverage: float) -> tuple[int, float]:
    """Largest divisor shared by at least ``coverage`` of ``values``.

    Starts from the GCD of all values, then greedily extends it by primes
    below 1000 while the dominant share still meets ``coverage``.
    """
    divisor = int(np.gcd.reduce(values))
    for prime in _PRIMES:
        while True:
            trial = divisor * prime
            if trial > values.max():
                break
            share = float(np.mean(values % trial == 0))
            if share < coverage:
                break
            divisor = trial
    return divisor, float(np.mean(values % divisor == 0))


_PRIMES = tuple(p for p in range(2, 1000) if all(p % q for q in range(2, int(p ** 0.5) + 1)))


def _dq16(b: _Builder) -> _Result:
    v = b.v
    coverage = float(b.config.granularity_coverage)
    found: dict[InputColumn, tuple[int, float, np.ndarray]] = {}
    for column in COUNTER_COLUMNS:
        values = np.nan_to_num(v.num[column])
        mask = v.ok[column] & (values > 0) & (values == np.floor(values))
        if mask.sum() >= b.config.granularity_min_rows:
            divisor, share = _granularity(values[mask].astype(np.int64), coverage)
            found[column] = (divisor, share, mask)
    if C.DEMAND_REQUESTS not in found:
        return RuleOutcome.NOT_TESTABLE, []

    def facts(column: InputColumn) -> list[str]:
        divisor, share, mask = found[column]
        return [
            b.fact(M.COUNTER_GRANULARITY, Decimal(divisor), Unit.COUNT, b.account, b.full,
                   [column], mask, Weighting.NOT_APPLICABLE),
            b.fact(M.GRANULARITY_COVERAGE, _fd(share), Unit.FRACTION, b.account, b.full,
                   [column], mask, Weighting.NOT_APPLICABLE),
        ]

    thresholds = [b.threshold("granularity_coverage", M.GRANULARITY_COVERAGE, Unit.FRACTION),
                  b.threshold("granularity_min_rows", M.ROW_COUNT, Unit.COUNT)]
    request_divisor = found[C.DEMAND_REQUESTS][0]
    different = [c for c in found if c is not C.DEMAND_REQUESTS and found[c][0] != request_divisor]
    mask = found[C.DEMAND_REQUESTS][2]
    if request_divisor > 1:
        measured = facts(C.DEMAND_REQUESTS) + [f for c in different for f in facts(c)]
        template = "DQ16_counter_granularity"
    elif any(found[c][0] > 1 for c in different):
        measured = [f for c in different if found[c][0] > 1 for f in facts(c)]
        measured += facts(C.DEMAND_REQUESTS)
        template = "DQ16_mixed_granularity"
    else:
        return RuleOutcome.NOT_TRIGGERED, []
    return RuleOutcome.TRIGGERED, [b.issue(
        R.DQ16, Severity.WARNING, template, [b.account.segment_id], [b.full], measured,
        thresholds, rows=int(mask.sum()), exposure=b.exposure(mask, b.account, b.full),
    )]


def _dq17(b: _Builder) -> _Result:
    v = b.v
    responses = np.nan_to_num(v.num[C.DEMAND_RESPONSES])
    bids = np.nan_to_num(v.num[C.DEMAND_BIDPRICE])
    floors = np.nan_to_num(v.num[C.OUR_BIDFLOOR])
    positive = v.ok[C.DEMAND_RESPONSES] & v.ok[C.DEMAND_BIDPRICE] & (responses > 0)
    floor_rows = v.ok[C.OUR_BIDFLOOR] & v.ok[C.DEMAND_REQUESTS]
    notice_facts = []
    if floor_rows.any():
        zero_floor = floor_rows & (floors == 0)
        notice_facts.append(b.fact(
            M.ZERO_FLOOR_REQUEST_SHARE,
            _share(v.dsum(C.DEMAND_REQUESTS, zero_floor), v.dsum(C.DEMAND_REQUESTS, floor_rows)),
            Unit.FRACTION, b.account, b.full, [C.OUR_BIDFLOOR, C.DEMAND_REQUESTS], floor_rows,
            Weighting.REQUEST_WEIGHTED))
    issues = []
    outcome = RuleOutcome.NOT_TESTABLE
    total = v.dsum(C.DEMAND_RESPONSES, positive)
    if total > 0:
        zero_bid = positive & (bids == 0)
        below = positive & v.ok[C.OUR_BIDFLOOR] & (bids > 0) & (bids < floors)
        checks = (
            (zero_bid, M.ZERO_BID_RESPONSE_SHARE, "zero_bid_response_share",
             "DQ17_zero_bid_responses", "dq17_zero_bid", True),
            (below, M.BID_BELOW_FLOOR_RESPONSE_SHARE, "bid_below_floor_response_share",
             "DQ17_bid_below_floor", "dq17_bid_below_floor", False),
        )
        for mask, metric, name, template, tag, notice_when_any in checks:
            share = v.dsum(C.DEMAND_RESPONSES, mask) / total
            fact = b.fact(metric, share, Unit.FRACTION, b.account, b.full,
                          [C.DEMAND_BIDPRICE, C.OUR_BIDFLOOR, C.DEMAND_RESPONSES], positive,
                          Weighting.RESPONSE_WEIGHTED)
            notice_facts.append(fact)
            warn = share >= getattr(b.config, name)
            if not (warn or (notice_when_any and mask.any())):
                continue
            cohort = b.cohort(tag)
            issues.append(b.issue(
                R.DQ17, Severity.WARNING if warn else Severity.NOTICE, template,
                [b.account.segment_id], [b.full], [fact],
                [b.threshold(name, metric, Unit.FRACTION)], rows=int(mask.sum()),
                exposure=b.exposure(mask, cohort, b.full),
                restrictions=[IR.NO_FLOOR_CAUSAL_CLAIM],
            ))
        outcome = _triggered(issues)
    issues.append(b.issue(R.DQ17, Severity.NOTICE, "DQ17_causal_limit", [b.account.segment_id],
                          [b.full], notice_facts, restrictions=[IR.NO_FLOOR_CAUSAL_CLAIM]))
    return outcome, issues


def _dq18(b: _Builder) -> _Result:
    v, issues = b.v, []
    negative = {c: v.ok[c] & (np.nan_to_num(v.num[c]) < 0) for c in (C.REVENUE, C.COST)}
    mask = negative[C.REVENUE] | negative[C.COST]
    if mask.any():
        cohort = b.cohort("dq18_negative_money")
        segments, per_segment = b.affected(mask, "dq18_negative_money")
        measured = [
            b.money(C.REVENUE, negative[C.REVENUE], b.cohort("dq18_negative_revenue"), b.full),
            b.money(C.COST, negative[C.COST], b.cohort("dq18_negative_cost"), b.full),
            b.count(mask, cohort, b.full, [C.REVENUE, C.COST]),
            *per_segment,
        ]
        issues.append(b.issue(
            R.DQ18, Severity.WARNING, "DQ18_negative_money", segments, [b.full], measured,
            rows=int(mask.sum()), exposure=b.exposure(mask, cohort, b.full),
            restrictions=[IR.NO_YIELD_ESTIMATE, IR.NO_TREND_CLAIM],
        ))
    zero = (v.ok[C.SUPPLY_IMPRESSIONS] & v.ok[C.REVENUE]
            & (np.nan_to_num(v.num[C.SUPPLY_IMPRESSIONS]) > 0)
            & (np.nan_to_num(v.num[C.REVENUE]) == 0))
    if zero.any():
        cohort = b.cohort("dq18_zero_revenue")
        segments, per_segment = b.affected(zero, "dq18_zero_revenue")
        measured = [
            b.fact(M.IMPRESSION_SHARE,
                   _share(v.dsum(C.SUPPLY_IMPRESSIONS, zero), b.total_impressions),
                   Unit.FRACTION, cohort, b.full, [C.SUPPLY_IMPRESSIONS, C.REVENUE], zero,
                   Weighting.RATIO_OF_SUMS),
            b.count(zero, cohort, b.full, [C.SUPPLY_IMPRESSIONS, C.REVENUE]),
            *per_segment,
        ]
        issues.append(b.issue(
            R.DQ18, Severity.NOTICE, "DQ18_zero_revenue_rounding", segments, [b.full], measured,
            rows=int(zero.sum()), exposure=b.exposure(zero, cohort, b.full),
        ))
    return _triggered(issues), issues


def _dq20_notice(b: _Builder) -> _Result:
    return RuleOutcome.NOT_TRIGGERED, [b.issue(
        R.DQ20, Severity.NOTICE, "DQ20_observational_limits", [b.account.segment_id], [b.full],
        restrictions=[IR.NO_FLOOR_CAUSAL_CLAIM],
    )]


# --- Second pass -----------------------------------------------------------


def refine_quality(
    data: LoadedData,
    metrics: MetricsStore,
    candidates: list[Candidate],
    initial: QualityReport,
    config: QualityConfig,
) -> QualityReport:
    """Second pass over every preliminary candidate before truncation.

    Adds concentration, coverage, peer and threshold-sensitivity issues; it
    never removes a first-pass issue.

    Candidate scope is resolved through ``metrics.segments``/``metrics.periods``
    and measured directly from the loaded rows. Parent segments are the
    observed segments in ``metrics`` whose filters are a strict, non-empty
    subset of a candidate's. ``metrics.quality_flags`` (keyed by candidate or
    segment ID) may carry ``RANK_SENSITIVE_FLAG`` and ``PEER_KEYS_DIFFER_FLAG``
    for detector-side checks this module cannot recompute.
    """
    if metrics.run_namespace != initial.run_namespace:
        raise ValueError("metrics belong to another run namespace")
    view = _View(data)
    coverage = _DayCoverage.compute(view, config)
    b = _Builder(initial.run_namespace, view, config, coverage)
    cfg_hash = config_hash(config)
    if cfg_hash != initial.config_hash:
        raise ValueError("refine_quality needs the configuration of the initial pass")
    initial_outcomes = {r.rule_id: r.outcome for r in initial.rule_results}

    targets: dict[tuple[str, str], list[str]] = {}
    parents: dict[tuple[str, str], list[str]] = {}
    # A five-key slice has at most 30 nonempty proper parents. Looking each
    # one up avoids scanning every observed slice for every candidate.
    by_filters = {
        (s.cohort_id, frozenset(s.filters.items())): s
        for s in metrics.segments.values()
    }
    for candidate in candidates:
        seg, per = _scope(metrics, candidate)
        targets.setdefault((seg.segment_id, per.period_id), []).append(candidate.candidate_id)
        for size in range(1, len(seg.filters)):
            for subset in combinations(seg.filters.items(), size):
                other = by_filters.get((seg.cohort_id, frozenset(subset)))
                if other is not None:
                    parents.setdefault((other.segment_id, per.period_id), []).append(
                        candidate.candidate_id)

    outcomes: dict[QualityRuleId, list[RuleOutcome]] = {r: [] for r in R}
    issues: dict[QualityRuleId, list[QualityIssue]] = {r: [] for r in R}

    def record(rule: QualityRuleId, result: _Result) -> None:
        outcomes[rule].append(result[0])
        issues[rule].extend(result[1])

    by_id = {c.candidate_id: c for c in candidates}
    for (segment_id, period_id), ids in targets.items():
        seg, per = metrics.segments[segment_id], metrics.periods[period_id]
        mask = view.matching(seg.filters, per.included_dates)
        concentration = _dq08(b, seg, per, mask, ids)
        record(R.DQ07, _dq07(b, seg, per, mask, ids))
        record(R.DQ08, concentration)
        record(R.DQ09, _dq09(b, seg, per, mask, ids, yield_steps=True))
        for candidate_id in ids:
            candidate = by_id[candidate_id]
            flags = (*metrics.quality_flags.get(candidate_id, ()),
                     *metrics.quality_flags.get(segment_id, ()))
            record(R.DQ19, _dq19(b, candidate, seg, per, mask, bool(concentration[1]), flags))
            record(R.DQ20, _dq20_peer(b, candidate, seg, per, flags))
    for (segment_id, period_id), ids in parents.items():
        if (segment_id, period_id) in targets:
            continue
        seg, per = metrics.segments[segment_id], metrics.periods[period_id]
        mask = view.matching(seg.filters, per.included_dates)
        record(R.DQ08, _dq08(b, seg, per, mask, ids))
        record(R.DQ09, _dq09(b, seg, per, mask, ids, yield_steps=True))

    results: dict[QualityRuleId, _Result] = {}
    for rule in R:
        prior = initial_outcomes.get(rule, RuleOutcome.NOT_TESTABLE)
        if rule in _REFINE_ONLY:
            outcome = _worst(outcomes[rule], RuleOutcome.NOT_TRIGGERED)
        else:
            outcome = _worst([prior, *outcomes[rule]], prior)
        prior_issue_ids = next((r.issue_ids for r in initial.rule_results if r.rule_id == rule),
                               [])
        prior_issues = [i for i in initial.issues if i.issue_id in set(prior_issue_ids)]
        results[rule] = (outcome, [*prior_issues, *issues[rule]])
    return _report(b, cfg_hash, QualityPass.REFINED, results, prior=initial)


def _scope(metrics: MetricsStore, candidate: Candidate) -> tuple[Segment, Period]:
    try:
        return metrics.segments[candidate.segment_id], metrics.periods[candidate.period_id]
    except KeyError as error:
        raise ValueError(f"candidate {candidate.candidate_id} references unknown scope "
                         f"{error.args[0]}") from None


def _dq07(b: _Builder, seg: Segment, per: Period, mask: np.ndarray,
          candidates: Sequence[str]) -> _Result:
    reference = [d for d in b.coverage.observed if d in set(per.included_dates)]
    observed = b.v.days_of(mask)
    revenue = b.v.daily(C.REVENUE, mask)
    positive_days = [d for d, x in revenue.items() if x > 0]
    absent = [d for d in reference if d not in set(observed)]
    if not (len(observed) < b.config.min_stable_days
            or len(positive_days) < b.config.min_stable_days or absent):
        return RuleOutcome.NOT_TRIGGERED, []
    measured = [
        b.fact(M.OBSERVED_DAYS, Decimal(len(observed)), Unit.DAYS, seg, per, [_DAY], mask),
        b.fact(M.OBSERVED_DAYS, Decimal(len(reference)), Unit.DAYS, b.account, per, [_DAY],
               b.v.matching({}, per.included_dates)),
        b.fact(M.REVENUE_POSITIVE_DAYS, Decimal(len(positive_days)), Unit.DAYS, seg, per,
               [_DAY, C.REVENUE], mask),
        b.fact(M.MISSING_DAYS, Decimal(len(absent)), Unit.DAYS, seg, per, [_DAY], mask),
    ]
    return RuleOutcome.TRIGGERED, [b.issue(
        R.DQ07, Severity.WARNING, "DQ07_intermittent_coverage", [seg.segment_id], [per],
        measured, [b.threshold("min_stable_days", M.OBSERVED_DAYS, Unit.DAYS)],
        rows=int(mask.sum()), exposure=b.exposure(mask, seg, per),
        restrictions=[IR.NO_STABLE_SCALE_CLAIM, IR.NO_TREND_CLAIM], candidates=candidates,
    )]


def _eligible(b: _Builder, family: DetectorFamily, mask: np.ndarray, scale: Decimal) -> bool:
    config, v = b.config, b.v
    requests = v.dsum(C.DEMAND_REQUESTS, mask)
    revenue = v.dsum(C.REVENUE, mask)
    days = v.days_of(mask)
    if len(days) < config.min_stable_days:
        return False
    if family is DetectorFamily.SCALE_WINNER:
        positive = [d for d, x in v.daily(C.REVENUE, mask).items() if x > 0]
        if len(positive) < config.min_stable_days:
            return False
    if family is DetectorFamily.WASTED_REQUESTS:
        return requests >= scale * config.materiality_waste_min_requests
    if family is DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY:
        return (v.dsum(C.DEMAND_WINS, mask) >= scale * config.materiality_wins_min_wins
                and revenue >= scale * config.materiality_min_revenue)
    return (requests >= scale * config.materiality_min_requests
            and revenue >= scale * config.materiality_min_revenue)


def _dq19(b: _Builder, candidate: Candidate, seg: Segment, per: Period, mask: np.ndarray,
          concentrated: bool, flags: Sequence[str]) -> _Result:
    family = candidate.family
    base = _eligible(b, family, mask, Decimal(1))
    templates = []
    if not base or candidate.eligibility is not EligibilityStatus.ELIGIBLE:
        templates.append("DQ19_below_materiality")
    multipliers = b.config.sensitivity_multipliers
    if any(_eligible(b, family, mask, Decimal(m)) != base for m in multipliers):
        templates.append("DQ19_threshold_sensitive")
    flagged = b.coverage.flagged & set(per.included_dates)
    if concentrated or (flagged and _eligible(
            b, family, mask & ~b.v.matching({}, flagged), Decimal(1)) != base):
        templates.append("DQ19_flagged_day_dependent")
    if RANK_SENSITIVE_FLAG in flags:
        templates.append("DQ19_rank_sensitive")
    if not templates:
        return RuleOutcome.NOT_TRIGGERED, []
    measured = [
        b.fact(M.REQUESTS, b.v.dsum(C.DEMAND_REQUESTS, mask), Unit.COUNT, seg, per,
               [C.DEMAND_REQUESTS], mask),
        b.fact(M.REVENUE, b.v.dsum(C.REVENUE, mask), Unit.EXPORT_DOLLAR, seg, per, [C.REVENUE],
               mask),
        b.fact(M.OBSERVED_DAYS, Decimal(len(b.v.days_of(mask))), Unit.DAYS, seg, per, [_DAY],
               mask),
    ]
    thresholds = [b.threshold("min_stable_days", M.OBSERVED_DAYS, Unit.DAYS)]
    if family is DetectorFamily.WASTED_REQUESTS:
        thresholds.append(b.threshold("materiality_waste_min_requests", M.REQUESTS, Unit.COUNT))
    elif family is DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY:
        measured.append(b.fact(M.WINS, b.v.dsum(C.DEMAND_WINS, mask), Unit.COUNT, seg, per,
                               [C.DEMAND_WINS], mask))
        thresholds += [b.threshold("materiality_wins_min_wins", M.WINS, Unit.COUNT),
                       b.threshold("materiality_min_revenue", M.REVENUE, Unit.EXPORT_DOLLAR)]
    else:
        thresholds += [b.threshold("materiality_min_requests", M.REQUESTS, Unit.COUNT),
                       b.threshold("materiality_min_revenue", M.REVENUE, Unit.EXPORT_DOLLAR)]
    exposure = b.exposure(mask, seg, per)
    return RuleOutcome.TRIGGERED, [
        b.issue(R.DQ19, Severity.WARNING, template, [seg.segment_id], [per], measured,
                thresholds, rows=int(mask.sum()), exposure=exposure,
                restrictions=[IR.NO_STABLE_SCALE_CLAIM], candidates=[candidate.candidate_id])
        for template in templates
    ]


def _dq20_peer(b: _Builder, candidate: Candidate, seg: Segment, per: Period,
               flags: Sequence[str]) -> _Result:
    if PEER_KEYS_DIFFER_FLAG not in flags:
        return RuleOutcome.NOT_TRIGGERED, []
    return RuleOutcome.TRIGGERED, [b.issue(
        R.DQ20, Severity.WARNING, "DQ20_unmatched_peer", [seg.segment_id], [per],
        restrictions=[IR.NO_MATCHED_PEER_LABEL], candidates=[candidate.candidate_id],
    )]
