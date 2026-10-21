# adops-optimizer

A tool that takes a daily ad-performance CSV export, measures it with
deterministic Python, asks a self-hosted open-weight LLM to judge and explain
the opportunities it finds, and produces a short optimization report of
controlled tests and investigations.

Python measures; the model evaluates and explains by referencing typed
evidence; code validates every reference and renders every number.

Bring your own CSV export. Raw data, derived reports and recordings are never
committed to this repository; only independently generated synthetic data is.

## Status

| Part | State |
|---|---|
| Contracts, IDs, schemas, synthetic CSV generator | done |
| Strict CSV loader and data-quality rules DQ01–DQ20 | done |
| Self-hosted model deployment (`deploy/`), profiles and fail-closed client | done; see [`deploy/README.md`](deploy/README.md) |
| Aggregation, all eight detector families and conditional scenarios | done |
| Report renderer, run bundle, runner, CLI, Streamlit app | done, against synthetic run fixtures |
| Evidence builder and validator | in progress (stubs raise `NotImplementedError`) |
| Prompts, analysis, tools and ranking | in progress (stubs) |

Until the remaining modules land, the app's **cached demo run** and the CLI's
`demo` command show the complete flow on a synthetic run. A live run loads the
file, checks quality, aggregates, detects candidates and refines quality, then
stops at the evidence stage with a clear message instead of crashing.

Analytics (`pipeline/metrics.py`, `detectors.py`, `scenarios.py`): observed
groups only, rates as ratios of sums with null-with-reason denominators,
eligibility gates with 0.5x/2x sensitivity and a stability gate, candidates
with daily, peer, coverage, concentration and contrary evidence plus an
overlap graph, and code-only conditional scenarios. Thresholds are versioned
defaults in `AnalyticsConfig` and `DetectorConfig`.

## Architecture

```text
CSV bytes (in memory; SHA-256 before parsing)
  -> schema + load + data-quality assessment            pipeline/load.py, quality.py
  -> additive totals, daily series, observed groups     pipeline/metrics.py
  -> all detector families + sensitivity views          pipeline/detectors.py
  -> refined quality, overlap-aware candidate selection pipeline/quality.py
  -> immutable typed evidence + conditional scenarios   pipeline/evidence.py, scenarios.py
  -> self-hosted model: one analysis per candidate,     llm/analysis.py, tools.py
     bounded drill-down tools
  -> strict validation, at most one repair              llm/validator.py
  -> model ranking/merge + summary themes, validated    llm/ranking.py
  -> code-rendered Markdown report + audit map          report/render.py
  -> private run bundle, Streamlit drill-down, download report/bundle.py, app.py
```

`runner.py` runs these stages in order, emitting a progress event per stage
and per candidate; `app.py` and `cli.py` are thin front ends over it.

| Python | Model | Validator and renderer |
|---|---|---|
| Schema, data traps, metric definitions, materiality | Which candidates deserve attention, which are weak | Every ID resolves in the current run namespace |
| Every quantity, comparison and scenario | Which evidence, test template and codes apply | Metric, unit, segment, period and role match |
| Every detector over every observed group | Counter-evidence, rejection, merging | Assumptions within approved bounds |
| Overlap and double-counting | Priority and qualitative explanation codes | Every number in the report is rendered from a typed object |

No raw rows reach the model. Segment labels are treated as untrusted data and
escaped wherever they are displayed.

## Input and metrics

The export has 17 columns: `Day, Supply ID, Country, Size, Placement Type,
Device Type, Demand Requests, Demand Responses, Our Bidfloor, Demand Bidprice,
Demand sRPM $, Demand Wins, Supply Impressions, Revenue, Cost, Demand eCPM,
Profit $`. Rows are daily aggregates, not auction-level data, so the tool
recommends controlled tests and investigations, never proven fixes.

- Counters and money are additive; prices and rates are not. Every rate is a
  ratio of sums and weighted by traffic, never an average of row rates.
- Exported sRPM is revenue per **million** requests, verified per file.
- Money is "export dollars": the currency code is not supplied, so none is
  asserted and nothing is converted.
- Reported profit is preserved; revenue minus cost is reconciled separately
  within declared cent-rounding bounds.
- A zero denominator gives an undefined value with a reason, never zero.

## Data-quality warnings

Twenty versioned rules (DQ01–DQ20) cover parsing and schema, unknown labels,
duplicate grain, counter validity, money without traffic, partial days,
intermittent coverage, one- or two-day concentration, spikes, placeholder
countries and sizes, profit reconciliation, sRPM and eCPM units, row versus
traffic weighting, request granularity, bid/floor semantics, signed
adjustments, threshold sensitivity and causal limits. Thresholds are defaults
in `contracts/config.py` and part of the configuration hash.

```python
from contracts.config import InputConfig, QualityConfig
from pipeline.load import load_csv
from pipeline.quality import assess_quality, render_message

with open("export.csv", "rb") as source:
    data = load_csv(source, InputConfig())
report = assess_quality(data, QualityConfig())
facts = {f.fact_id: f for f in report.facts}
for issue in report.issues:
    print(issue.severity, render_message(issue, facts))
```

- `load_csv` hashes the bytes, enforces size and row bounds, requires the 17
  header names (extra columns are ignored and reported), parses dates
  strictly, keeps Supply ID as an opaque string and keeps numbers as exact
  decimals. A cell it cannot parse becomes `None` and is recorded in
  `parse_audit`; nothing is coerced to zero and no row is dropped.
- `assess_quality` runs the first pass (file, schema, counters, money,
  rates, account-day coverage). `refine_quality` adds candidate coverage,
  concentration, spike, materiality and peer checks once candidates exist; it
  only appends.
- Each issue carries a severity, inference restrictions and fact references;
  its message is rendered by `render_message` from those facts, and the report
  shows that message as one audited span.

Warnings are the **first** section of every report and the first thing the app
shows. A fatal issue blocks model recommendations; the report then contains
the quality findings and a diagnostic summary only. Rules that cannot be
tested on a file are reported as "not testable", never as passed.

## The report

`report/render.py` builds every recommendation in a fixed test form:

1. **Test / investigation** and its code-rendered target
2. **What the data shows**: templated observations (value, comparison with a
   baseline or matched peer, trend, coverage, concentration) with fact IDs,
   period, denominators and units
3. **What it does not prove**: counter-evidence, quality restrictions,
   coverage, family-specific and causal limits
4. **Hypothesis — unverified**
5. **Controlled experiment**: design, target eligibility, treatment and
   holdout, randomization unit, reversible rollout, prerequisites
6. **How to judge it**: incremental total profit, guardrails, duration and
   stop rules from assumptions
7. **Conditional impact if the test works**, or "not estimable from this
   export"
8. **Test assumptions — not historical facts**, with bounds and who set them
9. **Risks / additional data**
10. **Confidence in the observation**; the intervention effect is unproven

The renderer writes each value through one formatter and records a span
(offsets, source ID, role) for it. `audit_report` then requires every digit in
the Markdown to lie inside a span whose source resolves in the run (fact,
assumption, scenario, segment, period, manifest); `render_report` refuses to
return a report that fails. Static wording in `report/text.py` contains no
digits, which a test enforces. The model never writes report text.

Run statuses are unmistakable in the header and the app banner:
`final`, `partial` (some candidates or the ranking did not validate),
`deterministic_only` (no model output; a diagnostic draft), `failed` and
`cached_previous`. A sample report rendered from the synthetic run is in
[`examples/synthetic/demo_report.md`](examples/synthetic/demo_report.md).

## Detectors, tools and scenarios

Eight detector families produce candidates, not conclusions: scale winners,
wasted requests, margin leaks, high and low bid/floor investigations, wins
versus impressions discrepancies, daily trends and combination outliers.
Overlapping parent/child candidates are grouped so impacts are never summed.

Three deterministic drill-down tools (`breakdown`, `daily_trend`,
`compare_to_peers`) are available to the model, at most three calls per
candidate, and in the app's Drill-down tab. Truncated breakdowns keep an
"other" bucket so coverage stays complete.

Conditional scenarios (additional eligible requests, throttle, margin
sensitivity, rendering sensitivity) are computed by code from historical facts
and labelled test assumptions, over the observed period, with displaced
profit and revenue/profit at risk stated. Floor changes get no forecast from
daily averages.

## Model and serving

The default profile is the official FP8 checkpoint of `Qwen/Qwen3.8-27B-FP8`
(Apache-2.0) on one L40S through a self-built vLLM server, with thinking
disabled on every call, a bounded 16K context and JSON-schema-constrained
output. FP8 is chosen because BF16 weights alone exceed one 48GB L40S; the
bounded context trades long packs for fit and cost. No task-specific model
ranking is claimed from publisher benchmarks. A lower-change contingency and a
cheaper fallback live in the same profile file and must pass the same
acceptance tests; a fallback is never substituted silently.

Every model setting (immutable revision, vLLM and transformers pins, context
and output bounds, parsers) lives in `config/model_profiles.yaml`, loaded by
`llm/profiles.py`; the image digest stays unresolved until the acceptance
tests pass. `deploy/modal_vllm.py` serves the active profile with vLLM behind
a bearer-key check; [`deploy/README.md`](deploy/README.md) covers setup,
serving, cold starts (scale to zero after five idle minutes, three to five
minutes to wake) and stopping. A profile's SHA-256 goes into the run manifest
and cache key, and its model ID and revision into the report header.

`llm/client.py` talks to the endpoint over HTTPS. `LLM_BASE_URL`,
`LLM_API_KEY` and `LLM_ALLOWED_BASE_URLS` are required; the client refuses
missing configuration, non-allowlisted or plain-HTTP endpoints, embedded
credentials and known hosted model APIs, and there is no hosted-API
fallback. It polls readiness with backoff before the first request, which the
app shows as the cold-start stage. Credentials stay in local environment or the deployment's secret
store and are never sent to the browser. If the endpoint or model layer is
unavailable, the run becomes an explicitly labelled `deterministic_only`
draft.

## Cost and authorization

Deployment and spending need separate authorization; nothing in this
repository starts a GPU on its own. Modal pricing checked on 21 October 2026:
one L40S at about $1.95/hour of GPU time, roughly $2–2.70/hour with host CPU
and memory while a container runs.
Loading, compilation, retries and warm idle are billable; cached weights do
not make cold starts free. The serving plan is one replica, no warm minimum,
one concurrent generation and a five-minute idle scaledown. Provider credits may
cover usage but are not a guaranteed $0 cost, and an app-side counter is not
a provider-enforced spending cap.

## Running it

Requires [uv](https://docs.astral.sh/uv/); Python 3.12 is selected from
`.python-version` and dependencies are pinned in `uv.lock`.

```sh
uv sync
uv run pytest                      # no network, no model, synthetic data only
uv run streamlit run app.py        # local app
uv run python cli.py demo          # print the synthetic cached run
uv run python cli.py run INPUT.csv [--model-profile ID] [--out PRIVATE_DIR]
uv run python scripts/wake_model.py # wake the endpoint before a live run
uv run python cli.py show PRIVATE_DIR/run_<id>
```

In the app, choose **Upload a CSV**, the **Synthetic sample CSV** or the
**Cached demo run (synthetic)** in the sidebar. A run shows, in order: the
status banner, data-quality warnings, then tabs for the data summary (input
fingerprint, account totals, reconciliation, full and sensitivity cohorts),
sortable metrics at every aggregation level, flagged candidates with daily
charts, matched comparisons and contrary evidence (including rejected and
exploratory ones), live stage and per-candidate progress (readiness, warm
inference, tool calls, validation), drill-downs, validation results with a
fact inspector and separate observation and scenario views, and the report
preview with downloads. Test assumptions are edited in the sidebar within
approved bounds; an edit creates a new run namespace and cache key. A run can
be cancelled: no new stage or model call starts, but an in-flight call is not
interrupted and may still be billed.

Live tests are deselected by default; run them explicitly with
`uv run pytest -m live` once an endpoint is configured (see `.env.example`
and `deploy/README.md`).
Set `PRIVATE_INPUT_DIR` and `PRIVATE_RUN_DIR` to directories outside this
repository for private inputs and run bundles.

## Caching, privacy and retention

- A cached result is reused only when the complete key matches:
  `(input_sha256, schema_version, analytics_config_hash, model_profile_hash,
  prompt_hash, tool_version, assumption_hash)`. A different file, config,
  profile, prompt, tool version or assumption never reuses a result.
- The cache is per browser session and in memory only. The app uses no global
  Streamlit cache; uploads and reports never leave the session unless you
  download them.
- A cached result is labelled **CACHED PREVIOUS RUN — not fresh inference**
  on screen and in the report header, with its original generation time,
  input hash, model ID and revision and config hash. Re-rendering does not
  change the generation time. Without a matching cache, the app offers
  deterministic diagnostics or the synthetic demo; it never shows another
  file's report.
- Downloads appear only after acknowledging that they contain private derived
  data. The CLI writes a run bundle only with an explicit `--out`.
- A run bundle (`manifest.json`, `run.json`, `quality.json`, `facts.json`,
  `assumptions.json`, `scenarios.json`, `candidates.json`, `analyses.json`,
  `validation.json`, `report.json`, `report.md`, `render_audit.json`) records
  each file's SHA-256 in the manifest; an edited or incomplete bundle is
  refused when loaded. The source path is never recorded.
- Errors shown in the app or CLI carry stage-level messages only, never
  labels, keys or raw exception text. Streamlit usage statistics are disabled
  in `.streamlit/config.toml`.
- The real-data report and the demo recording are delivered privately, not
  through this repository.

## Evaluation

The synthetic generator plants known traps (partial final day, two-day revenue
spike, placeholder labels, counter violations, request granularity, profit
rounding, cent-rounded zero revenue, loss-making and intermittent slices) and
writes their labels to `<name>.traps.json`. Detector precision/recall against
those labels and validator rejection counts are reported once the pipeline and
model lanes are integrated. Each run's manifest records input, configuration,
profile, prompt and tool hashes so a report can be reproduced as an artifact;
a repeated model run may still rank differently.

Known limits: daily aggregates cannot show causation, auction-level floor
effects, hourly completeness, request-processing costs or alternative demand;
the export has no partner dimension.

## Demo video outline

1. The problem and the split: Python measures, the model evaluates, code
   validates.
2. Architecture, the selected FP8 model and proof of the authenticated
   self-hosted endpoint, without secrets.
3. Upload, quality warnings, aggregates, candidate analysis with a tool call,
   validation and the report, stating whether the endpoint was warm.
4. Two or three recommendations: observations with fact IDs, contrary
   evidence, the controlled test, separate assumptions and conditional impact.
5. A validator rejection (wrong segment, unit or stale fact).
6. Download, the cached-run banner with exact-input matching, then limits and
   costs.

The real-data recording is private; any public demo uses synthetic data.

## Future work

Deeper model comparison on task-specific evidence packs, auction-level and
floor-loss evidence, variance-aware experiment sizing, scheduled runs,
multi-partner comparison and an optional fast triage step.

## Layout

```text
contracts/          shared data contracts (pydantic v2), IDs, templates, JSON Schemas
pipeline/           load, quality, metrics, detectors, scenarios (implemented);
                    evidence (stub); scope.py shared segment/period IDs
llm/                client and profiles (implemented); prompts, analysis, tools,
                    ranking, validator (stubs)
report/
  render.py         Markdown renderer and post-render audit
  text.py           reviewed wording (no digits)
  bundle.py         private run bundle: assemble, hash, write, verify, relabel as cached
runner.py           stage orchestration, progress, cancel, jobs, cache key, session cache
cli.py, app.py      command line and Streamlit front ends
deploy/             self-hosted vLLM serving on Modal
config/             run configuration files; model_profiles.yaml holds every model setting
scripts/
  generate_synthetic.py   seeded synthetic export with planted traps
  build_demo_run.py       synthetic run fixture for the demo and tests
  wake_model.py           wake the self-hosted endpoint before a demo
  export_contracts.py     regenerates contracts/schemas and contract examples
examples/synthetic/ synthetic contract examples, demo run bundle and report
tests/              contracts, unit, integration (incl. Streamlit AppTest), live (opt-in)
```

After changing a contract model, regenerate schemas and examples with
`uv run python scripts/export_contracts.py`; after changing the renderer or
the demo run, regenerate the fixture with
`uv run python scripts/build_demo_run.py`.

```sh
uv run python scripts/generate_synthetic.py            # rewrites tests/fixtures/synthetic/
uv run python scripts/generate_synthetic.py --seed 7 --out /tmp/other.csv
```
