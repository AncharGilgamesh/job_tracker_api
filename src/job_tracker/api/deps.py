"""FastAPI dependencies."""

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.config import Settings


@lru_cache
def get_settings() -> Settings:
    """Return validated settings.

    Validation runs when the application is created, so a missing required
    value fails process startup rather than the first HTTP request.
    """
    try:
        return Settings()
    except ValidationError as exc:
        field_names = ", ".join(
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        )
        raise RuntimeError(
            f"Missing or invalid application settings: {field_names}"
        ) from None


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one session for the request and close it afterward.

    This dependency does not begin a transaction and does not commit.
    """
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        await session.close()
