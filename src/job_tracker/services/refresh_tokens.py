"""Initial refresh-token issuance.

Only the SHA-256 digest is stored. The raw token is returned to the caller in
memory and is never logged.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.refresh_token import RefreshToken

REFRESH_TOKEN_TTL = timedelta(days=30)


def hash_refresh_token(raw_token: str) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of a raw refresh token."""
    if not isinstance(raw_token, str) or raw_token == "":
        raise ValueError("Refresh token must be a non-empty string.")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def issue_initial_refresh_token(session: AsyncSession, user_id: UUID) -> str:
    """Insert one new refresh-token family and return the raw token.

    The caller's session owns the transaction. This helper flushes the insert
    and does not commit, roll back, or begin a transaction.
    """
    if not isinstance(user_id, UUID):
        raise TypeError("Refresh token owner must be a UUID.")
    raw_token = secrets.token_urlsafe(32)
    session.add(
        RefreshToken(
            user_id=user_id,
            family_id=uuid4(),
            token_hash=hash_refresh_token(raw_token),
            expires_at=datetime.now(UTC) + REFRESH_TOKEN_TTL,
        )
    )
    await session.flush()
    return raw_token
