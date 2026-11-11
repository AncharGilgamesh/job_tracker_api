"""Registration and login orchestration.

This module owns authentication transaction boundaries. Persistence helpers
flush and return; they do not commit.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.services.access_tokens import (
    ACCESS_TOKEN_LIFETIME_SECONDS,
    create_access_token,
)
from job_tracker.services.passwords import (
    _hasher,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from job_tracker.services.refresh_tokens import issue_initial_refresh_token
from job_tracker.services.users import create_user, get_user_by_email, parse_email

_EMAIL_CONSTRAINT = "users_email_key"
_UNIQUE_VIOLATION = "23505"
# Not a user password. Hashed once so unknown-email login can run one verification.
_DUMMY_PASSWORD = "x" * 32
_DUMMY_PASSWORD_HASH: str | None = None
_dummy_hash_lock: asyncio.Lock | None = None
_dummy_hash_loop: asyncio.AbstractEventLoop | None = None


class InvalidEmailError(Exception):
    """The supplied email address failed approved validation."""

    def __init__(self) -> None:
        super().__init__("A valid email address is required")


class InvalidCredentialsError(Exception):
    """Unknown email and incorrect password share this result."""

    def __init__(self) -> None:
        super().__init__("Invalid email or password")


@dataclass(frozen=True)
class LoginResult:
    """Access token plus the raw refresh token for the HTTP cookie only."""

    access_token: str
    refresh_token: str
    expires_in: int = ACCESS_TOKEN_LIFETIME_SECONDS
    token_type: str = "bearer"

    def __repr__(self) -> str:
        return "LoginResult(access_token='[redacted]', refresh_token='[redacted]')"


async def register_account(
    session: AsyncSession,
    email: str,
    password: str,
) -> None:
    """Create an account, or absorb only the users.email unique violation.

    A new account and an already registered normalized email both finish
    without an error. This function does not issue a token or a cookie.
    """
    normalized = _require_email(email)
    password_hash = await hash_password(password)
    try:
        await create_user(session, normalized, password_hash)
        await session.commit()
    except IntegrityError as exc:
        await _rollback(session)
        if _is_users_email_unique_violation(exc):
            return
        raise
    except Exception:
        await _rollback(session)
        raise


async def authenticate(
    session: AsyncSession,
    email: str,
    password: str,
) -> LoginResult:
    """Verify a password, then commit a new refresh family and access token.

    Unknown email and a wrong password raise the same error after one Argon2id
    verification. The raw refresh token is returned only for the cookie.
    """
    normalized = _require_email(email)
    try:
        user = await get_user_by_email(session, normalized)
        if user is None:
            await verify_password(password, await _dummy_password_hash())
            await _rollback(session)
            raise InvalidCredentialsError()
        if not await verify_password(password, user.password_hash):
            await _rollback(session)
            raise InvalidCredentialsError()
        if password_needs_rehash(user.password_hash):
            user.password_hash = await hash_password(password)
        raw_refresh_token = await issue_initial_refresh_token(session, user.id)
        access_token = create_access_token(user.id)
        await session.commit()
    except InvalidCredentialsError:
        raise
    except Exception:
        await _rollback(session)
        raise
    return LoginResult(
        access_token=access_token,
        refresh_token=raw_refresh_token,
    )


def _dummy_hash_guard() -> asyncio.Lock:
    """Return the lock for this event loop.

    ``asyncio.Lock`` binds to the loop that first acquires it. A new loop
    therefore gets a new lock. Callers on one loop share the same lock.
    """
    global _dummy_hash_lock, _dummy_hash_loop
    loop = asyncio.get_running_loop()
    if _dummy_hash_lock is None or _dummy_hash_loop is not loop:
        _dummy_hash_lock = asyncio.Lock()
        _dummy_hash_loop = loop
    return _dummy_hash_lock


async def _dummy_password_hash() -> str:
    """Return the cached dummy hash, creating it once off the event loop.

    The lock stays held while the thread hashes, so concurrent first-time
    callers share one encoded hash.
    """
    global _DUMMY_PASSWORD_HASH
    cached = _DUMMY_PASSWORD_HASH
    if cached is not None:
        return cached
    async with _dummy_hash_guard():
        cached = _DUMMY_PASSWORD_HASH
        if cached is not None:
            return cached
        encoded = await asyncio.to_thread(_hasher.hash, _DUMMY_PASSWORD)
        _DUMMY_PASSWORD_HASH = encoded
        return encoded


def _require_email(email: str) -> str:
    try:
        return parse_email(email)
    except (TypeError, ValidationError):
        raise InvalidEmailError() from None


def _is_users_email_unique_violation(exc: IntegrityError) -> bool:
    """True only for PostgreSQL unique violation of users_email_key."""
    if not isinstance(exc, IntegrityError):
        return False
    for current in _exception_chain(exc):
        if getattr(current, "constraint_name", None) != _EMAIL_CONSTRAINT:
            continue
        sqlstate = getattr(current, "sqlstate", None)
        pgcode = getattr(current, "pgcode", None)
        if sqlstate == _UNIQUE_VIOLATION or pgcode == _UNIQUE_VIOLATION:
            return True
    return False


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    stack = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            stack.append(orig)
        cause = current.__cause__
        if isinstance(cause, BaseException):
            stack.append(cause)


async def _rollback(session: AsyncSession) -> None:
    if session.in_transaction():
        await session.rollback()
