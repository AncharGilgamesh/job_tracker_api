"""Application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from job_tracker.api.deps import get_settings
from job_tracker.api.routers import health
from job_tracker.db.session import create_db_engine, create_session_factory


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create the async engine and session factory, then dispose the engine.

    Engine construction does not open a connection. Migrations are not applied
    during startup.
    """
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Create the FastAPI application and validate settings immediately."""
    get_settings()
    application = FastAPI(title="Job Tracker API", lifespan=lifespan)
    application.include_router(health.router)
    return application


app = create_app()
