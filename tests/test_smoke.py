"""Smoke test for application startup and liveness."""

import asyncio

import httpx
import pytest


def test_health(monkeypatch: pytest.MonkeyPatch) -> None:
    test_database_url = "postgresql://job_tracker:test@localhost:5432/job_tracker"
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    from job_tracker.api.deps import get_settings
    from job_tracker.main import create_app

    get_settings.cache_clear()
    application = create_app()
    settings = get_settings()

    assert settings.environment == "test"
    assert settings.database_url == test_database_url

    async def get_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/health")

    response = asyncio.run(get_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
