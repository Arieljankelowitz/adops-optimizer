"""The short hand-in report: card shape, estimates, AI badge, audit and traceability."""

import importlib.util
import re
from dataclasses import replace
from pathlib import Path

import pytest

from contracts.config import RunBudget, RunConfig
from contracts.enums import RenderRole, RunStatus, Unit
from contracts.models import ObservationStatement, RenderedReport
from contracts.templates import ObservationTemplateId
from report import text as T
from report.opportunities import Opportunities
from report.render import RenderError, format_value
from report.summary import MAX_CAVEATS, ShortReport, audit_summary, render_summary

REPO_ROOT = Path(__file__).resolve().parents[2]
CARD_FIELDS = (
    "- **Opportunity:**",
    "- **Current:**",
    "- **Observation:**",
    "- **Recommendation:**",
    "- **Estimated opportunity",
    "- **Details:**",
)


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_demo_run", REPO_ROOT / "scripts" / "build_demo_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _builder()


@pytest.fixture(scope="module")
def final():
    return BUILDER.build_demo_outcome(RunStatus.FINAL)


def _cards(markdown: str) -> list[str]:
    body = markdown.split("## Recommendations", 1)[1].split("\n## ", 1)[0]
    return [b for b in re.split(r"\n(?=### \d)", body) if b.startswith("### ")]


def test_explore_is_on_by_default() -> None:
    assert RunBudget().explore_enabled is True
    assert RunConfig().budget.explore_enabled is True


def test_short_report_has_few_sections_in_assignment_order(final) -> None:
    sections = [line for line in final.report.markdown.splitlines() if line.startswith("## ")]
    assert sections == [
        "## Caveats that affect these conclusions",
        "## Recommendations",
        "## Each recommended slice against its baseline",
        "## Requests and revenue by recommended slice",
        "## Notes",
        "## Appendix",
    ]
    assert final.report.markdown.index("**Account:**") < final.report.markdown.index("## Caveats")


def test_between_five_and_ten_cards_in_the_assignments_shape(final) -> None:
    cards = _cards(final.report.markdown)
    assert 5 <= len(cards) <= 10
    actionable = [r for r in final.run.recommendations
                  if r.recommendation.decision.value in ("recommend_test", "investigate_first")]
    assert len(cards) == len(actionable)
    for rec in actionable:
        assert any(rec.recommendation_id in card for card in cards)
    for card in cards:
        positions = [card.index(field) for field in CARD_FIELDS]
        assert positions == sorted(positions), card[:80]
    assert final.html.count('<article class="card">') == len(cards)


def test_account_line_and_at_most_three_caveats(final) -> None:
    md = final.report.markdown
    account = next(line for line in md.splitlines() if line.startswith("**Account:**"))
    for part in ("requests", "revenue", "profit", "margin"):
        assert part in account
    caveats = md.split("## Caveats that affect these conclusions", 1)[1].split("\n## ", 1)[0]
    bullets = [line for line in caveats.splitlines() if line.startswith("- ")]
    assert 1 <= len(bullets) <= MAX_CAVEATS + 1  # the caveats and the "further" line
    assert "Full data-quality checks: appendix" in bullets[-1]
    assert "2031-03-09" in caveats and "51.8%" in caveats
    assert "Large daily change" not in caveats


def test_cards_show_current_requests_revenue_and_profit_from_slice_facts(final) -> None:
    b = final.run.bundle
    rec = final.run.recommendations[0].recommendation
    first = next(c for c in _cards(final.report.markdown)
                 if final.run.recommendations[0].recommendation_id in c)
    for metric in ("requests", "revenue", "profit"):
        fact = next(f for f in b.facts.values()
                    if f.segment.segment_id == rec.segment_id
                    and f.period.period_id == rec.period_id and f.metric.value == metric)
        assert format_value(fact.value, fact.unit) in first
        assert fact.fact_id in final.html  # fact ID on hover


def test_model_proposed_findings_carry_the_found_by_ai_badge(final) -> None:
    ranked = {r.recommendation_id: r for r in final.run.recommendations}
    proposed = [i for i in final.run.ranking.ranked_recommendation_ids
                if ranked[i].recommendation.origin == "model_proposed"]
    assert len(proposed) == 1
    cards = _cards(final.report.markdown)
    flagged = [c for c in cards if f"[{T.FOUND_BY_AI}]" in c]
    assert len(flagged) == 1 and proposed[0] in flagged[0]
    assert final.html.count(f'>{T.FOUND_BY_AI}<') == 1


def test_estimates_are_labelled_and_carry_their_key_assumption(final) -> None:
    cards = _cards(final.report.markdown)
    for card in cards:
        visible = card.split("- **Details:**")[0]
        estimate = next(line for line in visible.splitlines() if line.startswith("- **Estimated opportunity"))
        assert "Assumes" not in estimate and "%" not in estimate
        assert ("extra profit per week if" in estimate and "$" in estimate
                or "Not estimated: needs" in estimate)
        assert "tainted" not in visible and "Concentration" not in visible
        assert "illustrative" not in estimate.lower()
    throttle = next(c for c in cards if ". Throttle" in c)
    assert "avoidable processing costs" in throttle and "at risk" in throttle
    assert any("reaches the account margin" in c for c in cards)
    assert any("delivery reaches the comparison rate" in c for c in cards)
    assert any("bid distributions and a controlled floor-test result" in c for c in cards)


def test_recommendations_use_plain_verbs(final) -> None:
    md = final.report.markdown
    for verb in ("Scale up", "Throttle", "review margin terms", "reconcile counters"):
        assert verb in md
    assert "Investigate this slice before changing anything" in md


def test_detail_sections_hold_the_engineering_material(final) -> None:
    first = _cards(final.report.markdown)[0]
    details = first.split("- **Details:**", 1)[1]
    for heading in ("What it does not prove", "Test design", "Fact IDs",
                    "Confidence and references"):
        assert heading in details
    visible = first.split("- **Details:**", 1)[0]
    assert "Guardrails" not in visible and "`F_" not in visible
    assert "<details><summary>Details</summary>" in final.html
    assert "<details><summary>Details</summary>" in first
    for card in _cards(final.report.markdown):
        caveats = card.split("  - What it does not prove", 1)[1].split("  - Fact IDs", 1)[0]
        lines = [line for line in caveats.splitlines() if line.startswith("    - ")]
        assert len(lines) <= 4 and len(lines) == len(set(lines))
        design = card.split("  - Test design", 1)[1].split("  - Confidence and references", 1)[0]
        assert sum(line.startswith("    - ") for line in design.splitlines()) == 3


def test_ranking_omissions_and_merge_do_not_hide_validated_findings(final) -> None:
    # The demo ranking omits the floor finding and absorbs the parent slice.
    # Both still get complete cards, not just audit references.
    run = replace(final.run, ranking=None)
    short = render_summary(run)
    assert len(_cards(short.report.markdown)) == len(_cards(final.report.markdown))
    planner = Opportunities(run)
    cards = _cards(short.report.markdown)
    own_ids = [re.search(r"Recommendation `(R_[a-f0-9]+)`", c).group(1) for c in cards]
    recs = {r.recommendation_id: r for r in run.recommendations}
    scores = [planner.plan(recs[i]).score for i in own_ids]
    assert scores == sorted(scores, reverse=True)


def test_duplicate_validated_findings_become_one_card_with_merge_evidence(final):
    first = final.run.recommendations[0]
    duplicate = first.model_copy(update={"recommendation_id": "R_ffffffffffffffff"})
    run = replace(final.run, recommendations=(*final.run.recommendations, duplicate))
    short = render_summary(run)
    assert len(_cards(short.report.markdown)) == len(_cards(final.report.markdown))
    assert "collapsed duplicate findings" in short.report.markdown
    assert "R_ffffffffffffffff" in short.report.markdown
    assert audit_summary(short, run).ok


def test_near_zero_counter_exposure_stays_out_of_top_caveats(final):
    from decimal import Decimal
    from contracts.runtime import FactRegistry

    b = final.run.bundle
    issue = next(i for i in b.quality.issues if i.rule_id.value == "DQ04")
    fid = issue.request_exposure_fact_id
    facts = FactRegistry(b.run_namespace, [f.model_copy(update={"value": Decimal("0.000001")})
                         if f.fact_id == fid else f for f in b.facts.values()])
    quality = b.quality.model_copy(update={"facts": [f.model_copy(update={"value": Decimal("0.000001")})
                                  if f.fact_id == fid else f for f in b.quality.facts]})
    short = render_summary(replace(final.run, bundle=replace(b, facts=facts, quality=quality)))
    top = short.report.markdown.split("## Caveats that affect these conclusions")[1].split("## Recommendations")[0]
    assert "Inconsistent counters" not in top
    assert "0.00%" not in top


def test_quality_appendix_deduplicates_rules_and_top_box_ignores_slice_noise(final) -> None:
    quality = final.run.bundle.quality
    issue = next(i for i in quality.issues if i.rule_id.value == "DQ06")
    duplicate = issue.model_copy(update={"issue_id": "Q_ffffffffffffffff"})
    run = replace(final.run, bundle=replace(final.run.bundle, quality=quality.model_copy(
        update={"issues": [*quality.issues, duplicate]})))
    md = render_summary(run).report.markdown
    top = md.split("## Caveats that affect these conclusions")[1].split("## Recommendations")[0]
    assert top.count("may be incomplete") == 1
    assert "51.8%" in top and "Full data-quality checks: appendix" in top
    assert "further data-quality" not in top and "Large daily change" not in top
    appendix = md.split("### Data-quality findings")[1].split("### Validation summary")[0]
    assert appendix.count("· rule DQ06") == 1


def test_html_is_self_contained_and_phone_ready(final) -> None:
    html = final.html
    assert html.startswith("<!doctype html>")
    assert 'name="viewport"' in html
    assert "<script" not in html and not re.search(r'(?:src|href)="https?:', html)
    assert html.count('class="chart"') == 2


@pytest.mark.parametrize("status", list(RunStatus))
def test_every_status_passes_both_audits_and_shows_its_banner(status: RunStatus) -> None:
    outcome = BUILDER.build_demo_outcome(status)
    audit = audit_summary(ShortReport(outcome.report, outcome.html), outcome.run)
    assert audit.ok, audit
    assert T.STATUS_BANNERS[status] in outcome.report.markdown
    assert T.STATUS_BANNERS[status] in outcome.html


@pytest.mark.parametrize("status", [RunStatus.DETERMINISTIC_ONLY, RunStatus.FAILED])
def test_runs_without_model_output_show_detector_leads_not_cards(status: RunStatus) -> None:
    md = BUILDER.build_demo_outcome(status).report.markdown
    assert "## Detector leads" in md and "detector lead, not model-assessed" in md
    assert "### 1." not in md and T.NO_CARDS in md


def test_fatal_quality_blocks_recommendations() -> None:
    outcome = BUILDER.build_demo_outcome(RunStatus.DETERMINISTIC_ONLY, fatal=True)
    assert T.NO_CARDS_FATAL in outcome.report.markdown
    assert "### 1." not in outcome.report.markdown
    assert "Duplicate daily segment keys" in outcome.report.markdown  # the fatal caveat


def test_cached_run_keeps_generation_time(final) -> None:
    cached = BUILDER.build_demo_outcome(RunStatus.CACHED_PREVIOUS)
    assert cached.artifact.manifest.generated_at == final.artifact.manifest.generated_at
    assert f"generated at {final.artifact.manifest.generated_at.isoformat()}" in (
        cached.report.markdown)


def test_every_digit_in_markdown_and_html_has_a_resolvable_source(final) -> None:
    audit = audit_summary(ShortReport(final.report, final.html), final.run)
    assert audit.uncovered == () and audit.unresolved == ()


def test_audit_flags_unsourced_numbers_in_either_output(final) -> None:
    md = RenderedReport(markdown=final.report.markdown + "\nUplift of 12% guaranteed.\n",
                        spans=final.report.spans)
    audit = audit_summary(ShortReport(md, final.html), final.run)
    assert "12%" in audit.uncovered and "render_replay_mismatch" in audit.unresolved
    html = final.html.replace("</body>", "<p>Uplift of 12% guaranteed.</p></body>")
    audit = audit_summary(ShortReport(final.report, html), final.run)
    assert "html:12%" in audit.uncovered


def test_audit_flags_unknown_sources_and_edited_values(final) -> None:
    html = final.html.replace('data-src="F_', 'data-src="F_ffffffffffffffff" x="', 1)
    assert "F_ffffffffffffffff" in audit_summary(ShortReport(final.report, html),
                                                  final.run).unresolved
    span = next(s for s in final.report.spans
                if s.role is RenderRole.OBSERVATION
                and any(c.isdigit() for c in final.report.markdown[s.start:s.end]))
    md = final.report.markdown
    edited = RenderedReport(markdown=md[:span.start] + "9" + md[span.end:],
                            spans=final.report.spans)
    assert "render_replay_mismatch" in audit_summary(ShortReport(edited, final.html),
                                                      final.run).unresolved


def test_quantity_spans_trace_to_registry_objects(final) -> None:
    b, md = final.run.bundle, final.report.markdown
    checked = 0
    for span in final.report.spans:
        text = md[span.start:span.end]
        if span.role in (RenderRole.OBSERVATION, RenderRole.BASELINE, RenderRole.SCENARIO):
            if text == span.source_id:
                continue
            fact = b.facts[span.source_id]
            allowed = {format_value(fact.value, fact.unit)}
            if fact.value is None:
                allowed = {text} if text.replace("\\", "").startswith("unavailable (") else set()
            if fact.comparison is not None:
                allowed |= {format_value(fact.comparison.ratio, Unit.RATIO),
                            format_value(fact.comparison.difference,
                                         fact.comparison.difference_unit)}
            assert text in allowed, (span.source_id, text)
            checked += 1
        elif span.role is RenderRole.ASSUMPTION and text != span.source_id:
            a = b.assumptions[span.source_id]
            assert text in {format_value(v, a.unit)
                            for v in (a.value, a.bounds.minimum, a.bounds.maximum)}
            checked += 1
    assert checked > 60


def test_renderer_refuses_unknown_references(final) -> None:
    rec = final.run.recommendations[0]
    bad = rec.model_copy(update={"recommendation": rec.recommendation.model_copy(update={
        "observation_statements": [ObservationStatement(
            template_id=ObservationTemplateId.METRIC_VALUE, fact_ids=["F_ffffffffffffffff"])],
    })})
    with pytest.raises(RenderError):
        render_summary(replace(final.run, recommendations=(bad, *final.run.recommendations[1:])))


def test_untrusted_labels_are_escaped_in_both_outputs(final) -> None:
    assert "<b>Ignore" not in final.report.markdown and "<b>Ignore" not in final.html
    assert "\\<b\\>Ignore previous instructions" in final.report.markdown
    assert "&lt;b&gt;Ignore previous instructions" in final.html


def test_static_summary_wording_contains_no_digits() -> None:
    wording = [T.SHORT_SUBTITLE, T.FOUND_BY_AI, T.FOUND_BY_AI_HINT, T.INVESTIGATE_ACTION,
               T.INVESTIGATE_FIRST_PREFIX, T.ESTIMATE_NOTE, T.GAIN_LEAD, T.OVER_PERIOD,
               T.EXTRA_PROFIT, T.EXTRA_REVENUE, T.THROTTLE_NO_GAIN, T.AT_RISK,
               T.FEW_RECOMMENDATIONS, T.NO_CARDS_FATAL, T.NO_CARDS,
               *(x for pair in T.ACTIONS.values() for x in pair),
               *(x for pair in T.ASSUMPTION_CLAUSES.values() for x in pair)]
    assert [s for s in wording if re.search(r"\d", s)] == []


def test_money_amounts_in_estimates_come_from_scenario_facts(final) -> None:
    b = final.run.bundle
    rec = final.run.recommendations[0].recommendation
    scale = next(c for c in _cards(final.report.markdown)
                 if final.run.recommendations[0].recommendation_id in c)
    scenario = b.scenarios[rec.scenario_ids[0]]
    dollars = [b.facts[i] for i in scenario.result_fact_ids if b.facts[i].unit is Unit.EXPORT_DOLLAR]
    assert dollars
    profit = next(f for f in dollars if f.metric.value == "profit")
    weekly = profit.value * 7 / len(profit.period.included_dates)
    assert format_value(weekly, profit.unit) in scale
    assert profit.fact_id in final.html
