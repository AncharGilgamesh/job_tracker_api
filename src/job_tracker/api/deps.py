"""FastAPI dependencies."""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.api.exception_handlers import ApiError
from job_tracker.config import Settings
from job_tracker.db.models.user import User
from job_tracker.services.access_tokens import (
    InvalidAccessTokenError,
    decode_access_token,
)
from job_tracker.services.users import get_user_by_id


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


_access_token_scheme = HTTPBearer(auto_error=False)


def _authentication_error(code: str, message: str) -> ApiError:
    return ApiError(
        status_code=401,
        code=code,
        message=message,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_access_token_scheme),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Return the user identified by a Bearer access token.

    A missing bearer credential and an invalid token use different public
    codes. A valid token whose user row is absent uses the invalid-token
    response, so the result does not reveal whether that user exists.

    This dependency does not commit, roll back, or begin a transaction.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _authentication_error(
            "MISSING_ACCESS_TOKEN",
            "Authentication is required",
        )
    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidAccessTokenError:
        raise _authentication_error(
            "INVALID_ACCESS_TOKEN",
            "Invalid access token",
        ) from None
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise _authentication_error("INVALID_ACCESS_TOKEN", "Invalid access token")
    return user
