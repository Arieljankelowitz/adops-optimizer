# adops-optimizer

A tool that takes a daily ad-performance CSV export, measures it with
deterministic Python, asks a self-hosted open-weight LLM to judge and explain
the opportunities it finds, and produces a short optimization report of
controlled tests and investigations.

Python measures; the model evaluates and explains by referencing typed
evidence; code validates every reference and renders every number.

Bring your own CSV export (day, supply ID, country, size, placement type,
device type, demand requests/responses, bid floor, bid price, sRPM, wins,
impressions, revenue, cost, eCPM, profit). Raw data, derived reports and
recordings are never committed to this repository; only independently
generated synthetic data is.

## Status

Frozen contracts, a synthetic data generator, the self-hosted model
deployment (`deploy/`, see `deploy/README.md`) and its fail-closed client
(`llm/client.py`). The pipeline, model reasoning, report and app are not
implemented yet; their functions raise `NotImplementedError`.

## Layout

```text
contracts/          shared data contracts (pydantic v2)
  enums.py          closed vocabularies: input columns, dimensions, metrics, units, severities, DQ rules
  ids.py            stable ID and run-namespace rules
  models.py         facts, assumptions, scenarios, quality, candidates, model output IR, run artifacts
  config.py         configuration types with default thresholds
  runtime.py        in-process types: loaded data, metrics store, registries, model/tool protocols
  templates.py      observation templates and reviewed qualitative codes
  schemas/v1/       exported JSON Schemas (generated)
pipeline/           load, quality, metrics, detectors, scenarios, evidence (stubs)
llm/                client, profiles, prompts, analysis, tools, ranking, validator
report/             report rendering (stub)
deploy/             self-hosted vLLM serving on Modal
config/             run configuration files; model_profiles.yaml holds every model setting
runner.py, cli.py, app.py   orchestration and entry points (stubs)
scripts/
  generate_synthetic.py   seeded synthetic export with planted data-quality traps
  export_contracts.py     regenerates contracts/schemas and examples/synthetic
examples/synthetic/ synthetic contract examples
tests/
  contracts/        signature and schema tests
  unit/             IDs, registries, synthetic generator
  integration/      cross-module checks against the synthetic fixture
  live/             opt-in tests against a real endpoint (marker: live)
  fixtures/synthetic/   generated fixture and its trap manifest
```

## Install and test

Requires [uv](https://docs.astral.sh/uv/); Python 3.12 is selected from
`.python-version` and dependencies are pinned in `uv.lock`.

```sh
uv sync
uv run pytest
```

Live tests are deselected by default; run them explicitly with
`uv run pytest -m live` once an endpoint is configured (see `.env.example`
and `deploy/README.md`).

## Synthetic data

```sh
uv run python scripts/generate_synthetic.py            # rewrites tests/fixtures/synthetic/
uv run python scripts/generate_synthetic.py --seed 7 --out /tmp/other.csv
```

The generator invents every ID and number from a fixed seed and plants known
traps: a partial final day, a two-day revenue spike, placeholder country and
size labels, responses above requests, impressions without requests, request
counts on a common granularity, sRPM expressed per million requests, profit
rounding residuals, cent-rounded zero revenue, a loss-making slice and an
intermittent slice. `<name>.traps.json` lists each one for tests.

After changing a contract model, regenerate schemas and examples:

```sh
uv run python scripts/export_contracts.py
```
