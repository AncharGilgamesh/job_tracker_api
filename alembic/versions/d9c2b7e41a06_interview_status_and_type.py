"""Add nullable interview status and type.

Revision ID: d9c2b7e41a06
Revises: a7d4e2c91b58
Create Date: 2026-11-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9c2b7e41a06"
down_revision: str | None = "a7d4e2c91b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable interview status and type without a server default."""
    op.add_column("interviews", sa.Column("status", sa.Text(), nullable=True))
    op.add_column(
        "interviews",
        sa.Column("interview_type", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop interview type, then status."""
    op.drop_column("interviews", "interview_type")
    op.drop_column("interviews", "status")
