"""A busy export end to end with a scripted model under guided decoding.

The small fixture has 39 slices; a real export has tens of thousands, and a
private full-size run failed in ways the fixture could not show: overlap
groups beyond the answer contract's cap, breakdowns by an already-fixed
dimension, and packs too large for the window. ``scripts/generate_fullsize``
reproduces that scale with invented data. The medium file runs by default;
the full-size one is opt-in (``-m fullsize``).
"""

import time
from collections import Counter
from dataclasses import replace

import pytest

from contracts.config import RunConfig
from contracts.enums import AnalysisStatus, Decision, Dimension, MetricKey, QualityRuleId
from llm.profiles import load_profile
from llm.tokens import estimate_prompt_tokens
from report.opportunities import Opportunities
from report.summary import ShortReport, _Builder, audit_summary
from runner import default_stages, execute
from scripts.generate_fullsize import write
from tests.support.scripted_model import ScriptedModel


def _run(path):
    model = ScriptedModel()
    stages = replace(default_stages(), create_client=lambda env, budget, profile: model)
    config = RunConfig(model_profile_id=load_profile().profile_id)
    events = []
    started = time.monotonic()
    with path.open("rb") as source:
        outcome = execute(source, config, events.append, stages=stages, env={})
    return outcome, model, events, time.monotonic() - started


def _check(outcome, model, events, min_validated):
    run = outcome.run
    bundle = run.bundle
    # Every pack fits the window with room for tools: none is left unjudged.
    assert not bundle.unpacked
    # The bounded shortlist still overlaps; code-attached groups must survive.
    assert max(len(p.candidate.overlap_group_ids) for p in bundle.packs.values()) > 0
    assert sum(c.origin == "detector" for c in bundle.candidates.values()) <= 40
    refused = Counter(
        (e.path, e.detail) for a in run.analyses for at in a.rejected_attempts for e in at.errors
    )
    assert not refused, refused
    assert not any(e.message_code == "tool_refused" for e in events)
    detector = [a for a in run.analyses if bundle.candidates[a.candidate_id].origin == "detector"]
    assert len(detector) == 10
    assert all(a.status is AnalysisStatus.COMPLETE for a in detector)
    actionable = [
        r for r in run.recommendations
        if r.recommendation.decision in (Decision.RECOMMEND_TEST, Decision.INVESTIGATE_FIRST)
    ]
    assert len(actionable) >= min_validated
    # A validated recommendation carries every overlap group of its candidate.
    for r in run.recommendations:
        candidate = bundle.candidates[r.recommendation.candidate_id]
        assert r.recommendation.overlap_group_ids == candidate.overlap_group_ids
    assert run.ranking is not None and run.ranking.ranked_recommendation_ids
    assert outcome.artifact.manifest.model_call_counts["exposure_selection"] == 1
    assert all(len(a.tool_evidence) <= 1 and a.generation_turns <= 2 for a in detector)
    assert not any(r.response_schema_name == "recommendation_ranking" for r in model.requests)
    md = outcome.report.markdown
    assert "Final selection is by code" in md
    shown = _Builder(run).ordered_recommendations()
    assert 5 <= len(shown) <= 10
    assert md.count("- **Opportunity:**") == len(shown)
    assert outcome.html.count('<article class="card">') == len(shown)
    _distinct_and_varied(run, shown)
    estimates = [line for line in md.splitlines() if line.startswith("- **Estimated opportunity")]
    assert all(("$" in line and "extra profit per week if" in line
                or "Not estimated: needs" in line) and "Assumes" not in line
               and "%" not in line for line in estimates)
    assert "Investigate, then investigate" not in md
    assert audit_summary(ShortReport(outcome.report, outcome.html), run).ok
    planner = Opportunities(run)
    scores = [(bundle.candidates[r.recommendation.candidate_id].eligibility.value != "eligible",
               -planner.plan(r).score) for r in shown]
    assert scores == sorted(scores)
    # Every request fits the profile's prompt window.
    limit = load_profile().max_prompt_tokens
    assert max(estimate_prompt_tokens(r.messages) for r in model.requests) <= limit


def _distinct_and_varied(run, shown):
    planner = Opportunities(run)
    families, dims, signatures, hashes, supplies = set(), set(), set(), set(), Counter()
    for rec in shown:
        r = rec.recommendation
        segment = run.bundle.segments[r.segment_id]
        families.add(planner.family(r))
        dims.update(segment.filters)
        if Dimension.SUPPLY_ID in segment.filters:
            supplies[segment.filters[Dimension.SUPPLY_ID]] += 1
        facts = [planner.metric(r, m) for m in
                 (MetricKey.REQUESTS, MetricKey.REVENUE, MetricKey.PROFIT)]
        signature = tuple(f.value for f in facts)
        row_hash = facts[0].provenance.source_row_set_hash
        assert signature not in signatures and row_hash not in hashes
        signatures.add(signature)
        hashes.add(row_hash)
    assert max(supplies.values()) <= 2
    assert len(families) >= 4
    assert {Dimension.COUNTRY, Dimension.SIZE} <= dims
    broad = [run.bundle.segments[r.recommendation.segment_id] for r in shown
             if len(run.bundle.segments[r.recommendation.segment_id].filters) <= 2]
    assert any(Dimension.COUNTRY in s.filters for s in broad)
    assert any(Dimension.SIZE in s.filters for s in broad)
    dq10 = [i for i in run.bundle.quality.issues if i.rule_id is QualityRuleId.DQ10]
    # Only planted '-' labels, never alpha-3 countries, are unverified.
    assert not any(i.message_template_id == 'DQ10_unverified_country' for i in dq10)


def test_existing_small_fixture_report_with_a_scripted_model():
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "fixtures/synthetic/ad_performance.csv"
    outcome, _, _, _ = _run(path)
    assert outcome.report.markdown.count("- **Opportunity:**") >= 5
    assert outcome.html.count('<article class="card">') == outcome.report.markdown.count("- **Opportunity:**")
    assert audit_summary(ShortReport(outcome.report, outcome.html), outcome.run).ok
    estimates = [line for line in outcome.report.markdown.splitlines()
                 if line.startswith("- **Estimated opportunity")]
    assert all("Not estimated: needs" in line or "extra profit per week if" in line
               for line in estimates)


def test_medium_export_runs_clean_with_a_scripted_model(tmp_path):
    path = tmp_path / "medium.csv"
    write(seed=20310418, rows=6_000, out=path)
    outcome, model, events, _ = _run(path)
    _check(outcome, model, events, min_validated=5)


@pytest.mark.fullsize
def test_full_size_export_runs_clean_and_fast_with_a_scripted_model(tmp_path):
    path = tmp_path / "fullsize.csv"
    write(seed=20310417, rows=118_000, out=path)
    outcome, model, events, seconds = _run(path)
    _check(outcome, model, events, min_validated=5)
    # The deterministic stages leave most of a 25-minute demo to the model.
    assert seconds < 10 * 60
