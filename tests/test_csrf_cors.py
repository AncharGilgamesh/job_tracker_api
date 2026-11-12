"""CSRF dependency and credentialed CORS."""

import httpx
import pytest
from fastapi import FastAPI
from tests.auth_support import (
    TEST_ORIGIN,
    clear_settings_caches,
    configure_test_environment,
    csrf_headers,
    make_app,
    send,
)

from job_tracker.api.deps import get_session
from job_tracker.services.auth import InvalidCredentialsError


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    configure_test_environment(
        monkeypatch,
        origins="http://127.0.0.1:3000,https://app.example",
    )
    created = make_app()

    async def unused_session() -> object:
        yield None

    created.dependency_overrides[get_session] = unused_session

    async def reject_login(
        session: object,
        email: str,
        password: str,
    ) -> None:
        del session, email, password
        raise InvalidCredentialsError()

    monkeypatch.setattr("job_tracker.api.routers.auth.authenticate", reject_login)
    yield created
    clear_settings_caches()


async def _login(
    application: FastAPI,
    headers: dict[str, str] | None,
) -> httpx.Response:
    return await send(
        application,
        "POST",
        "/auth/login",
        headers=headers,
        json={"email": "user@example.com", "password": "c" * 15},
    )


def _csrf_body(response: httpx.Response) -> None:
    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "CSRF_CHECK_FAILED",
            "message": "Cross-site request blocked",
            "details": [],
        }
    }
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_allowed_origin_and_custom_header_pass(
    application: FastAPI,
) -> None:
    response = await _login(application, csrf_headers())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_missing_or_incorrect_custom_header_is_rejected(
    application: FastAPI,
) -> None:
    missing = await _login(application, {"Origin": TEST_ORIGIN})
    incorrect = await _login(
        application,
        {"Origin": TEST_ORIGIN, "X-Requested-With": "fetch"},
    )

    _csrf_body(missing)
    _csrf_body(incorrect)


@pytest.mark.asyncio
async def test_missing_origin_and_referer_is_rejected(application: FastAPI) -> None:
    response = await _login(application, {"X-Requested-With": "XMLHttpRequest"})

    _csrf_body(response)


@pytest.mark.asyncio
async def test_referer_is_used_only_when_origin_is_absent(
    application: FastAPI,
) -> None:
    allowed = await _login(
        application,
        {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "http://127.0.0.1:3000/auth/login?next=1",
        },
    )
    rescued = await _login(
        application,
        {
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "http://evil.example",
            "Referer": "http://127.0.0.1:3000/auth/login",
        },
    )

    assert allowed.status_code == 401
    _csrf_body(rescued)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://evil.example",
        "http://127.0.0.1:3000.evil.example",
        "http://127.0.0.1:30000",
        "http://127.0.0.1:3000/",
        "https://127.0.0.1:3000",
        "http://evil.example/http://127.0.0.1:3000",
        "not-a-url",
    ],
)
async def test_origin_must_match_exactly(application: FastAPI, origin: str) -> None:
    response = await _login(
        application,
        {"X-Requested-With": "XMLHttpRequest", "Origin": origin},
    )

    _csrf_body(response)
    assert origin not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "referer",
    [
        "not a url",
        "javascript:alert(1)",
        "http://user:secret-password@127.0.0.1:3000/auth",
        "http://evil.example/auth/login",
        "/auth/login",
    ],
)
async def test_malformed_or_unapproved_referer_is_rejected(
    application: FastAPI,
    referer: str,
) -> None:
    response = await _login(
        application,
        {"X-Requested-With": "XMLHttpRequest", "Referer": referer},
    )

    _csrf_body(response)
    assert "secret-password" not in response.text


@pytest.mark.asyncio
async def test_second_allowlisted_origin_is_accepted(application: FastAPI) -> None:
    response = await _login(
        application,
        csrf_headers("https://app.example"),
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cors_allows_the_exact_origin_and_credentials(
    application: FastAPI,
) -> None:
    health = await send(
        application,
        "GET",
        "/health",
        headers={"Origin": TEST_ORIGIN},
    )
    preflight = await send(
        application,
        "OPTIONS",
        "/auth/login",
        headers={
            "Origin": TEST_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-requested-with",
        },
    )
    denied = await send(
        application,
        "GET",
        "/health",
        headers={"Origin": "http://evil.example"},
    )
    plain = await send(application, "GET", "/health")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["access-control-allow-origin"] == TEST_ORIGIN
    assert health.headers["access-control-allow-credentials"] == "true"
    assert health.headers["access-control-allow-origin"] != "*"
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == TEST_ORIGIN
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert (
        "x-requested-with" in preflight.headers["access-control-allow-headers"].lower()
    )
    assert "*" not in preflight.headers["access-control-allow-origin"]
    assert "*" not in preflight.headers["access-control-allow-methods"]
    denied_origin = denied.headers.get("access-control-allow-origin")
    assert denied_origin != "http://evil.example"
    assert denied_origin != "*"
    assert plain.status_code == 200
    assert plain.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_preflight_does_not_require_csrf_and_post_does(
    application: FastAPI,
) -> None:
    preflight = await send(
        application,
        "OPTIONS",
        "/auth/login",
        headers={
            "Origin": TEST_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-requested-with",
        },
    )
    post = await send(
        application,
        "POST",
        "/auth/login",
        headers={"Origin": TEST_ORIGIN, "Content-Type": "application/json"},
        json={"email": "user@example.com", "password": "c" * 15},
    )

    assert preflight.status_code == 200
    assert "CSRF_CHECK_FAILED" not in preflight.text
    _csrf_body(post)
    assert post.headers["access-control-allow-origin"] == TEST_ORIGIN
