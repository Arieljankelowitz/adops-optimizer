# Model and deployment

## Selected model

The active profile in [`config/model_profiles.yaml`](../config/model_profiles.yaml)
is `qwen38_fp8_l40s_v1`: **Qwen/Qwen3.8-27B-FP8**, an Apache-2.0 open-weight
model, pinned to a specific checkpoint revision.

This task needs instruction following, evidence-based judgment and tool use,
not a model doing spreadsheet arithmetic. Qwen3.8 offers relevant reasoning and
instruction-following capabilities; publisher comparisons suggest improvements
over its predecessor, but are not evidence of better recommendations on this
application. The official FP8 checkpoint fits the single-48-GB-GPU deployment
budget; BF16 weights alone would exceed that GPU's memory. It is a practical
quality/footprint choice, not a claim that it wins a task-specific benchmark.

## How it is served

[`deploy/modal_vllm.py`](../deploy/modal_vllm.py) serves the model with **vLLM on
one Modal L40S**. The profile pins vLLM 0.32.0 and Transformers 5.19.0, limits
the context to 16,384 tokens, and serves one generation at a time. Thinking is
disabled so the output budget goes to the structured answer.

The stable endpoint follows this pattern (placeholder workspace only):

```text
https://<workspace>--adops-llm-serve.modal.run/v1
```

vLLM checks a bearer key stored in a Modal secret; the application receives the
same key through its local environment. The container scales to zero after five
idle minutes. Deployment persists independently of a local terminal, but does
not require a GPU to stay warm.

As recorded in [`deploy/README.md`](../deploy/README.md):

- Cold startup is roughly **3–5 minutes**; the client allows a separate readiness
  wait before inference.
- Eager-mode generation is about **11 output tokens/second**. A full 2,048-token
  answer takes about three minutes, near the per-request timeout. A multi-call
  report is not an instant interaction.
- Recorded pricing (21 October 2026) is about **$1.95/GPU-hour**, or roughly
  **$2–2.70/hour** including host CPU and memory while running. Loading, retries
  and warm idle also cost money; scale-to-zero does not make a run free.

These are deployment notes, not a guaranteed per-report latency or price. The
profile is marked serving-smoke-tested, not fully acceptance-tested; the image
digest remains unresolved pending compatibility acceptance. No comparative
application-quality evaluation is claimed.

## How the application talks to it

`llm/client.py` uses `httpx` over HTTPS to call the **OpenAI-compatible
`/v1/chat/completions` API** on this self-hosted server. OpenAI-compatible is a
protocol, not a hosted OpenAI inference dependency. There is no hosted-API fallback.

The client reads `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_ALLOWED_BASE_URLS` from the
process environment. It requires an explicitly allowed endpoint and sends the key
as bearer authorization. Prompts contain computed evidence and segment labels,
not raw CSV rows. Responses use **JSON-Schema-constrained JSON**: evidence IDs,
decisions and explanation/test codes rather than invented report numbers.

For tool calls, the default is a schema-based loop: the model returns a typed tool
request, Python executes the approved handler, and its evidence goes back to the
model. Native OpenAI-style tool calls are supported but disabled by default until
the deployed parser passes compatibility tests. Both paths use the same bounded
tools and validation. Accepted decisions are then checked and rendered by code,
as described in [ARCHITECTURE.md](ARCHITECTURE.md).
