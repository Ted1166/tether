import pytest

from app.db.database import Database
from app.db.repositories import WebhookEventRepository
from app.schemas.records import WebhookEventStatus
from tests.db.conftest import StepClock

pytestmark = pytest.mark.anyio

PAYLOAD = {"id": "WH-1", "event_type": "INVOICING.INVOICE.PAID", "resource": {"id": "INV2-A"}}


async def test_a_new_event_is_recorded_and_should_be_processed(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        record, should_process = await WebhookEventRepository(session, clock).begin(
            "WH-1", "INVOICING.INVOICE.PAID", "INV2-A", PAYLOAD
        )
    assert should_process is True
    assert record.status is WebhookEventStatus.RECEIVED
    assert record.attempts == 1
    assert record.invoice_paypal_id == "INV2-A"
    assert record.payload == PAYLOAD
    assert record.finished_at is None


@pytest.mark.parametrize("final", [WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED])
async def test_a_finished_event_is_not_processed_again(
    db: Database, clock: StepClock, final: WebhookEventStatus
) -> None:
    async with db.session() as session:
        repository = WebhookEventRepository(session, clock)
        await repository.begin("WH-1", "T", None, PAYLOAD)
        await repository.finish("WH-1", final, "done")

    async with db.session() as session:
        record, should_process = await WebhookEventRepository(session, clock).begin(
            "WH-1", "T", None, PAYLOAD
        )
    assert should_process is False
    assert record.status is final
    assert record.attempts == 1


async def test_a_failed_event_is_retried_and_counted(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        repository = WebhookEventRepository(session, clock)
        await repository.begin("WH-1", "T", "INV2-A", PAYLOAD)
        await repository.finish("WH-1", WebhookEventStatus.FAILED, "PayPal was down")

    async with db.session() as session:
        record, should_process = await WebhookEventRepository(session, clock).begin(
            "WH-1", "T", "INV2-A", PAYLOAD
        )
    assert should_process is True
    assert record.attempts == 2
    assert record.status is WebhookEventStatus.RECEIVED


async def test_an_event_stuck_in_received_is_picked_up_again(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        await WebhookEventRepository(session, clock).begin("WH-1", "T", None, PAYLOAD)
    async with db.session() as session:
        record, should_process = await WebhookEventRepository(session, clock).begin(
            "WH-1", "T", None, PAYLOAD
        )
    assert should_process is True
    assert record.attempts == 2


async def test_finishing_records_the_note_and_time(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        repository = WebhookEventRepository(session, clock)
        await repository.begin("WH-1", "T", None, PAYLOAD)
        finished = await repository.finish("WH-1", WebhookEventStatus.PROCESSED, "created")
    assert finished.note == "created"
    assert finished.finished_at is not None
    assert finished.finished_at > finished.received_at


async def test_finishing_an_unknown_event_raises(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(LookupError):
            await WebhookEventRepository(session).finish("WH-NOPE", WebhookEventStatus.PROCESSED)


async def test_events_are_listed_newest_first(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        repository = WebhookEventRepository(session, clock)
        for number in (1, 2, 3):
            await repository.begin(f"WH-{number}", "T", None, PAYLOAD)
    async with db.session() as session:
        listed = await WebhookEventRepository(session).list()
        limited = await WebhookEventRepository(session).list(limit=2)
        found = await WebhookEventRepository(session).get("WH-2")
        missing = await WebhookEventRepository(session).get("WH-9")
    assert [event.paypal_event_id for event in listed] == ["WH-3", "WH-2", "WH-1"]
    assert len(limited) == 2
    assert found is not None
    assert missing is None
