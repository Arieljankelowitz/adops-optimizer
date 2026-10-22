"""Native mode uses the same fail-closed transport, schema and thinking checks."""

import json

import httpx
import pytest

from contracts.enums import ToolName
from contracts.models import NextStep
from contracts.runtime import ChatMessage, ModelRequest
from llm.analysis import native_tool_schemas
from llm.client import ModelOutputError, ModelToolProtocolError
from tests.unit.test_llm_client import Server, _client, _completion


def request():
    return ModelRequest(
        messages=[ChatMessage(role="user", content="Synthetic evidence only")],
        response_schema_name="next_step",
        response_schema=NextStep.model_json_schema(),
        max_output_tokens=256,
        tools=native_tool_schemas({ToolName.DAILY_TREND}),
    )


def native_call(name="daily_trend", extra=None):
    return {
        "id": "tool_1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                {
                    "segment_id": "G_" + "1" * 16,
                    "period_id": "P_" + "2" * 16,
                    **(extra or {}),
                }
            ),
        },
    }


def test_native_round_trip_uses_same_json_and_non_thinking_transport():
    server = Server([_completion(None, "tool_calls", tool_calls=[native_call()])])
    client = _client(server)
    response = client.generate(request())
    assert response.tool_calls[0]["function"]["name"] == "daily_trend"
    body = json.loads(server.completion_requests[0].content)
    assert body["parallel_tool_calls"] is False and body["tool_choice"] == "auto"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert client.stats.network_attempts == 1
    client.close()


@pytest.mark.parametrize(
    "calls",
    [
        [native_call("shell")],
        [native_call(extra={"sql": "DROP TABLE"})],
        [native_call(), native_call()],
    ],
)
def test_native_unknown_extra_and_parallel_calls_rejected(calls):
    server = Server([_completion(None, "tool_calls", tool_calls=calls)])
    with pytest.raises(ModelOutputError):
        _client(server).generate(request())


def test_native_never_accepts_thinking_trace():
    server = Server(
        [
            _completion(
                None,
                "tool_calls",
                tool_calls=[native_call()],
                reasoning_content="forbidden",
            )
        ]
    )
    with pytest.raises(ModelOutputError, match="thinking"):
        _client(server).generate(request())


def test_native_parser_refusal_is_explicit_without_transport_retry():
    server = Server(
        [httpx.Response(400, json={"error": {"message": "parser not configured"}})]
    )
    client = _client(server)
    with pytest.raises(ModelToolProtocolError):
        client.generate(request())
    assert client.stats.network_attempts == 1 and client.stats.transport_retries == 0
