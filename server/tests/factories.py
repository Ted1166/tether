from datetime import UTC, datetime
from typing import Any

from app.schemas.actions import (
    ActionContext,
    ActionKind,
    InvoiceStatus,
    ProposedAction,
)

NOON = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
EARLY_MORNING = datetime(2026, 11, 4, 5, 15, tzinfo=UTC)


def make_action(**overrides: Any) -> ProposedAction:
    fields: dict[str, Any] = {
        "kind": ActionKind.CREATE_INVOICE,
        "client_email": "dan@example.com",
        "amount": "120.00",
    }
    fields.update(overrides)
    return ProposedAction(**fields)


def make_context(**overrides: Any) -> ActionContext:
    fields: dict[str, Any] = {
        "now": NOON,
        "invoice_status": InvoiceStatus.OVERDUE,
        "days_overdue": 8,
    }
    fields.update(overrides)
    return ActionContext(**fields)


def make_plan(**overrides: Any) -> ProposedAction:
    fields: dict[str, Any] = {
        "kind": ActionKind.OFFER_PAYMENT_PLAN,
        "amount": "900.00",
        "installments": 3,
        "upfront_percent": 30,
        "days_out": 45,
    }
    fields.update(overrides)
    return make_action(**fields)
