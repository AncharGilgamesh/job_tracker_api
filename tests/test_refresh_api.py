"""Refresh and logout HTTP contract without PostgreSQL."""

from uuid import UUID

import pytest
from fastapi import FastAPI
from tests.auth_support import (
    TEST_ORIGIN,
    AuthHarness,
    clear_settings_caches,
    configure_test_environment,
    csrf_headers,
    make_app,
    refresh_cookie,
    send,
)

from job_tracker.api.deps import get_session
from job_tracker.services.access_tokens import decode_access_token
from job_tracker.services.auth import RefreshOutcome, RefreshResult

USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
_INVALID = {
    "error": {
        "code": "INVALID_REFRESH_TOKEN",
        "message": "Invalid refresh token",
        "details": [],
    }
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AuthHarness, list[object]]:
    configure_test_environment(monkeypatch)
    application = make_app()
    harness = AuthHarness()
    application.dependency_overrides[get_session] = harness.override_session
    calls: list[object] = []

    async def rotate(session: object, raw_token: str) -> RefreshResult:
        calls.append(("refresh", raw_token))
        del session
        return RefreshResult(RefreshOutcome.INVALID)

    async def logout(session: object, raw_token: str | None) -> None:
        calls.append(("logout", raw_token))
        del session

    monkeypatch.setattr("job_tracker.api.routers.auth.rotate_refresh_token", rotate)
    monkeypatch.setattr("job_tracker.api.routers.auth.logout_account", logout)
    application.state.refresh_calls = calls
    yield application, harness, calls
    clear_settings_caches()


def _rotated(raw_cookie: str) -> RefreshResult:
    from job_tracker.services.access_tokens import create_access_token

    return RefreshResult(
        RefreshOutcome.ROTATED,
        access_token=create_access_token(USER_ID),
        refresh_token=raw_cookie,
    )


@pytest.mark.asyncio
async def test_refresh_sets_the_replacement_cookie_after_success(
    api: tuple[FastAPI, AuthHarness, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _harness, calls = api

    async def rotate(session: object, raw_token: str) -> RefreshResult:
        calls.append(raw_token)
        del session
        return _rotated("replacement-cookie-value")

    monkeypatch.setattr("job_tracker.api.routers.auth.rotate_refresh_token", rotate)
    response = await send(
        application,
        "POST",
        "/auth/refresh",
        headers={**csrf_headers(), "Cookie": "refresh_token=presented-cookie-value"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert decode_access_token(body["access_token"]) == USER_ID
    morsel = refresh_cookie(response)["refresh_token"]
    assert morsel.value == "replacement-cookie-value"
    assert morsel["httponly"]
    assert morsel["secure"]
    assert morsel["path"] == "/auth"
    assert morsel["max-age"] == "2592000"
    assert morsel["samesite"].lower() == "lax"
    assert morsel["domain"] == ""
    assert "domain=" not in response.headers["set-cookie"].lower()
    assert "replacement-cookie-value" not in response.text
    assert "presented-cookie-value" not in response.text
    if calls != ["presented-cookie-value"]:
        raise AssertionError("refresh did not receive the cookie value")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [RefreshOutcome.INVALID, RefreshOutcome.REPLAY])
async def test_refresh_failure_has_no_cookie_or_authenticate_header(
    api: tuple[FastAPI, AuthHarness, list[object]],
    monkeypatch: pytest.MonkeyPatch,
    outcome: RefreshOutcome,
) -> None:
    application, _harness, _calls = api

    async def rotate(session: object, raw_token: str) -> RefreshResult:
        del session, raw_token
        return RefreshResult(outcome)

    monkeypatch.setattr("job_tracker.api.routers.auth.rotate_refresh_token", rotate)
    response = await send(
        application,
        "POST",
        "/auth/refresh",
        headers={**csrf_headers(), "Cookie": "refresh_token=presented-cookie-value"},
    )

    assert response.status_code == 401
    assert response.json() == _INVALID
    assert "www-authenticate" not in response.headers
    assert "set-cookie" not in response.headers
    assert "presented-cookie-value" not in response.text


@pytest.mark.asyncio
async def test_missing_refresh_cookie_is_invalid_without_calling_rotation(
    api: tuple[FastAPI, AuthHarness, list[object]],
) -> None:
    application, _harness, calls = api

    response = await send(
        application,
        "POST",
        "/auth/refresh",
        headers=csrf_headers(),
    )

    assert response.status_code == 401
    assert response.json() == _INVALID
    assert calls == []
    assert "set-cookie" not in response.headers
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_refresh_and_logout_require_csrf_before_the_cookie_is_used(
    api: tuple[FastAPI, AuthHarness, list[object]],
) -> None:
    application, _harness, calls = api
    refresh = await send(application, "POST", "/auth/refresh")
    logout = await send(application, "POST", "/auth/logout")
    wrong = await send(
        application,
        "POST",
        "/auth/logout",
        headers={"Origin": TEST_ORIGIN, "X-Requested-With": "fetch"},
    )

    for response in (refresh, logout, wrong):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_CHECK_FAILED"
        assert "set-cookie" not in response.headers
    assert calls == []


@pytest.mark.asyncio
async def test_logout_clears_the_cookie_for_every_successful_case(
    api: tuple[FastAPI, AuthHarness, list[object]],
) -> None:
    application, _harness, calls = api
    missing = await send(
        application,
        "POST",
        "/auth/logout",
        headers=csrf_headers(),
    )
    present = await send(
        application,
        "POST",
        "/auth/logout",
        headers={**csrf_headers(), "Cookie": "refresh_token=presented-cookie-value"},
    )

    for response in (missing, present):
        assert response.status_code == 204
        assert response.content == b""
        assert "www-authenticate" not in response.headers
        morsel = refresh_cookie(response)["refresh_token"]
        assert morsel.value == ""
        assert morsel["httponly"]
        assert morsel["secure"]
        assert morsel["path"] == "/auth"
        assert morsel["max-age"] == "0"
        assert morsel["samesite"].lower() == "lax"
        assert morsel["domain"] == ""
        assert "presented-cookie-value" not in response.headers["set-cookie"]
    if calls != [("logout", None), ("logout", "presented-cookie-value")]:
        raise AssertionError("logout did not receive the expected cookie state")


@pytest.mark.asyncio
async def test_logout_database_failure_is_not_success(
    api: tuple[FastAPI, AuthHarness, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _harness, _calls = api

    async def fail(session: object, raw_token: str | None) -> None:
        del session, raw_token
        raise RuntimeError("logout failed")

    monkeypatch.setattr("job_tracker.api.routers.auth.logout_account", fail)
    response = await send(
        application,
        "POST",
        "/auth/logout",
        headers={**csrf_headers(), "Cookie": "refresh_token=presented-cookie-value"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "set-cookie" not in response.headers
    assert "presented-cookie-value" not in response.text


@pytest.mark.asyncio
async def test_local_refresh_and_logout_cookies_omit_secure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_environment(monkeypatch, environment="local")
    application = make_app()
    harness = AuthHarness()
    application.dependency_overrides[get_session] = harness.override_session

    async def rotate(session: object, raw_token: str) -> RefreshResult:
        del session, raw_token
        return _rotated("replacement-cookie-value")

    async def logout(session: object, raw_token: str | None) -> None:
        del session, raw_token

    monkeypatch.setattr("job_tracker.api.routers.auth.rotate_refresh_token", rotate)
    monkeypatch.setattr("job_tracker.api.routers.auth.logout_account", logout)
    try:
        refreshed = await send(
            application,
            "POST",
            "/auth/refresh",
            headers={
                **csrf_headers(),
                "Cookie": "refresh_token=presented-cookie-value",
            },
        )
        signed_out = await send(
            application,
            "POST",
            "/auth/logout",
            headers=csrf_headers(),
        )
    finally:
        clear_settings_caches()

    assert refresh_cookie(refreshed)["refresh_token"]["secure"] == ""
    assert refresh_cookie(signed_out)["refresh_token"]["secure"] == ""
    assert refresh_cookie(signed_out)["refresh_token"]["max-age"] == "0"


@pytest.mark.asyncio
async def test_refresh_and_logout_preflight_does_not_require_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_test_environment(monkeypatch)
    application = make_app()
    try:
        refresh = await send(
            application,
            "OPTIONS",
            "/auth/refresh",
            headers={
                "Origin": TEST_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-requested-with",
            },
        )
        logout = await send(
            application,
            "OPTIONS",
            "/auth/logout",
            headers={
                "Origin": TEST_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
    finally:
        clear_settings_caches()

    assert refresh.status_code == 200
    assert logout.status_code == 200
    assert refresh.headers["access-control-allow-origin"] == TEST_ORIGIN
    assert refresh.headers["access-control-allow-credentials"] == "true"
    assert "CSRF_CHECK_FAILED" not in refresh.text
    assert "*" not in refresh.headers["access-control-allow-origin"]
