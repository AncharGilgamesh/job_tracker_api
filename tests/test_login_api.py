"""Login HTTP contract, cookie attributes, and verification order."""

from uuid import UUID

import pytest
from argon2 import PasswordHasher, Type
from fastapi import FastAPI
from tests.auth_support import (
    TEST_JWT_SECRET,
    AuthHarness,
    clear_settings_caches,
    configure_test_environment,
    csrf_headers,
    make_app,
    refresh_cookie,
    send,
    user_with_hash,
)

from job_tracker.api.deps import get_session
from job_tracker.services import auth as auth_module
from job_tracker.services.access_tokens import decode_access_token
from job_tracker.services.passwords import (
    hash_password,
    password_needs_rehash,
    verify_password,
)

PASSWORD = "c" * 15
USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
EMAIL = "user.name+tag@example.com"
_INVALID = {
    "error": {
        "code": "INVALID_CREDENTIALS",
        "message": "Invalid email or password",
        "details": [],
    }
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AuthHarness]:
    configure_test_environment(monkeypatch)
    application = make_app()
    harness = AuthHarness()
    application.dependency_overrides[get_session] = harness.override_session
    yield application, harness
    clear_settings_caches()


async def _known_hash() -> str:
    encoded = await hash_password(PASSWORD)
    assert password_needs_rehash(encoded) is False
    return encoded


def _outdated_hash(password: str) -> str:
    hasher = PasswordHasher(
        time_cost=3,
        memory_cost=19456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
    encoded = hasher.hash(password)
    assert password_needs_rehash(encoded) is True
    return encoded


@pytest.mark.asyncio
async def test_login_returns_access_token_and_refresh_cookie(
    api: tuple[FastAPI, AuthHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, harness = api
    harness.lookup = user_with_hash(USER_ID, EMAIL, await _known_hash())
    harness.store.hashes[EMAIL] = harness.lookup.password_hash
    monkeypatch.setattr(
        "job_tracker.services.refresh_tokens.secrets.token_urlsafe",
        lambda nbytes: "fixed-refresh-token" if nbytes == 32 else "",
    )

    response = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": "  User.Name+Tag@Example.COM  ", "password": PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert decode_access_token(body["access_token"]) == USER_ID
    morsel = refresh_cookie(response)["refresh_token"]
    assert morsel.value == "fixed-refresh-token"
    assert morsel["httponly"]
    assert morsel["secure"]
    assert morsel["path"] == "/auth"
    assert morsel["max-age"] == "2592000"
    assert morsel["samesite"].lower() == "lax"
    assert morsel["domain"] == ""
    assert "domain=" not in response.headers["set-cookie"].lower()
    assert "fixed-refresh-token" not in response.text
    assert TEST_JWT_SECRET not in response.text
    assert PASSWORD not in response.text
    assert "$argon2" not in response.text
    assert len(harness.store.tokens) == 1
    assert harness.store.tokens[0].token_hash != "fixed-refresh-token"
    assert harness.store.tokens[0].family_id != USER_ID
    assert harness.sessions[-1].commits == 1


@pytest.mark.asyncio
async def test_unknown_email_and_wrong_password_match(
    api: tuple[FastAPI, AuthHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, harness = api
    encoded = await _known_hash()
    harness.lookup = user_with_hash(USER_ID, EMAIL, encoded)
    calls: list[str] = []
    real_verify = verify_password

    async def spy(password: str, encoded_hash: str) -> bool:
        calls.append(encoded_hash)
        return await real_verify(password, encoded_hash)

    monkeypatch.setattr(auth_module, "verify_password", spy)
    wrong = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": EMAIL, "password": "d" * 15},
    )
    wrong_calls = list(calls)
    calls.clear()
    harness.lookup = None
    unknown = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": "missing@example.com", "password": "d" * 15},
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == _INVALID
    assert wrong.content == unknown.content
    assert "www-authenticate" not in wrong.headers
    assert "www-authenticate" not in unknown.headers
    assert "set-cookie" not in wrong.headers
    assert "set-cookie" not in unknown.headers
    assert wrong_calls == [encoded]
    if calls != [auth_module._DUMMY_PASSWORD_HASH]:
        raise AssertionError("unknown email did not verify the cached dummy hash")
    assert len(wrong_calls) == 1
    assert len(calls) == 1
    assert harness.store.tokens == []


@pytest.mark.asyncio
async def test_unknown_email_verifies_once_against_the_dummy_hash(
    api: tuple[FastAPI, AuthHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, harness = api
    harness.lookup = None
    events: list[str] = []
    real_verify = verify_password
    real_hash = hash_password

    async def spy_verify(password: str, encoded_hash: str) -> bool:
        events.append("verify")
        if encoded_hash != auth_module._DUMMY_PASSWORD_HASH:
            raise AssertionError("unknown email did not verify the cached dummy hash")
        return await real_verify(password, encoded_hash)

    async def spy_hash(password: str) -> str:
        events.append("hash")
        return await real_hash(password)

    monkeypatch.setattr(auth_module, "verify_password", spy_verify)
    monkeypatch.setattr(auth_module, "hash_password", spy_hash)

    response = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": "nobody@example.com", "password": "d" * 15},
    )

    assert response.status_code == 401
    assert response.json() == _INVALID
    assert events == ["verify"]
    assert harness.sessions[-1].rollbacks == 1
    assert harness.sessions[-1].commits == 0


@pytest.mark.asyncio
async def test_successful_login_verifies_before_rehash(
    api: tuple[FastAPI, AuthHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, harness = api
    outdated = _outdated_hash(PASSWORD)
    harness.lookup = user_with_hash(USER_ID, EMAIL, outdated)
    harness.store.hashes[EMAIL] = outdated
    events: list[str] = []
    real_verify = verify_password
    real_hash = hash_password

    async def spy_verify(password: str, encoded_hash: str) -> bool:
        events.append("verify")
        return await real_verify(password, encoded_hash)

    async def spy_hash(password: str) -> str:
        events.append("hash")
        return await real_hash(password)

    monkeypatch.setattr(auth_module, "verify_password", spy_verify)
    monkeypatch.setattr(auth_module, "hash_password", spy_hash)

    response = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": EMAIL, "password": PASSWORD},
    )

    assert response.status_code == 200
    assert events == ["verify", "hash"]
    assert password_needs_rehash(harness.store.hashes[EMAIL]) is False
    assert harness.store.hashes[EMAIL] != outdated
    assert len(harness.store.tokens) == 1
    assert harness.sessions[-1].commits == 1


@pytest.mark.asyncio
async def test_rehash_and_refresh_insert_roll_back_together(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, harness = api
    outdated = _outdated_hash(PASSWORD)
    harness.lookup = user_with_hash(USER_ID, EMAIL, outdated)
    harness.store.hashes[EMAIL] = outdated
    harness.fail_commit = True

    response = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": EMAIL, "password": PASSWORD},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "set-cookie" not in response.headers
    assert "access_token" not in response.text
    assert PASSWORD not in response.text
    assert harness.store.hashes[EMAIL] == outdated
    assert harness.store.tokens == []
    assert harness.sessions[-1].rollbacks == 1
    assert harness.sessions[-1].commits == 0


@pytest.mark.asyncio
async def test_short_password_is_still_invalid_credentials(
    api: tuple[FastAPI, AuthHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, harness = api
    harness.lookup = user_with_hash(USER_ID, EMAIL, await _known_hash())
    events: list[str] = []
    real_verify = verify_password

    async def spy(password: str, encoded_hash: str) -> bool:
        events.append("verify")
        return await real_verify(password, encoded_hash)

    monkeypatch.setattr(auth_module, "verify_password", spy)
    password = "d" * 14

    response = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": EMAIL, "password": password},
    )

    assert response.status_code == 401
    assert response.json() == _INVALID
    assert events == ["verify"]
    assert password not in response.text
    assert "set-cookie" not in response.headers
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_login_invalid_email_has_no_cookie(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, harness = api
    email = "leak-marker-not-an-email"

    response = await send(
        application,
        "POST",
        "/auth/login",
        headers=csrf_headers(),
        json={"email": email, "password": PASSWORD},
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {
            "field": "body.email",
            "code": "INVALID_FORMAT",
            "message": "A valid email address is required",
        }
    ]
    assert email not in response.text
    assert "set-cookie" not in response.headers
    assert "www-authenticate" not in response.headers
    assert harness.store.tokens == []


@pytest.mark.asyncio
async def test_local_environment_omits_secure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_environment(monkeypatch, environment="local")
    application = make_app()
    harness = AuthHarness()
    application.dependency_overrides[get_session] = harness.override_session
    harness.lookup = user_with_hash(USER_ID, EMAIL, await _known_hash())
    try:
        response = await send(
            application,
            "POST",
            "/auth/login",
            headers=csrf_headers(),
            json={"email": EMAIL, "password": PASSWORD},
        )
    finally:
        clear_settings_caches()

    assert response.status_code == 200
    morsel = refresh_cookie(response)["refresh_token"]
    assert morsel["secure"] == ""
    assert morsel["httponly"]
    assert morsel["path"] == "/auth"
    assert morsel["max-age"] == "2592000"
    assert morsel["samesite"].lower() == "lax"
    assert morsel["domain"] == ""
