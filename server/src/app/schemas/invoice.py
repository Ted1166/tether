from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import normalize_email


class PayPalInvoiceStatus(StrEnum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    SENT = "SENT"
    UNPAID = "UNPAID"
    SHARED = "SHARED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    MARKED_AS_PAID = "MARKED_AS_PAID"
    PAID_EXTERNAL = "PAID_EXTERNAL"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    MARKED_AS_REFUNDED = "MARKED_AS_REFUNDED"
    REFUNDED_EXTERNAL = "REFUNDED_EXTERNAL"
    CANCELLED = "CANCELLED"
    AUTO_CANCELLED = "AUTO_CANCELLED"


class PaymentMethod(StrEnum):
    BANK_TRANSFER = "BANK_TRANSFER"
    CASH = "CASH"
    CHECK = "CHECK"
    CREDIT_CARD = "CREDIT_CARD"
    DEBIT_CARD = "DEBIT_CARD"
    PAYPAL = "PAYPAL"
    WIRE_TRANSFER = "WIRE_TRANSFER"
    OTHER = "OTHER"


class LineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    quantity: Decimal = Field(default=Decimal(1), gt=0)
    unit_amount: Decimal = Field(gt=0)

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_amount


class InvoiceDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_name: str = Field(min_length=1, max_length=300)
    client_email: str
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    items: tuple[LineItem, ...] = Field(min_length=1, max_length=100)
    due_in_days: int = Field(default=14, ge=0, le=365)
    note: str | None = Field(default=None, max_length=4000)
    reference: str | None = Field(default=None, max_length=120)
    allow_partial_payment: bool = False

    @field_validator("client_name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("client_name cannot be blank")
        return cleaned

    @field_validator("client_email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)

    @property
    def total(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal(0))


class RemoteInvoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    number: str | None = None
    paypal_status: PayPalInvoiceStatus
    currency: str
    total: Decimal
    paid: Decimal = Decimal(0)
    balance: Decimal
    issue_date: date | None = None
    due_date: date | None = None
    client_name: str | None = None
    client_email: str | None = None
    payer_view_url: str | None = None
    reference: str | None = None


class InvoicePage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[RemoteInvoice, ...]
    total_items: int = 0
    total_pages: int = 0
