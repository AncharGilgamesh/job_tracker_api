"""Regression tests for required application settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from job_tracker.config import Settings

TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcd"
TEST_ORIGIN = "http://127.0.0.1:3000"


def _settings(
    environment: str,
    database_url: str,
    *,
    jwt_algorithm: str = "HS256",
    jwt_secret_key: str = TEST_JWT_SECRET,
    allowed_origins: str | list[str] | tuple[str, ...] = TEST_ORIGIN,
) -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,
        database_url=database_url,
        jwt_algorithm=jwt_algorithm,
        jwt_secret_key=jwt_secret_key,
        allowed_origins=allowed_origins,
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
        jwt_algorithm=" HS256 ",
        jwt_secret_key=f" {TEST_JWT_SECRET} ",
        allowed_origins=" HTTP://LocalHost:3000 ",
    )

    assert settings.environment == "local"
    assert settings.database_url == "postgresql://example"
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_secret_key == TEST_JWT_SECRET
    assert settings.allowed_origins == ("http://localhost:3000",)
    rendered = repr(settings)
    assert "postgresql://example" not in rendered
    assert TEST_JWT_SECRET not in rendered


def test_environment_variables_override_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ENVIRONMENT=from-file\n"
        "DATABASE_URL=postgresql://from-file\n"
        "JWT_ALGORITHM=HS256\n"
        "JWT_SECRET_KEY=file-secret-value-not-used-0123456789\n"
        "ALLOWED_ORIGINS=http://from-file.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENVIRONMENT", " from-env ")
    monkeypatch.setenv("DATABASE_URL", " postgresql://from-env ")
    monkeypatch.setenv("JWT_ALGORITHM", " HS256 ")
    monkeypatch.setenv("JWT_SECRET_KEY", f" {TEST_JWT_SECRET} ")
    monkeypatch.setenv("ALLOWED_ORIGINS", " HTTP://From-Env.Example:3000 ")

    settings = Settings(_env_file=env_file)

    assert settings.environment == "from-env"
    assert settings.database_url == "postgresql://from-env"
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_secret_key == TEST_JWT_SECRET
    assert settings.allowed_origins == ("http://from-env.example:3000",)
    assert TEST_JWT_SECRET not in repr(settings)
    assert "file-secret-value-not-used-0123456789" not in repr(settings)


def test_hs256_is_accepted() -> None:
    settings = _settings("local", "postgresql://example", jwt_algorithm="HS256")

    assert settings.jwt_algorithm == "HS256"


@pytest.mark.parametrize("algorithm", ["RS256", "hs256", "none", "HS384"])
def test_other_jwt_algorithm_is_rejected(algorithm: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings("local", "postgresql://example", jwt_algorithm=algorithm)

    assert any(error["loc"] == ("jwt_algorithm",) for error in exc_info.value.errors())
    assert algorithm not in str(exc_info.value)


def test_missing_jwt_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="local",
            database_url="postgresql://example",
            jwt_algorithm="HS256",
            allowed_origins=TEST_ORIGIN,
        )

    assert any(error["loc"] == ("jwt_secret_key",) for error in exc_info.value.errors())


@pytest.mark.parametrize("secret", ["", "   ", "short-test-secret", "y" * 31])
def test_blank_or_short_jwt_secret_is_rejected(secret: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings("local", "postgresql://example", jwt_secret_key=secret)

    rendered = str(exc_info.value)
    assert any(error["loc"] == ("jwt_secret_key",) for error in exc_info.value.errors())
    if secret.strip():
        assert secret not in rendered
        assert secret.strip() not in rendered


def test_jwt_secret_length_uses_utf8_bytes() -> None:
    secret = "é" * 16
    assert len(secret) < 32
    assert len(secret.encode("utf-8")) == 32

    settings = _settings("local", "postgresql://example", jwt_secret_key=secret)

    assert settings.jwt_secret_key == secret
    assert secret not in repr(settings)


def test_missing_allowed_origins_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="local",
            database_url="postgresql://example",
            jwt_algorithm="HS256",
            jwt_secret_key=TEST_JWT_SECRET,
        )

    errors = exc_info.value.errors()
    assert any(error["loc"] == ("allowed_origins",) for error in errors)
    assert TEST_JWT_SECRET not in str(exc_info.value)


@pytest.mark.parametrize("origins", ["", "   ", []])
def test_empty_allowed_origins_is_rejected(origins: str | list[str]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings("local", "postgresql://example", allowed_origins=origins)

    errors = exc_info.value.errors()
    assert any(error["loc"] == ("allowed_origins",) for error in errors)


@pytest.mark.parametrize(
    "origins",
    [
        "*",
        "http://*",
        "https://*.example.com",
        "http://127.0.0.1:3000/",
        "http://example.com/app",
        "http://example.com?next=/",
        "http://example.com#section",
        "http://user:super-secret-password@example.com",
        "http://127.0.0.1:3000,",
        "http://127.0.0.1:3000,,http://localhost",
        "ftp://example.com",
    ],
)
def test_wildcard_and_malformed_origins_are_rejected(origins: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings("local", "postgresql://example", allowed_origins=origins)

    rendered = str(exc_info.value)
    errors = exc_info.value.errors()
    assert any(error["loc"] == ("allowed_origins",) for error in errors)
    assert "super-secret-password" not in rendered
    assert TEST_JWT_SECRET not in rendered


def test_localhost_http_origin_is_accepted() -> None:
    settings = _settings(
        "local",
        "postgresql://example",
        allowed_origins=" HTTP://127.0.0.1:3000, HTTPS://Example.COM:8443 ",
    )

    assert settings.allowed_origins == (
        "http://127.0.0.1:3000",
        "https://example.com:8443",
    )
    assert TEST_JWT_SECRET not in repr(settings)
