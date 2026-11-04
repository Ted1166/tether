import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.paypal.errors import PayPalNotFound, PayPalResponseError
from app.paypal.invoices import InvoiceGateway
from app.schemas.invoice import InvoiceDraft, LineItem, PaymentMethod, PayPalInvoiceStatus
from tests.paypal.helpers import Harness, sample_invoice

pytestmark = pytest.mark.anyio

BASE = "https://api-m.sandbox.paypal.com"
INVOICES = "/v2/invoicing/invoices"
NUMBER = "/v2/invoicing/generate-next-invoice-number"
INVOICE_ID = "INV2-ABCD-1234-WXYZ-5678"
TODAY = date(2026, 11, 4)


def draft() -> InvoiceDraft:
    return InvoiceDraft(
        client_name="Dan Okoro",
        client_email="dan@example.com",
        items=(LineItem(name="Logo design", unit_amount=Decimal("400")),),
        reference="tether-demo-1",
    )


def body_of(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)  # type: ignore[no-any-return]


async def test_creating_a_draft_numbers_it_posts_it_and_returns_the_id(harness: Harness) -> None:
    harness.fake.queue("POST", NUMBER, httpx.Response(200, json={"invoice_number": "0042"}))
    harness.fake.queue(
        "POST",
        INVOICES,
        httpx.Response(
            201, json={"rel": "self", "href": f"{BASE}{INVOICES}/{INVOICE_ID}", "method": "GET"}
        ),
    )
    async with harness.client() as client:
        invoice_id = await InvoiceGateway(client).create_draft(
            draft(), issue_date=TODAY, request_id="create-1"
        )

    sent = harness.fake.calls("POST", INVOICES)[0]
    assert invoice_id == INVOICE_ID
    assert sent.headers["PayPal-Request-Id"] == "create-1"
    assert body_of(sent)["detail"]["invoice_number"] == "0042"  # type: ignore[index]


async def test_the_merchant_name_is_added_when_configured(harness: Harness) -> None:
    harness.fake.queue("POST", NUMBER, httpx.Response(200, json={"invoice_number": "7"}))
    harness.fake.queue(
        "POST", INVOICES, httpx.Response(201, json={"href": f"{BASE}{INVOICES}/{INVOICE_ID}"})
    )
    async with harness.client() as client:
        gateway = InvoiceGateway(client, merchant_name="Tether Demo Studio")
        await gateway.create_draft(draft(), issue_date=TODAY)
    payload = body_of(harness.fake.calls("POST", INVOICES)[0])
    assert payload["invoicer"] == {"name": {"business_name": "Tether Demo Studio"}}


async def test_a_missing_invoice_number_is_an_error(harness: Harness) -> None:
    harness.fake.queue("POST", NUMBER, httpx.Response(200, json={}))
    async with harness.client() as client:
        with pytest.raises(PayPalResponseError):
            await InvoiceGateway(client).next_invoice_number()


async def test_a_missing_link_after_creation_is_an_error(harness: Harness) -> None:
    harness.fake.queue("POST", NUMBER, httpx.Response(200, json={"invoice_number": "1"}))
    harness.fake.queue("POST", INVOICES, httpx.Response(201, json={}))
    async with harness.client() as client:
        with pytest.raises(PayPalResponseError):
            await InvoiceGateway(client).create_draft(draft(), issue_date=TODAY)


async def test_sending_notifies_the_recipient_but_not_the_merchant(harness: Harness) -> None:
    path = f"{INVOICES}/{INVOICE_ID}/send"
    harness.fake.queue("POST", path, httpx.Response(202, json={"href": "x", "rel": "x"}))
    async with harness.client() as client:
        await InvoiceGateway(client).send(INVOICE_ID, request_id="send-1")
    request = harness.fake.calls("POST", path)[0]
    assert body_of(request) == {"send_to_recipient": True, "send_to_invoicer": False}
    assert request.headers["PayPal-Request-Id"] == "send-1"


async def test_sending_can_share_a_link_instead_of_an_email(harness: Harness) -> None:
    path = f"{INVOICES}/{INVOICE_ID}/send"
    harness.fake.queue("POST", path, httpx.Response(200, json={}))
    async with harness.client() as client:
        await InvoiceGateway(client).send(
            INVOICE_ID, notify_recipient=False, subject="Hello", note="Thanks"
        )
    assert body_of(harness.fake.calls("POST", path)[0]) == {
        "send_to_recipient": False,
        "send_to_invoicer": False,
        "subject": "Hello",
        "note": "Thanks",
    }


async def test_reminders_carry_the_subject_and_note(harness: Harness) -> None:
    path = f"{INVOICES}/{INVOICE_ID}/remind"
    harness.fake.queue("POST", path, httpx.Response(204))
    async with harness.client() as client:
        await InvoiceGateway(client).remind(INVOICE_ID, subject="Friendly nudge", note="Hi Dan")
    assert body_of(harness.fake.calls("POST", path)[0]) == {
        "subject": "Friendly nudge",
        "note": "Hi Dan",
        "send_to_invoicer": False,
    }


async def test_cancelling_sends_the_notification_settings(harness: Harness) -> None:
    path = f"{INVOICES}/{INVOICE_ID}/cancel"
    harness.fake.queue("POST", path, httpx.Response(204))
    async with harness.client() as client:
        await InvoiceGateway(client).cancel(INVOICE_ID, subject="Cancelled", note="Replaced")
    assert body_of(harness.fake.calls("POST", path)[0]) == {
        "subject": "Cancelled",
        "note": "Replaced",
        "send_to_invoicer": False,
        "send_to_recipient": True,
    }


async def test_recording_a_payment_returns_the_payment_id(harness: Harness) -> None:
    path = f"{INVOICES}/{INVOICE_ID}/payments"
    harness.fake.queue("POST", path, httpx.Response(200, json={"payment_id": "EXTR-123"}))
    async with harness.client() as client:
        payment_id = await InvoiceGateway(client).record_payment(
            INVOICE_ID,
            amount=Decimal("133.5"),
            currency="USD",
            paid_on=TODAY,
            note="Deposit",
        )
    assert payment_id == "EXTR-123"
    assert body_of(harness.fake.calls("POST", path)[0]) == {
        "method": PaymentMethod.BANK_TRANSFER.value,
        "payment_date": "2026-11-04",
        "amount": {"currency_code": "USD", "value": "133.50"},
        "note": "Deposit",
    }


async def test_a_missing_payment_id_is_an_error(harness: Harness) -> None:
    path = f"{INVOICES}/{INVOICE_ID}/payments"
    harness.fake.queue("POST", path, httpx.Response(200, json={}))
    async with harness.client() as client:
        with pytest.raises(PayPalResponseError):
            await InvoiceGateway(client).record_payment(
                INVOICE_ID, amount=Decimal("1"), currency="USD", paid_on=TODAY
            )


async def test_getting_an_invoice_maps_it(harness: Harness) -> None:
    harness.fake.queue(
        "GET", f"{INVOICES}/{INVOICE_ID}", httpx.Response(200, json=sample_invoice())
    )
    async with harness.client() as client:
        invoice = await InvoiceGateway(client).get(INVOICE_ID)
    assert invoice.paypal_status is PayPalInvoiceStatus.SENT
    assert invoice.client_email == "dan@example.com"


async def test_getting_an_unknown_invoice_raises_not_found(harness: Harness) -> None:
    harness.fake.queue(
        "GET", f"{INVOICES}/NOPE", httpx.Response(404, json={"name": "INVALID_RESOURCE_ID"})
    )
    async with harness.client() as client:
        with pytest.raises(PayPalNotFound):
            await InvoiceGateway(client).get("NOPE")


async def test_listing_maps_every_invoice_and_reads_paging(harness: Harness) -> None:
    items = [sample_invoice(id="INV2-A"), sample_invoice(id="INV2-B", status="PAID")]
    harness.fake.queue(
        "GET",
        INVOICES,
        httpx.Response(200, json={"items": items, "total_items": 5, "total_pages": 3}),
    )
    async with harness.client() as client:
        page = await InvoiceGateway(client).list_invoices(page=2, page_size=2)
    request = harness.fake.calls("GET", INVOICES)[0]
    assert [invoice.id for invoice in page.items] == ["INV2-A", "INV2-B"]
    assert (page.total_items, page.total_pages) == (5, 3)
    assert request.url.params["page"] == "2"
    assert request.url.params["page_size"] == "2"
    assert request.url.params["total_required"] == "true"


async def test_listing_an_empty_account_returns_an_empty_page(harness: Harness) -> None:
    harness.fake.queue("GET", INVOICES, httpx.Response(200, json={}))
    async with harness.client() as client:
        page = await InvoiceGateway(client).list_invoices()
    assert page.items == ()
    assert page.total_pages == 1
