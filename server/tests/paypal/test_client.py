import httpx
import pytest

from app.paypal.errors import (
    PayPalAuthError,
    PayPalNotFound,
    PayPalRateLimited,
    PayPalRequestError,
    PayPalResponseError,
    PayPalUnavailable,
)
from tests.paypal.helpers import Harness

pytestmark = pytest.mark.anyio

PATH = "/v2/invoicing/invoices"


async def test_requests_carry_the_bearer_token(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(200, json={"items": []}))
    async with harness.client() as client:
        await client.request("GET", PATH)
    assert harness.fake.calls("GET", PATH)[0].headers["Authorization"] == "Bearer token-1"


async def test_posts_get_an_idempotency_key_and_gets_do_not(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(200, json={}))
    harness.fake.queue("POST", PATH, httpx.Response(201, json={}))
    async with harness.client() as client:
        await client.request("GET", PATH)
        await client.request("POST", PATH, json={"a": 1})
    assert "PayPal-Request-Id" not in harness.fake.calls("GET", PATH)[0].headers
    assert harness.fake.calls("POST", PATH)[0].headers["PayPal-Request-Id"]


async def test_posts_without_a_body_still_declare_json(harness: Harness) -> None:
    harness.fake.queue("POST", PATH, httpx.Response(200, json={}))
    async with harness.client() as client:
        await client.request("POST", PATH)
    request = harness.fake.calls("POST", PATH)[0]
    assert request.headers["Content-Type"] == "application/json"
    assert request.content == b"{}"


async def test_gets_do_not_send_a_body_or_content_type(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(200, json={}))
    async with harness.client() as client:
        await client.request("GET", PATH)
    request = harness.fake.calls("GET", PATH)[0]
    assert "Content-Type" not in request.headers
    assert request.content == b""


async def test_a_caller_supplied_request_id_is_used(harness: Harness) -> None:
    harness.fake.queue("POST", PATH, httpx.Response(201, json={}))
    async with harness.client() as client:
        await client.request("POST", PATH, json={}, request_id="action-17")
    assert harness.fake.calls("POST", PATH)[0].headers["PayPal-Request-Id"] == "action-17"


async def test_retries_reuse_the_same_idempotency_key(harness: Harness) -> None:
    harness.fake.queue(
        "POST",
        PATH,
        httpx.Response(503, json={"name": "BUSY"}),
        httpx.Response(201, json={"ok": 1}),
    )
    async with harness.client() as client:
        body = await client.request("POST", PATH, json={})
    calls = harness.fake.calls("POST", PATH)
    assert body == {"ok": 1}
    assert len(calls) == 2
    assert calls[0].headers["PayPal-Request-Id"] == calls[1].headers["PayPal-Request-Id"]


async def test_server_errors_back_off_exponentially_then_succeed(harness: Harness) -> None:
    harness.fake.queue(
        "GET",
        PATH,
        httpx.Response(503, json={}),
        httpx.Response(502, json={}),
        httpx.Response(200, json={"ok": True}),
    )
    async with harness.client() as client:
        body = await client.request("GET", PATH)
    assert body == {"ok": True}
    assert harness.sleeper.delays == [0.5, 1.0]


async def test_rate_limits_honor_retry_after(harness: Harness) -> None:
    harness.fake.queue(
        "GET",
        PATH,
        httpx.Response(429, headers={"Retry-After": "2"}, json={}),
        httpx.Response(200, json={}),
    )
    async with harness.client() as client:
        await client.request("GET", PATH)
    assert harness.sleeper.delays == [2.0]


async def test_gives_up_after_the_retry_budget(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(503, json={"name": "DOWN", "message": "down"}))
    async with harness.client(max_retries=2) as client:
        with pytest.raises(PayPalUnavailable):
            await client.request("GET", PATH)
    assert len(harness.fake.calls("GET", PATH)) == 3
    assert len(harness.sleeper.delays) == 2


async def test_persistent_rate_limiting_surfaces_as_rate_limited(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(429, json={"name": "RATE_LIMIT_REACHED"}))
    async with harness.client(max_retries=1) as client:
        with pytest.raises(PayPalRateLimited):
            await client.request("GET", PATH)


async def test_transport_errors_are_retried(harness: Harness) -> None:
    harness.fake.queue(
        "GET", PATH, httpx.ConnectError("boom"), httpx.Response(200, json={"ok": True})
    )
    async with harness.client() as client:
        assert await client.request("GET", PATH) == {"ok": True}


async def test_persistent_transport_errors_become_unavailable(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.ConnectError("boom"))
    async with harness.client(max_retries=1) as client:
        with pytest.raises(PayPalUnavailable):
            await client.request("GET", PATH)
    assert len(harness.fake.calls("GET", PATH)) == 2


async def test_an_expired_token_is_refreshed_once(harness: Harness) -> None:
    harness.fake.queue(
        "GET",
        PATH,
        httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}),
        httpx.Response(200, json={"ok": True}),
    )
    async with harness.client() as client:
        body = await client.request("GET", PATH)
    calls = harness.fake.calls("GET", PATH)
    assert body == {"ok": True}
    assert harness.fake.token_requests == 2
    assert calls[1].headers["Authorization"] == "Bearer token-2"


async def test_repeated_401s_do_not_loop(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}))
    async with harness.client() as client:
        with pytest.raises(PayPalAuthError):
            await client.request("GET", PATH)
    assert harness.fake.token_requests == 2
    assert len(harness.fake.calls("GET", PATH)) == 2


async def test_missing_resources_raise_not_found_without_retrying(harness: Harness) -> None:
    harness.fake.queue(
        "GET",
        f"{PATH}/INV2-NOPE",
        httpx.Response(404, json={"name": "INVALID_RESOURCE_ID", "message": "Not found"}),
    )
    async with harness.client() as client:
        with pytest.raises(PayPalNotFound):
            await client.request("GET", f"{PATH}/INV2-NOPE")
    assert len(harness.fake.calls("GET", f"{PATH}/INV2-NOPE")) == 1
    assert harness.sleeper.delays == []


async def test_validation_errors_carry_paypal_details(harness: Harness) -> None:
    harness.fake.queue(
        "POST",
        PATH,
        httpx.Response(
            422,
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "message": "The requested action could not be performed.",
                "debug_id": "abc123",
                "details": [
                    {"field": "/items/0/name", "issue": "MISSING_REQUIRED_PARAMETER"},
                    {"issue": "INVALID_EMAIL"},
                ],
            },
        ),
    )
    async with harness.client() as client:
        with pytest.raises(PayPalRequestError) as caught:
            await client.request("POST", PATH, json={})
    error = caught.value
    assert error.status == 422
    assert error.debug_id == "abc123"
    assert error.issues == ("/items/0/name: MISSING_REQUIRED_PARAMETER", "INVALID_EMAIL")
    assert "abc123" in str(error)
    assert len(harness.fake.calls("POST", PATH)) == 1


async def test_empty_success_bodies_return_an_empty_dict(harness: Harness) -> None:
    harness.fake.queue("POST", PATH, httpx.Response(202))
    async with harness.client() as client:
        assert await client.request("POST", PATH, json={}) == {}


async def test_non_json_success_bodies_are_rejected(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(200, text="<html>oops</html>"))
    async with harness.client() as client:
        with pytest.raises(PayPalResponseError):
            await client.request("GET", PATH)


async def test_non_object_success_bodies_are_rejected(harness: Harness) -> None:
    harness.fake.queue("GET", PATH, httpx.Response(200, json=[1, 2, 3]))
    async with harness.client() as client:
        with pytest.raises(PayPalResponseError):
            await client.request("GET", PATH)
