"""Shared fixtures for authentication tests that do not need PostgreSQL."""

from collections.abc import AsyncIterator
from http.cookies import SimpleCookie
from uuid import UUID

import httpx
from asyncpg.exceptions import UniqueViolationError
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from job_tracker.db.models.refresh_token import RefreshToken
from job_tracker.db.models.user import User

TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcd"
TEST_ORIGIN = "http://127.0.0.1:3000"
TEST_DATABASE_URL = "postgresql+asyncpg://job_tracker:test@127.0.0.1:1/job_tracker"
_EMAIL_CONSTRAINT = "users_email_key"


class AuthStore:
    """State that becomes visible only after a successful commit."""

    def __init__(self) -> None:
        self.hashes: dict[str, str] = {}
        self.tokens: list[RefreshToken] = []


class FakeSession:
    """Session stand-in that publishes rows only inside commit."""

    def __init__(self, store: AuthStore) -> None:
        self.store = store
        self.lookup: User | None = None
        self.pending_users: list[User] = []
        self.pending_tokens: list[RefreshToken] = []
        self.fail_commit = False
        self.flush_error: BaseException | None = None
        self.commits = 0
        self.rollbacks = 0
        self._open = False

    def begin(self) -> None:
        raise AssertionError("session.begin was called")

    def in_transaction(self) -> bool:
        return self._open

    async def execute(self, _statement: object) -> "_Scalar":
        self._open = True
        return _Scalar(self.lookup)

    def add(self, obj: object) -> None:
        self._open = True
        if isinstance(obj, User):
            self.pending_users.append(obj)
            return
        if isinstance(obj, RefreshToken):
            self.pending_tokens.append(obj)
            return
        raise AssertionError("unexpected object added to the session")

    async def flush(self) -> None:
        self._open = True
        if self.flush_error is not None:
            error = self.flush_error
            self.flush_error = None
            raise error
        for user in self.pending_users:
            if user.email in self.store.hashes:
                raise users_email_unique_error()

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit failed")
        await self.flush()
        for user in self.pending_users:
            self.store.hashes[user.email] = user.password_hash
        if self.lookup is not None:
            self.store.hashes[self.lookup.email] = self.lookup.password_hash
        self.store.tokens.extend(self.pending_tokens)
        self.pending_users.clear()
        self.pending_tokens.clear()
        self.commits += 1
        self._open = False

    async def rollback(self) -> None:
        self.pending_users.clear()
        self.pending_tokens.clear()
        self.rollbacks += 1
        self._open = False


class AuthHarness:
    """One store and a session factory for dependency overrides."""

    def __init__(self) -> None:
        self.store = AuthStore()
        self.lookup: User | None = None
        self.fail_commit = False
        self.flush_error: BaseException | None = None
        self.sessions: list[FakeSession] = []

    def open_session(self) -> FakeSession:
        session = FakeSession(self.store)
        session.lookup = self.lookup
        session.fail_commit = self.fail_commit
        if self.flush_error is not None:
            session.flush_error = self.flush_error
            self.flush_error = None
        self.sessions.append(session)
        return session

    async def override_session(self) -> AsyncIterator[FakeSession]:
        yield self.open_session()


class _Scalar:
    def __init__(self, value: User | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> User | None:
        return self._value


def configure_test_environment(
    monkeypatch: object,
    *,
    environment: str = "test",
    origins: str = TEST_ORIGIN,
) -> None:
    """Install explicit test settings without reading a generated secret."""
    setter = getattr(monkeypatch, "setenv")
    setter("ENVIRONMENT", environment)
    setter("DATABASE_URL", TEST_DATABASE_URL)
    setter("JWT_ALGORITHM", "HS256")
    setter("JWT_SECRET_KEY", TEST_JWT_SECRET)
    setter("ALLOWED_ORIGINS", origins)
    from job_tracker.api.deps import get_settings
    from job_tracker.services.access_tokens import _load_settings

    get_settings.cache_clear()
    _load_settings.cache_clear()


def clear_settings_caches() -> None:
    from job_tracker.api.deps import get_settings
    from job_tracker.services.access_tokens import _load_settings

    get_settings.cache_clear()
    _load_settings.cache_clear()


def make_app() -> FastAPI:
    """Create an application after configure_test_environment has run."""
    from job_tracker.main import create_app

    return create_app()


def users_email_unique_error() -> IntegrityError:
    """Build the driver chain PostgreSQL produces for users_email_key."""
    driver_error = UniqueViolationError("duplicate key")
    driver_error.constraint_name = _EMAIL_CONSTRAINT
    return _integrity_error(driver_error)


def other_unique_error() -> IntegrityError:
    """A unique violation for a different constraint."""
    driver_error = UniqueViolationError("duplicate key users_email_key")
    driver_error.constraint_name = "refresh_tokens_token_hash_key"
    return _integrity_error(driver_error)


def _integrity_error(driver_error: UniqueViolationError) -> IntegrityError:
    translated = Exception("translated database error")
    translated.sqlstate = "23505"  # type: ignore[attr-defined]
    translated.pgcode = "23505"  # type: ignore[attr-defined]
    translated.__cause__ = driver_error
    return IntegrityError("INSERT INTO users", {}, translated)


def csrf_headers(origin: str = TEST_ORIGIN) -> dict[str, str]:
    return {"X-Requested-With": "XMLHttpRequest", "Origin": origin}


async def send(
    application: FastAPI,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


def user_with_hash(user_id: UUID, email: str, password_hash: str) -> User:
    return User(id=user_id, email=email, password_hash=password_hash)


def refresh_cookie(response: httpx.Response) -> SimpleCookie:
    """Return the single refresh cookie jar from a response."""
    headers = response.headers.get_list("set-cookie")
    assert len(headers) == 1
    jar: SimpleCookie = SimpleCookie()
    jar.load(headers[0])
    assert list(jar.keys()) == ["refresh_token"]
    return jar
