"""Thin command-line entry point for reproducible and batch runs.

    uv run python cli.py run INPUT.csv [--out PRIVATE_DIR] [--model-profile ID]
    uv run python cli.py demo [--out PRIVATE_DIR]
    uv run python cli.py show RUN_DIR

``run`` executes the pipeline and prints the report; ``demo`` prints the
synthetic cached run; ``show`` re-renders a saved bundle as a previous run
after verifying its file hashes. Bundles are written only with an explicit
``--out``; nothing is saved by default. Progress goes to stderr as stage
codes only, never labels or values.
"""

import argparse
import sys
from pathlib import Path

from contracts.config import RunConfig
from contracts.models import ProgressEvent
from report.bundle import BundleError, RunOutcome, as_cached, read_bundle, write_bundle
from runner import PipelineError, RunCancelled, execute, load_demo_run

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUN_FAILED = 3
EXIT_CANCELLED = 130


def _progress(event: ProgressEvent) -> None:
    who = f" {event.candidate_id}" if event.candidate_id else ""
    print(f"[{event.stage.value}] {event.state.value}{who} ({event.message_code})",
          file=sys.stderr)


def _finish(outcome: RunOutcome, out: Path | None) -> int:
    print(outcome.report.markdown)
    if out is not None:
        target = write_bundle(outcome, out)
        print(f"Private run bundle written to {target}. It contains derived private data.",
              file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adops-optimizer", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="analyse a CSV export")
    run_p.add_argument("input", type=Path)
    run_p.add_argument("--out", type=Path, help="private directory for the run bundle")
    run_p.add_argument("--model-profile", help="model profile ID; omit for deterministic only")
    demo_p = sub.add_parser("demo", help="print the synthetic cached demo run")
    demo_p.add_argument("--out", type=Path, help="private directory for the run bundle")
    show_p = sub.add_parser("show", help="re-render a saved run bundle as a previous run")
    show_p.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)

    if args.command == "demo":
        return _finish(load_demo_run(), args.out)
    if args.command == "show":
        try:
            outcome = as_cached(read_bundle(args.run_dir))
        except (BundleError, OSError, ValueError) as exc:
            print(f"Cannot show this bundle: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(outcome.report.markdown)
        return EXIT_OK

    if not args.input.is_file():
        print("Input file not found.", file=sys.stderr)
        return EXIT_USAGE
    config = RunConfig(model_profile_id=args.model_profile)
    try:
        with args.input.open("rb") as source:
            outcome = execute(source, config, _progress)
    except RunCancelled as exc:
        print(exc.public_message, file=sys.stderr)
        return EXIT_CANCELLED
    except PipelineError as exc:
        print(f"Run stopped at {exc.stage.value}: {exc.public_message}", file=sys.stderr)
        return EXIT_RUN_FAILED
    return _finish(outcome, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
