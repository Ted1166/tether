from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.types import UtcDateTime
from app.schemas.actions import InvoiceStatus, Verdict
from app.schemas.invoice import PayPalInvoiceStatus
from app.schemas.records import AuditActor, WebhookEventStatus


class Base(DeclarativeBase):
    pass


def _enum(kind: type[StrEnum], length: int) -> Enum:
    return Enum(
        kind,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class ClientRow(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class InvoiceRow(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    paypal_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    reference: Mapped[str | None] = mapped_column(String(120), index=True)
    number: Mapped[str | None] = mapped_column(String(64))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    client: Mapped[ClientRow | None] = relationship(lazy="joined")
    status: Mapped[InvoiceStatus] = mapped_column(_enum(InvoiceStatus, 20), index=True)
    paypal_status: Mapped[PayPalInvoiceStatus] = mapped_column(_enum(PayPalInvoiceStatus, 24))
    currency: Mapped[str] = mapped_column(String(3))
    total_cents: Mapped[int] = mapped_column(BigInteger)
    paid_cents: Mapped[int] = mapped_column(BigInteger)
    balance_cents: Mapped[int] = mapped_column(BigInteger)
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    payer_view_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class AuditRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    actor: Mapped[AuditActor] = mapped_column(_enum(AuditActor, 10))
    kind: Mapped[str] = mapped_column(String(60), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), index=True)
    verdict: Mapped[Verdict | None] = mapped_column(_enum(Verdict, 20))
    rule: Mapped[str | None] = mapped_column(String(60))
    policy_version: Mapped[int | None]
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)


class WebhookEventRow(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    paypal_event_id: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    invoice_paypal_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[WebhookEventStatus] = mapped_column(_enum(WebhookEventStatus, 12))
    attempts: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
