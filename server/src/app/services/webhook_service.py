import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.db.database import Database
from app.db.repositories import (
    AuditRepository,
    ClientRepository,
    Clock,
    InvoiceRepository,
    WebhookEventRepository,
)
from app.lib.time import utc_now
from app.paypal.errors import PayPalNotFound
from app.paypal.webhooks import invoice_id_from_event
from app.schemas.invoice import RemoteInvoice
from app.schemas.records import InvoiceRecord, WebhookEventStatus
from app.schemas.webhooks import MissingWebhookHeader, WebhookEvent, WebhookHeaders
from app.services.invoice_sync import InvoiceSyncService, SyncSource

logger = logging.getLogger("tether.webhooks")


class InvalidWebhook(Exception):
    pass


class SignatureVerifier(Protocol):
    async def verify(self, headers: WebhookHeaders, raw_body: bytes) -> bool: ...


class InvoiceSource(Protocol):
    async def get(self, invoice_id: str) -> RemoteInvoice: ...


class WebhookOutcome(StrEnum):
    PROCESSED = "processed"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class WebhookResult:
    outcome: WebhookOutcome
    event_id: str
    invoice: InvoiceRecord | None = None
    note: str | None = None


def _utc_today() -> date:
    return utc_now().date()


class WebhookService:
    def __init__(
        self,
        database: Database,
        invoices: InvoiceSource,
        verifier: SignatureVerifier,
        *,
        today: Callable[[], date] = _utc_today,
        clock: Clock = utc_now,
    ) -> None:
        self._database = database
        self._invoices = invoices
        self._verifier = verifier
        self._today = today
        self._clock = clock

    async def handle(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookResult:
        try:
            parsed_headers = WebhookHeaders.from_headers(headers)
        except MissingWebhookHeader as error:
            raise InvalidWebhook(str(error)) from error

        if not await self._verifier.verify(parsed_headers, raw_body):
            raise InvalidWebhook("signature verification failed")

        event, payload = self._parse(raw_body)
        invoice_id = invoice_id_from_event(event)

        try:
            should_process = await self._begin(event, invoice_id, payload)
        except IntegrityError:
            return WebhookResult(
                WebhookOutcome.DUPLICATE, event.id, note="the event is already being handled"
            )
        if not should_process:
            return WebhookResult(WebhookOutcome.DUPLICATE, event.id)

        if invoice_id is None:
            note = f"{event.event_type} carries no invoice to sync"
            await self._finish(event.id, WebhookEventStatus.IGNORED, note)
            return WebhookResult(WebhookOutcome.IGNORED, event.id, note=note)

        try:
            remote = await self._invoices.get(invoice_id)
        except PayPalNotFound:
            note = "the invoice does not exist in this PayPal account"
            await self._finish(event.id, WebhookEventStatus.IGNORED, note)
            return WebhookResult(WebhookOutcome.IGNORED, event.id, note=note)
        except Exception as error:
            await self._finish(event.id, WebhookEventStatus.FAILED, str(error))
            raise

        try:
            async with self._database.session() as session:
                service = InvoiceSyncService(
                    InvoiceRepository(session, self._clock),
                    ClientRepository(session, self._clock),
                    AuditRepository(session, self._clock),
                )
                result = await service.apply(remote, today=self._today(), source=SyncSource.WEBHOOK)
                await WebhookEventRepository(session, self._clock).finish(
                    event.id, WebhookEventStatus.PROCESSED, result.outcome.value
                )
        except Exception as error:
            await self._finish(event.id, WebhookEventStatus.FAILED, str(error))
            raise

        logger.info("webhook %s %s for %s", event.id, result.outcome.value, invoice_id)
        return WebhookResult(
            WebhookOutcome.PROCESSED, event.id, invoice=result.invoice, note=result.outcome.value
        )

    def _parse(self, raw_body: bytes) -> tuple[WebhookEvent, dict[str, Any]]:
        try:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("the body is not a JSON object")
            return WebhookEvent.model_validate(payload), payload
        except (ValueError, ValidationError) as error:
            raise InvalidWebhook(f"malformed webhook event: {error}") from error

    async def _begin(
        self, event: WebhookEvent, invoice_id: str | None, payload: dict[str, Any]
    ) -> bool:
        async with self._database.session() as session:
            _, should_process = await WebhookEventRepository(session, self._clock).begin(
                event.id, event.event_type, invoice_id, payload
            )
        return should_process

    async def _finish(self, event_id: str, status: WebhookEventStatus, note: str) -> None:
        async with self._database.session() as session:
            await WebhookEventRepository(session, self._clock).finish(event_id, status, note)
