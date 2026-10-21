# Self-hosted model on Modal

`deploy/modal_vllm.py` serves the active profile from
`config/model_profiles.yaml` (by default `Qwen/Qwen3.8-27B-FP8` at a pinned
revision) with vLLM on one L40S GPU, behind vLLM's bearer-key check. The tool
talks to it through `llm/client.py`; there is no hosted-API fallback.

Every command below except `--help` starts billable containers. Run them from
the repository root with the `deploy` dependency group:

```sh
uv sync --group deploy
uv run --group deploy modal setup        # once, if this machine is not logged in
```

## One-time setup

The server reads its key from the Modal secret `adops-llm-key` (variable
`LLM_API_KEY`). Create it once with a long random value, and keep the same
value in your local `.env`:

```sh
uv run --group deploy modal secret create adops-llm-key LLM_API_KEY="$(openssl rand -hex 32)"
```

Download the pinned weights (about 31 GB) into the `hf-cache` volume on a CPU
container. Already-downloaded files are reused:

```sh
uv run --group deploy modal run -m deploy.modal_vllm::download_model
```

## Serve

Development, a temporary URL that lives while the command runs (Ctrl-C stops it):

```sh
uv run --group deploy modal serve -m deploy.modal_vllm
# https://<workspace>--adops-llm-serve-dev.<region>.modal.run
```

Stable URL that survives the terminal closing:

```sh
uv run --group deploy modal deploy -m deploy.modal_vllm
# https://<workspace>--adops-llm-serve.modal.run
```

Configure the client in `.env` (see `.env.example`) with the printed URL plus
`/v1`. The same exact URL must also appear in `LLM_ALLOWED_BASE_URLS`; the
client refuses anything else, plain HTTP, embedded credentials and known
hosted model APIs.

## Wake before a demo

The container scales to zero after **5 idle minutes**. A cold start (container,
weights from the volume, vLLM start) takes about **3–5 minutes**, so wake it
first:

```sh
set -a; source .env; set +a
uv run python scripts/wake_model.py
```

The client itself polls readiness with backoff for up to 600 s before its
first request.

## Speed and limits

- About **11 output tokens per second** with `--enforce-eager`; a full
  2,048-token answer takes about three minutes, close to the 180 s per-request
  timeout. Keep answers compact.
- Requests longer than 150 s are answered with a same-origin redirect by Modal;
  the client follows it within the same deadline.
- Thinking is disabled on every request (`chat_template_kwargs.enable_thinking:
  false`). With thinking on, the model can spend the whole output budget
  reasoning and return no answer; the client treats empty content as a failure.

## Cost

Modal pricing checked 21 October 2026: L40S **$0.000542/s ≈ $1.95/hour** for the
GPU alone, plus CPU ($0.0000131 per core-second) and memory ($0.00000222 per
GiB-second); budget roughly **$2–2.70/hour** while a container runs. Loading,
idle time before scale-down and retries are billed too. Check the Modal
dashboard for actual usage.

## Stop

- `modal serve`: press Ctrl-C.
- `modal deploy`: `uv run --group deploy modal app stop adops-llm`.

Idle containers stop on their own after five minutes either way.

## Live test

One synthetic, schema-constrained request against the running endpoint
(deselected from the normal test run):

```sh
set -a; source .env; set +a
uv run pytest -m live -s
```

## Serving pins and fixes

Pins come from the profile: vLLM `0.32.0` and transformers `5.19.0` (vLLM
0.32.0 needs `>=5.16.1,<5.20`). The image sets `VLLM_USE_FLASHINFER_SAMPLER=0`,
because the FlashInfer sampler compiles a kernel on the first generation and
the slim image has no CUDA compiler, and `HF_XET_HIGH_PERFORMANCE=1`, the
successor of the deprecated `HF_HUB_ENABLE_HF_TRANSFER`. The Modal resource
names (`adops-llm`, `adops-llm-key`, `hf-cache`, `vllm-cache`) are fixed so an
existing download is reused. `image_digest` in the profile stays unresolved
until the serving image passes the acceptance tests and is locked.
