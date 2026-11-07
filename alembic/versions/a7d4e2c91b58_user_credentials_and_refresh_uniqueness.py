"""Add user credentials and refresh-token uniqueness.

Revision ID: a7d4e2c91b58
Revises: c4a8e1b27d05
Create Date: 2026-11-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d4e2c91b58"
down_revision: str | None = "c4a8e1b27d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS_NOT_EMPTY = (
    "Cannot add users.email and users.password_hash because users contains rows. "
    "These columns are NOT NULL and have no defaults, and this migration does not "
    "invent email addresses or password hashes. Remove every row from users, then "
    "run the upgrade again."
)
_DUPLICATE_TOKEN_HASH = (
    "Cannot add a UNIQUE constraint on refresh_tokens.token_hash because duplicate "
    "token_hash values exist. Remove the duplicate refresh-token rows, then run "
    "the upgrade again."
)
_DUPLICATE_REPLACED_BY = (
    "Cannot add a UNIQUE constraint on refresh_tokens.replaced_by_id because "
    "duplicate non-null replaced_by_id values exist. Keep each replacement linked "
    "from at most one token, then run the upgrade again."
)


def _require_unique_refresh_tokens() -> None:
    """Reject refresh-token rows that would violate the new UNIQUE constraints."""
    connection = op.get_bind()
    duplicate_token_hash = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM refresh_tokens
                GROUP BY token_hash
                HAVING count(*) > 1
            )
            """
        )
    ).scalar_one()
    if duplicate_token_hash:
        raise RuntimeError(_DUPLICATE_TOKEN_HASH)

    duplicate_replaced_by = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM refresh_tokens
                WHERE replaced_by_id IS NOT NULL
                GROUP BY replaced_by_id
                HAVING count(*) > 1
            )
            """
        )
    ).scalar_one()
    if duplicate_replaced_by:
        raise RuntimeError(_DUPLICATE_REPLACED_BY)


def _require_empty_users_table() -> None:
    """Reject a users table that cannot receive NOT NULL columns without defaults."""
    user_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM users")
    ).scalar_one()
    if user_count != 0:
        raise RuntimeError(_USERS_NOT_EMPTY)


def upgrade() -> None:
    """Add the approved user columns and refresh-token uniqueness."""
    _require_unique_refresh_tokens()
    _require_empty_users_table()
    op.add_column("users", sa.Column("email", sa.Text(), nullable=False))
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=False))
    op.create_unique_constraint("users_email_key", "users", ["email"])
    op.create_unique_constraint(
        "refresh_tokens_token_hash_key",
        "refresh_tokens",
        ["token_hash"],
    )
    op.create_unique_constraint(
        "refresh_tokens_replaced_by_id_key",
        "refresh_tokens",
        ["replaced_by_id"],
    )


def downgrade() -> None:
    """Remove the Iteration 2 user columns and refresh-token uniqueness."""
    op.drop_constraint(
        "refresh_tokens_replaced_by_id_key",
        "refresh_tokens",
        type_="unique",
    )
    op.drop_constraint(
        "refresh_tokens_token_hash_key",
        "refresh_tokens",
        type_="unique",
    )
    op.drop_constraint("users_email_key", "users", type_="unique")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email")
