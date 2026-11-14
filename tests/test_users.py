"""Email normalization tests and PostgreSQL user persistence tests."""

import importlib.util
from collections.abc import AsyncIterator
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services.users import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    normalize_email,
    parse_email,
)


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so persistence tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_email_strips_surrounding_whitespace() -> None:
    assert normalize_email("  user.name+tag@example.com  ") == (
        "user.name+tag@example.com"
    )


def test_email_lowercases_the_whole_address() -> None:
    assert normalize_email("User.Name+Tag@Example.COM") == ("user.name+tag@example.com")


def test_email_preserves_dots() -> None:
    assert normalize_email("first.last@example.com") == "first.last@example.com"


def test_email_preserves_plus_tags() -> None:
    assert normalize_email("user+tag@example.com") == "user+tag@example.com"


def test_email_normalization_is_idempotent() -> None:
    original = "  User.Name+Tag@Example.COM  "
    once = normalize_email(original)
    assert normalize_email(once) == once


def test_email_lowercase_is_not_casefold() -> None:
    assert normalize_email("Straße@Example.COM") == "straße@example.com"


def test_parse_email_uses_full_lowercase_after_emailstr() -> None:
    assert parse_email("  User.Name+Tag@Example.COM  ") == ("user.name+tag@example.com")


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "user@", "@example.com", "user name@example.com"],
)
def test_invalid_email_is_rejected_by_emailstr(email: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        parse_email(email)

    errors = exc_info.value.errors()
    assert any(error["loc"] == ("email",) for error in errors)
    assert email not in str(exc_info.value)


class _UserDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def user_database() -> AsyncIterator[_UserDatabase]:
    """Reset job_tracker_test and yield an uncommitted session factory."""
    schema = _schema_tests()
    database_url, parsed = schema.require_test_database_url()
    await schema._prepare_database(database_url, parsed)
    await schema._run_alembic(database_url, "upgrade", "head")
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        try:
            current_database = await session.scalar(text("SELECT current_database()"))
        except SQLAlchemyError:
            raise RuntimeError(
                "Could not open a session for job_tracker_test."
            ) from None
        if current_database != schema.TEST_DATABASE_NAME:
            raise RuntimeError(
                "Refusing to test a database other than job_tracker_test."
            )
        yield _UserDatabase(session, session_factory)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_user_lookup_and_stored_values(
    user_database: _UserDatabase,
) -> None:
    session = user_database.session
    supplied_hash = "supplied-encoded-hash"
    user = await create_user(
        session,
        "  User.Name+Tag@Example.COM  ",
        supplied_hash,
    )

    assert isinstance(user.id, UUID)
    assert user.email == "user.name+tag@example.com"
    assert user.password_hash == supplied_hash

    found = await get_user_by_email(session, " USER.NAME+tag@example.com ")
    missing_tag = await get_user_by_email(session, "user.name@example.com")
    missing_dots = await get_user_by_email(session, "username+tag@example.com")
    other = await get_user_by_email(session, "other@example.com")

    assert found is not None
    assert found.id == user.id
    assert missing_tag is None
    assert missing_dots is None
    assert other is None


@pytest.mark.asyncio
async def test_postgres_create_user_does_not_commit(
    user_database: _UserDatabase,
) -> None:
    session = user_database.session

    async def fail_commit(*args: object, **kwargs: object) -> None:
        raise AssertionError("create_user committed the session")

    session.commit = fail_commit  # type: ignore[method-assign]
    await create_user(session, "  User@Example.COM ", "supplied-encoded-hash")

    other = user_database.session_factory()
    try:
        found = await get_user_by_email(other, "user@example.com")
    finally:
        await other.close()

    assert found is None


@pytest.mark.asyncio
async def test_postgres_duplicate_email_conflicts_with_unique_constraint(
    user_database: _UserDatabase,
) -> None:
    session = user_database.session
    await create_user(session, "User@Example.com", "first-encoded-hash")

    with pytest.raises(IntegrityError):
        await create_user(session, " user@example.com ", "second-encoded-hash")

    await session.rollback()


@pytest.mark.asyncio
async def test_postgres_get_user_by_id_returns_the_stored_user_or_none(
    user_database: _UserDatabase,
) -> None:
    user = await create_user(
        user_database.session,
        "Lookup@Example.com",
        "supplied-encoded-hash",
    )
    await user_database.session.commit()
    user_id = user.id

    observer = user_database.session_factory()
    try:
        found = await get_user_by_id(observer, user_id)
        missing = await get_user_by_id(observer, uuid4())
    finally:
        await observer.close()

    assert found is not None
    assert found.id == user_id
    assert found.email == "lookup@example.com"
    assert found.password_hash == "supplied-encoded-hash"
    assert missing is None
