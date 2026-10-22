"""Streamlit smoke tests (AppTest) and the CLI on the synthetic paths."""

import ast
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = str(REPO_ROOT / "app.py")
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ad_performance.csv"
TABS = ["Report", "Details"]
DETAIL_TABS = ["Data quality", "Summary", "Metrics", "Candidates", "Progress", "Drill-down",
               "Validation", "Full audit report"]


@pytest.fixture
def demo_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.radio(key="mode").set_value("Cached demo run (synthetic)").run()
    assert not at.exception
    return at


def test_app_starts_without_input() -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    assert any("Upload a CSV export" in i.value for i in at.info)


def test_demo_path_shows_the_short_report_first_and_details_behind_it(demo_app: AppTest) -> None:
    labels = [t.label for t in demo_app.tabs]
    assert [label for label in labels if label in TABS] == TABS
    assert [label for label in labels if label in DETAIL_TABS] == DETAIL_TABS
    banner = demo_app.warning[0].value
    assert "CACHED PREVIOUS RUN — not fresh inference." in banner
    assert "synthetic-demo-model" in banner and "Generated at" in banner
    (page,) = demo_app.get("iframe")
    assert "<!doctype html>" in page.proto.srcdoc
    assert "found by AI" in page.proto.srcdoc and "Estimated opportunity" in page.proto.srcdoc
    # The long audit rendering and the quality detail table live under Details.
    assert any(w.value.startswith("**DQ04") for w in demo_app.warning)
    full = next(m.value for m in demo_app.markdown
                if m.value.startswith("# Ad-performance optimization report"))
    assert "\\$" in full  # money escaped so Streamlit does not treat it as LaTeX
    assert len(demo_app.dataframe) >= 8


def test_downloads_need_the_private_acknowledgement(demo_app: AppTest) -> None:
    assert not demo_app.get("download_button")
    demo_app.checkbox(key="private_ack").check().run()
    assert not demo_app.exception
    assert len(demo_app.get("download_button")) == 3  # html, markdown, full bundle


def test_candidate_inspection_and_fact_inspector(demo_app: AppTest) -> None:
    picker = demo_app.selectbox(key="cand_pick")
    for option in picker.options[:3]:
        picker.set_value(option).run()
        assert not demo_app.exception
    facts = demo_app.selectbox(key="fact_pick")
    facts.set_value(facts.options[-1]).run()
    assert not demo_app.exception


def test_demo_drilldown_explains_recorded_tool_calls(demo_app: AppTest) -> None:
    demo_app.button(key="dd_run").click().run()
    assert not demo_app.exception
    assert any("Recorded tool calls" in i.value for i in demo_app.info)


def test_synthetic_path_runs_deterministic_evidence_without_model() -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.radio(key="mode").set_value("Synthetic sample CSV").run()
    at.button(key="run").click().run()
    job = at.session_state["jobs"].current
    deadline = time.monotonic() + 10
    while job.active and time.monotonic() < deadline:
        time.sleep(0.05)
    at.run()
    assert not at.exception
    assert not at.error
    assert job.outcome is not None
    assert job.outcome.status.value == "deterministic_only"
    assert job.outcome.run.bundle.packs


def test_app_never_uses_global_streamlit_caches() -> None:
    tree = ast.parse((REPO_ROOT / "app.py").read_text())
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "st"}
    assert not used & {"cache", "cache_data", "cache_resource", "experimental_memo",
                       "experimental_singleton"}


def test_cli_demo_prints_the_cached_report(capsys) -> None:
    assert cli.main(["demo"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("# Ad-performance optimization report")
    assert "CACHED PREVIOUS RUN — not fresh inference." in out
    assert "## Recommendations" in out and "[found by AI]" in out


def test_cli_writes_the_html_report_when_asked(capsys, tmp_path) -> None:
    page = tmp_path / "report.html"
    assert cli.main(["demo", "--html", str(page)]) == cli.EXIT_OK
    html = page.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "found by AI" in html


def test_cli_run_renders_deterministic_evidence(capsys) -> None:
    assert cli.main(["run", str(SAMPLE)]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "DETERMINISTIC" in captured.out
    assert "[evidence] succeeded" in captured.err
    assert "not implemented" not in captured.err


def test_cli_missing_input(capsys, tmp_path) -> None:
    assert cli.main(["run", str(tmp_path / "absent.csv")]) == cli.EXIT_USAGE


def test_cli_writes_and_shows_a_bundle_only_when_asked(capsys, tmp_path) -> None:
    assert cli.main(["demo", "--out", str(tmp_path)]) == cli.EXIT_OK
    (bundle_dir,) = list(tmp_path.iterdir())
    capsys.readouterr()
    assert cli.main(["show", str(bundle_dir)]) == cli.EXIT_OK
    assert "CACHED PREVIOUS RUN" in capsys.readouterr().out
    assert (bundle_dir / "report.html").read_text().startswith("<!doctype html>")
    (bundle_dir / "report.md").write_text("edited\n")
    assert cli.main(["show", str(bundle_dir)]) == cli.EXIT_USAGE
