from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        options: dict[str, Any] = {}
        if ":memory:" in url:
            options["poolclass"] = StaticPool
            options["connect_args"] = {"check_same_thread": False}

        self._engine = create_async_engine(url, echo=echo, **options)
        if self._engine.dialect.name == "sqlite":
            event.listen(self._engine.sync_engine, "connect", _enable_foreign_keys)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()


def _enable_foreign_keys(connection: Any, record: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
