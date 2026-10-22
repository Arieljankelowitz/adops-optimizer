# adops-optimizer

Turn a daily ad-performance CSV into a short HTML optimization report:
**data in → Python measures → AI judges and explores → fact-checked report.**

Python computes the metrics and possible opportunities. A self-hosted open-weight
LLM decides which deserve attention, asks for drill-downs, and recommends tests.
Code checks its evidence and renders the numbers. The output is a decision aid,
not proof that a change will increase profit.

## Quick start

Requires `uv` and Python 3.12 (selected by `.python-version`). From the repository root:

```sh
uv sync --frozen
uv run streamlit run app.py
```

In the sidebar, choose **Synthetic sample CSV**, leave **Model profile** at
**None: deterministic analytics only**, then click **Run analysis**. This runs
fresh Python analytics without inference and produces a labelled diagnostic draft.
Choose **Cached demo run (synthetic)** to inspect a complete example report
without a model call; it is a saved fixture, not fresh AI analysis.

### Command line: no model needed

```sh
uv run python cli.py run tests/fixtures/synthetic/ad_performance.csv --html /tmp/adops-diagnostic.html
uv run python cli.py demo --html /tmp/adops-demo.html
```

Open the resulting HTML file in a browser. Markdown also prints to the terminal.
For a full run bundle, add `--out /tmp/adops-runs`; the report lands at
`/tmp/adops-runs/run_<id>/report.html`, alongside Markdown and audit artifacts.
Without `--html` or `--out`, the CLI saves nothing. In Streamlit, the **Report**
tab offers HTML and Markdown downloads after the privacy acknowledgement.

### With the self-hosted model

Configure an existing endpoint following [deployment instructions](deploy/README.md).
Export `LLM_BASE_URL` (ending in `/v1`), `LLM_API_KEY` and
`LLM_ALLOWED_BASE_URLS` (including that exact base URL) into the launching shell;
the app does not automatically load `.env`.

```sh
uv run python cli.py run tests/fixtures/synthetic/ad_performance.csv --model-profile qwen38_fp8_l40s_v1 --html /tmp/adops-model.html
```

Alternatively, select `qwen38_fp8_l40s_v1` in Streamlit before running the sample.
This uses billable inference when configured. If the model is unavailable, the
report is labelled **deterministic-only**; incomplete model analysis is labelled
**partial**, never silently presented as complete. The offline quick starts need
no credentials. Live model quality and serving acceptance require separate testing.

## Read more

- [Architecture](docs/ARCHITECTURE.md): the four-stage flow and its limits.
- [Model](docs/MODEL.md): selection, self-hosting and application protocol.
- [Video / live-demo outline](docs/VIDEO_SCRIPT.md): a short synthetic walkthrough.
- [Deployment](deploy/README.md): setup, latency, cost and stopping the endpoint.
- [Example report](examples/synthetic/demo_report.html) and [Markdown version](examples/synthetic/demo_report.md).

## Results on the provided dataset

**Placeholder — results are delivered separately with the hand-in.**
The real-data report is private and must not be committed to this repository.

## Privacy and limits

Only independently generated synthetic data belongs here. Keep private CSVs,
reports, run bundles and recordings outside the repository. Raw rows are not sent
to the model; computed evidence includes segment labels, so treat the endpoint
and its credentials as private too.

Daily aggregates support investigations and controlled tests, not causal claims
or reliable floor-change forecasts. The tool does not change ad-serving settings.

## Offline tests

```sh
uv run pytest
```

The normal suite uses synthetic data and mocks; live tests are excluded by default.
It verifies software behavior, not recommendation quality from the deployed model.
