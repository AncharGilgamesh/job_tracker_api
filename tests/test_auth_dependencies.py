"""Bearer authentication dependency."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI

from job_tracker.api import deps
from job_tracker.api.deps import get_current_user, get_session
from job_tracker.api.exception_handlers import register_exception_handlers
from job_tracker.db.models.user import User
from job_tracker.services import access_tokens
from job_tracker.services.access_tokens import (
    ACCESS_TOKEN_LIFETIME_SECONDS,
    create_access_token,
)

TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcd"
USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
PASSWORD_HASH = "encoded-password-hash-marker"
_MISSING = {
    "error": {
        "code": "MISSING_ACCESS_TOKEN",
        "message": "Authentication is required",
        "details": [],
    }
}
_INVALID = {
    "error": {
        "code": "INVALID_ACCESS_TOKEN",
        "message": "Invalid access token",
        "details": [],
    }
}


class _SessionProbe:
    """Session stand-in that fails if the dependency manages a transaction."""

    def begin(self) -> None:
        raise AssertionError("the dependency began a transaction")

    async def commit(self) -> None:
        raise AssertionError("the dependency committed the session")

    async def rollback(self) -> None:
        raise AssertionError("the dependency rolled back the session")


class _Harness(NamedTuple):
    app: FastAPI
    calls: list[UUID]
    users: dict[UUID, User]


@pytest.fixture(autouse=True)
def jwt_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point token verification at the shared test settings."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://job_tracker:test@127.0.0.1:1/job_tracker",
    )
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_JWT_SECRET)
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://127.0.0.1:3000")
    access_tokens._load_settings.cache_clear()
    yield
    access_tokens._load_settings.cache_clear()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Serve one protected route without a database session."""
    user = User(id=USER_ID, email="user@example.com", password_hash=PASSWORD_HASH)
    users = {user.id: user}
    calls: list[UUID] = []
    probe = _SessionProbe()

    async def lookup(session: object, user_id: UUID) -> User | None:
        if session is not probe:
            raise AssertionError("lookup received an unexpected session")
        calls.append(user_id)
        return users.get(user_id)

    async def override_session() -> AsyncIterator[_SessionProbe]:
        yield probe

    monkeypatch.setattr(deps, "get_user_by_id", lookup)
    application = FastAPI()
    register_exception_handlers(application)
    application.dependency_overrides[get_session] = override_session

    @application.get("/protected")
    async def protected(current: User = Depends(get_current_user)) -> dict[str, str]:
        return {"id": str(current.id)}

    return _Harness(application, calls, users)


async def _send(
    application: FastAPI,
    path: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.get(path, headers=headers)


def _assert_bearer_failure(response: httpx.Response, body: dict[str, object]) -> None:
    assert response.status_code == 401
    assert response.json() == body
    assert response.headers["www-authenticate"] == "Bearer"
    assert isinstance(response.json()["error"]["details"], list)


@pytest.mark.asyncio
async def test_valid_bearer_token_returns_the_user(harness: _Harness) -> None:
    token = create_access_token(USER_ID)

    response = await _send(
        harness.app,
        "/protected",
        {"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": str(USER_ID)}
    assert harness.calls == [USER_ID]
    assert PASSWORD_HASH not in response.text
    assert token not in response.text
    assert TEST_JWT_SECRET not in response.text


@pytest.mark.asyncio
async def test_lowercase_bearer_scheme_is_accepted(harness: _Harness) -> None:
    token = create_access_token(USER_ID)

    response = await _send(
        harness.app,
        "/protected",
        {"Authorization": f"bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": str(USER_ID)}
    assert token not in response.text


@pytest.mark.asyncio
async def test_missing_authorization_header(harness: _Harness) -> None:
    response = await _send(harness.app, "/protected")

    _assert_bearer_failure(response, _MISSING)
    assert harness.calls == []


@pytest.mark.asyncio
async def test_blank_bearer_token_is_missing_credentials(harness: _Harness) -> None:
    response = await _send(harness.app, "/protected", {"Authorization": "Bearer"})

    _assert_bearer_failure(response, _MISSING)
    assert harness.calls == []


@pytest.mark.asyncio
async def test_unsupported_authentication_scheme(harness: _Harness) -> None:
    response = await _send(
        harness.app,
        "/protected",
        {"Authorization": "Basic basic-credential-marker"},
    )

    _assert_bearer_failure(response, _MISSING)
    assert harness.calls == []
    assert "basic-credential-marker" not in response.text


@pytest.mark.asyncio
async def test_invalid_token(harness: _Harness) -> None:
    response = await _send(
        harness.app,
        "/protected",
        {"Authorization": "Bearer not-a-jwt-marker"},
    )

    _assert_bearer_failure(response, _INVALID)
    assert harness.calls == []
    assert "not-a-jwt-marker" not in response.text


@pytest.mark.asyncio
async def test_expired_token(harness: _Harness) -> None:
    issued_at = int(datetime.now(UTC).timestamp()) - 1_000
    token = jwt.encode(
        {
            "sub": str(USER_ID),
            "iat": issued_at,
            "exp": issued_at + ACCESS_TOKEN_LIFETIME_SECONDS,
            "type": "access",
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )

    response = await _send(
        harness.app,
        "/protected",
        {"Authorization": f"Bearer {token}"},
    )

    _assert_bearer_failure(response, _INVALID)
    assert harness.calls == []
    assert token not in response.text


@pytest.mark.asyncio
async def test_valid_token_for_missing_user_matches_invalid_token(
    harness: _Harness,
) -> None:
    token = create_access_token(USER_ID)
    harness.users.clear()
    missing_user = await _send(
        harness.app,
        "/protected",
        {"Authorization": f"Bearer {token}"},
    )
    invalid = await _send(
        harness.app,
        "/protected",
        {"Authorization": "Bearer not-a-jwt-marker"},
    )

    _assert_bearer_failure(missing_user, _INVALID)
    assert missing_user.status_code == invalid.status_code
    assert missing_user.json() == invalid.json()
    assert (
        missing_user.headers["www-authenticate"] == invalid.headers["www-authenticate"]
    )
    assert harness.calls == [USER_ID]
    assert str(USER_ID) not in missing_user.text
    assert token not in missing_user.text


@pytest.mark.asyncio
async def test_lookup_failure_is_not_reported_as_an_invalid_token(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_lookup(session: object, user_id: UUID) -> User | None:
        del session, user_id
        raise RuntimeError("sensitive-marker-value SELECT password_hash")

    monkeypatch.setattr(deps, "get_user_by_id", broken_lookup)
    token = create_access_token(USER_ID)

    response = await _send(
        harness.app,
        "/protected",
        {"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "details": [],
        }
    }
    assert "sensitive-marker-value" not in response.text
    assert "SELECT" not in response.text
    assert "RuntimeError" not in response.text
    assert token not in response.text


def test_bearer_scheme_is_advertised_in_openapi(harness: _Harness) -> None:
    schema = harness.app.openapi()
    bearer = schema["components"]["securitySchemes"]["HTTPBearer"]
    security = schema["paths"]["/protected"]["get"]["security"]

    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert {"HTTPBearer": []} in security
