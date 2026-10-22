# Video / live-demo outline (about four minutes)

Use **synthetic data only**, including on screen, in terminal history and in
any downloads. Hide keys, workspace names and unrelated browser tabs. This is an
outline for the recording or live walkthrough, not a claim that a video is included.

## Before recording

Follow the [README quick start](../README.md) and launch Streamlit. Have the
synthetic cached demo available for a reliable walkthrough. For fresh AI analysis,
configure an already-authorized self-hosted endpoint first, allow for its cold
start and run the synthetic sample ahead of the recording. Do not spend the whole
video waiting for inference. Never present the cached fixture as live model output.

## 0:00–0:30 — Problem

“Daily ad exports contain lots of slices and misleading averages. This tool
turns one export into a short list of evidence-backed tests and investigations,
not an automatic set of account changes.”

## 0:30–1:00 — Architecture and model

Show the simple diagram in [ARCHITECTURE.md](ARCHITECTURE.md).

“Python checks the data and measures it. Self-hosted Qwen3.8-27B-FP8 judges the
opportunities and can ask Python tools to explore further. Code checks the
references and writes the final numbers. The model runs through vLLM on one
Modal L40S, with an authenticated OpenAI-compatible API.”

## 1:00–2:00 — Live interaction with the app

1. Choose **Synthetic sample CSV** in the sidebar. Point out that it contains
   deliberately planted problems, not customer data.
2. Keep **None: deterministic analytics only** and click **Run analysis**.
   Show progress, then the diagnostic-only status and quality warnings.
3. For a model-enabled walkthrough, select `qwen38_fp8_l40s_v1` and show the
   prepared fresh synthetic run, including its actual status and generation time.
   Otherwise switch to **Cached demo run (synthetic)** and explicitly say:
   “This is a saved synthetic fixture. No inference is running now.”
4. Open **Report**. Show the compact summary, recommendation cards and charts.
   Mention that **Details** holds metrics, drill-downs and validation evidence.

## 2:00–3:20 — What the AI found

Choose two cards that actually appear; do not promise a fixed live-model answer.
For each, point to the slice, current requests/revenue/profit, the observation,
the proposed test and the estimate or “not estimable” label.

“Here is the measured signal; here is the reason to investigate; here is what
could contradict it. The proposed action is a controlled test, not proven uplift.”

In a fresh model run, show a **found by AI** card if one survived exploration and
validation, and its supporting drill-down. If none did, say so. In the cached
fixture, describe the cards as scripted examples of the output format, not
measured evidence of model quality. Explain that estimates depend on stated
assumptions and that code supplies every displayed number.

## 3:20–4:00 — Deliverable and limits

Show the privacy acknowledgement and HTML download. Open the self-contained
report in a browser if time permits.

“Daily aggregates cannot establish causality or forecast floor-change uplift.
The tool does not modify production settings. Invalid AI findings are omitted;
partial, cached and diagnostic-only results are labelled. Cold starts take
minutes and GPU time is billed, so this favors considered analysis over instant
chat.”

Close with the README and its architecture, model and deployment links. The
private dataset's results and any private recording are delivered separately,
never committed here.
