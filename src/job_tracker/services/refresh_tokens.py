"""Refresh-token persistence helpers.

Only the SHA-256 digest is stored. The raw token is returned to the caller in
memory and is never logged. These helpers flush on the caller's session and
do not commit, roll back, or begin a transaction.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
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


async def get_refresh_token_by_hash(
    session: AsyncSession,
    token_hash: str,
) -> RefreshToken | None:
    """Return the token row for this hash, refreshed from the database."""
    if not isinstance(token_hash, str) or token_hash == "":
        raise ValueError("Refresh token hash must be a non-empty string.")
    result = await session.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def create_replacement_refresh_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    family_id: UUID,
    issued_at: datetime,
) -> tuple[str, RefreshToken]:
    """Insert one replacement in an existing family and return its raw token.

    The row is flushed so its id is available for ``replaced_by_id``. This
    helper does not revoke the previous token and does not commit.
    """
    if not isinstance(user_id, UUID) or not isinstance(family_id, UUID):
        raise TypeError("Refresh token replacement requires UUID identifiers.")
    if issued_at.tzinfo is None:
        raise ValueError("Refresh token issuance time must be timezone-aware.")
    raw_token = secrets.token_urlsafe(32)
    replacement = RefreshToken(
        user_id=user_id,
        family_id=family_id,
        token_hash=hash_refresh_token(raw_token),
        expires_at=issued_at + REFRESH_TOKEN_TTL,
    )
    session.add(replacement)
    await session.flush()
    return raw_token, replacement


async def revoke_active_family_tokens(
    session: AsyncSession,
    family_id: UUID,
    revoked_at: datetime,
) -> None:
    """Set ``revoked_at`` on every unrevoked token in one family.

    Tokens that are already revoked are left unchanged. Other families are
    not queried. This helper flushes and does not commit.
    """
    if not isinstance(family_id, UUID):
        raise TypeError("Refresh token family must be a UUID.")
    if revoked_at.tzinfo is None:
        raise ValueError("Revocation time must be timezone-aware.")
    result = await session.execute(
        select(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    for token in result.scalars():
        token.revoked_at = revoked_at
    await session.flush()
