from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.db.repositories import AuditRepository, ClientRepository, InvoiceRepository
from app.domain.errors import IllegalTransition
from app.domain.invoice_state import advance_invoice
from app.paypal.mappers import to_internal_status
from app.schemas.actions import InvoiceStatus
from app.schemas.invoice import RemoteInvoice
from app.schemas.records import (
    AuditActor,
    InvoiceChanges,
    InvoiceRecord,
    NewAuditEntry,
    NewInvoice,
)


class SyncSource(StrEnum):
    IMPORT = "import"
    WEBHOOK = "webhook"
    RECONCILE = "reconcile"


class SyncOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SyncResult:
    invoice: InvoiceRecord
    outcome: SyncOutcome
    previous_status: InvoiceStatus | None = None
    reason: str | None = None


class InvoiceSyncService:
    def __init__(
        self,
        invoices: InvoiceRepository,
        clients: ClientRepository,
        audit: AuditRepository,
    ) -> None:
        self._invoices = invoices
        self._clients = clients
        self._audit = audit

    async def apply(self, remote: RemoteInvoice, *, today: date, source: SyncSource) -> SyncResult:
        target = to_internal_status(remote, today)
        existing = await self._invoices.get_by_paypal_id(remote.id)
        if existing is None:
            return await self._create(remote, target, source)
        return await self._update(existing, remote, target, source)

    async def _create(
        self, remote: RemoteInvoice, target: InvoiceStatus, source: SyncSource
    ) -> SyncResult:
        client_id = None
        if remote.client_email:
            client = await self._clients.get_or_create(
                remote.client_name or remote.client_email, remote.client_email
            )
            client_id = client.id

        invoice = await self._invoices.create(
            NewInvoice(
                paypal_id=remote.id,
                reference=remote.reference,
                number=remote.number,
                client_id=client_id,
                status=target,
                paypal_status=remote.paypal_status,
                currency=remote.currency,
                total=remote.total,
                paid=remote.paid,
                balance=remote.balance,
                issue_date=remote.issue_date,
                due_date=remote.due_date,
                payer_view_url=remote.payer_view_url,
            )
        )
        await self._record(
            source,
            "invoice_imported",
            invoice.id,
            {"paypal_id": remote.id, "status": target.value, "paypal_status": remote.paypal_status},
        )
        return SyncResult(invoice=invoice, outcome=SyncOutcome.CREATED)

    async def _update(
        self,
        existing: InvoiceRecord,
        remote: RemoteInvoice,
        target: InvoiceStatus,
        source: SyncSource,
    ) -> SyncResult:
        try:
            new_status = advance_invoice(existing.status, target)
        except IllegalTransition as error:
            await self._record(
                source,
                "transition_rejected",
                existing.id,
                {
                    "from": existing.status.value,
                    "attempted": target.value,
                    "paypal_status": remote.paypal_status,
                },
            )
            return SyncResult(
                invoice=existing,
                outcome=SyncOutcome.REJECTED,
                previous_status=existing.status,
                reason=str(error),
            )

        fields = self._differences(existing, remote, new_status)
        if not fields:
            return SyncResult(invoice=existing, outcome=SyncOutcome.UNCHANGED)

        updated = await self._invoices.update(existing.id, InvoiceChanges(**fields))
        if existing.status != new_status:
            await self._record(
                source,
                "invoice_status_changed",
                existing.id,
                {
                    "from": existing.status.value,
                    "to": new_status.value,
                    "paypal_status": remote.paypal_status,
                },
            )
        else:
            await self._record(
                source,
                "invoice_updated",
                existing.id,
                {"fields": sorted(fields), "paypal_status": remote.paypal_status},
            )
        return SyncResult(
            invoice=updated, outcome=SyncOutcome.UPDATED, previous_status=existing.status
        )

    def _differences(
        self, existing: InvoiceRecord, remote: RemoteInvoice, new_status: InvoiceStatus
    ) -> dict[str, Any]:
        candidates: dict[str, Any] = {
            "status": new_status,
            "paypal_status": remote.paypal_status,
            "paid": remote.paid,
            "balance": remote.balance,
            "number": remote.number,
            "due_date": remote.due_date,
            "payer_view_url": remote.payer_view_url,
        }
        return {
            field: value
            for field, value in candidates.items()
            if value is not None and getattr(existing, field) != value
        }

    async def _record(
        self, source: SyncSource, kind: str, invoice_id: int, detail: dict[str, Any]
    ) -> None:
        actor = AuditActor.SYSTEM if source is SyncSource.IMPORT else AuditActor.PAYPAL
        await self._audit.append(
            NewAuditEntry(
                actor=actor,
                kind=kind,
                invoice_id=invoice_id,
                detail={**detail, "source": source.value},
            )
        )
