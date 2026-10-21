"""Fail-closed transport to the approved self-hosted, OpenAI-compatible endpoint.

The client talks plain HTTP through ``httpx`` so there is no SDK default
endpoint to fall back to, no environment proxy or base-URL override and no
hidden retry. It enforces the transport budgets; repairing a schema-invalid
answer belongs to the caller, which gets the machine-readable errors.
"""

import ipaddress
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
import jsonschema

from contracts.config import RunBudget
from contracts.runtime import ModelClient, ModelRequest, ModelResponse
from llm.profiles import ModelProfile, load_profile

ENV_BASE_URL = "LLM_BASE_URL"
ENV_API_KEY = "LLM_API_KEY"
# Comma-separated exact base URLs this deployment may talk to.
ENV_ALLOWED_BASE_URLS = "LLM_ALLOWED_BASE_URLS"

# Hosted model APIs are never a valid endpoint, even if allowlisted by mistake.
BLOCKED_HOST_SUFFIXES = (
    "openai.com",
    "openai.azure.com",
    "anthropic.com",
    "claude.ai",
    "googleapis.com",
    "mistral.ai",
    "cohere.ai",
    "cohere.com",
    "together.xyz",
    "together.ai",
    "groq.com",
    "openrouter.ai",
    "fireworks.ai",
    "deepseek.com",
    "x.ai",
    "perplexity.ai",
    "deepinfra.com",
    "huggingface.co",
    "hf.space",
    "amazonaws.com",
    "azure.com",
    "cerebras.ai",
    "replicate.com",
    "ai21.com",
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
# Long requests on the serving platform answer 303 to a same-origin result URL
# every 150 s; the per-attempt deadline still bounds the total wait.
MAX_REDIRECTS = 8
READINESS_POLL_TIMEOUT_SECONDS = 60.0
READINESS_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 15.0, 30.0)


class ModelClientError(Exception):
    """Base for every failure the client reports; none fall back to another model."""


class ModelConfigError(ModelClientError):
    """Missing or refused configuration: endpoint, key, allowlist or request bounds."""


class ModelAuthError(ModelClientError):
    """The endpoint rejected the key. Never retried."""


class ModelUnavailableError(ModelClientError):
    """Not ready within the readiness budget, or transport failed after its retry."""


class ModelBudgetError(ModelClientError):
    """The run's network-attempt or warm-inference budget is spent."""


class ModelOutputError(ModelClientError):
    """The server answered but the answer is unusable (empty, truncated, invalid).

    ``errors`` are machine-readable strings for one repair attempt by the caller.
    """

    def __init__(self, message: str, *, errors: list[str], content: str | None,
                 finish_reason: str | None, prompt_tokens: int, output_tokens: int) -> None:
        super().__init__(message)
        self.errors = errors
        self.content = content
        self.finish_reason = finish_reason
        self.prompt_tokens = prompt_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class Endpoint:
    """A validated base URL; ``url`` has no trailing slash and ends in ``/v1``."""

    scheme: str
    host: str
    port: int
    path: str

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}"

    def same_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return (parts.scheme, (parts.hostname or "").lower(), port) == (
            self.scheme, self.host, self.port)


@dataclass
class ClientStats:
    """Counters for the run manifest."""

    network_attempts: int = 0
    transport_retries: int = 0
    readiness_polls: int = 0
    readiness_seconds: float = 0.0
    requests_succeeded: int = 0
    requests_failed: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0


def parse_endpoint(raw: str, *, allow_insecure_localhost: bool = False) -> Endpoint:
    """Validate one base URL: HTTPS, no credentials, query or fragment, path ``/v1``."""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError as exc:
        raise ModelConfigError(f"malformed endpoint URL: {exc}") from exc
    host = (parts.hostname or "").lower()
    if not host:
        raise ModelConfigError("endpoint URL has no host")
    if parts.username is not None or parts.password is not None:
        raise ModelConfigError("endpoint URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ModelConfigError("endpoint URL must not carry a query or fragment")
    if parts.scheme == "http":
        if not (allow_insecure_localhost and host in _LOCAL_HOSTS):
            raise ModelConfigError("endpoint must use HTTPS")
    elif parts.scheme != "https":
        raise ModelConfigError("endpoint must use HTTPS")
    if any(host == s or host.endswith("." + s) for s in BLOCKED_HOST_SUFFIXES):
        raise ModelConfigError(f"{host} is a hosted model API, not the self-hosted endpoint")
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        raise ModelConfigError("endpoint path must end in /v1")
    if parts.scheme == "https" and host not in _LOCAL_HOSTS:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ModelConfigError("endpoint must be a host name, not an IP address")
    return Endpoint(parts.scheme, host, port or (443 if parts.scheme == "https" else 80), path)


def resolve_endpoint(base_url: str, allowlist: Iterable[str], *,
                     allow_insecure_localhost: bool = False) -> Endpoint:
    """``base_url`` if it equals an allowlisted entry exactly (after normalization)."""
    endpoint = parse_endpoint(base_url, allow_insecure_localhost=allow_insecure_localhost)
    allowed = {parse_endpoint(e, allow_insecure_localhost=allow_insecure_localhost)
               for e in allowlist if e.strip()}
    if not allowed:
        raise ModelConfigError(f"{ENV_ALLOWED_BASE_URLS} is empty; no endpoint is approved")
    if endpoint not in allowed:
        raise ModelConfigError(f"{endpoint.url} is not in {ENV_ALLOWED_BASE_URLS}")
    return endpoint


def create_client(
    env: Mapping[str, str],
    budget: RunBudget,
    *,
    profile: ModelProfile | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    allow_insecure_localhost: bool = False,
) -> ModelClient:
    """Build a client from explicit ``LLM_BASE_URL`` and ``LLM_API_KEY``.

    Refuses missing configuration, non-allowlisted endpoints and hosted model
    APIs; never falls back to an SDK default endpoint. ``profile`` defaults to
    the active entry in ``config/model_profiles.yaml``. ``transport``, ``clock``
    and ``sleep`` exist for tests; ``allow_insecure_localhost`` is test-only and
    deliberately not reachable from the environment.
    """
    base_url = env.get(ENV_BASE_URL, "").strip()
    api_key = env.get(ENV_API_KEY, "").strip()
    if not base_url:
        raise ModelConfigError(f"{ENV_BASE_URL} is not set")
    if not api_key:
        raise ModelConfigError(f"{ENV_API_KEY} is not set")
    if any(c.isspace() or not c.isprintable() for c in api_key):
        raise ModelConfigError(f"{ENV_API_KEY} contains whitespace or control characters")
    allowlist = env.get(ENV_ALLOWED_BASE_URLS, "").split(",")
    endpoint = resolve_endpoint(base_url, allowlist,
                                allow_insecure_localhost=allow_insecure_localhost)
    return SelfHostedModelClient(
        endpoint=endpoint,
        api_key=api_key,
        profile=profile or load_profile(),
        budget=budget,
        transport=transport,
        clock=clock,
        sleep=sleep,
    )


class _Transient(Exception):
    """A failure that one transport retry may cure."""


class _Timeout(Exception):
    """The deadline passed. Readiness polls again; a generation is not retried."""


class SelfHostedModelClient:
    """``ModelClient`` for one vLLM deployment. Not thread-safe; one run at a time."""

    def __init__(self, *, endpoint: Endpoint, api_key: str, profile: ModelProfile,
                 budget: RunBudget, transport: httpx.BaseTransport | None,
                 clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.endpoint = endpoint
        self.profile = profile
        self.budget = budget
        self.stats = ClientStats()
        self._clock = clock
        self._sleep = sleep
        self._ready_at: float | None = None
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    @property
    def is_ready(self) -> bool:
        return self._ready_at is not None

    # --- readiness ---------------------------------------------------------

    def wait_until_ready(self, on_poll: Callable[[float], None] | None = None) -> float:
        """Poll ``/models`` with backoff until the served model is listed.

        Returns the seconds waited. A cold start takes minutes, so call this
        from a background job; ``on_poll`` receives the elapsed seconds after
        every unsuccessful poll. Auth failures and a missing model fail at once.
        """
        if self._ready_at is not None:
            return 0.0
        start = self._clock()
        deadline = start + self.budget.readiness_seconds
        polls = 0
        while True:
            polls += 1
            self.stats.readiness_polls += 1
            poll_deadline = min(deadline, self._clock() + READINESS_POLL_TIMEOUT_SECONDS)
            try:
                response = self._send("GET", "/models", None, poll_deadline)
            except (_Transient, _Timeout):
                pass
            else:
                self._check_auth(response)
                if response.status_code == 200:
                    self._check_served_model(response)
                    self._ready_at = self._clock()
                    self.stats.readiness_seconds = self._ready_at - start
                    return self.stats.readiness_seconds
                if response.status_code == 404:
                    raise ModelConfigError(
                        "endpoint answered 404; is the server deployed and the URL correct?")
                if response.status_code not in _TRANSIENT_STATUS:
                    raise ModelUnavailableError(
                        f"readiness check failed with HTTP {response.status_code}")
            now = self._clock()
            if on_poll is not None:
                on_poll(now - start)
            pause = READINESS_BACKOFF_SECONDS[min(polls, len(READINESS_BACKOFF_SECONDS)) - 1]
            if now + pause >= deadline:
                raise ModelUnavailableError(
                    f"model not ready within {self.budget.readiness_seconds} s "
                    f"({polls} polls)")
            self._sleep(pause)

    def _check_served_model(self, response: httpx.Response) -> None:
        try:
            ids = {m["id"] for m in response.json()["data"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelUnavailableError("readiness response is not a model list") from exc
        if self.profile.served_model_name not in ids:
            raise ModelConfigError(
                f"endpoint does not serve {self.profile.served_model_name!r}")

    # --- generation --------------------------------------------------------

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.max_output_tokens > self.profile.max_output_tokens:
            raise ModelConfigError(
                f"max_output_tokens {request.max_output_tokens} exceeds the profile's "
                f"{self.profile.max_output_tokens}")
        if request.enable_thinking is not self.profile.enable_thinking:
            raise ModelConfigError("request and profile disagree on thinking")
        try:
            jsonschema.Draft202012Validator.check_schema(request.response_schema)
        except jsonschema.SchemaError as exc:
            raise ModelConfigError(f"response schema is invalid: {exc.message}") from exc
        self.wait_until_ready()
        body = self._request_body(request)

        retries_left = self.budget.max_transport_retries
        while True:
            deadline = self._attempt_deadline()
            self.stats.network_attempts += 1
            try:
                response = self._send("POST", "/chat/completions", body, deadline)
                self._check_auth(response)
                if response.status_code in _TRANSIENT_STATUS:
                    raise _Transient(f"HTTP {response.status_code}")
            except _Transient as exc:
                if retries_left <= 0:
                    self.stats.requests_failed += 1
                    raise ModelUnavailableError(f"transport failed: {exc}") from exc
                retries_left -= 1
                self.stats.transport_retries += 1
                continue
            except _Timeout as exc:
                self.stats.requests_failed += 1
                raise ModelUnavailableError(f"generation timed out: {exc}") from exc
            except ModelClientError:
                self.stats.requests_failed += 1
                raise
            try:
                result = self._parse(response, request)
            except ModelClientError:
                self.stats.requests_failed += 1
                raise
            self.stats.requests_succeeded += 1
            return result

    def _request_body(self, request: ModelRequest) -> dict[str, object]:
        return {
            "model": self.profile.served_model_name,
            "messages": [m.model_dump() for m in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": self.profile.temperature,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_schema_name,
                    "strict": True,
                    "schema": request.response_schema,
                },
            },
            # Explicit on every call: Qwen thinks by default and can spend the
            # whole output budget before any answer.
            "chat_template_kwargs": {"enable_thinking": self.profile.enable_thinking},
        }

    def _attempt_deadline(self) -> float:
        if self.stats.network_attempts >= self.budget.max_network_attempts:
            raise ModelBudgetError(
                f"network attempt budget of {self.budget.max_network_attempts} is spent")
        assert self._ready_at is not None
        now = self._clock()
        warm_left = self._ready_at + self.budget.warm_inference_seconds - now
        if warm_left <= 0:
            raise ModelBudgetError(
                f"warm inference budget of {self.budget.warm_inference_seconds} s is spent")
        return now + min(self.budget.request_timeout_seconds, warm_left)

    def _parse(self, response: httpx.Response, request: ModelRequest) -> ModelResponse:
        if response.status_code != 200:
            raise ModelUnavailableError(
                f"generation failed with HTTP {response.status_code}: {_error_text(response)}")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason")
            usage = payload.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailableError("response is not a chat completion") from exc
        # Tokens were spent even if the answer is unusable.
        self.stats.prompt_tokens += prompt_tokens
        self.stats.output_tokens += output_tokens
        if payload.get("model") != self.profile.served_model_name:
            raise ModelConfigError(f"response came from model {payload.get('model')!r}")

        content = message.get("content")

        def fail(message_: str, errors: list[str]) -> ModelOutputError:
            return ModelOutputError(message_, errors=errors, content=content,
                                    finish_reason=finish_reason,
                                    prompt_tokens=prompt_tokens, output_tokens=output_tokens)

        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            raise fail("model returned a thinking trace with thinking disabled",
                       ["thinking_trace_present"])
        if content is None or not content.strip():
            raise fail(f"model returned no content (finish_reason={finish_reason!r})",
                       ["empty_content"])
        if finish_reason != "stop":
            raise fail(f"generation did not finish (finish_reason={finish_reason!r})",
                       [f"finish_reason:{finish_reason}"])
        try:
            document = json.loads(content)
        except ValueError as exc:
            raise fail("content is not JSON", [f"invalid_json: {exc}"]) from exc
        validator = jsonschema.Draft202012Validator(request.response_schema)
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        if errors:
            raise fail("content does not match the response schema", [
                f"{'/' + '/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors])
        return ModelResponse(content=content, prompt_tokens=prompt_tokens,
                             output_tokens=output_tokens, finish_reason=finish_reason)

    # --- HTTP ----------------------------------------------------------------

    def _send(self, method: str, path: str, body: dict[str, object] | None,
              deadline: float) -> httpx.Response:
        """One logical request, following same-origin redirects until ``deadline``."""
        url = self.endpoint.url + path
        for _ in range(MAX_REDIRECTS + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _Timeout(f"{method} {path}")
            try:
                response = self._http.request(
                    method, url, json=body, timeout=httpx.Timeout(remaining))
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                    httpx.ReadError, httpx.WriteError, httpx.PoolTimeout) as exc:
                raise _Transient(type(exc).__name__) from exc
            except httpx.TimeoutException as exc:
                raise _Timeout(f"{method} {path}") from exc
            if response.status_code not in _REDIRECT_STATUS:
                return response
            location = response.headers.get("location")
            if not location:
                raise ModelUnavailableError("redirect without a location")
            target = urljoin(url, location)
            if not self.endpoint.same_origin(target):
                raise ModelConfigError("endpoint redirected to another host; refused")
            url = target
            if response.status_code == 303:
                method, body = "GET", None
        raise ModelUnavailableError(f"more than {MAX_REDIRECTS} redirects")

    @staticmethod
    def _check_auth(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise ModelAuthError(f"endpoint rejected the API key (HTTP {response.status_code})")


def _error_text(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:300]
    except (ValueError, AttributeError):
        return response.text[:300]
