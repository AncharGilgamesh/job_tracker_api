"""FastAPI dependencies."""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit

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


def require_csrf(request: Request) -> None:
    """Reject cookie-authenticated requests that fail the origin check.

    A present Origin header is compared exactly with the allowlist. Referer is
    used only when Origin is absent.
    """
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise _csrf_error()
    allowed = get_settings().allowed_origins
    origin = request.headers.get("origin")
    if origin is not None:
        if origin not in allowed:
            raise _csrf_error()
        return
    referer = request.headers.get("referer")
    if referer is None:
        raise _csrf_error()
    referer_origin = _origin_from_referer(referer)
    if referer_origin is None or referer_origin not in allowed:
        raise _csrf_error()


def _csrf_error() -> ApiError:
    return ApiError(
        status_code=403,
        code="CSRF_CHECK_FAILED",
        message="Cross-site request blocked",
    )


def _origin_from_referer(referer: str) -> str | None:
    """Return the scheme and authority of a referer, without its path."""
    candidate = referer.strip()
    if candidate == "" or any(character in candidate for character in "\r\n\x00\\"):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        return None
    hostname = parsed.hostname
    if hostname is None or hostname == "":
        return None
    try:
        host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if "*" in host:
        return None
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"
