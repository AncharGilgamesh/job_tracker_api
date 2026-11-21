"""Refresh rotation, replay, and logout against job_tracker_test."""

import asyncio
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
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.auth_support import (
    clear_settings_caches,
    configure_test_environment,
    make_app,
)

from job_tracker.api.deps import get_session
from job_tracker.db.models.refresh_token import RefreshToken
from job_tracker.db.models.user import User
from job_tracker.db.session import create_db_engine, create_session_factory
from job_tracker.services import auth as auth_module
from job_tracker.services.access_tokens import decode_access_token
from job_tracker.services.auth import (
    RefreshOutcome,
    logout_account,
    rotate_refresh_token,
)
from job_tracker.services.refresh_tokens import (
    REFRESH_TOKEN_TTL,
    hash_refresh_token,
)
from job_tracker.services.users import create_user


@cache
def _schema_tests() -> ModuleType:
    module_path = Path(__file__).resolve().parent / "test_schema.py"
    spec = importlib.util.spec_from_file_location(
        "job_tracker_schema_tests_refresh",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the schema test safeguards.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RefreshDatabase(NamedTuple):
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def refresh_database(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_RefreshDatabase]:
    """Reset job_tracker_test and require PostgreSQL 17 before any assertion."""
    configure_test_environment(monkeypatch)
    schema = _schema_tests()
    database_url, parsed = schema.require_test_database_url()
    await schema._prepare_database(database_url, parsed)
    await schema._run_alembic(database_url, "upgrade", "head")
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        current_database = await session.scalar(text("SELECT current_database()"))
        version_num = await session.scalar(text("SHOW server_version_num"))
        if current_database != schema.TEST_DATABASE_NAME:
            raise RuntimeError(
                "Refusing to test a database other than job_tracker_test."
            )
        if not isinstance(version_num, str) or not version_num.startswith("17"):
            raise RuntimeError("Authentication tests require PostgreSQL 17.")
        yield _RefreshDatabase(session, session_factory)
    finally:
        await session.close()
        await engine.dispose()
        clear_settings_caches()


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(days=10)


async def _user(session: AsyncSession) -> User:
    user = await create_user(session, "user@example.com", "encoded-hash")
    await session.commit()
    return user


async def _token(
    session: AsyncSession,
    user: User,
    raw_token: str,
    *,
    family_id: object | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> RefreshToken:
    row = RefreshToken(
        user_id=user.id,
        family_id=family_id or uuid4(),
        token_hash=hash_refresh_token(raw_token),
        expires_at=expires_at or _later(),
        revoked_at=revoked_at,
    )
    session.add(row)
    await session.flush()
    return row


async def _rows(session: AsyncSession, family_id: object) -> list[RefreshToken]:
    result = await session.scalars(
        select(RefreshToken).where(RefreshToken.family_id == family_id)
    )
    return list(result.all())


def _assert_revoked_replacement_chain(
    rows: list[RefreshToken],
    outcome: RefreshOutcome,
) -> None:
    """Accept either serialization: rotation-then-logout, or logout-then-replay."""
    by_id = {row.id: row for row in rows}
    links = [row for row in rows if row.replaced_by_id is not None]
    if any(row.revoked_at is None for row in rows):
        raise AssertionError("the presented family still has an active token")
    if any(row.replaced_by_id == row.id for row in rows):
        raise AssertionError("a refresh token replaces itself")
    if outcome is RefreshOutcome.ROTATED:
        if len(rows) != 2 or len(links) != 1:
            raise AssertionError("rotation did not leave one revoked replacement")
    elif outcome is RefreshOutcome.REPLAY:
        if len(rows) != 1 or links:
            raise AssertionError("replay left a replacement row")
    else:
        raise AssertionError("refresh finished with an unexpected outcome")
    for row in links:
        target = by_id.get(row.replaced_by_id)
        if target is None or target.family_id != row.family_id:
            raise AssertionError("replacement link leaves its family")
        if target.replaced_by_id is not None or target.revoked_at is None:
            raise AssertionError("replacement chain is not a single revoked link")


def _app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    application = make_app()

    async def override() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
        finally:
            await session.close()

    application.dependency_overrides[get_session] = override
    return application


@pytest.mark.asyncio
async def test_postgres_rotation_links_one_family_and_hides_the_raw_token(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    original = await _token(refresh_database.session, user, "active-refresh-token")
    await refresh_database.session.commit()
    family_id = original.family_id
    worker = refresh_database.session_factory()
    try:
        result = await rotate_refresh_token(worker, "active-refresh-token")
    finally:
        await worker.close()

    if result.outcome is not RefreshOutcome.ROTATED or result.refresh_token is None:
        raise AssertionError("active refresh token did not rotate")
    if result.expires_in != 900 or result.token_type != "bearer":
        raise AssertionError("refresh did not return the access-token contract")
    if decode_access_token(result.access_token or "") != user.id:
        raise AssertionError("refresh access token subject did not match the user")
    observer = refresh_database.session_factory()
    try:
        rows = await _rows(observer, family_id)
        stored = {row.token_hash: row for row in rows}
        replacement_hash = hash_refresh_token(result.refresh_token)
        if replacement_hash not in stored or original.token_hash not in stored:
            raise AssertionError("rotation did not store both token hashes")
        previous = stored[original.token_hash]
        replacement = stored[replacement_hash]
        if previous.revoked_at is None or previous.replaced_by_id != replacement.id:
            raise AssertionError("rotation did not link the revoked token")
        if replacement.family_id != family_id or replacement.revoked_at is not None:
            raise AssertionError("replacement did not stay in the active family")
        if replacement.replaced_by_id is not None:
            raise AssertionError("replacement was already linked")
        remaining = replacement.expires_at - previous.revoked_at
        if abs(remaining - REFRESH_TOKEN_TTL) > timedelta(seconds=5):
            raise AssertionError("replacement lifetime was not 30 days")
        if result.refresh_token in replacement.token_hash:
            raise AssertionError("raw refresh token was stored")
        count = await observer.scalar(select(func.count()).select_from(RefreshToken))
        if count != 2:
            raise AssertionError("rotation stored an unexpected number of tokens")
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_expired_token_does_not_revoke_its_family(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    expired = await _token(
        refresh_database.session,
        user,
        "expired-refresh-token",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    sibling = await _token(
        refresh_database.session,
        user,
        "sibling-refresh-token",
        family_id=expired.family_id,
    )
    await refresh_database.session.commit()
    worker = refresh_database.session_factory()
    try:
        result = await rotate_refresh_token(worker, "expired-refresh-token")
    finally:
        await worker.close()

    assert result.outcome is RefreshOutcome.INVALID
    observer = refresh_database.session_factory()
    try:
        rows = await _rows(observer, expired.family_id)
        assert len(rows) == 2
        assert all(row.revoked_at is None for row in rows)
        assert all(row.replaced_by_id is None for row in rows)
        assert sibling.family_id == expired.family_id
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_replay_revokes_only_the_presented_family(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    revoked_at = datetime.now(UTC) - timedelta(days=1)
    presented = await _token(
        refresh_database.session,
        user,
        "revoked-refresh-token",
        revoked_at=revoked_at,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    active = await _token(
        refresh_database.session,
        user,
        "replacement-refresh-token",
        family_id=presented.family_id,
    )
    other = await _token(refresh_database.session, user, "other-family-token")
    await refresh_database.session.commit()
    worker = refresh_database.session_factory()
    try:
        result = await rotate_refresh_token(worker, "revoked-refresh-token")
    finally:
        await worker.close()

    assert result.outcome is RefreshOutcome.REPLAY
    observer = refresh_database.session_factory()
    try:
        family = await _rows(observer, presented.family_id)
        by_hash = {row.token_hash: row for row in family}
        replayed = by_hash[presented.token_hash]
        replacement = by_hash[active.token_hash]
        assert replayed.revoked_at == revoked_at
        assert replacement.revoked_at is not None
        other_rows = await _rows(observer, other.family_id)
        assert len(other_rows) == 1
        assert other_rows[0].revoked_at is None
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_unknown_refresh_token_revokes_nothing(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    await _token(refresh_database.session, user, "kept-refresh-token")
    await refresh_database.session.commit()
    worker = refresh_database.session_factory()
    try:
        result = await rotate_refresh_token(worker, "unknown-refresh-token")
    finally:
        await worker.close()

    assert result.outcome is RefreshOutcome.INVALID
    observer = refresh_database.session_factory()
    try:
        count = await observer.scalar(select(func.count()).select_from(RefreshToken))
        active = await observer.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.revoked_at.is_(None))
        )
        assert count == 1
        assert active == 1
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_concurrent_refresh_leaves_the_family_revoked(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    presented = await _token(refresh_database.session, user, "shared-refresh-token")
    other = await _token(refresh_database.session, user, "separate-family-token")
    await refresh_database.session.commit()
    first = refresh_database.session_factory()
    second = refresh_database.session_factory()
    try:
        results = await asyncio.gather(
            rotate_refresh_token(first, "shared-refresh-token"),
            rotate_refresh_token(second, "shared-refresh-token"),
        )
    finally:
        await first.close()
        await second.close()

    outcomes = sorted(result.outcome.name for result in results)
    if outcomes != ["REPLAY", "ROTATED"]:
        raise AssertionError(
            "concurrent refresh did not produce one rotation and one replay"
        )
    observer = refresh_database.session_factory()
    try:
        family = await _rows(observer, presented.family_id)
        if any(row.revoked_at is None for row in family):
            raise AssertionError("concurrent replay left an active family token")
        separate = await _rows(observer, other.family_id)
        if len(separate) != 1 or separate[0].revoked_at is not None:
            raise AssertionError("concurrent refresh revoked another family")
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_concurrent_refresh_and_logout_leave_one_family_revoked(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    presented = await _token(
        refresh_database.session,
        user,
        "refresh-or-logout-token",
    )
    other = await _token(refresh_database.session, user, "untouched-family-token")
    await refresh_database.session.commit()
    refresh_session = refresh_database.session_factory()
    logout_session = refresh_database.session_factory()
    try:
        refresh_result, logout_result = await asyncio.gather(
            rotate_refresh_token(refresh_session, "refresh-or-logout-token"),
            logout_account(logout_session, "refresh-or-logout-token"),
        )
    finally:
        await refresh_session.close()
        await logout_session.close()

    if logout_result is not None:
        raise AssertionError("logout returned a result")
    observer = refresh_database.session_factory()
    try:
        family = await _rows(observer, presented.family_id)
        separate = await _rows(observer, other.family_id)
        _assert_revoked_replacement_chain(family, refresh_result.outcome)
        if presented.family_id == other.family_id:
            raise AssertionError("the two families were not independent")
        if len(separate) != 1 or separate[0].revoked_at is not None:
            raise AssertionError("the other family is no longer active")
        if separate[0].replaced_by_id is not None:
            raise AssertionError("the other family gained a replacement link")
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_failed_rotation_rolls_back_the_link(
    refresh_database: _RefreshDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _user(refresh_database.session)
    original = await _token(refresh_database.session, user, "atomic-refresh-token")
    await refresh_database.session.commit()

    async def fail(
        session: AsyncSession,
        *,
        user_id: object,
        family_id: object,
        issued_at: datetime,
    ) -> tuple[str, RefreshToken]:
        del user_id
        session.add(
            RefreshToken(
                user_id=uuid4(),
                family_id=family_id,  # type: ignore[arg-type]
                token_hash="orphan-token-hash",
                expires_at=issued_at + REFRESH_TOKEN_TTL,
            )
        )
        await session.flush()
        raise AssertionError("the foreign key should have rejected the token")

    monkeypatch.setattr(auth_module, "create_replacement_refresh_token", fail)
    worker = refresh_database.session_factory()
    try:
        with pytest.raises(IntegrityError):
            await rotate_refresh_token(worker, "atomic-refresh-token")
    finally:
        await worker.close()

    observer = refresh_database.session_factory()
    try:
        rows = await _rows(observer, original.family_id)
        assert len(rows) == 1
        assert rows[0].revoked_at is None
        assert rows[0].replaced_by_id is None
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_logout_commits_one_family_and_can_repeat(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    presented = await _token(refresh_database.session, user, "logout-refresh-token")
    await _token(
        refresh_database.session,
        user,
        "logout-sibling-token",
        family_id=presented.family_id,
    )
    other = await _token(refresh_database.session, user, "logout-other-family")
    await refresh_database.session.commit()
    first = refresh_database.session_factory()
    second = refresh_database.session_factory()
    try:
        await logout_account(first, "logout-refresh-token")
        await logout_account(second, "logout-refresh-token")
        await logout_account(second, None)
        await logout_account(second, "missing-refresh-token")
    finally:
        await first.close()
        await second.close()

    observer = refresh_database.session_factory()
    try:
        family = await _rows(observer, presented.family_id)
        assert len(family) == 2
        assert all(row.revoked_at is not None for row in family)
        separate = await _rows(observer, other.family_id)
        assert len(separate) == 1
        assert separate[0].revoked_at is None
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_postgres_logout_of_a_revoked_token_revokes_its_replacement(
    refresh_database: _RefreshDatabase,
) -> None:
    user = await _user(refresh_database.session)
    original = await _token(
        refresh_database.session,
        user,
        "already-revoked-token",
        revoked_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await _token(
        refresh_database.session,
        user,
        "still-active-replacement",
        family_id=original.family_id,
    )
    await refresh_database.session.commit()
    worker = refresh_database.session_factory()
    try:
        await logout_account(worker, "already-revoked-token")
    finally:
        await worker.close()

    observer = refresh_database.session_factory()
    try:
        family = await _rows(observer, original.family_id)
        assert len(family) == 2
        assert all(row.revoked_at is not None for row in family)
    finally:
        await observer.close()
