"""Unit tests for password policy and Argon2id hashing."""

import base64
from collections.abc import Callable

import pytest
from argon2 import PasswordHasher, Type

from job_tracker.services import passwords
from job_tracker.services.passwords import (
    PasswordHashError,
    PasswordPolicyError,
    hash_password,
    password_needs_rehash,
    verify_password,
)

_ACCEPTED = "p" * 15


def _assert_message_hides(password: str, message: str) -> None:
    if password and password in message:
        raise AssertionError("password validation error included the password")


@pytest.mark.asyncio
async def test_unicode_password_uses_character_length() -> None:
    too_short = "😀" * 8
    accepted = "😀" * 40
    assert len(too_short) == 8
    assert len(too_short.encode("utf-8")) > 15
    assert len(accepted) == 40
    assert len(accepted.encode("utf-8")) > 128

    with pytest.raises(PasswordPolicyError) as exc_info:
        await hash_password(too_short)

    _assert_message_hides(too_short, str(exc_info.value))
    encoded = await hash_password(accepted)
    matched = await verify_password(accepted, encoded)
    assert matched is True


@pytest.mark.asyncio
async def test_correct_and_incorrect_passwords() -> None:
    encoded = await hash_password(_ACCEPTED)
    correct = await verify_password(_ACCEPTED, encoded)
    incorrect = await verify_password("q" * 15, encoded)

    assert correct is True
    assert incorrect is False


@pytest.mark.asyncio
async def test_same_password_produces_different_argon2id_hashes() -> None:
    first = await hash_password(_ACCEPTED)
    second = await hash_password(_ACCEPTED)
    first_parameters = _hash_parameters(first)
    second_parameters = _hash_parameters(second)

    assert first != second
    assert first_parameters == ("argon2id", "m=19456,t=2,p=1", 16, 32)
    assert second_parameters == first_parameters
    assert password_needs_rehash(first) is False
    assert password_needs_rehash(second) is False


def test_outdated_hash_needs_rehash() -> None:
    outdated = PasswordHasher(
        time_cost=1,
        memory_cost=19456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    ).hash(_ACCEPTED)

    assert password_needs_rehash(outdated) is True


@pytest.mark.asyncio
async def test_hash_and_verify_use_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    async def spy(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        seen.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(passwords.asyncio, "to_thread", spy)

    encoded = await hash_password(_ACCEPTED)
    matched = await verify_password(_ACCEPTED, encoded)

    assert matched is True
    assert seen == [passwords._hasher.hash, passwords._verify_password]


@pytest.mark.asyncio
async def test_creation_validates_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Hasher:
        def hash(self, password: str) -> str:
            raise AssertionError("Argon2 was called for an invalid password")

    monkeypatch.setattr(passwords, "_hasher", _Hasher())

    with pytest.raises(PasswordPolicyError):
        await hash_password("p" * 14)


@pytest.mark.asyncio
async def test_malformed_hash_error_hides_the_hash() -> None:
    malformed = "not-a-valid-password-hash"

    with pytest.raises(PasswordHashError) as exc_info:
        await verify_password(_ACCEPTED, malformed)

    message = str(exc_info.value)
    assert malformed not in message
    assert exc_info.value.__cause__ is None
    assert _ACCEPTED not in message


def _hash_parameters(encoded: str) -> tuple[str, str, int, int]:
    parts = encoded.split("$")
    salt = _decode_b64(parts[4])
    digest = _decode_b64(parts[5])
    return parts[1], parts[3], len(salt), len(digest)


def _decode_b64(segment: str) -> bytes:
    padded = segment + ("=" * (-len(segment) % 4))
    return base64.b64decode(padded)
