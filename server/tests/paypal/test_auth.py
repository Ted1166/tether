import asyncio
import base64
from urllib.parse import parse_qs

import httpx
import pytest

from app.paypal.auth import TokenProvider
from app.paypal.errors import PayPalAuthError, PayPalUnavailable
from tests.paypal.helpers import Harness

pytestmark = pytest.mark.anyio


async def test_token_request_uses_basic_auth_and_client_credentials(harness: Harness) -> None:
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        token = await provider.get()

    request = harness.fake.requests[0]
    expected = base64.b64encode(b"client-id:client-secret").decode()
    assert token == "token-1"
    assert request.headers["Authorization"] == f"Basic {expected}"
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert request.url.host == "api-m.sandbox.paypal.com"


async def test_token_request_asks_for_the_invoicing_and_webhook_permissions(
    harness: Harness,
) -> None:
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        await provider.get()

    form = parse_qs(harness.fake.requests[0].content.decode())
    requested = set(form["scope"][0].split())
    assert "https://uri.paypal.com/services/invoicing" in requested
    assert "https://uri.paypal.com/services/applications/webhooks" in requested


async def test_token_is_cached_between_calls(harness: Harness) -> None:
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        first = await provider.get()
        second = await provider.get()
    assert first == second
    assert harness.fake.token_requests == 1


async def test_token_is_refreshed_shortly_before_it_expires(harness: Harness) -> None:
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        await provider.get()
        harness.clock.advance(3539)
        await provider.get()
        assert harness.fake.token_requests == 1
        harness.clock.advance(2)
        refreshed = await provider.get()
    assert refreshed == "token-2"


async def test_invalidate_forces_a_new_token(harness: Harness) -> None:
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        await provider.get()
        provider.invalidate()
        assert await provider.get() == "token-2"


async def test_concurrent_callers_share_one_token_request(harness: Harness) -> None:
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        tokens = await asyncio.gather(*(provider.get() for _ in range(8)))
    assert set(tokens) == {"token-1"}
    assert harness.fake.token_requests == 1


async def test_bad_credentials_raise_an_auth_error(harness: Harness) -> None:
    harness.fake.token_response = httpx.Response(
        401, json={"error": "invalid_client", "error_description": "Client Authentication failed"}
    )
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        with pytest.raises(PayPalAuthError) as caught:
            await provider.get()
    assert "Client Authentication failed" in str(caught.value)
    assert caught.value.status == 401


async def test_a_paypal_outage_during_auth_is_reported_as_unavailable(harness: Harness) -> None:
    harness.fake.token_response = httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"})
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        with pytest.raises(PayPalUnavailable):
            await provider.get()


async def test_an_unreadable_token_response_is_an_auth_error(harness: Harness) -> None:
    harness.fake.token_response = httpx.Response(200, json={"unexpected": True})
    async with harness.http() as http:
        provider = TokenProvider(harness.settings, http, harness.clock)
        with pytest.raises(PayPalAuthError):
            await provider.get()
