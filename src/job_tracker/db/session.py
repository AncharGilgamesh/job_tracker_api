"""Async engine and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_db_engine(database_url: str) -> AsyncEngine:
    """Create an async engine.

    Constructing the engine does not open a database connection.
    """
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory that keeps loaded attributes after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
