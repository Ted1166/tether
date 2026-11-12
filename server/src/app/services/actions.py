import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import Database
from app.db.repositories import (
    AuditRepository,
    ClientRepository,
    Clock,
    InvoiceRepository,
    PendingActionRepository,
)
from app.domain.action_state import DECIDABLE, advance_action
from app.lib.time import utc_now
from app.paypal.errors import PayPalError
from app.policy.combine import combine_decisions
from app.policy.engine import evaluate
from app.schemas.actions import (
    ActionContext,
    ActionKind,
    Decision,
    InvoiceStatus,
    ProposedAction,
    Verdict,
)
from app.schemas.agent import InvoiceProposal
from app.schemas.invoice import InvoiceDraft
from app.schemas.policy import Policy
from app.schemas.records import (
    AuditActor,
    ClientRecord,
    NewAuditEntry,
    NewPendingAction,
    PendingActionChanges,
    PendingActionRecord,
    PendingActionStatus,
)
from app.services.invoice_executor import InvoiceExecutor
from app.services.invoice_sync import InvoiceSyncService, SyncSource
from app.services.policy_service import load_policy
from app.services.webhook_service import InvoiceSource

logger = logging.getLogger("tether.actions")


class ActionError(Exception):
    pass


class ActionNotFound(ActionError):
    pass


class ActionNotDecidable(ActionError):
    pass


class ActionBlocked(ActionError):
    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class ActionFailed(ActionError):
    pass


def _utc_today() -> date:
    return utc_now().date()


class ActionService:
    def __init__(
        self,
        database: Database,
        executor: InvoiceExecutor,
        invoices: InvoiceSource,
        *,
        today: Callable[[], date] = _utc_today,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._executor = executor
        self._invoices = invoices
        self._today = today
        self._clock = clock

    async def known_clients(self, limit: int = 50) -> list[ClientRecord]:
        async with self._database.session() as session:
            return (await ClientRepository(session, self._clock).all())[:limit]

    async def propose_invoice(self, proposal: InvoiceProposal) -> PendingActionRecord:
        now = self._clock()
        async with self._database.session() as session:
            policy = await load_policy(session, self._clock)
            decision, decisions = self._evaluate(proposal, policy, now)
            status = (
                PendingActionStatus.BLOCKED
                if decision.verdict is Verdict.BLOCK
                else PendingActionStatus.PENDING
            )
            record = await PendingActionRepository(session, self._clock).create(
                NewPendingAction(
                    kind=ActionKind.CREATE_INVOICE,
                    status=status,
                    client_email=proposal.draft.client_email,
                    amount=proposal.total,
                    currency=proposal.draft.currency,
                    summary=_summary(proposal),
                    payload=proposal.model_dump(mode="json"),
                    decisions=[item.model_dump(mode="json") for item in decisions],
                    verdict=decision.verdict,
                    rule=decision.rule,
                    reason=decision.reason,
                    policy_version=decision.policy_version,
                )
            )
            await self._audit(
                session,
                AuditActor.AGENT,
                "action_blocked" if status is PendingActionStatus.BLOCKED else "action_proposed",
                record,
                decision,
            )

        if decision.verdict is Verdict.ALLOW:
            return await self._execute(record.id, AuditActor.AGENT)
        return record

    async def approve(self, action_id: int) -> PendingActionRecord:
        blocked: Decision | None = None
        async with self._database.session() as session:
            record = await self._load(session, action_id)
            if record.status not in DECIDABLE:
                raise ActionNotDecidable(f"the action is already {record.status.value}")

            policy = await load_policy(session, self._clock)
            proposal = InvoiceProposal.model_validate(record.payload)
            decision, _ = self._evaluate(proposal, policy, self._clock())
            repository = PendingActionRepository(session, self._clock)

            if decision.verdict is Verdict.BLOCK:
                advance_action(record.status, PendingActionStatus.BLOCKED)
                record = await repository.update(
                    action_id,
                    PendingActionChanges(
                        status=PendingActionStatus.BLOCKED,
                        verdict=decision.verdict,
                        rule=decision.rule,
                        reason=decision.reason,
                        policy_version=decision.policy_version,
                        error=decision.reason,
                        decided_at=self._clock(),
                    ),
                )
                await self._audit(session, AuditActor.OWNER, "action_blocked", record, decision)
                blocked = decision
            else:
                record = await repository.update(
                    action_id, PendingActionChanges(decided_at=self._clock())
                )
                await self._audit(session, AuditActor.OWNER, "action_approved", record, decision)

        if blocked is not None:
            raise ActionBlocked(blocked)
        return await self._execute(action_id, AuditActor.OWNER)

    async def reject(self, action_id: int, reason: str | None = None) -> PendingActionRecord:
        async with self._database.session() as session:
            record = await self._load(session, action_id)
            if record.status not in DECIDABLE:
                raise ActionNotDecidable(f"the action is already {record.status.value}")
            advance_action(record.status, PendingActionStatus.REJECTED)
            updated = await PendingActionRepository(session, self._clock).update(
                action_id,
                PendingActionChanges(
                    status=PendingActionStatus.REJECTED,
                    error=reason,
                    decided_at=self._clock(),
                ),
            )
            await AuditRepository(session, self._clock).append(
                NewAuditEntry(
                    actor=AuditActor.OWNER,
                    kind="action_rejected",
                    detail={"action_id": action_id, "reason": reason, "summary": record.summary},
                )
            )
            return updated

    async def get(self, action_id: int) -> PendingActionRecord:
        async with self._database.session() as session:
            return await self._load(session, action_id)

    async def list_actions(
        self, status: PendingActionStatus | None = None, limit: int = 100
    ) -> list[PendingActionRecord]:
        async with self._database.session() as session:
            return await PendingActionRepository(session, self._clock).list(status, limit)

    async def _execute(self, action_id: int, actor: AuditActor) -> PendingActionRecord:
        async with self._database.session() as session:
            record = await self._load(session, action_id)
        proposal = InvoiceProposal.model_validate(record.payload)

        async def remember(paypal_id: str) -> None:
            async with self._database.session() as inner:
                await PendingActionRepository(inner, self._clock).update(
                    action_id,
                    PendingActionChanges(result={**record.result, "paypal_id": paypal_id}),
                )

        try:
            paypal_id = await self._executor.create_and_send(
                proposal.draft,
                send=proposal.send,
                action_id=action_id,
                resume_paypal_id=record.result.get("paypal_id"),
                on_created=remember,
            )
            remote = await self._invoices.get(paypal_id)
        except PayPalError as error:
            await self._fail(action_id, str(error))
            raise ActionFailed(str(error)) from error
        except Exception as error:
            await self._fail(action_id, str(error))
            raise

        async with self._database.session() as session:
            sync = await InvoiceSyncService(
                InvoiceRepository(session, self._clock),
                ClientRepository(session, self._clock),
                AuditRepository(session, self._clock),
            ).apply(remote, today=self._today(), source=SyncSource.AGENT)

            current = await self._load(session, action_id)
            advance_action(current.status, PendingActionStatus.EXECUTED)
            updated = await PendingActionRepository(session, self._clock).update(
                action_id,
                PendingActionChanges(
                    status=PendingActionStatus.EXECUTED,
                    invoice_id=sync.invoice.id,
                    result={
                        "paypal_id": paypal_id,
                        "number": sync.invoice.number,
                        "sent": proposal.send,
                    },
                    error=None,
                    executed_at=self._clock(),
                ),
            )
            await AuditRepository(session, self._clock).append(
                NewAuditEntry(
                    actor=actor,
                    kind="action_executed",
                    invoice_id=sync.invoice.id,
                    verdict=updated.verdict,
                    rule=updated.rule,
                    policy_version=updated.policy_version,
                    detail={
                        "action_id": action_id,
                        "paypal_id": paypal_id,
                        "summary": updated.summary,
                    },
                )
            )
        logger.info("action %s executed as %s", action_id, paypal_id)
        return updated

    async def _fail(self, action_id: int, message: str) -> None:
        async with self._database.session() as session:
            current = await self._load(session, action_id)
            advance_action(current.status, PendingActionStatus.FAILED)
            record = await PendingActionRepository(session, self._clock).update(
                action_id,
                PendingActionChanges(status=PendingActionStatus.FAILED, error=message),
            )
            await AuditRepository(session, self._clock).append(
                NewAuditEntry(
                    actor=AuditActor.AGENT,
                    kind="action_failed",
                    detail={"action_id": action_id, "error": message, "summary": record.summary},
                )
            )

    async def _load(self, session: AsyncSession, action_id: int) -> PendingActionRecord:
        record = await PendingActionRepository(session, self._clock).get(action_id)
        if record is None:
            raise ActionNotFound(f"action {action_id} does not exist")
        return record

    def _evaluate(
        self, proposal: InvoiceProposal, policy: Policy, now: datetime
    ) -> tuple[Decision, list[Decision]]:
        email = proposal.draft.client_email
        decisions = [
            evaluate(
                ProposedAction(
                    kind=ActionKind.CREATE_INVOICE, client_email=email, amount=proposal.total
                ),
                policy,
                ActionContext(now=now, invoice_status=None),
            )
        ]
        if proposal.send:
            decisions.append(
                evaluate(
                    ProposedAction(
                        kind=ActionKind.SEND_INVOICE, client_email=email, amount=proposal.total
                    ),
                    policy,
                    ActionContext(now=now, invoice_status=InvoiceStatus.DRAFT),
                )
            )
        return combine_decisions(decisions), decisions

    async def _audit(
        self,
        session: AsyncSession,
        actor: AuditActor,
        kind: str,
        record: PendingActionRecord,
        decision: Decision,
    ) -> None:
        detail: dict[str, Any] = {
            "action_id": record.id,
            "summary": record.summary,
            "reason": decision.reason,
            "triggered_rules": list(decision.triggered_rules),
        }
        await AuditRepository(session, self._clock).append(
            NewAuditEntry(
                actor=actor,
                kind=kind,
                verdict=decision.verdict,
                rule=decision.rule,
                policy_version=decision.policy_version,
                detail=detail,
            )
        )


def _summary(proposal: InvoiceProposal) -> str:
    draft: InvoiceDraft = proposal.draft
    text = (
        f"Invoice {draft.currency} {proposal.total:,.2f} to {draft.client_name} "
        f"<{draft.client_email}>, due in {draft.due_in_days} days"
    )
    return text + (", sent on creation" if proposal.send else ", saved as a draft")
