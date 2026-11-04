import json

import httpx
import pytest

from app.paypal.errors import PayPalAuthError, PayPalResponseError, PayPalUnavailable
from app.paypal.webhooks import (
    INVOICE_EVENT_TYPES,
    WebhookRegistry,
    WebhookVerifier,
    invoice_id_from_event,
)
from app.schemas.webhooks import MissingWebhookHeader, WebhookEvent, WebhookHeaders
from tests.paypal.helpers import Harness
from tests.webhooks.fakes import INVOICE_ID, event_body, signed_headers

pytestmark = pytest.mark.anyio

VERIFY = "/v1/notifications/verify-webhook-signature"
HOOKS = "/v1/notifications/webhooks"


def headers() -> WebhookHeaders:
    return WebhookHeaders.from_headers(signed_headers())


async def test_verification_forwards_the_untouched_event_bytes(harness: Harness) -> None:
    raw = b'{"id":"WH-1",  "event_type":"INVOICING.INVOICE.PAID","resource":{"id":"INV2-A"}}\n'
    harness.fake.queue("POST", VERIFY, httpx.Response(200, json={"verification_status": "SUCCESS"}))
    async with harness.client() as client:
        verified = await WebhookVerifier(client, "WEBHOOK-ID-1").verify(headers(), raw)

    sent = harness.fake.calls("POST", VERIFY)[0]
    assert verified is True
    assert raw in sent.content
    body = json.loads(sent.content)
    assert body["webhook_id"] == "WEBHOOK-ID-1"
    assert body["auth_algo"] == "SHA256withRSA"
    assert body["transmission_id"] == "69cd13f0-d67a-11e5-baa3-778b53f4ae55"
    assert body["cert_url"].startswith("https://api.sandbox.paypal.com/")
    assert body["webhook_event"]["id"] == "WH-1"
    assert sent.headers["Content-Type"] == "application/json"


async def test_a_failure_verdict_means_not_verified(harness: Harness) -> None:
    harness.fake.queue("POST", VERIFY, httpx.Response(200, json={"verification_status": "FAILURE"}))
    async with harness.client() as client:
        assert await WebhookVerifier(client, "W").verify(headers(), event_body()) is False


async def test_a_rejected_verification_request_means_not_verified(harness: Harness) -> None:
    harness.fake.queue(
        "POST", VERIFY, httpx.Response(400, json={"name": "INVALID_REQUEST", "message": "bad"})
    )
    async with harness.client() as client:
        assert await WebhookVerifier(client, "W").verify(headers(), event_body()) is False


async def test_a_body_that_is_not_json_is_not_sent_to_paypal(harness: Harness) -> None:
    async with harness.client() as client:
        verified = await WebhookVerifier(client, "W").verify(headers(), b"not json")
    assert verified is False
    assert harness.fake.calls("POST", VERIFY) == []


async def test_a_paypal_outage_during_verification_is_not_hidden(harness: Harness) -> None:
    harness.fake.queue("POST", VERIFY, httpx.Response(503, json={"name": "DOWN"}))
    async with harness.client(max_retries=1) as client:
        with pytest.raises(PayPalUnavailable):
            await WebhookVerifier(client, "W").verify(headers(), event_body())


async def test_an_auth_problem_during_verification_is_not_hidden(harness: Harness) -> None:
    harness.fake.queue("POST", VERIFY, httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}))
    async with harness.client() as client:
        with pytest.raises(PayPalAuthError):
            await WebhookVerifier(client, "W").verify(headers(), event_body())


async def test_registering_sends_the_url_and_the_invoice_events(harness: Harness) -> None:
    harness.fake.queue(
        "POST",
        HOOKS,
        httpx.Response(
            201,
            json={
                "id": "WH-ID-1",
                "url": "https://tether.example.com/webhooks/paypal",
                "event_types": [{"name": name} for name in INVOICE_EVENT_TYPES],
            },
        ),
    )
    async with harness.client() as client:
        hook = await WebhookRegistry(client).register("https://tether.example.com/webhooks/paypal")

    sent = json.loads(harness.fake.calls("POST", HOOKS)[0].content)
    assert sent["url"] == "https://tether.example.com/webhooks/paypal"
    assert [entry["name"] for entry in sent["event_types"]] == list(INVOICE_EVENT_TYPES)
    assert hook.id == "WH-ID-1"
    assert hook.event_types == INVOICE_EVENT_TYPES


async def test_listing_returns_every_registered_webhook(harness: Harness) -> None:
    harness.fake.queue(
        "GET",
        HOOKS,
        httpx.Response(
            200,
            json={
                "webhooks": [
                    {"id": "A", "url": "https://a.example.com", "event_types": [{"name": "X"}]},
                    {"id": "B", "url": "https://b.example.com", "event_types": []},
                ]
            },
        ),
    )
    async with harness.client() as client:
        hooks = await WebhookRegistry(client).list()
    assert [(hook.id, hook.event_types) for hook in hooks] == [("A", ("X",)), ("B", ())]


async def test_listing_with_none_registered_returns_nothing(harness: Harness) -> None:
    harness.fake.queue("GET", HOOKS, httpx.Response(200, json={}))
    async with harness.client() as client:
        assert await WebhookRegistry(client).list() == []


async def test_deleting_a_webhook_calls_the_right_path(harness: Harness) -> None:
    harness.fake.queue("DELETE", f"{HOOKS}/WH-ID-1", httpx.Response(204))
    async with harness.client() as client:
        await WebhookRegistry(client).delete("WH-ID-1")
    assert len(harness.fake.calls("DELETE", f"{HOOKS}/WH-ID-1")) == 1


async def test_an_unreadable_registration_response_is_an_error(harness: Harness) -> None:
    harness.fake.queue("POST", HOOKS, httpx.Response(201, json={"unexpected": True}))
    async with harness.client() as client:
        with pytest.raises(PayPalResponseError):
            await WebhookRegistry(client).register("https://x.example.com/webhooks/paypal")


def test_headers_are_read_regardless_of_case() -> None:
    parsed = WebhookHeaders.from_headers({k.lower(): v for k, v in signed_headers().items()})
    assert parsed.transmission_id == "69cd13f0-d67a-11e5-baa3-778b53f4ae55"
    assert parsed.auth_algo == "SHA256withRSA"


def test_missing_headers_are_named_in_the_error() -> None:
    incomplete = signed_headers()
    del incomplete["PayPal-Transmission-Sig"]
    with pytest.raises(MissingWebhookHeader, match="paypal-transmission-sig"):
        WebhookHeaders.from_headers(incomplete)


def event(**fields: object) -> WebhookEvent:
    base: dict[str, object] = {"id": "WH-1", "event_type": "INVOICING.INVOICE.PAID", "resource": {}}
    base.update(fields)
    return WebhookEvent.model_validate(base)


@pytest.mark.parametrize(
    "resource",
    [
        {"id": INVOICE_ID},
        {"invoice": {"id": INVOICE_ID}},
        {"invoice_id": INVOICE_ID},
        {"links": [{"rel": "self", "href": f"https://x.test/v2/invoicing/invoices/{INVOICE_ID}"}]},
    ],
)
def test_the_invoice_id_is_found_in_every_known_payload_shape(resource: dict[str, object]) -> None:
    assert invoice_id_from_event(event(resource=resource)) == INVOICE_ID


@pytest.mark.parametrize(
    "resource",
    [{}, {"id": "PAY-123"}, {"invoice": "not-a-dict"}, {"links": [{"rel": "next", "href": "x"}]}],
)
def test_events_without_an_invoice_id_yield_nothing(resource: dict[str, object]) -> None:
    assert invoice_id_from_event(event(resource=resource)) is None


def test_non_invoice_events_yield_nothing_even_with_an_invoice_like_id() -> None:
    other = event(event_type="PAYMENT.CAPTURE.COMPLETED", resource={"id": INVOICE_ID})
    assert invoice_id_from_event(other) is None
