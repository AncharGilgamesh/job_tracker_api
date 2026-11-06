"""Regression tests for required application settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from job_tracker.config import Settings


def _settings(environment: str, database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,
        database_url=database_url,
    )


@pytest.mark.parametrize(
    ("field_name", "blank"),
    [
        ("environment", ""),
        ("environment", "   "),
        ("database_url", ""),
        ("database_url", "   "),
    ],
)
def test_blank_setting_is_rejected(field_name: str, blank: str) -> None:
    values = {
        "environment": "local",
        "database_url": "postgresql://example",
    }
    values[field_name] = blank

    with pytest.raises(ValidationError) as exc_info:
        _settings(**values)

    rejected_fields = {error["loc"] for error in exc_info.value.errors()}
    assert (field_name,) in rejected_fields


def test_settings_strip_surrounding_whitespace() -> None:
    settings = _settings(
        environment=" local ",
        database_url=" postgresql://example ",
    )

    assert settings.environment == "local"
    assert settings.database_url == "postgresql://example"
    assert "postgresql://example" not in repr(settings)


def test_environment_variables_override_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ENVIRONMENT=from-file\nDATABASE_URL=postgresql://from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENVIRONMENT", " from-env ")
    monkeypatch.setenv("DATABASE_URL", " postgresql://from-env ")

    settings = Settings(_env_file=env_file)

    assert settings.environment == "from-env"
    assert settings.database_url == "postgresql://from-env"
