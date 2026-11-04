from datetime import date
from decimal import Decimal
from typing import Any

from app.paypal.client import PayPalClient
from app.paypal.errors import PayPalResponseError
from app.paypal.mappers import (
    from_paypal_invoice,
    invoice_id_from_href,
    money,
    to_paypal_draft,
)
from app.schemas.invoice import (
    InvoiceDraft,
    InvoicePage,
    PaymentMethod,
    RemoteInvoice,
)

INVOICES = "/v2/invoicing/invoices"


class InvoiceGateway:
    def __init__(self, client: PayPalClient, merchant_name: str | None = None) -> None:
        self._client = client
        self._merchant_name = merchant_name

    async def next_invoice_number(self) -> str:
        body = await self._client.request("POST", "/v2/invoicing/generate-next-invoice-number")
        number = body.get("invoice_number")
        if not number:
            raise PayPalResponseError("PayPal did not return an invoice number")
        return str(number)

    async def create_draft(
        self, draft: InvoiceDraft, *, issue_date: date, request_id: str | None = None
    ) -> str:
        number = await self.next_invoice_number()
        payload = to_paypal_draft(draft, number, issue_date, self._merchant_name)
        body = await self._client.request("POST", INVOICES, json=payload, request_id=request_id)
        href = body.get("href")
        if not href:
            raise PayPalResponseError("PayPal did not return a link for the new invoice")
        return invoice_id_from_href(str(href))

    async def send(
        self,
        invoice_id: str,
        *,
        notify_recipient: bool = True,
        subject: str | None = None,
        note: str | None = None,
        request_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "send_to_recipient": notify_recipient,
            "send_to_invoicer": False,
        }
        if subject:
            payload["subject"] = subject
        if note:
            payload["note"] = note
        await self._client.request(
            "POST", f"{INVOICES}/{invoice_id}/send", json=payload, request_id=request_id
        )

    async def remind(
        self, invoice_id: str, *, subject: str, note: str, request_id: str | None = None
    ) -> None:
        payload = {"subject": subject, "note": note, "send_to_invoicer": False}
        await self._client.request(
            "POST", f"{INVOICES}/{invoice_id}/remind", json=payload, request_id=request_id
        )

    async def cancel(
        self,
        invoice_id: str,
        *,
        subject: str,
        note: str,
        notify_recipient: bool = True,
        request_id: str | None = None,
    ) -> None:
        payload = {
            "subject": subject,
            "note": note,
            "send_to_invoicer": False,
            "send_to_recipient": notify_recipient,
        }
        await self._client.request(
            "POST", f"{INVOICES}/{invoice_id}/cancel", json=payload, request_id=request_id
        )

    async def record_payment(
        self,
        invoice_id: str,
        *,
        amount: Decimal,
        currency: str,
        paid_on: date,
        method: PaymentMethod = PaymentMethod.BANK_TRANSFER,
        note: str | None = None,
        request_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "method": method.value,
            "payment_date": paid_on.isoformat(),
            "amount": money(amount, currency),
        }
        if note:
            payload["note"] = note
        body = await self._client.request(
            "POST", f"{INVOICES}/{invoice_id}/payments", json=payload, request_id=request_id
        )
        payment_id = body.get("payment_id")
        if not payment_id:
            raise PayPalResponseError("PayPal did not return a payment id")
        return str(payment_id)

    async def get(self, invoice_id: str) -> RemoteInvoice:
        body = await self._client.request("GET", f"{INVOICES}/{invoice_id}")
        return from_paypal_invoice(body)

    async def list_invoices(self, *, page: int = 1, page_size: int = 100) -> InvoicePage:
        body = await self._client.request(
            "GET",
            INVOICES,
            params={"page": page, "page_size": page_size, "total_required": "true"},
        )
        items = tuple(from_paypal_invoice(entry) for entry in body.get("items") or [])
        return InvoicePage(
            items=items,
            total_items=int(body.get("total_items") or len(items)),
            total_pages=int(body.get("total_pages") or 1),
        )
