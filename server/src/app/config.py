from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_HOST = "api-m.sandbox.paypal.com"
SANDBOX_URL = f"https://{SANDBOX_HOST}"
DEFAULT_SCOPE = (
    "https://uri.paypal.com/services/invoicing "
    "https://uri.paypal.com/services/applications/webhooks"
)


class PayPalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAYPAL_",
        env_file=(".env", "../.env"),
        env_ignore_empty=True,
        extra="ignore",
    )

    client_id: str
    client_secret: SecretStr
    base_url: str = SANDBOX_URL
    allow_live: bool = False
    scope: str = DEFAULT_SCOPE
    webhook_id: str | None = None
    merchant_name: str | None = None

    @model_validator(mode="after")
    def sandbox_only_unless_overridden(self) -> "PayPalSettings":
        host = urlparse(self.base_url).hostname
        if host != SANDBOX_HOST and not self.allow_live:
            raise ValueError(
                "Tether only talks to the PayPal sandbox. Set PAYPAL_ALLOW_LIVE=true to override."
            )
        return self


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TETHER_",
        env_file=(".env", "../.env"),
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./tether.db"
