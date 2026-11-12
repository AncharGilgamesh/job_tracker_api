"""Authentication transactions against the job_tracker_test database."""

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from uuid import uuid4

import pytest
import pytest_asyncio
from argon2 import PasswordHasher, Type
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from job_tracker.db.models.refresh_token import RefreshToken
from job_tracker.db.models.user import User
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services import auth as auth_module
from job_tracker.services.auth import (
    InvalidCredentialsError,
    authenticate,
    register_account,
)
from job_tracker.services.passwords import password_needs_rehash
from job_tracker.services.refresh_tokens import hash_refresh_token
from job_tracker.services.users import create_user, get_user_by_email

PASSWORD = "c" * 15


@cache
def _schema_tests() -> ModuleType:
    """Load the schema tests so these tests use their database guards."""
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_auth",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _AuthDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def auth_database() -> AsyncIterator[_AuthDatabase]:
    """Reset job_tracker_test and yield an independent session factory."""
    schema = _schema_tests()
    database_url, parsed = schema.require_test_database_url()
    await schema._prepare_database(database_url, parsed)
    await schema._run_alembic(database_url, "upgrade", "head")
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        yield _AuthDatabase(session, session_factory)
    finally:
        await session.close()
        await engine.dispose()


def _outdated_hash(password: str) -> str:
    hasher = PasswordHasher(
        time_cost=3,
        memory_cost=19456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
    encoded = hasher.hash(password)
    assert password_needs_rehash(encoded) is True
    return encoded


async def _user_count(session: AsyncSession, email: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(User).where(User.email == email)
    )
    assert isinstance(count, int)
    return count


async def _tokens_for(session: AsyncSession, user_id: object) -> list[RefreshToken]:
    result = await session.scalars(
        select(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    return list(result.all())


@pytest.mark.asyncio
async def test_postgres_registration_commits_a_normalized_user(
    auth_database: _AuthDatabase,
) -> None:
    await register_account(
        auth_database.session,
        "  User.Name+Tag@Example.COM  ",
        PASSWORD,
    )
    observer = auth_database.session_factory()
    try:
        user = await get_user_by_email(observer, "user.name+tag@example.com")
        assert user is not None
        assert user.email == "user.name+tag@example.com"
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash
        assert await _tokens_for(observer, user.id) == []
        assert await _user_count(observer, "user.name+tag@example.com") == 1
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_duplicate_registration_matches_success(
    auth_database: _AuthDatabase,
) -> None:
    email = "user.name+tag@example.com"
    await register_account(
        auth_database.session,
        "  User.Name+Tag@Example.COM ",
        PASSWORD,
    )
    second = auth_database.session_factory()
    try:
        await register_account(second, " USER.NAME+tag@example.com", PASSWORD)
    finally:
        await second.close()

    observer = auth_database.session_factory()
    try:
        assert await _user_count(observer, email) == 1
        user = await get_user_by_email(observer, email)
        assert user is not None
        assert await _tokens_for(observer, user.id) == []
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_other_integrity_errors_are_not_suppressed(
    auth_database: _AuthDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def insert_orphan_token(
        session: AsyncSession,
        email: str,
        password_hash: str,
    ) -> User:
        del email, password_hash
        session.add(
            RefreshToken(
                user_id=uuid4(),
                family_id=uuid4(),
                token_hash="orphan-token-hash",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.flush()
        raise AssertionError("the foreign key should have rejected the token")

    monkeypatch.setattr(auth_module, "create_user", insert_orphan_token)

    with pytest.raises(IntegrityError) as exc_info:
        await register_account(
            auth_database.session,
            "other@example.com",
            PASSWORD,
        )

    assert auth_module._is_users_email_unique_violation(exc_info.value) is False
    observer = auth_database.session_factory()
    try:
        assert await get_user_by_email(observer, "other@example.com") is None
        count = await observer.scalar(select(func.count()).select_from(RefreshToken))
        assert count == 0
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_login_commits_a_new_refresh_family(
    auth_database: _AuthDatabase,
) -> None:
    email = "login@example.com"
    await register_account(auth_database.session, email, PASSWORD)
    login = auth_database.session_factory()
    try:
        first = await authenticate(login, f"  {email.upper()} ", PASSWORD)
        second = await authenticate(login, email, PASSWORD)
    finally:
        await login.close()

    observer = auth_database.session_factory()
    try:
        user = await get_user_by_email(observer, email)
        assert user is not None
        tokens = await _tokens_for(observer, user.id)
        assert len(tokens) == 2
        families = {token.family_id for token in tokens}
        assert len(families) == 2
        stored_hashes = {token.token_hash for token in tokens}
        assert stored_hashes == {
            hash_refresh_token(first.refresh_token),
            hash_refresh_token(second.refresh_token),
        }
        for token in tokens:
            assert token.revoked_at is None
            assert token.replaced_by_id is None
            assert first.refresh_token not in token.token_hash
            assert second.refresh_token not in token.token_hash
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_failed_login_rolls_back(
    auth_database: _AuthDatabase,
) -> None:
    email = "failed@example.com"
    await register_account(auth_database.session, email, PASSWORD)
    login = auth_database.session_factory()
    try:
        with pytest.raises(InvalidCredentialsError):
            await authenticate(login, email, "d" * 15)
        with pytest.raises(InvalidCredentialsError):
            await authenticate(login, "missing@example.com", PASSWORD)
    finally:
        await login.close()

    observer = auth_database.session_factory()
    try:
        user = await get_user_by_email(observer, email)
        assert user is not None
        assert await _tokens_for(observer, user.id) == []
        assert await _user_count(observer, "missing@example.com") == 0
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_rehash_and_refresh_commit_together(
    auth_database: _AuthDatabase,
) -> None:
    email = "rehash@example.com"
    outdated = _outdated_hash(PASSWORD)
    user = await create_user(auth_database.session, email, outdated)
    await auth_database.session.commit()
    user_id = user.id

    login = auth_database.session_factory()
    try:
        result = await authenticate(login, email, PASSWORD)
    finally:
        await login.close()

    observer = auth_database.session_factory()
    try:
        stored = await get_user_by_email(observer, email)
        assert stored is not None
        assert stored.id == user_id
        assert stored.password_hash != outdated
        assert password_needs_rehash(stored.password_hash) is False
        tokens = await _tokens_for(observer, user_id)
        assert len(tokens) == 1
        assert tokens[0].token_hash == hash_refresh_token(result.refresh_token)
        assert tokens[0].family_id != user_id
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_refresh_insert_failure_leaves_no_partial_row(
    auth_database: _AuthDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = "atomic@example.com"
    outdated = _outdated_hash(PASSWORD)
    await create_user(auth_database.session, email, outdated)
    await auth_database.session.commit()

    async def fail_after_rehash(session: AsyncSession, user_id: object) -> str:
        del user_id
        session.add(
            RefreshToken(
                user_id=uuid4(),
                family_id=uuid4(),
                token_hash="partial-token-hash",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.flush()
        return "unused"

    monkeypatch.setattr(
        auth_module,
        "issue_initial_refresh_token",
        fail_after_rehash,
    )
    login = auth_database.session_factory()
    try:
        with pytest.raises(IntegrityError):
            await authenticate(login, email, PASSWORD)
    finally:
        await login.close()

    observer = auth_database.session_factory()
    try:
        stored = await get_user_by_email(observer, email)
        assert stored is not None
        assert stored.password_hash == outdated
        assert await _tokens_for(observer, stored.id) == []
    finally:
        await observer.close()
