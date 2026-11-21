"""Application create and read HTTP contract."""

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.auth_support import (
    clear_settings_caches,
    configure_test_environment,
    make_app,
    send,
)

from job_tracker.api.deps import get_session
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
_CONFLICT = {
    "error": {
        "code": "APPLICATION_ALREADY_EXISTS",
        "message": "An application already exists for this vacancy",
        "details": [],
    }
}
_HAS_INTERVIEWS = {
    "error": {
        "code": "APPLICATION_HAS_INTERVIEWS",
        "message": "The application has an interview",
        "details": [],
    }
}
_APPLICATION_FIELDS = {"id", "vacancy_id", "status"}
_HISTORY_FIELDS = {
    "id",
    "application_id",
    "from_status",
    "to_status",
    "changed_at",
}
_VACANCY_ID = "123e4567-e89b-12d3-a456-426614174000"
_INVALID_STATUS = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.status",
                "code": "INVALID_VALUE",
                "message": "The value is not an allowed choice",
            }
        ],
    }
}
_MISSING_STATUS = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.status",
                "code": "REQUIRED",
                "message": "This field is required",
            }
        ],
    }
}


class _ApplicationsApi(NamedTuple):
    app: FastAPI
    session_factory: async_sessionmaker[AsyncSession]


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_applications_api",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def applications_api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_ApplicationsApi]:
    """Reset job_tracker_test and serve the real application routes."""
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
            raise RuntimeError("Application tests require PostgreSQL 17.")
        application = make_app()

        async def override() -> AsyncIterator[AsyncSession]:
            session = session_factory()
            try:
                yield session
            finally:
                await session.close()

        application.dependency_overrides[get_session] = override
        yield _ApplicationsApi(application, session_factory)
    finally:
        await engine.dispose()
        clear_settings_caches()


async def _account(api: _ApplicationsApi, email: str) -> tuple[UUID, dict[str, str]]:
    session = api.session_factory()
    try:
        user = await create_user(session, email, "encoded-hash")
        await session.commit()
        user_id = user.id
    finally:
        await session.close()
    return user_id, {"Authorization": f"Bearer {create_access_token(user_id)}"}


async def _create_company(
    api: _ApplicationsApi,
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


async def _create_vacancy(
    api: _ApplicationsApi,
    headers: dict[str, str],
) -> str:
    company_id = await _create_company(api, headers, "Acme")
    response = await send(
        api.app,
        "POST",
        "/vacancies",
        headers=headers,
        json={"company_id": company_id, "title": "Backend Engineer"},
    )
    assert response.status_code == 201
    vacancy_id = response.json()["id"]
    assert isinstance(vacancy_id, str)
    return vacancy_id


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


def _application(response: httpx.Response, status_code: int) -> dict[str, object]:
    assert response.history == []
    assert response.status_code == status_code
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == _APPLICATION_FIELDS
    assert "user_id" not in body
    return body


async def _stored_counts(api: _ApplicationsApi) -> tuple[int, int]:
    session = api.session_factory()
    try:
        applications = await session.scalar(
            select(func.count()).select_from(Application)
        )
        history = await session.scalar(
            select(func.count()).select_from(ApplicationStatusHistory)
        )
    finally:
        await session.close()
    if not isinstance(applications, int) or not isinstance(history, int):
        raise AssertionError("Application counts were not integers.")
    return applications, history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "options"),
    [
        (
            "POST",
            f"/vacancies/{_VACANCY_ID}/applications",
            {"json": {}},
        ),
        ("GET", f"/vacancies/{_VACANCY_ID}/applications", {}),
        (
            "PATCH",
            f"/vacancies/{_VACANCY_ID}/applications",
            {"json": {"status": "SCREENING"}},
        ),
        ("GET", f"/vacancies/{_VACANCY_ID}/applications/history", {}),
        ("DELETE", f"/vacancies/{_VACANCY_ID}/applications", {}),
    ],
)
async def test_application_routes_require_a_bearer_token_and_not_csrf(
    applications_api: _ApplicationsApi,
    method: str,
    path: str,
    options: dict[str, object],
) -> None:
    response = await send(applications_api.app, method, path, **options)
    assert response.history == []
    assert response.status_code == 401
    assert response.json() == _MISSING
    assert response.headers["www-authenticate"] == "Bearer"
    assert "csrf" not in response.text.lower()
    _assert_public_error(response.json())


@pytest.mark.asyncio
async def test_owner_create_and_get_return_the_application_without_user_id(
    applications_api: _ApplicationsApi,
) -> None:
    owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created_response = await send(
        applications_api.app,
        "POST",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
        json={},
    )
    created = _application(created_response, 201)
    assert created["vacancy_id"] == vacancy_id
    assert created["status"] == "APPLIED"
    assert "user_id" not in created_response.text
    assert str(owner_id) not in created_response.text
    assert "X-Requested-With" not in created_response.request.headers

    fetched_response = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
    )
    fetched = _application(fetched_response, 200)
    assert fetched == created
    assert str(owner_id) not in fetched_response.text

    session = applications_api.session_factory()
    try:
        applications = list(await session.scalars(select(Application)))
        history = list(await session.scalars(select(ApplicationStatusHistory)))
    finally:
        await session.close()
    assert len(applications) == 1
    assert applications[0].id == UUID(str(created["id"]))
    assert applications[0].vacancy_id == UUID(vacancy_id)
    assert applications[0].status == "APPLIED"
    assert "user_id" not in applications[0].__table__.columns
    assert len(history) == 1
    assert history[0].application_id == applications[0].id
    assert history[0].from_status is None
    assert history[0].to_status == "APPLIED"
    assert history[0].changed_at.tzinfo is not None
    assert history[0].changed_at.utcoffset() == timedelta(0)
    assert "user_id" not in history[0].__table__.columns


@pytest.mark.asyncio
async def test_missing_and_foreign_create_and_get_share_one_not_found_body(
    applications_api: _ApplicationsApi,
) -> None:
    owner_id, owner = await _account(applications_api, "owner@example.com")
    other_id, other = await _account(applications_api, "other@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    missing_id = uuid4()
    absent = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
    )
    missing_get = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{missing_id}/applications",
        headers=owner,
    )
    foreign_get = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications",
        headers=other,
    )
    missing_post = await send(
        applications_api.app,
        "POST",
        f"/vacancies/{missing_id}/applications",
        headers=owner,
        json={},
    )
    foreign_post = await send(
        applications_api.app,
        "POST",
        f"/vacancies/{vacancy_id}/applications",
        headers=other,
        json={},
    )
    bodies = [
        absent,
        missing_get,
        foreign_get,
        missing_post,
        foreign_post,
    ]
    assert [response.status_code for response in bodies] == [404, 404, 404, 404, 404]
    assert absent.json() == missing_get.json() == foreign_get.json() == _NOT_FOUND
    assert missing_post.json() == foreign_post.json() == _NOT_FOUND
    assert missing_get.json() == foreign_post.json()
    for response in bodies:
        _assert_public_error(response.json())
    assert str(missing_id) not in missing_get.text
    assert str(missing_id) not in missing_post.text
    assert vacancy_id not in foreign_get.text
    assert vacancy_id not in foreign_post.text
    assert str(owner_id) not in foreign_get.text
    assert str(owner_id) not in foreign_post.text
    assert str(other_id) not in foreign_get.text
    assert str(other_id) not in foreign_post.text
    assert await _stored_counts(applications_api) == (0, 0)


@pytest.mark.asyncio
async def test_foreign_get_after_create_matches_a_missing_vacancy(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    _other_id, other = await _account(applications_api, "other@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    missing_id = uuid4()
    foreign = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications",
        headers=other,
    )
    missing = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{missing_id}/applications",
        headers=owner,
    )
    assert foreign.history == []
    assert foreign.status_code == 404
    assert foreign.json() == _NOT_FOUND
    assert missing.status_code == 404
    assert foreign.json() == missing.json()
    assert str(created["id"]) not in foreign.text
    _assert_public_error(foreign.json())
    applications, _history = await _stored_counts(applications_api)
    assert applications == 1


@pytest.mark.asyncio
async def test_second_post_conflicts_and_keeps_the_single_application(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    conflict = await send(
        applications_api.app,
        "POST",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
        json={},
    )
    assert conflict.history == []
    assert conflict.status_code == 409
    assert conflict.json() == _CONFLICT
    _assert_public_error(conflict.json())
    assert await _stored_counts(applications_api) == (1, 1)
    fetched = _application(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
        ),
        200,
    )
    assert fetched == created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "SCREENING"),
        ("user_id", "123e4567-e89b-12d3-a456-426614174099"),
        ("vacancy_id", "123e4567-e89b-12d3-a456-426614174098"),
        ("notes", "extra"),
    ],
)
async def test_client_fields_are_rejected_and_insert_nothing(
    applications_api: _ApplicationsApi,
    field: str,
    value: str,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    response = await send(
        applications_api.app,
        "POST",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
        json={field: value},
    )
    assert response.history == []
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": [
                {
                    "field": f"body.{field}",
                    "code": "UNEXPECTED_FIELD",
                    "message": "This field is not allowed",
                }
            ],
        }
    }
    assert value not in response.text
    _assert_public_error(response.json())
    assert await _stored_counts(applications_api) == (0, 0)


@pytest.mark.asyncio
async def test_delete_application_removes_history_and_keeps_the_vacancy(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    patched = _application(
        await send(
            applications_api.app,
            "PATCH",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={"status": "SCREENING"},
        ),
        200,
    )
    assert patched["id"] == created["id"]
    assert patched["status"] == "SCREENING"
    before = _history_items(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications/history",
            headers=owner,
        )
    )
    assert [item["to_status"] for item in before] == ["APPLIED", "SCREENING"]
    deleted = await send(
        applications_api.app,
        "DELETE",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 204
    assert deleted.content == b""
    missing_application = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
    )
    missing_history = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications/history",
        headers=owner,
    )
    assert missing_application.status_code == 404
    assert missing_history.status_code == 404
    assert missing_application.json() == missing_history.json() == _NOT_FOUND
    vacancy = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}",
        headers=owner,
    )
    assert vacancy.history == []
    assert vacancy.status_code == 200
    assert vacancy.json()["id"] == vacancy_id
    session = applications_api.session_factory()
    try:
        applications = list(
            await session.scalars(
                select(Application).where(Application.vacancy_id == UUID(vacancy_id))
            )
        )
        history = list(
            await session.scalars(
                select(ApplicationStatusHistory).where(
                    ApplicationStatusHistory.application_id == UUID(str(created["id"]))
                )
            )
        )
        stored_vacancy = await session.get(Vacancy, UUID(vacancy_id))
    finally:
        await session.close()
    assert applications == []
    assert history == []
    assert stored_vacancy is not None
    assert stored_vacancy.id == UUID(vacancy_id)
    assert await _stored_counts(applications_api) == (0, 0)


@pytest.mark.asyncio
async def test_delete_application_with_an_interview_conflicts(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    scheduled_at = datetime(2026, 4, 1, 16, 0, tzinfo=UTC)
    session = applications_api.session_factory()
    try:
        interview = Interview(
            application_id=UUID(str(created["id"])),
            scheduled_at=scheduled_at,
            notes="Intro call",
        )
        session.add(interview)
        await session.flush()
        interview_id = interview.id
        await session.commit()
    finally:
        await session.close()
    before = _history_items(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications/history",
            headers=owner,
        )
    )
    deleted = await send(
        applications_api.app,
        "DELETE",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 409
    assert deleted.json() == _HAS_INTERVIEWS
    _assert_public_error(deleted.json())
    fetched = _application(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
        ),
        200,
    )
    assert fetched == created
    assert fetched["status"] == "APPLIED"
    after = _history_items(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications/history",
            headers=owner,
        )
    )
    assert after == before
    vacancy = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}",
        headers=owner,
    )
    assert vacancy.status_code == 200
    assert vacancy.json()["id"] == vacancy_id
    session = applications_api.session_factory()
    try:
        stored_application = await session.get(Application, UUID(str(created["id"])))
        stored_history = list(await session.scalars(select(ApplicationStatusHistory)))
        stored_interview = await session.get(Interview, interview_id)
        stored_vacancy = await session.get(Vacancy, UUID(vacancy_id))
    finally:
        await session.close()
    assert stored_application is not None
    assert stored_application.status == "APPLIED"
    assert stored_application.vacancy_id == UUID(vacancy_id)
    assert len(stored_history) == 1
    assert stored_history[0].application_id == stored_application.id
    assert stored_interview is not None
    assert stored_interview.application_id == stored_application.id
    assert stored_interview.scheduled_at == scheduled_at
    assert stored_interview.notes == "Intro call"
    assert stored_vacancy is not None
    assert stored_vacancy.id == UUID(vacancy_id)
    assert await _stored_counts(applications_api) == (1, 1)


@pytest.mark.asyncio
async def test_delete_missing_foreign_and_absent_share_one_not_found_body(
    applications_api: _ApplicationsApi,
) -> None:
    owner_id, owner = await _account(applications_api, "owner@example.com")
    other_id, other = await _account(applications_api, "other@example.com")
    empty_vacancy_id = await _create_vacancy(applications_api, owner)
    foreign_vacancy_id = await _create_vacancy(applications_api, other)
    foreign_created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{foreign_vacancy_id}/applications",
            headers=other,
            json={},
        ),
        201,
    )
    foreign_patched = _application(
        await send(
            applications_api.app,
            "PATCH",
            f"/vacancies/{foreign_vacancy_id}/applications",
            headers=other,
            json={"status": "SCREENING"},
        ),
        200,
    )
    assert foreign_patched["status"] == "SCREENING"
    scheduled_at = datetime(2026, 4, 1, 16, 0, tzinfo=UTC)
    session = applications_api.session_factory()
    try:
        interview = Interview(
            application_id=UUID(str(foreign_created["id"])),
            scheduled_at=scheduled_at,
            notes="Intro call",
        )
        session.add(interview)
        await session.flush()
        interview_id = interview.id
        await session.commit()
    finally:
        await session.close()
    missing_id = uuid4()
    missing = await send(
        applications_api.app,
        "DELETE",
        f"/vacancies/{missing_id}/applications",
        headers=owner,
    )
    foreign = await send(
        applications_api.app,
        "DELETE",
        f"/vacancies/{foreign_vacancy_id}/applications",
        headers=owner,
    )
    absent = await send(
        applications_api.app,
        "DELETE",
        f"/vacancies/{empty_vacancy_id}/applications",
        headers=owner,
    )
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert absent.status_code == 404
    assert missing.json() == foreign.json()
    assert absent.json() == missing.json() == _NOT_FOUND
    for response in (missing, foreign, absent):
        _assert_public_error(response.json())
        text = response.text
        assert str(missing_id) not in text
        assert foreign_vacancy_id not in text
        assert empty_vacancy_id not in text
        assert str(owner_id) not in text
        assert str(other_id) not in text
        assert str(foreign_created["id"]) not in text
        assert "APPLIED" not in text
        assert "SCREENING" not in text
        assert str(interview_id) not in text
        assert "Intro call" not in text
        assert scheduled_at.isoformat() not in text
        assert "APPLICATION_HAS_INTERVIEWS" not in text
    kept = _application(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{foreign_vacancy_id}/applications",
            headers=other,
        ),
        200,
    )
    assert kept == foreign_patched
    kept_history = _history_items(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{foreign_vacancy_id}/applications/history",
            headers=other,
        )
    )
    assert [item["to_status"] for item in kept_history] == ["APPLIED", "SCREENING"]
    foreign_vacancy = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{foreign_vacancy_id}",
        headers=other,
    )
    assert foreign_vacancy.status_code == 200
    assert foreign_vacancy.json()["id"] == foreign_vacancy_id
    empty_vacancy = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{empty_vacancy_id}",
        headers=owner,
    )
    assert empty_vacancy.status_code == 200
    assert empty_vacancy.json()["id"] == empty_vacancy_id
    still_absent = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{empty_vacancy_id}/applications",
        headers=owner,
    )
    assert still_absent.status_code == 404
    assert still_absent.json() == _NOT_FOUND
    session = applications_api.session_factory()
    try:
        stored_application = await session.get(
            Application, UUID(str(foreign_created["id"]))
        )
        stored_history = list(
            await session.scalars(
                select(ApplicationStatusHistory).where(
                    ApplicationStatusHistory.application_id
                    == UUID(str(foreign_created["id"]))
                )
            )
        )
        stored_foreign_vacancy = await session.get(Vacancy, UUID(foreign_vacancy_id))
        stored_empty_vacancy = await session.get(Vacancy, UUID(empty_vacancy_id))
        stored_interview = await session.get(Interview, interview_id)
    finally:
        await session.close()
    assert stored_application is not None
    assert stored_application.status == "SCREENING"
    assert len(stored_history) == 2
    assert stored_interview is not None
    assert stored_interview.application_id == stored_application.id
    assert stored_interview.scheduled_at == scheduled_at
    assert stored_interview.notes == "Intro call"
    assert stored_foreign_vacancy is not None
    assert stored_empty_vacancy is not None
    assert await _stored_counts(applications_api) == (1, 2)


def _history_items(response: httpx.Response) -> list[dict[str, object]]:
    assert response.history == []
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == {"items"}
    items = body["items"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        assert set(item) == _HISTORY_FIELDS
    return items


def _aware(value: object) -> datetime:
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    return parsed


@pytest.mark.asyncio
async def test_owner_can_patch_every_status_and_return_to_applied(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    path = f"/vacancies/{vacancy_id}/applications"
    sequence = [
        "SCREENING",
        "INTERVIEW",
        "OFFER",
        "REJECTED",
        "WITHDRAWN",
        "APPLIED",
    ]
    current = created
    for index, new_status in enumerate(sequence, start=1):
        current = _application(
            await send(
                applications_api.app,
                "PATCH",
                path,
                headers=owner,
                json={"status": new_status},
            ),
            200,
        )
        assert current["id"] == created["id"]
        assert current["vacancy_id"] == vacancy_id
        assert current["status"] == new_status
        assert await _stored_counts(applications_api) == (1, 1 + index)
    assert current["status"] == "APPLIED"
    items = _history_items(
        await send(
            applications_api.app,
            "GET",
            f"{path}/history",
            headers=owner,
        )
    )
    assert [item["to_status"] for item in items] == ["APPLIED", *sequence]
    assert [item["from_status"] for item in items] == [
        None,
        "APPLIED",
        "SCREENING",
        "INTERVIEW",
        "OFFER",
        "REJECTED",
        "WITHDRAWN",
    ]
    assert all(item["application_id"] == created["id"] for item in items)
    parsed = [_aware(item["changed_at"]) for item in items]
    stamped = list(zip(parsed, (str(item["id"]) for item in items), strict=True))
    assert stamped == sorted(stamped)
    assert "user_id" not in "".join(str(item) for item in items)


@pytest.mark.asyncio
async def test_patch_of_the_current_status_adds_no_history(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    unchanged = _application(
        await send(
            applications_api.app,
            "PATCH",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={"status": "APPLIED"},
        ),
        200,
    )
    assert unchanged == created
    assert await _stored_counts(applications_api) == (1, 1)
    items = _history_items(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications/history",
            headers=owner,
        )
    )
    assert len(items) == 1
    assert items[0]["from_status"] is None
    assert items[0]["to_status"] == "APPLIED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected", "hidden"),
    [
        ({"status": "HIRED"}, _INVALID_STATUS, "HIRED"),
        ({"status": None}, _INVALID_STATUS, None),
        ({}, _MISSING_STATUS, None),
        (
            {
                "status": "OFFER",
                "user_id": "123e4567-e89b-12d3-a456-426614174099",
            },
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": [
                        {
                            "field": "body.user_id",
                            "code": "UNEXPECTED_FIELD",
                            "message": "This field is not allowed",
                        }
                    ],
                }
            },
            "123e4567-e89b-12d3-a456-426614174099",
        ),
    ],
)
async def test_invalid_status_patch_writes_nothing(
    applications_api: _ApplicationsApi,
    payload: dict[str, object],
    expected: dict[str, object],
    hidden: str | None,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    response = await send(
        applications_api.app,
        "PATCH",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
        json=payload,
    )
    assert response.history == []
    assert response.status_code == 422
    assert response.json() == expected
    if hidden is not None:
        assert hidden not in response.text
    _assert_public_error(response.json())
    assert await _stored_counts(applications_api) == (1, 1)
    stored = _application(
        await send(
            applications_api.app,
            "GET",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
        ),
        200,
    )
    assert stored == created


@pytest.mark.asyncio
async def test_patch_and_history_not_found_bodies_match(
    applications_api: _ApplicationsApi,
) -> None:
    owner_id, owner = await _account(applications_api, "owner@example.com")
    other_id, other = await _account(applications_api, "other@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    missing_id = uuid4()
    requests = [
        ("PATCH", f"/vacancies/{missing_id}/applications", owner, {"status": "OFFER"}),
        (
            "PATCH",
            f"/vacancies/{vacancy_id}/applications",
            other,
            {"status": "OFFER"},
        ),
        (
            "PATCH",
            f"/vacancies/{vacancy_id}/applications",
            owner,
            {"status": "OFFER"},
        ),
        ("GET", f"/vacancies/{missing_id}/applications/history", owner, None),
        ("GET", f"/vacancies/{vacancy_id}/applications/history", other, None),
        ("GET", f"/vacancies/{vacancy_id}/applications/history", owner, None),
    ]
    bodies = []
    for method, path, headers, payload in requests:
        options: dict[str, object] = {"headers": headers}
        if payload is not None:
            options["json"] = payload
        response = await send(applications_api.app, method, path, **options)
        assert response.status_code == 404
        assert response.json() == _NOT_FOUND
        _assert_public_error(response.json())
        bodies.append(response.json())
    assert bodies == [_NOT_FOUND] * len(requests)
    assert await _stored_counts(applications_api) == (0, 0)

    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    foreign_patch = await send(
        applications_api.app,
        "PATCH",
        f"/vacancies/{vacancy_id}/applications",
        headers=other,
        json={"status": "OFFER"},
    )
    foreign_history = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications/history",
        headers=other,
    )
    missing_history = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{missing_id}/applications/history",
        headers=owner,
    )
    assert foreign_patch.status_code == 404
    assert foreign_history.status_code == 404
    assert missing_history.status_code == 404
    assert foreign_patch.json() == foreign_history.json() == _NOT_FOUND
    assert foreign_history.json() == missing_history.json()
    assert str(created["id"]) not in foreign_patch.text
    assert str(created["id"]) not in foreign_history.text
    assert vacancy_id not in foreign_patch.text
    assert str(owner_id) not in foreign_history.text
    assert str(other_id) not in foreign_history.text
    _assert_public_error(foreign_patch.json())
    assert await _stored_counts(applications_api) == (1, 1)


@pytest.mark.asyncio
async def test_history_after_create_returns_the_initial_row(
    applications_api: _ApplicationsApi,
) -> None:
    owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    response = await send(
        applications_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications/history",
        headers=owner,
    )
    items = _history_items(response)
    assert len(items) == 1
    assert items[0]["application_id"] == created["id"]
    assert items[0]["from_status"] is None
    assert items[0]["to_status"] == "APPLIED"
    _aware(items[0]["changed_at"])
    assert "user_id" not in response.text
    assert str(owner_id) not in response.text


@pytest.mark.asyncio
async def test_history_methods_other_than_get_are_not_implemented(
    applications_api: _ApplicationsApi,
) -> None:
    _owner_id, owner = await _account(applications_api, "owner@example.com")
    vacancy_id = await _create_vacancy(applications_api, owner)
    created = _application(
        await send(
            applications_api.app,
            "POST",
            f"/vacancies/{vacancy_id}/applications",
            headers=owner,
            json={},
        ),
        201,
    )
    path = f"/vacancies/{vacancy_id}/applications/history"
    for method in ("POST", "PATCH", "DELETE"):
        response = await send(
            applications_api.app,
            method,
            path,
            headers=owner,
            json={"status": "OFFER"},
        )
        assert response.status_code == 405
    items = _history_items(await send(applications_api.app, "GET", path, headers=owner))
    assert len(items) == 1
    assert items[0]["application_id"] == created["id"]
    assert await _stored_counts(applications_api) == (1, 1)
