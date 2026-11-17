"""Vacancy create and read HTTP contract."""

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.auth_support import (
    clear_settings_caches,
    configure_test_environment,
    make_app,
    send,
)

from job_tracker.api.deps import get_session
from job_tracker.api.schemas.vacancies import UpdateVacancyRequest
from job_tracker.db.models.application import Application
from job_tracker.db.models.application_status_history import ApplicationStatusHistory
from job_tracker.db.models.interview import Interview
from job_tracker.db.models.vacancy import Vacancy
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
_NOT_FOUND = {
    "error": {
        "code": "RESOURCE_NOT_FOUND",
        "message": "The requested resource was not found",
        "details": [],
    }
}
_ARCHIVED = {
    "error": {
        "code": "COMPANY_ARCHIVED",
        "message": "The company is archived",
        "details": [],
    }
}
_HAS_APPLICATION = {
    "error": {
        "code": "VACANCY_HAS_APPLICATION",
        "message": "The vacancy has an application",
        "details": [],
    }
}
_BLANK_TITLE = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.title",
                "code": "INVALID_VALUE",
                "message": "A title is required",
            }
        ],
    }
}
_NULL_COMPANY = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.company_id",
                "code": "INVALID_FORMAT",
                "message": "A valid UUID is required",
            }
        ],
    }
}
_EMPTY_PATCH = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body",
                "code": "INVALID_VALUE",
                "message": "At least one field is required",
            }
        ],
    }
}
_VACANCY_FIELDS = {
    "id",
    "user_id",
    "company_id",
    "title",
    "description",
    "source_url",
    "location",
}


class _VacanciesApi(NamedTuple):
    app: FastAPI
    session_factory: async_sessionmaker[AsyncSession]


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_vacancies_api",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def vacancies_api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_VacanciesApi]:
    """Reset job_tracker_test and serve the real vacancy routes."""
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
            raise RuntimeError("Vacancy tests require PostgreSQL 17.")
        application = make_app()

        async def override() -> AsyncIterator[AsyncSession]:
            session = session_factory()
            try:
                yield session
            finally:
                await session.close()

        application.dependency_overrides[get_session] = override
        yield _VacanciesApi(application, session_factory)
    finally:
        await engine.dispose()
        clear_settings_caches()


async def _account(api: _VacanciesApi, email: str) -> tuple[UUID, dict[str, str]]:
    session = api.session_factory()
    try:
        user = await create_user(session, email, "encoded-hash")
        await session.commit()
        user_id = user.id
    finally:
        await session.close()
    return user_id, {"Authorization": f"Bearer {create_access_token(user_id)}"}


async def _vacancy_count(api: _VacanciesApi) -> int:
    session = api.session_factory()
    try:
        count = await session.scalar(text("SELECT COUNT(*) FROM vacancies"))
    finally:
        await session.close()
    if not isinstance(count, int):
        raise AssertionError("Vacancy count was not an integer.")
    return count


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


def _vacancy(response: httpx.Response, status_code: int) -> dict[str, object]:
    assert response.history == []
    assert response.status_code == status_code
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == _VACANCY_FIELDS
    return body


async def _create_company(
    api: _VacanciesApi,
    headers: dict[str, str],
    name: str,
) -> str:
    response = await send(
        api.app,
        "POST",
        "/companies",
        headers=headers,
        json={"name": name},
    )
    assert response.status_code == 201
    company_id = response.json()["id"]
    assert isinstance(company_id, str)
    return company_id


async def _attach_application(
    api: _VacanciesApi,
    vacancy_id: UUID,
    changed_at: datetime,
) -> tuple[UUID, UUID, UUID]:
    """Insert one applied application, its first history row, and one interview."""
    session = api.session_factory()
    try:
        application = Application(vacancy_id=vacancy_id, status="APPLIED")
        session.add(application)
        await session.flush()
        history = ApplicationStatusHistory(
            application_id=application.id,
            from_status=None,
            to_status="APPLIED",
            changed_at=changed_at,
        )
        interview = Interview(
            application_id=application.id,
            scheduled_at=datetime(2026, 4, 1, 16, 0, tzinfo=UTC),
        )
        session.add(history)
        session.add(interview)
        await session.flush()
        application_id = application.id
        history_id = history.id
        interview_id = interview.id
        await session.commit()
        return application_id, history_id, interview_id
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "options"),
    [
        (
            "POST",
            "/vacancies",
            {
                "json": {
                    "company_id": "123e4567-e89b-12d3-a456-426614174000",
                    "title": "Role",
                }
            },
        ),
        ("GET", "/vacancies", {}),
        ("GET", "/vacancies/123e4567-e89b-12d3-a456-426614174000", {}),
        (
            "PATCH",
            "/vacancies/123e4567-e89b-12d3-a456-426614174000",
            {"json": {"title": "Role"}},
        ),
        ("DELETE", "/vacancies/123e4567-e89b-12d3-a456-426614174000", {}),
    ],
)
async def test_vacancy_routes_require_a_bearer_token_and_not_csrf(
    vacancies_api: _VacanciesApi,
    method: str,
    path: str,
    options: dict[str, object],
) -> None:
    response = await send(vacancies_api.app, method, path, **options)
    assert response.history == []
    assert response.status_code == 401
    assert response.json() == _MISSING
    assert response.headers["www-authenticate"] == "Bearer"
    assert "csrf" not in response.text.lower()
    _assert_public_error(response.json())


@pytest.mark.asyncio
async def test_create_persists_owner_nullable_fields_and_get_returns_it(
    vacancies_api: _VacanciesApi,
) -> None:
    owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={
                "company_id": company_id,
                "title": "Backend Engineer",
                "description": "Build APIs",
                "source_url": "not a url",
                "location": "Remote",
            },
        ),
        201,
    )
    assert created["user_id"] == str(owner_id)
    assert created["company_id"] == company_id
    assert created["title"] == "Backend Engineer"
    assert created["description"] == "Build APIs"
    assert created["source_url"] == "not a url"
    assert created["location"] == "Remote"

    fetched = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert fetched == created

    omitted = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Second Role"},
        ),
        201,
    )
    assert omitted["user_id"] == str(owner_id)
    assert omitted["description"] is None
    assert omitted["source_url"] is None
    assert omitted["location"] is None
    explicit_null = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={
                "company_id": company_id,
                "title": "Third Role",
                "description": None,
                "source_url": None,
                "location": None,
            },
        ),
        201,
    )
    assert explicit_null["description"] is None
    assert explicit_null["source_url"] is None
    assert explicit_null["location"] is None


@pytest.mark.asyncio
async def test_body_user_id_is_rejected(vacancies_api: _VacanciesApi) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    marker = str(uuid4())
    rejected = await send(
        vacancies_api.app,
        "POST",
        "/vacancies",
        headers=owner,
        json={
            "company_id": company_id,
            "title": "Should Not Save",
            "user_id": marker,
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert rejected.json()["error"]["message"] == "Request validation failed"
    assert rejected.json()["error"]["details"] == [
        {
            "field": "body.user_id",
            "code": "UNEXPECTED_FIELD",
            "message": "This field is not allowed",
        }
    ]
    assert marker not in rejected.text
    assert "Should Not Save" not in rejected.text
    _assert_public_error(rejected.json())
    assert await _vacancy_count(vacancies_api) == 0


@pytest.mark.asyncio
async def test_missing_and_foreign_vacancies_return_the_same_not_found(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    _other_id, other = await _account(vacancies_api, "other@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Owner Role"},
        ),
        201,
    )
    missing_id = uuid4()
    missing = await send(
        vacancies_api.app,
        "GET",
        f"/vacancies/{missing_id}",
        headers=owner,
    )
    foreign = await send(
        vacancies_api.app,
        "GET",
        f"/vacancies/{created['id']}",
        headers=other,
    )
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.json() == foreign.json() == _NOT_FOUND
    assert str(missing_id) not in missing.text
    assert str(created["id"]) not in foreign.text
    assert str(_other_id) not in foreign.text
    _assert_public_error(missing.json())
    _assert_public_error(foreign.json())
    still_there = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert still_there == created


@pytest.mark.asyncio
async def test_missing_company_returns_not_found_and_inserts_nothing(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    missing_company = uuid4()
    response = await send(
        vacancies_api.app,
        "POST",
        "/vacancies",
        headers=owner,
        json={"company_id": str(missing_company), "title": "Orphan Role"},
    )
    assert response.status_code == 404
    assert response.json() == _NOT_FOUND
    assert str(missing_company) not in response.text
    _assert_public_error(response.json())
    assert await _vacancy_count(vacancies_api) == 0


@pytest.mark.asyncio
async def test_archived_company_returns_conflict_and_inserts_nothing(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Old Co")
    archived = await send(
        vacancies_api.app,
        "POST",
        f"/companies/{company_id}/archive",
        headers=owner,
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    response = await send(
        vacancies_api.app,
        "POST",
        "/vacancies",
        headers=owner,
        json={"company_id": company_id, "title": "Too Late"},
    )
    assert response.status_code == 409
    assert response.json() == _ARCHIVED
    assert company_id not in response.text
    _assert_public_error(response.json())
    assert await _vacancy_count(vacancies_api) == 0


@pytest.mark.asyncio
async def test_whitespace_title_is_rejected(vacancies_api: _VacanciesApi) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    for title in ("", "   ", "\t\n"):
        response = await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": title},
        )
        assert response.status_code == 422
        assert response.json() == _BLANK_TITLE
        assert len(response.json()["error"]["details"]) == 1
        _assert_public_error(response.json())
    assert await _vacancy_count(vacancies_api) == 0


@pytest.mark.asyncio
async def test_title_with_surrounding_spaces_is_stored_unchanged(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    title = "  Senior   Engineer  "
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": title},
        ),
        201,
    )
    assert created["title"] == title
    fetched = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert fetched["title"] == title
    session = vacancies_api.session_factory()
    try:
        stored = await session.scalar(
            text("SELECT title FROM vacancies WHERE id = :vacancy_id"),
            {"vacancy_id": UUID(str(created["id"]))},
        )
    finally:
        await session.close()
    assert stored == title


@pytest.mark.asyncio
async def test_archiving_the_company_leaves_the_vacancy_readable(
    vacancies_api: _VacanciesApi,
) -> None:
    owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={
                "company_id": company_id,
                "title": "Kept Role",
                "source_url": "not a url",
            },
        ),
        201,
    )
    archived = await send(
        vacancies_api.app,
        "POST",
        f"/companies/{company_id}/archive",
        headers=owner,
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    fetched = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert fetched == created
    assert fetched["user_id"] == str(owner_id)
    assert fetched["company_id"] == company_id


@pytest.mark.asyncio
async def test_two_users_can_create_vacancies_for_one_active_company(
    vacancies_api: _VacanciesApi,
) -> None:
    first_id, first = await _account(vacancies_api, "first@example.com")
    second_id, second = await _account(vacancies_api, "second@example.com")
    company_id = await _create_company(vacancies_api, first, "Shared Co")
    first_vacancy = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=first,
            json={"company_id": company_id, "title": "First Role"},
        ),
        201,
    )
    second_vacancy = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=second,
            json={"company_id": company_id, "title": "Second Role"},
        ),
        201,
    )
    assert first_vacancy["id"] != second_vacancy["id"]
    assert first_vacancy["company_id"] == second_vacancy["company_id"] == company_id
    assert first_vacancy["user_id"] == str(first_id)
    assert second_vacancy["user_id"] == str(second_id)
    assert await _vacancy_count(vacancies_api) == 2


def test_patch_schema_keeps_omitted_defaults_out_of_the_change_set() -> None:
    omitted = UpdateVacancyRequest.model_validate({"title": "  Role  "})
    assert omitted.model_fields_set == {"title"}
    assert omitted.model_dump(exclude_unset=True) == {"title": "  Role  "}
    explicit_null = UpdateVacancyRequest.model_validate(
        {"description": None, "source_url": None, "location": None}
    )
    assert explicit_null.model_dump(exclude_unset=True) == {
        "description": None,
        "source_url": None,
        "location": None,
    }
    assert "title" not in explicit_null.model_fields_set
    assert "company_id" not in explicit_null.model_fields_set


@pytest.mark.asyncio
async def test_list_returns_only_the_callers_vacancies_in_id_order(
    vacancies_api: _VacanciesApi,
) -> None:
    owner_id, owner = await _account(vacancies_api, "owner@example.com")
    other_id, _other = await _account(vacancies_api, "other@example.com")
    _empty_id, empty = await _account(vacancies_api, "empty@example.com")
    company_id = UUID(await _create_company(vacancies_api, owner, "Acme"))
    later_id = UUID("00000000-0000-4000-8000-000000000020")
    earlier_id = UUID("00000000-0000-4000-8000-000000000010")
    hidden_id = UUID("00000000-0000-4000-8000-000000000015")
    session = vacancies_api.session_factory()
    try:
        session.add(
            Vacancy(
                id=later_id,
                user_id=owner_id,
                company_id=company_id,
                title="Created first",
            )
        )
        await session.commit()
        session.add(
            Vacancy(
                id=earlier_id,
                user_id=owner_id,
                company_id=company_id,
                title="Created second",
            )
        )
        session.add(
            Vacancy(
                id=hidden_id,
                user_id=other_id,
                company_id=company_id,
                title="Someone else",
            )
        )
        await session.commit()
    finally:
        await session.close()

    listed = await send(vacancies_api.app, "GET", "/vacancies", headers=owner)
    assert listed.status_code == 200
    assert set(listed.json()) == {"items"}
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [str(earlier_id), str(later_id)]
    assert [item["title"] for item in items] == ["Created second", "Created first"]
    assert all(set(item) == _VACANCY_FIELDS for item in items)
    assert str(hidden_id) not in listed.text
    assert "Someone else" not in listed.text
    empty_list = await send(vacancies_api.app, "GET", "/vacancies", headers=empty)
    assert empty_list.status_code == 200
    assert empty_list.json() == {"items": []}


@pytest.mark.asyncio
async def test_patch_omitted_fields_stay_and_explicit_null_clears(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={
                "company_id": company_id,
                "title": "Backend Engineer",
                "description": "Build APIs",
                "source_url": "board/123",
                "location": "Remote",
            },
        ),
        201,
    )
    described = _vacancy(
        await send(
            vacancies_api.app,
            "PATCH",
            f"/vacancies/{created['id']}",
            headers=owner,
            json={"description": "Updated notes"},
        ),
        200,
    )
    assert described["description"] == "Updated notes"
    assert described["title"] == "Backend Engineer"
    assert described["source_url"] == "board/123"
    assert described["location"] == "Remote"
    assert described["company_id"] == company_id
    sourced = _vacancy(
        await send(
            vacancies_api.app,
            "PATCH",
            f"/vacancies/{created['id']}",
            headers=owner,
            json={"source_url": "not a url"},
        ),
        200,
    )
    assert sourced["source_url"] == "not a url"
    assert sourced["description"] == "Updated notes"
    assert sourced["title"] == "Backend Engineer"
    assert sourced["location"] == "Remote"
    assert sourced["company_id"] == company_id
    titled = _vacancy(
        await send(
            vacancies_api.app,
            "PATCH",
            f"/vacancies/{created['id']}",
            headers=owner,
            json={"title": "  Senior   Engineer  "},
        ),
        200,
    )
    assert titled["title"] == "  Senior   Engineer  "
    assert titled["description"] == "Updated notes"
    assert titled["source_url"] == "not a url"
    assert titled["location"] == "Remote"
    assert titled["company_id"] == company_id
    cleared = _vacancy(
        await send(
            vacancies_api.app,
            "PATCH",
            f"/vacancies/{created['id']}",
            headers=owner,
            json={"description": None, "source_url": None, "location": None},
        ),
        200,
    )
    assert cleared["description"] is None
    assert cleared["source_url"] is None
    assert cleared["location"] is None
    assert cleared["title"] == "  Senior   Engineer  "
    assert cleared["company_id"] == company_id


@pytest.mark.asyncio
async def test_patch_rejects_null_blank_and_empty_without_changing_the_row(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={
                "company_id": company_id,
                "title": "Backend Engineer",
                "description": "Keep me",
                "location": "Remote",
            },
        ),
        201,
    )
    path = f"/vacancies/{created['id']}"
    null_title = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={"title": None},
    )
    assert null_title.status_code == 422
    assert null_title.json() == _BLANK_TITLE
    _assert_public_error(null_title.json())
    null_company = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={"company_id": None},
    )
    assert null_company.status_code == 422
    assert null_company.json() == _NULL_COMPANY
    _assert_public_error(null_company.json())
    blank_title = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={"title": " \t "},
    )
    assert blank_title.status_code == 422
    assert blank_title.json() == _BLANK_TITLE
    assert len(blank_title.json()["error"]["details"]) == 1
    empty = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={},
    )
    assert empty.status_code == 422
    assert empty.json() == _EMPTY_PATCH
    assert empty.json()["error"]["details"][0]["field"] == "body"
    _assert_public_error(empty.json())
    marker = str(uuid4())
    extra_user = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={"title": "Should Not Save", "user_id": marker},
    )
    assert extra_user.status_code == 422
    assert extra_user.json()["error"]["details"] == [
        {
            "field": "body.user_id",
            "code": "UNEXPECTED_FIELD",
            "message": "This field is not allowed",
        }
    ]
    assert marker not in extra_user.text
    assert "Should Not Save" not in extra_user.text
    stored = _vacancy(
        await send(vacancies_api.app, "GET", path, headers=owner),
        200,
    )
    assert stored == created


@pytest.mark.asyncio
async def test_patch_missing_and_foreign_vacancies_return_the_same_not_found(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    _other_id, other = await _account(vacancies_api, "other@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Owner Role"},
        ),
        201,
    )
    missing_id = uuid4()
    missing = await send(
        vacancies_api.app,
        "PATCH",
        f"/vacancies/{missing_id}",
        headers=owner,
        json={"title": "Nope"},
    )
    foreign = await send(
        vacancies_api.app,
        "PATCH",
        f"/vacancies/{created['id']}",
        headers=other,
        json={"title": "Stolen"},
    )
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.json() == foreign.json() == _NOT_FOUND
    assert str(missing_id) not in missing.text
    assert "Stolen" not in foreign.text
    assert "Nope" not in missing.text
    _assert_public_error(missing.json())
    _assert_public_error(foreign.json())
    stored = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert stored == created


@pytest.mark.asyncio
async def test_patch_company_change_rejects_missing_and_archived_companies(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Current Co")
    archived_id = await _create_company(vacancies_api, owner, "Old Co")
    archived = await send(
        vacancies_api.app,
        "POST",
        f"/companies/{archived_id}/archive",
        headers=owner,
    )
    assert archived.status_code == 200
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Role"},
        ),
        201,
    )
    path = f"/vacancies/{created['id']}"
    missing_company = uuid4()
    missing = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={"company_id": str(missing_company), "title": "Should Not Save"},
    )
    assert missing.status_code == 404
    assert missing.json() == _NOT_FOUND
    assert str(missing_company) not in missing.text
    _assert_public_error(missing.json())
    conflict = await send(
        vacancies_api.app,
        "PATCH",
        path,
        headers=owner,
        json={"company_id": archived_id, "title": "Should Not Save"},
    )
    assert conflict.status_code == 409
    assert conflict.json() == _ARCHIVED
    assert archived_id not in conflict.text
    _assert_public_error(conflict.json())
    stored = _vacancy(
        await send(vacancies_api.app, "GET", path, headers=owner),
        200,
    )
    assert stored == created
    assert stored["company_id"] == company_id


@pytest.mark.asyncio
async def test_patch_allows_the_current_company_and_title_after_archive(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={
                "company_id": company_id,
                "title": "Original",
                "description": "Keep me",
                "source_url": "board/1",
                "location": "Remote",
            },
        ),
        201,
    )
    archived = await send(
        vacancies_api.app,
        "POST",
        f"/companies/{company_id}/archive",
        headers=owner,
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    same_company = _vacancy(
        await send(
            vacancies_api.app,
            "PATCH",
            f"/vacancies/{created['id']}",
            headers=owner,
            json={"company_id": company_id},
        ),
        200,
    )
    assert same_company["company_id"] == company_id
    assert same_company["title"] == "Original"
    assert same_company["description"] == "Keep me"
    assert same_company["source_url"] == "board/1"
    assert same_company["location"] == "Remote"
    retitled = _vacancy(
        await send(
            vacancies_api.app,
            "PATCH",
            f"/vacancies/{created['id']}",
            headers=owner,
            json={"title": "  Still Open  "},
        ),
        200,
    )
    assert retitled["title"] == "  Still Open  "
    assert retitled["company_id"] == company_id
    assert retitled["description"] == "Keep me"
    assert retitled["source_url"] == "board/1"
    assert retitled["location"] == "Remote"


@pytest.mark.asyncio
async def test_delete_owned_vacancy_without_an_application_returns_no_content(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Temporary Role"},
        ),
        201,
    )
    vacancy_id = UUID(str(created["id"]))
    deleted = await send(
        vacancies_api.app,
        "DELETE",
        f"/vacancies/{created['id']}",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 204
    assert deleted.content == b""
    missing = await send(
        vacancies_api.app,
        "GET",
        f"/vacancies/{created['id']}",
        headers=owner,
    )
    assert missing.status_code == 404
    assert missing.json() == _NOT_FOUND
    _assert_public_error(missing.json())
    session = vacancies_api.session_factory()
    try:
        stored = await session.get(Vacancy, vacancy_id)
    finally:
        await session.close()
    assert stored is None


@pytest.mark.asyncio
async def test_delete_owned_vacancy_with_an_application_returns_conflict(
    vacancies_api: _VacanciesApi,
) -> None:
    _owner_id, owner = await _account(vacancies_api, "owner@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Applied Role"},
        ),
        201,
    )
    vacancy_id = UUID(str(created["id"]))
    changed_at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    application_id, history_id, interview_id = await _attach_application(
        vacancies_api,
        vacancy_id,
        changed_at,
    )
    rejected = await send(
        vacancies_api.app,
        "DELETE",
        f"/vacancies/{created['id']}",
        headers=owner,
    )
    assert rejected.history == []
    assert rejected.status_code == 409
    assert rejected.json() == _HAS_APPLICATION
    _assert_public_error(rejected.json())
    stored = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert stored == created
    session = vacancies_api.session_factory()
    try:
        vacancy = await session.get(Vacancy, vacancy_id)
        application = await session.get(Application, application_id)
        history = await session.get(ApplicationStatusHistory, history_id)
        interview = await session.get(Interview, interview_id)
    finally:
        await session.close()
    assert vacancy is not None
    assert application is not None
    assert application.vacancy_id == vacancy_id
    assert application.status == "APPLIED"
    assert history is not None
    assert history.application_id == application_id
    assert history.from_status is None
    assert history.to_status == "APPLIED"
    assert history.changed_at == changed_at
    assert interview is not None
    assert interview.application_id == application_id


@pytest.mark.asyncio
async def test_delete_missing_and_foreign_vacancies_return_the_same_not_found(
    vacancies_api: _VacanciesApi,
) -> None:
    owner_id, owner = await _account(vacancies_api, "owner@example.com")
    other_id, other = await _account(vacancies_api, "other@example.com")
    company_id = await _create_company(vacancies_api, owner, "Acme")
    created = _vacancy(
        await send(
            vacancies_api.app,
            "POST",
            "/vacancies",
            headers=owner,
            json={"company_id": company_id, "title": "Private Role"},
        ),
        201,
    )
    missing_id = uuid4()
    leak_title = "Stolen Title"
    missing = await send(
        vacancies_api.app,
        "DELETE",
        f"/vacancies/{missing_id}",
        headers=owner,
        json={"title": leak_title},
    )
    foreign = await send(
        vacancies_api.app,
        "DELETE",
        f"/vacancies/{created['id']}",
        headers=other,
        json={"title": leak_title},
    )
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.json() == foreign.json() == _NOT_FOUND
    assert str(missing_id) not in missing.text
    assert str(created["id"]) not in foreign.text
    assert str(owner_id) not in foreign.text
    assert str(other_id) not in foreign.text
    assert leak_title not in missing.text
    assert leak_title not in foreign.text
    assert "Private Role" not in foreign.text
    _assert_public_error(missing.json())
    _assert_public_error(foreign.json())
    still_there = _vacancy(
        await send(
            vacancies_api.app,
            "GET",
            f"/vacancies/{created['id']}",
            headers=owner,
        ),
        200,
    )
    assert still_there == created
