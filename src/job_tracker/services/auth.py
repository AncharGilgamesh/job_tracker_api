"""Register, login, refresh, and logout."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.refresh_token import RefreshToken
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
from job_tracker.services.refresh_tokens import (
    create_replacement_refresh_token,
    get_refresh_token_by_hash,
    hash_refresh_token,
    issue_initial_refresh_token,
    revoke_active_family_tokens,
)
from job_tracker.services.users import (
    create_user,
    get_user_by_email,
    lock_user_by_id,
    parse_email,
)

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
    """Create an account, or absorb only the users.email unique violation."""
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
    """Verify a password, then commit a new refresh family and access token."""
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


class RefreshOutcome(Enum):
    """Service result for refresh. The router maps both failures to one HTTP body."""

    ROTATED = "rotated"
    INVALID = "invalid"
    REPLAY = "replay"


@dataclass(frozen=True)
class RefreshResult:
    """Committed refresh outcome. Raw tokens stay out of the public response."""

    outcome: RefreshOutcome
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int = ACCESS_TOKEN_LIFETIME_SECONDS
    token_type: str = "bearer"

    def __post_init__(self) -> None:
        rotated = self.outcome is RefreshOutcome.ROTATED
        has_token = self.access_token is not None or self.refresh_token is not None
        if rotated and (not self.access_token or not self.refresh_token):
            raise RuntimeError("Refresh rotation result is incomplete.")
        if not rotated and has_token:
            raise RuntimeError("Refresh rejection included a token.")

    def __repr__(self) -> str:
        return f"RefreshResult(outcome={self.outcome.name})"


async def rotate_refresh_token(session: AsyncSession, raw_token: str) -> RefreshResult:
    """Rotate one refresh token, or commit family revocation when it was reused."""
    completed: RefreshResult | None = None
    try:
        token_hash = hash_refresh_token(raw_token)
        presented = await get_refresh_token_by_hash(session, token_hash)
        if presented is None:
            await _rollback(session)
            return RefreshResult(RefreshOutcome.INVALID)
        current = await _locked_token(session, presented.user_id, token_hash)
        if current is None:
            await _rollback(session)
            return RefreshResult(RefreshOutcome.INVALID)
        now = datetime.now(UTC)
        if current.revoked_at is not None:
            await revoke_active_family_tokens(session, current.family_id, now)
            await session.commit()
            completed = RefreshResult(RefreshOutcome.REPLAY)
        elif current.expires_at <= now:
            await _rollback(session)
            return RefreshResult(RefreshOutcome.INVALID)
        else:
            raw_replacement, replacement = await create_replacement_refresh_token(
                session,
                user_id=current.user_id,
                family_id=current.family_id,
                issued_at=now,
            )
            if not isinstance(replacement.id, UUID):
                raise RuntimeError("Refresh token replacement could not be stored.")
            current.revoked_at = now
            current.replaced_by_id = replacement.id
            access_token = create_access_token(current.user_id)
            await session.commit()
            completed = RefreshResult(
                RefreshOutcome.ROTATED,
                access_token=access_token,
                refresh_token=raw_replacement,
            )
    except Exception:
        await _rollback(session)
        raise
    if completed is None:
        raise RuntimeError("Refresh operation did not finish.")
    return completed


async def logout_account(session: AsyncSession, raw_token: str | None) -> None:
    """Revoke the presented token's family, then commit."""
    if not isinstance(raw_token, str) or raw_token == "":
        return
    try:
        token_hash = hash_refresh_token(raw_token)
        presented = await get_refresh_token_by_hash(session, token_hash)
        if presented is None:
            await _rollback(session)
            return
        current = await _locked_token(session, presented.user_id, token_hash)
        if current is None:
            await _rollback(session)
            return
        await revoke_active_family_tokens(
            session,
            current.family_id,
            datetime.now(UTC),
        )
        await session.commit()
    except Exception:
        await _rollback(session)
        raise


async def _locked_token(
    session: AsyncSession,
    user_id: UUID,
    token_hash: str,
) -> RefreshToken | None:
    """Lock the user row, then re-read the token so the decision uses fresh state."""
    locked_user = await lock_user_by_id(session, user_id)
    if locked_user is None:
        return None
    return await get_refresh_token_by_hash(session, token_hash)


def _dummy_hash_guard() -> asyncio.Lock:
    """Return the lock for this event loop."""
    global _dummy_hash_lock, _dummy_hash_loop
    loop = asyncio.get_running_loop()
    if _dummy_hash_lock is None or _dummy_hash_loop is not loop:
        _dummy_hash_lock = asyncio.Lock()
        _dummy_hash_loop = loop
    return _dummy_hash_lock


async def _dummy_password_hash() -> str:
    """Return the cached dummy hash, creating it once off the event loop."""
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
