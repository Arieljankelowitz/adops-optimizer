"""Streamlit app: upload a CSV (or use the sample), run, read the short report.

The main flow is upload, run with live progress, then the short report with a
download button. Data-quality detail, metrics, candidates, drill-downs and
validation live under a secondary "Details" tab.

Run locally with ``uv run streamlit run app.py``.

Privacy rules this file keeps:

* no ``st.cache_data`` / ``st.cache_resource``: uploads, outcomes and the run
  cache live only in this session's ``st.session_state``;
* no default input path; the synthetic sample and demo run are explicit;
* downloads appear only after an explicit private-data acknowledgement;
* errors show stage-level messages only, never labels, keys or raw exceptions.
"""

from collections import Counter
from decimal import Decimal

import altair as alt
import pandas as pd
import streamlit as st

from contracts.config import EvidenceConfig, RunConfig
from contracts.enums import (
    Dimension,
    FactKind,
    ProgressStage,
    ProgressState,
    RunStatus,
    Severity,
    ToolName,
)
from contracts.models import Fact, ProgressEvent, ToolEvidence
from contracts.runtime import AnalysisContext
from llm.profiles import load_profiles
from report import text as T
from report.bundle import RunOutcome, bundle_zip
from report.render import account_fact_groups, format_value, period_label
from runner import (
    REPO_ROOT,
    JobManager,
    RunCache,
    cache_key,
    default_stages,
    input_sha256,
    load_demo_run,
)

SYNTHETIC_SAMPLE = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ad_performance.csv"
MODE_UPLOAD = "Upload a CSV"
MODE_SAMPLE = "Synthetic sample CSV"
MODE_DEMO = "Cached demo run (synthetic)"
SEVERITY_ORDER = (Severity.FATAL, Severity.WARNING, Severity.NOTICE)

# (field, label, minimum, maximum, step) for the bounded assumptions editor.
ASSUMPTION_CONTROLS = (
    ("test_allocation", "Treatment allocation", 0.01, 0.50, 0.01),
    ("default_additional_request_fraction", "Additional eligible request fraction",
     0.01, 0.50, 0.01),
    ("default_throttle_fraction", "Throttled request fraction", 0.05, 0.90, 0.05),
    ("margin_sensitivity_points", "Margin sensitivity (fraction-scale points)", 0.01, 0.10,
     0.01),
    ("rendering_sensitivity_relative", "Relative rendering sensitivity", 0.01, 0.10, 0.01),
    ("test_profit_stop_loss", "Relative total-profit stop-loss", 0.01, 0.20, 0.01),
)


# --- Session state ---------------------------------------------------------


def _state():  # noqa: ANN202 - Streamlit's SessionStateProxy
    ss = st.session_state
    ss.setdefault("jobs", JobManager())
    ss.setdefault("cache", RunCache())
    ss.setdefault("outcome", None)
    ss.setdefault("run_error", None)
    ss.setdefault("collected_job", None)
    return ss


def _collect_finished_job() -> None:
    ss = st.session_state
    job = ss.jobs.current
    if job is None or job.active or ss.collected_job is job:
        return
    ss.collected_job = job
    if job.outcome is not None:
        ss.cache.put(job.outcome)
        ss.outcome = job.outcome
        ss.run_error = None
    else:
        ss.run_error = (job.status.value, job.error or "The run did not complete.")


# --- Sidebar ---------------------------------------------------------------


def _assumptions_editor() -> EvidenceConfig:
    defaults = EvidenceConfig()
    values: dict[str, object] = {}
    with st.sidebar.expander("Test assumptions (bounded)"):
        st.caption("Assumptions are test parameters, not historical facts. Changing one "
                   "creates a new run namespace and cache key.")
        for name, label, lo, hi, step in ASSUMPTION_CONTROLS:
            value = st.slider(label, lo, hi, float(getattr(defaults, name)), step, key=f"a_{name}")
            values[name] = Decimal(f"{value:.2f}")
        values["test_min_days"] = st.slider("Minimum test duration (days)", 7, 28,
                                            defaults.test_min_days, key="a_test_min_days")
    return defaults.model_copy(update=values)


def _sidebar() -> tuple[str, bytes | None, RunConfig]:
    st.sidebar.header("Input")
    mode = st.sidebar.radio("Source", (MODE_UPLOAD, MODE_SAMPLE, MODE_DEMO), key="mode")
    base = RunConfig()
    data: bytes | None = None
    if mode == MODE_UPLOAD:
        upload = st.sidebar.file_uploader("Daily export CSV", type=["csv"], key="upload")
        st.sidebar.caption(
            f"Parsed in memory only; limit {base.input.max_bytes // (1024 * 1024)} MiB.")
        if upload is not None:
            data = upload.getvalue()
    elif mode == MODE_SAMPLE:
        st.sidebar.caption("Independently generated synthetic data with planted traps.")
        data = SYNTHETIC_SAMPLE.read_bytes()
    profile = None
    if mode != MODE_DEMO:
        try:
            profiles = load_profiles()
            options = [None, *profiles.profiles]
        except (OSError, ValueError):
            options = [None]
        profile = st.sidebar.selectbox(
            "Model profile", options, key="profile",
            format_func=lambda p: "None: deterministic analytics only" if p is None else p,
            help="Profiles come from config/model_profiles.yaml. Without one, the report is "
            "labelled a deterministic-only draft. A model run needs LLM_BASE_URL, LLM_API_KEY "
            "and LLM_ALLOWED_BASE_URLS in the environment.")
    evidence = _assumptions_editor() if mode != MODE_DEMO else base.evidence
    config = base.model_copy(update={"model_profile_id": profile, "evidence": evidence})
    return mode, data, config


# --- Banner and warnings ---------------------------------------------------


def _banner(outcome: RunOutcome) -> None:
    m = outcome.artifact.manifest
    text = T.STATUS_BANNERS[outcome.status]
    if outcome.status is RunStatus.CACHED_PREVIOUS:
        st.warning(
            f"**{text}**  \nGenerated at {m.generated_at.isoformat()} · input SHA-256 "
            f"`{m.input_sha256[:16]}…` · model {m.model_id or 'none (deterministic only)'}"
            f" revision {m.model_revision or 'none'} · config hash "
            f"`{m.analytics_config_hash[:16]}…`"
        )
    elif outcome.status is RunStatus.FINAL:
        st.success(text)
    elif outcome.status is RunStatus.FAILED:
        st.error(text)
    else:
        st.warning(text)


def _segment_label(outcome: RunOutcome, segment_id: str) -> str:
    seg = outcome.run.bundle.segments.get(segment_id)
    if seg is None:
        return segment_id
    if not seg.filters:
        label = T.ACCOUNT_LABEL
    else:
        label = " · ".join(f"{T.DIMENSION_LABELS[d]} {seg.filters[d]}"
                           for d in T.DIMENSION_LABELS if d in seg.filters)
    return label if seg.cohort_id == "full_input" else f"{label} (cohort {seg.cohort_id})"


def _period_label(outcome: RunOutcome, period_id: str) -> str:
    period = outcome.run.bundle.periods.get(period_id)
    return period_label(period) if period else period_id


def _fact_text(fact: Fact) -> str:
    if fact.value is None:
        return f"unavailable ({fact.validity.reason})"
    return format_value(fact.value, fact.unit)


def _warnings(outcome: RunOutcome) -> None:
    """Data-quality findings first, before any summary or recommendation."""
    quality = outcome.run.bundle.quality
    st.subheader("Data-quality warnings")
    if not quality.issues:
        st.info("No data-quality rule triggered.")
    for severity in SEVERITY_ORDER:
        issues = [i for i in quality.issues if i.severity is severity]
        by_rule: dict = {}
        for issue in issues:
            by_rule.setdefault(issue.rule_id, []).append(issue)
        for rule_id, group in sorted(by_rule.items()):
            rule = T.QUALITY_RULES[rule_id]
            scopes = "; ".join(sorted({_segment_label(outcome, s) for i in group
                                       for s in i.segment_ids}))
            body = f"**{rule_id.value} — {rule.title}.** {rule.consequence}  \nScope: {scopes}"
            if severity is Severity.FATAL:
                st.error(body)
            elif severity is Severity.WARNING:
                st.warning(body)
            else:
                with st.expander(f"Notice: {rule_id.value} — {rule.title}"):
                    st.markdown(body)
    if quality.issues:
        with st.expander("All quality issues (detail table)"):
            rows = []
            facts = outcome.run.bundle.facts
            for i in quality.issues:
                measured = [_fact_text(facts[f]) if f in facts else f
                            for f in i.measured_fact_ids]
                rows.append({
                    "rule": i.rule_id.value, "severity": i.severity.value,
                    "segments": "; ".join(_segment_label(outcome, s) for s in i.segment_ids),
                    "periods": "; ".join(_period_label(outcome, p) for p in i.period_ids),
                    "rows affected": i.row_count, "measured": "; ".join(measured),
                    "restrictions": ", ".join(T.RESTRICTION_TEXT[r]
                                              for r in i.inference_restrictions),
                    "affected candidates": ", ".join(i.affected_candidate_ids),
                    "issue": i.issue_id,
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True)
    not_testable = [r.rule_id.value for r in quality.rule_results
                    if r.outcome.value == "not_testable"]
    if not_testable:
        st.caption("Not testable with this input (unknown, not passed): "
                   + ", ".join(not_testable))


# --- Tabs ------------------------------------------------------------------


def _summary_tab(outcome: RunOutcome) -> None:
    m, b = outcome.artifact.manifest, outcome.run.bundle
    cols = st.columns(3)
    cols[0].markdown(f"**Input fingerprint**  \n`{m.input_sha256}`")
    cols[1].markdown(f"**Run**  \n`{m.run_id}` · namespace `{m.run_namespace}`")
    cols[2].markdown(f"**Model**  \n{m.model_id or 'none (deterministic only)'}"
                     f" · revision {m.model_revision or 'none'}")
    st.caption(T.CURRENCY_NOTE)
    groups = account_fact_groups(b)
    if not groups:
        st.info("No account totals are available for this run.")
        return
    labels = [("Full input" if cohort == "full_input" else f"Sensitivity: {cohort}")
              + f" · {_period_label(outcome, period_id)}" for cohort, period_id, _ in groups]
    for tab, (_, _, facts) in zip(st.tabs(labels), groups, strict=True):
        with tab:
            st.dataframe(pd.DataFrame([{
                "metric": T.METRIC_LABELS[f.metric], "value": _fact_text(f),
                "validity": T.VALIDITY_TEXT[f.validity.status], "fact": f.fact_id,
            } for f in facts]), hide_index=True)
    residual = [f for f in b.facts.values() if f.metric.value == "profit_reconciliation_residual"
                and not f.segment.filters]
    if residual:
        st.markdown("**Source-profit reconciliation** (reported profit is preserved)")
        for f in residual:
            st.markdown(f"- {_period_label(outcome, f.period.period_id)}: {_fact_text(f)} "
                        f"(revenue − cost − reported profit) `{f.fact_id}`")


def _level(fact: Fact) -> str:
    dims = [T.DIMENSION_LABELS[d] for d in T.DIMENSION_LABELS if d in fact.segment.filters]
    return " × ".join(dims) if dims else "Account"


def _metrics_tab(outcome: RunOutcome) -> None:
    facts = [f for f in outcome.run.bundle.facts.values()
             if f.kind in (FactKind.MEASURED, FactKind.CALCULATED)]
    if not facts:
        st.info("No metrics are available for this run.")
        return
    levels = sorted({_level(f) for f in facts})
    chosen = st.multiselect("Aggregation levels", levels, default=levels, key="metric_levels")
    rows: dict[tuple, dict] = {}
    for f in facts:
        level = _level(f)
        if level not in chosen:
            continue
        key = (f.segment.segment_id, f.period.period_id)
        row = rows.setdefault(key, {
            "level": level, "segment": _segment_label(outcome, f.segment.segment_id),
            "period": _period_label(outcome, f.period.period_id), "null reasons": "",
        })
        column = f"{T.METRIC_LABELS[f.metric]} [{f.unit.value}]"
        row[column] = float(f.value) if f.value is not None else None
        if f.value is None or f.validity.status.value == "tainted":
            note = f"{T.METRIC_LABELS[f.metric]}: {f.validity.reason}"
            row["null reasons"] = "; ".join(x for x in (row["null reasons"], note) if x)
    st.caption("Rates are ratios of additive sums; shares are traffic-weighted. Units are in "
               "column headers; fractions are unscaled (0.25 = 25%). Click a header to sort.")
    st.dataframe(pd.DataFrame(list(rows.values())), hide_index=True)


def _facts_table(outcome: RunOutcome, fact_ids: list[str]) -> pd.DataFrame:
    facts = outcome.run.bundle.facts
    return pd.DataFrame([{
        "metric": T.METRIC_LABELS[facts[i].metric],
        "segment": _segment_label(outcome, facts[i].segment.segment_id),
        "period": _period_label(outcome, facts[i].period.period_id),
        "value": _fact_text(facts[i]), "kind": facts[i].kind.value, "fact": i,
    } for i in fact_ids if i in facts])


def _daily_chart(outcome: RunOutcome, fact_ids: list[str]) -> None:
    facts = [outcome.run.bundle.facts[i] for i in fact_ids if i in outcome.run.bundle.facts]
    points = [{"day": f.period.start.isoformat(), "value": float(f.value),
               "metric": T.METRIC_LABELS[f.metric]} for f in facts if f.value is not None]
    if not points:
        st.caption("No daily series recorded for this candidate.")
        return
    chart = alt.Chart(pd.DataFrame(points)).mark_line(point=True).encode(
        x=alt.X("day:O", title="Day"), y=alt.Y("value:Q", title="Value"), color="metric:N",
        tooltip=["day", "metric", "value"])
    st.altair_chart(chart)
    observed = {p["day"] for p in points}
    account_days = {d.isoformat() for p in outcome.run.bundle.periods.values()
                    for d in p.included_dates}
    missing = sorted(account_days - observed)
    if missing:
        st.caption("Days with no row for this slice (absent, not confirmed zero): "
                   + ", ".join(missing))


def _candidates_tab(outcome: RunOutcome) -> None:
    b = outcome.run.bundle
    if not b.candidates:
        st.info("No candidates were flagged.")
        return
    audit = st.toggle("Include rejected and exploratory candidates (audit view)", value=True,
                      key="cand_audit")
    decisions = {r.recommendation.candidate_id: r.recommendation.decision.value
                 for r in outcome.run.recommendations}
    failures = outcome.run.validation_failures
    table = [{
        "candidate": c.candidate_id, "family": T.FAMILY_LABELS[c.family],
        "origin": "model-proposed" if c.origin == "model_proposed" else "detector-raised",
        "eligibility": c.eligibility.value, "segment": _segment_label(outcome, c.segment_id),
        "model decision": decisions.get(c.candidate_id,
                                        "rejected by validation" if c.candidate_id in failures
                                        else "not assessed"),
        "reason": c.rejection_reason or "",
    } for c in b.candidates.values() if audit or c.eligibility.value == "eligible"]
    st.dataframe(pd.DataFrame(table), hide_index=True)
    ids = [row["candidate"] for row in table]
    if not ids:
        return
    chosen = st.selectbox("Inspect candidate", ids, key="cand_pick",
                          format_func=lambda i: f"{T.FAMILY_LABELS[b.candidates[i].family]} — "
                          f"{_segment_label(outcome, b.candidates[i].segment_id)}")
    c = b.candidates[chosen]
    st.markdown(f"**{T.FAMILY_LABELS[c.family]}** · {T.ELIGIBILITY_TEXT[c.eligibility]} · "
                f"{_period_label(outcome, c.period_id)}")
    left, right = st.columns(2)
    with left:
        st.markdown("Historical evidence (trigger)")
        st.dataframe(_facts_table(outcome, c.trigger_fact_ids), hide_index=True)
        st.markdown("Matched comparison")
        if c.peer_fact_ids:
            st.dataframe(_facts_table(outcome, c.peer_fact_ids), hide_index=True)
        else:
            st.caption("No matched peer comparison recorded.")
    with right:
        st.markdown("Contrary evidence and concentration")
        if c.counter_evidence_fact_ids:
            st.dataframe(_facts_table(outcome, c.counter_evidence_fact_ids), hide_index=True)
        else:
            st.caption("No contrary evidence recorded.")
        issues = [i for i in b.quality.issues if i.issue_id in c.quality_issue_ids]
        for i in issues:
            st.warning(f"{i.rule_id.value} — {T.QUALITY_RULES[i.rule_id].title}: "
                       f"{T.QUALITY_RULES[i.rule_id].consequence}")
    st.markdown("Daily series")
    _daily_chart(outcome, c.daily_fact_ids)


def _event_rows(events: tuple[ProgressEvent, ...]) -> list[dict]:
    return [{"at": e.at.strftime("%H:%M:%S"), "stage": T.STAGE_LABELS[e.stage],
             "state": e.state.value, "candidate": e.candidate_id or "",
             "detail": e.message_code} for e in events]


def _progress_view(events: tuple[ProgressEvent, ...], detailed: bool = True) -> None:
    if not events:
        st.caption("No progress recorded yet.")
        return
    latest: dict[ProgressStage, ProgressState] = {}
    for e in events:
        if e.candidate_id is None:
            latest[e.stage] = e.state
    pipeline = [s for s in ProgressStage if s not in (ProgressStage.CANDIDATE_ANALYSIS,
                                                       ProgressStage.TOOL_CALL,
                                                       ProgressStage.VALIDATION)]
    st.dataframe(pd.DataFrame([{
        "stage": T.STAGE_LABELS[s], "state": latest[s].value if s in latest else "pending",
    } for s in pipeline]), hide_index=True)
    if not detailed:
        analysed = {e.candidate_id for e in events if e.candidate_id
                    and e.stage is ProgressStage.CANDIDATE_ANALYSIS
                    and e.state is not ProgressState.STARTED}
        if analysed:
            st.caption(f"Findings analysed so far: {len(analysed)}")
        return
    per_candidate: dict[str, dict] = {}
    for e in events:
        if e.candidate_id is None:
            continue
        row = per_candidate.setdefault(e.candidate_id, {
            "candidate": e.candidate_id, "warm inference": "pending", "tool calls": 0,
            "validation / repair": "pending"})
        if e.stage is ProgressStage.CANDIDATE_ANALYSIS:
            row["warm inference"] = f"{e.state.value} ({e.message_code})"
        elif e.stage is ProgressStage.TOOL_CALL and e.state is ProgressState.STARTED:
            row["tool calls"] += 1
        elif e.stage is ProgressStage.VALIDATION:
            row["validation / repair"] = f"{e.state.value} ({e.message_code})"
    if per_candidate:
        st.markdown("Per-finding model progress (cold-start readiness is the stage above)")
        st.dataframe(pd.DataFrame(list(per_candidate.values())), hide_index=True)
    with st.expander("Event log"):
        st.dataframe(pd.DataFrame(_event_rows(events)), hide_index=True)


@st.fragment(run_every=1.0)
def _live_progress() -> None:
    job = st.session_state.jobs.current
    if job is None:
        return
    status, events = job.snapshot()
    st.info(f"Analysis is {status.value}. Reruns reconnect to this run; they do not start "
            "another.")
    _progress_view(events, detailed=False)
    if not job.active:
        st.rerun()


def _tool_evidence(outcome: RunOutcome, ev: ToolEvidence) -> None:
    call = ev.tool_call
    st.markdown(f"**{call.tool.value}** on {_segment_label(outcome, call.segment_id)} · "
                f"{_period_label(outcome, call.period_id)} · invocation `{ev.tool_invocation_id}`"
                f" · tool version `{ev.tool_version}`")
    if ev.truncated:
        st.caption("Truncated: the remainder is reported in the 'other' bucket, so coverage "
                   "stays complete.")
    appended = {f.fact_id: f for f in ev.appended_facts}
    facts = outcome.run.bundle.facts
    rows = []
    for fid in ev.returned_fact_ids + ev.other_bucket_fact_ids:
        f = facts.get(fid) or appended.get(fid)
        if f is None:
            continue
        rows.append({"segment": _segment_label(outcome, f.segment.segment_id),
                     "metric": T.METRIC_LABELS[f.metric], "value": _fact_text(f),
                     "bucket": "other" if fid in ev.other_bucket_fact_ids else "returned",
                     "fact": fid})
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def _drilldown_tab(outcome: RunOutcome) -> None:
    b = outcome.run.bundle
    if not b.segments:
        st.info("No segments are available for drill-down in this run.")
        return
    st.markdown("**Run a drill-down** (deterministic tools; provenance and truncation shown)")
    seg_ids = list(b.segments)
    c1, c2, c3 = st.columns(3)
    segment_id = c1.selectbox("Segment", seg_ids, key="dd_segment",
                              format_func=lambda s: _segment_label(outcome, s))
    tool = c2.selectbox("Tool", [t.value for t in ToolName], key="dd_tool")
    period_id = c3.selectbox("Period", list(b.periods), key="dd_period",
                             format_func=lambda p: _period_label(outcome, p))
    by = st.selectbox("Break down by", [d.value for d in Dimension], key="dd_by")
    limit = st.slider("Top rows", 1, 25, 10, key="dd_limit")
    peer = st.text_input("Peer definition ID", key="dd_peer")
    if st.button("Run drill-down", key="dd_run"):
        if outcome.metrics is None:
            st.info("Live drill-downs need a run computed in this session; this run was loaded "
                    "from a bundle. Recorded tool calls are listed below.")
        else:
            stages = default_stages()
            ctx = AnalysisContext(run_namespace=b.run_namespace, metrics=outcome.metrics,
                                  bundle=b)
            try:
                if tool == ToolName.BREAKDOWN:
                    ev = stages.breakdown(ctx, segment_id, Dimension(by), period_id, limit)
                elif tool == ToolName.DAILY_TREND:
                    ev = stages.daily_trend(ctx, segment_id, period_id)
                else:
                    ev = stages.compare_to_peers(ctx, segment_id, period_id, peer)
            except NotImplementedError:
                st.info("Drill-down tools are not available in this build yet.")
            except Exception as exc:  # stage-level message only
                st.error(f"Drill-down failed ({type(exc).__name__}).")
            else:
                _tool_evidence(outcome, ev)
    st.markdown("**Recorded tool calls from the model run**")
    recorded = [ev for a in outcome.run.analyses for ev in a.tool_evidence]
    if not recorded:
        st.caption("No tool calls were recorded.")
    for ev in recorded:
        _tool_evidence(outcome, ev)


def _validation_tab(outcome: RunOutcome) -> None:
    run = outcome.run
    failures = run.validation_failures
    c1, c2, c3 = st.columns(3)
    c1.metric("Validated model outputs", len(run.recommendations))
    c2.metric("Rejected by validation", len(failures))
    c3.metric("Ranking", "validated" if run.ranking else "not available")
    if failures:
        st.markdown("**Rejected claims**")
        rows = [{"output": key, "code": err.code.value,
                 "meaning": T.VALIDATION_CODE_TEXT[err.code], "path": err.path,
                 "reference": err.ref or "", "detail": err.detail}
                for key, errors in failures.items() for err in errors]
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        codes = Counter(r["code"] for r in rows)
        st.bar_chart(pd.Series(codes, name="rejections"))
    incomplete = [a for a in run.analyses if a.status.value != "complete"]
    if incomplete:
        st.markdown("**Incomplete analyses**")
        st.dataframe(pd.DataFrame([{
            "candidate": a.candidate_id, "status": T.ANALYSIS_STATUS_TEXT[a.status],
            "turns": a.generation_turns, "repairs": a.repairs} for a in incomplete]),
            hide_index=True)

    st.markdown("**Fact inspector**")
    fact_ids = list(run.bundle.facts)
    if fact_ids:
        fid = st.selectbox("Fact", fact_ids, key="fact_pick",
                           format_func=lambda i: f"{i} · "
                           f"{T.METRIC_LABELS[run.bundle.facts[i].metric]}"
                           f" · {_segment_label(outcome, run.bundle.facts[i].segment.segment_id)}")
        fact = run.bundle.facts[fid]
        st.markdown(f"Rendered value: **{_fact_text(fact)}** ({fact.kind.value}, "
                    f"{T.VALIDITY_TEXT[fact.validity.status]})")
        st.json(fact.model_dump(mode="json"), expanded=False)

    observed, scenario = st.tabs(["Observed evidence", "Conditional scenarios"])
    with observed:
        ids = [f.fact_id for f in run.bundle.facts.values()
               if f.kind in (FactKind.MEASURED, FactKind.CALCULATED, FactKind.COMPARISON)]
        st.dataframe(_facts_table(outcome, ids), hide_index=True)
    with scenario:
        st.caption(T.CONDITIONAL_IMPACT + " " + T.NO_PORTFOLIO_SUM)
        for s in run.bundle.scenarios.values():
            st.markdown(f"**{T.SCENARIO_TEMPLATES[s.template]}** · "
                        f"{_segment_label(outcome, s.segment_id)} · `{s.scenario_id}`")
            st.dataframe(_facts_table(outcome, s.result_fact_ids), hide_index=True)
        st.markdown("**Test assumptions — not historical facts**")
        st.dataframe(pd.DataFrame([{
            "label": a.label, "value": format_value(a.value, a.unit),
            "bounds": f"{format_value(a.bounds.minimum, a.unit)} to "
                      f"{format_value(a.bounds.maximum, a.unit)}",
            "set by": a.selector.value, "assumption": a.assumption_id,
        } for a in run.bundle.assumptions.values()]), hide_index=True,
            )
        st.caption("Edit assumptions in the sidebar; edits start a new run namespace.")


def _report_tab(outcome: RunOutcome) -> None:
    """The short report page, embedded as the downloadable file itself."""
    # Code-rendered and escaped page without scripts, so embedding it as-is is safe.
    st.iframe(outcome.html, height=1100)
    st.warning("Downloads contain private derived data (labels, totals and evidence). Store "
               "them only in an approved private location.")
    if st.checkbox("I understand these files are private derived data", key="private_ack"):
        run_id = outcome.artifact.manifest.run_id
        st.download_button("Download report (HTML)", outcome.html,
                           file_name=f"{run_id}_report.html", mime="text/html", key="dl_html")
        st.download_button("Download report (Markdown)", outcome.report.markdown,
                           file_name=f"{run_id}_report.md", mime="text/markdown",
                           key="dl_report")


def _details_tab(outcome: RunOutcome) -> None:
    """Everything behind the short report: quality, metrics, candidates, drill-downs, audit."""
    sections = st.tabs(["Data quality", "Summary", "Metrics", "Candidates", "Progress",
                        "Drill-down", "Validation", "Full audit report"])
    with sections[0]:
        _warnings(outcome)
    with sections[1]:
        _summary_tab(outcome)
    with sections[2]:
        _metrics_tab(outcome)
    with sections[3]:
        _candidates_tab(outcome)
    with sections[4]:
        _progress_view(outcome.events)
    with sections[5]:
        _drilldown_tab(outcome)
    with sections[6]:
        _validation_tab(outcome)
    with sections[7]:
        # Escape "$" so Streamlit does not read money as LaTeX; the file keeps plain "$".
        st.markdown(outcome.full_report.markdown.replace("$", "\\$"))
        if st.session_state.get("private_ack"):
            st.download_button("Download full run bundle (JSON evidence, zip)",
                               bundle_zip(outcome), file_name=(
                                   f"{outcome.artifact.manifest.run_id}_bundle.zip"),
                               mime="application/zip", key="dl_bundle")
        else:
            st.caption("Tick the private-data acknowledgement on the Report tab to download "
                       "the full run bundle.")


def _show(outcome: RunOutcome) -> None:
    _banner(outcome)
    report, details = st.tabs(["Report", "Details"])
    with report:
        _report_tab(outcome)
    with details:
        _details_tab(outcome)


# --- Main ------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="adops-optimizer", layout="wide")
    ss = _state()
    _collect_finished_job()
    st.title("Ad-performance optimizer")
    st.caption("Upload a daily ad-performance CSV to get a short, ranked list of optimization "
               "recommendations. Every number is computed by code from your data.")
    mode, data, config = _sidebar()

    if mode == MODE_DEMO:
        st.info("Synthetic demo: a previously generated run is shown. No model is called.")
        _show(load_demo_run())
        return

    if data is None:
        st.info("Upload a CSV export, or choose the synthetic sample or the cached demo run.")
        return
    if len(data) > config.input.max_bytes:
        st.error("The file exceeds the configured size limit; it was not processed.")
        return

    key = cache_key(input_sha256(data), config)
    job = ss.jobs.current
    running = job is not None and job.active
    cached = ss.cache.get(key)
    c1, c2, c3 = st.columns(3)
    start = c1.button("Run analysis", type="primary", key="run", disabled=running)
    show_cached = c2.button("Show cached previous run", key="show_cached",
                            disabled=cached is None)
    if c3.button("Cancel run", key="cancel", disabled=not running):
        job.cancel()
        st.info("Cancellation requested: no new stage or model call will start. An in-flight "
                "call is not interrupted and may still be billed.")
    if start:
        try:
            job = ss.jobs.submit(key, data, config)
        except RuntimeError as exc:
            st.error(str(exc))
        else:
            ss.outcome, ss.run_error, running = None, None, True
    if show_cached and cached is not None:
        ss.outcome = cached
    if running:
        _live_progress()
        return
    if ss.run_error:
        status, message = ss.run_error
        st.error(f"Run {status}: {message}")
        if ss.jobs.current is not None:
            _progress_view(ss.jobs.current.snapshot()[1])
    outcome: RunOutcome | None = ss.outcome
    if outcome is not None:
        if outcome.artifact.manifest.input_sha256 != key.input_sha256:
            st.info("The last result belongs to a different input; it is not shown.")
            return
        _show(outcome)


if __name__ == "__main__":
    main()
