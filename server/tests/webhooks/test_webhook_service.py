import pytest
from sqlalchemy.exc import IntegrityError

from app.db.database import Database
from app.db.repositories import (
    AuditRepository,
    InvoiceRepository,
    WebhookEventRepository,
)
from app.paypal.errors import PayPalUnavailable
from app.schemas.actions import InvoiceStatus
from app.schemas.records import WebhookEventStatus
from app.services.invoice_sync import InvoiceSyncService
from app.services.webhook_service import InvalidWebhook, WebhookOutcome, WebhookService
from tests.db.conftest import StepClock
from tests.webhooks.fakes import (
    INVOICE_ID,
    PAID,
    TODAY,
    FakeInvoices,
    FakeVerifier,
    event_body,
    remote,
    signed_headers,
)

pytestmark = pytest.mark.anyio


def make_service(
    db: Database,
    clock: StepClock,
    invoices: FakeInvoices,
    verifier: FakeVerifier | None = None,
) -> WebhookService:
    return WebhookService(
        db, invoices, verifier or FakeVerifier(), today=lambda: TODAY, clock=clock
    )


async def invoice_status(db: Database) -> InvoiceStatus | None:
    async with db.session() as session:
        record = await InvoiceRepository(session).get_by_paypal_id(INVOICE_ID)
    return record.status if record else None


async def event_record(db: Database, event_id: str = "WH-1"):  # type: ignore[no-untyped-def]
    async with db.session() as session:
        return await WebhookEventRepository(session).get(event_id)


async def test_a_paid_event_creates_the_invoice_as_paid(db: Database, clock: StepClock) -> None:
    service = make_service(db, clock, FakeInvoices(**{INVOICE_ID: remote(**PAID)}))
    result = await service.handle(event_body(), signed_headers())

    assert result.outcome is WebhookOutcome.PROCESSED
    assert result.invoice is not None
    assert result.invoice.status is InvoiceStatus.PAID
    assert await invoice_status(db) is InvoiceStatus.PAID
    record = await event_record(db)
    assert record is not None
    assert record.status is WebhookEventStatus.PROCESSED
    assert record.note == "created"


async def test_a_paid_event_moves_a_sent_invoice_to_paid_and_is_audited(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote()})
    service = make_service(db, clock, invoices)
    await service.handle(event_body("WH-SENT", "INVOICING.INVOICE.CREATED"), signed_headers())
    assert await invoice_status(db) is InvoiceStatus.SENT

    invoices.set(remote(**PAID))
    result = await service.handle(event_body("WH-PAID"), signed_headers())

    assert result.note == "updated"
    assert await invoice_status(db) is InvoiceStatus.PAID
    async with db.session() as session:
        entries = await AuditRepository(session).list(limit=1)
    assert entries[0].kind == "invoice_status_changed"
    assert entries[0].detail["source"] == "webhook"
    assert (entries[0].detail["from"], entries[0].detail["to"]) == ("sent", "paid")


async def test_a_repeated_delivery_is_acknowledged_without_reprocessing(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    service = make_service(db, clock, invoices)
    first = await service.handle(event_body(), signed_headers())
    second = await service.handle(event_body(), signed_headers())

    assert first.outcome is WebhookOutcome.PROCESSED
    assert second.outcome is WebhookOutcome.DUPLICATE
    assert invoices.requested == [INVOICE_ID]


async def test_a_late_event_cannot_undo_what_paypal_already_says(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    service = make_service(db, clock, invoices)
    await service.handle(event_body("WH-PAID"), signed_headers())
    late = await service.handle(event_body("WH-OLD", "INVOICING.INVOICE.UPDATED"), signed_headers())

    assert late.outcome is WebhookOutcome.PROCESSED
    assert late.note == "unchanged"
    assert await invoice_status(db) is InvoiceStatus.PAID


async def test_a_stale_paypal_answer_is_rejected_and_the_payment_survives(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    service = make_service(db, clock, invoices)
    await service.handle(event_body("WH-PAID"), signed_headers())

    invoices.set(remote())
    result = await service.handle(
        event_body("WH-STALE", "INVOICING.INVOICE.UPDATED"), signed_headers()
    )

    assert result.note == "rejected"
    assert await invoice_status(db) is InvoiceStatus.PAID
    async with db.session() as session:
        entries = await AuditRepository(session).list(limit=1)
    assert entries[0].kind == "transition_rejected"


@pytest.mark.parametrize("shape", ["flat", "nested", "links"])
async def test_every_payload_shape_finds_its_invoice(
    db: Database, clock: StepClock, shape: str
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    service = make_service(db, clock, invoices)
    result = await service.handle(event_body(shape=shape), signed_headers())
    assert result.outcome is WebhookOutcome.PROCESSED
    assert invoices.requested == [INVOICE_ID]


async def test_events_for_other_resources_are_recorded_and_ignored(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices()
    service = make_service(db, clock, invoices)
    body = event_body(event_type="PAYMENT.CAPTURE.COMPLETED")
    result = await service.handle(body, signed_headers())

    assert result.outcome is WebhookOutcome.IGNORED
    assert invoices.requested == []
    record = await event_record(db)
    assert record is not None
    assert record.status is WebhookEventStatus.IGNORED


async def test_an_invoice_event_without_an_invoice_is_ignored(
    db: Database, clock: StepClock
) -> None:
    result = await make_service(db, clock, FakeInvoices()).handle(
        event_body(invoice_id=None), signed_headers()
    )
    assert result.outcome is WebhookOutcome.IGNORED


async def test_an_invoice_from_another_account_is_ignored(db: Database, clock: StepClock) -> None:
    result = await make_service(db, clock, FakeInvoices()).handle(event_body(), signed_headers())

    assert result.outcome is WebhookOutcome.IGNORED
    assert result.note is not None
    assert "does not exist" in result.note
    assert await invoice_status(db) is None


async def test_a_bad_signature_is_refused_and_nothing_is_recorded(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    service = make_service(db, clock, invoices, FakeVerifier(result=False))
    with pytest.raises(InvalidWebhook, match="signature"):
        await service.handle(event_body(), signed_headers())

    assert invoices.requested == []
    assert await event_record(db) is None


async def test_the_verifier_receives_the_exact_bytes_that_arrived(
    db: Database, clock: StepClock
) -> None:
    verifier = FakeVerifier()
    service = make_service(db, clock, FakeInvoices(**{INVOICE_ID: remote(**PAID)}), verifier)
    body = event_body()
    await service.handle(body, signed_headers())
    assert verifier.calls[0][1] == body
    assert verifier.calls[0][0].transmission_id == "69cd13f0-d67a-11e5-baa3-778b53f4ae55"


async def test_missing_signature_headers_are_refused(db: Database, clock: StepClock) -> None:
    verifier = FakeVerifier()
    service = make_service(db, clock, FakeInvoices(), verifier)
    with pytest.raises(InvalidWebhook, match="missing webhook headers"):
        await service.handle(event_body(), {})
    assert verifier.calls == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"event_type": "X"}', b'{"id": "A"}'])
async def test_malformed_events_are_refused(db: Database, clock: StepClock, body: bytes) -> None:
    with pytest.raises(InvalidWebhook, match="malformed"):
        await make_service(db, clock, FakeInvoices()).handle(body, signed_headers())


async def test_a_paypal_outage_fails_the_event_and_a_retry_recovers(
    db: Database, clock: StepClock
) -> None:
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    invoices.failure = PayPalUnavailable("PayPal is down", status=503)
    service = make_service(db, clock, invoices)

    with pytest.raises(PayPalUnavailable):
        await service.handle(event_body(), signed_headers())
    failed = await event_record(db)
    assert failed is not None
    assert failed.status is WebhookEventStatus.FAILED
    assert failed.note is not None
    assert "PayPal is down" in failed.note
    assert await invoice_status(db) is None

    invoices.failure = None
    retried = await service.handle(event_body(), signed_headers())
    assert retried.outcome is WebhookOutcome.PROCESSED
    recovered = await event_record(db)
    assert recovered is not None
    assert recovered.status is WebhookEventStatus.PROCESSED
    assert recovered.attempts == 2
    assert await invoice_status(db) is InvoiceStatus.PAID


async def test_a_sync_failure_rolls_back_and_marks_the_event_failed(
    db: Database, clock: StepClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(InvoiceSyncService, "apply", explode)
    service = make_service(db, clock, FakeInvoices(**{INVOICE_ID: remote(**PAID)}))

    with pytest.raises(RuntimeError):
        await service.handle(event_body(), signed_headers())

    record = await event_record(db)
    assert record is not None
    assert record.status is WebhookEventStatus.FAILED
    assert record.note == "database exploded"
    assert await invoice_status(db) is None


async def test_a_concurrent_duplicate_is_treated_as_a_duplicate(
    db: Database, clock: StepClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def collide(*args: object, **kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(WebhookEventRepository, "begin", collide)
    invoices = FakeInvoices(**{INVOICE_ID: remote(**PAID)})
    result = await make_service(db, clock, invoices).handle(event_body(), signed_headers())

    assert result.outcome is WebhookOutcome.DUPLICATE
    assert invoices.requested == []
