from datetime import date
from decimal import Decimal

import pytest

from app.paypal.errors import PayPalResponseError
from app.paypal.mappers import (
    from_paypal_invoice,
    invoice_id_from_href,
    money,
    split_name,
    to_internal_status,
    to_paypal_draft,
)
from app.schemas.actions import InvoiceStatus
from app.schemas.invoice import InvoiceDraft, LineItem, PayPalInvoiceStatus
from tests.paypal.helpers import sample_invoice

TODAY = date(2026, 11, 4)


def make_draft(**overrides: object) -> InvoiceDraft:
    fields: dict[str, object] = {
        "client_name": "Dan Okoro",
        "client_email": "Dan@Example.com",
        "items": (LineItem(name="Logo design", unit_amount=Decimal("400")),),
        "due_in_days": 14,
        "reference": "tether-demo-1",
        "note": "Thank you for your business.",
    }
    fields.update(overrides)
    return InvoiceDraft(**fields)  # type: ignore[arg-type]


def test_draft_payload_has_the_fields_paypal_requires() -> None:
    payload = to_paypal_draft(make_draft(), "0042", TODAY)
    assert payload["detail"] == {
        "invoice_number": "0042",
        "invoice_date": "2026-11-04",
        "currency_code": "USD",
        "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": "2026-11-18"},
        "reference": "tether-demo-1",
        "note": "Thank you for your business.",
    }
    assert payload["primary_recipients"] == [
        {
            "billing_info": {
                "name": {"given_name": "Dan", "surname": "Okoro"},
                "email_address": "dan@example.com",
            }
        }
    ]
    assert payload["items"] == [
        {
            "name": "Logo design",
            "quantity": "1",
            "unit_amount": {"currency_code": "USD", "value": "400.00"},
        }
    ]
    assert "configuration" not in payload
    assert "invoicer" not in payload


def test_optional_sections_are_added_only_when_requested() -> None:
    draft = make_draft(allow_partial_payment=True, reference=None, note=None)
    payload = to_paypal_draft(draft, "0042", TODAY, merchant_name="Tether Demo Studio")
    assert payload["configuration"] == {"partial_payment": {"allow_partial_payment": True}}
    assert payload["invoicer"] == {"name": {"business_name": "Tether Demo Studio"}}
    assert "reference" not in payload["detail"]
    assert "note" not in payload["detail"]


def test_due_date_follows_the_requested_terms() -> None:
    payload = to_paypal_draft(make_draft(due_in_days=0), "1", TODAY)
    assert payload["detail"]["payment_term"]["due_date"] == "2026-11-04"


def test_quantities_and_item_descriptions_are_formatted_for_paypal() -> None:
    item = LineItem(
        name="Hours", description="Consulting", quantity=Decimal("2.50"), unit_amount=Decimal("80")
    )
    payload = to_paypal_draft(make_draft(items=(item,)), "1", TODAY)
    assert payload["items"][0]["quantity"] == "2.5"
    assert payload["items"][0]["description"] == "Consulting"
    assert make_draft(items=(item,)).total == Decimal("200.00")


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [
        ("Dan", {"given_name": "Dan"}),
        ("Dan Okoro", {"given_name": "Dan", "surname": "Okoro"}),
        ("Maria de la Cruz", {"given_name": "Maria", "surname": "de la Cruz"}),
    ],
)
def test_names_are_split_for_paypal(full_name: str, expected: dict[str, str]) -> None:
    assert split_name(full_name) == expected


def test_money_is_rounded_to_cents_as_a_string() -> None:
    assert money(Decimal("10"), "USD") == {"currency_code": "USD", "value": "10.00"}
    assert money(Decimal("10.005"), "EUR") == {"currency_code": "EUR", "value": "10.01"}


def test_a_paypal_invoice_is_mapped_into_our_model() -> None:
    invoice = from_paypal_invoice(sample_invoice())
    assert invoice.id == "INV2-ABCD-1234-WXYZ-5678"
    assert invoice.number == "0042"
    assert invoice.paypal_status is PayPalInvoiceStatus.SENT
    assert invoice.total == Decimal("400.00")
    assert invoice.paid == Decimal("0")
    assert invoice.balance == Decimal("400.00")
    assert invoice.issue_date == date(2026, 11, 4)
    assert invoice.due_date == date(2026, 11, 18)
    assert invoice.client_name == "Dan Okoro"
    assert invoice.client_email == "dan@example.com"
    assert invoice.payer_view_url == "https://www.sandbox.paypal.com/invoice/p/#ABCD"
    assert invoice.reference == "tether-demo-1"


def test_paid_amounts_come_from_the_payments_block() -> None:
    payload = sample_invoice(
        status="PARTIALLY_PAID",
        payments={"paid_amount": {"currency_code": "USD", "value": "150.00"}},
        due_amount={"currency_code": "USD", "value": "250.00"},
    )
    invoice = from_paypal_invoice(payload)
    assert invoice.paid == Decimal("150.00")
    assert invoice.balance == Decimal("250.00")


def test_balance_is_derived_when_paypal_omits_the_due_amount() -> None:
    payload = sample_invoice(payments={"paid_amount": {"currency_code": "USD", "value": "100"}})
    del payload["due_amount"]
    assert from_paypal_invoice(payload).balance == Decimal("300")


def test_a_business_recipient_uses_the_business_name() -> None:
    payload = sample_invoice(
        primary_recipients=[
            {"billing_info": {"business_name": "Okoro Ltd", "email_address": "ap@okoro.test"}}
        ]
    )
    assert from_paypal_invoice(payload).client_name == "Okoro Ltd"


def test_a_recipientless_invoice_still_maps() -> None:
    invoice = from_paypal_invoice(sample_invoice(primary_recipients=[]))
    assert invoice.client_name is None
    assert invoice.client_email is None


@pytest.mark.parametrize("missing", ["id", "status", "detail", "amount"])
def test_malformed_payloads_fail_loudly(missing: str) -> None:
    payload = sample_invoice()
    del payload[missing]
    with pytest.raises(PayPalResponseError):
        from_paypal_invoice(payload)


def test_an_unknown_paypal_status_fails_loudly() -> None:
    with pytest.raises(PayPalResponseError):
        from_paypal_invoice(sample_invoice(status="SOMETHING_NEW"))


def internal(status: str, today: date = TODAY, **overrides: object) -> InvoiceStatus:
    return to_internal_status(
        from_paypal_invoice(sample_invoice(status=status, **overrides)), today
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("DRAFT", InvoiceStatus.DRAFT),
        ("SCHEDULED", InvoiceStatus.DRAFT),
        ("SENT", InvoiceStatus.SENT),
        ("UNPAID", InvoiceStatus.SENT),
        ("SHARED", InvoiceStatus.SENT),
        ("PAYMENT_PENDING", InvoiceStatus.SENT),
        ("PARTIALLY_PAID", InvoiceStatus.PARTIALLY_PAID),
        ("PAID", InvoiceStatus.PAID),
        ("MARKED_AS_PAID", InvoiceStatus.PAID),
        ("PAID_EXTERNAL", InvoiceStatus.PAID),
        ("REFUNDED", InvoiceStatus.PAID),
        ("PARTIALLY_REFUNDED", InvoiceStatus.PAID),
        ("MARKED_AS_REFUNDED", InvoiceStatus.PAID),
        ("REFUNDED_EXTERNAL", InvoiceStatus.PAID),
        ("CANCELLED", InvoiceStatus.CANCELLED),
        ("AUTO_CANCELLED", InvoiceStatus.CANCELLED),
    ],
)
def test_every_paypal_status_maps_to_an_internal_status(
    status: str, expected: InvoiceStatus
) -> None:
    assert internal(status) == expected


def test_every_paypal_status_has_a_mapping() -> None:
    for status in PayPalInvoiceStatus:
        assert isinstance(internal(status.value), InvoiceStatus)


def test_an_unpaid_invoice_becomes_overdue_the_day_after_it_is_due() -> None:
    assert internal("SENT", today=date(2026, 11, 18)) == InvoiceStatus.SENT
    assert internal("SENT", today=date(2026, 11, 19)) == InvoiceStatus.OVERDUE
    assert internal("UNPAID", today=date(2026, 12, 25)) == InvoiceStatus.OVERDUE


def test_an_invoice_with_no_balance_is_never_overdue() -> None:
    due = {"currency_code": "USD", "value": "0.00"}
    assert internal("SENT", today=date(2027, 1, 1), due_amount=due) == InvoiceStatus.SENT


def test_a_paid_invoice_is_never_overdue() -> None:
    assert internal("PAID", today=date(2027, 1, 1)) == InvoiceStatus.PAID


def test_an_invoice_without_a_due_date_is_never_overdue() -> None:
    payload = sample_invoice(status="SENT")
    payload["detail"] = {k: v for k, v in payload["detail"].items() if k != "payment_term"}
    assert to_internal_status(from_paypal_invoice(payload), date(2030, 1, 1)) == InvoiceStatus.SENT


def test_invoice_ids_are_read_from_links() -> None:
    href = "https://api.sandbox.paypal.com/v2/invoicing/invoices/INV2-ABCD-1234"
    assert invoice_id_from_href(href) == "INV2-ABCD-1234"
    assert invoice_id_from_href(href + "/") == "INV2-ABCD-1234"


@pytest.mark.parametrize("href", ["", "https://", "https://example.com"])
def test_unusable_links_are_rejected(href: str) -> None:
    with pytest.raises(PayPalResponseError):
        invoice_id_from_href(href)
