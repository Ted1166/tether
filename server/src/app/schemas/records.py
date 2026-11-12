from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.actions import ActionKind, InvoiceStatus, Verdict
from app.schemas.invoice import PayPalInvoiceStatus


class AuditActor(StrEnum):
    AGENT = "agent"
    OWNER = "owner"
    PAYPAL = "paypal"
    SYSTEM = "system"


class PendingActionStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class WebhookEventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class ClientRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    email: str


class InvoiceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    paypal_id: str | None
    reference: str | None
    number: str | None
    client: ClientRecord | None
    status: InvoiceStatus
    paypal_status: PayPalInvoiceStatus
    currency: str
    total: Decimal
    paid: Decimal
    balance: Decimal
    issue_date: date | None
    due_date: date | None
    payer_view_url: str | None
    created_at: datetime
    updated_at: datetime


class NewInvoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    paypal_id: str | None = None
    reference: str | None = None
    number: str | None = None
    client_id: int | None = None
    status: InvoiceStatus
    paypal_status: PayPalInvoiceStatus
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total: Decimal = Field(ge=0)
    paid: Decimal = Field(default=Decimal(0), ge=0)
    balance: Decimal = Field(ge=0)
    issue_date: date | None = None
    due_date: date | None = None
    payer_view_url: str | None = None


class InvoiceChanges(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: str | None = None
    status: InvoiceStatus | None = None
    paypal_status: PayPalInvoiceStatus | None = None
    paid: Decimal | None = Field(default=None, ge=0)
    balance: Decimal | None = Field(default=None, ge=0)
    due_date: date | None = None
    payer_view_url: str | None = None


class NewAuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor: AuditActor
    kind: str = Field(min_length=1, max_length=60)
    invoice_id: int | None = None
    verdict: Verdict | None = None
    rule: str | None = None
    policy_version: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    at: datetime
    actor: AuditActor
    kind: str
    invoice_id: int | None
    verdict: Verdict | None
    rule: str | None
    policy_version: int | None
    detail: dict[str, Any]


class WebhookEventRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    paypal_event_id: str
    event_type: str
    invoice_paypal_id: str | None
    status: WebhookEventStatus
    attempts: int
    note: str | None
    payload: dict[str, Any]
    received_at: datetime
    finished_at: datetime | None


class NewPendingAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ActionKind
    status: PendingActionStatus
    client_email: str
    amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    summary: str
    payload: dict[str, Any]
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    verdict: Verdict
    rule: str
    reason: str
    policy_version: int


class PendingActionChanges(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: PendingActionStatus | None = None
    verdict: Verdict | None = None
    rule: str | None = None
    reason: str | None = None
    policy_version: int | None = None
    invoice_id: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    decided_at: datetime | None = None
    executed_at: datetime | None = None


class PendingActionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    kind: ActionKind
    status: PendingActionStatus
    client_email: str
    amount: Decimal
    currency: str
    summary: str
    payload: dict[str, Any]
    decisions: list[dict[str, Any]]
    verdict: Verdict
    rule: str
    reason: str
    policy_version: int
    invoice_id: int | None
    result: dict[str, Any]
    error: str | None
    created_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
