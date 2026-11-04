from collections.abc import AsyncIterator

import pytest

from app.db.database import Database
from tests.db.conftest import StepClock


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
def clock() -> StepClock:
    return StepClock()
