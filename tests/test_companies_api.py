"""Company catalog HTTP contract."""

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.auth_support import (
    clear_settings_caches,
    configure_test_environment,
    make_app,
    send,
)

from job_tracker.api.deps import get_session
from job_tracker.api.routers import companies as companies_router
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services.access_tokens import create_access_token
from job_tracker.services.users import create_user

_MISSING = {
    "error": {
        "code": "MISSING_ACCESS_TOKEN",
        "message": "Authentication is required",
        "details": [],
    }
}
_INVALID_TOKEN = {
    "error": {
        "code": "INVALID_ACCESS_TOKEN",
        "message": "Invalid access token",
        "details": [],
    }
}
_NOT_FOUND = {
    "error": {
        "code": "RESOURCE_NOT_FOUND",
        "message": "The requested resource was not found",
        "details": [],
    }
}
_FORBIDDEN = {
    "error": {
        "code": "FORBIDDEN",
        "message": "The request is forbidden",
        "details": [],
    }
}
_BLANK_NAME = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.name",
                "code": "INVALID_NAME",
                "message": "A company name is required",
            }
        ],
    }
}
_COMPANY_FIELDS = {
    "id",
    "created_by_user_id",
    "name",
    "normalized_name",
    "archived_at",
}


class _CompaniesApi(NamedTuple):
    app: FastAPI
    session_factory: async_sessionmaker[AsyncSession]


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_companies_api",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def companies_api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_CompaniesApi]:
    """Reset job_tracker_test and serve the real company routes."""
    configure_test_environment(monkeypatch)
    schema = _schema_tests()
    database_url, parsed = schema.require_test_database_url()
    await schema._prepare_database(database_url, parsed)
    await schema._run_alembic(database_url, "upgrade", "head")
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        probe = session_factory()
        try:
            current_database = await probe.scalar(text("SELECT current_database()"))
            version_num = await probe.scalar(text("SHOW server_version_num"))
        finally:
            await probe.close()
        if current_database != schema.TEST_DATABASE_NAME:
            raise RuntimeError(
                "Refusing to test a database other than job_tracker_test."
            )
        if not isinstance(version_num, str) or not version_num.startswith("17"):
            raise RuntimeError("Company tests require PostgreSQL 17.")
        application = make_app()

        async def override() -> AsyncIterator[AsyncSession]:
            session = session_factory()
            try:
                yield session
            finally:
                await session.close()

        application.dependency_overrides[get_session] = override
        yield _CompaniesApi(application, session_factory)
    finally:
        await engine.dispose()
        clear_settings_caches()


async def _account(api: _CompaniesApi, email: str) -> tuple[UUID, dict[str, str]]:
    session = api.session_factory()
    try:
        user = await create_user(session, email, "encoded-hash")
        await session.commit()
        user_id = user.id
    finally:
        await session.close()
    return user_id, {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _iter_routes(routes: object):
    """Yield concrete routes, including routers included by the application."""
    if not isinstance(routes, list | tuple):
        return
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_routes(original.routes)
            continue
        nested = getattr(route, "routes", None)
        methods = getattr(route, "methods", None)
        if nested and not methods:
            yield from _iter_routes(nested)
            continue
        yield route


def _assert_public_error(payload: object) -> None:
    assert isinstance(payload, dict)
    assert set(payload) == {"error"}
    error = payload["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "details"}
    assert isinstance(error["details"], list)
    for detail in error["details"]:
        assert isinstance(detail, dict)
        assert set(detail) == {"field", "code", "message"}


def _company(response: httpx.Response, status_code: int) -> dict[str, object]:
    assert response.history == []
    assert response.status_code == status_code
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == _COMPANY_FIELDS
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "options"),
    [
        ("GET", "/companies", {}),
        ("GET", "/companies/suggestions", {"params": {"name": "Acme"}}),
        ("GET", "/companies/123e4567-e89b-12d3-a456-426614174000", {}),
        ("POST", "/companies", {"json": {"name": "Acme"}}),
        (
            "PATCH",
            "/companies/123e4567-e89b-12d3-a456-426614174000",
            {"json": {"name": "Acme"}},
        ),
        ("POST", "/companies/123e4567-e89b-12d3-a456-426614174000/archive", {}),
    ],
)
async def test_company_routes_require_a_bearer_token(
    companies_api: _CompaniesApi,
    method: str,
    path: str,
    options: dict[str, object],
) -> None:
    response = await send(companies_api.app, method, path, **options)
    assert response.history == []
    assert response.status_code == 401
    assert response.json() == _MISSING
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_public_error(response.json())


@pytest.mark.asyncio
async def test_invalid_bearer_token_is_rejected(companies_api: _CompaniesApi) -> None:
    response = await send(
        companies_api.app,
        "GET",
        "/companies",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    basic = await send(
        companies_api.app,
        "GET",
        "/companies",
        headers={"Authorization": "Basic abc"},
    )
    assert response.status_code == 401
    assert response.json() == _INVALID_TOKEN
    assert response.headers["www-authenticate"] == "Bearer"
    assert "not-a-jwt" not in response.text
    assert basic.status_code == 401
    assert basic.json() == _MISSING
    _assert_public_error(response.json())
    _assert_public_error(basic.json())


@pytest.mark.asyncio
async def test_any_authenticated_user_can_create_list_and_read(
    companies_api: _CompaniesApi,
) -> None:
    creator_id, creator = await _account(companies_api, "creator@example.com")
    _reader_id, reader = await _account(companies_api, "reader@example.com")
    created = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "  Acme   Corporation, LLC "},
        ),
        201,
    )
    assert created["created_by_user_id"] == str(creator_id)
    assert created["name"] == "Acme Corporation, LLC"
    assert created["normalized_name"] == "acme corporation, llc"
    assert created["archived_at"] is None

    listing = await send(companies_api.app, "GET", "/companies", headers=reader)
    assert listing.status_code == 200
    assert set(listing.json()) == {"items"}
    assert listing.json()["items"] == [created]

    fetched = _company(
        await send(
            companies_api.app,
            "GET",
            f"/companies/{created['id']}",
            headers=reader,
        ),
        200,
    )
    assert fetched == created


@pytest.mark.asyncio
async def test_duplicate_company_creation_is_not_rejected(
    companies_api: _CompaniesApi,
) -> None:
    _creator_id, creator = await _account(companies_api, "creator@example.com")
    _other_id, other = await _account(companies_api, "other@example.com")
    first = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "Acme"},
        ),
        201,
    )
    second = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=other,
            json={"name": "  ACME "},
        ),
        201,
    )
    assert first["id"] != second["id"]
    assert first["normalized_name"] == second["normalized_name"] == "acme"
    assert first["name"] == "Acme"
    assert second["name"] == "ACME"


@pytest.mark.asyncio
async def test_suggestions_use_exact_normalized_names(
    companies_api: _CompaniesApi,
) -> None:
    _creator_id, creator = await _account(companies_api, "creator@example.com")
    paths = [
        route.path
        for route in _iter_routes(companies_api.app.routes)
        if isinstance(route, APIRoute)
        and "GET" in route.methods
        and route.path.startswith("/companies")
    ]
    assert paths.index("/companies/suggestions") < paths.index(
        "/companies/{company_id}"
    )
    first = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "  Acme   Corporation "},
        ),
        201,
    )
    second = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "ACME CORPORATION"},
        ),
        201,
    )
    archived = await send(
        companies_api.app,
        "POST",
        f"/companies/{second['id']}/archive",
        headers=creator,
    )
    assert archived.status_code == 200
    exact = await send(
        companies_api.app,
        "GET",
        "/companies/suggestions",
        headers=creator,
        params={"name": "  acme   corporation "},
    )
    assert exact.status_code == 200
    assert {item["id"] for item in exact.json()["items"]} == {
        first["id"],
        second["id"],
    }
    empty = await send(
        companies_api.app,
        "GET",
        "/companies/suggestions",
        headers=creator,
        params={"name": "Acme"},
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": []}
    missing_query = await send(
        companies_api.app,
        "GET",
        "/companies/suggestions",
        headers=creator,
    )
    assert missing_query.status_code == 422
    assert missing_query.json()["error"]["details"] == [
        {
            "field": "query.name",
            "code": "REQUIRED",
            "message": "This field is required",
        }
    ]
    _assert_public_error(missing_query.json())


@pytest.mark.asyncio
async def test_creator_rename_and_archive_are_idempotent_for_the_timestamp(
    companies_api: _CompaniesApi,
) -> None:
    creator_id, creator = await _account(companies_api, "creator@example.com")
    _reader_id, reader = await _account(companies_api, "reader@example.com")
    created = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "Acme"},
        ),
        201,
    )
    archived = _company(
        await send(
            companies_api.app,
            "POST",
            f"/companies/{created['id']}/archive",
            headers=creator,
        ),
        200,
    )
    stamp = archived["archived_at"]
    assert isinstance(stamp, str)
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)
    await asyncio.sleep(0.05)
    repeated = _company(
        await send(
            companies_api.app,
            "POST",
            f"/companies/{created['id']}/archive",
            headers=creator,
        ),
        200,
    )
    assert repeated["archived_at"] == stamp
    renamed = _company(
        await send(
            companies_api.app,
            "PATCH",
            f"/companies/{created['id']}",
            headers=creator,
            json={"name": "  Renamed   LLC "},
        ),
        200,
    )
    assert renamed["name"] == "Renamed LLC"
    assert renamed["normalized_name"] == "renamed llc"
    assert renamed["created_by_user_id"] == str(creator_id)
    assert renamed["archived_at"] == stamp
    visible = _company(
        await send(
            companies_api.app,
            "GET",
            f"/companies/{created['id']}",
            headers=reader,
        ),
        200,
    )
    assert visible == renamed
    listing = await send(companies_api.app, "GET", "/companies", headers=reader)
    assert listing.json()["items"] == [renamed]


@pytest.mark.asyncio
async def test_missing_and_non_creator_outcomes_differ(
    companies_api: _CompaniesApi,
) -> None:
    assert _NOT_FOUND != _FORBIDDEN
    _creator_id, creator = await _account(companies_api, "creator@example.com")
    _other_id, other = await _account(companies_api, "other@example.com")
    created = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "Acme LLC"},
        ),
        201,
    )
    company_id = created["id"]
    denied = (
        ("PATCH", f"/companies/{company_id}", {"json": {"name": "Hijacked"}}),
        ("POST", f"/companies/{company_id}/archive", {}),
    )
    for method, path, options in denied:
        response = await send(companies_api.app, method, path, headers=other, **options)
        assert response.status_code == 403
        assert response.json() == _FORBIDDEN
        assert "Hijacked" not in response.text
        _assert_public_error(response.json())
    unchanged = _company(
        await send(
            companies_api.app,
            "GET",
            f"/companies/{company_id}",
            headers=creator,
        ),
        200,
    )
    assert unchanged["name"] == "Acme LLC"
    assert unchanged["archived_at"] is None

    missing = uuid4()
    probes = (
        ("GET", f"/companies/{missing}", {}),
        ("PATCH", f"/companies/{missing}", {"json": {"name": "Nope"}}),
        ("POST", f"/companies/{missing}/archive", {}),
    )
    for actor in (creator, other):
        for method, path, options in probes:
            response = await send(
                companies_api.app,
                method,
                path,
                headers=actor,
                **options,
            )
            assert response.status_code == 404
            assert response.json() == _NOT_FOUND
            assert str(missing) not in response.text
            _assert_public_error(response.json())


@pytest.mark.asyncio
async def test_blank_names_and_immutable_fields_are_rejected(
    companies_api: _CompaniesApi,
) -> None:
    creator_id, creator = await _account(companies_api, "creator@example.com")
    for name in ("", "   ", "\t\n"):
        response = await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": name},
        )
        assert response.status_code == 422
        assert response.json() == _BLANK_NAME
        _assert_public_error(response.json())
    created = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "Acme"},
        ),
        201,
    )
    blank_rename = await send(
        companies_api.app,
        "PATCH",
        f"/companies/{created['id']}",
        headers=creator,
        json={"name": "   "},
    )
    assert blank_rename.status_code == 422
    assert blank_rename.json() == _BLANK_NAME

    marker = "immutable-field-leak-marker"
    rejected = await send(
        companies_api.app,
        "PATCH",
        f"/companies/{created['id']}",
        headers=creator,
        json={
            "name": "Should Not Save",
            "id": str(uuid4()),
            "created_by_user_id": marker,
            "normalized_name": marker,
            "archived_at": "2000-01-01T00:00:00Z",
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert rejected.json()["error"]["message"] == "Request validation failed"
    details = rejected.json()["error"]["details"]
    assert {item["field"] for item in details} == {
        "body.id",
        "body.created_by_user_id",
        "body.normalized_name",
        "body.archived_at",
    }
    assert {item["code"] for item in details} == {"UNEXPECTED_FIELD"}
    assert {item["message"] for item in details} == {"This field is not allowed"}
    assert marker not in rejected.text
    assert "Should Not Save" not in rejected.text
    _assert_public_error(rejected.json())

    extra_create = await send(
        companies_api.app,
        "POST",
        "/companies",
        headers=creator,
        json={"name": "Other Co", "normalized_name": marker},
    )
    assert extra_create.status_code == 422
    assert marker not in extra_create.text
    listing = await send(companies_api.app, "GET", "/companies", headers=creator)
    assert listing.json()["items"] == [created]
    assert created["created_by_user_id"] == str(creator_id)


@pytest.mark.asyncio
async def test_company_errors_hide_validation_input_and_database_details(
    companies_api: _CompaniesApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _creator_id, creator = await _account(companies_api, "creator@example.com")
    invalid_id = await send(
        companies_api.app,
        "GET",
        "/companies/not-a-uuid",
        headers=creator,
    )
    assert invalid_id.status_code == 422
    assert invalid_id.json()["error"]["details"] == [
        {
            "field": "path.company_id",
            "code": "INVALID_FORMAT",
            "message": "A valid UUID is required",
        }
    ]
    assert "not-a-uuid" not in invalid_id.text
    _assert_public_error(invalid_id.json())

    marker = "sql-leak-marker"

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise SQLAlchemyError(
            f"SELECT password_hash FROM companies WHERE name = '{marker}'"
        )

    monkeypatch.setattr(companies_router.company_service, "create_company", explode)
    response = await send(
        companies_api.app,
        "POST",
        "/companies",
        headers=creator,
        json={"name": "Acme"},
    )
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred",
            "details": [],
        }
    }
    assert marker not in response.text
    assert "password_hash" not in response.text
    assert "SELECT" not in response.text
    assert "SQLAlchemyError" not in response.text
    _assert_public_error(response.json())
    listing = await send(companies_api.app, "GET", "/companies", headers=creator)
    assert listing.json() == {"items": []}


@pytest.mark.asyncio
async def test_company_catalog_has_no_delete_route(
    companies_api: _CompaniesApi,
) -> None:
    _creator_id, creator = await _account(companies_api, "creator@example.com")
    company_routes = [
        route
        for route in _iter_routes(companies_api.app.routes)
        if isinstance(getattr(route, "path", None), str)
        and route.path.startswith("/companies")
    ]
    assert company_routes
    for route in company_routes:
        assert "DELETE" not in route.methods
    created = _company(
        await send(
            companies_api.app,
            "POST",
            "/companies",
            headers=creator,
            json={"name": "Acme"},
        ),
        201,
    )
    deleted = await send(
        companies_api.app,
        "DELETE",
        f"/companies/{created['id']}",
        headers=creator,
    )
    assert deleted.status_code == 405
    assert deleted.json() == {
        "error": {
            "code": "METHOD_NOT_ALLOWED",
            "message": "The method is not allowed",
            "details": [],
        }
    }
    _assert_public_error(deleted.json())
    remaining = _company(
        await send(
            companies_api.app,
            "GET",
            f"/companies/{created['id']}",
            headers=creator,
        ),
        200,
    )
    assert remaining == created


@pytest.mark.asyncio
async def test_health_stays_public_and_login_keeps_its_csrf_check(
    companies_api: _CompaniesApi,
) -> None:
    health = await send(companies_api.app, "GET", "/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    login = await send(
        companies_api.app,
        "POST",
        "/auth/login",
        json={"email": "user@example.com", "password": "c" * 15},
    )
    assert login.status_code == 403
    assert login.json()["error"]["code"] == "CSRF_CHECK_FAILED"
    assert login.json()["error"]["details"] == []
