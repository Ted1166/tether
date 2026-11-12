from collections.abc import Awaitable, Callable
from datetime import date
from typing import Protocol

from app.lib.time import utc_now
from app.paypal.invoices import InvoiceGateway
from app.schemas.invoice import InvoiceDraft


class InvoiceExecutor(Protocol):
    async def create_and_send(
        self,
        draft: InvoiceDraft,
        *,
        send: bool,
        action_id: int,
        resume_paypal_id: str | None,
        on_created: Callable[[str], Awaitable[None]],
    ) -> str: ...


def _utc_today() -> date:
    return utc_now().date()


class PayPalInvoiceExecutor:
    def __init__(self, gateway: InvoiceGateway, today: Callable[[], date] = _utc_today) -> None:
        self._gateway = gateway
        self._today = today

    async def create_and_send(
        self,
        draft: InvoiceDraft,
        *,
        send: bool,
        action_id: int,
        resume_paypal_id: str | None,
        on_created: Callable[[str], Awaitable[None]],
    ) -> str:
        paypal_id = resume_paypal_id
        if paypal_id is None:
            tagged = draft.model_copy(update={"reference": f"tether-action-{action_id}"})
            paypal_id = await self._gateway.create_draft(
                tagged, issue_date=self._today(), request_id=f"tether-{action_id}-create"
            )
            await on_created(paypal_id)

        if send:
            await self._gateway.send(paypal_id, request_id=f"tether-{action_id}-send")
        return paypal_id
