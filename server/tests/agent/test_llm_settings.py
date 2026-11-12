import pytest
from pydantic import ValidationError

from app.config import LLMSettings


def test_the_key_and_model_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    settings = LLMSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.api_key.get_secret_value() == "sk-ant-test-key"
    assert settings.model == "claude-haiku-5-5"
    assert settings.base_url == "https://api.anthropic.com"


def test_the_default_model_is_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert LLMSettings(_env_file=None).model == "claude-sonnet-5-5"  # type: ignore[call-arg]


def test_a_missing_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None)  # type: ignore[call-arg]


def test_a_blank_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_key_never_appears_in_the_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    settings = LLMSettings(_env_file=None)  # type: ignore[call-arg]
    assert "sk-ant-secret-value" not in repr(settings)
    assert "sk-ant-secret-value" not in str(settings)
