"""User table."""

from uuid import UUID

from sqlalchemy import Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from job_tracker.db.base import Base


class User(Base):
    """A user identity.

    Email and the encoded password hash are the approved account columns.
    Profile columns are not part of the approved schema.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="users_email_key"),)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
