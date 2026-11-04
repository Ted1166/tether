from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.actions import ActionKind
from app.schemas.common import normalize_email


class ContactMode(StrEnum):
    ALLOWLIST = "allowlist"
    ANYONE = "anyone"


class ContactPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: ContactMode = ContactMode.ALLOWLIST
    allowed: frozenset[str] = frozenset()
    blocked: frozenset[str] = frozenset()

    @field_validator("allowed", "blocked")
    @classmethod
    def clean_emails(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(normalize_email(item) for item in value)


class ReminderPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_per_week: int = Field(default=2, ge=0)
    max_total: int = Field(default=6, ge=0)
    min_days_overdue: int = Field(default=1, ge=0)


class AmountPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    auto_approve_below: Decimal = Field(default=Decimal("200"), ge=0)
    hard_cap: Decimal = Field(default=Decimal("5000"), ge=0)

    @model_validator(mode="after")
    def cap_covers_threshold(self) -> "AmountPolicy":
        if self.hard_cap < self.auto_approve_below:
            raise ValueError("hard_cap must be at least auto_approve_below")
        return self


class PlanPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_installments: int = Field(default=3, ge=1)
    min_upfront_percent: int = Field(default=25, ge=0, le=100)
    max_days_out: int = Field(default=60, ge=0)
    max_discount_percent: int = Field(default=0, ge=0, le=100)
    min_installment_amount: Decimal = Field(default=Decimal("25"), ge=0)


class QuietHours(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    start_hour: int = Field(default=21, ge=0, le=23)
    end_hour: int = Field(default=7, ge=0, le=23)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone {value!r}") from error
        return value


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int = Field(default=1, ge=1)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    contacts: ContactPolicy = Field(default_factory=ContactPolicy)
    reminders: ReminderPolicy = Field(default_factory=ReminderPolicy)
    amounts: AmountPolicy = Field(default_factory=AmountPolicy)
    plans: PlanPolicy = Field(default_factory=PlanPolicy)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    auto_actions: frozenset[ActionKind] = frozenset({ActionKind.CREATE_INVOICE})
    disabled_actions: frozenset[ActionKind] = frozenset({ActionKind.CANCEL_INVOICE})

    @model_validator(mode="after")
    def actions_do_not_overlap(self) -> "Policy":
        overlap = self.auto_actions & self.disabled_actions
        if overlap:
            names = ", ".join(sorted(kind.value for kind in overlap))
            raise ValueError(f"actions cannot be both automatic and disabled: {names}")
        return self
