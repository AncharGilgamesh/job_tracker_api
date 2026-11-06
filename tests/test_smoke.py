"""Smoke test for application startup and liveness."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI


class _Request:
    """Minimal request shape used to resolve the session dependency."""

    def __init__(self, application: FastAPI) -> None:
        self.app = application


def test_health(monkeypatch: pytest.MonkeyPatch) -> None:
    test_database_url = (
        "postgresql+asyncpg://job_tracker:test@127.0.0.1:1/job_tracker"
    )
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    from job_tracker.api.deps import get_session, get_settings
    from job_tracker.main import create_app

    get_settings.cache_clear()
    application = create_app()
    settings = get_settings()

    assert settings.environment == "test"
    assert settings.database_url == test_database_url

    async def exercise_lifespan() -> httpx.Response:
        async with application.router.lifespan_context(application):
            engine = application.state.engine
            assert engine.pool.checkedout() == 0

            generator = get_session(_Request(application))
            session = await anext(generator)
            assert session.in_transaction() is False
            await generator.aclose()
            assert engine.pool.checkedout() == 0

            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.get("/health")

            assert engine.pool.checkedout() == 0
            return response

    response = asyncio.run(exercise_lifespan())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
