"""Interview create, read, update, and delete HTTP contract."""

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
_MISSING_TIME = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.scheduled_at",
                "code": "REQUIRED",
                "message": "This field is required",
            }
        ],
    }
}
_INVALID_TIME = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.scheduled_at",
                "code": "INVALID_FORMAT",
                "message": "A valid timestamp is required",
            }
        ],
    }
}
_INTERVIEW_FIELDS = {
    "id",
    "application_id",
    "scheduled_at",
    "outcome",
    "notes",
    "status",
    "interview_type",
}
_UNSUPPORTED_STATUS = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.status",
                "code": "INVALID_VALUE",
                "message": "The interview status is not supported",
            }
        ],
    }
}
_UNSUPPORTED_TYPE = {
    "error": {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": [
            {
                "field": "body.interview_type",
                "code": "INVALID_VALUE",
                "message": "The interview type is not supported",
            }
        ],
    }
}
_INTERVIEW_STATUSES = ("SCHEDULED", "COMPLETED", "CANCELLED", "NO_SHOW")
_INTERVIEW_TYPES = ("PHONE", "VIDEO", "ONSITE")
_APPLICATION_ID = "123e4567-e89b-12d3-a456-426614174000"
_INTERVIEW_ID = "123e4567-e89b-12d3-a456-426614174001"
_WHEN_Z = "2026-06-15T12:00:00Z"
_LATER_Z = "2026-06-15T13:00:00Z"
_OFFSET = "2026-06-15T18:30:00+03:00"
_NAIVE = "2026-06-15T18:30:00"
_WHEN_Z_UTC = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
_LATER_Z_UTC = datetime(2026, 6, 15, 13, 0, tzinfo=UTC)
_OFFSET_UTC = datetime(2026, 6, 15, 15, 30, tzinfo=UTC)


class _InterviewsApi(NamedTuple):
    app: FastAPI
    session_factory: async_sessionmaker[AsyncSession]


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_interviews_api",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def interviews_api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_InterviewsApi]:
    """Reset job_tracker_test and serve the real interview routes."""
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
            raise RuntimeError("Interview tests require PostgreSQL 17.")
        application = make_app()

        async def override() -> AsyncIterator[AsyncSession]:
            session = session_factory()
            try:
                yield session
            finally:
                await session.close()

        application.dependency_overrides[get_session] = override
        yield _InterviewsApi(application, session_factory)
    finally:
        await engine.dispose()
        clear_settings_caches()


async def _account(api: _InterviewsApi, email: str) -> tuple[UUID, dict[str, str]]:
    session = api.session_factory()
    try:
        user = await create_user(session, email, "encoded-hash")
        await session.commit()
        user_id = user.id
    finally:
        await session.close()
    return user_id, {"Authorization": f"Bearer {create_access_token(user_id)}"}


async def _create_company(
    api: _InterviewsApi,
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


async def _create_vacancy(api: _InterviewsApi, headers: dict[str, str]) -> str:
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


async def _create_application(
    api: _InterviewsApi,
    headers: dict[str, str],
) -> tuple[str, str]:
    vacancy_id = await _create_vacancy(api, headers)
    response = await send(
        api.app,
        "POST",
        f"/vacancies/{vacancy_id}/applications",
        headers=headers,
        json={},
    )
    assert response.status_code == 201
    application_id = response.json()["id"]
    assert isinstance(application_id, str)
    return vacancy_id, application_id


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


def _assert_not_found(response: httpx.Response) -> None:
    assert response.history == []
    assert response.status_code == 404
    assert response.json() == _NOT_FOUND
    _assert_public_error(response.json())


def _interview(response: httpx.Response, status_code: int) -> dict[str, object]:
    assert response.history == []
    assert response.status_code == status_code
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == _INTERVIEW_FIELDS
    assert "user_id" not in body
    return body


def _items(response: httpx.Response) -> list[dict[str, object]]:
    assert response.history == []
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == {"items"}
    items = body["items"]
    assert isinstance(items, list)
    checked: list[dict[str, object]] = []
    for item in items:
        assert isinstance(item, dict)
        assert set(item) == _INTERVIEW_FIELDS
        assert "user_id" not in item
        checked.append(item)
    return checked


def _utc_instant(value: object) -> datetime:
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    return parsed


def _unexpected(field: str) -> dict[str, object]:
    return {
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


async def _interview_count(api: _InterviewsApi) -> int:
    session = api.session_factory()
    try:
        count = await session.scalar(select(func.count()).select_from(Interview))
    finally:
        await session.close()
    if not isinstance(count, int):
        raise AssertionError("Interview count was not an integer.")
    return count


async def _stored(
    api: _InterviewsApi,
    interview_id: str,
) -> tuple[datetime, str | None, str | None]:
    session = api.session_factory()
    try:
        interview = await session.get(Interview, UUID(interview_id))
        if interview is None:
            raise AssertionError("Interview was not stored.")
        return interview.scheduled_at, interview.notes, interview.outcome
    finally:
        await session.close()


async def _stored_status_and_type(
    api: _InterviewsApi,
    interview_id: str,
) -> tuple[str | None, str | None]:
    session = api.session_factory()
    try:
        interview = await session.get(Interview, UUID(interview_id))
        if interview is None:
            raise AssertionError("Interview was not stored.")
        return interview.status, interview.interview_type
    finally:
        await session.close()


def _assert_rejected_value_hidden(value: object, payload: str) -> None:
    """The public error does not repeat the rejected value."""
    if isinstance(value, str):
        if value != "":
            assert value not in payload
        return
    assert str(value) not in payload
    if isinstance(value, bool):
        assert str(value).lower() not in payload.lower()


async def _ordered_ids(api: _InterviewsApi, application_id: str) -> list[str]:
    session = api.session_factory()
    try:
        rows = await session.scalars(
            select(Interview.id)
            .where(Interview.application_id == UUID(application_id))
            .order_by(Interview.id.asc())
        )
        return [str(row) for row in rows.all()]
    finally:
        await session.close()


async def _status_and_history(
    api: _InterviewsApi,
    application_id: str,
) -> tuple[str, int]:
    session = api.session_factory()
    try:
        status_value = await session.scalar(
            select(Application.status).where(Application.id == UUID(application_id))
        )
        history = await session.scalar(
            select(func.count())
            .select_from(ApplicationStatusHistory)
            .where(ApplicationStatusHistory.application_id == UUID(application_id))
        )
    finally:
        await session.close()
    if not isinstance(status_value, str) or not isinstance(history, int):
        raise AssertionError("Application state was not available.")
    return status_value, history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "options"),
    [
        (
            "POST",
            f"/applications/{_APPLICATION_ID}/interviews",
            {"json": {"scheduled_at": _WHEN_Z}},
        ),
        ("GET", f"/applications/{_APPLICATION_ID}/interviews", {}),
        ("GET", f"/interviews/{_INTERVIEW_ID}", {}),
        (
            "PATCH",
            f"/interviews/{_INTERVIEW_ID}",
            {"json": {"notes": "later"}},
        ),
        ("DELETE", f"/interviews/{_INTERVIEW_ID}", {}),
    ],
)
async def test_interview_routes_require_a_bearer_token_and_not_csrf(
    interviews_api: _InterviewsApi,
    method: str,
    path: str,
    options: dict[str, object],
) -> None:
    response = await send(interviews_api.app, method, path, **options)
    assert response.history == []
    assert response.status_code == 401
    assert response.json() == _MISSING
    assert response.headers["www-authenticate"] == "Bearer"
    assert "csrf" not in response.text.lower()
    _assert_public_error(response.json())


@pytest.mark.asyncio
async def test_owner_lists_only_owned_interviews_in_id_order(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _other_id, other = await _account(interviews_api, "other@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    _empty_vacancy_id, empty_application_id = await _create_application(
        interviews_api,
        owner,
    )
    _other_vacancy_id, other_application_id = await _create_application(
        interviews_api,
        other,
    )
    first_response = await send(
        interviews_api.app,
        "POST",
        f"/applications/{application_id}/interviews",
        headers=owner,
        json={
            "scheduled_at": _WHEN_Z,
            "notes": "first",
            "outcome": "pending",
        },
    )
    first = _interview(first_response, 201)
    assert "X-Requested-With" not in first_response.request.headers
    assert first["application_id"] == application_id
    assert _utc_instant(first["scheduled_at"]) == _WHEN_Z_UTC
    second = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _LATER_Z,
                "notes": "second",
                "outcome": "pending",
            },
        ),
        201,
    )
    other_interview = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{other_application_id}/interviews",
            headers=other,
            json={"scheduled_at": _WHEN_Z, "notes": "other-note"},
        ),
        201,
    )
    listed = _items(
        await send(
            interviews_api.app,
            "GET",
            f"/applications/{application_id}/interviews",
            headers=owner,
        )
    )
    assert [item["id"] for item in listed] == await _ordered_ids(
        interviews_api,
        application_id,
    )
    assert [item["id"] for item in listed] == sorted(str(item["id"]) for item in listed)
    assert {item["id"] for item in listed} == {first["id"], second["id"]}
    assert other_interview["id"] not in {item["id"] for item in listed}
    assert "other-note" not in "".join(str(item["notes"]) for item in listed)
    fetched = _interview(
        await send(
            interviews_api.app,
            "GET",
            f"/interviews/{first['id']}",
            headers=owner,
        ),
        200,
    )
    assert fetched == first
    empty = await send(
        interviews_api.app,
        "GET",
        f"/applications/{empty_application_id}/interviews",
        headers=owner,
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": []}


@pytest.mark.asyncio
async def test_omitted_and_null_notes_and_outcome_are_stored_null(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    omitted = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _WHEN_Z},
        ),
        201,
    )
    explicit = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _LATER_Z, "notes": None, "outcome": None},
        ),
        201,
    )
    assert omitted["notes"] is None
    assert omitted["outcome"] is None
    assert explicit["notes"] is None
    assert explicit["outcome"] is None
    assert await _stored(interviews_api, str(omitted["id"])) == (
        _WHEN_Z_UTC,
        None,
        None,
    )
    assert await _stored(interviews_api, str(explicit["id"])) == (
        _LATER_Z_UTC,
        None,
        None,
    )
    updated = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{omitted['id']}",
            headers=owner,
            json={"notes": "bring a notebook", "outcome": "not-a-status"},
        ),
        200,
    )
    assert updated["notes"] == "bring a notebook"
    assert updated["outcome"] == "not-a-status"
    assert updated["scheduled_at"] == omitted["scheduled_at"]
    assert await _stored(interviews_api, str(omitted["id"])) == (
        _WHEN_Z_UTC,
        "bring a notebook",
        "not-a-status",
    )


@pytest.mark.asyncio
async def test_scheduled_at_is_required_and_naive_writes_nothing(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    missing = await send(
        interviews_api.app,
        "POST",
        f"/applications/{application_id}/interviews",
        headers=owner,
        json={"notes": "not stored"},
    )
    null_time = await send(
        interviews_api.app,
        "POST",
        f"/applications/{application_id}/interviews",
        headers=owner,
        json={"scheduled_at": None, "notes": "not stored"},
    )
    naive = await send(
        interviews_api.app,
        "POST",
        f"/applications/{application_id}/interviews",
        headers=owner,
        json={"scheduled_at": _NAIVE, "notes": "not stored"},
    )
    assert missing.status_code == 422
    assert missing.json() == _MISSING_TIME
    assert null_time.status_code == 422
    assert null_time.json() == _INVALID_TIME
    assert naive.status_code == 422
    assert naive.json() == _INVALID_TIME
    for response in (missing, null_time, naive):
        _assert_public_error(response.json())
        assert "not stored" not in response.text
    assert await _interview_count(interviews_api) == 0


@pytest.mark.asyncio
async def test_offset_scheduled_at_is_stored_as_the_same_utc_instant(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _OFFSET},
        ),
        201,
    )
    assert _utc_instant(created["scheduled_at"]) == _OFFSET_UTC
    assert await _stored(interviews_api, str(created["id"])) == (
        _OFFSET_UTC,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_patch_keeps_omitted_fields_and_rejects_empty_or_null_time(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _WHEN_Z,
                "notes": "keep",
                "outcome": "pending",
            },
        ),
        201,
    )
    interview_id = str(created["id"])
    notes_only = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"notes": "next"},
        ),
        200,
    )
    assert notes_only["notes"] == "next"
    assert notes_only["outcome"] == "pending"
    assert _utc_instant(notes_only["scheduled_at"]) == _WHEN_Z_UTC
    assert await _stored(interviews_api, interview_id) == (
        _WHEN_Z_UTC,
        "next",
        "pending",
    )
    cleared_outcome = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"outcome": None},
        ),
        200,
    )
    assert cleared_outcome["outcome"] is None
    assert cleared_outcome["notes"] == "next"
    assert _utc_instant(cleared_outcome["scheduled_at"]) == _WHEN_Z_UTC
    cleared_notes = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"notes": None},
        ),
        200,
    )
    assert cleared_notes["notes"] is None
    assert cleared_notes["outcome"] is None
    assert await _stored(interviews_api, interview_id) == (_WHEN_Z_UTC, None, None)
    null_time = await send(
        interviews_api.app,
        "PATCH",
        f"/interviews/{interview_id}",
        headers=owner,
        json={"scheduled_at": None},
    )
    empty = await send(
        interviews_api.app,
        "PATCH",
        f"/interviews/{interview_id}",
        headers=owner,
        json={},
    )
    naive = await send(
        interviews_api.app,
        "PATCH",
        f"/interviews/{interview_id}",
        headers=owner,
        json={"scheduled_at": _NAIVE},
    )
    assert null_time.status_code == 422
    assert null_time.json() == _INVALID_TIME
    assert empty.status_code == 422
    assert empty.json() == _EMPTY_PATCH
    assert empty.json()["error"]["details"][0]["field"] == "body"
    assert naive.status_code == 422
    assert naive.json() == _INVALID_TIME
    for response in (null_time, empty, naive):
        _assert_public_error(response.json())
    assert await _stored(interviews_api, interview_id) == (_WHEN_Z_UTC, None, None)
    moved = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"scheduled_at": _OFFSET},
        ),
        200,
    )
    assert _utc_instant(moved["scheduled_at"]) == _OFFSET_UTC
    assert moved["notes"] is None
    assert moved["outcome"] is None
    assert await _stored(interviews_api, interview_id) == (_OFFSET_UTC, None, None)


@pytest.mark.asyncio
async def test_saving_interview_fields_leaves_application_status(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    vacancy_id, application_id = await _create_application(interviews_api, owner)
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _WHEN_Z, "notes": "keep", "outcome": None},
        ),
        201,
    )
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)
    interview_id = str(created["id"])
    with_outcome = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"outcome": "not-a-status"},
        ),
        200,
    )
    assert with_outcome["outcome"] == "not-a-status"
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)
    _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"notes": "still the same application"},
        ),
        200,
    )
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)
    moved = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"scheduled_at": _OFFSET},
        ),
        200,
    )
    assert _utc_instant(moved["scheduled_at"]) == _OFFSET_UTC
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)
    application = await send(
        interviews_api.app,
        "GET",
        f"/vacancies/{vacancy_id}/applications",
        headers=owner,
    )
    assert application.status_code == 200
    assert application.json()["status"] == "APPLIED"
    assert await _stored(interviews_api, interview_id) == (
        _OFFSET_UTC,
        "still the same application",
        "not-a-status",
    )


@pytest.mark.asyncio
async def test_missing_and_foreign_resources_share_one_not_found_body(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _other_id, other = await _account(interviews_api, "other@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    _other_vacancy_id, other_application_id = await _create_application(
        interviews_api,
        other,
    )
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _WHEN_Z, "notes": "keep", "outcome": "pending"},
        ),
        201,
    )
    interview_id = str(created["id"])
    missing_application_id = str(uuid4())
    missing_interview_id = str(uuid4())
    missing_post = await send(
        interviews_api.app,
        "POST",
        f"/applications/{missing_application_id}/interviews",
        headers=owner,
        json={"scheduled_at": _WHEN_Z},
    )
    foreign_post = await send(
        interviews_api.app,
        "POST",
        f"/applications/{application_id}/interviews",
        headers=other,
        json={"scheduled_at": _LATER_Z, "notes": "foreign"},
    )
    missing_list = await send(
        interviews_api.app,
        "GET",
        f"/applications/{missing_application_id}/interviews",
        headers=owner,
    )
    foreign_list = await send(
        interviews_api.app,
        "GET",
        f"/applications/{application_id}/interviews",
        headers=other,
    )
    other_list = await send(
        interviews_api.app,
        "GET",
        f"/applications/{other_application_id}/interviews",
        headers=owner,
    )
    missing_get = await send(
        interviews_api.app,
        "GET",
        f"/interviews/{missing_interview_id}",
        headers=owner,
    )
    foreign_get = await send(
        interviews_api.app,
        "GET",
        f"/interviews/{interview_id}",
        headers=other,
    )
    missing_patch = await send(
        interviews_api.app,
        "PATCH",
        f"/interviews/{missing_interview_id}",
        headers=owner,
        json={"notes": "missing"},
    )
    foreign_patch = await send(
        interviews_api.app,
        "PATCH",
        f"/interviews/{interview_id}",
        headers=other,
        json={
            "notes": "stolen",
            "outcome": "changed",
            "status": "COMPLETED",
            "interview_type": "ONSITE",
        },
    )
    responses = [
        missing_post,
        foreign_post,
        missing_list,
        foreign_list,
        other_list,
        missing_get,
        foreign_get,
        missing_patch,
        foreign_patch,
    ]
    assert [response.json() for response in responses] == [_NOT_FOUND] * len(responses)
    for response in responses:
        _assert_not_found(response)
    for response in (foreign_get, foreign_patch, foreign_list, foreign_post):
        assert interview_id not in response.text
    assert await _interview_count(interviews_api) == 1
    assert await _stored(interviews_api, interview_id) == (
        _WHEN_Z_UTC,
        "keep",
        "pending",
    )
    assert await _stored_status_and_type(interviews_api, interview_id) == (
        None,
        None,
    )
    owned_list = _items(
        await send(
            interviews_api.app,
            "GET",
            f"/applications/{application_id}/interviews",
            headers=owner,
        )
    )
    assert [item["id"] for item in owned_list] == [interview_id]


@pytest.mark.asyncio
async def test_delete_owned_interview_returns_no_content(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _WHEN_Z,
                "notes": "keep",
                "outcome": "pending",
            },
        ),
        201,
    )
    survivor = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _LATER_Z,
                "notes": "remain",
                "outcome": "later",
            },
        ),
        201,
    )
    interview_id = str(created["id"])
    survivor_id = str(survivor["id"])
    deleted = await send(
        interviews_api.app,
        "DELETE",
        f"/interviews/{interview_id}",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 204
    assert deleted.content == b""
    missing = await send(
        interviews_api.app,
        "GET",
        f"/interviews/{interview_id}",
        headers=owner,
    )
    _assert_not_found(missing)
    session = interviews_api.session_factory()
    try:
        stored = await session.get(Interview, UUID(interview_id))
    finally:
        await session.close()
    assert stored is None
    assert await _status_and_history(interviews_api, application_id) == (
        "APPLIED",
        1,
    )
    assert await _stored(interviews_api, survivor_id) == (
        _LATER_Z_UTC,
        "remain",
        "later",
    )
    assert await _interview_count(interviews_api) == 1


@pytest.mark.asyncio
async def test_delete_missing_and_foreign_interviews_return_the_same_not_found(
    interviews_api: _InterviewsApi,
) -> None:
    owner_id, owner = await _account(interviews_api, "owner@example.com")
    other_id, other = await _account(interviews_api, "other@example.com")
    _vacancy_id, _application_id = await _create_application(interviews_api, owner)
    private_notes = "private notes stay hidden"
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{_application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _WHEN_Z,
                "notes": private_notes,
                "outcome": "pending",
            },
        ),
        201,
    )
    interview_id = str(created["id"])
    missing_id = uuid4()
    missing = await send(
        interviews_api.app,
        "DELETE",
        f"/interviews/{missing_id}",
        headers=owner,
    )
    foreign = await send(
        interviews_api.app,
        "DELETE",
        f"/interviews/{interview_id}",
        headers=other,
    )
    assert missing.json() == foreign.json()
    assert missing.json() == _NOT_FOUND
    _assert_not_found(missing)
    _assert_not_found(foreign)
    for response in (missing, foreign):
        assert interview_id not in response.text
        assert str(missing_id) not in response.text
        assert str(owner_id) not in response.text
        assert str(other_id) not in response.text
        assert private_notes not in response.text
    assert await _stored(interviews_api, interview_id) == (
        _WHEN_Z_UTC,
        private_notes,
        "pending",
    )
    assert await _status_and_history(interviews_api, _application_id) == (
        "APPLIED",
        1,
    )
    assert await _interview_count(interviews_api) == 1
    fetched = _interview(
        await send(
            interviews_api.app,
            "GET",
            f"/interviews/{interview_id}",
            headers=owner,
        ),
        200,
    )
    assert fetched == created


@pytest.mark.asyncio
async def test_delete_interview_collection_is_not_allowed(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _WHEN_Z, "notes": "remove"},
        ),
        201,
    )
    remaining = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _LATER_Z,
                "notes": "remain",
                "outcome": "later",
            },
        ),
        201,
    )
    interview_id = str(created["id"])
    remaining_id = str(remaining["id"])
    blocked = await send(
        interviews_api.app,
        "DELETE",
        f"/applications/{application_id}/interviews",
        headers=owner,
    )
    assert blocked.history == []
    assert blocked.status_code == 405
    assert await _interview_count(interviews_api) == 2
    deleted = await send(
        interviews_api.app,
        "DELETE",
        f"/interviews/{interview_id}",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 204
    assert deleted.content == b""
    still_blocked = await send(
        interviews_api.app,
        "DELETE",
        f"/applications/{application_id}/interviews",
        headers=owner,
    )
    assert still_blocked.history == []
    assert still_blocked.status_code == 405
    session = interviews_api.session_factory()
    try:
        removed = await session.get(Interview, UUID(interview_id))
    finally:
        await session.close()
    assert removed is None
    assert await _stored(interviews_api, remaining_id) == (
        _LATER_Z_UTC,
        "remain",
        "later",
    )
    assert await _interview_count(interviews_api) == 1
    assert await _status_and_history(interviews_api, application_id) == (
        "APPLIED",
        1,
    )


@pytest.mark.asyncio
async def test_delete_one_of_two_owned_interviews_keeps_id_order(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    first = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _WHEN_Z, "notes": "first", "outcome": "pending"},
        ),
        201,
    )
    second = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _LATER_Z, "notes": "second", "outcome": "later"},
        ),
        201,
    )
    before = _items(
        await send(
            interviews_api.app,
            "GET",
            f"/applications/{application_id}/interviews",
            headers=owner,
        )
    )
    ordered_ids = [str(item["id"]) for item in before]
    assert ordered_ids == sorted(ordered_ids)
    assert ordered_ids == await _ordered_ids(interviews_api, application_id)
    assert set(ordered_ids) == {str(first["id"]), str(second["id"])}
    removed_id = ordered_ids[0]
    survivor_id = ordered_ids[1]
    deleted = await send(
        interviews_api.app,
        "DELETE",
        f"/interviews/{removed_id}",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 204
    assert deleted.content == b""
    after = _items(
        await send(
            interviews_api.app,
            "GET",
            f"/applications/{application_id}/interviews",
            headers=owner,
        )
    )
    survivor_ids = [str(item["id"]) for item in after]
    assert survivor_ids == [survivor_id]
    assert survivor_ids == sorted(survivor_ids)
    assert survivor_ids == await _ordered_ids(interviews_api, application_id)
    assert removed_id not in survivor_ids
    session = interviews_api.session_factory()
    try:
        removed = await session.get(Interview, UUID(removed_id))
    finally:
        await session.close()
    assert removed is None
    assert await _interview_count(interviews_api) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "user_id",
            "123e4567-e89b-12d3-a456-426614174099",
            _unexpected("user_id"),
        ),
        (
            "application_id",
            "123e4567-e89b-12d3-a456-426614174098",
            _unexpected("application_id"),
        ),
        ("status", "NOT_A_STATUS", _UNSUPPORTED_STATUS),
        ("status", "scheduled", _UNSUPPORTED_STATUS),
        ("status", "", _UNSUPPORTED_STATUS),
        ("status", " SCHEDULED ", _UNSUPPORTED_STATUS),
        ("status", 4, _UNSUPPORTED_STATUS),
        ("interview_type", "onsite", _UNSUPPORTED_TYPE),
        ("interview_type", "", _UNSUPPORTED_TYPE),
        ("interview_type", " ONSITE ", _UNSUPPORTED_TYPE),
        ("interview_type", False, _UNSUPPORTED_TYPE),
    ],
)
async def test_client_cannot_send_owner_status_or_type(
    interviews_api: _InterviewsApi,
    field: str,
    value: object,
    expected: dict[str, object],
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    rejected = await send(
        interviews_api.app,
        "POST",
        f"/applications/{application_id}/interviews",
        headers=owner,
        json={"scheduled_at": _WHEN_Z, field: value},
    )
    assert rejected.status_code == 422
    assert rejected.json() == expected
    _assert_rejected_value_hidden(value, rejected.text)
    _assert_public_error(rejected.json())
    assert await _interview_count(interviews_api) == 0
    create_body: dict[str, object] = {
        "scheduled_at": _WHEN_Z,
        "notes": "keep",
        "outcome": "pending",
    }
    if field in {"status", "interview_type"}:
        create_body["status"] = "SCHEDULED"
        create_body["interview_type"] = "PHONE"
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json=create_body,
        ),
        201,
    )
    if field in {"status", "interview_type"}:
        assert created["status"] == "SCHEDULED"
        assert created["interview_type"] == "PHONE"
    else:
        assert created["status"] is None
        assert created["interview_type"] is None
    patched = await send(
        interviews_api.app,
        "PATCH",
        f"/interviews/{created['id']}",
        headers=owner,
        json={field: value},
    )
    assert patched.status_code == 422
    assert patched.json() == expected
    _assert_rejected_value_hidden(value, patched.text)
    _assert_public_error(patched.json())
    assert await _stored(interviews_api, str(created["id"])) == (
        _WHEN_Z_UTC,
        "keep",
        "pending",
    )
    if field in {"status", "interview_type"}:
        assert await _stored_status_and_type(interviews_api, str(created["id"])) == (
            "SCHEDULED",
            "PHONE",
        )
    else:
        assert await _stored_status_and_type(interviews_api, str(created["id"])) == (
            None,
            None,
        )
    assert await _interview_count(interviews_api) == 1


@pytest.mark.asyncio
async def test_create_stores_each_status_and_type_or_null(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    for status in _INTERVIEW_STATUSES:
        created = _interview(
            await send(
                interviews_api.app,
                "POST",
                f"/applications/{application_id}/interviews",
                headers=owner,
                json={"scheduled_at": _WHEN_Z, "status": status},
            ),
            201,
        )
        assert created["status"] == status
        assert created["interview_type"] is None
        assert created["notes"] is None
        assert created["outcome"] is None
        assert await _stored(interviews_api, str(created["id"])) == (
            _WHEN_Z_UTC,
            None,
            None,
        )
        assert await _stored_status_and_type(interviews_api, str(created["id"])) == (
            status,
            None,
        )
        assert await _status_and_history(interviews_api, application_id) == (
            "APPLIED",
            1,
        )
    for interview_type in _INTERVIEW_TYPES:
        created = _interview(
            await send(
                interviews_api.app,
                "POST",
                f"/applications/{application_id}/interviews",
                headers=owner,
                json={
                    "scheduled_at": _WHEN_Z,
                    "interview_type": interview_type,
                    "outcome": "not-a-status",
                },
            ),
            201,
        )
        assert created["status"] is None
        assert created["interview_type"] == interview_type
        assert created["outcome"] == "not-a-status"
        assert await _stored(interviews_api, str(created["id"])) == (
            _WHEN_Z_UTC,
            None,
            "not-a-status",
        )
        assert await _stored_status_and_type(interviews_api, str(created["id"])) == (
            None,
            interview_type,
        )
        assert await _status_and_history(interviews_api, application_id) == (
            "APPLIED",
            1,
        )
    omitted = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={"scheduled_at": _WHEN_Z},
        ),
        201,
    )
    explicit = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _LATER_Z,
                "status": None,
                "interview_type": None,
            },
        ),
        201,
    )
    assert omitted["status"] is None
    assert omitted["interview_type"] is None
    assert explicit["status"] is None
    assert explicit["interview_type"] is None
    assert await _stored_status_and_type(interviews_api, str(omitted["id"])) == (
        None,
        None,
    )
    assert await _stored_status_and_type(interviews_api, str(explicit["id"])) == (
        None,
        None,
    )
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)


@pytest.mark.asyncio
async def test_patch_sets_keeps_and_clears_status_and_type(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _WHEN_Z,
                "notes": "keep",
                "outcome": "not-a-status",
            },
        ),
        201,
    )
    interview_id = str(created["id"])
    assert created["status"] is None
    assert created["interview_type"] is None
    assert created["outcome"] == "not-a-status"
    set_status = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"status": "COMPLETED"},
        ),
        200,
    )
    assert set_status["status"] == "COMPLETED"
    assert set_status["interview_type"] is None
    assert set_status["notes"] == "keep"
    assert set_status["outcome"] == "not-a-status"
    assert _utc_instant(set_status["scheduled_at"]) == _WHEN_Z_UTC
    set_type = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"interview_type": "VIDEO"},
        ),
        200,
    )
    assert set_type["status"] == "COMPLETED"
    assert set_type["interview_type"] == "VIDEO"
    assert set_type["outcome"] == "not-a-status"
    omitted = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"notes": "next"},
        ),
        200,
    )
    assert omitted["status"] == "COMPLETED"
    assert omitted["interview_type"] == "VIDEO"
    assert omitted["notes"] == "next"
    assert omitted["outcome"] == "not-a-status"
    assert await _stored(interviews_api, interview_id) == (
        _WHEN_Z_UTC,
        "next",
        "not-a-status",
    )
    assert await _stored_status_and_type(interviews_api, interview_id) == (
        "COMPLETED",
        "VIDEO",
    )
    cleared_type = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"interview_type": None},
        ),
        200,
    )
    assert cleared_type["status"] == "COMPLETED"
    assert cleared_type["interview_type"] is None
    assert cleared_type["notes"] == "next"
    assert cleared_type["outcome"] == "not-a-status"
    cleared_status = _interview(
        await send(
            interviews_api.app,
            "PATCH",
            f"/interviews/{interview_id}",
            headers=owner,
            json={"status": None},
        ),
        200,
    )
    assert cleared_status["status"] is None
    assert cleared_status["interview_type"] is None
    assert cleared_status["notes"] == "next"
    assert cleared_status["outcome"] == "not-a-status"
    assert await _stored(interviews_api, interview_id) == (
        _WHEN_Z_UTC,
        "next",
        "not-a-status",
    )
    assert await _stored_status_and_type(interviews_api, interview_id) == (None, None)
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)


@pytest.mark.asyncio
async def test_list_keeps_id_order_when_status_and_type_are_set(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    first = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _WHEN_Z,
                "status": "SCHEDULED",
                "interview_type": "PHONE",
                "notes": "first",
            },
        ),
        201,
    )
    second = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _LATER_Z,
                "status": "COMPLETED",
                "interview_type": "ONSITE",
                "notes": "second",
            },
        ),
        201,
    )
    listed = _items(
        await send(
            interviews_api.app,
            "GET",
            f"/applications/{application_id}/interviews",
            headers=owner,
        )
    )
    ordered_ids = [str(item["id"]) for item in listed]
    assert ordered_ids == sorted(ordered_ids)
    assert ordered_ids == await _ordered_ids(interviews_api, application_id)
    by_id = {str(item["id"]): item for item in listed}
    assert by_id[str(first["id"])]["status"] == "SCHEDULED"
    assert by_id[str(first["id"])]["interview_type"] == "PHONE"
    assert by_id[str(second["id"])]["status"] == "COMPLETED"
    assert by_id[str(second["id"])]["interview_type"] == "ONSITE"


@pytest.mark.asyncio
async def test_delete_does_not_require_status_or_type(
    interviews_api: _InterviewsApi,
) -> None:
    _owner_id, owner = await _account(interviews_api, "owner@example.com")
    _vacancy_id, application_id = await _create_application(interviews_api, owner)
    created = _interview(
        await send(
            interviews_api.app,
            "POST",
            f"/applications/{application_id}/interviews",
            headers=owner,
            json={
                "scheduled_at": _WHEN_Z,
                "status": "CANCELLED",
                "interview_type": "VIDEO",
            },
        ),
        201,
    )
    deleted = await send(
        interviews_api.app,
        "DELETE",
        f"/interviews/{created['id']}",
        headers=owner,
    )
    assert deleted.history == []
    assert deleted.status_code == 204
    assert deleted.content == b""
    session = interviews_api.session_factory()
    try:
        removed = await session.get(Interview, UUID(str(created["id"])))
    finally:
        await session.close()
    assert removed is None
    assert await _interview_count(interviews_api) == 0
    assert await _status_and_history(interviews_api, application_id) == ("APPLIED", 1)
