from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

HEADER_NAMES = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


class MissingWebhookHeader(ValueError):
    pass


class WebhookHeaders(BaseModel):
    model_config = ConfigDict(frozen=True)

    auth_algo: str
    cert_url: str
    transmission_id: str
    transmission_sig: str
    transmission_time: str

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "WebhookHeaders":
        lowered = {name.lower(): value for name, value in headers.items()}
        missing = [name for name in HEADER_NAMES.values() if not lowered.get(name)]
        if missing:
            raise MissingWebhookHeader(f"missing webhook headers: {', '.join(missing)}")
        return cls(**{field: lowered[name] for field, name in HEADER_NAMES.items()})


class WebhookEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=80)
    create_time: datetime | None = None
    resource_type: str | None = None
    summary: str | None = None
    resource: dict[str, Any] = Field(default_factory=dict)
