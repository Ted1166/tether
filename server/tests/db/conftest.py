from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.db.database import Database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


class StepClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(minutes=1)
        return current


@pytest.fixture
def clock() -> StepClock:
    return StepClock()
