"""Email normalization and user persistence helpers."""

from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.user import User


class _EmailInput(BaseModel):
    """Validate one address with EmailStr after approved normalization."""

    model_config = ConfigDict(hide_input_in_errors=True)

    email: EmailStr


def normalize_email(email: str) -> str:
    """Strip surrounding whitespace and lowercase the whole address.

    Dots and plus tags are preserved. Unicode casefolding is not used.
    """
    if not isinstance(email, str):
        raise TypeError("Email must be a string.")
    return email.strip().lower()


def parse_email(email: str) -> str:
    """Normalize an address and reject values that EmailStr does not accept."""
    normalized = normalize_email(email)
    _EmailInput(email=normalized)
    return normalized


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Return the user stored under the normalized email, if one exists.

    The caller's session owns the transaction. This helper does not commit.
    """
    normalized = normalize_email(email)
    result = await session.execute(select(User).where(User.email == normalized))
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    email: str,
    password_hash: str,
) -> User:
    """Insert a user with a normalized email and the supplied encoded hash.

    The email is normalized before validation and storage. A duplicate email
    is left to the database UNIQUE constraint. This helper flushes and does
    not commit.
    """
    user = User(email=parse_email(email), password_hash=password_hash)
    session.add(user)
    await session.flush()
    return user
