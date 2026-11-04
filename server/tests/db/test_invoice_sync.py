from datetime import date
from typing import Any

import pytest

from app.db.database import Database
from app.db.repositories import AuditRepository, ClientRepository, InvoiceRepository
from app.paypal.mappers import from_paypal_invoice
from app.schemas.actions import InvoiceStatus
from app.schemas.invoice import RemoteInvoice
from app.schemas.records import AuditActor
from app.services.invoice_sync import InvoiceSyncService, SyncOutcome, SyncResult, SyncSource
from tests.db.conftest import StepClock
from tests.paypal.helpers import sample_invoice

pytestmark = pytest.mark.anyio

TODAY = date(2026, 11, 4)
AFTER_DUE = date(2026, 11, 25)
PAID_STATUS: dict[str, Any] = {
    "status": "PAID",
    "due_amount": {"currency_code": "USD", "value": "0.00"},
}


def remote(**overrides: Any) -> RemoteInvoice:
    return from_paypal_invoice(sample_invoice(**overrides))


async def apply(
    db: Database,
    clock: StepClock,
    invoice: RemoteInvoice,
    *,
    today: date = TODAY,
    source: SyncSource = SyncSource.WEBHOOK,
) -> SyncResult:
    async with db.session() as session:
        service = InvoiceSyncService(
            InvoiceRepository(session, clock),
            ClientRepository(session, clock),
            AuditRepository(session, clock),
        )
        return await service.apply(invoice, today=today, source=source)


async def audit_kinds(db: Database) -> list[str]:
    async with db.session() as session:
        entries = await AuditRepository(session).list()
    return [entry.kind for entry in reversed(entries)]


async def test_an_unknown_invoice_is_imported_with_its_client(
    db: Database, clock: StepClock
) -> None:
    result = await apply(db, clock, remote(), source=SyncSource.IMPORT)

    assert result.outcome is SyncOutcome.CREATED
    invoice = result.invoice
    assert invoice.paypal_id == "INV2-ABCD-1234-WXYZ-5678"
    assert invoice.status is InvoiceStatus.SENT
    assert invoice.total == invoice.balance
    assert invoice.client is not None
    assert invoice.client.email == "dan@example.com"
    assert invoice.client.name == "Dan Okoro"
    assert await audit_kinds(db) == ["invoice_imported"]


async def test_imports_are_attributed_to_the_system_and_webhooks_to_paypal(
    db: Database, clock: StepClock
) -> None:
    await apply(db, clock, remote(id="INV2-A"), source=SyncSource.IMPORT)
    await apply(db, clock, remote(id="INV2-A", **PAID_STATUS), source=SyncSource.WEBHOOK)

    async with db.session() as session:
        entries = await AuditRepository(session).list()
    assert [(entry.actor, entry.detail["source"]) for entry in reversed(entries)] == [
        (AuditActor.SYSTEM, "import"),
        (AuditActor.PAYPAL, "webhook"),
    ]


async def test_applying_the_same_state_twice_changes_nothing(
    db: Database, clock: StepClock
) -> None:
    await apply(db, clock, remote())
    again = await apply(db, clock, remote())

    assert again.outcome is SyncOutcome.UNCHANGED
    assert await audit_kinds(db) == ["invoice_imported"]


async def test_a_payment_moves_the_invoice_to_paid_and_is_audited(
    db: Database, clock: StepClock
) -> None:
    await apply(db, clock, remote())
    paid = remote(
        status="PAID",
        due_amount={"currency_code": "USD", "value": "0.00"},
        payments={"paid_amount": {"currency_code": "USD", "value": "400.00"}},
    )
    result = await apply(db, clock, paid)

    assert result.outcome is SyncOutcome.UPDATED
    assert result.previous_status is InvoiceStatus.SENT
    assert result.invoice.status is InvoiceStatus.PAID
    assert result.invoice.paid == 400
    assert result.invoice.balance == 0

    async with db.session() as session:
        entries = await AuditRepository(session).list(limit=1)
    assert entries[0].kind == "invoice_status_changed"
    assert entries[0].detail["from"] == "sent"
    assert entries[0].detail["to"] == "paid"


async def test_a_partial_payment_then_a_full_payment(db: Database, clock: StepClock) -> None:
    await apply(db, clock, remote())
    partial = remote(
        status="PARTIALLY_PAID",
        payments={"paid_amount": {"currency_code": "USD", "value": "100.00"}},
        due_amount={"currency_code": "USD", "value": "300.00"},
    )
    first = await apply(db, clock, partial)
    second = await apply(db, clock, remote(**PAID_STATUS))

    assert first.invoice.status is InvoiceStatus.PARTIALLY_PAID
    assert first.invoice.balance == 300
    assert second.invoice.status is InvoiceStatus.PAID


async def test_a_bigger_partial_payment_updates_money_without_a_status_change(
    db: Database, clock: StepClock
) -> None:
    def partial(paid: str, due: str) -> RemoteInvoice:
        return remote(
            status="PARTIALLY_PAID",
            payments={"paid_amount": {"currency_code": "USD", "value": paid}},
            due_amount={"currency_code": "USD", "value": due},
        )

    await apply(db, clock, partial("100.00", "300.00"))
    result = await apply(db, clock, partial("150.00", "250.00"))

    assert result.outcome is SyncOutcome.UPDATED
    assert result.invoice.paid == 150
    assert result.invoice.balance == 250
    assert (await audit_kinds(db))[-1] == "invoice_updated"


async def test_an_unpaid_invoice_becomes_overdue_after_its_due_date(
    db: Database, clock: StepClock
) -> None:
    await apply(db, clock, remote(), today=TODAY)
    result = await apply(db, clock, remote(), today=AFTER_DUE)

    assert result.outcome is SyncOutcome.UPDATED
    assert result.invoice.status is InvoiceStatus.OVERDUE


async def test_an_overdue_invoice_can_still_be_paid(db: Database, clock: StepClock) -> None:
    await apply(db, clock, remote(), today=AFTER_DUE)
    result = await apply(db, clock, remote(**PAID_STATUS), today=AFTER_DUE)
    assert result.invoice.status is InvoiceStatus.PAID


async def test_a_stale_event_cannot_undo_a_payment(db: Database, clock: StepClock) -> None:
    await apply(db, clock, remote(**PAID_STATUS))
    result = await apply(db, clock, remote())

    assert result.outcome is SyncOutcome.REJECTED
    assert result.reason is not None
    assert result.invoice.status is InvoiceStatus.PAID
    assert result.invoice.balance == 0

    async with db.session() as session:
        stored = await InvoiceRepository(session).get(result.invoice.id)
        entries = await AuditRepository(session).list(limit=1)
    assert stored is not None
    assert stored.status is InvoiceStatus.PAID
    assert entries[0].kind == "transition_rejected"
    assert entries[0].detail["from"] == "paid"
    assert entries[0].detail["attempted"] == "sent"


async def test_a_cancelled_invoice_stays_cancelled(db: Database, clock: StepClock) -> None:
    await apply(db, clock, remote(status="CANCELLED"))
    result = await apply(db, clock, remote(**PAID_STATUS))
    assert result.outcome is SyncOutcome.REJECTED
    assert result.invoice.status is InvoiceStatus.CANCELLED


async def test_a_draft_moves_to_sent(db: Database, clock: StepClock) -> None:
    await apply(db, clock, remote(status="DRAFT"))
    result = await apply(db, clock, remote(status="SENT"))
    assert result.invoice.status is InvoiceStatus.SENT


async def test_refunded_invoices_stay_settled(db: Database, clock: StepClock) -> None:
    await apply(db, clock, remote(**PAID_STATUS))
    result = await apply(
        db, clock, remote(status="REFUNDED", due_amount={"currency_code": "USD", "value": "0.00"})
    )
    assert result.invoice.status is InvoiceStatus.PAID
    assert result.invoice.paypal_status.value == "REFUNDED"


async def test_an_invoice_without_a_recipient_is_imported_without_a_client(
    db: Database, clock: StepClock
) -> None:
    result = await apply(db, clock, remote(primary_recipients=[]))
    assert result.outcome is SyncOutcome.CREATED
    assert result.invoice.client is None


async def test_invoices_for_the_same_email_share_one_client(db: Database, clock: StepClock) -> None:
    first = await apply(db, clock, remote(id="INV2-A"))
    second = await apply(db, clock, remote(id="INV2-B"))

    assert first.invoice.client is not None
    assert second.invoice.client is not None
    assert first.invoice.client.id == second.invoice.client.id


async def test_a_client_without_a_name_is_named_after_their_email(
    db: Database, clock: StepClock
) -> None:
    recipients = [{"billing_info": {"email_address": "ruth@example.com"}}]
    result = await apply(db, clock, remote(primary_recipients=recipients))
    assert result.invoice.client is not None
    assert result.invoice.client.name == "ruth@example.com"
