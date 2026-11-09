"""Argon2id password policy, hashing, and verification."""

import asyncio

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_MIN_PASSWORD_LENGTH = 15
_MAX_PASSWORD_LENGTH = 128

_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


class PasswordPolicyError(ValueError):
    """A password cannot be created because it breaks the length policy."""


class PasswordHashError(RuntimeError):
    """A stored password hash could not be processed."""


def validate_password(password: str) -> None:
    """Reject passwords outside 15 to 128 Python characters.

    The password is not trimmed, lowercased, or otherwise normalized.
    The exception message does not include the submitted password.
    """
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be a string.")
    length = len(password)
    if length < _MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError("Password must be at least 15 characters.")
    if length > _MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError("Password must be at most 128 characters.")


async def hash_password(password: str) -> str:
    """Validate the password, then return its encoded Argon2id hash.

    Length is checked before Argon2 runs. Hashing runs off the event loop.
    """
    validate_password(password)
    return await asyncio.to_thread(_hasher.hash, password)


def _verify_password(encoded_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(encoded_hash, password) is True
    except VerifyMismatchError:
        return False


async def verify_password(password: str, encoded_hash: str) -> bool:
    """Return whether the password matches the encoded hash.

    A mismatch is False. A malformed hash raises PasswordHashError and the
    error does not include the hash or the library exception text.
    """
    if not isinstance(password, str) or not isinstance(encoded_hash, str):
        raise PasswordHashError("Password verification could not be completed.")
    try:
        return await asyncio.to_thread(_verify_password, encoded_hash, password)
    except (InvalidHashError, VerificationError):
        raise PasswordHashError(
            "Password verification could not be completed."
        ) from None


def password_needs_rehash(encoded_hash: str) -> bool:
    """Return whether the encoded hash uses outdated hasher parameters.

    This does not write a replacement hash.
    """
    try:
        return _hasher.check_needs_rehash(encoded_hash)
    except (InvalidHashError, VerificationError):
        raise PasswordHashError("Password hash could not be inspected.") from None
