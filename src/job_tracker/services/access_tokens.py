"""HS256 access tokens."""

from datetime import UTC, datetime
from functools import lru_cache
from uuid import UUID

import jwt
from jwt.exceptions import PyJWTError

from job_tracker.config import Settings

ACCESS_TOKEN_LIFETIME_SECONDS = 900

_ACCESS_TOKEN_TYPE = "access"
_REQUIRED_CLAIMS = frozenset({"sub", "iat", "exp", "type"})


class InvalidAccessTokenError(Exception):
    """An access token cannot be accepted."""

    def __init__(self) -> None:
        super().__init__("Invalid access token")


@lru_cache
def _load_settings() -> Settings:
    settings = Settings()
    if settings.jwt_algorithm != "HS256":
        raise RuntimeError("The access token algorithm must be HS256.")
    return settings


def _utc_timestamp() -> int:
    """Return the current UTC time as a whole-second NumericDate."""
    return int(datetime.now(UTC).timestamp())


def create_access_token(user_id: UUID) -> str:
    """Return an HS256 access token for this user."""
    if not isinstance(user_id, UUID):
        raise TypeError("Access token subject must be a UUID.")
    settings = _load_settings()
    issued_at = _utc_timestamp()
    claims = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + ACCESS_TOKEN_LIFETIME_SECONDS,
        "type": _ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(
        claims,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> UUID:
    """Return the user id carried by a valid access token."""
    if not isinstance(token, str) or token == "":
        raise InvalidAccessTokenError()
    settings = _load_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=["HS256"],
            leeway=0,
            options={
                "require": ["sub", "iat", "exp", "type"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
            },
        )
    except PyJWTError:
        raise InvalidAccessTokenError() from None
    return _subject_from_claims(claims)


def _subject_from_claims(claims: object) -> UUID:
    if not isinstance(claims, dict) or set(claims) != _REQUIRED_CLAIMS:
        raise InvalidAccessTokenError()
    if claims["type"] != _ACCESS_TOKEN_TYPE:
        raise InvalidAccessTokenError()
    issued_at = _numeric_date(claims["iat"])
    expires_at = _numeric_date(claims["exp"])
    if expires_at - issued_at != ACCESS_TOKEN_LIFETIME_SECONDS:
        raise InvalidAccessTokenError()
    return _canonical_uuid(claims["sub"])


def _numeric_date(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidAccessTokenError()
    return value


def _canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise InvalidAccessTokenError()
    try:
        user_id = UUID(value)
    except (TypeError, ValueError):
        raise InvalidAccessTokenError() from None
    if str(user_id) != value:
        raise InvalidAccessTokenError()
    return user_id
