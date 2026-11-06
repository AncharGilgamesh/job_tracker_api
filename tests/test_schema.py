"""PostgreSQL schema and persistence infrastructure tests."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, UniqueConstraint, text
from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from job_tracker.db.base import Base
from job_tracker.db.models import (
    Application,
    ApplicationStatusHistory,
    Company,
    Interview,
    RefreshToken,
    User,
    Vacancy,
)
from job_tracker.db.session import create_db_engine, create_session_factory

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_NAME = "job_tracker_test"
REQUIRED_DRIVER = "postgresql+asyncpg"
REQUIRED_HOST = "127.0.0.1"
REQUIRED_PORT = 5433
REVISION = "c4a8e1b27d05"
APPLICATION_TABLES = (
    "users",
    "companies",
    "vacancies",
    "applications",
    "application_status_history",
    "interviews",
    "refresh_tokens",
)
DELETE_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}
APPROVED_DATA_TYPES = {"uuid", "text", "timestamp with time zone"}
FORBIDDEN_COLUMN_NAMES = {
    "email",
    "password_hash",
    "full_name",
    "country",
    "salary",
    "compensation",
    "amount",
    "currency",
    "wage",
    "pay",
    "interview_type",
}


@dataclass(frozen=True)
class ColumnSpec:
    """Approved column type, nullability, and database default."""

    name: str
    data_type: str
    nullable: bool
    default_snippet: str | None = None


@dataclass(frozen=True)
class ForeignKeySpec:
    """Approved foreign key and its PostgreSQL delete action."""

    table: str
    column: str
    target_table: str
    target_column: str
    ondelete: str


def _pk(name: str = "id") -> ColumnSpec:
    return ColumnSpec(name, "uuid", False, "gen_random_uuid()")


EXPECTED_COLUMNS: dict[str, tuple[ColumnSpec, ...]] = {
    "users": (_pk(),),
    "companies": (
        _pk(),
        ColumnSpec("created_by_user_id", "uuid", False),
        ColumnSpec("name", "text", False),
        ColumnSpec("normalized_name", "text", False),
        ColumnSpec("archived_at", "timestamp with time zone", True),
    ),
    "vacancies": (
        _pk(),
        ColumnSpec("user_id", "uuid", False),
        ColumnSpec("company_id", "uuid", False),
        ColumnSpec("title", "text", False),
        ColumnSpec("description", "text", True),
        ColumnSpec("source_url", "text", True),
        ColumnSpec("location", "text", True),
    ),
    "applications": (
        _pk(),
        ColumnSpec("vacancy_id", "uuid", False),
        ColumnSpec("status", "text", False),
    ),
    "application_status_history": (
        _pk(),
        ColumnSpec("application_id", "uuid", False),
        ColumnSpec("from_status", "text", True),
        ColumnSpec("to_status", "text", False),
        ColumnSpec("changed_at", "timestamp with time zone", False),
    ),
    "interviews": (
        _pk(),
        ColumnSpec("application_id", "uuid", False),
        ColumnSpec("scheduled_at", "timestamp with time zone", False),
        ColumnSpec("outcome", "text", True),
        ColumnSpec("notes", "text", True),
    ),
    "refresh_tokens": (
        _pk(),
        ColumnSpec("user_id", "uuid", False),
        ColumnSpec("family_id", "uuid", False),
        ColumnSpec("token_hash", "text", False),
        ColumnSpec("expires_at", "timestamp with time zone", False),
        ColumnSpec("revoked_at", "timestamp with time zone", True),
        ColumnSpec("replaced_by_id", "uuid", True),
        ColumnSpec("created_at", "timestamp with time zone", False, "now()"),
    ),
}

EXPECTED_FOREIGN_KEYS = (
    ForeignKeySpec("companies", "created_by_user_id", "users", "id", "NO ACTION"),
    ForeignKeySpec("vacancies", "user_id", "users", "id", "NO ACTION"),
    ForeignKeySpec("vacancies", "company_id", "companies", "id", "RESTRICT"),
    ForeignKeySpec("applications", "vacancy_id", "vacancies", "id", "NO ACTION"),
    ForeignKeySpec(
        "application_status_history",
        "application_id",
        "applications",
        "id",
        "NO ACTION",
    ),
    ForeignKeySpec("interviews", "application_id", "applications", "id", "NO ACTION"),
    ForeignKeySpec("refresh_tokens", "user_id", "users", "id", "NO ACTION"),
    ForeignKeySpec(
        "refresh_tokens",
        "replaced_by_id",
        "refresh_tokens",
        "id",
        "NO ACTION",
    ),
)


def require_test_database_url() -> tuple[str, URL]:
    """Validate the test URL before any connection or subprocess is started."""
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if raw_url is None or raw_url.strip() == "":
        raise RuntimeError("TEST_DATABASE_URL is required.")
    candidate = raw_url.strip()
    try:
        parsed = make_url(candidate)
    except Exception:
        raise RuntimeError(
            "TEST_DATABASE_URL must be a valid postgresql+asyncpg URL."
        ) from None
    if parsed.drivername != REQUIRED_DRIVER:
        raise RuntimeError("TEST_DATABASE_URL must use the postgresql+asyncpg driver.")
    if parsed.database != TEST_DATABASE_NAME:
        raise RuntimeError(
            "TEST_DATABASE_URL must target the job_tracker_test database."
        )
    if parsed.host != REQUIRED_HOST:
        raise RuntimeError("TEST_DATABASE_URL must use host 127.0.0.1.")
    if parsed.port != REQUIRED_PORT:
        raise RuntimeError("TEST_DATABASE_URL must use port 5433.")
    return candidate, parsed


def _safe_process_detail(output: str, database_url: str) -> str:
    parsed = make_url(database_url)
    markers = [database_url, parsed.render_as_string(hide_password=False)]
    if parsed.password:
        markers.append(parsed.password)
    if parsed.username and parsed.password:
        markers.append(f"{parsed.username}:{parsed.password}")
    if any(marker and marker in output for marker in markers) or "://" in output:
        return "output omitted because it contained a credential or URL."
    trimmed = output.strip()
    if len(trimmed) > 500:
        trimmed = trimmed[-500:]
    return trimmed or "no output."


async def _run_alembic(database_url: str, *arguments: str) -> None:
    env = os.environ.copy()
    env.pop("TEST_DATABASE_URL", None)
    env["DATABASE_URL"] = (
        "postgresql+asyncpg://ignored:ignored@127.0.0.1:1/job_tracker"
    )
    env["ALEMBIC_DATABASE_URL"] = database_url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *arguments,
        cwd=REPO_ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        output = stdout.decode("utf-8", errors="replace") + stderr.decode(
            "utf-8",
            errors="replace",
        )
        detail = _safe_process_detail(output, database_url)
        command = " ".join(arguments)
        raise RuntimeError(
            f"Alembic {command} failed with exit code {process.returncode}. {detail}"
        )


async def _alembic_output(env: dict[str, str], *arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *arguments,
        cwd=REPO_ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout.decode("utf-8", errors="replace") + stderr.decode(
        "utf-8",
        errors="replace",
    )
    return process.returncode or 0, output


def _assert_output_has_no_url(output: str) -> None:
    if "://" in output or "@127.0.0.1" in output:
        raise AssertionError("Alembic output included a database URL.")


async def _prepare_database(database_url: str, parsed: URL) -> None:
    admin_engine = create_async_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with admin_engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": TEST_DATABASE_NAME},
            )
            if exists is None:
                await connection.execute(text("CREATE DATABASE job_tracker_test"))
    except SQLAlchemyError:
        raise RuntimeError(
            "Could not prepare job_tracker_test. PostgreSQL must be available "
            "on 127.0.0.1:5433."
        ) from None
    finally:
        await admin_engine.dispose()

    engine = create_db_engine(database_url)
    try:
        async with engine.begin() as connection:
            current_database = await connection.scalar(
                text("SELECT current_database()")
            )
            if current_database != TEST_DATABASE_NAME:
                raise RuntimeError(
                    "Refusing to reset a database other than job_tracker_test."
                )
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            remaining = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
            if remaining != 0:
                raise RuntimeError("The test public schema was not empty after reset.")
    except SQLAlchemyError:
        raise RuntimeError(
            "Could not reset job_tracker_test. PostgreSQL must be available "
            "on 127.0.0.1:5433."
        ) from None
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Reset job_tracker_test, migrate it, and yield one uncommitted session."""
    database_url, parsed = require_test_database_url()
    await _prepare_database(database_url, parsed)
    await _run_alembic(database_url, "upgrade", "head")
    engine = create_db_engine(database_url)
    session = create_session_factory(engine)()
    try:
        try:
            current_database = await session.scalar(text("SELECT current_database()"))
        except SQLAlchemyError:
            raise RuntimeError(
                "Could not open a session for job_tracker_test."
            ) from None
        if current_database != TEST_DATABASE_NAME:
            raise RuntimeError(
                "Refusing to test a database other than job_tracker_test."
            )
        yield session
    finally:
        await session.close()
        await engine.dispose()


def _server_default_sql(column_default: object) -> str | None:
    if column_default is None:
        return None
    return str(column_default)


async def _columns(session: AsyncSession, table_name: str) -> list[dict[str, object]]:
    result = await session.execute(
        text(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table_name
            ORDER BY ordinal_position
            """
        ),
        {"table_name": table_name},
    )
    return [dict(row) for row in result.mappings()]


async def _add_user(session: AsyncSession) -> User:
    user = User()
    session.add(user)
    await session.flush()
    return user


async def _add_company(
    session: AsyncSession,
    user: User,
    *,
    name: str = "Acme",
    normalized_name: str = "acme",
) -> Company:
    company = Company(
        created_by_user_id=user.id,
        name=name,
        normalized_name=normalized_name,
    )
    session.add(company)
    await session.flush()
    return company


async def _add_vacancy(
    session: AsyncSession,
    user: User,
    company: Company,
    *,
    title: str = "Engineer",
) -> Vacancy:
    vacancy = Vacancy(
        user_id=user.id,
        company_id=company.id,
        title=title,
    )
    session.add(vacancy)
    await session.flush()
    return vacancy


async def _add_application(
    session: AsyncSession,
    vacancy: Vacancy,
    *,
    status: str = "APPLIED",
) -> Application:
    application = Application(vacancy_id=vacancy.id, status=status)
    session.add(application)
    await session.flush()
    return application


def _refresh_token(
    user: User,
    *,
    token_hash: str,
    replaced_by_id: UUID | None = None,
) -> RefreshToken:
    return RefreshToken(
        user_id=user.id,
        family_id=uuid4(),
        token_hash=token_hash,
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
        replaced_by_id=replaced_by_id,
    )


def test_orm_metadata_matches_approved_schema() -> None:
    assert set(Base.metadata.tables) == set(APPLICATION_TABLES)
    for mapper in Base.registry.mappers:
        assert list(mapper.relationships) == []

    for table_name, specs in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        assert [column.name for column in table.columns] == [
            spec.name for spec in specs
        ]
        assert list(table.indexes) == []
        for spec, column in zip(specs, table.columns, strict=True):
            assert column.nullable == spec.nullable
            assert column.default is None
            assert column.onupdate is None
            rendered_default = _server_default_sql(
                None if column.server_default is None else column.server_default.arg
            )
            if spec.default_snippet is None:
                assert column.server_default is None
            else:
                assert rendered_default is not None
                assert spec.default_snippet in rendered_default

    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            assert not isinstance(constraint, CheckConstraint)

    unique_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        for column in constraint.columns
    }
    assert unique_columns == {("applications", "vacancy_id")}

    foreign_keys = {
        (
            column.table.name,
            column.name,
            foreign_key.column.table.name,
            foreign_key.column.name,
            foreign_key.ondelete,
        )
        for table in Base.metadata.tables.values()
        for column in table.columns
        for foreign_key in column.foreign_keys
    }
    assert foreign_keys == {
        (
            spec.table,
            spec.column,
            spec.target_table,
            spec.target_column,
            None if spec.ondelete == "NO ACTION" else spec.ondelete,
        )
        for spec in EXPECTED_FOREIGN_KEYS
    }

    changed_at = Base.metadata.tables["application_status_history"].c.changed_at
    expires_at = Base.metadata.tables["refresh_tokens"].c.expires_at
    assert changed_at.nullable is False
    assert changed_at.default is None
    assert changed_at.server_default is None
    assert expires_at.default is None
    assert expires_at.server_default is None


@pytest.mark.asyncio
async def test_alembic_requires_its_own_database_url() -> None:
    env = os.environ.copy()
    env.pop("ALEMBIC_DATABASE_URL", None)
    env.pop("TEST_DATABASE_URL", None)
    env["DATABASE_URL"] = (
        "postgresql+asyncpg://ignored:ignored@127.0.0.1:1/job_tracker"
    )
    return_code, output = await _alembic_output(env, "upgrade", "head")
    _assert_output_has_no_url(output)
    assert return_code != 0
    assert "ALEMBIC_DATABASE_URL is required" in output


@pytest.mark.asyncio
async def test_alembic_rejects_a_non_asyncpg_url() -> None:
    env = os.environ.copy()
    env.pop("TEST_DATABASE_URL", None)
    env["DATABASE_URL"] = (
        "postgresql+asyncpg://ignored:ignored@127.0.0.1:1/job_tracker"
    )
    env["ALEMBIC_DATABASE_URL"] = (
        "postgresql://rejected:rejected@127.0.0.1:1/not_the_test_database"
    )
    return_code, output = await _alembic_output(env, "upgrade", "head")
    _assert_output_has_no_url(output)
    assert return_code != 0
    assert "postgresql+asyncpg driver" in output


@pytest.mark.asyncio
async def test_upgrade_from_empty_schema_creates_eight_tables(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """
        )
    )
    table_names = set(result.scalars())
    assert table_names == set(APPLICATION_TABLES) | {"alembic_version"}
    assert len(table_names) == 8
    revision = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == REVISION
    current_database = await db_session.scalar(text("SELECT current_database()"))
    assert current_database == TEST_DATABASE_NAME


@pytest.mark.asyncio
async def test_catalog_matches_approved_columns_and_defaults(
    db_session: AsyncSession,
) -> None:
    for table_name, specs in EXPECTED_COLUMNS.items():
        rows = await _columns(db_session, table_name)
        assert [row["column_name"] for row in rows] == [spec.name for spec in specs]
        for spec, row in zip(specs, rows, strict=True):
            assert row["data_type"] in APPROVED_DATA_TYPES
            assert row["data_type"] == spec.data_type
            assert (row["is_nullable"] == "YES") is spec.nullable
            default = row["column_default"]
            if spec.default_snippet is None:
                assert default is None
            else:
                assert default is not None
                assert spec.default_snippet in str(default)


@pytest.mark.asyncio
async def test_schema_has_no_enums_triggers_or_check_constraints(
    db_session: AsyncSession,
) -> None:
    enum_names = (
        await db_session.execute(
            text(
                """
                SELECT t.typname
                FROM pg_type AS t
                JOIN pg_namespace AS n ON n.oid = t.typnamespace
                WHERE t.typtype = 'e' AND n.nspname = 'public'
                """
            )
        )
    ).scalars().all()
    assert list(enum_names) == []

    triggers = (
        await db_session.execute(
            text(
                """
                SELECT trigger_name
                FROM information_schema.triggers
                WHERE trigger_schema = 'public'
                """
            )
        )
    ).scalars().all()
    assert list(triggers) == []

    checks = (
        await db_session.execute(
            text(
                """
                SELECT con.conname
                FROM pg_constraint AS con
                JOIN pg_class AS rel ON rel.oid = con.conrelid
                JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
                WHERE con.contype = 'c' AND nsp.nspname = 'public'
                """
            )
        )
    ).scalars().all()
    assert list(checks) == []


@pytest.mark.asyncio
async def test_deferred_and_monetary_columns_are_absent(
    db_session: AsyncSession,
) -> None:
    for table_name in APPLICATION_TABLES:
        rows = await _columns(db_session, table_name)
        column_names = {row["column_name"] for row in rows}
        assert column_names.isdisjoint(FORBIDDEN_COLUMN_NAMES)
    user_columns = {
        row["column_name"] for row in await _columns(db_session, "users")
    }
    company_columns = {
        row["column_name"] for row in await _columns(db_session, "companies")
    }
    interview_columns = {
        row["column_name"] for row in await _columns(db_session, "interviews")
    }
    assert {"email", "password_hash", "full_name"}.isdisjoint(user_columns)
    assert "country" not in company_columns
    assert {"status", "interview_type"}.isdisjoint(interview_columns)
    assert "user_id" not in {
        row["column_name"] for row in await _columns(db_session, "applications")
    }
    assert "user_id" not in {
        row["column_name"]
        for row in await _columns(db_session, "application_status_history")
    }
    assert "user_id" not in interview_columns


@pytest.mark.asyncio
async def test_foreign_keys_use_only_the_approved_delete_actions(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT
                src_rel.relname AS table_name,
                src_att.attname AS column_name,
                tgt_rel.relname AS target_table,
                tgt_att.attname AS target_column,
                con.confdeltype AS delete_action
            FROM pg_constraint AS con
            JOIN pg_class AS src_rel ON src_rel.oid = con.conrelid
            JOIN pg_namespace AS src_ns ON src_ns.oid = src_rel.relnamespace
            JOIN pg_class AS tgt_rel ON tgt_rel.oid = con.confrelid
            JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS src_cols(attnum, ord)
                ON true
            JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS tgt_cols(attnum, ord)
                ON src_cols.ord = tgt_cols.ord
            JOIN pg_attribute AS src_att
                ON src_att.attrelid = src_rel.oid
               AND src_att.attnum = src_cols.attnum
            JOIN pg_attribute AS tgt_att
                ON tgt_att.attrelid = tgt_rel.oid
               AND tgt_att.attnum = tgt_cols.attnum
            WHERE con.contype = 'f' AND src_ns.nspname = 'public'
            """
        )
    )
    found = set()
    for row in result.mappings():
        delete_action = row["delete_action"]
        if isinstance(delete_action, bytes):
            delete_action = delete_action.decode("ascii")
        found.add(
            (
                row["table_name"],
                row["column_name"],
                row["target_table"],
                row["target_column"],
                DELETE_ACTIONS[delete_action],
            )
        )
    expected = {
        (spec.table, spec.column, spec.target_table, spec.target_column, spec.ondelete)
        for spec in EXPECTED_FOREIGN_KEYS
    }
    assert found == expected


@pytest.mark.asyncio
async def test_only_primary_keys_and_vacancy_id_are_indexed(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT
                rel.relname AS table_name,
                idx.relname AS index_name,
                ix.indisprimary AS is_primary,
                ix.indisunique AS is_unique,
                att.attname AS column_name
            FROM pg_index AS ix
            JOIN pg_class AS rel ON rel.oid = ix.indrelid
            JOIN pg_class AS idx ON idx.oid = ix.indexrelid
            JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
            JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS keys(attnum, ord)
                ON true
            JOIN pg_attribute AS att
                ON att.attrelid = rel.oid
               AND att.attnum = keys.attnum
            WHERE nsp.nspname = 'public'
            ORDER BY rel.relname, idx.relname, keys.ord
            """
        )
    )
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for row in result.mappings():
        key = (row["table_name"], row["index_name"])
        entry = grouped.setdefault(
            key,
            {
                "table_name": row["table_name"],
                "columns": [],
                "is_primary": row["is_primary"],
                "is_unique": row["is_unique"],
            },
        )
        columns = entry["columns"]
        assert isinstance(columns, list)
        columns.append(row["column_name"])
    indexes = {
        (
            entry["table_name"],
            tuple(entry["columns"]),
            entry["is_primary"],
            entry["is_unique"],
        )
        for entry in grouped.values()
    }
    expected = {
        (table, ("id",), True, True) for table in APPLICATION_TABLES
    } | {
        ("applications", ("vacancy_id",), False, True),
        ("alembic_version", ("version_num",), True, True),
    }
    assert indexes == expected

    unique_constraints = (
        await db_session.execute(
            text(
                """
                SELECT rel.relname AS table_name, att.attname AS column_name
                FROM pg_constraint AS con
                JOIN pg_class AS rel ON rel.oid = con.conrelid
                JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
                JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS cols(attnum, ord)
                    ON true
                JOIN pg_attribute AS att
                    ON att.attrelid = rel.oid
                   AND att.attnum = cols.attnum
                WHERE con.contype = 'u' AND nsp.nspname = 'public'
                """
            )
        )
    ).mappings().all()
    assert {(row["table_name"], row["column_name"]) for row in unique_constraints} == {
        ("applications", "vacancy_id")
    }


@pytest.mark.asyncio
async def test_application_status_has_no_database_allow_list(
    db_session: AsyncSession,
) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    vacancy = await _add_vacancy(db_session, user, company)
    await _add_application(db_session, vacancy, status="not-an-approved-status")


@pytest.mark.asyncio
async def test_applications_vacancy_id_is_not_null(db_session: AsyncSession) -> None:
    db_session.add(Application(status="APPLIED"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_applications_vacancy_id_is_unique(db_session: AsyncSession) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    vacancy = await _add_vacancy(db_session, user, company)
    await _add_application(db_session, vacancy)
    db_session.add(Application(vacancy_id=vacancy.id, status="SCREENING"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_vacancy_title_is_not_null(db_session: AsyncSession) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    db_session.add(Vacancy(user_id=user.id, company_id=company.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_changed_at_is_not_null_and_has_no_default(
    db_session: AsyncSession,
) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    vacancy = await _add_vacancy(db_session, user, company)
    application = await _add_application(db_session, vacancy)
    db_session.add(
        ApplicationStatusHistory(
            application_id=application.id,
            from_status=None,
            to_status="APPLIED",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    vacancy = await _add_vacancy(db_session, user, company)
    application = await _add_application(db_session, vacancy)
    changed_at = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    history = ApplicationStatusHistory(
        application_id=application.id,
        from_status=None,
        to_status="APPLIED",
        changed_at=changed_at,
    )
    db_session.add(history)
    await db_session.flush()
    assert history.changed_at == changed_at


@pytest.mark.asyncio
async def test_interview_scheduled_at_is_not_null(db_session: AsyncSession) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    vacancy = await _add_vacancy(db_session, user, company)
    application = await _add_application(db_session, vacancy)
    db_session.add(Interview(application_id=application.id, outcome=None, notes=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_referenced_company_delete_is_restricted(
    db_session: AsyncSession,
) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    await _add_vacancy(db_session, user, company)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("DELETE FROM companies WHERE id = :company_id"),
            {"company_id": company.id},
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unreferenced_company_delete_is_permitted(
    db_session: AsyncSession,
) -> None:
    user = await _add_user(db_session)
    company = await _add_company(db_session, user)
    await db_session.execute(
        text("DELETE FROM companies WHERE id = :company_id"),
        {"company_id": company.id},
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_normalized_name_is_not_unique(db_session: AsyncSession) -> None:
    user = await _add_user(db_session)
    await _add_company(db_session, user, name="Acme", normalized_name="acme")
    await _add_company(db_session, user, name="ACME", normalized_name="acme")


@pytest.mark.asyncio
async def test_token_hash_is_not_unique(db_session: AsyncSession) -> None:
    user = await _add_user(db_session)
    db_session.add(_refresh_token(user, token_hash="same-hash"))
    db_session.add(_refresh_token(user, token_hash="same-hash"))
    await db_session.flush()


@pytest.mark.asyncio
async def test_replaced_by_id_is_nullable_self_reference_and_not_unique(
    db_session: AsyncSession,
) -> None:
    user = await _add_user(db_session)
    replacement = _refresh_token(user, token_hash="replacement")
    db_session.add(replacement)
    await db_session.flush()
    assert isinstance(replacement.id, UUID)

    first = _refresh_token(
        user,
        token_hash="first",
        replaced_by_id=replacement.id,
    )
    second = _refresh_token(
        user,
        token_hash="second",
        replaced_by_id=replacement.id,
    )
    absent = _refresh_token(user, token_hash="active")
    db_session.add_all([first, second, absent])
    await db_session.flush()
    assert first.replaced_by_id == replacement.id
    assert second.replaced_by_id == replacement.id
    assert absent.replaced_by_id is None


@pytest.mark.asyncio
async def test_expires_at_has_no_default(db_session: AsyncSession) -> None:
    user = await _add_user(db_session)
    db_session.add(
        RefreshToken(
            user_id=user.id,
            family_id=uuid4(),
            token_hash="hash",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_database_generates_uuid_and_created_at(
    db_session: AsyncSession,
) -> None:
    user = await _add_user(db_session)
    token = _refresh_token(user, token_hash="generated")
    db_session.add(token)
    await db_session.flush()
    assert isinstance(user.id, UUID)
    assert isinstance(token.id, UUID)
    assert token.created_at is not None
    assert token.created_at.tzinfo is not None
    assert db_session.sync_session.expire_on_commit is False


@pytest.mark.asyncio
async def test_downgrade_drops_application_tables(db_session: AsyncSession) -> None:
    database_url, _parsed = require_test_database_url()
    engine = db_session.bind
    await db_session.close()
    assert engine is not None
    await engine.dispose()
    await _run_alembic(database_url, "downgrade", "base")

    check_engine = create_db_engine(database_url)
    try:
        async with check_engine.connect() as connection:
            current_database = await connection.scalar(
                text("SELECT current_database()")
            )
            assert current_database == TEST_DATABASE_NAME
            result = await connection.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                    """
                )
            )
            table_names = set(result.scalars())
    finally:
        await check_engine.dispose()

    assert table_names.isdisjoint(APPLICATION_TABLES)
    assert table_names == {"alembic_version"}
