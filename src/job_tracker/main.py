"""Application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from job_tracker.api.deps import get_settings
from job_tracker.api.exception_handlers import register_exception_handlers
from job_tracker.api.routers import (
    auth,
    companies,
    health,
)
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
    settings = get_settings()
    application = FastAPI(title="Job Tracker API", lifespan=lifespan)
    register_exception_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-Requested-With"],
    )
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(companies.router)
    return application


app = create_app()
