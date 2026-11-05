"""Application entrypoint."""

from fastapi import FastAPI

from job_tracker.api.deps import get_settings
from job_tracker.api.routers import health


def create_app() -> FastAPI:
    """Create the FastAPI application and validate settings immediately."""
    get_settings()
    application = FastAPI(title="Job Tracker API")
    application.include_router(health.router)
    return application


app = create_app()
