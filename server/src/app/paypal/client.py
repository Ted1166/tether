import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, Self

import httpx

from app.config import PayPalSettings
from app.paypal.auth import TokenProvider
from app.paypal.errors import PayPalResponseError, PayPalUnavailable, from_response

logger = logging.getLogger("tether.paypal")

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0

Sleeper = Callable[[float], Awaitable[None]]


class PayPalClient:
    def __init__(
        self,
        settings: PayPalSettings,
        http: httpx.AsyncClient | None = None,
        *,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 3,
    ) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=_default_timeout())
        self._tokens = TokenProvider(settings, self._http, clock)
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

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if json is not None and content is not None:
            raise ValueError("pass either json or content, not both")

        headers = {"Accept": "application/json"}
        body = json
        if method.upper() == "POST":
            headers["PayPal-Request-Id"] = request_id or uuid.uuid4().hex
            if content is None:
                body = json if json is not None else {}
        if content is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self._base_url}{path}"
        attempt = 0
        refreshed = False

        while True:
            token = await self._tokens.get()
            try:
                response = await self._http.request(
                    method,
                    url,
                    json=body,
                    content=content,
                    params=params,
                    headers={**headers, "Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError as error:
                if attempt >= self._max_retries:
                    raise PayPalUnavailable(f"could not reach PayPal: {error}") from error
                logger.warning("paypal transport error on %s %s, retrying", method, path)
                await self._pause(attempt, None)
                attempt += 1
                continue

            if response.status_code == 401 and not refreshed:
                self._tokens.invalidate()
                refreshed = True
                continue

            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                logger.warning(
                    "paypal returned %s on %s %s, retrying", response.status_code, method, path
                )
                await self._pause(attempt, response)
                attempt += 1
                continue

            return self._parse(response)

    async def _pause(self, attempt: int, response: httpx.Response | None) -> None:
        delay = min(BASE_BACKOFF_SECONDS * 2**attempt, MAX_BACKOFF_SECONDS)
        if response is not None:
            hinted = response.headers.get("Retry-After", "")
            if hinted.isdigit():
                delay = min(float(hinted), MAX_RETRY_AFTER_SECONDS)
        await self._sleep(delay)

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        if 200 <= response.status_code < 300:
            if not response.content:
                return {}
            try:
                body = response.json()
            except ValueError as error:
                raise PayPalResponseError(
                    "PayPal returned a response that is not valid JSON",
                    status=response.status_code,
                ) from error
            if not isinstance(body, dict):
                raise PayPalResponseError(
                    "PayPal returned an unexpected response shape", status=response.status_code
                )
            return body

        try:
            payload = response.json()
        except ValueError:
            payload = None
        raise from_response(response.status_code, payload)


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(20.0, connect=10.0)
