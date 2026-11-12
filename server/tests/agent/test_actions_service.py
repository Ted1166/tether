from datetime import UTC, datetime

import pytest

from app.db.database import Database
from app.db.repositories import (
    AuditRepository,
    ClientRepository,
    InvoiceRepository,
    PolicyRepository,
)
from app.paypal.errors import PayPalUnavailable
from app.schemas.actions import ActionKind, InvoiceStatus, Verdict
from app.schemas.policy import AmountPolicy, ContactPolicy
from app.schemas.records import AuditActor, PendingActionStatus
from app.services.actions import (
    ActionBlocked,
    ActionFailed,
    ActionNotDecidable,
    ActionNotFound,
)
from app.services.invoice_executor import PayPalInvoiceExecutor
from tests.agent.fakes import (
    DAN,
    TODAY,
    Rig,
    base_policy,
    proposal,
    set_policy,
    trusting_policy,
)
from tests.db.conftest import StepClock

pytestmark = pytest.mark.anyio


async def audit_kinds(db: Database) -> list[str]:
    async with db.session() as session:
        return [entry.kind for entry in reversed(await AuditRepository(session).list())]


async def ledger(db: Database) -> list:  # type: ignore[type-arg]
    async with db.session() as session:
        return await InvoiceRepository(session).list()


async def test_sending_needs_approval_under_the_default_policy(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    record = await rig.service.propose_invoice(proposal("400"))

    assert record.status is PendingActionStatus.PENDING
    assert record.verdict is Verdict.NEEDS_APPROVAL
    assert record.kind is ActionKind.CREATE_INVOICE
    assert record.policy_version == 1
    assert len(record.decisions) == 2
    assert rig.executor.calls == []
    assert await ledger(db) == []

    async with db.session() as session:
        (entry,) = await AuditRepository(session).list()
    assert entry.kind == "action_proposed"
    assert entry.actor is AuditActor.AGENT
    assert entry.verdict is Verdict.NEEDS_APPROVAL
    assert entry.policy_version == 1
    assert entry.detail["action_id"] == record.id


async def test_a_small_draft_for_a_known_client_runs_without_asking(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    record = await rig.service.propose_invoice(proposal("120", send=False))

    assert record.status is PendingActionStatus.EXECUTED
    assert record.verdict is Verdict.ALLOW
    assert record.invoice_id is not None
    assert rig.executor.calls[0]["send"] is False
    (invoice,) = await ledger(db)
    assert invoice.status is InvoiceStatus.DRAFT
    assert invoice.client is not None
    assert invoice.client.email == DAN
    assert await audit_kinds(db) == [
        "action_proposed",
        "invoice_imported",
        "action_executed",
    ]


async def test_a_trusting_policy_lets_a_small_send_run_without_asking(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, trusting_policy())
    record = await rig.service.propose_invoice(proposal("120"))

    assert record.status is PendingActionStatus.EXECUTED
    assert record.result["sent"] is True
    (invoice,) = await ledger(db)
    assert invoice.status is InvoiceStatus.SENT


async def test_a_large_invoice_still_needs_approval_even_under_a_trusting_policy(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, trusting_policy())
    record = await rig.service.propose_invoice(proposal("400"))
    assert record.status is PendingActionStatus.PENDING
    assert record.rule == "amount_needs_approval"
    assert rig.executor.calls == []


async def test_quiet_hours_pull_an_automatic_send_back_to_approval(
    db: Database, clock: StepClock
) -> None:
    clock.now = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
    rig = Rig(db, clock)
    await set_policy(db, trusting_policy())
    record = await rig.service.propose_invoice(proposal("120"))
    assert record.status is PendingActionStatus.PENDING
    assert record.rule == "quiet_hours"


async def test_an_unknown_contact_is_blocked_and_nothing_happens(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    record = await rig.service.propose_invoice(proposal("120", email="stranger@example.com"))

    assert record.status is PendingActionStatus.BLOCKED
    assert record.verdict is Verdict.BLOCK
    assert record.rule == "contact_not_allowed"
    assert rig.executor.calls == []
    assert await ledger(db) == []
    async with db.session() as session:
        (entry,) = await AuditRepository(session).list()
    assert entry.kind == "action_blocked"
    assert entry.rule == "contact_not_allowed"


async def test_an_amount_over_the_hard_cap_is_blocked(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, trusting_policy())
    record = await rig.service.propose_invoice(proposal("9999999"))
    assert record.status is PendingActionStatus.BLOCKED
    assert record.rule == "amount_over_cap"
    assert rig.executor.calls == []


async def test_approving_executes_the_exact_stored_proposal(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    done = await rig.service.approve(pending.id)

    assert done.status is PendingActionStatus.EXECUTED
    assert done.decided_at is not None
    assert done.executed_at is not None
    assert done.result["number"] == "0001"
    assert done.invoice_id is not None
    call = rig.executor.calls[0]
    assert call["action_id"] == pending.id
    assert call["send"] is True
    assert call["draft"].total == 400
    assert call["draft"].client_email == DAN
    (invoice,) = await ledger(db)
    assert invoice.status is InvoiceStatus.SENT
    assert await audit_kinds(db) == [
        "action_proposed",
        "action_approved",
        "invoice_imported",
        "action_executed",
    ]


async def test_the_audit_trail_names_who_did_what(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    await rig.service.approve(pending.id)
    async with db.session() as session:
        entries = list(reversed(await AuditRepository(session).list()))
    assert [(e.kind, e.actor) for e in entries] == [
        ("action_proposed", AuditActor.AGENT),
        ("action_approved", AuditActor.OWNER),
        ("invoice_imported", AuditActor.AGENT),
        ("action_executed", AuditActor.OWNER),
    ]
    assert entries[3].detail["paypal_id"].startswith("INV2-FAKE-")


async def test_an_action_cannot_be_approved_twice(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    await rig.service.approve(pending.id)
    with pytest.raises(ActionNotDecidable, match="executed"):
        await rig.service.approve(pending.id)
    assert rig.executor.create_count == 1


async def test_approving_an_unknown_action_raises(db: Database, clock: StepClock) -> None:
    with pytest.raises(ActionNotFound):
        await Rig(db, clock).service.approve(404)


async def test_a_blocked_action_cannot_be_approved(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    blocked = await rig.service.propose_invoice(proposal("120", email="stranger@example.com"))
    with pytest.raises(ActionNotDecidable, match="blocked"):
        await rig.service.approve(blocked.id)
    assert rig.executor.calls == []


async def test_rejecting_records_the_reason_and_ends_the_action(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    rejected = await rig.service.reject(pending.id, "wrong amount")

    assert rejected.status is PendingActionStatus.REJECTED
    assert rejected.error == "wrong amount"
    with pytest.raises(ActionNotDecidable):
        await rig.service.approve(pending.id)
    with pytest.raises(ActionNotDecidable):
        await rig.service.reject(pending.id)
    assert rig.executor.calls == []
    async with db.session() as session:
        entry = (await AuditRepository(session).list(limit=1))[0]
    assert entry.kind == "action_rejected"
    assert entry.actor is AuditActor.OWNER
    assert entry.detail["reason"] == "wrong amount"


async def test_rejecting_an_unknown_action_raises(db: Database, clock: StepClock) -> None:
    with pytest.raises(ActionNotFound):
        await Rig(db, clock).service.reject(404)


async def test_a_policy_change_after_the_proposal_is_respected_at_approval(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))

    tightened = base_policy(amounts=AmountPolicy(auto_approve_below=100, hard_cap=300))
    await set_policy(db, tightened)

    with pytest.raises(ActionBlocked) as caught:
        await rig.service.approve(pending.id)
    assert caught.value.decision.rule == "amount_over_cap"
    assert rig.executor.calls == []

    blocked = await rig.service.get(pending.id)
    assert blocked.status is PendingActionStatus.BLOCKED
    assert blocked.rule == "amount_over_cap"
    assert blocked.policy_version == 2
    assert (await audit_kinds(db))[-1] == "action_blocked"
    assert await ledger(db) == []


async def test_removing_a_contact_after_the_proposal_blocks_the_approval(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    await set_policy(
        db, base_policy(contacts=ContactPolicy(allowed=frozenset({"maya@example.com"})))
    )
    with pytest.raises(ActionBlocked, match="approved contact list"):
        await rig.service.approve(pending.id)
    assert rig.executor.calls == []


async def test_a_paypal_failure_marks_the_action_failed_and_a_retry_succeeds(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    rig.executor.fail_next("create", PayPalUnavailable("PayPal is down", status=503))

    with pytest.raises(ActionFailed, match="PayPal is down"):
        await rig.service.approve(pending.id)
    failed = await rig.service.get(pending.id)
    assert failed.status is PendingActionStatus.FAILED
    assert failed.error is not None
    assert "PayPal is down" in failed.error
    assert await ledger(db) == []

    done = await rig.service.approve(pending.id)
    assert done.status is PendingActionStatus.EXECUTED
    assert done.error is None
    assert (await audit_kinds(db)).count("action_failed") == 1


async def test_a_retry_after_a_failed_send_does_not_create_a_second_invoice(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    rig.executor.fail_next("send", PayPalUnavailable("send failed", status=503))

    with pytest.raises(ActionFailed):
        await rig.service.approve(pending.id)
    partial = await rig.service.get(pending.id)
    assert partial.status is PendingActionStatus.FAILED
    assert partial.result["paypal_id"].startswith("INV2-FAKE-")

    done = await rig.service.approve(pending.id)
    assert done.status is PendingActionStatus.EXECUTED
    assert rig.executor.create_count == 1
    assert rig.executor.calls[1]["resume"] == partial.result["paypal_id"]
    (invoice,) = await ledger(db)
    assert invoice.status is InvoiceStatus.SENT


async def test_an_unexpected_error_marks_the_action_failed_and_is_raised(
    db: Database, clock: StepClock
) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    rig.executor.fail_next("create", RuntimeError("disk on fire"))

    with pytest.raises(RuntimeError, match="disk on fire"):
        await rig.service.approve(pending.id)
    assert (await rig.service.get(pending.id)).status is PendingActionStatus.FAILED


async def test_a_failed_action_can_still_be_rejected(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    pending = await rig.service.propose_invoice(proposal("400"))
    rig.executor.fail_next("create", PayPalUnavailable("down", status=503))
    with pytest.raises(ActionFailed):
        await rig.service.approve(pending.id)
    rejected = await rig.service.reject(pending.id, "giving up")
    assert rejected.status is PendingActionStatus.REJECTED


async def test_the_first_policy_is_created_from_existing_clients(
    db: Database, clock: StepClock
) -> None:
    async with db.session() as session:
        await ClientRepository(session, clock).get_or_create("Dan Okoro", DAN)
    rig = Rig(db, clock)
    record = await rig.service.propose_invoice(proposal("120", send=False))

    assert record.status is PendingActionStatus.EXECUTED
    async with db.session() as session:
        policy = await PolicyRepository(session).current()
    assert policy is not None
    assert policy.contacts.allowed == frozenset({DAN})
    assert "policy_created" in await audit_kinds(db)


async def test_with_no_clients_the_first_policy_blocks_everyone(
    db: Database, clock: StepClock
) -> None:
    record = await Rig(db, clock).service.propose_invoice(proposal("120"))
    assert record.status is PendingActionStatus.BLOCKED
    assert record.rule == "contact_not_allowed"


async def test_known_clients_are_listed(db: Database, clock: StepClock) -> None:
    async with db.session() as session:
        clients = ClientRepository(session, clock)
        await clients.get_or_create("Dan Okoro", DAN)
        await clients.get_or_create("Maya", "maya@example.com")
    known = await Rig(db, clock).service.known_clients()
    assert [c.email for c in known] == [DAN, "maya@example.com"]


async def test_actions_can_be_listed_by_status(db: Database, clock: StepClock) -> None:
    rig = Rig(db, clock)
    await set_policy(db, base_policy())
    await rig.service.propose_invoice(proposal("400"))
    await rig.service.propose_invoice(proposal("120", email="stranger@example.com"))
    pending = await rig.service.list_actions(PendingActionStatus.PENDING)
    blocked = await rig.service.list_actions(PendingActionStatus.BLOCKED)
    everything = await rig.service.list_actions()
    assert (len(pending), len(blocked), len(everything)) == (1, 1, 2)


class FakeGateway:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []

    async def create_draft(self, draft: object, *, issue_date: object, request_id: str) -> str:
        self.created.append({"draft": draft, "issue_date": issue_date, "request_id": request_id})
        return "INV2-REAL-1"

    async def send(self, invoice_id: str, *, request_id: str) -> None:
        self.sent.append({"invoice_id": invoice_id, "request_id": request_id})


async def remember(_: str) -> None:
    return None


async def test_the_paypal_executor_tags_the_invoice_and_uses_stable_request_ids() -> None:
    gateway = FakeGateway()
    executor = PayPalInvoiceExecutor(gateway, today=lambda: TODAY)  # type: ignore[arg-type]
    created: list[str] = []

    async def on_created(paypal_id: str) -> None:
        created.append(paypal_id)

    result = await executor.create_and_send(
        proposal().draft, send=True, action_id=7, resume_paypal_id=None, on_created=on_created
    )
    assert result == "INV2-REAL-1"
    assert created == ["INV2-REAL-1"]
    assert gateway.created[0]["request_id"] == "tether-7-create"
    assert gateway.created[0]["issue_date"] == TODAY
    assert gateway.created[0]["draft"].reference == "tether-action-7"  # type: ignore[attr-defined]
    assert gateway.sent == [{"invoice_id": "INV2-REAL-1", "request_id": "tether-7-send"}]


async def test_the_paypal_executor_resumes_without_creating_again() -> None:
    gateway = FakeGateway()
    executor = PayPalInvoiceExecutor(gateway, today=lambda: TODAY)  # type: ignore[arg-type]
    result = await executor.create_and_send(
        proposal().draft, send=True, action_id=7, resume_paypal_id="INV2-OLD", on_created=remember
    )
    assert result == "INV2-OLD"
    assert gateway.created == []
    assert gateway.sent[0]["invoice_id"] == "INV2-OLD"


async def test_the_paypal_executor_can_leave_the_invoice_as_a_draft() -> None:
    gateway = FakeGateway()
    executor = PayPalInvoiceExecutor(gateway, today=lambda: TODAY)  # type: ignore[arg-type]
    await executor.create_and_send(
        proposal().draft, send=False, action_id=8, resume_paypal_id=None, on_created=remember
    )
    assert len(gateway.created) == 1
    assert gateway.sent == []
