import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalRequestError, PayPalResponseError
from app.schemas.webhooks import WebhookEvent, WebhookHeaders

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"
WEBHOOKS_PATH = "/v1/notifications/webhooks"
INVOICE_EVENT_PREFIX = "INVOICING.INVOICE."
INVOICE_ID_PREFIX = "INV2-"

INVOICE_EVENT_TYPES = (
    "INVOICING.INVOICE.CREATED",
    "INVOICING.INVOICE.UPDATED",
    "INVOICING.INVOICE.SCHEDULED",
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.REFUNDED",
    "INVOICING.INVOICE.CANCELLED",
)


@dataclass(frozen=True)
class RegisteredWebhook:
    id: str
    url: str
    event_types: tuple[str, ...]


class WebhookVerifier:
    def __init__(self, client: PayPalClient, webhook_id: str) -> None:
        self._client = client
        self._webhook_id = webhook_id

    async def verify(self, headers: WebhookHeaders, raw_body: bytes) -> bool:
        try:
            json.loads(raw_body)
        except ValueError:
            return False

        fields = {
            "auth_algo": headers.auth_algo,
            "cert_url": headers.cert_url,
            "transmission_id": headers.transmission_id,
            "transmission_sig": headers.transmission_sig,
            "transmission_time": headers.transmission_time,
            "webhook_id": self._webhook_id,
        }
        opening = json.dumps(fields, separators=(",", ":"))[:-1].encode()
        content = opening + b',"webhook_event":' + raw_body + b"}"

        try:
            result = await self._client.request("POST", VERIFY_PATH, content=content)
        except PayPalRequestError:
            return False
        return result.get("verification_status") == "SUCCESS"


class WebhookRegistry:
    def __init__(self, client: PayPalClient) -> None:
        self._client = client

    async def register(
        self, url: str, event_types: Sequence[str] = INVOICE_EVENT_TYPES
    ) -> RegisteredWebhook:
        body = await self._client.request(
            "POST",
            WEBHOOKS_PATH,
            json={"url": url, "event_types": [{"name": name} for name in event_types]},
        )
        return _registered(body)

    async def list(self) -> list[RegisteredWebhook]:
        body = await self._client.request("GET", WEBHOOKS_PATH)
        return [_registered(entry) for entry in body.get("webhooks") or []]

    async def delete(self, webhook_id: str) -> None:
        await self._client.request("DELETE", f"{WEBHOOKS_PATH}/{webhook_id}")


def invoice_id_from_event(event: WebhookEvent) -> str | None:
    if not event.event_type.startswith(INVOICE_EVENT_PREFIX):
        return None

    resource = event.resource
    nested = resource.get("invoice")
    candidates: list[Any] = [
        resource.get("id"),
        nested.get("id") if isinstance(nested, dict) else None,
        resource.get("invoice_id"),
    ]
    for link in resource.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "self":
            candidates.append(_id_from_href(str(link.get("href", ""))))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(INVOICE_ID_PREFIX):
            return candidate
    return None


def _id_from_href(href: str) -> str | None:
    segments = [segment for segment in urlparse(href).path.split("/") if segment]
    if len(segments) >= 2 and segments[-2] == "invoices":
        return segments[-1]
    return None


def _registered(body: dict[str, Any]) -> RegisteredWebhook:
    try:
        return RegisteredWebhook(
            id=str(body["id"]),
            url=str(body["url"]),
            event_types=tuple(str(entry["name"]) for entry in body.get("event_types") or []),
        )
    except (KeyError, TypeError) as error:
        raise PayPalResponseError("could not read the webhook PayPal returned") from error
