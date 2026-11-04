import json
from datetime import date
from typing import Any

from app.paypal.errors import PayPalNotFound
from app.paypal.mappers import from_paypal_invoice
from app.schemas.invoice import RemoteInvoice
from app.schemas.webhooks import WebhookHeaders
from tests.paypal.helpers import sample_invoice

INVOICE_ID = "INV2-ABCD-1234-WXYZ-5678"
TODAY = date(2026, 11, 4)
PAID: dict[str, Any] = {
    "status": "PAID",
    "due_amount": {"currency_code": "USD", "value": "0.00"},
    "payments": {"paid_amount": {"currency_code": "USD", "value": "400.00"}},
}


def signed_headers(**overrides: str) -> dict[str, str]:
    headers = {
        "PayPal-Auth-Algo": "SHA256withRSA",
        "PayPal-Cert-Url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
        "PayPal-Transmission-Id": "69cd13f0-d67a-11e5-baa3-778b53f4ae55",
        "PayPal-Transmission-Sig": "c2lnbmF0dXJl",
        "PayPal-Transmission-Time": "2026-11-04T12:00:00Z",
    }
    headers.update(overrides)
    return headers


def event_body(
    event_id: str = "WH-1",
    event_type: str = "INVOICING.INVOICE.PAID",
    invoice_id: str | None = INVOICE_ID,
    shape: str = "flat",
) -> bytes:
    resource: dict[str, Any] = {}
    if invoice_id is not None:
        if shape == "flat":
            resource = {"id": invoice_id, "status": "PAID"}
        elif shape == "nested":
            resource = {"invoice": {"id": invoice_id, "status": "PAID"}}
        elif shape == "links":
            resource = {
                "links": [
                    {
                        "rel": "self",
                        "href": f"https://api.sandbox.paypal.com/v2/invoicing/invoices/{invoice_id}",
                    }
                ]
            }
    return json.dumps(
        {
            "id": event_id,
            "event_version": "1.0",
            "create_time": "2026-11-04T12:00:00.000Z",
            "resource_type": "invoices",
            "event_type": event_type,
            "summary": "Invoice paid",
            "resource": resource,
        }
    ).encode()


def remote(**overrides: Any) -> RemoteInvoice:
    return from_paypal_invoice(sample_invoice(**overrides))


class FakeVerifier:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[WebhookHeaders, bytes]] = []

    async def verify(self, headers: WebhookHeaders, raw_body: bytes) -> bool:
        self.calls.append((headers, raw_body))
        return self.result


class FakeInvoices:
    def __init__(self, **invoices: RemoteInvoice) -> None:
        self.invoices = dict(invoices)
        self.requested: list[str] = []
        self.failure: Exception | None = None

    def set(self, invoice: RemoteInvoice) -> None:
        self.invoices[invoice.id] = invoice

    async def get(self, invoice_id: str) -> RemoteInvoice:
        self.requested.append(invoice_id)
        if self.failure is not None:
            raise self.failure
        if invoice_id not in self.invoices:
            raise PayPalNotFound("not found", status=404, name="INVALID_RESOURCE_ID")
        return self.invoices[invoice_id]
