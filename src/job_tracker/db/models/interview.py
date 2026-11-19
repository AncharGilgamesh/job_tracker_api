"""Interview table."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from job_tracker.db.base import Base


class Interview(Base):
    """An interview scheduled for an application."""

    __tablename__ = "interviews"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    application_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("applications.id"),
        nullable=False,
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    interview_type: Mapped[str | None] = mapped_column(Text, nullable=True)
