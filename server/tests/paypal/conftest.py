import pytest

from app.config import PayPalSettings
from tests.paypal.helpers import Harness


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> PayPalSettings:
    return PayPalSettings(_env_file=None, client_id="client-id", client_secret="client-secret")  # type: ignore[arg-type]


@pytest.fixture
def harness(settings: PayPalSettings) -> Harness:
    return Harness(settings)
