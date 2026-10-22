"""The fail-closed model client, against a mocked transport only (no network)."""

import json
from collections.abc import Callable

import httpx
import pytest

from contracts.config import RunBudget
from contracts.runtime import ChatMessage, ModelRequest
from llm.client import (
    ENV_ALLOWED_BASE_URLS,
    ENV_API_KEY,
    ENV_BASE_URL,
    READINESS_BACKOFF_SECONDS,
    ModelAuthError,
    ModelBudgetError,
    ModelConfigError,
    ModelOutputError,
    ModelUnavailableError,
    SelfHostedModelClient,
    create_client,
    parse_endpoint,
)
from llm.profiles import load_profile

BASE = "https://example-workspace--adops-llm-serve.modal.run/v1"
KEY = "synthetic-test-key-0123456789"
ENV = {ENV_BASE_URL: BASE, ENV_API_KEY: KEY, ENV_ALLOWED_BASE_URLS: BASE}
SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["investigate", "reject"]},
        "fact_ids": {"type": "array", "items": {"type": "string", "pattern": "^F_[0-9a-f]{16}$"}},
    },
    "required": ["decision", "fact_ids"],
    "additionalProperties": False,
}
GOOD = json.dumps({"decision": "investigate", "fact_ids": ["F_0123456789abcdef"]})

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _request(max_output_tokens: int = 256) -> ModelRequest:
    return ModelRequest(
        messages=[ChatMessage(role="system", content="Return JSON."),
                  ChatMessage(role="user", content="Synthetic evidence pack.")],
        response_schema_name="candidate_decision",
        response_schema=SCHEMA,
        max_output_tokens=max_output_tokens,
    )


def _models() -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": "adops-analysis"}]})


def _completion(content: str | None = GOOD, finish_reason: str = "stop",
                model: str = "adops-analysis", **message: object) -> httpx.Response:
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": content, **message}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    })


class Server:
    """Records requests; answers ``/models`` as ready unless told otherwise."""

    def __init__(self, completions: list[Handler | httpx.Response],
                 models: Handler | None = None) -> None:
        self.completions = list(completions)
        self.models = models or (lambda _: _models())
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return self.models(request)
        nxt = self.completions.pop(0)
        return nxt(request) if callable(nxt) else nxt

    @property
    def completion_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/chat/completions")]


def _client(server: Server, clock: FakeClock | None = None,
            budget: RunBudget | None = None) -> SelfHostedModelClient:
    clock = clock or FakeClock()
    client = create_client(ENV, budget or RunBudget(), transport=httpx.MockTransport(server),
                           clock=clock, sleep=clock.sleep)
    assert isinstance(client, SelfHostedModelClient)
    return client


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize("missing", [ENV_BASE_URL, ENV_API_KEY, ENV_ALLOWED_BASE_URLS])
def test_missing_environment_is_refused(missing: str) -> None:
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ModelConfigError, match=missing):
        create_client(env, RunBudget())


def test_blank_values_count_as_missing() -> None:
    with pytest.raises(ModelConfigError):
        create_client({**ENV, ENV_API_KEY: "   "}, RunBudget())


def test_key_with_whitespace_is_refused() -> None:
    with pytest.raises(ModelConfigError):
        create_client({**ENV, ENV_API_KEY: "two words"}, RunBudget())


@pytest.mark.parametrize("url", [
    "http://example-workspace--adops-llm-serve.modal.run/v1",
    "http://localhost:8000/v1",
    "ftp://example.test/v1",
    "https://user:secret@example.test/v1",
    "https://example.test/v1?target=elsewhere",
    "https://example.test/v1#frag",
    "https://example.test/",
    "https://example.test/v1/chat",
    "https://203.0.113.7/v1",
    "https://api.openai.com/v1",
    "https://eu.api.openai.com/v1",
    "https://api.anthropic.com/v1",
    "https://generativelanguage.googleapis.com/v1",
    "https://router.huggingface.co/v1",
    "https:///v1",
])
def test_endpoint_rules(url: str) -> None:
    with pytest.raises(ModelConfigError):
        parse_endpoint(url)


def test_hosted_api_is_refused_even_when_allowlisted() -> None:
    url = "https://api.openai.com/v1"
    with pytest.raises(ModelConfigError):
        create_client({**ENV, ENV_BASE_URL: url, ENV_ALLOWED_BASE_URLS: url}, RunBudget())


def test_url_outside_the_allowlist_is_refused() -> None:
    other = "https://other-workspace--adops-llm-serve.modal.run/v1"
    with pytest.raises(ModelConfigError, match="not in"):
        create_client({**ENV, ENV_BASE_URL: other}, RunBudget())


def test_allowlist_matches_exact_host_port_and_path_after_normalization() -> None:
    env = {**ENV, ENV_BASE_URL: BASE.replace(".run/", ".run:443/") + "/",
           ENV_ALLOWED_BASE_URLS: "https://unrelated.example.test/v1, "
                                  + BASE.replace("https://example", "HTTPS://EXAMPLE")}
    client = create_client(env, RunBudget())
    assert isinstance(client, SelfHostedModelClient)
    assert client.endpoint.url == BASE.replace(".run/", ".run:443/")
    for near_miss in (BASE.replace("/v1", "/api/v1"), BASE.replace(".run/", ".run:8443/")):
        with pytest.raises(ModelConfigError):
            create_client({**ENV, ENV_BASE_URL: near_miss}, RunBudget())


def test_plain_http_localhost_needs_the_test_only_switch() -> None:
    url = "http://127.0.0.1:8000/v1"
    env = {**ENV, ENV_BASE_URL: url, ENV_ALLOWED_BASE_URLS: url}
    with pytest.raises(ModelConfigError):
        create_client(env, RunBudget())
    assert create_client(env, RunBudget(), allow_insecure_localhost=True)


def test_request_over_the_profile_output_budget_is_refused() -> None:
    server = Server([])
    with pytest.raises(ModelConfigError):
        _client(server).generate(_request(max_output_tokens=4097))
    assert server.requests == []


# --- happy path ----------------------------------------------------------------


def test_schema_constrained_non_thinking_request() -> None:
    server = Server([_completion()])
    client = _client(server)
    response = client.generate(_request())

    assert json.loads(response.content)["decision"] == "investigate"
    assert (response.prompt_tokens, response.output_tokens, response.finish_reason) == (
        120, 30, "stop")
    assert [r.url.path for r in server.requests] == [
        "/v1/models", "/v1/chat/completions"]
    sent = server.completion_requests[0]
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["model"] == "adops-analysis"
    assert body["max_tokens"] == 256
    assert body["temperature"] == load_profile().temperature
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "candidate_decision", "strict": True, "schema": SCHEMA}}
    assert client.stats.network_attempts == 1
    assert (client.stats.prompt_tokens, client.stats.output_tokens) == (120, 30)
    assert client.stats.requests_succeeded == 1


def test_readiness_runs_once() -> None:
    server = Server([_completion(), _completion()])
    client = _client(server)
    client.generate(_request())
    client.generate(_request())
    assert [r.url.path for r in server.requests].count("/v1/models") == 1


# --- authentication -------------------------------------------------------------


def test_auth_failure_at_readiness_is_not_retried() -> None:
    server = Server([], models=lambda _: httpx.Response(401, json={"error": "Unauthorized"}))
    clock = FakeClock()
    with pytest.raises(ModelAuthError):
        _client(server, clock).generate(_request())
    assert len(server.requests) == 1
    assert clock.sleeps == []


def test_auth_failure_at_generation_is_not_retried() -> None:
    server = Server([httpx.Response(401), _completion()])
    client = _client(server)
    with pytest.raises(ModelAuthError):
        client.generate(_request())
    assert len(server.completion_requests) == 1
    assert client.stats.transport_retries == 0


# --- unusable answers -----------------------------------------------------------


def test_null_content_from_thinking_is_a_failure() -> None:
    server = Server([_completion(content=None, finish_reason="length")])
    client = _client(server)
    with pytest.raises(ModelOutputError) as info:
        client.generate(_request())
    assert info.value.errors == ["empty_content"]
    assert info.value.finish_reason == "length"
    # Spent tokens are still recorded.
    assert client.stats.output_tokens == 30
    assert client.stats.requests_failed == 1


@pytest.mark.parametrize("content", ["", "  \n"])
def test_empty_content_is_a_failure(content: str) -> None:
    with pytest.raises(ModelOutputError, match="no content"):
        _client(Server([_completion(content=content)])).generate(_request())


def test_truncated_answer_is_a_failure() -> None:
    with pytest.raises(ModelOutputError) as info:
        _client(Server([_completion(finish_reason="length")])).generate(_request())
    assert info.value.errors == ["finish_reason:length"]


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_thinking_trace_is_a_failure(field: str) -> None:
    server = Server([_completion(**{field: "Let me think..."})])
    with pytest.raises(ModelOutputError) as info:
        _client(server).generate(_request())
    assert info.value.errors == ["thinking_trace_present"]


def test_non_json_content_is_a_failure() -> None:
    with pytest.raises(ModelOutputError) as info:
        _client(Server([_completion(content="Sure! {decision: investigate}")])).generate(
            _request())
    assert info.value.errors[0].startswith("invalid_json")


def test_schema_invalid_content_reports_machine_readable_errors() -> None:
    bad = json.dumps({"decision": "scale_up", "fact_ids": ["revenue"], "note": "x"})
    server = Server([_completion(content=bad), _completion()])
    client = _client(server)
    with pytest.raises(ModelOutputError) as info:
        client.generate(_request())
    errors = info.value.errors
    assert any(e.startswith("/decision:") for e in errors)
    assert any(e.startswith("/fact_ids/0:") for e in errors)
    assert any("Additional properties" in e for e in errors)
    assert info.value.content == bad
    # The client never repairs or retries an invalid answer itself.
    assert len(server.completion_requests) == 1


def test_answer_from_another_model_is_refused() -> None:
    with pytest.raises(ModelConfigError):
        _client(Server([_completion(model="some-other-model")])).generate(_request())


def test_invalid_response_schema_is_refused_before_sending() -> None:
    server = Server([])
    request = _request().model_copy(update={"response_schema": {"type": "no-such-type"}})
    with pytest.raises(ModelConfigError):
        _client(server).generate(request)
    assert server.requests == []


# --- retry bounds ---------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_one_transport_retry_then_success(status: int) -> None:
    server = Server([httpx.Response(status), _completion()])
    client = _client(server)
    client.generate(_request())
    assert len(server.completion_requests) == 2
    assert (client.stats.network_attempts, client.stats.transport_retries) == (2, 1)


def test_transport_retries_are_bounded() -> None:
    server = Server([httpx.Response(503)] * 5)
    client = _client(server)
    with pytest.raises(ModelUnavailableError):
        client.generate(_request())
    assert len(server.completion_requests) == 2


def test_connection_error_is_retried_once() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server = Server([refuse, refuse, _completion()])
    with pytest.raises(ModelUnavailableError):
        _client(server).generate(_request())
    assert len(server.completion_requests) == 2


def test_retries_follow_the_budget() -> None:
    server = Server([httpx.Response(503), _completion()])
    with pytest.raises(ModelUnavailableError):
        _client(server, budget=RunBudget(max_transport_retries=0)).generate(_request())
    assert len(server.completion_requests) == 1


def test_generation_timeout_is_not_retried() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    server = Server([slow, _completion()])
    with pytest.raises(ModelUnavailableError, match="timed out"):
        _client(server).generate(_request())
    assert len(server.completion_requests) == 1


def test_warm_request_timeout_is_180_seconds() -> None:
    seen: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return _completion()

    _client(Server([record])).generate(_request())
    assert seen == [180.0]


def test_network_attempt_budget_is_enforced() -> None:
    server = Server([_completion(), _completion(), _completion()])
    client = _client(server, budget=RunBudget(max_network_attempts=2))
    client.generate(_request())
    client.generate(_request())
    with pytest.raises(ModelBudgetError):
        client.generate(_request())
    assert len(server.completion_requests) == 2


def test_warm_inference_budget_is_enforced() -> None:
    clock = FakeClock()
    timeouts: list[float] = []

    def slow(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        clock.now += 100
        return _completion()

    server = Server([slow, slow, slow])
    client = _client(server, clock, RunBudget(warm_inference_seconds=150))
    client.generate(_request())
    client.generate(_request())
    # The second attempt only gets what is left of the warm budget.
    assert timeouts == [150.0, 50.0]
    with pytest.raises(ModelBudgetError):
        client.generate(_request())
    assert len(server.completion_requests) == 2


# --- readiness ------------------------------------------------------------------


def test_readiness_times_out_within_the_budget() -> None:
    clock = FakeClock()
    server = Server([], models=lambda _: httpx.Response(503))
    polls: list[float] = []
    client = _client(server, clock)
    with pytest.raises(ModelUnavailableError, match="not ready within 600 s"):
        client.wait_until_ready(on_poll=polls.append)
    assert clock.now <= 600
    assert clock.sleeps[:len(READINESS_BACKOFF_SECONDS)] == list(READINESS_BACKOFF_SECONDS)
    assert max(clock.sleeps) == READINESS_BACKOFF_SECONDS[-1]
    assert len(polls) == client.stats.readiness_polls
    assert not client.is_ready


def test_readiness_waits_through_a_cold_start() -> None:
    clock = FakeClock()

    def booting(request: httpx.Request) -> httpx.Response:
        if clock.now < 240:
            raise httpx.ConnectError("booting", request=request)
        return _models()

    server = Server([_completion()], models=booting)
    client = _client(server, clock)
    response = client.generate(_request())
    assert response.finish_reason == "stop"
    assert 240 <= client.stats.readiness_seconds < 600
    # Readiness polls do not use the generation attempt budget.
    assert client.stats.network_attempts == 1


def test_slow_readiness_polls_are_bounded() -> None:
    clock = FakeClock()

    def hang(request: httpx.Request) -> httpx.Response:
        clock.now += request.extensions["timeout"]["read"]
        raise httpx.ReadTimeout("hang", request=request)

    with pytest.raises(ModelUnavailableError):
        _client(Server([], models=hang), clock).wait_until_ready()
    assert clock.now <= 600


def test_readiness_404_fails_fast() -> None:
    server = Server([], models=lambda _: httpx.Response(404))
    with pytest.raises(ModelConfigError, match="404"):
        _client(server).wait_until_ready()
    assert len(server.requests) == 1


def test_readiness_requires_the_served_model_name() -> None:
    server = Server([], models=lambda _: httpx.Response(200, json={"data": [{"id": "other"}]}))
    with pytest.raises(ModelConfigError, match="adops-analysis"):
        _client(server).wait_until_ready()


# --- redirects ------------------------------------------------------------------


def test_same_origin_303_is_followed_as_get() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"location": "/v1/chat/completions?__call=fc-1"})

    def result(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return _completion()

    server = Server([redirect, result])
    client = _client(server)
    client.generate(_request())
    assert client.stats.network_attempts == 1


def test_redirect_to_another_host_is_refused() -> None:
    server = Server([httpx.Response(307, headers={"location": "https://api.openai.com/v1/x"}),
                     _completion()])
    with pytest.raises(ModelConfigError, match="another host"):
        _client(server).generate(_request())
    assert all(r.url.host != "api.openai.com" for r in server.requests)
