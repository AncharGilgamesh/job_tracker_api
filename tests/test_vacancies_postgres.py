"""Vacancy persistence against job_tracker_test."""

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_tracker.db.models.application import Application
from job_tracker.db.models.application_status_history import ApplicationStatusHistory
from job_tracker.db.models.company import Company
from job_tracker.db.models.interview import Interview
from job_tracker.db.models.user import User
from job_tracker.db.models.vacancy import Vacancy
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services import companies as company_service
from job_tracker.services.companies import (
    archive_company,
    create_company,
    get_company,
)
from job_tracker.services.users import create_user
from job_tracker.services.vacancies import (
    CompanyArchivedError,
    VacancyHasApplicationError,
    create_vacancy,
    delete_vacancy,
    get_vacancy,
    update_vacancy,
)


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_vacancies",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _VacancyDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def vacancy_database() -> AsyncIterator[_VacancyDatabase]:
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
                raise RuntimeError("Vacancy tests require PostgreSQL 17.")
        finally:
            await session.close()
        worker = session_factory()
        try:
            yield _VacancyDatabase(worker, session_factory)
        finally:
            await worker.close()
    finally:
        await engine.dispose()


async def _add_user(session: AsyncSession, email: str) -> User:
    user = await create_user(session, email, "encoded-hash")
    await session.commit()
    return user


async def _vacancy_count(database: _VacancyDatabase) -> int:
    observer = database.session_factory()
    try:
        count = await observer.scalar(select(func.count()).select_from(Vacancy))
    finally:
        await observer.close()
    if not isinstance(count, int):
        raise AssertionError("Vacancy count was not an integer.")
    return count


async def _wait_for_ungranted_company_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Poll until a companies row lock is blocked, then return.

    PostgreSQL 17 records that wait as an ungranted transaction lock on the
    backend that holds the companies tuple lock. The tuple entry itself stays
    granted. A single sleep is not the check.
    """
    deadline = asyncio.get_running_loop().time() + 5
    statement = text(
        "SELECT EXISTS ("
        "SELECT 1 FROM pg_locks AS company_lock "
        "WHERE company_lock.relation = CAST('companies' AS regclass) "
        "AND company_lock.locktype = 'tuple' "
        "AND EXISTS ("
        "SELECT 1 FROM pg_locks AS waiting "
        "WHERE waiting.pid = company_lock.pid "
        "AND NOT waiting.granted"
        ")"
        ")"
    )
    while True:
        session = session_factory()
        try:
            waiting = await session.scalar(statement)
        finally:
            await session.close()
        if waiting:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "Timed out waiting for a lock on companies that is not granted."
            )
        await asyncio.sleep(0.05)


def _pause_after_first_lock(
    monkeypatch: pytest.MonkeyPatch,
    locked: asyncio.Event,
    release: asyncio.Event,
) -> None:
    original = company_service.lock_company_by_id

    async def lock_and_pause_first(
        session: AsyncSession,
        company_id: UUID,
    ) -> Company | None:
        row = await original(session, company_id)
        if not locked.is_set():
            locked.set()
            await release.wait()
        return row

    monkeypatch.setattr(company_service, "lock_company_by_id", lock_and_pause_first)


async def _finish(task: asyncio.Task[object] | None) -> None:
    if task is None or task.done():
        return
    done, _pending = await asyncio.wait({task}, timeout=5)
    if not done:
        task.cancel()
        await asyncio.wait({task})
    if task.done() and not task.cancelled():
        task.exception()


@pytest.mark.asyncio
async def test_archive_lock_blocks_create_until_the_company_is_archived(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await _add_user(vacancy_database.session, "creator@example.com")
    company = await create_company(vacancy_database.session, creator.id, "Acme")
    title = "Blocked Role"
    locked = asyncio.Event()
    release = asyncio.Event()
    _pause_after_first_lock(monkeypatch, locked, release)
    archive_session = vacancy_database.session_factory()
    create_session = vacancy_database.session_factory()
    archive_task: asyncio.Task[Company] | None = None
    create_task: asyncio.Task[Vacancy] | None = None
    try:
        archive_task = asyncio.create_task(
            archive_company(archive_session, company.id, creator.id)
        )
        await asyncio.wait_for(locked.wait(), timeout=5)
        create_task = asyncio.create_task(
            create_vacancy(
                create_session,
                creator.id,
                company.id,
                title,
                None,
                None,
                None,
            )
        )
        await _wait_for_ungranted_company_lock(vacancy_database.session_factory)
        release.set()
        await archive_task
        with pytest.raises(CompanyArchivedError) as caught:
            await create_task
        assert str(caught.value) == "The company is archived"
        observer = vacancy_database.session_factory()
        try:
            stored_company = await get_company(observer, company.id)
            vacancies = await observer.scalar(
                select(func.count())
                .select_from(Vacancy)
                .where(Vacancy.user_id == creator.id, Vacancy.title == title)
            )
        finally:
            await observer.close()
        assert stored_company is not None
        assert stored_company.archived_at is not None
        assert vacancies == 0
    finally:
        release.set()
        await _finish(archive_task)
        await _finish(create_task)
        await archive_session.close()
        await create_session.close()


@pytest.mark.asyncio
async def test_create_lock_blocks_archive_until_the_vacancy_commits(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await _add_user(vacancy_database.session, "creator@example.com")
    company = await create_company(vacancy_database.session, creator.id, "Acme")
    title = "Committed Role"
    locked = asyncio.Event()
    release = asyncio.Event()
    _pause_after_first_lock(monkeypatch, locked, release)
    create_session = vacancy_database.session_factory()
    archive_session = vacancy_database.session_factory()
    create_task: asyncio.Task[Vacancy] | None = None
    archive_task: asyncio.Task[Company] | None = None
    try:
        create_task = asyncio.create_task(
            create_vacancy(
                create_session,
                creator.id,
                company.id,
                title,
                "Notes",
                "not a url",
                "Remote",
            )
        )
        await asyncio.wait_for(locked.wait(), timeout=5)
        archive_task = asyncio.create_task(
            archive_company(archive_session, company.id, creator.id)
        )
        await _wait_for_ungranted_company_lock(vacancy_database.session_factory)
        release.set()
        created = await create_task
        archived = await archive_task
        assert created.title == title
        assert archived.archived_at is not None
        observer = vacancy_database.session_factory()
        try:
            stored_vacancy = await get_vacancy(observer, created.id, creator.id)
            stored_company = await get_company(observer, company.id)
        finally:
            await observer.close()
        assert stored_vacancy is not None
        assert stored_vacancy.title == title
        assert stored_vacancy.user_id == creator.id
        assert stored_company is not None
        assert stored_company.archived_at is not None
    finally:
        release.set()
        await _finish(create_task)
        await _finish(archive_task)
        await create_session.close()
        await archive_session.close()


@pytest.mark.asyncio
async def test_concurrent_first_archives_keep_the_first_timestamp(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await _add_user(vacancy_database.session, "creator@example.com")
    company = await create_company(vacancy_database.session, creator.id, "Acme")

    class _Clock:
        def __init__(self) -> None:
            self.calls = 0
            self.stamp = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)

        def now(self, tz: object = None) -> datetime:
            del tz
            self.calls += 1
            if self.calls == 1:
                return self.stamp
            return datetime(2026, 9, 9, 9, 9, 9, tzinfo=UTC)

    clock = _Clock()
    monkeypatch.setattr(company_service, "datetime", clock)
    first_session = vacancy_database.session_factory()
    second_session = vacancy_database.session_factory()
    try:
        first, second = await asyncio.gather(
            archive_company(first_session, company.id, creator.id),
            archive_company(second_session, company.id, creator.id),
        )
    finally:
        await first_session.close()
        await second_session.close()

    assert clock.calls == 1
    assert first.archived_at == clock.stamp
    assert second.archived_at == clock.stamp
    observer = vacancy_database.session_factory()
    try:
        stored = await get_company(observer, company.id)
    finally:
        await observer.close()
    assert stored is not None
    assert stored.archived_at == clock.stamp

    follow_up = vacancy_database.session_factory()
    try:
        repeated = await archive_company(follow_up, company.id, creator.id)
    finally:
        await follow_up.close()
    assert repeated.archived_at == clock.stamp
    assert clock.calls == 1


@pytest.mark.asyncio
async def test_same_company_and_title_only_do_not_lock_an_archived_company(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _add_user(vacancy_database.session, "owner@example.com")
    company = await create_company(vacancy_database.session, owner.id, "Acme")
    created = await create_vacancy(
        vacancy_database.session,
        owner.id,
        company.id,
        "Original",
        "Notes",
        None,
        None,
    )
    await archive_company(vacancy_database.session, company.id, owner.id)
    vacancy_id = created.id
    owner_id = owner.id
    company_id = company.id
    calls = 0
    original = company_service.lock_company_by_id

    async def _count(
        session: AsyncSession,
        locked_company_id: UUID,
    ) -> Company | None:
        nonlocal calls
        calls += 1
        return await original(session, locked_company_id)

    monkeypatch.setattr(company_service, "lock_company_by_id", _count)
    same_company = await update_vacancy(
        vacancy_database.session,
        vacancy_id,
        owner_id,
        {"company_id": company_id},
    )
    assert calls == 0
    assert same_company.company_id == company_id
    assert same_company.title == "Original"
    retitled = await update_vacancy(
        vacancy_database.session,
        vacancy_id,
        owner_id,
        {"title": "  Still Open  "},
    )
    assert calls == 0
    assert retitled.title == "  Still Open  "
    assert retitled.description == "Notes"
    assert retitled.company_id == company_id


@pytest.mark.asyncio
async def test_archive_lock_blocks_reassignment_until_the_company_is_archived(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await _add_user(vacancy_database.session, "creator@example.com")
    source = await create_company(vacancy_database.session, creator.id, "Current Co")
    target = await create_company(vacancy_database.session, creator.id, "Target Co")
    created = await create_vacancy(
        vacancy_database.session,
        creator.id,
        source.id,
        "Role",
        None,
        None,
        None,
    )
    vacancy_id = created.id
    owner_id = creator.id
    source_id = source.id
    target_id = target.id
    locked = asyncio.Event()
    release = asyncio.Event()
    _pause_after_first_lock(monkeypatch, locked, release)
    archive_session = vacancy_database.session_factory()
    update_session = vacancy_database.session_factory()
    archive_task: asyncio.Task[Company] | None = None
    update_task: asyncio.Task[Vacancy] | None = None
    try:
        archive_task = asyncio.create_task(
            archive_company(archive_session, target_id, owner_id)
        )
        await asyncio.wait_for(locked.wait(), timeout=5)
        update_task = asyncio.create_task(
            update_vacancy(
                update_session,
                vacancy_id,
                owner_id,
                {"company_id": target_id},
            )
        )
        await _wait_for_ungranted_company_lock(vacancy_database.session_factory)
        release.set()
        await archive_task
        with pytest.raises(CompanyArchivedError) as caught:
            await update_task
        assert str(caught.value) == "The company is archived"
        observer = vacancy_database.session_factory()
        try:
            stored_vacancy = await get_vacancy(observer, vacancy_id, owner_id)
            stored_target = await get_company(observer, target_id)
        finally:
            await observer.close()
        assert stored_vacancy is not None
        assert stored_vacancy.company_id == source_id
        assert stored_target is not None
        assert stored_target.archived_at is not None
    finally:
        release.set()
        await _finish(archive_task)
        await _finish(update_task)
        await archive_session.close()
        await update_session.close()


@pytest.mark.asyncio
async def test_reassignment_lock_blocks_archive_until_the_company_change_commits(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await _add_user(vacancy_database.session, "creator@example.com")
    source = await create_company(vacancy_database.session, creator.id, "Current Co")
    target = await create_company(vacancy_database.session, creator.id, "Target Co")
    created = await create_vacancy(
        vacancy_database.session,
        creator.id,
        source.id,
        "Role",
        None,
        None,
        None,
    )
    vacancy_id = created.id
    owner_id = creator.id
    target_id = target.id
    locked = asyncio.Event()
    release = asyncio.Event()
    _pause_after_first_lock(monkeypatch, locked, release)
    update_session = vacancy_database.session_factory()
    archive_session = vacancy_database.session_factory()
    update_task: asyncio.Task[Vacancy] | None = None
    archive_task: asyncio.Task[Company] | None = None
    try:
        update_task = asyncio.create_task(
            update_vacancy(
                update_session,
                vacancy_id,
                owner_id,
                {"company_id": target_id},
            )
        )
        await asyncio.wait_for(locked.wait(), timeout=5)
        archive_task = asyncio.create_task(
            archive_company(archive_session, target_id, owner_id)
        )
        await _wait_for_ungranted_company_lock(vacancy_database.session_factory)
        release.set()
        updated = await update_task
        archived = await archive_task
        assert updated.company_id == target_id
        assert archived.archived_at is not None
        observer = vacancy_database.session_factory()
        try:
            stored_vacancy = await get_vacancy(observer, vacancy_id, owner_id)
            stored_target = await get_company(observer, target_id)
        finally:
            await observer.close()
        assert stored_vacancy is not None
        assert stored_vacancy.company_id == target_id
        assert stored_target is not None
        assert stored_target.archived_at is not None
    finally:
        release.set()
        await _finish(update_task)
        await _finish(archive_task)
        await update_session.close()
        await archive_session.close()


@pytest.mark.asyncio
async def test_delete_with_an_application_rolls_back_and_keeps_related_rows(
    vacancy_database: _VacancyDatabase,
) -> None:
    owner = await _add_user(vacancy_database.session, "owner@example.com")
    company = await create_company(vacancy_database.session, owner.id, "Acme")
    created = await create_vacancy(
        vacancy_database.session,
        owner.id,
        company.id,
        "Applied Role",
        None,
        None,
        None,
    )
    session = vacancy_database.session
    application = Application(vacancy_id=created.id, status="APPLIED")
    session.add(application)
    await session.flush()
    changed_at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
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
    vacancy_id = created.id
    owner_id = owner.id
    application_id = application.id
    history_id = history.id
    interview_id = interview.id
    await session.commit()

    with pytest.raises(VacancyHasApplicationError) as error:
        await delete_vacancy(session, vacancy_id, owner_id)
    assert str(error.value) == "The vacancy has an application"
    assert session.in_transaction() is False

    observer = vacancy_database.session_factory()
    try:
        stored = await get_vacancy(observer, vacancy_id, owner_id)
        stored_application = await observer.get(Application, application_id)
        stored_history = await observer.get(ApplicationStatusHistory, history_id)
        stored_interview = await observer.get(Interview, interview_id)
    finally:
        await observer.close()
    assert stored is not None
    assert stored.id == vacancy_id
    assert stored_application is not None
    assert stored_application.vacancy_id == vacancy_id
    assert stored_application.status == "APPLIED"
    assert stored_history is not None
    assert stored_history.application_id == application_id
    assert stored_history.from_status is None
    assert stored_history.to_status == "APPLIED"
    assert stored_history.changed_at == changed_at
    assert stored_interview is not None
    assert stored_interview.application_id == application_id


@pytest.mark.asyncio
async def test_delete_does_not_lock_the_company(
    vacancy_database: _VacancyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await _add_user(vacancy_database.session, "owner@example.com")
    company = await create_company(vacancy_database.session, owner.id, "Acme")
    created = await create_vacancy(
        vacancy_database.session,
        owner.id,
        company.id,
        "Unlocked Role",
        None,
        None,
        None,
    )
    calls = 0
    original = company_service.lock_company_by_id

    async def _count(
        session: AsyncSession,
        locked_company_id: UUID,
    ) -> Company | None:
        nonlocal calls
        calls += 1
        return await original(session, locked_company_id)

    monkeypatch.setattr(company_service, "lock_company_by_id", _count)
    await delete_vacancy(vacancy_database.session, created.id, owner.id)
    assert calls == 0
