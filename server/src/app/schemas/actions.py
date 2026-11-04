from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import normalize_email


class ActionKind(StrEnum):
    CREATE_INVOICE = "create_invoice"
    SEND_INVOICE = "send_invoice"
    SEND_REMINDER = "send_reminder"
    OFFER_PAYMENT_PLAN = "offer_payment_plan"
    APPLY_DISCOUNT = "apply_discount"
    CANCEL_INVOICE = "cancel_invoice"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_PAID = "partially_paid"
    OVERDUE = "overdue"
    PAID = "paid"
    CANCELLED = "cancelled"


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    BLOCK = "block"


class ProposedAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ActionKind
    client_email: str
    amount: Decimal = Field(ge=0)
    installments: int | None = Field(default=None, ge=1)
    upfront_percent: int | None = Field(default=None, ge=0, le=100)
    days_out: int | None = Field(default=None, ge=0)
    discount_percent: int = Field(default=0, ge=0, le=100)

    @field_validator("client_email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)


class ActionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    now: datetime
    invoice_status: InvoiceStatus | None = None
    days_overdue: int = Field(default=0, ge=0)
    reminders_last_7_days: int = Field(default=0, ge=0)
    reminders_total: int = Field(default=0, ge=0)

    @field_validator("now")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    rule: str
    reason: str
    triggered_rules: tuple[str, ...] = ()
    policy_version: int
