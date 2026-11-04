from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import PayPalSettings
from app.paypal.client import PayPalClient

TOKEN_PATH = "/v1/oauth2/token"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FakePayPal:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_requests = 0
        self.token_lifetime = 3600
        self.token_response: httpx.Response | None = None
        self._queues: dict[tuple[str, str], list[httpx.Response | Exception]] = {}

    def queue(self, method: str, path: str, *responses: httpx.Response | Exception) -> None:
        self._queues[(method, path)] = list(responses)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            self.token_requests += 1
            if self.token_response is not None:
                return self.token_response
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.token_requests}",
                    "token_type": "Bearer",
                    "expires_in": self.token_lifetime,
                },
            )

        queue = self._queues.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(500, json={"name": "UNEXPECTED", "message": "no route queued"})
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Harness:
    def __init__(self, settings: PayPalSettings) -> None:
        self.settings = settings
        self.fake = FakePayPal()
        self.clock = FakeClock()
        self.sleeper = RecordingSleeper()

    @asynccontextmanager
    async def http(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.fake.handler)) as http:
            yield http

    @asynccontextmanager
    async def client(self, max_retries: int = 3) -> AsyncIterator[PayPalClient]:
        async with self.http() as http:
            yield PayPalClient(
                self.settings,
                http,
                sleep=self.sleeper,
                clock=self.clock,
                max_retries=max_retries,
            )


def sample_invoice(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "INV2-ABCD-1234-WXYZ-5678",
        "status": "SENT",
        "detail": {
            "invoice_number": "0042",
            "invoice_date": "2026-11-04",
            "currency_code": "USD",
            "reference": "tether-demo-1",
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": "2026-11-18"},
            "metadata": {
                "recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#ABCD",
                "invoicer_view_url": "https://www.sandbox.paypal.com/invoice/details/INV2-ABCD",
            },
        },
        "primary_recipients": [
            {
                "billing_info": {
                    "name": {"given_name": "Dan", "surname": "Okoro"},
                    "email_address": "Dan@Example.com",
                }
            }
        ],
        "amount": {"currency_code": "USD", "value": "400.00"},
        "due_amount": {"currency_code": "USD", "value": "400.00"},
    }
    payload.update(overrides)
    return payload
