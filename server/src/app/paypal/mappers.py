from collections.abc import Mapping
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from app.lib.time import days_overdue
from app.paypal.errors import PayPalResponseError
from app.schemas.actions import InvoiceStatus
from app.schemas.invoice import InvoiceDraft, PayPalInvoiceStatus, RemoteInvoice

CENT = Decimal("0.01")
P = PayPalInvoiceStatus

NOT_YET_SENT = frozenset({P.DRAFT, P.SCHEDULED})
AWAITING_PAYMENT = frozenset({P.SENT, P.UNPAID, P.SHARED, P.PAYMENT_PENDING})
SETTLED_BY_PAYMENT = frozenset(
    {
        P.PAID,
        P.MARKED_AS_PAID,
        P.PAID_EXTERNAL,
        P.PARTIALLY_REFUNDED,
        P.REFUNDED,
        P.MARKED_AS_REFUNDED,
        P.REFUNDED_EXTERNAL,
    }
)
CANCELLED_STATUSES = frozenset({P.CANCELLED, P.AUTO_CANCELLED})


def money(value: Decimal, currency: str) -> dict[str, str]:
    return {
        "currency_code": currency,
        "value": str(value.quantize(CENT, rounding=ROUND_HALF_UP)),
    }


def split_name(full_name: str) -> dict[str, str]:
    parts = full_name.split()
    if len(parts) == 1:
        return {"given_name": parts[0]}
    return {"given_name": parts[0], "surname": " ".join(parts[1:])}


def to_paypal_draft(
    draft: InvoiceDraft,
    invoice_number: str,
    issue_date: date,
    merchant_name: str | None = None,
) -> dict[str, Any]:
    due_date = issue_date + timedelta(days=draft.due_in_days)
    detail: dict[str, Any] = {
        "invoice_number": invoice_number,
        "invoice_date": issue_date.isoformat(),
        "currency_code": draft.currency,
        "payment_term": {
            "term_type": "DUE_ON_DATE_SPECIFIED",
            "due_date": due_date.isoformat(),
        },
    }
    if draft.reference:
        detail["reference"] = draft.reference
    if draft.note:
        detail["note"] = draft.note

    payload: dict[str, Any] = {
        "detail": detail,
        "primary_recipients": [
            {
                "billing_info": {
                    "name": split_name(draft.client_name),
                    "email_address": draft.client_email,
                }
            }
        ],
        "items": [_item(item, draft.currency) for item in draft.items],
    }
    if draft.allow_partial_payment:
        payload["configuration"] = {"partial_payment": {"allow_partial_payment": True}}
    if merchant_name:
        payload["invoicer"] = {"name": {"business_name": merchant_name}}
    return payload


def _item(item: Any, currency: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": item.name,
        "quantity": format(item.quantity.normalize(), "f"),
        "unit_amount": money(item.unit_amount, currency),
    }
    if item.description:
        entry["description"] = item.description
    return entry


def from_paypal_invoice(payload: Mapping[str, Any]) -> RemoteInvoice:
    try:
        detail = payload["detail"]
        currency = str(detail["currency_code"])
        total = Decimal(str(payload["amount"]["value"]))
        paid = _paid_amount(payload)
        balance = _balance(payload, total, paid)
        client_name, client_email = _recipient(payload)
        return RemoteInvoice(
            id=str(payload["id"]),
            number=detail.get("invoice_number"),
            paypal_status=PayPalInvoiceStatus(payload["status"]),
            currency=currency,
            total=total,
            paid=paid,
            balance=balance,
            issue_date=_parse_date(detail.get("invoice_date")),
            due_date=_parse_date((detail.get("payment_term") or {}).get("due_date")),
            client_name=client_name,
            client_email=client_email,
            payer_view_url=(detail.get("metadata") or {}).get("recipient_view_url"),
            reference=detail.get("reference"),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, ValidationError) as error:
        raise PayPalResponseError(f"could not read the invoice PayPal returned: {error}") from error


def to_internal_status(invoice: RemoteInvoice, today: date) -> InvoiceStatus:
    status = invoice.paypal_status
    if status in NOT_YET_SENT:
        return InvoiceStatus.DRAFT
    if status in CANCELLED_STATUSES:
        return InvoiceStatus.CANCELLED
    if status in SETTLED_BY_PAYMENT:
        return InvoiceStatus.PAID
    if status is P.PARTIALLY_PAID:
        return InvoiceStatus.PARTIALLY_PAID
    if status in AWAITING_PAYMENT:
        if invoice.balance > 0 and days_overdue(invoice.due_date, today) > 0:
            return InvoiceStatus.OVERDUE
        return InvoiceStatus.SENT
    raise PayPalResponseError(f"no internal status is defined for {status.value}")


def invoice_id_from_href(href: str) -> str:
    segments = [segment for segment in urlparse(href).path.split("/") if segment]
    if len(segments) < 2 or segments[-2] != "invoices":
        raise PayPalResponseError(f"could not find an invoice id in {href!r}")
    return segments[-1]


def _paid_amount(payload: Mapping[str, Any]) -> Decimal:
    paid = (payload.get("payments") or {}).get("paid_amount")
    return Decimal(str(paid["value"])) if paid else Decimal(0)


def _balance(payload: Mapping[str, Any], total: Decimal, paid: Decimal) -> Decimal:
    due = payload.get("due_amount")
    if due:
        return Decimal(str(due["value"]))
    return max(total - paid, Decimal(0))


def _recipient(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    recipients = payload.get("primary_recipients") or []
    if not recipients:
        return None, None
    billing = recipients[0].get("billing_info") or {}
    name = billing.get("name") or {}
    given = name.get("given_name", "")
    surname = name.get("surname", "")
    full = f"{given} {surname}".strip() or billing.get("business_name") or name.get("full_name")
    email = billing.get("email_address")
    return full or None, email.strip().lower() if email else None


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])
