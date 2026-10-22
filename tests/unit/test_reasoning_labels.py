"""Model-proposed provenance survives code rendering without changing the layout."""

from dataclasses import replace

from contracts.enums import RunStatus
from report.render import render_report
from scripts.build_demo_run import build_demo_outcome


def test_model_proposed_report_label_is_not_detector_raised():
    outcome = build_demo_outcome(RunStatus.FINAL)
    run = outcome.run
    original = run.recommendations[0]
    rec = original.model_copy(
        update={
            "recommendation": original.recommendation.model_copy(
                update={"origin": "model_proposed"}
            )
        }
    )
    candidates = dict(run.bundle.candidates)
    cid = rec.recommendation.candidate_id
    candidates[cid] = candidates[cid].model_copy(update={"origin": "model_proposed"})
    report = render_report(
        replace(
            run,
            bundle=replace(run.bundle, candidates=candidates),
            recommendations=(rec, *run.recommendations[1:]),
        )
    )
    assert "model-proposed (not detector-raised)" in report.markdown
