from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.database import Database
from app.db.repositories import PendingActionRepository, PolicyRepository
from app.schemas.actions import ActionKind, Verdict
from app.schemas.policy import ContactPolicy, Policy
from app.schemas.records import NewPendingAction, PendingActionChanges, PendingActionStatus
from tests.db.conftest import StepClock

pytestmark = pytest.mark.anyio


def new_action(**overrides: object) -> NewPendingAction:
    fields: dict[str, object] = {
        "kind": ActionKind.CREATE_INVOICE,
        "status": PendingActionStatus.PENDING,
        "client_email": "dan@example.com",
        "amount": Decimal("133.50"),
        "currency": "USD",
        "summary": "Invoice USD 133.50 to Dan",
        "payload": {"draft": {"client_name": "Dan"}, "send": True},
        "decisions": [{"verdict": "needs_approval", "rule": "approval_required"}],
        "verdict": Verdict.NEEDS_APPROVAL,
        "rule": "approval_required",
        "reason": "send_invoice always needs your approval",
        "policy_version": 2,
    }
    fields.update(overrides)
    return NewPendingAction(**fields)  # type: ignore[arg-type]


async def test_an_action_round_trips_with_exact_money_and_json(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        created = await PendingActionRepository(session, clock).create(new_action())
    async with db.session() as session:
        fetched = await PendingActionRepository(session).get(created.id)

    assert fetched is not None
    assert fetched.amount == Decimal("133.50")
    assert fetched.status is PendingActionStatus.PENDING
    assert fetched.verdict is Verdict.NEEDS_APPROVAL
    assert fetched.payload == {"draft": {"client_name": "Dan"}, "send": True}
    assert fetched.decisions == [{"verdict": "needs_approval", "rule": "approval_required"}]
    assert fetched.result == {}
    assert fetched.created_at == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert fetched.decided_at is None
    assert fetched.error is None


async def test_a_missing_action_returns_nothing(db: Database) -> None:
    async with db.session() as session:
        assert await PendingActionRepository(session).get(404) is None


async def test_actions_are_listed_newest_first_and_can_be_filtered(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        repository = PendingActionRepository(session, clock)
        await repository.create(new_action(summary="first"))
        await repository.create(new_action(summary="second", status=PendingActionStatus.BLOCKED))
        await repository.create(new_action(summary="third"))

    async with db.session() as session:
        repository = PendingActionRepository(session)
        everything = await repository.list()
        pending = await repository.list(PendingActionStatus.PENDING)
        limited = await repository.list(limit=1)

    assert [a.summary for a in everything] == ["third", "second", "first"]
    assert [a.summary for a in pending] == ["third", "first"]
    assert [a.summary for a in limited] == ["third"]


async def test_updates_change_only_the_fields_given(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        repository = PendingActionRepository(session, clock)
        created = await repository.create(new_action())
        decided = await repository.update(
            created.id, PendingActionChanges(decided_at=datetime(2026, 11, 5, tzinfo=UTC))
        )
        executed = await repository.update(
            created.id,
            PendingActionChanges(
                status=PendingActionStatus.EXECUTED,
                result={"paypal_id": "INV2-A"},
                executed_at=datetime(2026, 11, 6, tzinfo=UTC),
            ),
        )

    assert decided.status is PendingActionStatus.PENDING
    assert decided.decided_at == datetime(2026, 11, 5, tzinfo=UTC)
    assert executed.status is PendingActionStatus.EXECUTED
    assert executed.result == {"paypal_id": "INV2-A"}
    assert executed.decided_at == datetime(2026, 11, 5, tzinfo=UTC)
    assert executed.summary == "Invoice USD 133.50 to Dan"


async def test_an_error_can_be_set_and_then_cleared(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        repository = PendingActionRepository(session, clock)
        created = await repository.create(new_action())
        failed = await repository.update(created.id, PendingActionChanges(error="PayPal was down"))
        cleared = await repository.update(created.id, PendingActionChanges(error=None))
    assert failed.error == "PayPal was down"
    assert cleared.error is None


async def test_updating_a_missing_action_raises(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(LookupError):
            await PendingActionRepository(session).update(9, PendingActionChanges(error="x"))


async def test_there_is_no_policy_until_one_is_saved(db: Database) -> None:
    async with db.session() as session:
        assert await PolicyRepository(session).current() is None


async def test_saving_assigns_increasing_versions_and_current_is_the_latest(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        repository = PolicyRepository(session, clock)
        first = await repository.save(Policy(version=99))
        second = await repository.save(Policy(currency="EUR"))

    async with db.session() as session:
        current = await PolicyRepository(session).current()

    assert (first.version, second.version) == (1, 2)
    assert current is not None
    assert current.version == 2
    assert current.currency == "EUR"


async def test_a_stored_policy_reads_back_identical(db: Database, clock: StepClock) -> None:
    policy = Policy(
        contacts=ContactPolicy(allowed=frozenset({"dan@example.com", "ruth@example.com"}))
    )
    async with db.session() as session:
        saved = await PolicyRepository(session, clock).save(policy)
    async with db.session() as session:
        current = await PolicyRepository(session).current()
    assert current == saved
    assert current is not None
    assert current.contacts.allowed == frozenset({"dan@example.com", "ruth@example.com"})
