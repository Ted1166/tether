from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import StatementError

from app.db.database import Database
from app.db.repositories import (
    AuditRepository,
    ClientRepository,
    DuplicateInvoice,
    InvoiceRepository,
)
from app.schemas.actions import InvoiceStatus, Verdict
from app.schemas.invoice import PayPalInvoiceStatus
from app.schemas.records import AuditActor, InvoiceChanges, NewAuditEntry, NewInvoice
from tests.db.conftest import StepClock

pytestmark = pytest.mark.anyio


def new_invoice(**overrides: object) -> NewInvoice:
    fields: dict[str, object] = {
        "paypal_id": "INV2-AAAA",
        "reference": "tether-demo-1",
        "number": "0001",
        "status": InvoiceStatus.SENT,
        "paypal_status": PayPalInvoiceStatus.SENT,
        "currency": "USD",
        "total": Decimal("133.50"),
        "balance": Decimal("133.50"),
        "issue_date": date(2026, 11, 4),
        "due_date": date(2026, 11, 18),
        "payer_view_url": "https://www.sandbox.paypal.com/invoice/p/#AAAA",
    }
    fields.update(overrides)
    return NewInvoice(**fields)  # type: ignore[arg-type]


async def test_a_client_is_created_once_per_email(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        clients = ClientRepository(session, clock)
        first = await clients.get_or_create("Dan Okoro", "Dan@Example.com")
        second = await clients.get_or_create("Someone Else", " dan@example.com ")
    assert first.id == second.id
    assert second.name == "Dan Okoro"
    assert second.email == "dan@example.com"


async def test_finding_a_missing_client_returns_nothing(db: Database) -> None:
    async with db.session() as session:
        assert await ClientRepository(session).find_by_email("nobody@example.com") is None


async def test_an_invoice_round_trips_with_exact_money(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        client = await ClientRepository(session, clock).get_or_create("Dan", "dan@example.com")
        created = await InvoiceRepository(session, clock).create(
            new_invoice(client_id=client.id, paid=Decimal("0.10"), balance=Decimal("133.40"))
        )

    async with db.session() as session:
        fetched = await InvoiceRepository(session).get(created.id)

    assert fetched is not None
    assert fetched.paypal_id == "INV2-AAAA"
    assert fetched.total == Decimal("133.50")
    assert fetched.paid == Decimal("0.10")
    assert fetched.balance == Decimal("133.40")
    assert fetched.status is InvoiceStatus.SENT
    assert fetched.paypal_status is PayPalInvoiceStatus.SENT
    assert fetched.due_date == date(2026, 11, 18)
    assert fetched.client is not None
    assert fetched.client.email == "dan@example.com"


async def test_timestamps_come_back_timezone_aware_in_utc(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        created = await InvoiceRepository(session, clock).create(new_invoice())
    async with db.session() as session:
        fetched = await InvoiceRepository(session).get(created.id)

    assert fetched is not None
    assert fetched.created_at == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert fetched.created_at.tzinfo is not None
    assert fetched.updated_at == fetched.created_at


async def test_invoices_can_be_found_by_their_paypal_id(db: Database) -> None:
    async with db.session() as session:
        repository = InvoiceRepository(session)
        await repository.create(new_invoice())
        assert (await repository.get_by_paypal_id("INV2-AAAA")) is not None
        assert (await repository.get_by_paypal_id("INV2-NOPE")) is None


async def test_a_duplicate_paypal_id_is_refused(db: Database) -> None:
    async with db.session() as session:
        repository = InvoiceRepository(session)
        await repository.create(new_invoice())
        with pytest.raises(DuplicateInvoice):
            await repository.create(new_invoice())


async def test_an_invoice_without_a_client_is_allowed(db: Database) -> None:
    async with db.session() as session:
        created = await InvoiceRepository(session).create(new_invoice(client_id=None))
    assert created.client is None


async def test_listing_returns_invoices_in_order_and_filters_by_status(db: Database) -> None:
    async with db.session() as session:
        repository = InvoiceRepository(session)
        await repository.create(new_invoice(paypal_id="INV2-1", number="0001"))
        await repository.create(
            new_invoice(
                paypal_id="INV2-2",
                number="0002",
                status=InvoiceStatus.PAID,
                paypal_status=PayPalInvoiceStatus.PAID,
            )
        )
        await repository.create(new_invoice(paypal_id="INV2-3", number="0003"))

    async with db.session() as session:
        repository = InvoiceRepository(session)
        everything = await repository.list()
        paid = await repository.list(InvoiceStatus.PAID)
        sent = await repository.list(InvoiceStatus.SENT)

    assert [invoice.number for invoice in everything] == ["0001", "0002", "0003"]
    assert [invoice.number for invoice in paid] == ["0002"]
    assert [invoice.number for invoice in sent] == ["0001", "0003"]


async def test_updating_changes_only_the_fields_given(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        repository = InvoiceRepository(session, clock)
        created = await repository.create(new_invoice())
        updated = await repository.update(
            created.id,
            InvoiceChanges(
                status=InvoiceStatus.PARTIALLY_PAID,
                paypal_status=PayPalInvoiceStatus.PARTIALLY_PAID,
                paid=Decimal("50.00"),
                balance=Decimal("83.50"),
            ),
        )

    assert updated.status is InvoiceStatus.PARTIALLY_PAID
    assert updated.paid == Decimal("50.00")
    assert updated.balance == Decimal("83.50")
    assert updated.total == Decimal("133.50")
    assert updated.number == "0001"
    assert updated.updated_at > created.updated_at


async def test_updating_a_missing_invoice_raises(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(LookupError):
            await InvoiceRepository(session).update(999, InvoiceChanges(number="1"))


async def test_a_session_commits_when_it_finishes_cleanly(db: Database) -> None:
    async with db.session() as session:
        await ClientRepository(session).get_or_create("Dan", "dan@example.com")
    async with db.session() as session:
        assert await ClientRepository(session).find_by_email("dan@example.com") is not None


async def test_a_session_rolls_back_when_something_fails(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.session() as session:
            await ClientRepository(session).get_or_create("Dan", "dan@example.com")
            raise RuntimeError("boom")
    async with db.session() as session:
        assert await ClientRepository(session).find_by_email("dan@example.com") is None


async def test_audit_entries_are_listed_newest_first(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        audit = AuditRepository(session, clock)
        await audit.append(NewAuditEntry(actor=AuditActor.SYSTEM, kind="first"))
        await audit.append(NewAuditEntry(actor=AuditActor.AGENT, kind="second"))
        await audit.append(NewAuditEntry(actor=AuditActor.OWNER, kind="third"))

    async with db.session() as session:
        entries = await AuditRepository(session).list()
        limited = await AuditRepository(session).list(limit=2)

    assert [entry.kind for entry in entries] == ["third", "second", "first"]
    assert [entry.kind for entry in limited] == ["third", "second"]
    assert entries[0].at > entries[2].at


async def test_audit_entries_keep_the_policy_decision_and_detail(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        invoice = await InvoiceRepository(session, clock).create(new_invoice())
        await AuditRepository(session, clock).append(
            NewAuditEntry(
                actor=AuditActor.AGENT,
                kind="action_blocked",
                invoice_id=invoice.id,
                verdict=Verdict.BLOCK,
                rule="plan_too_long",
                policy_version=3,
                detail={"days_out": 90, "nested": {"limit": 60}},
            )
        )

    async with db.session() as session:
        (entry,) = await AuditRepository(session).list(invoice_id=invoice.id)

    assert entry.actor is AuditActor.AGENT
    assert entry.verdict is Verdict.BLOCK
    assert entry.rule == "plan_too_long"
    assert entry.policy_version == 3
    assert entry.detail == {"days_out": 90, "nested": {"limit": 60}}


async def test_audit_can_be_filtered_by_invoice(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        invoices = InvoiceRepository(session, clock)
        first = await invoices.create(new_invoice(paypal_id="INV2-1"))
        second = await invoices.create(new_invoice(paypal_id="INV2-2"))
        audit = AuditRepository(session, clock)
        await audit.append(NewAuditEntry(actor=AuditActor.SYSTEM, kind="a", invoice_id=first.id))
        await audit.append(NewAuditEntry(actor=AuditActor.SYSTEM, kind="b", invoice_id=second.id))
        await audit.append(NewAuditEntry(actor=AuditActor.SYSTEM, kind="c"))

    async with db.session() as session:
        entries = await AuditRepository(session).list(invoice_id=first.id)
    assert [entry.kind for entry in entries] == ["a"]


async def test_naive_datetimes_are_rejected_by_the_database_layer(db: Database) -> None:
    def naive_clock() -> datetime:
        return datetime(2026, 11, 4, 12, 0)

    with pytest.raises(StatementError, match="timezone-aware"):
        async with db.session() as session:
            await InvoiceRepository(session, naive_clock).create(new_invoice())

    async with db.session() as session:
        assert await InvoiceRepository(session).list() == []
