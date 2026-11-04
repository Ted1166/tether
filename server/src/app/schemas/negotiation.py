from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClientIntent(StrEnum):
    CANNOT_PAY_IN_FULL = "cannot_pay_in_full"
    REQUESTS_EXTENSION = "requests_extension"
    REQUESTS_DISCOUNT = "requests_discount"
    DISPUTES_INVOICE = "disputes_invoice"
    ACCEPTS_OFFER = "accepts_offer"
    DECLINES_OFFER = "declines_offer"
    WILL_PAY_SOON = "will_pay_soon"
    OTHER = "other"


class Installment(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int = Field(ge=1)
    amount: Decimal = Field(gt=0)
    due_in_days: int = Field(ge=0)


class PaymentPlanOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total: Decimal = Field(gt=0)
    installments: tuple[Installment, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def installments_are_consistent(self) -> "PaymentPlanOffer":
        numbers = [item.number for item in self.installments]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("installments must be numbered 1..n in order")
        days = [item.due_in_days for item in self.installments]
        if any(later <= earlier for earlier, later in zip(days, days[1:], strict=False)):
            raise ValueError("installment due dates must be strictly increasing")
        if sum((item.amount for item in self.installments), Decimal(0)) != self.total:
            raise ValueError("installments must add up to the total")
        return self

    @property
    def installment_count(self) -> int:
        return len(self.installments)

    @property
    def upfront_amount(self) -> Decimal:
        return self.installments[0].amount

    @property
    def upfront_percent(self) -> int:
        return int(self.upfront_amount * 100 // self.total)

    @property
    def days_out(self) -> int:
        return self.installments[-1].due_in_days
