from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.container import Container, build_container


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        owned: Container | None = None
        if container is None:
            owned = await build_container()
            application.state.container = owned
        try:
            yield
        finally:
            if owned is not None and owned.shutdown is not None:
                await owned.shutdown()

    application = FastAPI(title="Tether", lifespan=lifespan)
    if container is not None:
        application.state.container = container
    application.include_router(router)
    return application


app = create_app()
