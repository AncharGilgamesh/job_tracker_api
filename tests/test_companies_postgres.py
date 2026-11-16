"""Company catalog persistence against job_tracker_test."""

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_tracker.db.models.company import Company
from job_tracker.db.models.user import User
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services import companies as company_service
from job_tracker.services.companies import (
    CompanyForbiddenError,
    CompanyNotFoundError,
    InvalidCompanyNameError,
    archive_company,
    create_company,
    get_company,
    list_companies,
    rename_company,
    suggest_companies,
)
from job_tracker.services.users import create_user


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_companies",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CompanyDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def company_database() -> AsyncIterator[_CompanyDatabase]:
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
                raise RuntimeError("Company tests require PostgreSQL 17.")
        finally:
            await session.close()
        worker = session_factory()
        try:
            yield _CompanyDatabase(worker, session_factory)
        finally:
            await worker.close()
    finally:
        await engine.dispose()


async def _add_user(session: AsyncSession, email: str) -> User:
    user = await create_user(session, email, "encoded-hash")
    await session.commit()
    return user


async def _stored(database: _CompanyDatabase, company_id: UUID) -> Company | None:
    observer = database.session_factory()
    try:
        return await get_company(observer, company_id)
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_create_persists_creator_and_normalized_name(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    created = await create_company(
        company_database.session,
        creator.id,
        "  Acme   Corporation, LLC ",
    )
    german = await create_company(company_database.session, creator.id, "  Straße  ")

    assert created.created_by_user_id == creator.id
    assert created.name == "Acme Corporation, LLC"
    assert created.normalized_name == "acme corporation, llc"
    assert created.archived_at is None
    assert german.name == "Straße"
    assert german.normalized_name == "strasse"
    assert "Straße".lower() != german.normalized_name

    stored = await _stored(company_database, created.id)
    assert stored is not None
    assert stored.created_by_user_id == creator.id
    assert stored.name == "Acme Corporation, LLC"
    assert stored.normalized_name == "acme corporation, llc"
    assert stored.archived_at is None


@pytest.mark.asyncio
async def test_duplicate_normalized_names_create_distinct_rows(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    other = await _add_user(company_database.session, "other@example.com")
    first = await create_company(company_database.session, creator.id, "Acme")
    second = await create_company(company_database.session, other.id, "  ACME ")
    observer = company_database.session_factory()
    try:
        rows = await list_companies(observer)
    finally:
        await observer.close()
    assert first.id != second.id
    assert first.name == "Acme"
    assert second.name == "ACME"
    assert {row.id for row in rows} == {first.id, second.id}
    assert {row.normalized_name for row in rows} == {"acme"}
    assert {row.created_by_user_id for row in rows} == {creator.id, other.id}


@pytest.mark.asyncio
async def test_suggestions_match_exact_normalized_names_including_archived(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    session = company_database.session
    full = await create_company(session, creator.id, "  Acme   Corporation ")
    same = await create_company(session, creator.id, "ACME CORPORATION")
    prefix = await create_company(session, creator.id, "Acme")
    wildcard = await create_company(session, creator.id, "100%_Acme")
    await archive_company(session, same.id, creator.id)

    matches = await suggest_companies(session, "  acme   corporation ")
    assert {row.id for row in matches} == {full.id, same.id}
    assert all(row.normalized_name == "acme corporation" for row in matches)
    assert [row.id for row in await suggest_companies(session, "Acme")] == [prefix.id]
    assert await suggest_companies(session, "Acme Corp") == []
    assert await suggest_companies(session, "Corporation") == []
    assert await suggest_companies(session, "Acme Corporation Inc") == []
    assert {row.id for row in await suggest_companies(session, "100%_Acme")} == {
        wildcard.id
    }
    assert await suggest_companies(session, "100%") == []
    assert await suggest_companies(session, "%") == []
    assert await suggest_companies(session, "_") == []
    german = await create_company(session, creator.id, "Straße")
    assert {row.id for row in await suggest_companies(session, "STRASSE")} == {
        german.id
    }
    assert {row.id for row in await suggest_companies(session, "STRAßE")} == {german.id}


@pytest.mark.asyncio
async def test_rename_updates_both_names_and_keeps_creator_and_archive_time(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    session = company_database.session
    created = await create_company(session, creator.id, "Acme")
    archived = await archive_company(session, created.id, creator.id)
    renamed = await rename_company(
        session, created.id, creator.id, "  New   Name, LLC "
    )

    assert renamed.id == created.id
    assert renamed.name == "New Name, LLC"
    assert renamed.normalized_name == "new name, llc"
    assert renamed.created_by_user_id == creator.id
    assert renamed.archived_at == archived.archived_at
    stored = await _stored(company_database, created.id)
    assert stored is not None
    assert stored.name == "New Name, LLC"
    assert stored.normalized_name == "new name, llc"
    assert stored.created_by_user_id == creator.id
    assert stored.archived_at == archived.archived_at


@pytest.mark.asyncio
async def test_missing_company_is_not_found_before_the_creator_check(
    company_database: _CompanyDatabase,
) -> None:
    other = await _add_user(company_database.session, "other@example.com")
    missing = uuid4()
    assert await get_company(company_database.session, missing) is None
    with pytest.raises(CompanyNotFoundError) as rename_error:
        await rename_company(company_database.session, missing, other.id, "Acme")
    with pytest.raises(CompanyNotFoundError) as archive_error:
        await archive_company(company_database.session, missing, other.id)
    assert str(rename_error.value) == "The requested resource was not found"
    assert str(archive_error.value) == "The requested resource was not found"
    assert str(missing) not in str(rename_error.value)
    assert str(other.id) not in str(archive_error.value)


@pytest.mark.asyncio
async def test_non_creator_cannot_rename_or_archive(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    other = await _add_user(company_database.session, "other@example.com")
    created = await create_company(company_database.session, creator.id, "Acme LLC")
    with pytest.raises(CompanyForbiddenError) as rename_error:
        await rename_company(
            company_database.session,
            created.id,
            other.id,
            "Hijacked",
        )
    with pytest.raises(CompanyForbiddenError) as archive_error:
        await archive_company(company_database.session, created.id, other.id)
    assert str(rename_error.value) == "The request is forbidden"
    assert str(archive_error.value) == "The request is forbidden"
    assert "Hijacked" not in str(rename_error.value)
    assert str(other.id) not in str(rename_error.value)
    stored = await _stored(company_database, created.id)
    assert stored is not None
    assert stored.name == "Acme LLC"
    assert stored.normalized_name == "acme llc"
    assert stored.created_by_user_id == creator.id
    assert stored.archived_at is None


@pytest.mark.asyncio
async def test_archive_stores_one_utc_timestamp(
    company_database: _CompanyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    created = await create_company(company_database.session, creator.id, "Acme")

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
    archived = await archive_company(company_database.session, created.id, creator.id)
    repeated = await archive_company(company_database.session, created.id, creator.id)

    assert clock.calls == 1
    assert archived.archived_at == clock.stamp
    assert repeated.archived_at == clock.stamp
    stored = await _stored(company_database, created.id)
    assert stored is not None
    assert stored.archived_at == clock.stamp
    assert stored.archived_at is not None
    assert stored.archived_at.utcoffset() == clock.stamp.utcoffset()


@pytest.mark.asyncio
async def test_reads_include_archived_companies(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    session = company_database.session
    active = await create_company(session, creator.id, "Active Co")
    archived = await create_company(session, creator.id, "Old Co")
    await archive_company(session, archived.id, creator.id)
    observer = company_database.session_factory()
    try:
        rows = await list_companies(observer)
        stored_active = await get_company(observer, active.id)
        stored_archived = await get_company(observer, archived.id)
    finally:
        await observer.close()
    assert {row.id for row in rows} == {active.id, archived.id}
    assert stored_active is not None and stored_active.archived_at is None
    assert stored_archived is not None and stored_archived.archived_at is not None


@pytest.mark.asyncio
async def test_blank_name_does_not_insert_a_row(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    created = await create_company(company_database.session, creator.id, "Acme")
    with pytest.raises(InvalidCompanyNameError) as exc_info:
        await create_company(company_database.session, creator.id, " \t ")
    assert str(exc_info.value) == "A company name is required"
    with pytest.raises(InvalidCompanyNameError):
        await rename_company(company_database.session, created.id, creator.id, "   ")
    with pytest.raises(InvalidCompanyNameError):
        await suggest_companies(company_database.session, " \u3000 ")
    observer = company_database.session_factory()
    try:
        rows = await list_companies(observer)
    finally:
        await observer.close()
    assert [(row.id, row.name) for row in rows] == [(created.id, "Acme")]


@pytest.mark.asyncio
async def test_unknown_creator_rolls_back_and_reads_do_not_commit(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    created = await create_company(company_database.session, creator.id, "Acme")
    with pytest.raises(IntegrityError):
        await create_company(company_database.session, uuid4(), "Orphan Co")
    assert company_database.session.in_transaction() is False

    commits = 0
    original_commit = company_database.session.commit

    async def _spy_commit() -> None:
        nonlocal commits
        commits += 1
        await original_commit()

    company_database.session.commit = _spy_commit  # type: ignore[method-assign]
    await list_companies(company_database.session)
    await suggest_companies(company_database.session, "Acme")
    await get_company(company_database.session, created.id)
    await get_company(company_database.session, uuid4())
    assert commits == 0

    observer = company_database.session_factory()
    try:
        rows = await list_companies(observer)
    finally:
        await observer.close()
    assert [row.name for row in rows] == ["Acme"]


@pytest.mark.asyncio
async def test_failed_rename_rolls_back(
    company_database: _CompanyDatabase,
) -> None:
    creator = await _add_user(company_database.session, "creator@example.com")
    created = await create_company(company_database.session, creator.id, "Acme")
    company_id = created.id

    async def _fail_commit() -> None:
        raise RuntimeError("commit failed")

    original_commit = company_database.session.commit
    company_database.session.commit = _fail_commit  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            await rename_company(
                company_database.session,
                company_id,
                creator.id,
                "Renamed",
            )
        assert company_database.session.in_transaction() is False
    finally:
        company_database.session.commit = original_commit  # type: ignore[method-assign]
    stored = await _stored(company_database, company_id)
    assert stored is not None
    assert stored.name == "Acme"
    assert stored.normalized_name == "acme"
