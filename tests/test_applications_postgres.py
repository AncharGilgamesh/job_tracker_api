"""Application persistence against job_tracker_test."""

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
from asyncpg.exceptions import UniqueViolationError
from sqlalchemy import event, func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.auth_support import other_unique_error

from job_tracker.db.models.application import Application
from job_tracker.db.models.application_status_history import ApplicationStatusHistory
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services.applications import (
    ApplicationAlreadyExistsError,
    ApplicationHasInterviewsError,
    create_application,
    delete_application,
    list_status_history,
    update_application_status,
)
from job_tracker.services.companies import create_company
from job_tracker.services.users import create_user
from job_tracker.services.vacancies import create_vacancy, get_vacancy

_VACANCY_UNIQUE_CONSTRAINT = "applications_vacancy_id_key"
_FIXED_CHANGED_AT = datetime(2026, 11, 2, 15, 4, 5, tzinfo=UTC)


class _Clock:
    """Stand-in that records each ``datetime.now`` call."""

    calls = 0

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        cls.calls += 1
        if tz is not UTC:
            raise AssertionError("changed_at must be read with UTC.")
        return _FIXED_CHANGED_AT


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_applications",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ApplicationDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def application_database() -> AsyncIterator[_ApplicationDatabase]:
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
                raise RuntimeError("Application tests require PostgreSQL 17.")
        finally:
            await session.close()
        worker = session_factory()
        try:
            yield _ApplicationDatabase(worker, session_factory)
        finally:
            await worker.close()
    finally:
        await engine.dispose()


async def _add_user(session: AsyncSession, email: str) -> UUID:
    user = await create_user(session, email, "encoded-hash")
    await session.commit()
    return user.id


async def _seed(database: _ApplicationDatabase, email: str) -> tuple[UUID, UUID]:
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
    return owner_id, vacancy.id


async def _counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    session = session_factory()
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


def _driver_unique_violation(exc: BaseException) -> UniqueViolationError:
    """Return the asyncpg error for applications.vacancy_id, if present."""
    stack = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (
            isinstance(current, UniqueViolationError)
            and current.constraint_name == _VACANCY_UNIQUE_CONSTRAINT
            and current.sqlstate == "23505"
        ):
            return current
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            stack.append(orig)
        cause = current.__cause__
        if isinstance(cause, BaseException):
            stack.append(cause)
    raise AssertionError("applications_vacancy_id_key was not violated.")


@pytest.mark.asyncio
async def test_changed_at_stores_one_utc_now(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    _Clock.calls = 0
    monkeypatch.setattr("job_tracker.services.applications.datetime", _Clock)
    created = await create_application(
        application_database.session,
        vacancy_id,
        owner_id,
    )
    assert application_database.session.in_transaction() is False
    assert _Clock.calls == 1
    assert created.status == "APPLIED"
    assert created.vacancy_id == vacancy_id

    observer = application_database.session_factory()
    try:
        history = list(await observer.scalars(select(ApplicationStatusHistory)))
        applications = list(await observer.scalars(select(Application)))
    finally:
        await observer.close()
    assert _Clock.calls == 1
    assert len(applications) == 1
    assert len(history) == 1
    assert history[0].application_id == created.id
    assert history[0].from_status is None
    assert history[0].to_status == "APPLIED"
    assert history[0].changed_at == _FIXED_CHANGED_AT
    assert history[0].changed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_history_write_failure_rolls_back_the_flushed_application(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    session = application_database.session
    original_flush = session.flush
    calls = {"n": 0}

    async def fail_history_flush(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            with session.no_autoflush:
                applications = await session.scalar(
                    select(func.count()).select_from(Application)
                )
                history = await session.scalar(
                    select(func.count()).select_from(ApplicationStatusHistory)
                )
            assert applications == 1
            assert history == 0
            raise RuntimeError("history insert failed")
        await original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_history_flush)
    with pytest.raises(RuntimeError, match="history insert failed"):
        await create_application(session, vacancy_id, owner_id)
    assert calls["n"] == 2
    assert session.in_transaction() is False
    assert await _counts(application_database.session_factory) == (0, 0)


@pytest.mark.asyncio
async def test_other_integrity_error_rolls_back_and_propagates(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    session = application_database.session
    error = other_unique_error()

    async def raise_other(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(session, "flush", raise_other)
    with pytest.raises(IntegrityError) as caught:
        await create_application(session, vacancy_id, owner_id)
    assert caught.value is error
    assert not isinstance(caught.value, ApplicationAlreadyExistsError)
    assert session.in_transaction() is False
    assert await _counts(application_database.session_factory) == (0, 0)


@pytest.mark.asyncio
async def test_concurrent_creates_one_unique_violation_and_one_row(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    arrived = 0
    lock = asyncio.Lock()
    ready = asyncio.Event()
    release = asyncio.Event()

    async def wait_until_both_have_the_vacancy() -> None:
        nonlocal arrived
        async with lock:
            arrived += 1
            if arrived == 2:
                ready.set()
        await release.wait()

    monkeypatch.setattr(
        "job_tracker.services.applications._before_application_insert",
        wait_until_both_have_the_vacancy,
    )

    async def run() -> Application:
        session = application_database.session_factory()
        try:
            created = await create_application(session, vacancy_id, owner_id)
            assert session.in_transaction() is False
            return created
        except ApplicationAlreadyExistsError:
            assert session.in_transaction() is False
            raise
        finally:
            await session.close()

    first = asyncio.create_task(run())
    second = asyncio.create_task(run())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    finally:
        release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    successes = [result for result in results if isinstance(result, Application)]
    conflicts = [
        result
        for result in results
        if isinstance(result, ApplicationAlreadyExistsError)
    ]
    assert len(results) == 2
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].status == "APPLIED"
    assert successes[0].vacancy_id == vacancy_id
    violation = _driver_unique_violation(conflicts[0])
    assert violation.constraint_name == _VACANCY_UNIQUE_CONSTRAINT
    assert violation.sqlstate == "23505"
    assert await _counts(application_database.session_factory) == (1, 1)
    observer = application_database.session_factory()
    try:
        applications = list(await observer.scalars(select(Application)))
        history = list(await observer.scalars(select(ApplicationStatusHistory)))
    finally:
        await observer.close()
    assert len(applications) == 1
    assert applications[0].id == successes[0].id
    assert len(history) == 1
    assert history[0].application_id == applications[0].id
    assert history[0].from_status is None
    assert history[0].to_status == "APPLIED"


class _StepClock:
    """Return one UTC timestamp per ``datetime.now`` call."""

    def __init__(self, stamps: tuple[datetime, ...]) -> None:
        self.stamps = stamps
        self.calls = 0

    def now(self, tz: object = None) -> datetime:
        self.calls += 1
        if tz is not UTC:
            raise AssertionError("changed_at must be read with UTC.")
        if self.calls > len(self.stamps):
            raise AssertionError("datetime.now was called more than expected.")
        return self.stamps[self.calls - 1]


class _UnusedClock:
    """Fail if a status update reads the clock."""

    calls = 0

    def now(self, tz: object = None) -> datetime:
        self.calls += 1
        raise AssertionError("changed_at must not be read for an equal status.")


async def _history_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[ApplicationStatusHistory]:
    session = session_factory()
    try:
        rows = list(
            await session.scalars(
                select(ApplicationStatusHistory).order_by(
                    ApplicationStatusHistory.changed_at.asc(),
                    ApplicationStatusHistory.id.asc(),
                )
            )
        )
    finally:
        await session.close()
    return rows


async def _wait_for_ungranted_application_lock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Poll until a backend waits on an applications tuple lock."""
    deadline = asyncio.get_running_loop().time() + 5
    statement = text(
        "SELECT EXISTS ("
        "SELECT 1 FROM pg_locks AS application_lock "
        "WHERE application_lock.relation = CAST('applications' AS regclass) "
        "AND application_lock.locktype = 'tuple' "
        "AND EXISTS ("
        "SELECT 1 FROM pg_locks AS waiting "
        "WHERE waiting.pid = application_lock.pid "
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
                "Timed out waiting for a lock on applications that is not granted."
            )
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_status_change_stores_one_later_utc_timestamp(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    initial = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    changed = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    clock = _StepClock((initial, changed))
    monkeypatch.setattr("job_tracker.services.applications.datetime", clock)
    created = await create_application(
        application_database.session,
        vacancy_id,
        owner_id,
    )
    assert clock.calls == 1
    updated = await update_application_status(
        application_database.session,
        vacancy_id,
        owner_id,
        "SCREENING",
    )
    assert clock.calls == 2
    assert updated.status == "SCREENING"
    assert updated.id == created.id
    rows = await _history_rows(application_database.session_factory)
    assert len(rows) == 2
    assert rows[0].from_status is None
    assert rows[0].to_status == "APPLIED"
    assert rows[0].changed_at == initial
    assert rows[1].from_status == "APPLIED"
    assert rows[1].to_status == "SCREENING"
    assert rows[1].changed_at == changed
    assert rows[1].changed_at.tzinfo is not None


@pytest.mark.asyncio
async def test_history_insert_failure_rolls_back_the_status_change(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    session = application_database.session
    created = await create_application(session, vacancy_id, owner_id)
    created_id = created.id
    original_flush = session.flush
    calls = {"n": 0}

    async def fail_history_flush(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            with session.no_autoflush:
                status = await session.scalar(
                    select(Application.status).where(Application.id == created_id)
                )
                history = await session.scalar(
                    select(func.count()).select_from(ApplicationStatusHistory)
                )
            assert status == "SCREENING"
            assert history == 1
            raise RuntimeError("history insert failed")
        await original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_history_flush)
    with pytest.raises(RuntimeError, match="history insert failed"):
        await update_application_status(session, vacancy_id, owner_id, "SCREENING")
    assert calls["n"] == 2
    assert session.in_transaction() is False
    observer = application_database.session_factory()
    try:
        stored = await observer.get(Application, created_id)
        rows = list(await observer.scalars(select(ApplicationStatusHistory)))
    finally:
        await observer.close()
    assert stored is not None
    assert stored.status == "APPLIED"
    assert len(rows) == 1
    assert rows[0].from_status is None
    assert rows[0].to_status == "APPLIED"
    assert rows[0].application_id == created_id


@pytest.mark.asyncio
async def test_concurrent_status_updates_reread_under_for_update(
    application_database: _ApplicationDatabase,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    await create_application(application_database.session, vacancy_id, owner_id)
    paused = asyncio.Event()
    release = asyncio.Event()
    pause_guard = asyncio.Lock()
    held = {"done": False}
    statements: list[str] = []

    async def run(new_status: str) -> Application:
        session = application_database.session_factory()

        def capture(state: object) -> None:
            statement = getattr(state, "statement", None)
            if statement is None:
                return
            compiled = statement.compile(dialect=postgresql.dialect())
            statements.append(" ".join(str(compiled).split()))

        event.listen(session.sync_session, "do_orm_execute", capture)
        original_flush = session.flush

        async def flush_and_hold_first(*args: object, **kwargs: object) -> None:
            await original_flush(*args, **kwargs)
            async with pause_guard:
                hold = not held["done"]
                if hold:
                    held["done"] = True
            if hold:
                paused.set()
                await release.wait()

        session.flush = flush_and_hold_first  # type: ignore[method-assign]
        try:
            updated = await update_application_status(
                session,
                vacancy_id,
                owner_id,
                new_status,
            )
            assert session.in_transaction() is False
            return updated
        finally:
            await session.close()

    first = asyncio.create_task(run("SCREENING"))
    second = asyncio.create_task(run("INTERVIEW"))
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        await _wait_for_ungranted_application_lock(application_database.session_factory)
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    locked = [sql for sql in statements if "FOR UPDATE" in sql]
    assert locked
    assert all("FOR UPDATE OF applications" in sql for sql in locked)
    assert all("vacancies" not in sql.split("FOR UPDATE", 1)[1] for sql in locked)
    observer = application_database.session_factory()
    try:
        stored = list(await observer.scalars(select(Application)))
        history = await list_status_history(observer, vacancy_id, owner_id)
    finally:
        await observer.close()
    assert len(stored) == 1
    assert history is not None
    assert len(history) == 3
    assert [row.from_status for row in history] == [
        None,
        "APPLIED",
        history[1].to_status,
    ]
    assert history[2].from_status == history[1].to_status
    assert {history[1].to_status, history[2].to_status} == {"SCREENING", "INTERVIEW"}
    assert len([row for row in history if row.from_status == "APPLIED"]) == 1
    assert stored[0].status == history[-1].to_status
    assert history == sorted(history, key=lambda row: (row.changed_at, row.id))


@pytest.mark.asyncio
async def test_history_orders_equal_timestamps_by_id(
    application_database: _ApplicationDatabase,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    created = await create_application(
        application_database.session,
        vacancy_id,
        owner_id,
    )
    await update_application_status(
        application_database.session,
        vacancy_id,
        owner_id,
        "SCREENING",
    )
    tied = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    smaller = UUID("00000000-0000-4000-8000-000000000001")
    larger = UUID("00000000-0000-4000-8000-000000000002")
    editor = application_database.session_factory()
    try:
        await editor.execute(
            update(ApplicationStatusHistory)
            .where(
                ApplicationStatusHistory.application_id == created.id,
                ApplicationStatusHistory.to_status == "APPLIED",
            )
            .values(id=larger, changed_at=tied)
        )
        await editor.execute(
            update(ApplicationStatusHistory)
            .where(
                ApplicationStatusHistory.application_id == created.id,
                ApplicationStatusHistory.to_status == "SCREENING",
            )
            .values(id=smaller, changed_at=tied)
        )
        await editor.commit()
    finally:
        await editor.close()
    reader = application_database.session_factory()
    try:
        commits = 0
        original_commit = reader.commit

        async def spy_commit() -> None:
            nonlocal commits
            commits += 1
            await original_commit()

        reader.commit = spy_commit  # type: ignore[method-assign]
        rows = await list_status_history(reader, vacancy_id, owner_id)
        assert commits == 0
    finally:
        await reader.close()
    assert rows is not None
    assert [row.id for row in rows] == [smaller, larger]
    assert [row.to_status for row in rows] == ["SCREENING", "APPLIED"]
    assert rows[0].changed_at == rows[1].changed_at == tied
    assert rows[0].changed_at.tzinfo is not None
    assert rows == sorted(rows, key=lambda row: (row.changed_at, row.id))


def _history_identity(
    rows: list[ApplicationStatusHistory],
) -> list[tuple[UUID, datetime, str | None, str]]:
    return [(row.id, row.changed_at, row.from_status, row.to_status) for row in rows]


@pytest.mark.asyncio
async def test_delete_failure_after_history_removal_rolls_back(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    session = application_database.session
    created = await create_application(session, vacancy_id, owner_id)
    updated = await update_application_status(
        session,
        vacancy_id,
        owner_id,
        "WITHDRAWN",
    )
    application_id = created.id
    before = await _history_rows(application_database.session_factory)
    assert len(before) == 2
    assert updated.status == "WITHDRAWN"
    original_flush = session.flush
    calls = {"n": 0}

    async def fail_application_flush(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            with session.no_autoflush:
                applications = await session.scalar(
                    select(func.count()).select_from(Application)
                )
                history = await session.scalar(
                    select(func.count()).select_from(ApplicationStatusHistory)
                )
            assert applications == 1
            assert history == 0
            raise RuntimeError("application delete failed")
        await original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", fail_application_flush)
    with pytest.raises(RuntimeError, match="application delete failed"):
        await delete_application(session, vacancy_id, owner_id)
    assert calls["n"] == 2
    assert session.in_transaction() is False
    observer = application_database.session_factory()
    try:
        stored = await observer.get(Application, application_id)
        history = await _history_rows(application_database.session_factory)
        vacancy = await get_vacancy(observer, vacancy_id, owner_id)
    finally:
        await observer.close()
    assert stored is not None
    assert stored.status == "WITHDRAWN"
    assert stored.vacancy_id == vacancy_id
    assert _history_identity(history) == _history_identity(before)
    assert vacancy is not None
    assert vacancy.id == vacancy_id


@pytest.mark.asyncio
async def test_delete_other_integrity_error_rolls_back_and_propagates(
    application_database: _ApplicationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, vacancy_id = await _seed(application_database, "owner@example.com")
    session = application_database.session
    created = await create_application(session, vacancy_id, owner_id)
    application_id = created.id
    before = await _history_rows(application_database.session_factory)
    assert len(before) == 1
    assert created.status == "APPLIED"
    error = other_unique_error()

    async def raise_other(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(session, "flush", raise_other)
    with pytest.raises(IntegrityError) as caught:
        await delete_application(session, vacancy_id, owner_id)
    assert caught.value is error
    assert not isinstance(caught.value, ApplicationHasInterviewsError)
    assert session.in_transaction() is False
    observer = application_database.session_factory()
    try:
        stored = await observer.get(Application, application_id)
        history = await _history_rows(application_database.session_factory)
        vacancy = await get_vacancy(observer, vacancy_id, owner_id)
    finally:
        await observer.close()
    assert stored is not None
    assert stored.status == "APPLIED"
    assert stored.vacancy_id == vacancy_id
    assert _history_identity(history) == _history_identity(before)
    assert vacancy is not None
    assert vacancy.id == vacancy_id
