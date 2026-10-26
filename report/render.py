"""Markdown/JSON rendering where every quantity comes from a registry object.

The renderer composes reviewed wording from ``report.text`` with values read
from facts, assumptions, scenarios and the run manifest. Each value is
written through ``_Doc.value``, which records a span (offsets, source ID,
role) in the audit map. After rendering, ``audit_report`` checks that every
digit in the Markdown lies inside a span whose source resolves in the run;
``render_report`` refuses to return a report that fails that check.

Warnings come first, then the summary, then recommendations in the fixed
test form, then the audit of excluded output.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from contracts.enums import (
    Decision,
    FactKind,
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
    Period,
    QualityIssue,
    RenderedReport,
    RenderSpan,
    Scenario,
    ValidatedRecommendation,
)
from contracts.runtime import EvidenceBundle, ValidatedRun
from contracts.templates import OBSERVATION_TEMPLATES, ObservationTemplateId
from pipeline.quality import render_message
from report import text as T

# Static phrases that contain digits but are not quantities.
STATIC_ALLOWLIST: tuple[str, ...] = ("SHA-256",)

_SEVERITY_ORDER = (Severity.FATAL, Severity.WARNING, Severity.NOTICE)
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#|<>~$!])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_MAX_LABEL = 48
_MAX_REASON = 160
_MAX_MESSAGE = 600
_ID_REF = re.compile(r"^[A-Z]_[0-9a-f]{16}$")

ACCOUNT_SUMMARY_METRICS = (
    "row_count",
    "observed_days",
    "requests",
    "impressions",
    "revenue",
    "cost",
    "profit",
    "margin",
    "revenue_per_million_requests",
    "profit_per_million_requests",
    "profit_reconciliation_residual",
)


class RenderError(ValueError):
    """The run references something the renderer cannot resolve; nothing is rendered."""


# --- Value formatting ------------------------------------------------------


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _grouped(value: Decimal, places: int) -> str:
    return f"{_quantize(value, places):,.{places}f}"


def _money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${_grouped(abs(value), 2)}"


def format_value(value: Decimal | None, unit: Unit) -> str:
    """Display text for a typed value; the only place report numbers are formatted."""
    if value is None:
        return "undefined"
    match unit:
        case Unit.COUNT:
            places = 0 if value == value.to_integral_value() else 2
            return _grouped(value, places)
        case Unit.FRACTION:
            pct = value * 100
            places = 1 if pct == 0 or abs(pct) >= 1 else 2
            return f"{_grouped(pct, places)}%"
        case Unit.RATIO:
            places = 2 if value == 0 or abs(value) >= Decimal("0.1") else 4
            return f"{_grouped(value, places)}×"
        case Unit.EXPORT_DOLLAR:
            return _money(value)
        case Unit.EXPORT_DOLLAR_PER_THOUSAND_IMPRESSIONS:
            return f"{_money(value)} per thousand impressions"
        case Unit.EXPORT_DOLLAR_PER_MILLION_REQUESTS:
            return f"{_money(value)} per million requests"
        case Unit.EXPORT_PRICE_AS_EXPORTED:
            return f"{_grouped(value, 2)} (export price scale)"
        case Unit.PERCENTAGE_POINTS:
            # Stored on the fraction scale: 0.05 is five percentage points.
            return f"{_grouped(value * 100, 1)} percentage points"
        case Unit.DAYS:
            places = 0 if value == value.to_integral_value() else 1
            word = "day" if value == 1 else "days"
            return f"{_grouped(value, places)} {word}"
    raise RenderError(f"no display rule for unit {unit}")


def clean_label(raw: str, max_length: int = _MAX_LABEL) -> str:
    """Untrusted text (labels, reasons): no control characters or Markdown, bounded length."""
    flat = " ".join(_CONTROL.sub(" ", raw).split())
    if len(flat) > max_length:
        flat = flat[: max_length - 1] + "…"
    return _MD_SPECIAL.sub(r"\\\1", flat) or "(blank)"


# --- Document with an audit map --------------------------------------------


class _Doc:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self.spans: list[RenderSpan] = []

    def text(self, s: str) -> None:
        self._parts.append(s)
        self._length += len(s)

    def value(self, s: str, source_id: str, role: RenderRole) -> None:
        self.spans.append(
            RenderSpan(start=self._length, end=self._length + len(s), source_id=source_id,
                       role=role)
        )
        self.text(s)

    def line(self, s: str = "") -> None:
        self.text(s + "\n")

    def markdown(self) -> str:
        return "".join(self._parts)


# --- Audit -----------------------------------------------------------------


@dataclass(frozen=True)
class RenderAudit:
    uncovered: tuple[str, ...]
    unresolved: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.uncovered and not self.unresolved


def known_source_ids(run: ValidatedRun) -> frozenset[str]:
    b = run.bundle
    ids: set[str] = {run.manifest.run_id, *b.facts, *b.assumptions, *b.scenarios}
    ids.update(b.segments, b.periods, b.candidates, b.overlap_groups)
    ids.update(f.fact_id for f in b.quality.facts)
    for issue in b.quality.issues:
        ids.update((issue.issue_id, *issue.segment_ids, *issue.period_ids,
                    *issue.measured_fact_ids, *issue.threshold_fact_ids,
                    *issue.affected_candidate_ids))
        ids.update(i for i in (issue.request_exposure_fact_id, issue.revenue_exposure_fact_id) if i)
    ids.update(r.value for r in QualityRuleId)
    ids.update(r.recommendation_id for r in run.recommendations)
    for analysis in run.analyses:
        ids.add(analysis.candidate_id)
        ids.update(t.tool_invocation_id for t in analysis.tool_evidence)
    return frozenset(ids)


def audit_report(report: RenderedReport, run: ValidatedRun) -> RenderAudit:
    """Every digit must sit inside a span (or a static allowlisted phrase) whose source resolves."""
    md = report.markdown
    covered = bytearray(len(md))
    invalid_bounds = False
    for span in report.spans:
        if not 0 <= span.start < span.end <= len(md):
            invalid_bounds = True
            continue
        covered[span.start : span.end] = b"\x01" * (span.end - span.start)
    for phrase in STATIC_ALLOWLIST:
        for m in re.finditer(re.escape(phrase), md):
            covered[m.start() : m.end()] = b"\x01" * (m.end() - m.start())
    uncovered = []
    for m in re.finditer(r"\S*\d\S*", md):
        if any(md[i].isdigit() and not covered[i] for i in range(m.start(), m.end())):
            uncovered.append(m.group())
    known = known_source_ids(run)
    unresolved = sorted({s.source_id for s in report.spans if s.source_id not in known})
    if invalid_bounds:
        unresolved.append("invalid_span_bounds")
    # A known source ID is not permission to place arbitrary numbers beneath
    # it, change a role or exempt numeric-looking labels elsewhere. Replay the
    # deterministic renderer against this run's immutable objects and require
    # exact text AND spans. This also catches contaminated cached report text.
    expected = _Renderer(run).render()
    if report != expected:
        unresolved.append("render_replay_mismatch")
    return RenderAudit(tuple(uncovered), tuple(unresolved))


# --- Renderer --------------------------------------------------------------


def account_fact_groups(bundle: EvidenceBundle) -> list[tuple[str, str, list[Fact]]]:
    """Account totals: the longest full-input period, then each sensitivity cohort.

    Daily and other partial full-input periods belong to charts and warnings,
    not to the totals; cohorts are never mixed.
    """
    groups: dict[tuple[str, str], list[Fact]] = {}
    for fact in bundle.facts.values():
        if (
            fact.segment.filters
            or fact.kind not in (FactKind.MEASURED, FactKind.CALCULATED)
            or fact.metric.value not in ACCOUNT_SUMMARY_METRICS
        ):
            continue
        groups.setdefault((fact.segment.cohort_id, fact.period.period_id), []).append(fact)

    def key(item: tuple[tuple[str, str], list[Fact]]) -> tuple[bool, int, str]:
        (cohort, period_id), _ = item
        period = bundle.periods.get(period_id)
        return (cohort != "full_input", -len(period.included_dates) if period else 0, cohort)

    order = {m: n for n, m in enumerate(ACCOUNT_SUMMARY_METRICS)}
    out: list[tuple[str, str, list[Fact]]] = []
    for (cohort, period_id), facts in sorted(groups.items(), key=key):
        if cohort == "full_input" and any(c == "full_input" for c, _, _ in out):
            continue
        out.append((cohort, period_id, sorted(facts, key=lambda f: order[f.metric.value])))
    return out


class _Renderer:
    def __init__(self, run: ValidatedRun) -> None:
        self.run = run
        self.b = run.bundle
        self.doc = _Doc()
        self.run_id = run.manifest.run_id
        self.recs = {r.recommendation_id: r for r in run.recommendations}
        # Provisional quality facts, overridden by their promoted registry copies.
        self.issue_facts = {f.fact_id: f for f in self.b.quality.facts}
        self.issue_facts.update((f.fact_id, f) for f in self.b.facts.values())

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

    def ref(self, source_id: str, role: RenderRole = RenderRole.METADATA) -> None:
        self.doc.text("`")
        self.doc.value(source_id, source_id, role)
        self.doc.text("`")

    def refs(self, ids: Iterable[str], role: RenderRole = RenderRole.METADATA) -> None:
        for n, source_id in enumerate(ids):
            if n:
                self.doc.text(", ")
            self.ref(source_id, role)

    def segment(self, segment_id: str, strict: bool = True) -> None:
        """Segment label; quality issues may cite scope the evidence stage never built."""
        seg = self.b.segments.get(segment_id)
        if seg is None and not strict:
            self.doc.text("segment ")
            self.ref(segment_id, RenderRole.WARNING)
            return
        if seg is None:
            raise RenderError(f"unknown segment {segment_id}")
        if seg.filters:
            label = " · ".join(
                f"{T.DIMENSION_LABELS[d]} {clean_label(seg.filters[d])}"
                for d in T.DIMENSION_LABELS
                if d in seg.filters
            )
        else:
            label = T.ACCOUNT_LABEL
        if seg.cohort_id != "full_input":
            label += f" (cohort {clean_label(seg.cohort_id)})"
        self.doc.value(label, segment_id, RenderRole.METADATA)

    def period(self, period_id: str, strict: bool = True) -> None:
        period = self.b.periods.get(period_id)
        if period is None and not strict:
            self.doc.text("period ")
            self.ref(period_id, RenderRole.WARNING)
            return
        if period is None:
            raise RenderError(f"unknown period {period_id}")
        self.doc.value(period_label(period), period_id, RenderRole.METADATA)

    def fact_value(self, fact: Fact, role: RenderRole) -> None:
        if fact.value is None:
            reason = clean_label(fact.validity.reason or "no reason recorded", _MAX_REASON)
            self.doc.value(f"unavailable ({reason})", fact.fact_id, role)
            return
        self.doc.value(format_value(fact.value, fact.unit), fact.fact_id, role)
        if fact.validity.status is ValidityStatus.TAINTED:
            self.doc.text(f" ({T.VALIDITY_TEXT[ValidityStatus.TAINTED]})")

    def metric_fact(self, fact: Fact, role: RenderRole, with_scope: bool = True) -> None:
        """``<metric> for <segment>, <period>: <value> (from ...) [F_]``."""
        self.doc.text(f"{_sentence(T.METRIC_LABELS[fact.metric])}")
        if with_scope:
            self.doc.text(" for ")
            self.segment(fact.segment.segment_id)
            self.doc.text(", ")
            self.period(fact.period.period_id)
        self.doc.text(": ")
        self.fact_value(fact, role)
        self.denominators(fact, role)
        self.doc.text(" ")
        self.ref(fact.fact_id, role)

    def denominators(self, fact: Fact, role: RenderRole) -> None:
        if fact.kind is not FactKind.CALCULATED:
            return
        parents = [p for p in (self.b.facts.get(i) for i in fact.provenance.parent_fact_ids) if p]
        if not parents:
            return
        self.doc.text(" (from ")
        for n, parent in enumerate(parents):
            if n:
                self.doc.text(" and ")
            self.doc.text(f"{T.METRIC_LABELS[parent.metric]} ")
            self.fact_value(parent, role)
        self.doc.text(")")

    # Sections.

    def render(self) -> RenderedReport:
        self.header()
        fatal = self.warnings()
        self.summary()
        if fatal:
            self.doc.line("## Recommended tests and investigations")
            self.doc.line()
            self.doc.line(
                "A fatal data-quality issue blocks model recommendations. Only the quality "
                "report and diagnostic summary are shown."
            )
            self.doc.line()
        else:
            self.recommendations()
        self.audit_section()
        self.method()
        return RenderedReport(markdown=self.doc.markdown(), spans=self.doc.spans)

    def header(self) -> None:
        m, d = self.run.manifest, self.doc
        d.line(f"# {T.REPORT_TITLE}")
        d.line()
        d.line(f"> **{T.STATUS_BANNERS[self.run.status]}**")
        if self.run.status is RunStatus.CACHED_PREVIOUS:
            d.line(">")
            d.text("> Shown from a previous run generated at ")
            d.value(m.generated_at.isoformat(), self.run_id, RenderRole.METADATA)
            d.line(". Re-rendering does not change the inference generation time.")
            if m.model_id is None:
                d.line(">")
                d.line("> The cached run included no model analysis (deterministic only).")
        d.line()
        rows: list[tuple[str, str | None]] = [
            ("Run ID", m.run_id),
            ("Generated at", m.generated_at.isoformat()),
            ("Input SHA-256", m.input_sha256),
            ("Model", m.model_id),
            ("Model revision", m.model_revision),
            ("Model profile hash", m.model_profile_hash),
            ("Analytics config hash", m.analytics_config_hash),
            ("Prompt hash", m.prompt_hash),
            ("Tool version", m.tool_version),
            ("Assumption hash", m.assumption_hash),
            ("Run namespace", m.run_namespace),
            ("Schema version", m.schema_version),
        ]
        for label, value in rows:
            d.text(f"- {label}: ")
            if value is None:
                d.line("none (no model ran)" if label.startswith(("Model", "Prompt")) else "none")
            else:
                d.value(clean_label(value) if label == "Model" else value, self.run_id,
                        RenderRole.METADATA)
                d.line()
        d.line(f"- {T.CURRENCY_NOTE}")
        d.line()

    def warnings(self) -> bool:
        d, quality = self.doc, self.b.quality
        d.line("## Data-quality warnings")
        d.line()
        if not quality.issues:
            d.line("No data-quality rule triggered.")
            d.line()
        fatal = False
        for severity in _SEVERITY_ORDER:
            issues = [i for i in quality.issues if i.severity is severity]
            if not issues:
                continue
            fatal |= severity is Severity.FATAL
            d.line(f"### {T.SEVERITY_LABELS[severity]}s")
            d.line()
            by_rule: dict[QualityRuleId, list[QualityIssue]] = {}
            for issue in issues:
                by_rule.setdefault(issue.rule_id, []).append(issue)
            for rule_id in sorted(by_rule):
                group = by_rule[rule_id]
                rule = T.QUALITY_RULES[rule_id]
                d.text(f"- **{rule.title}** (")
                d.value(rule_id.value, rule_id.value, RenderRole.WARNING)
                d.line(")")
                for issue in group:
                    self.issue_detail(issue)
            d.line()
        not_testable = [r.rule_id for r in quality.rule_results
                        if r.outcome.value == "not_testable"]
        if not_testable:
            d.text("Not testable with this input (unknown, not passed): ")
            for n, rule_id in enumerate(not_testable):
                if n:
                    d.text(", ")
                d.value(rule_id.value, rule_id.value, RenderRole.WARNING)
            d.line(".")
            d.line()
        return fatal

    def issue_detail(self, issue: QualityIssue) -> None:
        """One issue: the quality module's code-rendered message, then scope and refs."""
        d = self.doc
        d.text("  - ")
        try:
            message = render_message(issue, self.issue_facts)
        except KeyError:  # template unknown to this build: fall back to the rule text
            message = None
        if message:
            d.value(clean_label(message, _MAX_MESSAGE), issue.issue_id, RenderRole.WARNING)
        else:
            d.text(T.QUALITY_RULES[issue.rule_id].consequence)
        d.text(" ")
        d.text("Scope: ")
        for n, segment_id in enumerate(issue.segment_ids):
            if n:
                d.text("; ")
            self.segment(segment_id, strict=False)
        if issue.period_ids:
            d.text(" — ")
            for n, period_id in enumerate(issue.period_ids):
                if n:
                    d.text("; ")
                self.period(period_id, strict=False)
        d.text(". Rows affected: ")
        d.value(format_value(Decimal(issue.row_count), Unit.COUNT), issue.issue_id,
                RenderRole.WARNING)
        if issue.inference_restrictions:
            restrictions = ", ".join(T.RESTRICTION_TEXT[r] for r in issue.inference_restrictions)
            d.text(f". Restrictions: {restrictions}")
        if issue.affected_candidate_ids:
            d.text(". Affected candidates: ")
            self.refs(issue.affected_candidate_ids)
        evidence = list(dict.fromkeys(
            i for i in (*issue.measured_fact_ids, *issue.threshold_fact_ids,
                        issue.request_exposure_fact_id, issue.revenue_exposure_fact_id)
            if i))
        if evidence:
            d.text(". Evidence: ")
            self.refs(evidence, RenderRole.WARNING)
        d.text(". Issue ")
        self.ref(issue.issue_id, RenderRole.WARNING)
        d.line()

    def summary(self) -> None:
        d = self.doc
        d.line("## Summary")
        d.line()
        groups = account_fact_groups(self.b)
        for n, (cohort, period_id, facts) in enumerate(groups):
            heading = "Account totals" if n == 0 else "Sensitivity view (not the whole account)"
            d.text(f"**{heading}** — ")
            self.segment(facts[0].segment.segment_id)
            d.text(", ")
            self.period(period_id)
            d.line(":")
            d.line()
            for fact in facts:
                d.text("- ")
                self.metric_fact(fact, RenderRole.OBSERVATION, with_scope=False)
                d.line()
            d.line()
        accepted = [r for r in self.run.recommendations if _is_actionable(r.recommendation)]
        d.text("Validation: validated recommendations ")
        d.value(str(len(accepted)), self.run_id, RenderRole.METADATA)
        d.text("; model outputs rejected by validation ")
        d.value(str(len(self.run.validation_failures)), self.run_id, RenderRole.METADATA)
        d.text("; candidates flagged by deterministic detectors ")
        d.value(str(len(self.b.candidates)), self.run_id, RenderRole.METADATA)
        d.line(".")
        d.line()
        ranking = self.run.ranking
        if ranking and ranking.summary_themes:
            d.line("**Themes** (model-selected, wording and values rendered by code):")
            d.line()
            for theme in ranking.summary_themes:
                d.text(f"- {T.SUMMARY_THEMES[theme.theme_code]}")
                for fact_id in theme.fact_ids:
                    d.text(" ")
                    self.metric_fact(self.fact(fact_id), RenderRole.OBSERVATION)
                    d.text(".")
                if theme.recommendation_ids:
                    d.text(" See ")
                    self.refs(theme.recommendation_ids)
                    d.text(".")
                d.line()
            d.line()

    def ordered_recommendations(self) -> tuple[list[tuple[ValidatedRecommendation, list[str]]],
                                               list[ValidatedRecommendation]]:
        actionable = {
            r.recommendation_id: r
            for r in self.run.recommendations
            if _is_actionable(r.recommendation)
        }
        ranking = self.run.ranking
        if ranking is None:
            return [(r, []) for r in actionable.values()], []
        merged_into: dict[str, list[str]] = {}
        absorbed: set[str] = set()
        for merge in ranking.merge_instructions:
            merged_into.setdefault(merge.target_recommendation_id, []).extend(
                merge.source_recommendation_ids
            )
            absorbed.update(merge.source_recommendation_ids)
        ranked = [
            (actionable[rid], merged_into.get(rid, []))
            for rid in ranking.ranked_recommendation_ids
            if rid in actionable
        ]
        shown = {rid for rid in ranking.ranked_recommendation_ids} | absorbed
        unranked = [r for rid, r in actionable.items() if rid not in shown]
        return ranked, unranked

    def recommendations(self) -> None:
        d = self.doc
        d.line("## Recommended tests and investigations")
        d.line()
        ranked, unranked = self.ordered_recommendations()
        if not ranked:
            d.line("No validated model recommendations are included in this run.")
            d.line()
            self.deterministic_leads()
            return
        if self.run.ranking is None:
            d.line("The final ranking did not complete; order follows candidate order.")
        elif self.run.manifest.model_call_counts.get("exposure_selection"):
            d.line("Final selection is by code using measured request exposure, not predicted "
                   "uplift; overlapping scopes are not selected together.")
            d.line()
        if len(ranked) < 5:
            d.line(
                "Fewer than five recommendations are supportable from this evidence; no "
                "further recommendations were invented."
            )
            d.line()
        for rank, (rec, merged) in enumerate(ranked, start=1):
            self.recommendation(rank, rec, merged)
        if unranked:
            d.line("### Validated but not selected by the final ranking")
            d.line()
            for rec in unranked:
                r = rec.recommendation
                d.text("- ")
                d.text(_title(r))
                if r.origin == "model_proposed":
                    d.text(" (model-proposed)")
                d.text(" — ")
                self.segment(r.segment_id)
                d.text(" ")
                self.ref(rec.recommendation_id)
                d.line()
            d.line()

    def deterministic_leads(self) -> None:
        d = self.doc
        leads = [c for c in self.b.candidates.values() if c.eligibility.value == "eligible"]
        d.line("### Flagged candidates (deterministic, not model-assessed)")
        d.line()
        if not leads:
            d.line("No eligible candidates were flagged.")
            d.line()
            return
        d.line(
            "These are detector outputs only. They are leads for investigation, not "
            "recommendations, and no model judged them."
        )
        d.line()
        for cand in leads:
            d.text(f"- **{T.FAMILY_LABELS[cand.family]}** — ")
            self.segment(cand.segment_id)
            d.text(", ")
            self.period(cand.period_id)
            d.text(". ")
            for n, fact_id in enumerate(cand.trigger_fact_ids):
                if n:
                    d.text("; ")
                self.metric_fact(self.fact(fact_id), RenderRole.OBSERVATION, with_scope=False)
            d.text(". Candidate ")
            self.ref(cand.candidate_id)
            d.line()
        d.line()

    def recommendation(self, rank: int, rec: ValidatedRecommendation, merged: list[str]) -> None:
        d, r = self.doc, rec.recommendation
        cand = self.b.candidates.get(r.candidate_id)
        d.text("### ")
        d.value(str(rank), rec.recommendation_id, RenderRole.METADATA)
        d.text(f". {_title(r)}: ")
        self.segment(r.segment_id)
        d.line()
        d.line()
        d.text("Recommendation ")
        self.ref(rec.recommendation_id)
        d.text(" · candidate ")
        self.ref(r.candidate_id)
        if cand is not None:
            d.text(f" · {T.FAMILY_LABELS[cand.family]}")
        if r.origin == "model_proposed":
            d.text(" · model-proposed (not detector-raised)")
        d.text(" · ")
        self.period(r.period_id)
        d.line()
        if merged:
            d.text("Merged with ")
            self.refs(merged)
            d.line("; the union scope was validated by code.")
        if r.overlap_group_ids:
            d.text("Overlaps other recommendations in ")
            self.refs(r.overlap_group_ids)
            d.line("; their impacts are not additive.")
        d.line()

        d.line("**What the data shows:**")
        d.line()
        for statement in r.observation_statements:
            d.text("- ")
            self.observation(statement)
            d.line()
        d.line()

        d.line("**What it does not prove:**")
        d.line()
        for fact_id in r.counter_evidence_fact_ids:
            d.text("- Counter-evidence: ")
            self.metric_fact(self.fact(fact_id), RenderRole.OBSERVATION)
            d.line()
        if cand is not None:
            for issue_id in cand.quality_issue_ids:
                issue = next((i for i in self.b.quality.issues if i.issue_id == issue_id), None)
                if issue is None:
                    continue
                rule = T.QUALITY_RULES[issue.rule_id]
                d.text(f"- {rule.title} (")
                d.value(issue.rule_id.value, issue.issue_id, RenderRole.WARNING)
                d.text(f"): {rule.consequence}")
                if issue.inference_restrictions:
                    d.text(" Restrictions: " + ", ".join(
                        T.RESTRICTION_TEXT[x] for x in issue.inference_restrictions) + ".")
                d.line()
        period = self.b.periods.get(r.period_id)
        if period is not None:
            d.line(f"- Period coverage: {T.COVERAGE_TEXT[period.coverage_status]}.")
        if cand is not None:
            d.line(f"- {T.FAMILY_LIMITS[cand.family]}")
        d.line(f"- {T.GENERAL_LIMIT}")
        d.line()

        d.text("**Hypothesis — unverified:** ")
        if r.hypothesis_codes:
            d.line(" ".join(T.HYPOTHESES[h] for h in r.hypothesis_codes) + " Not verified.")
        else:
            d.line("No hypothesis selected.")
        d.line()

        d.line("**Controlled experiment:**")
        d.line()
        if r.test_template_id is None:
            d.line("- Investigate first; no controlled test is proposed until the "
                   "prerequisites below are met.")
            test = None
        else:
            test = T.TEST_TEMPLATES[r.test_template_id]
            d.line(f"- Design: {test.design}")
            d.text("- Target eligibility: ")
            self.segment(r.segment_id)
            d.line(".")
            d.line(f"- Treatment and holdout: {test.treatment_and_holdout}")
            d.line(f"- Randomization unit: {test.randomization_unit}")
            d.line(f"- Rollout: {test.rollout}")
        if r.prerequisite_codes:
            d.line("- Feasibility prerequisites: "
                   + " ".join(T.PREREQUISITES[p] for p in r.prerequisite_codes))
        d.line()

        d.line("**How to judge it:**")
        d.line()
        if test is not None:
            d.line(f"- Primary: {test.primary_metric}")
            d.line(f"- Guardrails: {test.guardrails}")
        if r.success_metric_keys:
            d.line("- Success metrics: "
                   + ", ".join(T.METRIC_LABELS[k] for k in r.success_metric_keys) + ".")
        if test is None and not r.success_metric_keys:
            d.line("- Judge the investigation by whether it confirms or rules out the observed "
                   "pattern; no test outcome is forecast.")
        if r.assumption_ids:
            d.text("- Duration, allocation and stop rules (assumptions): ")
            for n, assumption_id in enumerate(r.assumption_ids):
                if n:
                    d.text("; ")
                a = self.assumption(assumption_id)
                d.text(f"{clean_label(a.label)} ")
                d.value(format_value(a.value, a.unit), a.assumption_id, RenderRole.ASSUMPTION)
            d.line(".")
        d.line()

        d.line("**Conditional impact if the test works:**")
        d.line()
        if not r.scenario_ids:
            d.line(f"- {T.NOT_ESTIMABLE}")
        for scenario_id in r.scenario_ids:
            self.scenario_block(self.scenario(scenario_id))
        d.line()

        d.line("**Test assumptions — not historical facts:**")
        d.line()
        if not r.assumption_ids:
            d.line("- None selected.")
        for assumption_id in r.assumption_ids:
            a = self.assumption(assumption_id)
            d.text(f"- {clean_label(a.label)}: ")
            d.value(format_value(a.value, a.unit), a.assumption_id, RenderRole.ASSUMPTION)
            d.text(" (approved bounds ")
            d.value(format_value(a.bounds.minimum, a.unit), a.assumption_id,
                    RenderRole.ASSUMPTION)
            d.text(" to ")
            d.value(format_value(a.bounds.maximum, a.unit), a.assumption_id,
                    RenderRole.ASSUMPTION)
            d.text(f"; set by {a.selector.value.replace('_', ' ')}; not observed) ")
            self.ref(a.assumption_id, RenderRole.ASSUMPTION)
            d.line()
        d.line()

        d.line("**Risks / additional data:**")
        d.line()
        for risk in r.risk_codes:
            d.line(f"- {T.RISKS[risk]}")
        for prereq in r.prerequisite_codes:
            d.line(f"- Needed: {T.PREREQUISITES[prereq]}")
        if not r.risk_codes and not r.prerequisite_codes:
            d.line("- No specific risks selected; the general limits above apply.")
        d.line()

        d.line(
            f"**Confidence in the observation:** "
            f"{T.CONFIDENCE_TEXT[r.confidence_in_observation]}; {T.INTERVENTION_UNPROVEN}"
        )
        d.line()

    def observation(self, statement: ObservationStatement) -> None:
        d = self.doc
        template = OBSERVATION_TEMPLATES[statement.template_id]
        if len(statement.fact_ids) != len(template.fact_roles):
            raise RenderError(f"{statement.template_id} needs {len(template.fact_roles)} facts")
        facts = dict(zip(template.fact_roles, (self.fact(i) for i in statement.fact_ids),
                         strict=True))
        match statement.template_id:
            case ObservationTemplateId.METRIC_VALUE:
                self.metric_fact(facts["target"], RenderRole.OBSERVATION)
            case ObservationTemplateId.METRIC_VS_BASELINE:
                target, baseline, comp = facts["target"], facts["baseline"], facts["comparison"]
                self.metric_fact(target, RenderRole.OBSERVATION)
                detail = comp.comparison
                relation = T.RELATION_WORDS[detail.relation] if detail else "compared with"
                d.text(f"; {relation} ")
                self.segment(baseline.segment.segment_id)
                d.text(": ")
                self.fact_value(baseline, RenderRole.BASELINE)
                self.denominators(baseline, RenderRole.BASELINE)
                d.text(" ")
                self.ref(baseline.fact_id, RenderRole.BASELINE)
                if detail is not None:
                    d.text(" (ratio ")
                    d.value(format_value(detail.ratio, Unit.RATIO), comp.fact_id,
                            RenderRole.OBSERVATION)
                    d.text(", difference ")
                    d.value(format_value(detail.difference, detail.difference_unit),
                            comp.fact_id, RenderRole.OBSERVATION)
                    d.text("; ")
                    if detail.matching_keys:
                        d.text("matched on " + ", ".join(
                            T.DIMENSION_LABELS[k].lower() for k in detail.matching_keys))
                    else:
                        d.text("no fixed matching keys, not a matched peer")
                    d.text(") ")
                    self.ref(comp.fact_id, RenderRole.OBSERVATION)
                d.text(".")
            case ObservationTemplateId.TREND:
                earlier, later, change = facts["earlier"], facts["later"], facts["change"]
                d.text(f"{_sentence(T.METRIC_LABELS[earlier.metric])} for ")
                self.segment(earlier.segment.segment_id)
                d.text(" moved from ")
                self.fact_value(earlier, RenderRole.OBSERVATION)
                d.text(" (")
                self.period(earlier.period.period_id)
                d.text(") to ")
                self.fact_value(later, RenderRole.OBSERVATION)
                d.text(" (")
                self.period(later.period.period_id)
                d.text("); change ")
                self.fact_value(change, RenderRole.OBSERVATION)
                d.text(" ")
                self.refs([earlier.fact_id, later.fact_id, change.fact_id],
                          RenderRole.OBSERVATION)
                d.text(".")
            case ObservationTemplateId.COVERAGE:
                observed, reference = facts["observed_days"], facts["reference_days"]
                self.segment(observed.segment.segment_id)
                d.text(" was observed on ")
                self.fact_value(observed, RenderRole.OBSERVATION)
                d.text(" out of ")
                self.fact_value(reference, RenderRole.BASELINE)
                d.text(" in ")
                self.period(reference.period.period_id)
                d.text("; absence is not confirmed zero traffic ")
                self.refs([observed.fact_id], RenderRole.OBSERVATION)
                d.text(" ")
                self.refs([reference.fact_id], RenderRole.BASELINE)
                d.text(".")
            case ObservationTemplateId.CONCENTRATION:
                share = facts["concentration_share"]
                d.text("Concentration: ")
                self.metric_fact(share, RenderRole.OBSERVATION)
                d.text(" came from the listed days.")

    def scenario_block(self, s: Scenario) -> None:
        d = self.doc
        d.text(f"- {T.SCENARIO_TEMPLATES[s.template]} ")
        self.ref(s.scenario_id, RenderRole.SCENARIO)
        d.line(f". {T.CONDITIONAL_IMPACT}")
        results = [self.fact(i) for i in s.result_fact_ids]
        if not results or all(f.value is None for f in results):
            d.line(f"  - {T.NOT_ESTIMABLE}")
        for fact in results:
            if fact.kind is not FactKind.SCENARIO:
                raise RenderError(f"{fact.fact_id} is not a scenario fact")
            d.text(f"  - Conditional {T.METRIC_LABELS[fact.metric]}: ")
            self.fact_value(fact, RenderRole.SCENARIO)
            d.text(" ")
            self.ref(fact.fact_id, RenderRole.SCENARIO)
            d.line()
        for label, fact_id in (("Revenue at risk", s.revenue_at_risk_fact_id),
                               ("Profit at risk", s.profit_at_risk_fact_id)):
            if fact_id:
                d.text(f"  - {label}: ")
                fact = self.fact(fact_id)
                self.fact_value(fact, RenderRole.SCENARIO)
                d.text(" ")
                self.ref(fact.fact_id, RenderRole.SCENARIO)
                d.line()
        d.text("  - Uses ")
        self.refs(s.historical_fact_ids, RenderRole.BASELINE)
        if s.assumption_ids:
            d.text(" with assumptions ")
            self.refs(s.assumption_ids, RenderRole.ASSUMPTION)
        d.line(f"; {T.DISPLACED_TEXT[s.displaced_cost_treatment]}.")
        d.line(f"  - {T.HORIZON_TEXT} Unchanged mix and available capacity are assumed.")
        if s.prerequisite_codes:
            d.line("  - Prerequisites: "
                   + " ".join(T.PREREQUISITES[p] for p in s.prerequisite_codes))
        if s.limitation_codes:
            d.line("  - Limitations: " + " ".join(T.RISKS[c] for c in s.limitation_codes))

    def audit_section(self) -> None:
        d = self.doc
        d.line("## Audit: excluded, rejected and incomplete output")
        d.line()
        excluded = [c for c in self.b.candidates.values() if c.eligibility.value != "eligible"]
        model_rejected = [r for r in self.run.recommendations
                          if not _is_actionable(r.recommendation)]
        incomplete = [a for a in self.run.analyses if a.status.value != "complete"]
        if not (excluded or model_rejected or incomplete or self.run.validation_failures):
            d.line("Nothing was excluded.")
            d.line()
            return
        for cand in excluded:
            d.text(f"- {T.FAMILY_LABELS[cand.family]}, {T.ELIGIBILITY_TEXT[cand.eligibility]}: ")
            if cand.origin == "model_proposed":
                d.text("model-proposed (not detector-raised) · ")
            self.segment(cand.segment_id)
            if cand.rejection_reason:
                d.text(" — ")
                d.value(clean_label(cand.rejection_reason, _MAX_REASON), cand.candidate_id,
                        RenderRole.METADATA)
            d.text(" ")
            self.ref(cand.candidate_id)
            d.line()
        for rec in model_rejected:
            r = rec.recommendation
            verb = "merged into another recommendation" if r.decision is Decision.MERGE else \
                "rejected by the model"
            d.text(f"- Candidate {verb}: ")
            if r.origin == "model_proposed":
                d.text("model-proposed · ")
            self.segment(r.segment_id)
            d.text(" ")
            self.ref(r.candidate_id)
            d.line()
        for analysis in incomplete:
            d.text(f"- {_sentence(T.ANALYSIS_STATUS_TEXT[analysis.status])}: candidate ")
            self.ref(analysis.candidate_id)
            candidate = self.b.candidates.get(analysis.candidate_id)
            if candidate is not None and candidate.origin == "model_proposed":
                d.text(" (model-proposed)")
            d.line()
        for key, errors in sorted(self.run.validation_failures.items()):
            d.text("- Rejected by validation: ")
            if key in self.b.candidates:
                self.ref(key)
                if self.b.candidates[key].origin == "model_proposed":
                    d.text(" (model-proposed)")
            else:
                d.text(clean_label(key))
            d.text(" — ")
            for n, err in enumerate(errors):
                if n:
                    d.text("; ")
                d.text(T.VALIDATION_CODE_TEXT[err.code])
                if err.ref:
                    d.text(" ")
                    known = known_source_ids(self.run)
                    source = err.ref if err.ref in known else (
                        key if key in self.b.candidates else self.run_id)
                    if _ID_REF.match(err.ref):
                        d.text("`")
                        d.value(err.ref, source, RenderRole.METADATA)
                        d.text("`")
                    else:
                        d.value(clean_label(err.ref), source, RenderRole.METADATA)
            d.line()
        d.line()

    def method(self) -> None:
        d = self.doc
        d.line("## Method and limits")
        d.line()
        d.line("- Python measures every quantity; the self-hosted model only selects typed "
               "evidence, tests and codes; code validates each reference and renders every number.")
        d.line("- Rates are ratios of additive sums; revenue per million requests is the "
               "export's sRPM scale.")
        d.line(f"- {T.GENERAL_LIMIT}")
        d.line(f"- {T.NO_PORTFOLIO_SUM}")
        d.line(f"- {T.CURRENCY_NOTE}")


def _sentence(s: str) -> str:
    return s[:1].upper() + s[1:]


def _is_actionable(r: CandidateRecommendation) -> bool:
    return r.decision in (Decision.RECOMMEND_TEST, Decision.INVESTIGATE_FIRST)


def _title(r: CandidateRecommendation) -> str:
    if r.test_template_id is None:
        return "Investigation"
    title = T.TEST_TEMPLATES[r.test_template_id].title
    if r.decision is Decision.INVESTIGATE_FIRST:
        return f"Investigation first, then {title[:1].lower()}{title[1:]}"
    return f"Test: {title}"


def period_label(period: Period) -> str:
    days = len(period.included_dates)
    if period.start == period.end:
        return period.start.isoformat()
    span_days = (period.end - period.start).days + 1
    label = f"{period.start.isoformat()} to {period.end.isoformat()}"
    if days != span_days:
        label += f", {days} of {span_days} days observed"
    return f"{label} ({T.COVERAGE_TEXT[period.coverage_status]})"


def render_report(run: ValidatedRun) -> RenderedReport:
    """Render warnings first, then summary and recommendations, with a span audit map."""
    report = _Renderer(run).render()
    audit = audit_report(report, run)
    if not audit.ok:
        raise RenderError(
            f"render audit failed: {len(audit.uncovered)} uncovered quantities, "
            f"{len(audit.unresolved)} unresolved sources"
        )
    return report


__all__ = [
    "RenderAudit",
    "account_fact_groups",
    "RenderError",
    "audit_report",
    "clean_label",
    "format_value",
    "period_label",
    "render_report",
]
