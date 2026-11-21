"""Interview persistence against job_tracker_test."""

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.auth_support import other_unique_error

from job_tracker.db.models.application import Application
from job_tracker.db.models.application_status_history import ApplicationStatusHistory
from job_tracker.db.models.interview import Interview
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services.applications import create_application
from job_tracker.services.companies import create_company
from job_tracker.services.interviews import (
    create_interview,
    get_interview,
    list_interviews,
    update_interview,
)
from job_tracker.services.users import create_user
from job_tracker.services.vacancies import create_vacancy

_WHEN = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


class _InterviewDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_interviews",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def interview_database() -> AsyncIterator[_InterviewDatabase]:
    """Reset job_tracker_test and require PostgreSQL 17 before any assertion."""
    schema = _schema_tests()
    database_url, parsed = schema.require_test_database_url()
    await schema._prepare_database(database_url, parsed)
    await schema._run_alembic(database_url, "upgrade", "head")
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        session = session_factory()
        try:
            current_database = await session.scalar(text("SELECT current_database()"))
            version_num = await session.scalar(text("SHOW server_version_num"))
            if current_database != schema.TEST_DATABASE_NAME:
                raise RuntimeError(
                    "Refusing to test a database other than job_tracker_test."
                )
            if not isinstance(version_num, str) or not version_num.startswith("17"):
                raise RuntimeError("Interview tests require PostgreSQL 17.")
        finally:
            await session.close()
        worker = session_factory()
        try:
            yield _InterviewDatabase(worker, session_factory)
        finally:
            await worker.close()
    finally:
        await engine.dispose()


async def _add_user(session: AsyncSession, email: str) -> UUID:
    user = await create_user(session, email, "encoded-hash")
    await session.commit()
    return user.id


async def _seed(database: _InterviewDatabase, email: str) -> tuple[UUID, UUID]:
    owner_id = await _add_user(database.session, email)
    company = await create_company(database.session, owner_id, "Acme")
    vacancy = await create_vacancy(
        database.session,
        owner_id,
        company.id,
        "Backend Engineer",
        None,
        None,
        None,
    )
    application = await create_application(database.session, vacancy.id, owner_id)
    return owner_id, application.id


async def _load(
    database: _InterviewDatabase,
    interview_id: UUID,
) -> tuple[str | None, str | None, datetime]:
    session = database.session_factory()
    try:
        interview = await session.get(Interview, interview_id)
        if interview is None:
            raise AssertionError("Interview was not stored.")
        return interview.notes, interview.outcome, interview.scheduled_at
    finally:
        await session.close()


async def _status_and_type(
    database: _InterviewDatabase,
    interview_id: UUID,
) -> tuple[str | None, str | None]:
    session = database.session_factory()
    try:
        interview = await session.get(Interview, interview_id)
        if interview is None:
            raise AssertionError("Interview was not stored.")
        return interview.status, interview.interview_type
    finally:
        await session.close()


async def _application_state(
    database: _InterviewDatabase,
    application_id: UUID,
) -> tuple[str, int]:
    session = database.session_factory()
    try:
        status_value = await session.scalar(
            select(Application.status).where(Application.id == application_id)
        )
        history = await session.scalar(
            select(func.count())
            .select_from(ApplicationStatusHistory)
            .where(ApplicationStatusHistory.application_id == application_id)
        )
    finally:
        await session.close()
    if not isinstance(status_value, str) or not isinstance(history, int):
        raise AssertionError("Application state was not available.")
    return status_value, history


async def _interview_count(database: _InterviewDatabase) -> int:
    session = database.session_factory()
    try:
        count = await session.scalar(select(func.count()).select_from(Interview))
    finally:
        await session.close()
    if not isinstance(count, int):
        raise AssertionError("Interview count was not an integer.")
    return count


@pytest.mark.asyncio
async def test_failed_update_rolls_back_and_keeps_notes(
    interview_database: _InterviewDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, application_id = await _seed(interview_database, "owner@example.com")
    session = interview_database.session
    created = await create_interview(
        session,
        application_id,
        owner_id,
        _WHEN,
        "keep",
        "pending",
    )
    interview_id = created.id
    assert await _application_state(interview_database, application_id) == (
        "APPLIED",
        1,
    )

    async def fail_commit() -> None:
        raise RuntimeError("update failed")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="update failed"):
        await update_interview(
            session,
            interview_id,
            owner_id,
            {"notes": "changed"},
        )
    assert session.in_transaction() is False
    assert await _load(interview_database, interview_id) == ("keep", "pending", _WHEN)
    assert await _application_state(interview_database, application_id) == (
        "APPLIED",
        1,
    )


@pytest.mark.asyncio
async def test_integrity_error_rolls_back_and_propagates(
    interview_database: _InterviewDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, application_id = await _seed(interview_database, "owner@example.com")
    session = interview_database.session
    error = other_unique_error()

    async def raise_other(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(session, "flush", raise_other)
    with pytest.raises(IntegrityError) as caught:
        await create_interview(
            session,
            application_id,
            owner_id,
            _WHEN,
            "not stored",
            "not stored",
        )
    assert caught.value is error
    assert session.in_transaction() is False
    assert await _interview_count(interview_database) == 0
    assert await _application_state(interview_database, application_id) == (
        "APPLIED",
        1,
    )


@pytest.mark.asyncio
async def test_reads_do_not_commit_and_foreign_matches_missing(
    interview_database: _InterviewDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, application_id = await _seed(interview_database, "owner@example.com")
    other_id, other_application_id = await _seed(
        interview_database,
        "other@example.com",
    )
    session = interview_database.session
    foreign = await create_interview(
        session,
        other_application_id,
        other_id,
        _WHEN,
        "hidden",
        "hidden",
    )
    reader = interview_database.session_factory()
    try:
        commits = 0
        original_commit = reader.commit

        async def spy_commit() -> None:
            nonlocal commits
            commits += 1
            await original_commit()

        monkeypatch.setattr(reader, "commit", spy_commit)
        assert await list_interviews(reader, application_id, owner_id) == []
        assert await list_interviews(reader, other_application_id, owner_id) is None
        assert await list_interviews(reader, uuid4(), owner_id) is None
        assert await get_interview(reader, foreign.id, owner_id) is None
        assert await get_interview(reader, uuid4(), owner_id) is None
        assert commits == 0
    finally:
        await reader.close()

    offset = datetime(2026, 6, 15, 18, 30, tzinfo=timezone(timedelta(hours=3)))
    owned = await create_interview(
        session,
        application_id,
        owner_id,
        offset,
        None,
        None,
    )
    assert owned.scheduled_at == datetime(2026, 6, 15, 15, 30, tzinfo=UTC)
    assert owned.scheduled_at.utcoffset() == timedelta(0)
    follower = interview_database.session_factory()
    try:
        read_commits = 0
        original_follow = follower.commit

        async def spy_follow() -> None:
            nonlocal read_commits
            read_commits += 1
            await original_follow()

        monkeypatch.setattr(follower, "commit", spy_follow)
        listed = await list_interviews(follower, application_id, owner_id)
        loaded = await get_interview(follower, owned.id, owner_id)
        hidden = await get_interview(follower, foreign.id, owner_id)
        assert read_commits == 0
        assert listed is not None
        listed_ids = [row.id for row in listed]
        assert loaded is not None
        loaded_id = loaded.id
        loaded_notes = loaded.notes
        loaded_outcome = loaded.outcome
        loaded_at = loaded.scheduled_at
    finally:
        await follower.close()
    assert listed_ids == [owned.id]
    assert loaded_id == owned.id
    assert loaded_notes is None
    assert loaded_outcome is None
    assert loaded_at == datetime(2026, 6, 15, 15, 30, tzinfo=UTC)
    assert loaded_at.utcoffset() == timedelta(0)
    assert hidden is None
    assert await _application_state(interview_database, application_id) == (
        "APPLIED",
        1,
    )
