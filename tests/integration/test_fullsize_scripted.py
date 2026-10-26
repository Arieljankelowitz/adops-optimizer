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
from contracts.enums import AnalysisStatus, Decision
from llm.profiles import load_profile
from llm.tokens import estimate_prompt_tokens
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
    # Overlap is heavy: the old contract (at most 8 groups) refused these.
    assert max(len(p.candidate.overlap_group_ids) for p in bundle.packs.values()) > 8
    refused = Counter(
        (e.path, e.detail) for a in run.analyses for at in a.rejected_attempts for e in at.errors
    )
    assert not refused, refused
    assert not any(e.message_code == "tool_refused" for e in events)
    detector = [a for a in run.analyses if bundle.candidates[a.candidate_id].origin == "detector"]
    assert len(detector) == 15
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
    # Every request fits the profile's prompt window.
    limit = load_profile().max_prompt_tokens
    assert max(estimate_prompt_tokens(r.messages) for r in model.requests) <= limit


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
