"""Environment-backed application settings."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_REQUIRED_JWT_ALGORITHM = "HS256"
_MIN_JWT_SECRET_BYTES = 32
_INVALID_ORIGIN = "ALLOWED_ORIGINS contains an invalid origin."


def _normalize_origin(candidate: str) -> str:
    """Return one explicit HTTP or HTTPS origin, or reject it."""
    if "*" in candidate or any(character in candidate for character in "?#\\"):
        raise ValueError(_INVALID_ORIGIN)
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise ValueError(_INVALID_ORIGIN) from None
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(_INVALID_ORIGIN)
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError(_INVALID_ORIGIN)
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError(_INVALID_ORIGIN)
    hostname = parsed.hostname
    if hostname is None or hostname == "":
        raise ValueError(_INVALID_ORIGIN)
    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError(_INVALID_ORIGIN) from None
    if ascii_host == "" or "*" in ascii_host:
        raise ValueError(_INVALID_ORIGIN)
    host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def _parse_allowed_origins(value: object) -> tuple[str, ...]:
    """Parse a comma-separated origin list into normalized origins."""
    if isinstance(value, str):
        if value.strip() == "":
            raise ValueError("ALLOWED_ORIGINS is required.")
        entries: list[object] = value.split(",")
    elif isinstance(value, list | tuple):
        if len(value) == 0:
            raise ValueError("ALLOWED_ORIGINS is required.")
        entries = list(value)
    else:
        raise ValueError("ALLOWED_ORIGINS is required.")

    origins: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or entry.strip() == "":
            raise ValueError("ALLOWED_ORIGINS must not contain empty entries.")
        origins.append(_normalize_origin(entry.strip()))
    return tuple(origins)


class Settings(BaseSettings):
    """Required process settings loaded from the environment and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: NonBlankStr
    database_url: Annotated[NonBlankStr, Field(repr=False)]
    jwt_algorithm: str
    jwt_secret_key: Annotated[str, Field(repr=False)]
    allowed_origins: Annotated[tuple[str, ...], NoDecode]

    @field_validator("jwt_algorithm", mode="before")
    @classmethod
    def _validate_jwt_algorithm(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("JWT_ALGORITHM must be HS256.")
        algorithm = value.strip()
        if algorithm != _REQUIRED_JWT_ALGORITHM:
            raise ValueError("JWT_ALGORITHM must be HS256.")
        return algorithm

    @field_validator("jwt_secret_key", mode="before")
    @classmethod
    def _validate_jwt_secret_key(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("JWT_SECRET_KEY is required.")
        secret = value.strip()
        if secret == "":
            raise ValueError("JWT_SECRET_KEY is required.")
        if len(secret.encode("utf-8")) < _MIN_JWT_SECRET_BYTES:
            raise ValueError("JWT_SECRET_KEY must contain at least 32 bytes.")
        return secret

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _validate_allowed_origins(cls, value: object) -> tuple[str, ...]:
        return _parse_allowed_origins(value)
