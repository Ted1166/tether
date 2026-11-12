import json
import logging
from typing import Any

import httpx
import pytest

from app.agent.llm import (
    AnthropicModel,
    LLMAuthError,
    LLMOutputError,
    LLMRefusal,
    LLMRequestError,
    LLMUnavailable,
)
from app.agent.tools import TOOLS
from app.config import LLMSettings

pytestmark = pytest.mark.anyio

SECRET = "sk-ant-very-secret"
URL = "https://api.anthropic.com/v1/messages"


class Transport:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def reply(content: list[dict[str, Any]], stop_reason: str = "tool_use") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 120, "output_tokens": 45},
        },
    )


TOOL_REPLY = reply(
    [
        {"type": "text", "text": "Preparing that invoice."},
        {
            "type": "tool_use",
            "id": "toolu_abc",
            "name": "create_invoice",
            "input": {"client_name": "Dan"},
        },
    ]
)


def make(transport: Transport, sleeper: Sleeper | None = None, retries: int = 3) -> AnthropicModel:
    settings = LLMSettings(_env_file=None, api_key=SECRET)  # type: ignore[arg-type, call-arg]
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return AnthropicModel(settings, http, sleep=sleeper or Sleeper(), max_retries=retries)


async def call(model: AnthropicModel, force: str | None = None) -> Any:
    return await model.call_tool(
        system="system text",
        messages=[{"role": "user", "content": "hello"}],
        tools=TOOLS,
        force=force,
    )


async def test_the_request_has_the_shape_the_messages_api_expects() -> None:
    transport = Transport(TOOL_REPLY)
    await call(make(transport))

    request = transport.requests[0]
    body = json.loads(request.content)
    assert str(request.url) == URL
    assert request.headers["x-api-key"] == SECRET
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "system text"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 1024
    assert body["tool_choice"] == {"type": "auto"}
    assert [tool["name"] for tool in body["tools"]] == [
        "create_invoice",
        "ask_clarification",
        "decline_request",
    ]
    assert "$ref" not in json.dumps(body["tools"])
    assert all(tool["input_schema"]["type"] == "object" for tool in body["tools"])


async def test_a_specific_tool_can_be_forced() -> None:
    transport = Transport(TOOL_REPLY)
    await call(make(transport), force="create_invoice")
    body = json.loads(transport.requests[0].content)
    assert body["tool_choice"] == {"type": "tool", "name": "create_invoice"}


async def test_the_tool_call_and_surrounding_text_are_read() -> None:
    result = await call(make(Transport(TOOL_REPLY)))
    assert result.id == "toolu_abc"
    assert result.name == "create_invoice"
    assert result.input == {"client_name": "Dan"}
    assert result.text == "Preparing that invoice."
    assert [block["type"] for block in result.content] == ["text", "tool_use"]


async def test_unknown_block_types_are_not_echoed_back() -> None:
    response = reply(
        [
            {"type": "thinking", "thinking": "hmm", "signature": "x"},
            {"type": "tool_use", "id": "t1", "name": "decline_request", "input": {"reason": "no"}},
        ]
    )
    result = await call(make(Transport(response)))
    assert [block["type"] for block in result.content] == ["tool_use"]


async def test_usage_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="tether.llm"):
        await call(make(Transport(TOOL_REPLY)))
    assert "120 in, 45 out tokens" in caplog.text


async def test_overloaded_responses_are_retried_with_backoff() -> None:
    sleeper = Sleeper()
    transport = Transport(
        httpx.Response(529, json={"error": {"message": "Overloaded"}}),
        httpx.Response(503, json={"error": {"message": "Busy"}}),
        TOOL_REPLY,
    )
    result = await call(make(transport, sleeper))
    assert result.name == "create_invoice"
    assert sleeper.delays == [1.0, 2.0]
    assert len(transport.requests) == 3


async def test_rate_limits_honor_retry_after() -> None:
    sleeper = Sleeper()
    transport = Transport(
        httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}}),
        TOOL_REPLY,
    )
    await call(make(transport, sleeper))
    assert sleeper.delays == [7.0]


async def test_persistent_overload_becomes_unavailable() -> None:
    transport = Transport(httpx.Response(529, json={"error": {"message": "Overloaded"}}))
    with pytest.raises(LLMUnavailable, match="Overloaded"):
        await call(make(transport, retries=2))
    assert len(transport.requests) == 3


async def test_transport_errors_are_retried_then_reported() -> None:
    transport = Transport(httpx.ConnectError("boom"), TOOL_REPLY)
    assert (await call(make(transport))).name == "create_invoice"

    failing = Transport(httpx.ConnectError("boom"))
    with pytest.raises(LLMUnavailable):
        await call(make(failing, retries=1))
    assert len(failing.requests) == 2


async def test_a_rejected_key_is_an_auth_error_that_does_not_leak_the_key() -> None:
    transport = Transport(
        httpx.Response(
            401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        )
    )
    with pytest.raises(LLMAuthError) as caught:
        await call(make(transport))
    assert "invalid x-api-key" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert len(transport.requests) == 1


async def test_bad_requests_are_not_retried_and_carry_the_reason() -> None:
    transport = Transport(
        httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "message": "bad schema"}}
        )
    )
    with pytest.raises(LLMRequestError, match="bad schema"):
        await call(make(transport))
    assert len(transport.requests) == 1


async def test_an_unknown_model_is_a_request_error() -> None:
    transport = Transport(
        httpx.Response(404, json={"error": {"type": "not_found_error", "message": "model: nope"}})
    )
    with pytest.raises(LLMRequestError, match="model: nope"):
        await call(make(transport))


async def test_a_response_without_a_tool_call_is_an_output_error() -> None:
    response = reply([{"type": "text", "text": "I would love to help."}], stop_reason="end_turn")
    with pytest.raises(LLMOutputError, match="did not call a tool"):
        await call(make(Transport(response)))


async def test_a_truncated_answer_is_an_output_error() -> None:
    response = reply(
        [{"type": "tool_use", "id": "t", "name": "create_invoice", "input": {}}],
        stop_reason="max_tokens",
    )
    with pytest.raises(LLMOutputError, match="ran out of room"):
        await call(make(Transport(response)))


async def test_a_refusal_is_reported_as_such() -> None:
    with pytest.raises(LLMRefusal):
        await call(make(Transport(reply([], stop_reason="refusal"))))


async def test_tool_input_that_is_not_an_object_is_rejected() -> None:
    response = reply([{"type": "tool_use", "id": "t", "name": "create_invoice", "input": "text"}])
    with pytest.raises(LLMOutputError, match="not an object"):
        await call(make(Transport(response)))


async def test_a_non_json_success_body_is_an_output_error() -> None:
    with pytest.raises(LLMOutputError):
        await call(make(Transport(httpx.Response(200, text="<html></html>"))))


async def test_a_non_object_success_body_is_an_output_error() -> None:
    with pytest.raises(LLMOutputError):
        await call(make(Transport(httpx.Response(200, json=[1, 2]))))
