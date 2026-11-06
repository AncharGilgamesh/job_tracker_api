"""Alembic migration environment.

The migration target is ``ALEMBIC_DATABASE_URL`` only. This module does not
read ``DATABASE_URL`` or ``TEST_DATABASE_URL``.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

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

_IMPORTED_MODELS = (
    Application,
    ApplicationStatusHistory,
    Company,
    Interview,
    RefreshToken,
    User,
    Vacancy,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_registered_tables = {model.__tablename__ for model in _IMPORTED_MODELS}
if _registered_tables != set(Base.metadata.tables):
    raise RuntimeError(
        "Application models were not registered before migration metadata was captured."
    )

target_metadata = Base.metadata


def _alembic_database_url() -> str:
    raw_url = os.environ.get("ALEMBIC_DATABASE_URL")
    if raw_url is None or raw_url.strip() == "":
        raise RuntimeError(
            "ALEMBIC_DATABASE_URL is required and must be a postgresql+asyncpg "
            "SQLAlchemy URL. Alembic does not read DATABASE_URL or TEST_DATABASE_URL."
        )
    candidate = raw_url.strip()
    try:
        parsed = make_url(candidate)
    except Exception:
        raise RuntimeError(
            "ALEMBIC_DATABASE_URL must be a valid postgresql+asyncpg SQLAlchemy URL."
        ) from None
    if parsed.drivername != "postgresql+asyncpg":
        raise RuntimeError(
            "ALEMBIC_DATABASE_URL must use the postgresql+asyncpg driver."
        )
    return candidate


def run_migrations_offline() -> None:
    """Emit SQL without opening a database connection."""
    context.configure(
        url=_alembic_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    connectable = create_async_engine(
        _alembic_database_url(),
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations through an async engine and ``run_sync``."""
    asyncio.run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
