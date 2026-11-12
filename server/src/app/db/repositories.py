from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditRow,
    ClientRow,
    InvoiceRow,
    PendingActionRow,
    PolicyRow,
    WebhookEventRow,
)
from app.lib.money import from_cents, to_cents
from app.lib.time import utc_now
from app.schemas.actions import InvoiceStatus
from app.schemas.policy import Policy
from app.schemas.records import (
    AuditRecord,
    ClientRecord,
    InvoiceChanges,
    InvoiceRecord,
    NewAuditEntry,
    NewInvoice,
    NewPendingAction,
    PendingActionChanges,
    PendingActionRecord,
    PendingActionStatus,
    WebhookEventRecord,
    WebhookEventStatus,
)

Clock = Callable[[], datetime]
MONEY_FIELDS = ("paid", "balance")
FINISHED_EVENT_STATUSES = frozenset({WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED})


class DuplicateInvoice(Exception):
    def __init__(self, paypal_id: str) -> None:
        super().__init__(f"an invoice with PayPal id {paypal_id} already exists")
        self.paypal_id = paypal_id


class ClientRepository:
    def __init__(self, session: AsyncSession, clock: Clock = utc_now) -> None:
        self._session = session
        self._clock = clock

    async def find_by_email(self, email: str) -> ClientRecord | None:
        row = await self._row_by_email(email)
        return _client_record(row) if row else None

    async def get_or_create(self, name: str, email: str) -> ClientRecord:
        existing = await self._row_by_email(email)
        if existing is not None:
            return _client_record(existing)
        row = ClientRow(email=email.strip().lower(), name=name.strip(), created_at=self._clock())
        self._session.add(row)
        await self._session.flush()
        return _client_record(row)

    async def all(self) -> list[ClientRecord]:
        rows = (await self._session.execute(select(ClientRow).order_by(ClientRow.id))).scalars()
        return [_client_record(row) for row in rows]

    async def _row_by_email(self, email: str) -> ClientRow | None:
        query = select(ClientRow).where(ClientRow.email == email.strip().lower())
        return (await self._session.execute(query)).scalar_one_or_none()


class InvoiceRepository:
    def __init__(self, session: AsyncSession, clock: Clock = utc_now) -> None:
        self._session = session
        self._clock = clock

    async def get(self, invoice_id: int) -> InvoiceRecord | None:
        row = await self._fetch(InvoiceRow.id == invoice_id)
        return _invoice_record(row) if row else None

    async def get_by_paypal_id(self, paypal_id: str) -> InvoiceRecord | None:
        row = await self._fetch(InvoiceRow.paypal_id == paypal_id)
        return _invoice_record(row) if row else None

    async def list(self, status: InvoiceStatus | None = None) -> list[InvoiceRecord]:
        query = select(InvoiceRow).order_by(InvoiceRow.id)
        if status is not None:
            query = query.where(InvoiceRow.status == status)
        rows = (await self._session.execute(query)).scalars().unique().all()
        return [_invoice_record(row) for row in rows]

    async def create(self, new: NewInvoice) -> InvoiceRecord:
        if new.paypal_id and await self.get_by_paypal_id(new.paypal_id):
            raise DuplicateInvoice(new.paypal_id)

        now = self._clock()
        row = InvoiceRow(
            paypal_id=new.paypal_id,
            reference=new.reference,
            number=new.number,
            client_id=new.client_id,
            status=new.status,
            paypal_status=new.paypal_status,
            currency=new.currency,
            total_cents=to_cents(new.total),
            paid_cents=to_cents(new.paid),
            balance_cents=to_cents(new.balance),
            issue_date=new.issue_date,
            due_date=new.due_date,
            payer_view_url=new.payer_view_url,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        loaded = await self._fetch(InvoiceRow.id == row.id)
        assert loaded is not None
        return _invoice_record(loaded)

    async def update(self, invoice_id: int, changes: InvoiceChanges) -> InvoiceRecord:
        row = await self._fetch(InvoiceRow.id == invoice_id)
        if row is None:
            raise LookupError(f"invoice {invoice_id} does not exist")

        data = changes.model_dump(exclude_unset=True)
        for field in MONEY_FIELDS:
            if field in data:
                setattr(row, f"{field}_cents", to_cents(data.pop(field)))
        for field, value in data.items():
            setattr(row, field, value)
        row.updated_at = self._clock()
        await self._session.flush()
        return _invoice_record(row)

    async def _fetch(self, condition: object) -> InvoiceRow | None:
        query = (
            select(InvoiceRow)
            .where(condition)  # type: ignore[arg-type]
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(query)).unique().scalar_one_or_none()


class AuditRepository:
    def __init__(self, session: AsyncSession, clock: Clock = utc_now) -> None:
        self._session = session
        self._clock = clock

    async def append(self, entry: NewAuditEntry) -> AuditRecord:
        row = AuditRow(
            at=self._clock(),
            actor=entry.actor,
            kind=entry.kind,
            invoice_id=entry.invoice_id,
            verdict=entry.verdict,
            rule=entry.rule,
            policy_version=entry.policy_version,
            detail=entry.detail,
        )
        self._session.add(row)
        await self._session.flush()
        return _audit_record(row)

    async def list(self, invoice_id: int | None = None, limit: int = 100) -> list[AuditRecord]:
        query = select(AuditRow).order_by(AuditRow.id.desc()).limit(limit)
        if invoice_id is not None:
            query = query.where(AuditRow.invoice_id == invoice_id)
        rows = (await self._session.execute(query)).scalars().all()
        return [_audit_record(row) for row in rows]


class WebhookEventRepository:
    def __init__(self, session: AsyncSession, clock: Clock = utc_now) -> None:
        self._session = session
        self._clock = clock

    async def begin(
        self,
        paypal_event_id: str,
        event_type: str,
        invoice_paypal_id: str | None,
        payload: dict[str, Any],
    ) -> tuple[WebhookEventRecord, bool]:
        row = await self._row(paypal_event_id)
        if row is None:
            row = WebhookEventRow(
                paypal_event_id=paypal_event_id,
                event_type=event_type,
                invoice_paypal_id=invoice_paypal_id,
                status=WebhookEventStatus.RECEIVED,
                attempts=1,
                payload=payload,
                received_at=self._clock(),
            )
            self._session.add(row)
            await self._session.flush()
            return _event_record(row), True

        if row.status in FINISHED_EVENT_STATUSES:
            return _event_record(row), False

        row.attempts += 1
        row.status = WebhookEventStatus.RECEIVED
        await self._session.flush()
        return _event_record(row), True

    async def finish(
        self, paypal_event_id: str, status: WebhookEventStatus, note: str | None = None
    ) -> WebhookEventRecord:
        row = await self._row(paypal_event_id)
        if row is None:
            raise LookupError(f"webhook event {paypal_event_id} was never recorded")
        row.status = status
        row.note = note
        row.finished_at = self._clock()
        await self._session.flush()
        return _event_record(row)

    async def get(self, paypal_event_id: str) -> WebhookEventRecord | None:
        row = await self._row(paypal_event_id)
        return _event_record(row) if row else None

    async def list(self, limit: int = 50) -> list[WebhookEventRecord]:
        query = select(WebhookEventRow).order_by(WebhookEventRow.id.desc()).limit(limit)
        rows = (await self._session.execute(query)).scalars().all()
        return [_event_record(row) for row in rows]

    async def _row(self, paypal_event_id: str) -> WebhookEventRow | None:
        query = (
            select(WebhookEventRow)
            .where(WebhookEventRow.paypal_event_id == paypal_event_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(query)).scalar_one_or_none()


class PendingActionRepository:
    def __init__(self, session: AsyncSession, clock: Clock = utc_now) -> None:
        self._session = session
        self._clock = clock

    async def create(self, new: NewPendingAction) -> PendingActionRecord:
        row = PendingActionRow(
            kind=new.kind,
            status=new.status,
            client_email=new.client_email,
            amount_cents=to_cents(new.amount),
            currency=new.currency,
            summary=new.summary,
            payload=new.payload,
            decisions=new.decisions,
            verdict=new.verdict,
            rule=new.rule,
            reason=new.reason,
            policy_version=new.policy_version,
            result={},
            created_at=self._clock(),
        )
        self._session.add(row)
        await self._session.flush()
        return _action_record(row)

    async def get(self, action_id: int) -> PendingActionRecord | None:
        row = await self._row(action_id)
        return _action_record(row) if row else None

    async def list(
        self, status: PendingActionStatus | None = None, limit: int = 100
    ) -> list[PendingActionRecord]:
        query = select(PendingActionRow).order_by(PendingActionRow.id.desc()).limit(limit)
        if status is not None:
            query = query.where(PendingActionRow.status == status)
        rows = (await self._session.execute(query)).scalars().all()
        return [_action_record(row) for row in rows]

    async def update(self, action_id: int, changes: PendingActionChanges) -> PendingActionRecord:
        row = await self._row(action_id)
        if row is None:
            raise LookupError(f"action {action_id} does not exist")
        for field, value in changes.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        await self._session.flush()
        return _action_record(row)

    async def _row(self, action_id: int) -> PendingActionRow | None:
        query = (
            select(PendingActionRow)
            .where(PendingActionRow.id == action_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(query)).scalar_one_or_none()


class PolicyRepository:
    def __init__(self, session: AsyncSession, clock: Clock = utc_now) -> None:
        self._session = session
        self._clock = clock

    async def current(self) -> Policy | None:
        query = select(PolicyRow).order_by(PolicyRow.version.desc()).limit(1)
        row = (await self._session.execute(query)).scalar_one_or_none()
        return Policy.model_validate(row.data) if row else None

    async def save(self, policy: Policy) -> Policy:
        latest = (await self._session.execute(select(func.max(PolicyRow.version)))).scalar()
        stored = policy.model_copy(update={"version": (latest or 0) + 1})
        self._session.add(
            PolicyRow(
                version=stored.version,
                data=stored.model_dump(mode="json"),
                created_at=self._clock(),
            )
        )
        await self._session.flush()
        return stored


def _action_record(row: PendingActionRow) -> PendingActionRecord:
    return PendingActionRecord(
        id=row.id,
        kind=row.kind,
        status=row.status,
        client_email=row.client_email,
        amount=from_cents(row.amount_cents),
        currency=row.currency,
        summary=row.summary,
        payload=row.payload,
        decisions=row.decisions,
        verdict=row.verdict,
        rule=row.rule,
        reason=row.reason,
        policy_version=row.policy_version,
        invoice_id=row.invoice_id,
        result=row.result,
        error=row.error,
        created_at=row.created_at,
        decided_at=row.decided_at,
        executed_at=row.executed_at,
    )


def _event_record(row: WebhookEventRow) -> WebhookEventRecord:
    return WebhookEventRecord(
        id=row.id,
        paypal_event_id=row.paypal_event_id,
        event_type=row.event_type,
        invoice_paypal_id=row.invoice_paypal_id,
        status=row.status,
        attempts=row.attempts,
        note=row.note,
        payload=row.payload,
        received_at=row.received_at,
        finished_at=row.finished_at,
    )


def _client_record(row: ClientRow) -> ClientRecord:
    return ClientRecord(id=row.id, name=row.name, email=row.email)


def _invoice_record(row: InvoiceRow) -> InvoiceRecord:
    return InvoiceRecord(
        id=row.id,
        paypal_id=row.paypal_id,
        reference=row.reference,
        number=row.number,
        client=_client_record(row.client) if row.client else None,
        status=row.status,
        paypal_status=row.paypal_status,
        currency=row.currency,
        total=from_cents(row.total_cents),
        paid=from_cents(row.paid_cents),
        balance=from_cents(row.balance_cents),
        issue_date=row.issue_date,
        due_date=row.due_date,
        payer_view_url=row.payer_view_url,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _audit_record(row: AuditRow) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        at=row.at,
        actor=row.actor,
        kind=row.kind,
        invoice_id=row.invoice_id,
        verdict=row.verdict,
        rule=row.rule,
        policy_version=row.policy_version,
        detail=row.detail,
    )
