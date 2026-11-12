"""Registration HTTP contract. Persistence uses an in-memory commit boundary."""

import pytest
from fastapi import FastAPI
from tests.auth_support import (
    AuthHarness,
    clear_settings_caches,
    configure_test_environment,
    make_app,
    other_unique_error,
    send,
)

from job_tracker.api.deps import get_session

_MESSAGE = {"message": "Registration request processed. You can now try signing in."}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AuthHarness]:
    configure_test_environment(monkeypatch)
    application = make_app()
    harness = AuthHarness()
    application.dependency_overrides[get_session] = harness.override_session
    yield application, harness
    clear_settings_caches()


def _error(response_json: object) -> dict[str, object]:
    assert isinstance(response_json, dict)
    assert set(response_json) == {"error"}
    error = response_json["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "details"}
    return error


@pytest.mark.asyncio
async def test_registration_normalizes_and_hides_the_account(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, harness = api
    email = "  User.Name+Tag@Example.COM  "
    password = "SecretMarker" + ("c" * 3)
    assert len(password) == 15

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": email, "password": password},
    )

    assert response.status_code == 200
    assert response.json() == _MESSAGE
    assert "set-cookie" not in response.headers
    assert "access_token" not in response.text
    assert "user.name+tag@example.com" not in response.text
    assert "User.Name" not in response.text
    assert password not in response.text
    assert "$argon2" not in response.text
    assert set(harness.store.hashes) == {"user.name+tag@example.com"}
    stored = harness.store.hashes["user.name+tag@example.com"]
    assert stored.startswith("$argon2id$")
    assert password not in stored
    assert harness.store.tokens == []
    assert harness.sessions[-1].commits == 1


@pytest.mark.asyncio
async def test_duplicate_normalized_email_matches_the_success_response(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, harness = api
    password = "SecretMarker" + ("c" * 3)
    first = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": "  User.Name+Tag@Example.COM ", "password": password},
    )
    second = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": " USER.NAME+tag@example.com", "password": password},
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == _MESSAGE
    assert first.content == second.content
    assert set(harness.store.hashes) == {"user.name+tag@example.com"}
    assert "users_email_key" not in second.text
    assert "duplicate" not in second.text.lower()


@pytest.mark.asyncio
async def test_registration_does_not_require_csrf(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, _harness = api

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": "csrf-free@example.com", "password": "c" * 15},
    )

    assert response.status_code == 200
    assert response.json() == _MESSAGE
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "email",
    ["not-an-email", "user@", "leak-marker-not-an-email", "user name@example.com"],
)
async def test_invalid_email_is_a_safe_validation_error(
    api: tuple[FastAPI, AuthHarness],
    email: str,
) -> None:
    application, harness = api

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": email, "password": "c" * 15},
    )

    assert response.status_code == 422
    error = _error(response.json())
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "Request validation failed"
    assert error["details"] == [
        {
            "field": "body.email",
            "code": "INVALID_FORMAT",
            "message": "A valid email address is required",
        }
    ]
    assert email not in response.text
    assert harness.store.hashes == {}
    assert harness.sessions[-1].commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [14, 129])
async def test_password_outside_policy_is_rejected(
    api: tuple[FastAPI, AuthHarness],
    length: int,
) -> None:
    application, harness = api
    password = "SecretMarker" + ("a" * (length - 12))
    assert len(password) == length

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": f"len{length}@example.com", "password": password},
    )

    assert response.status_code == 422
    error = _error(response.json())
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "Request validation failed"
    assert error["details"] == [
        {
            "field": "body.password",
            "code": "PASSWORD_POLICY_VIOLATION",
            "message": "Password must be between 15 and 128 characters",
        }
    ]
    assert "SecretMarker" not in response.text
    assert harness.store.hashes == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [15, 128])
async def test_password_on_policy_boundary_is_accepted(
    api: tuple[FastAPI, AuthHarness],
    length: int,
) -> None:
    application, harness = api
    password = "d" * length

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": f"boundary{length}@example.com", "password": password},
    )

    assert response.status_code == 200
    assert response.json() == _MESSAGE
    assert password not in response.text
    assert f"boundary{length}@example.com" in harness.store.hashes


@pytest.mark.asyncio
async def test_password_is_not_trimmed(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, harness = api
    password = " " * 15

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": "spaces@example.com", "password": password},
    )

    assert response.status_code == 200
    stored = harness.store.hashes["spaces@example.com"]
    assert stored.startswith("$argon2id$")


@pytest.mark.asyncio
async def test_other_integrity_errors_are_not_reported_as_success(
    api: tuple[FastAPI, AuthHarness],
) -> None:
    application, harness = api
    harness.flush_error = other_unique_error()
    password = "SecretMarker" + ("c" * 3)
    email = "leak-integrity@example.com"

    response = await send(
        application,
        "POST",
        "/auth/register",
        json={"email": email, "password": password},
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "details": [],
        }
    }
    assert email not in response.text
    assert password not in response.text
    assert "users_email_key" not in response.text
    assert "refresh_tokens_token_hash_key" not in response.text
    assert "SELECT" not in response.text
    assert harness.store.hashes == {}
    assert harness.sessions[-1].rollbacks == 1
    assert harness.sessions[-1].commits == 0
