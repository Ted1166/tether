import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self

import httpx
from pydantic import BaseModel

from app.agent.jsonschema import inline_refs
from app.config import LLMSettings

logger = logging.getLogger("tether.llm")

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0
MAX_RETRY_AFTER_SECONDS = 30.0


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    pass


class LLMRequestError(LLMError):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMOutputError(LLMError):
    pass


class LLMRefusal(LLMError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": inline_refs(self.input_model.model_json_schema()),
        }


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    content: tuple[dict[str, Any], ...] = ()
    text: str = ""


class LanguageModel(Protocol):
    async def call_tool(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        force: str | None = None,
        max_tokens: int = 1024,
    ) -> ToolCall: ...


Sleeper = Callable[[float], Awaitable[None]]


class AnthropicModel:
    def __init__(
        self,
        settings: LLMSettings,
        http: httpx.AsyncClient | None = None,
        *,
        sleep: Sleeper = asyncio.sleep,
        max_retries: int = 3,
    ) -> None:
        self._settings = settings
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=_timeout())
        self._sleep = sleep
        self._max_retries = max_retries

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def call_tool(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        force: str | None = None,
        max_tokens: int = 1024,
    ) -> ToolCall:
        body: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": list(messages),
            "tools": [tool.definition() for tool in tools],
            "tool_choice": {"type": "tool", "name": force} if force else {"type": "auto"},
        }
        payload = await self._post(body)
        return self._read(payload)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._settings.base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": self._settings.api_key.get_secret_value(),
            "anthropic-version": self._settings.api_version,
        }
        attempt = 0
        while True:
            try:
                response = await self._http.post(url, json=body, headers=headers)
            except httpx.TransportError as error:
                if attempt >= self._max_retries:
                    raise LLMUnavailable(f"could not reach the model: {error}") from error
                await self._pause(attempt, None)
                attempt += 1
                continue

            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                logger.warning("model returned %s, retrying", response.status_code)
                await self._pause(attempt, response)
                attempt += 1
                continue

            return self._parse(response)

    async def _pause(self, attempt: int, response: httpx.Response | None) -> None:
        delay = min(BASE_BACKOFF_SECONDS * 2**attempt, MAX_BACKOFF_SECONDS)
        if response is not None:
            hinted = response.headers.get("retry-after", "")
            if hinted.isdigit():
                delay = min(float(hinted), MAX_RETRY_AFTER_SECONDS)
        await self._sleep(delay)

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        if 200 <= response.status_code < 300:
            try:
                payload = response.json()
            except ValueError as error:
                raise LLMOutputError("the model returned a response that is not JSON") from error
            if not isinstance(payload, dict):
                raise LLMOutputError("the model returned an unexpected response shape")
            return payload

        message = _error_message(response)
        status = response.status_code
        if status in (401, 403):
            raise LLMAuthError(f"the model rejected the API key ({status}): {message}")
        if status in RETRYABLE_STATUSES:
            raise LLMUnavailable(f"the model is unavailable ({status}): {message}")
        raise LLMRequestError(f"the model refused the request ({status}): {message}")

    def _read(self, payload: dict[str, Any]) -> ToolCall:
        usage = payload.get("usage") or {}
        logger.info(
            "model call: %s in, %s out tokens",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )

        stop_reason = payload.get("stop_reason")
        if stop_reason == "refusal":
            raise LLMRefusal("the model declined to answer")
        if stop_reason == "max_tokens":
            raise LLMOutputError("the model ran out of room before finishing its answer")

        blocks = [block for block in payload.get("content") or [] if isinstance(block, dict)]
        tool_block = next((block for block in blocks if block.get("type") == "tool_use"), None)
        if tool_block is None:
            raise LLMOutputError("the model did not call a tool")

        arguments = tool_block.get("input")
        if not isinstance(arguments, dict):
            raise LLMOutputError("the model returned tool input that is not an object")

        text = "".join(
            str(block.get("text", "")) for block in blocks if block.get("type") == "text"
        )
        kept = tuple(block for block in blocks if block.get("type") in ("text", "tool_use"))
        return ToolCall(
            id=str(tool_block.get("id", "")),
            name=str(tool_block.get("name", "")),
            input=arguments,
            content=kept,
            text=text,
        )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:200] or "no details"


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(60.0, connect=10.0)
