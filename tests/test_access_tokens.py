"""Access JWT creation and verification."""

import base64
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timezone
from uuid import UUID, uuid4

import jwt
import jwt.api_jwt as api_jwt
import pytest
from pydantic import ValidationError

from job_tracker.services import access_tokens
from job_tracker.services.access_tokens import (
    ACCESS_TOKEN_LIFETIME_SECONDS,
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
)

TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcd"
USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")


@pytest.fixture(autouse=True)
def jwt_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point token signing at the shared test settings."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://job_tracker:test@127.0.0.1:1/job_tracker",
    )
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_JWT_SECRET)
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://127.0.0.1:3000")
    access_tokens._load_settings.cache_clear()
    yield
    access_tokens._load_settings.cache_clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Freeze token issuance and PyJWT's validation clock on one second."""
    state = {"now": 1_700_000_000}

    def timestamp() -> int:
        return state["now"]

    class FrozenDateTime:
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            moment = datetime.fromtimestamp(state["now"], tz=UTC)
            if tz is None:
                return moment.replace(tzinfo=None)
            return moment

    monkeypatch.setattr(access_tokens, "_utc_timestamp", timestamp)
    monkeypatch.setattr(api_jwt, "datetime", FrozenDateTime)
    return state


def _claims(issued_at: int, **updates: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "sub": str(USER_ID),
        "iat": issued_at,
        "exp": issued_at + ACCESS_TOKEN_LIFETIME_SECONDS,
        "type": "access",
    }
    claims.update(updates)
    return claims


def _sign(
    claims: dict[str, object],
    *,
    secret: str = TEST_JWT_SECRET,
    algorithm: str = "HS256",
) -> str:
    token = jwt.encode(claims, secret, algorithm=algorithm)
    if not isinstance(token, str):
        raise AssertionError("PyJWT did not return a string token.")
    return token


def _unsigned(claims: dict[str, object]) -> str:
    def segment(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = segment({"alg": "none", "typ": "JWT"})
    payload = segment(claims)
    return f"{header}.{payload}."


def _assert_rejected(token: str) -> None:
    with pytest.raises(InvalidAccessTokenError) as exc_info:
        decode_access_token(token)

    error = exc_info.value
    message = str(error)
    assert message == "Invalid access token"
    assert error.__cause__ is None
    if token:
        assert token not in message
    assert TEST_JWT_SECRET not in message
    assert TEST_JWT_SECRET not in repr(error)
    assert "Signature" not in message
    assert "Expired" not in message
    assert "PyJWT" not in message


def test_lifetime_is_900_seconds() -> None:
    assert ACCESS_TOKEN_LIFETIME_SECONDS == 900


def test_create_and_decode_round_trip(clock: dict[str, int]) -> None:
    token = create_access_token(USER_ID)
    other = uuid4()
    other_token = create_access_token(other)

    assert TEST_JWT_SECRET not in token
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    payload = jwt.decode(
        token,
        TEST_JWT_SECRET,
        algorithms=["HS256"],
        leeway=0,
        options={"require": ["sub", "iat", "exp", "type"]},
    )
    assert payload == {
        "sub": "123e4567-e89b-12d3-a456-426614174000",
        "iat": clock["now"],
        "exp": clock["now"] + 900,
        "type": "access",
    }
    assert list(payload) == ["sub", "iat", "exp", "type"]
    assert decode_access_token(token) == USER_ID
    assert isinstance(decode_access_token(token), UUID)
    assert decode_access_token(other_token) == other


def test_token_remains_valid_through_the_final_second(clock: dict[str, int]) -> None:
    token = create_access_token(USER_ID)

    clock["now"] += ACCESS_TOKEN_LIFETIME_SECONDS - 1

    assert decode_access_token(token) == USER_ID


def test_token_expires_at_exact_exp_with_zero_leeway(clock: dict[str, int]) -> None:
    token = create_access_token(USER_ID)
    clock["now"] += ACCESS_TOKEN_LIFETIME_SECONDS

    _assert_rejected(token)


def test_tokens_are_not_logged(
    clock: dict[str, int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    del clock
    with caplog.at_level(logging.DEBUG):
        token = create_access_token(USER_ID)
        assert decode_access_token(token) == USER_ID

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert token not in rendered
    assert TEST_JWT_SECRET not in rendered


def test_subject_must_be_a_uuid() -> None:
    with pytest.raises(TypeError) as exc_info:
        create_access_token("123e4567-e89b-12d3-a456-426614174000")  # type: ignore[arg-type]

    assert "123e4567" not in str(exc_info.value)


@pytest.mark.parametrize("claim", ["sub", "iat", "exp", "type"])
def test_missing_required_claim(clock: dict[str, int], claim: str) -> None:
    claims = _claims(clock["now"])
    del claims[claim]

    _assert_rejected(_sign(claims))


@pytest.mark.parametrize("token_type", ["refresh", "bearer", "ACCESS", ""])
def test_wrong_token_type(clock: dict[str, int], token_type: str) -> None:
    _assert_rejected(_sign(_claims(clock["now"], type=token_type)))


def test_invalid_signature(clock: dict[str, int]) -> None:
    del clock
    token = create_access_token(USER_ID)
    replacement = "A" if token[-1] != "A" else "B"

    _assert_rejected(token[:-1] + replacement)


def test_wrong_signing_key(clock: dict[str, int]) -> None:
    token = _sign(_claims(clock["now"]), secret="y" * 48)

    _assert_rejected(token)


def test_header_algorithm_is_not_trusted(clock: dict[str, int]) -> None:
    claims = _claims(clock["now"])

    _assert_rejected(_unsigned(claims))
    _assert_rejected(_sign(claims, secret="z" * 48, algorithm="HS384"))


def test_future_iat_is_rejected(clock: dict[str, int]) -> None:
    issued_at = clock["now"] + 1

    _assert_rejected(_sign(_claims(issued_at)))


def test_expired_token_is_rejected(clock: dict[str, int]) -> None:
    issued_at = clock["now"] - ACCESS_TOKEN_LIFETIME_SECONDS

    _assert_rejected(_sign(_claims(issued_at)))


@pytest.mark.parametrize("lifetime", [899, 901, 1_800])
def test_incorrect_lifetime_is_rejected(clock: dict[str, int], lifetime: int) -> None:
    issued_at = clock["now"]
    claims = _claims(issued_at, exp=issued_at + lifetime)

    _assert_rejected(_sign(claims))


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iat", 1_700_000_000.9),
        ("exp", 1_700_000_900.5),
        ("iat", True),
        ("exp", True),
        ("iat", "1700000000"),
        ("exp", "1700000900"),
    ],
)
def test_non_integer_and_boolean_timestamps(
    clock: dict[str, int],
    claim: str,
    value: object,
) -> None:
    _assert_rejected(_sign(_claims(clock["now"], **{claim: value})))


@pytest.mark.parametrize(
    "subject",
    [
        "not-a-uuid",
        "123e4567e89b12d3a456426614174000",
        "123E4567-E89B-12D3-A456-426614174000",
        "{123e4567-e89b-12d3-a456-426614174000}",
        "123e4567-e89b-12d3-a456-426614174000 ",
        "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
        "",
        None,
        123,
    ],
)
def test_malformed_uuid_subject(
    clock: dict[str, int],
    subject: object,
) -> None:
    _assert_rejected(_sign(_claims(clock["now"], sub=subject)))


@pytest.mark.parametrize("token", ["not-a-token", "a.b", "a.b.c", "   "])
def test_malformed_token(token: str) -> None:
    _assert_rejected(token)


@pytest.mark.parametrize("token", [None, b"not-a-token"])
def test_non_string_token(token: object) -> None:
    with pytest.raises(InvalidAccessTokenError) as exc_info:
        decode_access_token(token)  # type: ignore[arg-type]

    message = str(exc_info.value)
    assert message == "Invalid access token"
    assert exc_info.value.__cause__ is None
    assert TEST_JWT_SECRET not in message


@pytest.mark.parametrize(
    "extra",
    [
        {"nbf": 1_700_000_000},
        {"jti": "token-id"},
        {"iss": "job-tracker"},
        {"aud": "job-tracker"},
    ],
)
def test_unexpected_claims_are_rejected(
    clock: dict[str, int],
    extra: dict[str, object],
) -> None:
    claims = _claims(clock["now"])
    claims.update(extra)

    _assert_rejected(_sign(claims))


def test_settings_validation_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_ALGORITHM", "RS256")
    access_tokens._load_settings.cache_clear()

    with pytest.raises(ValidationError) as exc_info:
        create_access_token(USER_ID)

    assert TEST_JWT_SECRET not in str(exc_info.value)
