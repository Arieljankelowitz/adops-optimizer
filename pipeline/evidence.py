"""Namespace-bound evidence, immutable registries and bounded model payloads.

The full private bundle retains provenance. ``serialize_pack`` is the compact
model-facing view: no rows, no source-line handles, and labels are JSON data,
never interpolated instructions. Exact tokenizer counting also happens at the
transport boundary. A pack that cannot retain its warnings/counter-evidence
within budget is refused rather than silently weakening the evidence.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from types import MappingProxyType

from contracts.config import EvidenceConfig
from contracts.enums import (
    AssumptionSelector,
    BudgetReason,
    Decision,
    DetectorFamily,
    EligibilityStatus,
    FactKind,
    InferenceRestriction,
    MetricKey,
    Relation,
    ScenarioTemplate,
    ToolName,
    Unit,
    ValidityStatus,
)
from contracts.ids import IdPrefix, canonical_json, make_id
from contracts.models import (
    AnswerGuide,
    Assumption,
    AssumptionBounds,
    Candidate,
    CandidatePack,
    Fact,
    ObservationStatement,
    Period,
    QualityIssue,
    QualityIssueSummary,
    QualityReport,
    Scenario,
    ScenarioLabels,
    ScenarioRequest,
    Segment,
    TestRequirements,
)
from contracts.runtime import (
    AssumptionRegistry,
    EvidenceBundle,
    FactRegistry,
    MetricsStore,
    ScenarioRegistry,
)
from contracts.templates import ObservationTemplateId as Template
from contracts.templates import TestTemplateId as Test
from pipeline.detectors import (
    DIMENSION_BLOCKERS,
    FAMILY_BLOCKERS,
    candidate_evidence,
    opportunity_score,
)
from pipeline.metrics import (
    ADDITIVE,
    FUNNEL_METRICS,
    MONEY_RATE_METRICS,
    FactBuilder,
    IssueIndex,
    segments_overlap,
    stats_row,
)
from pipeline.scenarios import PARAMETERS, REQUIRED_METRICS, evaluate_scenarios
from pipeline.shortlist import Lead, collapse, select, total_signature

TESTS = {
    DetectorFamily.SCALE_WINNER: Test.MATCHED_ALLOCATION_HOLDOUT,
    DetectorFamily.WASTED_REQUESTS: Test.TARGETED_THROTTLE_HOLDOUT,
    DetectorFamily.MARGIN_LEAK: Test.COMMERCIAL_TERMS_REVIEW,
    DetectorFamily.FLOOR_HIGH_BID: Test.FLOOR_INCREASE_RANDOMIZED,
    DetectorFamily.FLOOR_LOW_BID: Test.FLOOR_DECREASE_RANDOMIZED,
    DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: Test.COUNTER_RECONCILIATION,
    DetectorFamily.TREND: Test.INSTRUMENTATION_FEASIBILITY,
    DetectorFamily.COMBINATION_OUTLIER: Test.MATCHED_ALLOCATION_HOLDOUT,
}
SCENARIOS = {
    DetectorFamily.SCALE_WINNER: ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS,
    DetectorFamily.WASTED_REQUESTS: ScenarioTemplate.THROTTLE,
    DetectorFamily.MARGIN_LEAK: ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY,
}


CONCENTRATION_METRICS = frozenset(
    {
        MetricKey.MAX_DAY_REVENUE_SHARE,
        MetricKey.TOP_TWO_DAY_REVENUE_SHARE,
        MetricKey.MAX_DAY_PROFIT_SHARE,
        MetricKey.TOP_TWO_DAY_PROFIT_SHARE,
    }
)
# Planning parameters a ``recommend_test`` must label (checked by the validator).
TEST_PARAMETERS = ("test_allocation", "test_min_days", "test_profit_stop_loss")
# Guardrails a ``recommend_test`` must name as success metrics.
TEST_SUCCESS_METRICS = (MetricKey.PROFIT, MetricKey.PROFIT_PER_MILLION_REQUESTS)
# Restrictions the validator applies to the statements of any candidate.
OBSERVATION_RESTRICTIONS = frozenset(
    {
        InferenceRestriction.NO_FUNNEL_INFERENCE,
        InferenceRestriction.NO_YIELD_ESTIMATE,
        InferenceRestriction.NO_TREND_CLAIM,
        InferenceRestriction.NO_MATCHED_PEER_LABEL,
    }
)
FLOOR_FAMILIES = frozenset({DetectorFamily.FLOOR_HIGH_BID, DetectorFamily.FLOOR_LOW_BID})
# Statements offered to the model per candidate; the contract caps the list.
OBSERVATION_OPTION_LIMIT = 24
# Issues of one rule listed with their facts per candidate; repeats of the same
# rule on other overlapping slices add no new restriction, only tokens.
DETAILED_ISSUES_PER_RULE = 2
# Affected slices a listed issue shows (the candidate's own first). A row-level
# rule on a full-size export names up to ``max_issue_segments`` slices; the
# restriction is the same for all of them, so the rest only cost tokens.
ISSUE_SEGMENTS_SHOWN = 4


class EvidenceBudgetError(ValueError):
    """Mandatory evidence cannot fit; no unsafe partial pack was emitted."""


def permitted_tests(candidate: Candidate, facts: Mapping[str, Fact]) -> list[Test]:
    """The family's controlled test, then the always-available investigation.

    A combination outlier can sit above or below its baselines. Below them,
    sending it more traffic is the wrong test, so the low-yield throttle test
    applies; with baselines on both sides only an investigation does.
    """
    test = TESTS[candidate.family]
    if candidate.family is DetectorFamily.COMBINATION_OUTLIER:
        relations = {
            facts[r].comparison.relation
            for r in candidate.trigger_fact_ids
            if r in facts and facts[r].comparison is not None
        }
        if relations == {Relation.LESS}:
            test = Test.TARGETED_THROTTLE_HOLDOUT
        elif relations != {Relation.GREATER}:
            return [Test.INSTRUMENTATION_FEASIBILITY]
    return [test, Test.INSTRUMENTATION_FEASIBILITY]


def claim_restrictions(
    candidate: Candidate, segment: Segment
) -> frozenset[InferenceRestriction]:
    """Restrictions that can change what may be claimed or decided for ``candidate``.

    The decision blockers of its family and of the dimensions it fixes, the
    statement-level restrictions every observation is checked against, and
    the export caveats. A quality issue carrying none of these is context
    rather than a constraint for this candidate: its pack summarises it in one
    line instead of listing it with its facts.
    """
    out = set(FAMILY_BLOCKERS[candidate.family]) | OBSERVATION_RESTRICTIONS
    out.add(InferenceRestriction.EXPORTED_RATE_UNTRUSTED)
    if candidate.family in FLOOR_FAMILIES:
        out.add(InferenceRestriction.NO_FLOOR_CAUSAL_CLAIM)
    out.update(r for r, d in DIMENSION_BLOCKERS.items() if d in segment.filters)
    return frozenset(out)


def scoped_issues(
    issues: Iterable[QualityIssue],
    segments: Mapping[str, Segment],
    periods: Mapping[str, Period],
    segment_id: str,
    period_id: str,
) -> list[QualityIssue]:
    """Issues on traffic and dates that overlap the scope; unknown scopes apply.

    The validator derives a candidate's restrictions from exactly this set.
    """
    segment, period = segments.get(segment_id), periods.get(period_id)
    out = []
    for issue in issues:
        touches = not issue.segment_ids or any(
            sid not in segments
            or segment is None
            or segments_overlap(segment.filters, segments[sid].filters)
            for sid in issue.segment_ids
        )
        dates = not issue.period_ids or any(
            pid not in periods
            or period is None
            or bool(set(period.included_dates) & set(periods[pid].included_dates))
            for pid in issue.period_ids
        )
        if touches and dates:
            out.append(issue)
    return out


def split_issues(
    candidate: Candidate,
    segment: Segment,
    issues: Iterable[QualityIssue],
    segments: Mapping[str, Segment],
    facts: Mapping[str, Fact] | None = None,
) -> tuple[list[QualityIssue], list[QualityIssue]]:
    """(listed in full, summarised) for one candidate's pack.

    Issues whose restrictions bear on this candidate's claims go in with their
    facts, at most ``DETAILED_ISSUES_PER_RULE`` per rule; the rest become one
    summary line. Every restriction still reaches the model through the
    answer guide, and the validator still applies all of them.
    """
    relevant = claim_restrictions(candidate, segment)
    per_rule: Counter = Counter()
    detailed, summarised = [], []
    # The candidate's own slice first, then the narrowest issue scopes.
    for issue in sorted(
        issues,
        key=lambda i: (
            candidate.segment_id not in i.segment_ids,
            len(i.segment_ids) or len(segments),
            i.issue_id,
        ),
    ):
        if (
            relevant & set(issue.inference_restrictions)
            and per_rule[issue.rule_id] < DETAILED_ISSUES_PER_RULE
        ):
            per_rule[issue.rule_id] += 1
            detailed.append(issue_view(issue, segment, segments, facts))
        else:
            summarised.append(issue)
    detailed.sort(key=lambda i: i.issue_id)
    return detailed, summarised


def issue_fact_ids(issues: Iterable[QualityIssue]) -> list[str]:
    """Every fact an issue row references; a pack listing the issue must carry them."""
    refs: list[str] = []
    for issue in issues:
        refs.extend((*issue.measured_fact_ids, *issue.threshold_fact_ids))
        refs.extend(
            r
            for r in (issue.request_exposure_fact_id, issue.revenue_exposure_fact_id)
            if r
        )
    return refs


def issue_view(
    issue: QualityIssue,
    segment: Segment,
    segments: Mapping[str, Segment],
    facts: Mapping[str, Fact] | None = None,
) -> QualityIssue:
    """One candidate's view of an issue: a few of the affected scopes it overlaps.

    At most ``ISSUE_SEGMENTS_SHOWN`` slices, the candidate's own first, and
    with ``facts`` only the per-slice measurements of the slices shown. The
    full issue (every affected segment, fact and candidate) stays in the
    bundle; the validator and the report read that, never the pack.
    """
    # Unknown scopes touch conservatively, as in ``IssueIndex``, so they stay.
    touching = [
        sid
        for sid in issue.segment_ids
        if sid not in segments
        or segments_overlap(segments[sid].filters, segment.filters)
    ]
    shown = sorted(touching, key=lambda sid: sid != segment.segment_id)
    shown = set(shown[:ISSUE_SEGMENTS_SHOWN])
    touching = [sid for sid in touching if sid in shown]
    measured = list(issue.measured_fact_ids)
    if facts is not None and touching:
        # Per-slice measurements of slices not shown (a row-level rule keeps
        # one per affected slice); totals and unknown refs stay.
        hidden = [
            segments[sid].filters
            for sid in issue.segment_ids
            if sid in segments and sid not in shown
        ]
        measured = [
            r for r in measured if r not in facts or facts[r].segment.filters not in hidden
        ]
    return issue.model_copy(
        update={
            # An empty list means "every segment"; never narrow to that.
            "segment_ids": touching or issue.segment_ids,
            "measured_fact_ids": measured,
            "affected_candidate_ids": [],
        },
        deep=True,
    )


def observation_options(
    candidate: Candidate,
    period: Period,
    facts: Mapping[str, Fact],
    restrictions: Iterable[InferenceRestriction],
    excluded: Iterable[str] = (),
    limit: int = OBSERVATION_OPTION_LIMIT,
) -> list[ObservationStatement]:
    """Observation statements the strict validator accepts for ``candidate``.

    Mirrors ``llm.validator`` role by role (scope, usable evidence, metric and
    trend restrictions, comparison roles), so the model selects evidence
    instead of assembling ordered fact roles. Trigger and peer evidence come
    first. Quality and scenario facts are never offered as observations.
    """
    restrictions = set(restrictions)
    excluded = set(excluded)

    def usable(f: Fact | None) -> bool:
        return (
            f is not None
            and f.kind not in (FactKind.SCENARIO, FactKind.QUALITY)
            and f.fact_id not in excluded
            and f.value is not None
            and f.validity.status is ValidityStatus.VALID
            and not (
                InferenceRestriction.NO_FUNNEL_INFERENCE in restrictions
                and f.metric in FUNNEL_METRICS
            )
            and not (
                InferenceRestriction.NO_YIELD_ESTIMATE in restrictions
                and f.metric in MONEY_RATE_METRICS
            )
        )

    def own(f: Fact) -> bool:
        return (f.segment.segment_id, f.period.period_id) == (
            candidate.segment_id,
            candidate.period_id,
        )

    rank = {
        ref: n
        for n, ref in enumerate(
            (*candidate.trigger_fact_ids, *candidate.peer_fact_ids)
        )
    }
    ordered = sorted(
        (f for f in facts.values() if usable(f)),
        key=lambda f: (rank.get(f.fact_id, len(rank)), f.fact_id),
    )
    out: list[ObservationStatement] = []

    def add(template: Template, *refs: str) -> None:
        statement = ObservationStatement(template_id=template, fact_ids=list(refs))
        if statement not in out:
            out.append(statement)

    for f in ordered:
        c = f.comparison
        if f.kind is not FactKind.COMPARISON or c is None:
            continue
        target, baseline = facts.get(c.left_fact_id), facts.get(c.right_fact_id)
        if (
            usable(target)
            and usable(baseline)
            and own(target)
            and f.segment == target.segment
            and f.period == target.period == baseline.period
            and f.provenance.parent_fact_ids == [target.fact_id, baseline.fact_id]
            and (
                baseline.segment.cohort_id == target.segment.cohort_id
                or baseline.segment.cohort_id == "excluding_" + candidate.segment_id
            )
            and not (
                InferenceRestriction.NO_MATCHED_PEER_LABEL in restrictions
                and c.matching_keys
            )
        ):
            add(Template.METRIC_VS_BASELINE, target.fact_id, baseline.fact_id, f.fact_id)
    for f in ordered:
        if f.fact_id in rank and own(f) and f.kind is not FactKind.COMPARISON:
            add(Template.METRIC_VALUE, f.fact_id)
    for f in ordered:
        if own(f) and f.metric in CONCENTRATION_METRICS:
            add(Template.CONCENTRATION, f.fact_id)
        if own(f) and f.metric is MetricKey.OBSERVED_DAYS:
            for reference in ordered:
                if (
                    reference.metric is MetricKey.OBSERVED_DAYS
                    and reference.period.period_id == candidate.period_id
                    and not reference.segment.filters
                    and reference.segment.cohort_id == f.segment.cohort_id
                ):
                    add(Template.COVERAGE, f.fact_id, reference.fact_id)
    if InferenceRestriction.NO_TREND_CLAIM not in restrictions:
        dates = set(period.included_dates)
        for change in ordered:
            parents = change.provenance.parent_fact_ids
            if change.metric is not MetricKey.DAILY_CHANGE or len(parents) != 2:
                continue
            earlier, later = facts.get(parents[0]), facts.get(parents[1])
            scoped = [
                x
                for x in (earlier, later, change)
                if usable(x)
                and x.segment.segment_id == candidate.segment_id
                and set(x.period.included_dates) <= dates
            ]
            if (
                len(scoped) == 3
                and earlier.metric is later.metric
                and earlier.unit is later.unit
                and earlier.period.end < later.period.start
                and earlier.value > 0
                and set(change.period.included_dates)
                == set(earlier.period.included_dates + later.period.included_dates)
                and change.value == (later.value - earlier.value) / earlier.value
            ):
                add(Template.TREND, earlier.fact_id, later.fact_id, change.fact_id)
    for f in sorted(
        (f for f in ordered if own(f) and f.kind is not FactKind.COMPARISON),
        key=lambda f: (list(MetricKey).index(f.metric), f.fact_id),
    ):
        add(Template.METRIC_VALUE, f.fact_id)
    return out[:limit]


def answer_guide(
    candidate: Candidate,
    segment: Segment,
    period: Period,
    issues: list[QualityIssue],
    summarised: list[QualityIssue],
    facts: Mapping[str, Fact],
    assumptions: list[Assumption],
    scenarios: list[Scenario],
    permitted_tests: list[Test],
) -> AnswerGuide:
    """The answer form the validator will accept for ``candidate``, computed by code.

    ``issues`` are all quality issues touching the candidate (the validator's
    restriction scope); ``summarised`` the subset its pack only summarises.
    Merge is never offered: an exact-scope duplicate is already rejected by
    the detectors.
    """
    restrictions = sorted({r for i in issues for r in i.inference_restrictions})
    options = observation_options(
        candidate,
        period,
        facts,
        restrictions,
        {r for i in issues for r in i.threshold_fact_ids},
    )
    blocked = (
        InferenceRestriction.NO_RECOMMENDATIONS in restrictions
        or candidate.eligibility is EligibilityStatus.REJECTED
        or not options
    )
    test_blocked = (
        candidate.eligibility is EligibilityStatus.EXPLORATORY
        or bool(set(restrictions) & FAMILY_BLOCKERS[candidate.family])
        or any(
            r in restrictions and d in segment.filters
            for r, d in DIMENSION_BLOCKERS.items()
        )
    )
    test_assumptions = {
        a.parameter_key: a.assumption_id
        for a in assumptions
        if a.parameter_key in TEST_PARAMETERS
    }
    test = (
        TestRequirements(
            test_template_ids=permitted_tests,
            assumption_ids=[test_assumptions[k] for k in TEST_PARAMETERS],
            success_metric_keys=list(TEST_SUCCESS_METRICS),
        )
        if not (blocked or test_blocked)
        and permitted_tests
        and set(test_assumptions) == set(TEST_PARAMETERS)
        else None
    )
    decisions = (
        [Decision.REJECT]
        if blocked
        else [
            *([Decision.RECOMMEND_TEST] if test else []),
            Decision.INVESTIGATE_FIRST,
            Decision.REJECT,
        ]
    )
    groups = Counter(
        (i.rule_id, i.severity, tuple(sorted(i.inference_restrictions)))
        for i in summarised
    )
    return AnswerGuide(
        effective_restrictions=restrictions,
        allowed_decisions=decisions,
        observation_options=options,
        recommend_test=test,
        scenario_labels=[
            ScenarioLabels(
                scenario_id=s.scenario_id,
                assumption_ids=s.assumption_ids,
                prerequisite_codes=s.prerequisite_codes,
                risk_codes=s.limitation_codes,
            )
            for s in scenarios
        ],
        other_quality_issues=[
            QualityIssueSummary(
                rule_id=rule,
                severity=severity,
                issue_count=n,
                inference_restrictions=list(restriction_set),
            )
            for (rule, severity, restriction_set), n in sorted(groups.items())
        ],
    )


# Fact rows carry ``None`` for the overwhelmingly common validity.
DEFAULT_VALIDITY = {"reason": None, "status": "valid"}


def pack_payload(pack: CandidatePack) -> dict:
    """The compact JSON document the model reads for one candidate.

    This is the single serialization of a pack: ``build_evidence`` measures it to
    fit the token budget and ``llm.prompts`` sends it, so what is gated is what
    is sent. The private pack stays fully typed.

    Scope/period tables, fact types and the shared namespace avoid repeating
    them per fact. Provenance needed for reasoning (formula, parents, weighting)
    stays visible and quality issues carry their own scope. Source
    hashes/columns/line handles remain in the private registry. Numeric-looking
    labels live only in the explicitly untrusted segment table.
    """
    payload = pack.model_dump(mode="json")
    payload["untrusted_segment_labels"] = payload.pop("segments")
    # Code attaches the overlap groups to the answer; the model only needs to
    # know how many other candidates share this traffic. A busy export gives
    # one slice dozens of groups.
    candidate = payload["candidate"]
    candidate["overlap_group_count"] = len(candidate.pop("overlap_group_ids"))
    # Likewise every issue on overlapping traffic: a coarse slice of a busy
    # export touches over a thousand. The ones that bear on its claims are in
    # quality_issues, the rest are counted by rule in the answer guide.
    candidate["quality_issue_count"] = len(candidate.pop("quality_issue_ids"))
    # Dictionary-encoded metadata avoids repeating long scope/unit/formula
    # strings. Table indices are metadata, never claim quantities. Stable fact
    # IDs and all decimal values stay verbatim; no numeric rounding/compression.
    segment_indices = {
        s["segment_id"]: n for n, s in enumerate(payload["untrusted_segment_labels"])
    }
    period_indices = {p["period_id"]: n for n, p in enumerate(payload["periods"])}
    # build_evidence always lists every scope its facts use; a hand-built pack
    # that does not still gets the missing ones appended rather than dropped.
    for f in payload["facts"]:
        if f["segment"]["segment_id"] not in segment_indices:
            segment_indices[f["segment"]["segment_id"]] = len(segment_indices)
            payload["untrusted_segment_labels"].append(f["segment"])
        if f["period"]["period_id"] not in period_indices:
            period_indices[f["period"]["period_id"]] = len(period_indices)
            payload["periods"].append(f["period"])
    fact_types = sorted(
        {
            (
                f["kind"],
                f["metric"],
                f["unit"],
                f["provenance"]["formula_id"],
                f["provenance"]["weighting"],
            )
            for f in payload["facts"]
        }
    )
    payload["fact_type_fields"] = ["kind", "metric", "unit", "formula_id", "weighting"]
    payload["fact_types"] = [list(t) for t in fact_types]
    payload["fact_fields"] = [
        "fact_id",
        "segment_index",
        "period_index",
        "type_index",
        "value",
        "validity",
        "parent_fact_ids",
        "comparison",
    ]
    payload["null_validity_means"] = DEFAULT_VALIDITY
    payload["facts"] = [
        [
            f["fact_id"],
            segment_indices[f["segment"]["segment_id"]],
            period_indices[f["period"]["period_id"]],
            fact_types.index(
                (
                    f["kind"],
                    f["metric"],
                    f["unit"],
                    f["provenance"]["formula_id"],
                    f["provenance"]["weighting"],
                )
            ),
            f["value"],
            None if f["validity"] == DEFAULT_VALIDITY else f["validity"],
            f["provenance"]["parent_fact_ids"],
            f["comparison"],
        ]
        for f in payload["facts"]
    ]
    # Issues are rows over one shared field list; every restriction and fact ref
    # is kept.
    issues = payload["quality_issues"]
    fields = sorted(issues[0]) if issues else []
    payload["quality_issue_fields"] = fields
    payload["quality_issues"] = [[i[k] for k in fields] for i in issues]
    # These tables retain every restriction and fact ref. Presentation-only
    # prose and redundant namespace fields stay in the private typed objects.
    payload["assumptions"] = [
        {
            k: a[k]
            for k in (
                "assumption_id",
                "parameter_key",
                "value",
                "unit",
                "bounds",
                "segment_id",
                "period_id",
                "dependency_ids",
                "not_observed",
            )
        }
        for a in payload["assumptions"]
    ]
    return payload


def serialize_pack(pack: CandidatePack) -> str:
    """Canonical text of ``pack_payload``; the size the token gate measures."""
    return canonical_json(pack_payload(pack))


def _closure(ids: Iterable[str], facts: dict[str, Fact]) -> set[str]:
    seen: set[str] = set()
    pending = list(ids)
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        if ref not in facts:
            raise ValueError("evidence contains an unresolved fact reference")
        seen.add(ref)
        fact = facts[ref]
        pending.extend(fact.provenance.parent_fact_ids)
        if fact.comparison:
            pending.extend(
                (fact.comparison.left_fact_id, fact.comparison.right_fact_id)
            )
    return seen


def _assumption(ns, segment, period, key, value, unit, low, high) -> Assumption:
    payload = {
        "parameter_key": key,
        "value": format(Decimal(value), "f"),
        "unit": unit.value,
        "segment_id": segment,
        "period_id": period,
    }
    return Assumption(
        assumption_id=make_id(IdPrefix.ASSUMPTION, ns, payload),
        run_namespace=ns,
        label=key.replace("_", " "),
        parameter_key=key,
        value=value,
        unit=unit,
        bounds=AssumptionBounds(minimum=low, maximum=high),
        rationale="Planning parameter only; not observed and not a statistical guarantee.",
        selector=AssumptionSelector.DEFAULT_TEST_TEMPLATE,
        segment_id=segment,
        period_id=period,
        dependency_ids=[],
    )


def build_evidence(
    metrics: MetricsStore,
    quality: QualityReport,
    candidates: list[Candidate],
    config: EvidenceConfig,
    *,
    count_tokens: Callable[[str], int] | None = None,
) -> EvidenceBundle:
    """``build_evidence_bounded`` where an irreducible pack fails the build."""
    return build_evidence_bounded(
        metrics, quality, candidates, config, count_tokens=count_tokens
    )


def build_evidence_bounded(
    metrics: MetricsStore,
    quality: QualityReport,
    candidates: list[Candidate],
    config: EvidenceConfig,
    *,
    count_tokens: Callable[[str], int] | None = None,
    skip_oversize: bool = False,
) -> EvidenceBundle:
    """Promote real detector/quality facts, compute scenarios, then bound packs.

    Optional ``count_tokens`` must be the deployed tokenizer. Without one only
    the byte-size gate is applied; the client MUST count the complete request
    with its deployed tokenizer before inference. No chars/token guess is used.
    All candidates stay in the private audit registry; only non-rejected first
    ``max_candidates`` get model packs. Redundant daily/peer rows may be removed,
    never triggers, contrary evidence, quality facts or scenario dependencies.
    A candidate whose mandatory evidence alone exceeds the budget raises
    ``EvidenceBudgetError``, or with ``skip_oversize`` gets no pack and is
    listed in the bundle's ``unpacked`` with the reason.
    """
    ns = metrics.run_namespace
    if quality.run_namespace != ns:
        raise ValueError("quality belongs to another run namespace")
    if len({c.candidate_id for c in candidates}) != len(candidates):
        raise ValueError("duplicate candidate IDs")
    detected = candidate_evidence(metrics, quality, candidates)
    facts = {f.fact_id: f for f in detected.facts.values()}
    for fact in quality.facts:
        if fact.fact_id in facts and facts[fact.fact_id] != fact:
            raise ValueError("quality fact conflicts with a registered immutable fact")
        facts[fact.fact_id] = fact

    # Supply account and candidate historical inputs even if a detector never
    # needed that metric. Reuse B's FactBuilder (same IDs, units and lineage).
    builder = FactBuilder(metrics, quality)
    scopes = {(c.segment_id, c.period_id) for c in candidates}
    scopes.update(
        (r["segment_id"], r["period_id"])
        for r in metrics.aggregates.to_dict("records")
        if not metrics.segments[r["segment_id"]].filters
    )
    for sid, pid in sorted(scopes):
        row = stats_row(metrics, sid, pid)
        if row is None:
            raise ValueError("candidate references an unknown aggregate scope")
        segment, period = metrics.segments[sid], metrics.periods[pid]
        for metric in ADDITIVE:
            builder.additive(segment, period, row, metric)
        for metric in (
            MetricKey.REVENUE_PER_MILLION_REQUESTS,
            MetricKey.PROFIT_PER_MILLION_REQUESTS,
            MetricKey.MARGIN,
        ):
            builder.rate(segment, period, row, metric)
    for ref, fact in builder.facts.items():
        if ref in facts and facts[ref] != fact:
            raise ValueError("computed fact conflicts with a registered immutable fact")
        facts[ref] = fact
    historical = FactRegistry(ns, facts.values())

    index = IssueIndex(metrics, quality)
    enriched = []
    for candidate in candidates:
        issue_ids = index.issues_for(
            metrics.segments[candidate.segment_id], metrics.periods[candidate.period_id]
        )
        enriched.append(
            candidate.model_copy(update={"quality_issue_ids": issue_ids}, deep=True)
        )
    by_id = {c.candidate_id: c for c in enriched}
    leads = []
    account_rows = {r["period_id"]: r for r in metrics.aggregates[
        metrics.aggregates["level"] == "account"
    ].to_dict("records") if r["cohort_id"] == "full_input"}
    for c in enriched:
        if c.eligibility is EligibilityStatus.REJECTED:
            continue
        row = stats_row(metrics, c.segment_id, c.period_id)
        account = account_rows[c.period_id]
        leads.append(Lead(
            c.candidate_id, c.family, metrics.segments[c.segment_id].filters,
            c.period_id, row["row_set_hash"],
            total_signature(row["requests"], row["revenue"], row["profit"]),
            opportunity_score(c.family, row, account), c.eligibility is EligibilityStatus.ELIGIBLE,
        ))
    distinct, merged = collapse(leads)
    absorbed = {source: target for target, sources in merged.items() for source in sources}
    enriched = [c.model_copy(update={
        "eligibility": EligibilityStatus.REJECTED,
        "rejection_reason": f"same traffic as {absorbed[c.candidate_id]}; merged into the simpler finding",
    }) if c.candidate_id in absorbed else c for c in enriched]
    cap = config.max_candidates
    if len(metrics.segments) > config.large_export_segments:
        cap = min(cap, config.large_export_max_candidates)
        selected = select(distinct, cap)
    else:
        # Small exports retain the caller's detector/inspection order. The
        # hand-in card selector still applies dollar order and supply caps.
        positions = {c.candidate_id: n for n, c in enumerate(candidates)}
        selected = sorted(distinct, key=lambda x: (not x.eligible, positions[x.key]))[:cap]
    chosen = [] if quality.has_fatal else [by_id[x.key] for x in selected]

    assumptions: dict[str, Assumption] = {}
    requests = []
    for candidate in chosen:
        sid, pid = candidate.segment_id, candidate.period_id
        template = SCENARIOS.get(candidate.family, ScenarioTemplate.NOT_IDENTIFIABLE)
        params = [
            ("test_allocation", config.test_allocation, Unit.FRACTION, "0.001", "0.5"),
            ("test_min_days", Decimal(config.test_min_days), Unit.DAYS, "1", "365"),
            (
                "test_profit_stop_loss",
                config.test_profit_stop_loss,
                Unit.FRACTION,
                "0.001",
                "0.5",
            ),
        ]
        if template in PARAMETERS:
            key, unit = PARAMETERS[template]
            value = {
                ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: config.default_additional_request_fraction,
                ScenarioTemplate.THROTTLE: config.default_throttle_fraction,
                ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: config.margin_sensitivity_points,
            }[template]
            params.append((key, value, unit, "0", "1"))
        scoped = [_assumption(ns, sid, pid, *p) for p in params]
        assumptions.update((a.assumption_id, a) for a in scoped)
        own = {
            f.metric: f
            for f in builder.facts.values()
            if f.segment.segment_id == sid and f.period.period_id == pid
        }
        metrics_needed = REQUIRED_METRICS[template] or (MetricKey.REQUESTS,)
        requests.append(
            ScenarioRequest(
                template=template,
                segment_id=sid,
                period_id=pid,
                historical_fact_ids=[own[m].fact_id for m in metrics_needed],
                assumption_ids=[
                    a.assumption_id
                    for a in scoped
                    if template in PARAMETERS
                    and a.parameter_key == PARAMETERS[template][0]
                ],
            )
        )
    assumption_registry = AssumptionRegistry(ns, assumptions.values())
    result = evaluate_scenarios(historical, assumption_registry, requests)
    registry = historical.extended(result.facts)
    scenarios = [
        s.model_copy(
            update={
                "overlap_group_ids": sorted(
                    {
                        g
                        for c in enriched
                        if c.segment_id == s.segment_id and c.period_id == s.period_id
                        for g in c.overlap_group_ids
                    }
                )
            },
            deep=True,
        )
        for s in result.scenarios.values()
    ]
    scenario_registry = ScenarioRegistry(ns, scenarios)
    facts = {f.fact_id: f for f in registry.values()}
    segments = dict(metrics.segments)
    periods = dict(metrics.periods)
    for fact in facts.values():
        for table, key, value in (
            (segments, fact.segment.segment_id, fact.segment),
            (periods, fact.period.period_id, fact.period),
        ):
            if key in table and table[key] != value:
                raise ValueError("scope ID resolves to inconsistent metadata")
            table[key] = value
    _closure(facts, facts)
    packs = {}
    unpacked: dict[str, BudgetReason] = {}
    counter = count_tokens
    for candidate in chosen:
        # The validator's own issue scope, so the guide states its restrictions.
        issues = scoped_issues(
            quality.issues, segments, periods, candidate.segment_id, candidate.period_id
        )
        segment = segments[candidate.segment_id]
        detailed, summarised = split_issues(candidate, segment, issues, segments, facts)
        scenario_list = [
            s
            for s in scenarios
            if s.segment_id == candidate.segment_id
            and s.period_id == candidate.period_id
        ]
        assumption_list = [
            a
            for a in assumptions.values()
            if a.segment_id == candidate.segment_id
            and a.period_id == candidate.period_id
        ]
        mandatory = [*candidate.trigger_fact_ids, *candidate.counter_evidence_fact_ids]
        # B computes these for every candidate even when no warning fires.
        # Coverage/concentration are required context, not optional contrary
        # evidence only when a threshold happens to be exceeded.
        context_metrics = {
            MetricKey.OBSERVED_DAYS,
            MetricKey.REVENUE_POSITIVE_DAYS,
            *CONCENTRATION_METRICS,
        }
        mandatory.extend(
            f.fact_id
            for f in facts.values()
            if f.period.period_id == candidate.period_id
            and f.kind in (FactKind.MEASURED, FactKind.CALCULATED)
            and (
                (
                    f.segment.segment_id == candidate.segment_id
                    and f.metric in context_metrics
                )
                or (
                    not f.segment.filters
                    and f.segment.cohort_id == "full_input"
                    and f.metric is MetricKey.OBSERVED_DAYS
                )
            )
        )
        mandatory.extend(issue_fact_ids(detailed))
        for scenario in scenario_list:
            mandatory.extend((*scenario.historical_fact_ids, *scenario.result_fact_ids))
        required = _closure(mandatory, facts)

        def pack_for(
            ids: set[str],
            candidate=candidate,
            segment=segment,
            issues=issues,
            detailed=detailed,
            summarised=summarised,
            assumption_list=assumption_list,
            scenario_list=scenario_list,
        ) -> CandidatePack:
            subset = [facts[r] for r in sorted(ids)]
            sids = {f.segment.segment_id for f in subset} | {candidate.segment_id}
            pids = {f.period.period_id for f in subset} | {candidate.period_id}
            for issue in detailed:
                sids.update(issue.segment_ids)
                pids.update(issue.period_ids)
            permitted = permitted_tests(candidate, facts)
            # The full candidate remains in bundle.candidates. The prompt must
            # not advertise refs it did not receive; omissions are retrievable
            # through the allowlisted tools and never alter contrary evidence.
            view = candidate.model_copy(
                update={
                    "daily_fact_ids": [r for r in candidate.daily_fact_ids if r in ids],
                    "peer_fact_ids": [r for r in candidate.peer_fact_ids if r in ids],
                },
                deep=True,
            )
            return CandidatePack(
                run_namespace=ns,
                candidate=view,
                quality_issues=detailed,
                segments=[segments[r] for r in sorted(sids)],
                periods=[periods[r] for r in sorted(pids)],
                facts=subset,
                assumptions=assumption_list,
                scenarios=scenario_list,
                permitted_test_templates=permitted,
                tool_allowlist=list(ToolName),
                token_budget=config.max_pack_tokens,
                answer_guide=answer_guide(
                    candidate,
                    segment,
                    periods[candidate.period_id],
                    issues,
                    summarised,
                    {f.fact_id: f for f in subset},
                    assumption_list,
                    scenario_list,
                    permitted,
                ),
            )

        def fits(pack: CandidatePack) -> bool:
            text = serialize_pack(pack)
            return len(text.encode("utf-8")) <= config.max_pack_bytes and (
                counter is None or counter(text) <= pack.token_budget
            )

        pack = pack_for(required)
        if not fits(pack):
            # Mandatory data is irreducible: never drop caveats or contrary
            # evidence to make it fit. Either fail the build, or leave this one
            # candidate unjudged and say why in ``unpacked``.
            if not skip_oversize:
                raise EvidenceBudgetError(
                    "mandatory evidence exceeds the configured pack budget"
                )
            unpacked[candidate.candidate_id] = BudgetReason.EVIDENCE_TOO_LARGE
            continue
        selected = required
        for ref in (*candidate.peer_fact_ids, *candidate.daily_fact_ids):
            proposal = selected | _closure([ref], facts)
            proposed_pack = pack_for(proposal)
            if fits(proposed_pack):
                selected, pack = proposal, proposed_pack
        packs[candidate.candidate_id] = pack
    return EvidenceBundle(
        run_namespace=ns,
        facts=registry,
        assumptions=assumption_registry,
        scenarios=scenario_registry,
        segments=MappingProxyType(segments),
        periods=MappingProxyType(periods),
        candidates=MappingProxyType({c.candidate_id: c for c in enriched}),
        overlap_groups=MappingProxyType(dict(detected.overlap_groups)),
        quality=quality,
        packs=MappingProxyType(packs),
        unpacked=MappingProxyType(unpacked),
    )
