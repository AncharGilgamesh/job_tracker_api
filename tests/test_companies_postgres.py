"""Company catalog persistence against job_tracker_test."""

import importlib.util
from collections.abc import AsyncIterator
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
from job_tracker.services.companies import (
    CompanyNotFoundError,
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
