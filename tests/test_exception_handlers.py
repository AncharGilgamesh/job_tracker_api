"""D22 API error envelope."""

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, EmailStr

from job_tracker.api.exception_handlers import ApiError, register_exception_handlers
from job_tracker.api.schemas.errors import ErrorDetail

LEAK = "sensitive-marker-value"
SQL = "SELECT password_hash FROM users"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.signature"
PASSWORD = "plaintext-password-should-not-leak"
PASSWORD_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$hashvalue"
REFRESH_TOKEN = "raw-refresh-token-should-not-leak"
TOKEN_HASH = "token-hash-should-not-leak"
_SECRETS = (LEAK, SQL, TOKEN, PASSWORD, PASSWORD_HASH, REFRESH_TOKEN, TOKEN_HASH)


class _SampleBody(BaseModel):
    email: EmailStr
    name: str


class SensitiveBoom(RuntimeError):
    """Raised so tests can prove the class name stays off the response."""


def _application() -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/api-error")
    async def api_error() -> None:
        raise ApiError(
            status_code=401,
            code="MISSING_ACCESS_TOKEN",
            message="Authentication is required",
            details=[
                ErrorDetail(
                    field=None,
                    code="REQUIRED",
                    message="This field is required",
                )
            ],
            headers={"WWW-Authenticate": "Bearer", "X-Request-Id": "safe-id"},
        )

    @application.get("/http-error")
    async def http_error() -> None:
        raise HTTPException(
            status_code=401,
            detail=f"{LEAK} {SQL} {TOKEN} {PASSWORD} {PASSWORD_HASH}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @application.get("/boom")
    async def boom() -> None:
        raise SensitiveBoom(
            f"{LEAK} {SQL} {TOKEN} {PASSWORD} {PASSWORD_HASH} "
            f"{REFRESH_TOKEN} {TOKEN_HASH}"
        )

    @application.get("/crafted-validation")
    async def crafted_validation() -> None:
        raise RequestValidationError(
            [
                {
                    "type": "some_new_error",
                    "loc": (),
                    "msg": f"leak {LEAK} {SQL} {TOKEN}",
                    "input": PASSWORD,
                    "ctx": {"reason": PASSWORD_HASH},
                    "url": "https://errors.pydantic.dev/2.0/v/some_new_error",
                }
            ]
        )

    @application.post("/sample")
    async def sample(body: _SampleBody) -> dict[str, str]:
        return {"email": body.email, "name": body.name}

    return application


@pytest.fixture
def application() -> FastAPI:
    return _application()


async def _send(
    application: FastAPI,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


def _error(response: httpx.Response) -> dict[str, object]:
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "details"}
    assert isinstance(error["details"], list)
    for detail in error["details"]:
        assert isinstance(detail, dict)
        assert set(detail) == {"field", "code", "message"}
    return error


def _assert_no_disclosure(response: httpx.Response) -> None:
    text = response.text
    for secret in _SECRETS:
        assert secret not in text
    assert "Traceback" not in text
    assert '"input"' not in text
    assert '"ctx"' not in text
    assert "errors.pydantic.dev" not in text
    assert "SensitiveBoom" not in text


@pytest.mark.asyncio
async def test_application_error_uses_the_envelope(application: FastAPI) -> None:
    response = await _send(application, "GET", "/api-error")

    assert response.status_code == 401
    error = _error(response)
    assert error["code"] == "MISSING_ACCESS_TOKEN"
    assert error["message"] == "Authentication is required"
    assert error["details"] == [
        {"field": None, "code": "REQUIRED", "message": "This field is required"}
    ]
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-request-id"] == "safe-id"


@pytest.mark.asyncio
async def test_request_validation_maps_public_details(application: FastAPI) -> None:
    missing = await _send(application, "POST", "/sample", json={})
    invalid_email = await _send(
        application,
        "POST",
        "/sample",
        json={"email": f"{LEAK}-not-an-email", "name": "Ada"},
    )
    invalid_type = await _send(
        application,
        "POST",
        "/sample",
        json={"email": "ada@example.com", "name": 5},
    )

    assert missing.status_code == 422
    missing_error = _error(missing)
    assert missing_error["code"] == "VALIDATION_ERROR"
    assert missing_error["message"] == "Request validation failed"
    assert missing_error["details"] == [
        {
            "field": "body.email",
            "code": "REQUIRED",
            "message": "This field is required",
        },
        {
            "field": "body.name",
            "code": "REQUIRED",
            "message": "This field is required",
        },
    ]
    assert "Field required" not in missing.text

    assert invalid_email.status_code == 422
    email_error = _error(invalid_email)
    assert email_error["details"] == [
        {
            "field": "body.email",
            "code": "INVALID_FORMAT",
            "message": "A valid email address is required",
        }
    ]
    assert "value is not a valid email address" not in invalid_email.text
    assert "@-sign" not in invalid_email.text
    _assert_no_disclosure(invalid_email)

    assert invalid_type.status_code == 422
    type_error = _error(invalid_type)
    assert type_error["details"] == [
        {
            "field": "body.name",
            "code": "INVALID_TYPE",
            "message": "A string is required",
        }
    ]
    assert "Input should be a valid string" not in invalid_type.text


@pytest.mark.asyncio
async def test_invalid_json_does_not_echo_the_body(application: FastAPI) -> None:
    response = await _send(
        application,
        "POST",
        "/sample",
        content=f'{{"email": "{LEAK}"'.encode(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "Request validation failed"
    details = error["details"]
    assert isinstance(details, list)
    assert len(details) == 1
    detail = details[0]
    assert isinstance(detail, dict)
    assert detail["code"] == "INVALID_JSON"
    assert detail["message"] == "The request body must be valid JSON"
    assert isinstance(detail["field"], str)
    assert detail["field"].startswith("body")
    assert "JSON decode error" not in response.text
    assert "Expecting" not in response.text
    _assert_no_disclosure(response)


@pytest.mark.asyncio
async def test_unknown_validation_error_hides_parser_context(
    application: FastAPI,
) -> None:
    response = await _send(application, "GET", "/crafted-validation")

    assert response.status_code == 422
    error = _error(response)
    assert error["details"] == [
        {"field": None, "code": "INVALID_VALUE", "message": "The value is invalid"}
    ]
    _assert_no_disclosure(response)


@pytest.mark.asyncio
async def test_http_exception_hides_detail_and_keeps_headers(
    application: FastAPI,
) -> None:
    response = await _send(application, "GET", "/http-error")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication is required",
            "details": [],
        }
    }
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_no_disclosure(response)


@pytest.mark.asyncio
async def test_unexpected_exception_hides_internals(application: FastAPI) -> None:
    response = await _send(application, "GET", "/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "details": [],
        }
    }
    _assert_no_disclosure(response)


@pytest.mark.asyncio
async def test_application_factory_preserves_health_and_wraps_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://job_tracker:test@127.0.0.1:1/job_tracker",
    )
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-jwt-secret-0123456789abcd")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://127.0.0.1:3000")

    from job_tracker.api.deps import get_settings
    from job_tracker.main import create_app

    get_settings.cache_clear()
    application = create_app()
    try:
        async with application.router.lifespan_context(application):
            health = await _send(application, "GET", "/health")
            missing = await _send(application, "GET", "/does-not-exist")
    finally:
        get_settings.cache_clear()

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Not found",
            "details": [],
        }
    }
