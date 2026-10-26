"""Generate ONLY synthetic input, run end to end, save timings and audit bundle.

Offline: uv run python scripts/benchmark_fullsize.py --out /tmp/adops-scale
Live (explicit opt-in): add --live; credentials come from the environment.
No existing input path is accepted, so this cannot accidentally read private rows.
"""

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.config import RunConfig  # noqa: E402
from contracts.enums import Dimension, MetricKey  # noqa: E402
from llm.profiles import load_profile  # noqa: E402
from report.bundle import write_bundle  # noqa: E402
from report.opportunities import Opportunities  # noqa: E402
from report.summary import ShortReport, _Builder, audit_summary  # noqa: E402
from runner import default_stages, execute  # noqa: E402
from scripts.generate_fullsize import write  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=118_000)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "generated-synthetic.csv"
    rows = write(20310417, args.rows, path)
    stages = default_stages()
    if not args.live:
        from tests.support.scripted_model import ScriptedModel
        model = ScriptedModel()
        stages = replace(stages, create_client=lambda *args: model)
    started = time.monotonic()

    def progress(event):
        print(f"{time.monotonic() - started:.1f}s {event.stage} {event.state} "
              f"{event.message_code}", flush=True)

    with path.open("rb") as source:
        outcome = execute(source, RunConfig(model_profile_id=load_profile().profile_id),
                          progress, stages=stages)
    seconds = time.monotonic() - started
    run = outcome.run
    bundle = write_bundle(outcome, args.out)
    shown = _Builder(run).ordered_recommendations()
    planner = Opportunities(run)
    families = Counter(planner.family(r.recommendation).value for r in shown)
    signatures = [tuple(planner.metric(r.recommendation, m).value
                        for m in (MetricKey.REQUESTS, MetricKey.REVENUE, MetricKey.PROFIT))
                  for r in shown]
    supplies = Counter(run.bundle.segments[r.recommendation.segment_id].filters[Dimension.SUPPLY_ID]
                       for r in shown if Dimension.SUPPLY_ID in run.bundle.segments[r.recommendation.segment_id].filters)
    broad = [run.bundle.segments[r.recommendation.segment_id] for r in shown
             if len(run.bundle.segments[r.recommendation.segment_id].filters) <= 2]
    visible = outcome.report.markdown.split('## Recommendations')[-1].split('## Each recommended')[0]
    visible = '\n'.join(part.split('- **Details:**')[0] for part in visible.split('### '))
    estimates = [line for line in visible.splitlines() if line.startswith('- **Estimated opportunity')]
    stats = {
        "synthetic_rows": rows,
        "country_convention": "ISO alpha-3 plus planted placeholders",
        "card_families": dict(families),
        "distinct_card_totals": len(signatures) == len(set(signatures)),
        "max_cards_per_supply": max(supplies.values(), default=0),
        "broad_country_card": any(Dimension.COUNTRY in s.filters for s in broad),
        "broad_size_card": any(Dimension.SIZE in s.filters for s in broad),
        "plain_cards": ('tainted' not in visible and 'Concentration' not in visible
                        and all('Assumes' not in x and '%' not in x
                                and ('Not estimated: needs' in x or 'extra profit per week if' in x)
                                for x in estimates)),
        "country_unverified_issues": sum(i.message_template_id == 'DQ10_unverified_country'
                                         for i in run.bundle.quality.issues), "seed": 20310417, "days": 7,
        "mode": "live" if args.live else "scripted", "wall_seconds": round(seconds, 2),
        "status": str(run.status), "validated": len(run.recommendations),
        "report_cards": outcome.report.markdown.count("- **Opportunity:**"),
        "report_dollar_estimates": sum(
            "(estimate):**" in line and "$" in line
            for line in outcome.report.markdown.splitlines()
        ),
        "report_audit_ok": audit_summary(ShortReport(outcome.report, outcome.html), run).ok,
        "analysis_statuses": dict(Counter(str(a.status) for a in run.analyses)),
        "decisions": dict(Counter(str(r.recommendation.decision) for r in run.recommendations)),
        "rejection_codes": dict(Counter(str(e.code) + ":" + e.detail
            for a in run.analyses for attempt in a.rejected_attempts for e in attempt.errors)),
        "unpacked": dict(Counter(str(x) for x in run.bundle.unpacked.values())),
        "repairs": sum(a.repairs for a in run.analyses),
        "turns": sum(a.generation_turns for a in run.analyses),
        "tool_calls": sum(len(a.tool_evidence) for a in run.analyses),
        "model_call_counts": outcome.artifact.manifest.model_call_counts,
        "bundle": str(bundle),
    }
    (args.out / "summary.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2), flush=True)


if __name__ == "__main__":
    main()
