"""The short hand-in report: ranked recommendation cards in the assignment's shape.

Each card answers what was found (the slice), why it is interesting (the
data), what to do, and the potential impact. Everything else (what the data
does not prove, test design, guardrails, assumptions, fact IDs, run metadata)
sits behind a collapsed "Details" per card or in an appendix.

The page is built once into a small intermediate form in which every quantity
is a ``Val`` carrying the ID of the registry object it came from. Markdown and
self-contained HTML are both written from that form, so they cannot disagree,
and ``audit_summary`` checks both: every digit must sit inside a ``Val`` whose
source resolves in the run, and a replay of the renderer must reproduce the
text exactly. The model never types a number.
"""

import html
import re
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser

from contracts.enums import (
    Decision,
    DetectorFamily,
    EligibilityStatus,
    FactKind,
    MetricKey,
    QualityRuleId,
    RenderRole,
    RunStatus,
    Severity,
    Unit,
    ValidityStatus,
)
from contracts.models import (
    Assumption,
    CandidateRecommendation,
    Fact,
    ObservationStatement,
    QualityIssue,
    RenderedReport,
    RenderSpan,
    Scenario,
    ValidatedRecommendation,
)
from contracts.runtime import ValidatedRun
from contracts.templates import (
    OBSERVATION_TEMPLATES,
    ObservationTemplateId,
    TestTemplateId,
)
from pipeline.quality import render_message
from pipeline.shortlist import Lead, collapse, select, total_signature
from report import text as T
from report.opportunities import Opportunities
from report.render import (
    STATIC_ALLOWLIST,
    RenderAudit,
    RenderError,
    account_fact_groups,
    format_value,
    known_source_ids,
    period_label,
)

MAX_CARDS = 10
MAX_CAVEATS = 3
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#|<>~!])")
_MAX_LABEL = 48
_MAX_REASON = 160
_MAX_MESSAGE = 600


# --- Intermediate form -------------------------------------------------------


@dataclass(frozen=True)
class Lit:
    """Reviewed wording or structure; the audit forbids digits in it."""

    s: str


@dataclass(frozen=True)
class Val:
    """A code-rendered value and the registry object it was read from."""

    s: str
    src: str
    role: RenderRole = RenderRole.METADATA
    untrusted: bool = False  # labels from the export: escaped, never interpreted
    ref: bool = False  # an ID shown as such


Inline = Lit | Val
Line = list[Inline]


@dataclass
class Card:
    rec_id: str
    rank: Val
    ai: bool
    opportunity: Line
    period: Line
    current: list[tuple[str, Line]]
    observation: list[Line]
    verb: str
    action: Line
    estimate: Line
    has_estimate: bool
    details: list[tuple[str, list[Line]]]
    compare: tuple[Fact, Fact] | None = None
    size: tuple[Fact | None, Fact | None] = (None, None)


@dataclass
class Bar:
    label: str
    value: Val
    fraction: float
    negative: bool = False
    emphasis: bool = False


@dataclass
class ChartRow:
    label: Line
    bars: list[Bar]


@dataclass
class Chart:
    title: str
    caption: str
    rows: list[ChartRow]


@dataclass
class Page:
    status_text: str
    status_ok: bool
    status_extra: list[Line]
    account: Line
    account_period: Line
    caveats: list[Line]
    more_caveats: Line | None
    cards: list[Card]
    notice: list[Line]
    leads: list[Line]
    charts: list[Chart]
    notes: list[Line]
    appendix: list[tuple[str, list[Line]]] = field(default_factory=list)


@dataclass(frozen=True)
class ShortReport:
    """The short report as Markdown with an audit map, and as one HTML page."""

    report: RenderedReport
    html: str


# --- Text helpers ------------------------------------------------------------


def _plain(raw: str, max_length: int = _MAX_LABEL) -> str:
    """Untrusted text: no control characters, bounded length (escaping happens on output)."""
    flat = " ".join(_CONTROL.sub(" ", raw).split())
    if len(flat) > max_length:
        flat = flat[: max_length - 1] + "…"
    return flat or "(blank)"


def _sentence(s: str) -> str:
    return s[:1].upper() + s[1:]


# Day-concentration shares are caveats, not observations worth a card line.
_CONCENTRATION_METRICS = frozenset({
    MetricKey.MAX_DAY_REVENUE_SHARE, MetricKey.TOP_TWO_DAY_REVENUE_SHARE,
    MetricKey.MAX_DAY_PROFIT_SHARE, MetricKey.TOP_TWO_DAY_PROFIT_SHARE,
})


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:]


def _join(parts: list[Line], sep: str) -> Line:
    out: Line = []
    for n, part in enumerate(parts):
        if n:
            out.append(Lit(sep))
        out.extend(part)
    return out


def _metric_name(fact: Fact) -> str:
    """Metric label without a unit phrase the formatted value already carries."""
    label = T.METRIC_LABELS[fact.metric]
    for suffix in (" per million requests", " per thousand impressions"):
        if label.endswith(suffix) and format_value(Decimal(1), fact.unit).endswith(suffix[1:]):
            return label[: -len(suffix)]
    return label


def _actionable(r: CandidateRecommendation) -> bool:
    return r.decision in (Decision.RECOMMEND_TEST, Decision.INVESTIGATE_FIRST)


# --- Building the page -------------------------------------------------------


class _Builder:
    def __init__(self, run: ValidatedRun) -> None:
        self.run = run
        self.b = run.bundle
        self.run_id = run.manifest.run_id
        self.recs = {r.recommendation_id: r for r in run.recommendations}
        self.planner = Opportunities(run)
        self.plans = {r.recommendation.candidate_id: self.planner.plan(r)
                      for r in run.recommendations if _actionable(r.recommendation)}
        self.card_merges: dict[str, list[str]] = {}
        self.issue_facts = {f.fact_id: f for f in self.b.quality.facts}
        self.issue_facts.update((f.fact_id, f) for f in self.b.facts.values())
        self.measured: dict[tuple[str, str, MetricKey], Fact] = {}
        for fact in self.b.facts.values():
            if fact.kind is FactKind.MEASURED:
                self.measured.setdefault(
                    (fact.segment.segment_id, fact.period.period_id, fact.metric), fact)

    # Lookups fail closed: a validated run never references unknown IDs.

    def fact(self, fact_id: str) -> Fact:
        fact = self.b.facts.get(fact_id)
        if fact is None:
            raise RenderError(f"unknown fact {fact_id}")
        return fact

    def assumption(self, assumption_id: str) -> Assumption:
        found = self.b.assumptions.get(assumption_id)
        if found is None:
            raise RenderError(f"unknown assumption {assumption_id}")
        return found

    def scenario(self, scenario_id: str) -> Scenario:
        found = self.b.scenarios.get(scenario_id)
        if found is None:
            raise RenderError(f"unknown scenario {scenario_id}")
        return found

    # Inline pieces.

    def ref(self, source_id: str, role: RenderRole = RenderRole.METADATA) -> Val:
        return Val(source_id, source_id, role, ref=True)

    def refs(self, ids: list[str], role: RenderRole = RenderRole.METADATA) -> Line:
        return _join([[self.ref(i, role)] for i in ids], ", ")

    def segment(self, segment_id: str, strict: bool = True) -> Line:
        seg = self.b.segments.get(segment_id)
        if seg is None:
            if strict:
                raise RenderError(f"unknown segment {segment_id}")
            return [Lit("segment "), self.ref(segment_id, RenderRole.WARNING)]
        if seg.filters:
            label = " · ".join(
                f"{T.DIMENSION_LABELS[d]} {_plain(seg.filters[d])}"
                for d in T.DIMENSION_LABELS if d in seg.filters)
        else:
            label = T.ACCOUNT_LABEL
        if seg.cohort_id != "full_input":
            label += f" (cohort {_plain(seg.cohort_id)})"
        return [Val(label, segment_id, untrusted=True)]

    def period(self, period_id: str, strict: bool = True) -> Line:
        period = self.b.periods.get(period_id)
        if period is None:
            if strict:
                raise RenderError(f"unknown period {period_id}")
            return [Lit("period "), self.ref(period_id, RenderRole.WARNING)]
        return [Val(period_label(period), period_id)]

    def value(self, fact: Fact, role: RenderRole = RenderRole.OBSERVATION) -> Line:
        if fact.value is None:
            reason = _plain(fact.validity.reason or "no reason recorded", _MAX_REASON)
            return [Val(f"unavailable ({reason})", fact.fact_id, role, untrusted=True)]
        out: Line = [Val(format_value(fact.value, fact.unit), fact.fact_id, role)]
        return out

    def metric_line(self, fact: Fact, role: RenderRole = RenderRole.OBSERVATION) -> Line:
        """``Metric for segment, period: value [F_]`` for the Details evidence list."""
        return [Lit(f"{_sentence(T.METRIC_LABELS[fact.metric])} for "),
                *self.segment(fact.segment.segment_id), Lit(", "),
                *self.period(fact.period.period_id), Lit(": "), *self.value(fact, role),
                Lit(" "), self.ref(fact.fact_id, role)]

    # Page.

    def page(self) -> Page:
        quality = self.b.quality
        fatal = quality.has_fatal
        ranked = [] if fatal else self.ordered_recommendations()
        cards = [self.card(n, rec) for n, rec in enumerate(ranked, start=1)]
        shown = {c.recommendation.candidate_id for c in ranked}
        caveats, more = self.caveats(shown)
        notice: list[Line] = []
        leads: list[Line] = []
        if fatal:
            notice.append([Lit(T.NO_CARDS_FATAL)])
        elif not cards:
            notice.append([Lit(T.NO_CARDS)])
            leads = self.deterministic_leads()
        elif len(cards) < 5:
            notice.append([Lit(T.FEW_RECOMMENDATIONS)])
        return Page(
            status_text=T.STATUS_BANNERS[self.run.status],
            status_ok=self.run.status is RunStatus.FINAL,
            status_extra=self.status_extra(),
            account=self.account_line(),
            account_period=self.account_period(),
            caveats=caveats, more_caveats=more, cards=cards, notice=notice, leads=leads,
            charts=self.charts(cards),
            notes=[[Lit(T.NO_PORTFOLIO_SUM)], [Lit(T.GENERAL_LIMIT)], [Lit(T.CURRENCY_NOTE)]],
            appendix=self.appendix(ranked),
        )

    def status_extra(self) -> list[Line]:
        m = self.run.manifest
        out: list[Line] = []
        if self.run.status is RunStatus.CACHED_PREVIOUS:
            out.append([Lit("Shown from a previous run generated at "),
                        Val(m.generated_at.isoformat(), self.run_id),
                        Lit(". Re-rendering does not change the inference generation time.")])
            if m.model_id is None:
                out.append([Lit("The cached run included no model analysis "
                                "(deterministic only).")])
        return out

    def account_line(self) -> Line:
        groups = account_fact_groups(self.b)
        if not groups:
            return [Lit("Account totals are not available for this run.")]
        facts = {f.metric: f for f in groups[0][2]}
        parts: list[Line] = []
        for metric, label in ((MetricKey.REQUESTS, "requests"), (MetricKey.REVENUE, "revenue"),
                              (MetricKey.PROFIT, "profit"), (MetricKey.MARGIN, "margin")):
            fact = facts.get(metric)
            if fact is not None:
                parts.append([*self.value(fact), Lit(f" {label}")])
        return _join(parts, " · ")

    def account_period(self) -> Line:
        groups = account_fact_groups(self.b)
        return self.period(groups[0][1]) if groups else []

    # Caveats: only warnings that touch what the report concludes.

    def caveats(self, shown: set[str]) -> tuple[list[Line], Line | None]:
        issues = [i for i in self.b.quality.issues if i.severity is not Severity.NOTICE]

        def account_wide(issue: QualityIssue) -> bool:
            return any(not (self.b.segments[s].filters if s in self.b.segments else True)
                       for s in issue.segment_ids)

        def weight(issue: QualityIssue) -> tuple[int, int, Decimal]:
            priority = {QualityRuleId.DQ06: 3, QualityRuleId.DQ04: 2,
                        QualityRuleId.DQ10: 1}.get(issue.rule_id, 0)
            exposure = self.issue_facts.get(issue.request_exposure_fact_id)
            return (issue.severity is Severity.FATAL, priority,
                    exposure.value if exposure and exposure.value is not None else Decimal(0))

        def material(issue: QualityIssue) -> bool:
            exposure = self.issue_facts.get(issue.request_exposure_fact_id)
            return (issue.severity is Severity.FATAL or exposure is None
                    or exposure.value is None or exposure.value >= Decimal("0.00005"))

        relevant = [i for i in issues if material(i) and (i.severity is Severity.FATAL
                    or account_wide(i) or i.rule_id in (QualityRuleId.DQ04, QualityRuleId.DQ10))]
        relevant.sort(key=weight, reverse=True)
        chosen = []
        seen = set()
        for issue in relevant:
            if issue.rule_id not in seen:
                seen.add(issue.rule_id)
                chosen.append(issue)
        lines = [self.account_caveat(i) for i in chosen[:MAX_CAVEATS]]
        more = [Lit("Full data-quality checks: appendix.")] if self.b.quality.issues else None
        return lines, more

    def account_caveat(self, issue: QualityIssue) -> Line:
        if issue.message_template_id == "DQ06_possible_partial_day":
            ratio = next((self.issue_facts[i] for i in issue.measured_fact_ids
                          if i in self.issue_facts
                          and self.issue_facts[i].metric is MetricKey.DAY_REQUEST_RATIO), None)
            if ratio and ratio.value is not None:
                return [Lit("The export on "), Val(ratio.period.start.isoformat(), issue.issue_id,
                                                  RenderRole.WARNING),
                        Lit(" has only "), Val(format_value(ratio.value, Unit.FRACTION),
                                                issue.issue_id, RenderRole.WARNING),
                        Lit(" of normal request volume and may be incomplete; check complete-day results before scaling.")]
        exposure = self.issue_facts.get(issue.request_exposure_fact_id)
        if issue.rule_id in (QualityRuleId.DQ04, QualityRuleId.DQ10) and exposure:
            label, consequence = (
                ("Inconsistent counters affect at least ", " of account requests; check delivery logs before estimating recovered impressions.")
                if issue.rule_id is QualityRuleId.DQ04 else
                ("Placeholder or unverified labels affect at least ", " of account requests; do not target them as real countries.")
            )
            return [Lit(label), *self.value(exposure, RenderRole.WARNING), Lit(consequence)]
        rule = T.QUALITY_RULES[issue.rule_id]
        return [Lit(f"{rule.title}: {rule.consequence}")]

    def caveat(self, issue: QualityIssue) -> Line:
        rule = T.QUALITY_RULES[issue.rule_id]
        try:
            message = render_message(issue, self.issue_facts)
        except KeyError:
            message = None
        if message:
            return [Val(_plain(message, _MAX_MESSAGE), issue.issue_id, RenderRole.WARNING,
                        untrusted=True)]
        return [Lit(f"{rule.title}: {rule.consequence}")]

    # Recommendations.

    def ordered_recommendations(self) -> list[ValidatedRecommendation]:
        actionable = {r.recommendation_id: r for r in self.run.recommendations
                      if _actionable(r.recommendation)}
        leads = []
        for rec in actionable.values():
            r = rec.recommendation
            cand = self.b.candidates.get(r.candidate_id)
            metrics = [self.planner.metric(r, m) for m in
                       (MetricKey.REQUESTS, MetricKey.REVENUE, MetricKey.PROFIT)]
            complete = all(f is not None and f.value is not None for f in metrics)
            leads.append(Lead(
                rec.recommendation_id, self.plans[r.candidate_id].family,
                self.b.segments[r.segment_id].filters, r.period_id,
                metrics[0].provenance.source_row_set_hash if complete else None,
                total_signature(*(f.value for f in metrics)) if complete else None,
                self.plans[r.candidate_id].score,
                cand is None or cand.eligibility is EligibilityStatus.ELIGIBLE,
            ))
        distinct, self.card_merges = collapse(leads)
        return [actionable[x.key] for x in select(distinct, MAX_CARDS)]

    def card(self, rank: int, rec: ValidatedRecommendation) -> Card:
        r = rec.recommendation
        cand = self.b.candidates.get(r.candidate_id)
        slice_facts = {m: self.measured.get((r.segment_id, r.period_id, m))
                       for m in (MetricKey.REQUESTS, MetricKey.REVENUE, MetricKey.PROFIT)}
        current = [(label, self.value(slice_facts[m], RenderRole.OBSERVATION)
                    if slice_facts[m] else [Lit("not available")])
                   for m, label in ((MetricKey.REQUESTS, "Requests"),
                                    (MetricKey.REVENUE, "Revenue"),
                                    (MetricKey.PROFIT, "Profit"))]
        estimate, has_estimate = self.estimate(r)
        return Card(
            rec_id=rec.recommendation_id,
            rank=Val(format_value(Decimal(rank), Unit.COUNT), rec.recommendation_id),
            ai=r.origin == "model_proposed" or (cand is not None
                                                and cand.origin == "model_proposed"),
            opportunity=self.segment(r.segment_id),
            period=self.period(r.period_id),
            current=current,
            observation=self.observations(r),
            verb=self.verb(r), action=self.action(r),
            estimate=estimate, has_estimate=has_estimate,
            details=self.details(rec),
            compare=self.comparison(r),
            size=(slice_facts[MetricKey.REQUESTS], slice_facts[MetricKey.REVENUE]),
        )

    def verb(self, r: CandidateRecommendation) -> str:
        family = self.plans[r.candidate_id].family
        return {
            DetectorFamily.SCALE_WINNER: "Scale up",
            DetectorFamily.WASTED_REQUESTS: "Throttle",
            DetectorFamily.MARGIN_LEAK: "Investigate",
            DetectorFamily.FLOOR_HIGH_BID: "Raise floor",
            DetectorFamily.FLOOR_LOW_BID: "Lower floor",
            DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: "Fix delivery/counters",
        }.get(family, T.ACTIONS[r.test_template_id][0] if r.test_template_id else "Investigate")

    def test_id(self, r: CandidateRecommendation) -> TestTemplateId | None:
        family = self.plans[r.candidate_id].family
        return {
            DetectorFamily.SCALE_WINNER: TestTemplateId.MATCHED_ALLOCATION_HOLDOUT,
            DetectorFamily.WASTED_REQUESTS: TestTemplateId.TARGETED_THROTTLE_HOLDOUT,
            DetectorFamily.MARGIN_LEAK: TestTemplateId.COMMERCIAL_TERMS_REVIEW,
            DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: TestTemplateId.COUNTER_RECONCILIATION,
            DetectorFamily.FLOOR_HIGH_BID: TestTemplateId.FLOOR_INCREASE_RANDOMIZED,
            DetectorFamily.FLOOR_LOW_BID: TestTemplateId.FLOOR_DECREASE_RANDOMIZED,
        }.get(family, r.test_template_id)

    def action(self, r: CandidateRecommendation) -> Line:
        family = self.plans[r.candidate_id].family
        if family is DetectorFamily.WASTED_REQUESTS:
            sentence = "Test throttling low-yield requests against a concurrent holdout; confirm contract terms and monitor revenue at risk."
        elif family is DetectorFamily.MARGIN_LEAK:
            sentence = "Review margin terms and reconcile costs before testing a commercial change."
        elif family is DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY:
            sentence = "Reconcile counters with delivery logs before testing a delivery fix."
        elif self.test_id(r) is None:
            return [Lit(T.INVESTIGATE_ACTION)]
        else:
            sentence = T.ACTIONS[self.test_id(r)][1]
        if r.decision is Decision.INVESTIGATE_FIRST:
            sentence = T.INVESTIGATE_FIRST_PREFIX + _lower_first(sentence)
        return [Lit(sentence)]

    def observations(self, r: CandidateRecommendation) -> list[Line]:
        family = self.plans[r.candidate_id].family
        metric = {
            DetectorFamily.WASTED_REQUESTS: MetricKey.REVENUE_PER_MILLION_REQUESTS,
            DetectorFamily.SCALE_WINNER: MetricKey.PROFIT_PER_MILLION_REQUESTS,
            DetectorFamily.MARGIN_LEAK: MetricKey.MARGIN,
            DetectorFamily.FLOOR_HIGH_BID: MetricKey.RESPONSE_WEIGHTED_BID,
            DetectorFamily.FLOOR_LOW_BID: MetricKey.RESPONSE_WEIGHTED_BID,
            DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: MetricKey.IMPRESSION_TO_WIN_RATE,
            DetectorFamily.COMBINATION_OUTLIER: MetricKey.PROFIT_PER_MILLION_REQUESTS,
        }.get(family)
        target = self.planner.metric(r, metric) if metric else None
        out: list[Line] = []
        if family in (DetectorFamily.FLOOR_HIGH_BID, DetectorFamily.FLOOR_LOW_BID) and target:
            floor = self.planner.metric(r, MetricKey.RESPONSE_WEIGHTED_FLOOR)
            if floor:
                out.append([Lit("Average bid: "), *self.value(target), Lit(" versus an average floor of "),
                            *self.value(floor), Lit("; test pricing before changing it broadly.")])
                cand = self.b.candidates.get(r.candidate_id)
                if cand:
                    own_ratio = next((self.fact(i) for i in cand.trigger_fact_ids
                                      if self.fact(i).comparison and self.fact(i).metric is metric), None)
                    account_ratio = next((self.fact(i) for i in cand.peer_fact_ids
                                          if self.fact(i).comparison and self.fact(i).metric is metric), None)
                    if family is DetectorFamily.FLOOR_HIGH_BID and own_ratio and account_ratio:
                        out.append([Lit("Bid-to-floor ratio: "), *self.value(own_ratio),
                                    Lit(" versus "), *self.value(account_ratio, RenderRole.BASELINE),
                                    Lit(" for the account.")])
                    if family is DetectorFamily.FLOOR_LOW_BID:
                        comp = next((self.fact(i) for i in cand.peer_fact_ids
                                     if self.fact(i).comparison and self.fact(i).value is not None
                                     and self.fact(i).value < 1), None)
                        if comp:
                            own = self.fact(comp.comparison.left_fact_id)
                            peer = self.fact(comp.comparison.right_fact_id)
                            out.append([Lit(_sentence(_metric_name(own)) + ": "), *self.value(own),
                                        Lit(" versus "), *self.value(peer, RenderRole.BASELINE),
                                        Lit(" for "), *self.baseline_label(peer, comp), Lit(".")])
                return out
        if target:
            baseline = self.planner.accounts.get((r.period_id, metric))
            cand = self.b.candidates.get(r.candidate_id)
            comparison = None
            if cand and family is DetectorFamily.COMBINATION_OUTLIER:
                comparison = next((self.fact(i) for i in cand.trigger_fact_ids
                                   if self.fact(i).comparison and self.fact(i).metric is metric), None)
                if comparison:
                    baseline = self.fact(comparison.comparison.right_fact_id)
            line = [Lit(f"{_sentence(_metric_name(target))}: "), *self.value(target)]
            if baseline:
                line += [Lit(" versus "), *self.value(baseline, RenderRole.BASELINE),
                         Lit(" for "), *(self.baseline_label(baseline, comparison)
                                         if comparison else [Lit("the account")])]
            line += [Lit("; very little revenue for the request load.") if family is DetectorFamily.WASTED_REQUESTS else Lit(".")]
            out.append(line)
        if not out:
            for statement in r.observation_statements:
                if statement.template_id in (ObservationTemplateId.METRIC_VS_BASELINE,
                                              ObservationTemplateId.TREND):
                    out.append(self.observation(statement))
                    break
        cand = self.b.candidates.get(r.candidate_id)
        if not out and family is DetectorFamily.TREND and cand:
            # The trend's own trigger: the day-to-day move that raised it.
            change = next((self.fact(i) for i in cand.trigger_fact_ids
                           if self.fact(i).metric is MetricKey.DAILY_CHANGE
                           and len(self.fact(i).provenance.parent_fact_ids) == 2), None)
            if change:
                earlier, later = (self.fact(i) for i in change.provenance.parent_fact_ids)
                O = RenderRole.OBSERVATION  # noqa: N806
                out.append([Lit(f"{_sentence(T.METRIC_LABELS[earlier.metric])} moved from "),
                            *self.value(earlier, O), Lit(" ("), *self.period(earlier.period.period_id),
                            Lit(") to "), *self.value(later, O), Lit(" ("),
                            *self.period(later.period.period_id), Lit("), a change of "),
                            *self.value(change, O), Lit(".")])
        if not out:
            # Never leave the card blank: fall back to the model's validated
            # single-value statements, skipping concentration shares.
            for statement in r.observation_statements:
                if statement.template_id is ObservationTemplateId.METRIC_VALUE and not any(
                    self.fact(i).metric in _CONCENTRATION_METRICS for i in statement.fact_ids
                ):
                    out.append(self.observation(statement))
                if len(out) == 2:
                    break
        return out

    def observation(self, statement: ObservationStatement) -> Line:
        template = OBSERVATION_TEMPLATES[statement.template_id]
        if len(statement.fact_ids) != len(template.fact_roles):
            raise RenderError(f"{statement.template_id} needs {len(template.fact_roles)} facts")
        facts = dict(zip(template.fact_roles, (self.fact(i) for i in statement.fact_ids),
                         strict=True))
        O = RenderRole.OBSERVATION  # noqa: N806
        match statement.template_id:
            case ObservationTemplateId.METRIC_VALUE:
                target = facts["target"]
                return [Lit(f"{_sentence(_metric_name(target))}: "),
                        *self.value(target, O), Lit(".")]
            case ObservationTemplateId.METRIC_VS_BASELINE:
                target, baseline, comp = facts["target"], facts["baseline"], facts["comparison"]
                out: Line = [Lit(f"{_sentence(_metric_name(target))}: "),
                             *self.value(target, O), Lit(" against "),
                             *self.value(baseline, RenderRole.BASELINE), Lit(" for "),
                             *self.baseline_label(baseline, comp)]
                detail = comp.comparison
                if detail is not None and detail.ratio is not None:
                    out += [Lit(" ("), Val(format_value(detail.ratio, Unit.RATIO), comp.fact_id, O),
                            Lit(" the baseline)")]
                return [*out, Lit(".")]
            case ObservationTemplateId.TREND:
                earlier, later, change = facts["earlier"], facts["later"], facts["change"]
                return [Lit(f"{_sentence(T.METRIC_LABELS[earlier.metric])} moved from "),
                        *self.value(earlier, O), Lit(" ("), *self.period(earlier.period.period_id),
                        Lit(") to "), *self.value(later, O), Lit(" ("),
                        *self.period(later.period.period_id), Lit("), a change of "),
                        *self.value(change, O), Lit(".")]
            case ObservationTemplateId.COVERAGE:
                observed, reference = facts["observed_days"], facts["reference_days"]
                return [Lit("Seen on "), *self.value(observed, O), Lit(" out of "),
                        *self.value(reference, RenderRole.BASELINE),
                        Lit("; a missing day is not confirmed zero traffic.")]
            case ObservationTemplateId.CONCENTRATION:
                share = facts["concentration_share"]
                return [Lit(f"Concentration, {T.METRIC_LABELS[share.metric]}: "),
                        *self.value(share, O), Lit(".")]
        raise RenderError(f"no rendering for {statement.template_id}")

    def baseline_label(self, baseline: Fact, comp: Fact) -> Line:
        if not baseline.segment.filters and baseline.segment.cohort_id == "full_input":
            return [Lit("the whole account")]
        detail = comp.comparison
        if detail is not None and detail.matching_keys:
            return [Lit("other traffic with the same "
                        + ", ".join(T.DIMENSION_LABELS[k].lower() for k in detail.matching_keys))]
        return self.segment(baseline.segment.segment_id)

    def comparison(self, r: CandidateRecommendation) -> tuple[Fact, Fact] | None:
        for statement in r.observation_statements:
            if statement.template_id is ObservationTemplateId.METRIC_VS_BASELINE:
                target, baseline = (self.fact(i) for i in statement.fact_ids[:2])
                if target.value is not None and baseline.value is not None:
                    return target, baseline
        return None

    # Estimated opportunity.

    def estimate(self, r: CandidateRecommendation) -> tuple[Line, bool]:
        plan = self.plans[r.candidate_id]
        if plan.mode == "risk":
            return [Lit("Not estimated: needs avoidable processing costs or a measured redirect gain; throttling puts existing revenue at risk.")], False
        if not plan.results:
            return [Lit("Not estimated: needs " + plan.reason.removeprefix("missing ") + ".")], False
        profits = [f for f in plan.results if f.metric is MetricKey.PROFIT]
        if not profits:
            return [Lit("Not estimated: needs measured incremental profit.")], False
        result = profits[-1]
        if result.value <= 0:
            return [Lit("Not estimated: needs evidence of a positive profit improvement.")], False
        if result.fact_id not in self.b.facts:
            raise RenderError("planning estimates must be registered before rendering")
        days = len(result.period.included_dates)
        weekly = result.value * Decimal(7) / Decimal(days)
        amount = Val(format_value(weekly, result.unit), result.fact_id, RenderRole.SCENARIO)
        condition = {
            "scale": "the planned extra traffic keeps this profit yield",
            "scenario": "the planned extra traffic keeps this profit yield",
            "margin": "the slice reaches the account margin at unchanged revenue",
            "delivery": "delivery reaches the comparison rate at unchanged profit per impression",
        }[plan.mode]
        return [Lit("About "), amount, Lit(" extra profit per week if " + condition + ".")], True

    # Per-card details.

    def details(self, rec: ValidatedRecommendation) -> list[tuple[str, list[Line]]]:
        r = rec.recommendation
        cand = self.b.candidates.get(r.candidate_id)
        out: list[tuple[str, list[Line]]] = []

        proves: list[Line] = []
        if any(self.planner.metric(r, m) and self.planner.metric(r, m).validity.status is ValidityStatus.TAINTED
               for m in (MetricKey.REQUESTS, MetricKey.REVENUE, MetricKey.PROFIT)):
            proves.append([Lit("Some historical inputs are flagged by quality checks; reconcile them before acting on the estimate.")])
        for fact_id in r.counter_evidence_fact_ids:
            proves.append([Lit("Counter-evidence: "), *self.metric_line(self.fact(fact_id))])
        if cand is not None:
            seen_rules = set()
            for issue_id in cand.quality_issue_ids:
                issue = next((i for i in self.b.quality.issues if i.issue_id == issue_id), None)
                if issue is None or issue.rule_id in seen_rules:
                    continue
                if not issue.inference_restrictions and issue.rule_id is not QualityRuleId.DQ12:
                    continue
                seen_rules.add(issue.rule_id)
                rule = T.QUALITY_RULES[issue.rule_id]
                line: Line = [Lit(f"{rule.title} ("),
                              Val(issue.rule_id.value, issue.issue_id, RenderRole.WARNING),
                              Lit(f"): {rule.consequence}")]
                if issue.inference_restrictions:
                    line.append(Lit(" Restrictions: " + ", ".join(
                        T.RESTRICTION_TEXT[x] for x in issue.inference_restrictions) + "."))
                proves.append(line)
        period = self.b.periods.get(r.period_id)
        if period is not None:
            proves.append([Lit(f"Period coverage: {T.COVERAGE_TEXT[period.coverage_status]}.")])
        if cand is not None:
            proves.append([Lit(T.FAMILY_LIMITS[cand.family])])
        proves.append([Lit(T.GENERAL_LIMIT)])
        unique: list[Line] = []
        seen_caveats: set[str] = set()
        # Keep actual inference restrictions, not repeated scope-level issues.
        for line in proves:
            key = "".join(p.s for p in line)
            if key not in seen_caveats:
                seen_caveats.add(key)
                unique.append(line)
        out.append(("What it does not prove", unique[:4]))

        evidence_ids = list(dict.fromkeys(
            i for s in r.observation_statements for i in s.fact_ids))
        target = self.planner.metric(r, {
            DetectorFamily.WASTED_REQUESTS: MetricKey.REVENUE_PER_MILLION_REQUESTS,
            DetectorFamily.SCALE_WINNER: MetricKey.PROFIT_PER_MILLION_REQUESTS,
            DetectorFamily.MARGIN_LEAK: MetricKey.MARGIN,
            DetectorFamily.FLOOR_HIGH_BID: MetricKey.RESPONSE_WEIGHTED_BID,
            DetectorFamily.FLOOR_LOW_BID: MetricKey.RESPONSE_WEIGHTED_BID,
            DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: MetricKey.IMPRESSION_TO_WIN_RATE,
        }.get(self.plans[r.candidate_id].family))
        if target:
            evidence_ids.append(target.fact_id)
            baseline = self.planner.accounts.get((r.period_id, target.metric))
            if baseline:
                evidence_ids.append(baseline.fact_id)
        out.append(("Fact IDs", [self.refs(list(dict.fromkeys(evidence_ids)))]))

        test_id = self.test_id(r)
        test = T.TEST_TEMPLATES.get(test_id) if test_id else None
        if test is not None:
            design = [[Lit(f"Design: {test.design} {test.treatment_and_holdout}")],
                      [Lit(f"Rollout: {test.rollout} Randomization: {test.randomization_unit}")],
                      [Lit(f"Judge by {test.primary_metric} Guardrails: {test.guardrails}")]]
        else:
            design = [[Lit("Investigate the defining metric against logs and a comparable baseline.")],
                      [Lit("Confirm prerequisites before changing traffic or prices.")],
                      [Lit("Run a small reversible holdout only if the pattern survives reconciliation.")]]
        if r.prerequisite_codes:
            design[0].append(Lit(" Before starting: " + " ".join(
                dict.fromkeys(T.PREREQUISITES[p] for p in r.prerequisite_codes))))
        terms = {a.parameter_key: a for a in (self.assumption(i) for i in r.assumption_ids)}
        if test_id in T.RANDOMIZED_TESTS and {"test_allocation", "test_min_days", "test_profit_stop_loss"} <= set(terms):
            design[1] += [Lit(" Allocation "),
                          Val(format_value(terms["test_allocation"].value, Unit.FRACTION),
                              terms["test_allocation"].assumption_id, RenderRole.ASSUMPTION),
                          Lit("; minimum duration "),
                          Val(format_value(terms["test_min_days"].value, Unit.DAYS),
                              terms["test_min_days"].assumption_id, RenderRole.ASSUMPTION),
                          Lit("; stop on total-profit loss of "),
                          Val(format_value(terms["test_profit_stop_loss"].value, Unit.FRACTION),
                              terms["test_profit_stop_loss"].assumption_id, RenderRole.ASSUMPTION),
                          Lit(".")]
        out.append(("Test design", design))

        refs: list[Line] = [
            [Lit(f"Confidence in the observation: {T.CONFIDENCE_TEXT[r.confidence_in_observation]}"
                 f"; {T.INTERVENTION_UNPROVEN}")],
            [Lit("Recommendation "), self.ref(rec.recommendation_id), Lit(" · candidate "),
             self.ref(r.candidate_id)]
            + ([Lit(f" · {T.FAMILY_LABELS[cand.family]}")] if cand is not None else [])
            + ([Lit(" · found by the model, not a detector rule")]
               if r.origin == "model_proposed" else [])]
        absorbed = self.card_merges.get(rec.recommendation_id, [])
        if absorbed:
            refs.append([Lit("Same traffic; collapsed duplicate findings "), *self.refs(absorbed),
                         Lit(" into this simpler description.")])
        if r.overlap_group_ids:
            refs.append([Lit("Overlaps other recommendations in "), *self.refs(r.overlap_group_ids),
                         Lit("; their impacts are not additive.")])
        merged = [m for m in self.merged_sources(rec.recommendation_id)]
        if merged:
            refs.append([Lit("Also related by the model ranking to "), *self.refs(merged),
                         Lit("; shown separately here, so impacts must not be added.")])
        plan = self.plans[r.candidate_id]
        if plan.assumption:
            refs.append([Lit("Planning assumption: "),
                         Val(format_value(plan.assumption.value, plan.assumption.unit),
                             plan.assumption.assumption_id, RenderRole.ASSUMPTION),
                         Lit(" — " + plan.assumption.label + ". "), self.ref(plan.assumption.assumption_id),
                         Lit("; estimate facts "), *self.refs([f.fact_id for f in plan.results]),
                         Lit("; historical inputs "),
                         *self.refs(plan.results[0].provenance.parent_fact_ids)])
        if plan.scenario_id:
            scenario = self.scenario(plan.scenario_id)
            refs.append([Lit("Estimate scenario: "), self.ref(plan.scenario_id),
                         Lit("; historical inputs "), *self.refs(scenario.historical_fact_ids),
                         Lit("; assumptions "), *self.refs(scenario.assumption_ids)])
            for aid in scenario.assumption_ids:
                a = self.assumption(aid)
                refs.append([Lit(a.label + ": "),
                             Val(format_value(a.value, a.unit), aid, RenderRole.ASSUMPTION)])
        if plan.results:
            refs.append([Lit("Weekly estimates rescale the observed period to seven days; they are conditional, not forecasts. Additional-traffic estimates exclude unknown displaced profit.")])
        out.append(("Confidence and references", refs))
        return out

    def merged_sources(self, recommendation_id: str) -> list[str]:
        ranking = self.run.ranking
        if ranking is None:
            return []
        return [s for m in ranking.merge_instructions
                if m.target_recommendation_id == recommendation_id
                for s in m.source_recommendation_ids]

    # Deterministic leads (no validated recommendations).

    def deterministic_leads(self) -> list[Line]:
        out: list[Line] = []
        leads = [c for c in self.b.candidates.values() if c.eligibility.value == "eligible"]
        for cand in leads[:MAX_CARDS]:
            parts = [self.value(self.fact(i)) for i in cand.trigger_fact_ids[:1]]
            metric = T.METRIC_LABELS[self.fact(cand.trigger_fact_ids[0]).metric]
            out.append([Lit(f"{T.FAMILY_LABELS[cand.family]} (detector lead, not model-assessed): "),
                        *self.segment(cand.segment_id), Lit(f"; {metric} "), *_join(parts, ", ")])
        return out

    # Charts.

    def charts(self, cards: list[Card]) -> list[Chart]:
        charts: list[Chart] = []
        rows: list[ChartRow] = []
        for card in cards:
            if card.compare is None:
                continue
            target, baseline = card.compare
            assert target.value is not None and baseline.value is not None
            scale = max(abs(target.value), abs(baseline.value)) or Decimal(1)
            rows.append(ChartRow(
                label=[Lit("#"), card.rank, Lit(f" {_sentence(_metric_name(target))}")],
                bars=[Bar("This slice", self.bar_val(target), float(abs(target.value) / scale),
                          target.value < 0, True),
                      Bar("Baseline", self.bar_val(baseline, RenderRole.BASELINE),
                          float(abs(baseline.value) / scale), baseline.value < 0)]))
        if rows:
            charts.append(Chart(
                "Each recommended slice against its baseline",
                "Bars compare the slice with the whole account or matched peers on the metric "
                "behind its recommendation; each row has its own scale.", rows))

        sized = [(c, *c.size) for c in cards if c.size[0] and c.size[1]
                 and c.size[0].value is not None and c.size[1].value is not None]
        if sized:
            top_req = max((req.value for _, req, _ in sized), default=Decimal(1)) or Decimal(1)
            top_rev = max((rev.value for _, _, rev in sized), default=Decimal(1)) or Decimal(1)
            size_rows = [ChartRow(
                label=[Lit("#"), card.rank, Lit(" "), *card.opportunity],
                bars=[Bar("Requests", self.bar_val(req), float(abs(req.value) / top_req)),
                      Bar("Revenue", self.bar_val(rev), float(abs(rev.value) / top_rev),
                          emphasis=True)]) for card, req, rev in sized]
            charts.append(Chart(
                "Requests and revenue by recommended slice",
                "How much traffic each slice carries and what it earns; requests and revenue "
                "each use their own scale.", size_rows))
        return charts

    def bar_val(self, fact: Fact, role: RenderRole = RenderRole.OBSERVATION) -> Val:
        return Val(format_value(fact.value, fact.unit), fact.fact_id, role)

    # Appendix.

    def appendix(self, ranked: list[ValidatedRecommendation]) -> list[tuple[str, list[Line]]]:
        quality = self.b.quality
        out: list[tuple[str, list[Line]]] = []

        findings: list[Line] = []
        seen_rules = set()
        for severity in (Severity.FATAL, Severity.WARNING, Severity.NOTICE):
            for issue in (i for i in quality.issues if i.severity is severity):
                if issue.rule_id in seen_rules:
                    continue
                seen_rules.add(issue.rule_id)
                line: Line = [Lit(f"{T.SEVERITY_LABELS[severity]} · "), *self.caveat(issue),
                              Lit(" Scope: ")]
                line += _join([self.segment(s, strict=False) for s in issue.segment_ids], "; ")
                if issue.period_ids:
                    line += [Lit(" — ")] + _join(
                        [self.period(p, strict=False) for p in issue.period_ids], "; ")
                line += [Lit(" · rule "), Val(issue.rule_id.value, issue.rule_id.value,
                                              RenderRole.WARNING),
                         Lit(" · issue "), self.ref(issue.issue_id, RenderRole.WARNING)]
                line += [Lit(". Full issue scopes and measurements are retained in quality.json and the full audit report.")]
                findings.append(line)
        not_testable = [r.rule_id for r in quality.rule_results
                        if r.outcome.value == "not_testable"]
        if not_testable:
            findings.append([Lit("Not testable with this input (unknown, not passed): "),
                             *_join([[Val(r.value, r.value, RenderRole.WARNING)]
                                     for r in not_testable], ", ")])
        out.append(("Data-quality findings", findings or [[Lit("No data-quality rule "
                                                                "triggered.")]]))

        counts: Line = [Lit("Validated recommendations "),
                        Val(format_value(Decimal(len([
                            r for r in self.run.recommendations if _actionable(r.recommendation)
                        ])), Unit.COUNT), self.run_id),
                        Lit("; model outputs rejected by validation "),
                        Val(format_value(Decimal(len(self.run.validation_failures)), Unit.COUNT),
                            self.run_id),
                        Lit("; candidates flagged by deterministic detectors "),
                        Val(format_value(Decimal(len(self.b.candidates)), Unit.COUNT),
                            self.run_id), Lit(".")]
        out.append(("Validation summary", [counts, *self.unselected(ranked), *self.excluded()]))

        selection_note = []
        if self.run.manifest.model_call_counts.get("exposure_selection"):
            selection_note = [[Lit(
                "Final selection is by code: equivalent traffic is collapsed, with at most two "
                "cards per supply. Available detector families, countries and sizes are covered "
                "before filling remaining slots. Eligible findings precede exploratory ones, "
                "ordered by estimated dollar gain. Unknown gains are not invented; unselected "
                "findings and duplicate evidence remain in the audit. Do not add overlapping impacts."
            )]]
        out.append(("Method and limits", [
            *selection_note,
            [Lit("Python measures every quantity; the self-hosted model only selects typed "
                 "evidence, tests and codes; code validates each reference and renders every "
                 "number.")],
            [Lit("Rates are ratios of additive sums; revenue per million requests is the "
                 "export's sRPM scale.")],
            [Lit(T.GENERAL_LIMIT)], [Lit(T.NO_PORTFOLIO_SUM)], [Lit(T.CURRENCY_NOTE)]]))

        m = self.run.manifest
        meta: list[Line] = []
        for label, value in (
            ("Run ID", m.run_id), ("Generated at", m.generated_at.isoformat()),
            ("Input SHA-256", m.input_sha256), ("Model", m.model_id),
            ("Model revision", m.model_revision), ("Model profile hash", m.model_profile_hash),
            ("Analytics config hash", m.analytics_config_hash), ("Prompt hash", m.prompt_hash),
            ("Tool version", m.tool_version), ("Assumption hash", m.assumption_hash),
            ("Run namespace", m.run_namespace), ("Schema version", m.schema_version),
        ):
            if value is None:
                meta.append([Lit(f"{label}: none")])
            else:
                shown = _plain(value, 120) if label == "Model" else value
                meta.append([Lit(f"{label}: "), Val(shown, self.run_id, untrusted=True)])
        out.append(("Run details", meta))
        return out

    def unselected(self, ranked: list[ValidatedRecommendation]) -> list[Line]:
        shown = {r.recommendation_id for r in ranked}
        merged = {s for rid in shown for s in self.merged_sources(rid)}
        merged.update(s for rid in shown for s in self.card_merges.get(rid, []))
        out: list[Line] = []
        for rec in self.run.recommendations:
            r = rec.recommendation
            if not _actionable(r) or rec.recommendation_id in shown | merged:
                continue
            out.append([Lit("Validated but not selected for the short report: "),
                        *self.segment(r.segment_id), Lit(" "), self.ref(rec.recommendation_id)])
        return out

    def excluded(self) -> list[Line]:
        out: list[Line] = []
        for cand in self.b.candidates.values():
            if cand.eligibility.value == "eligible":
                continue
            line: Line = [Lit(f"{T.FAMILY_LABELS[cand.family]}, "
                              f"{T.ELIGIBILITY_TEXT[cand.eligibility]}: "),
                          *self.segment(cand.segment_id)]
            if cand.rejection_reason:
                line += [Lit(" — "), Val(_plain(cand.rejection_reason, _MAX_REASON),
                                         cand.candidate_id, untrusted=True)]
            out.append([*line, Lit(" "), self.ref(cand.candidate_id)])
        for rec in self.run.recommendations:
            r = rec.recommendation
            if _actionable(r):
                continue
            verb = ("merged into another recommendation" if r.decision is Decision.MERGE
                    else "rejected by the model")
            out.append([Lit(f"Candidate {verb}: "), *self.segment(r.segment_id), Lit(" "),
                        self.ref(r.candidate_id)])
        for analysis in self.run.analyses:
            if analysis.status.value != "complete":
                out.append([Lit(f"{_sentence(T.ANALYSIS_STATUS_TEXT[analysis.status])}: "
                                "candidate "), self.ref(analysis.candidate_id)])
        known = known_source_ids(self.run)
        for key, errors in sorted(self.run.validation_failures.items()):
            line = [Lit("Rejected by validation: ")]
            line += [self.ref(key)] if key in self.b.candidates else [Lit(_plain(key))]
            line.append(Lit(" — "))
            for n, err in enumerate(errors):
                if n:
                    line.append(Lit("; "))
                line.append(Lit(T.VALIDATION_CODE_TEXT[err.code]))
                if err.ref:
                    source = err.ref if err.ref in known else (
                        key if key in self.b.candidates else self.run_id)
                    line += [Lit(" "), Val(_plain(err.ref), source, ref=True)]
            out.append(line)
        return out


# --- Markdown ----------------------------------------------------------------


class _Md:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.spans: list[RenderSpan] = []

    def text(self, s: str) -> None:
        self.parts.append(s)
        self.length += len(s)

    def line(self, s: str = "") -> None:
        self.text(s + "\n")

    def inline(self, parts: Line) -> None:
        for part in parts:
            if isinstance(part, Lit):
                self.text(part.s)
                continue
            shown = _MD_SPECIAL.sub(r"\\\1", part.s) if part.untrusted else part.s
            if part.ref:
                self.text("`")
            self.spans.append(RenderSpan(start=self.length, end=self.length + len(shown),
                                         source_id=part.src, role=part.role))
            self.text(shown)
            if part.ref:
                self.text("`")


def _markdown(page: Page) -> RenderedReport:
    d = _Md()
    d.line(f"# {T.REPORT_TITLE}")
    d.line()
    d.line(f"> **{page.status_text}**")
    for extra in page.status_extra:
        d.line(">")
        d.text("> ")
        d.inline(extra)
        d.line()
    d.line()
    d.text("**Account:** ")
    d.inline(page.account)
    if page.account_period:
        d.text(" — ")
        d.inline(page.account_period)
    d.line()
    d.line()
    if page.caveats:
        d.line("## Caveats that affect these conclusions")
        d.line()
        for caveat in page.caveats:
            d.text("- ")
            d.inline(caveat)
            d.line()
        if page.more_caveats:
            d.text("- ")
            d.inline(page.more_caveats)
            d.line()
        d.line()
    for notice in page.notice:
        d.inline(notice)
        d.line()
        d.line()
    if page.cards:
        d.line("## Recommendations")
        d.line()
    for card in page.cards:
        d.text("### ")
        d.inline([card.rank])
        d.text(f". {card.verb}")
        if card.ai:
            d.text(f" [{T.FOUND_BY_AI}]")
        d.line()
        d.line()
        d.text("- **Opportunity:** ")
        d.inline(card.opportunity)
        d.text(" — ")
        d.inline(card.period)
        d.line()
        d.text("- **Current:** ")
        for n, (label, value) in enumerate(card.current):
            d.text((" · " if n else "") + f"{label.lower()} ")
            d.inline(value)
        d.line()
        d.line("- **Observation:**")
        for line in card.observation:
            d.text("  - ")
            d.inline(line)
            d.line()
        d.text(f"- **Recommendation:** {card.verb}. ")
        d.inline(card.action)
        d.line()
        d.text("- **Estimated opportunity" + (" (estimate):** " if card.has_estimate else ":** "))
        d.inline(card.estimate)
        d.line()
        d.line("- **Details:**")
        d.line()
        d.line("<details><summary>Details</summary>")
        d.line()
        for title, lines in card.details:
            d.line(f"  - {title}")
            for line in lines:
                d.text("    - ")
                d.inline(line)
                d.line()
        d.line("</details>")
        d.line()
    if page.leads:
        d.line("## Detector leads")
        d.line()
        for lead in page.leads:
            d.text("- ")
            d.inline(lead)
            d.line()
        d.line()
    for chart in page.charts:
        d.line(f"## {chart.title}")
        d.line()
        d.line(chart.caption)
        d.line()
        for row in chart.rows:
            d.text("- ")
            d.inline(row.label)
            d.text(": ")
            for n, bar in enumerate(row.bars):
                d.text((", " if n else "") + f"{bar.label.lower()} ")
                d.inline([bar.value])
            d.line()
        d.line()
    d.line("## Notes")
    d.line()
    for note in page.notes:
        d.text("- ")
        d.inline(note)
        d.line()
    d.line()
    d.line("## Appendix")
    d.line()
    for title, lines in page.appendix:
        d.line(f"### {title}")
        d.line()
        for line in lines:
            d.text("- ")
            d.inline(line)
            d.line()
        d.line()
    return RenderedReport(markdown="".join(d.parts), spans=d.spans)


# --- HTML --------------------------------------------------------------------

_CSS = """
:root { color-scheme: light; --page:#f9f9f7; --card:#fff; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#6f6d68; --line:rgba(11,11,11,.12); --grid:#e1e0d9; --accent:#2a78d6;
  --accent-wash:#e8f1fc; --warn:#7a4f00; --warn-wash:#fff4dc; --warn-line:#f1d9a6;
  --bad:#d03b3b; --base:#b9b7ae; --ai:#6250d6; --ai-wash:#f0eefc;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
* { box-sizing: border-box; }
.rows > div > *, .stats > div, .bar > * { min-width: 0; }
body { overflow-wrap: anywhere; }
body { margin:0; background:var(--page); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.page { max-width:900px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:28px 0 10px; }
h3 { font-size:16px; margin:0; } h4 { font-size:13px; margin:12px 0 4px; color:var(--ink2); }
p { margin:0 0 .6em; } ul { margin:.2em 0 .6em; padding-left:1.2em; } li { margin:.2em 0; }
code { font:12px var(--mono); word-break:break-all; }
.sub { color:var(--ink2); margin:0 0 14px; }
.status { border-radius:8px; padding:8px 12px; font-size:13px; margin-bottom:14px;
  background:var(--card); border:1px solid var(--line); color:var(--ink2); }
.status.warn { background:var(--warn-wash); border-color:var(--warn-line); color:var(--warn);
  font-weight:600; }
.account { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; font-size:16px; font-weight:600; }
.account small { display:block; font-weight:400; color:var(--muted); font-size:12px; }
.caveats { background:var(--warn-wash); border:1px solid var(--warn-line); border-radius:10px;
  padding:10px 14px; margin-top:12px; font-size:13.5px; }
.caveats h2 { font-size:14px; margin:0 0 4px; color:var(--warn); }
.caveats ul { list-style:none; padding:0; margin:0; }
.caveats li { margin:6px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin:12px 0; }
.card header { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:6px; }
.num { width:26px; height:26px; border-radius:50%; background:var(--ink); color:#fff;
  display:grid; place-items:center; font-weight:700; font-size:13px; flex:none; }
.badge { font-size:12px; font-weight:600; padding:2px 8px; border-radius:999px;
  background:var(--ai-wash); color:var(--ai); cursor:help; }
.verb { font-size:12px; font-weight:700; padding:2px 8px; border-radius:6px;
  background:var(--accent-wash); color:#184f95; }
.opp { font-weight:600; }
.period { color:var(--muted); font-size:12.5px; }
.stats { display:flex; flex-wrap:wrap; gap:8px 22px; margin:8px 0; }
.stats div { min-width:110px; } .stats b { display:block; font-size:12px; color:var(--muted);
  font-weight:500; } .stats span.big { font-size:17px; font-weight:650; }
.rows { border-top:1px solid var(--grid); margin-top:8px; padding-top:6px; }
.rows > div { display:grid; grid-template-columns:130px 1fr; gap:8px; padding:4px 0;
  font-size:14px; }
.rows .k { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.03em;
  color:var(--ink2); padding-top:2px; }
.rows .est .k { color:#1c5cab; } .rows .est { background:#eaf2fd; border-radius:6px;
  padding:6px 8px; margin-top:4px; }
.rows ul { margin:0; padding-left:1.1em; }
.v { border-bottom:1px dotted var(--axis, #c3c2b7); cursor:help; }
details { margin-top:8px; font-size:13px; } summary { cursor:pointer; color:var(--accent);
  font-weight:600; }
.chart { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; margin:12px 0; } .chart p { color:var(--muted); font-size:12.5px; }
.crow { margin:10px 0; } .crow .cl { font-size:13px; font-weight:600; }
.bar { display:grid; grid-template-columns:78px 1fr auto; gap:8px; align-items:center;
  font-size:12.5px; margin:3px 0; }
.track { height:10px; background:#f0efec; border-radius:4px; position:relative; }
.fill { height:10px; border-radius:4px; background:var(--base); }
.fill.em { background:var(--accent); } .fill.neg { background:var(--bad); }
.notes, .app { font-size:13px; color:var(--ink2); }
@media (max-width:560px) { .rows > div { grid-template-columns:1fr; gap:0; }
  .bar { grid-template-columns:64px 1fr auto; } }
"""


def _h(s: str) -> str:
    return html.escape(s, quote=True)


def _inline_html(parts: Line) -> str:
    out = []
    for part in parts:
        if isinstance(part, Lit):
            out.append(_h(part.s))
            continue
        tag = "code" if part.ref else "span"
        out.append(f'<{tag} class="v" data-src="{_h(part.src)}" title="{_h(part.src)}">'
                   f"{_h(part.s)}</{tag}>")
    return "".join(out)


def _html_page(page: Page) -> str:
    o: list[str] = []
    w = o.append
    w("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
      "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
      f"<title>{_h(T.REPORT_TITLE)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
      "<div class=\"page\">\n")
    w(f"<h1>{_h(T.REPORT_TITLE)}</h1>\n<p class=\"sub\">{_h(T.SHORT_SUBTITLE)}</p>\n")
    w(f'<div class="status{"" if page.status_ok else " warn"}">{_h(page.status_text)}')
    for extra in page.status_extra:
        w(f"<br>{_inline_html(extra)}")
    w("</div>\n")
    w(f'<div class="account">{_inline_html(page.account)}'
      f'<small>{_inline_html(page.account_period)}</small></div>\n')
    if page.caveats:
        w('<section class="caveats"><h2>Caveats that affect these conclusions</h2><ul>')
        for caveat in page.caveats:
            w(f"<li>{_inline_html(caveat)}</li>")
        if page.more_caveats:
            w(f"<li>{_inline_html(page.more_caveats)}</li>")
        w("</ul></section>\n")
    for notice in page.notice:
        w(f"<p><b>{_inline_html(notice)}</b></p>\n")
    if page.cards:
        w("<h2>Recommendations</h2>\n")
    for card in page.cards:
        w('<article class="card">\n<header>')
        w(f'<span class="num">{_inline_html([card.rank])}</span>')
        w(f'<span class="verb">{_h(card.verb)}</span>')
        if card.ai:
            w(f'<span class="badge" title="{_h(T.FOUND_BY_AI_HINT)}">{_h(T.FOUND_BY_AI)}</span>')
        w("</header>\n")
        w(f'<div class="opp">{_inline_html(card.opportunity)}</div>'
          f'<div class="period">{_inline_html(card.period)}</div>\n')
        w('<div class="stats">')
        for label, value in card.current:
            w(f'<div><b>Current {_h(label.lower())}</b><span class="big">'
              f"{_inline_html(value)}</span></div>")
        w("</div>\n<div class=\"rows\">\n")
        w('<div><span class="k">Observation</span><ul>'
          + "".join(f"<li>{_inline_html(line)}</li>" for line in card.observation)
          + "</ul></div>\n")
        w(f'<div><span class="k">Recommendation</span><span><b>{_h(card.verb)}.</b> '
          f"{_inline_html(card.action)}</span></div>\n")
        label = "Estimated opportunity" + (" (estimate)" if card.has_estimate else "")
        w(f'<div class="est"><span class="k">{_h(label)}</span>'
          f"<span>{_inline_html(card.estimate)}</span></div>\n")
        w("</div>\n<details><summary>Details</summary>\n")
        for title, lines in card.details:
            w(f"<h4>{_h(title)}</h4><ul>"
              + "".join(f"<li>{_inline_html(line)}</li>" for line in lines) + "</ul>\n")
        w("</details>\n</article>\n")
    if page.leads:
        w("<h2>Detector leads</h2><ul>"
          + "".join(f"<li>{_inline_html(lead)}</li>" for lead in page.leads) + "</ul>\n")
    for chart in page.charts:
        w(f'<section class="chart"><h3>{_h(chart.title)}</h3><p>{_h(chart.caption)}</p>\n')
        for row in chart.rows:
            w(f'<div class="crow"><div class="cl">{_inline_html(row.label)}</div>')
            for bar in row.bars:
                cls = "fill" + (" em" if bar.emphasis else "") + (" neg" if bar.negative else "")
                w(f'<div class="bar"><span>{_h(bar.label)}</span><div class="track">'
                  f'<div class="{cls}" style="width:{max(bar.fraction * 100, 1.0):.1f}%"></div>'
                  f"</div><span>{_inline_html([bar.value])}</span></div>")
            w("</div>\n")
        w("</section>\n")
    w('<section class="notes"><ul>'
      + "".join(f"<li>{_inline_html(note)}</li>" for note in page.notes) + "</ul></section>\n")
    w('<details class="app"><summary>Appendix: data quality, exclusions, method and run '
      "details</summary>\n")
    for title, lines in page.appendix:
        w(f"<h4>{_h(title)}</h4><ul>"
          + "".join(f"<li>{_inline_html(line)}</li>" for line in lines) + "</ul>\n")
    w("</details>\n</div>\n</body>\n</html>\n")
    return "".join(o)


# --- Public API and audit ----------------------------------------------------


def build_summary(run: ValidatedRun) -> ShortReport:
    """Render the short report without auditing it (``render_summary`` audits)."""
    page = _Builder(run).page()
    return ShortReport(report=_markdown(page), html=_html_page(page))


class _HtmlAudit(HTMLParser):
    """Collect digit-bearing text that no ``data-src`` element covers, and the sources."""

    VOID = frozenset({"meta", "br", "hr", "link", "img", "input"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.uncovered: list[str] = []
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        src = dict(attrs).get("data-src")
        if src is not None:
            self.sources.append(src)
        if tag not in self.VOID:
            covered = src is not None or (bool(self.stack) and self.stack[-1][1])
            self.stack.append((tag, covered))

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data: str) -> None:
        if any(t in ("style", "script") for t, _ in self.stack):
            return
        if self.stack and self.stack[-1][1]:
            return
        for phrase in STATIC_ALLOWLIST:
            data = data.replace(phrase, " ")
        self.uncovered.extend(m.group() for m in re.finditer(r"\S*\d\S*", data))


def audit_summary(short: ShortReport, run: ValidatedRun) -> RenderAudit:
    """Digits need a resolvable source in both outputs, and a replay must match exactly."""
    md = short.report.markdown
    covered = bytearray(len(md))
    invalid = False
    for span in short.report.spans:
        if not 0 <= span.start < span.end <= len(md):
            invalid = True
            continue
        covered[span.start : span.end] = b"\x01" * (span.end - span.start)
    for phrase in STATIC_ALLOWLIST:
        for m in re.finditer(re.escape(phrase), md):
            covered[m.start() : m.end()] = b"\x01" * (m.end() - m.start())
    uncovered = [m.group() for m in re.finditer(r"\S*\d\S*", md)
                 if any(md[i].isdigit() and not covered[i] for i in range(m.start(), m.end()))]
    parser = _HtmlAudit()
    parser.feed(short.html)
    parser.close()
    uncovered += [f"html:{u}" for u in parser.uncovered]
    known = known_source_ids(run)
    unresolved = sorted({s.source_id for s in short.report.spans if s.source_id not in known}
                        | {s for s in parser.sources if s not in known})
    if invalid:
        unresolved.append("invalid_span_bounds")
    if short != build_summary(run):
        unresolved.append("render_replay_mismatch")
    return RenderAudit(tuple(uncovered), tuple(unresolved))


def render_summary(run: ValidatedRun) -> ShortReport:
    """The short report (Markdown plus HTML); refuses to return one that fails its audit."""
    short = build_summary(run)
    audit = audit_summary(short, run)
    if not audit.ok:
        raise RenderError(
            f"summary audit failed: {len(audit.uncovered)} uncovered quantities, "
            f"{len(audit.unresolved)} unresolved sources")
    return short


__all__ = ["ShortReport", "audit_summary", "build_summary", "render_summary"]
