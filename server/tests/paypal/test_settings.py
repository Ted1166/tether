import pytest
from pydantic import ValidationError

from app.config import DEFAULT_SCOPE, SANDBOX_URL, DatabaseSettings, PayPalSettings


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "abc")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "xyz")
    monkeypatch.setenv("PAYPAL_MERCHANT_NAME", "Tether Demo Studio")
    settings = PayPalSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.client_id == "abc"
    assert settings.client_secret.get_secret_value() == "xyz"
    assert settings.merchant_name == "Tether Demo Studio"
    assert settings.base_url == SANDBOX_URL


def test_missing_credentials_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    with pytest.raises(ValidationError):
        PayPalSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_secret_never_appears_in_the_repr(settings: PayPalSettings) -> None:
    assert "client-secret" not in repr(settings)
    assert "client-secret" not in str(settings)


def test_live_urls_are_refused_by_default() -> None:
    with pytest.raises(ValidationError):
        PayPalSettings(
            _env_file=None,  # type: ignore[call-arg]
            client_id="a",
            client_secret="b",  # type: ignore[arg-type]
            base_url="https://api-m.paypal.com",
        )


def test_live_urls_need_an_explicit_override() -> None:
    settings = PayPalSettings(
        _env_file=None,  # type: ignore[call-arg]
        client_id="a",
        client_secret="b",  # type: ignore[arg-type]
        base_url="https://api-m.paypal.com",
        allow_live=True,
    )
    assert settings.base_url == "https://api-m.paypal.com"


def test_the_default_scope_includes_invoicing() -> None:
    assert "services/invoicing" in DEFAULT_SCOPE


def test_the_scope_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "abc")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "xyz")
    monkeypatch.setenv("PAYPAL_SCOPE", "https://uri.paypal.com/services/invoicing")
    settings = PayPalSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.scope == "https://uri.paypal.com/services/invoicing"


def test_blank_values_in_the_environment_count_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "xyz")
    with pytest.raises(ValidationError):
        PayPalSettings(_env_file=None)  # type: ignore[call-arg]


def test_database_settings_default_to_a_local_sqlite_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TETHER_DATABASE_URL", raising=False)
    assert DatabaseSettings(_env_file=None).database_url == "sqlite+aiosqlite:///./tether.db"


def test_database_url_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TETHER_DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    assert DatabaseSettings(_env_file=None).database_url == "postgresql+asyncpg://u:p@host/db"
