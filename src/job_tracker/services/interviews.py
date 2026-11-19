"""Interviews on an application."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.application import Application
from job_tracker.db.models.interview import Interview
from job_tracker.db.models.vacancy import Vacancy

_UPDATE_FIELDS = (
    "scheduled_at",
    "notes",
    "outcome",
    "status",
    "interview_type",
)
_STATUSES = frozenset({"SCHEDULED", "COMPLETED", "CANCELLED", "NO_SHOW"})
_INTERVIEW_TYPES = frozenset({"PHONE", "VIDEO", "ONSITE"})


class InterviewNotFoundError(Exception):
    """The application or interview is not available to this user."""

    def __init__(self) -> None:
        super().__init__("The requested resource was not found")


class EmptyInterviewUpdateError(Exception):
    """The update did not include a field."""

    def __init__(self) -> None:
        super().__init__("At least one field is required")


class UnsupportedInterviewStatusError(Exception):
    """The status is outside the approved set."""

    def __init__(self) -> None:
        super().__init__("The interview status is not supported")


class UnsupportedInterviewTypeError(Exception):
    """The interview type is outside the approved set."""

    def __init__(self) -> None:
        super().__init__("The interview type is not supported")


async def create_interview(
    session: AsyncSession,
    application_id: UUID,
    user_id: UUID,
    scheduled_at: datetime,
    notes: str | None,
    outcome: str | None,
    status: str | None = None,
    interview_type: str | None = None,
) -> Interview:
    """Insert one interview for this user's application and commit it."""
    try:
        _require_uuid(application_id, "Application id")
        _require_uuid(user_id, "User id")
        when = _as_utc(scheduled_at)
        stored_notes = _optional_text(notes)
        stored_outcome = _optional_text(outcome)
        application = await _owned_application(session, application_id, user_id)
        if application is None:
            raise InterviewNotFoundError()
        stored_status = _optional_status(status)
        stored_type = _optional_interview_type(interview_type)
        interview = Interview(
            application_id=application.id,
            scheduled_at=when,
            notes=stored_notes,
            outcome=stored_outcome,
            status=stored_status,
            interview_type=stored_type,
        )
        session.add(interview)
        await session.flush()
        await session.commit()
    except (
        InterviewNotFoundError,
        UnsupportedInterviewStatusError,
        UnsupportedInterviewTypeError,
    ):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise
    return interview


async def list_interviews(
    session: AsyncSession,
    application_id: UUID,
    user_id: UUID,
) -> list[Interview] | None:
    """Return this user's interviews for the application, in id order."""
    _require_uuid(application_id, "Application id")
    _require_uuid(user_id, "User id")
    application = await _owned_application(session, application_id, user_id)
    if application is None:
        return None
    result = await session.scalars(
        _interview_select(user_id)
        .where(Interview.application_id == application.id)
        .order_by(Interview.id.asc())
    )
    return list(result.all())


async def get_interview(
    session: AsyncSession,
    interview_id: UUID,
    user_id: UUID,
) -> Interview | None:
    """Return this user's interview, or ``None`` when it is absent."""
    _require_uuid(interview_id, "Interview id")
    _require_uuid(user_id, "User id")
    result = await session.execute(
        _interview_select(user_id).where(Interview.id == interview_id)
    )
    return result.scalar_one_or_none()


async def update_interview(
    session: AsyncSession,
    interview_id: UUID,
    user_id: UUID,
    changes: dict[str, object],
) -> Interview:
    """Apply the present fields and commit them."""
    try:
        _require_uuid(interview_id, "Interview id")
        _require_uuid(user_id, "User id")
        interview = await get_interview(session, interview_id, user_id)
        if interview is None:
            raise InterviewNotFoundError()
        present = {
            field: changes[field] for field in _UPDATE_FIELDS if field in changes
        }
        if len(present) == 0:
            raise EmptyInterviewUpdateError()
        stored_status = (
            _optional_status(present["status"]) if "status" in present else None
        )
        stored_type = (
            _optional_interview_type(present["interview_type"])
            if "interview_type" in present
            else None
        )
        if "scheduled_at" in present:
            interview.scheduled_at = _as_utc(present["scheduled_at"])
        if "notes" in present:
            interview.notes = _optional_text(present["notes"])
        if "outcome" in present:
            interview.outcome = _optional_text(present["outcome"])
        if "status" in present:
            interview.status = stored_status
        if "interview_type" in present:
            interview.interview_type = stored_type
        await session.flush()
        await session.commit()
    except (
        InterviewNotFoundError,
        EmptyInterviewUpdateError,
        UnsupportedInterviewStatusError,
        UnsupportedInterviewTypeError,
    ):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise
    return interview


async def delete_interview(
    session: AsyncSession,
    interview_id: UUID,
    user_id: UUID,
) -> None:
    """Delete the owned interview and commit."""
    try:
        _require_uuid(interview_id, "Interview id")
        _require_uuid(user_id, "User id")
        interview = await get_interview(session, interview_id, user_id)
        if interview is None:
            raise InterviewNotFoundError()
        await session.delete(interview)
        await session.flush()
        await session.commit()
    except InterviewNotFoundError:
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise


def _interview_select(user_id: UUID) -> Select[tuple[Interview]]:
    """Interviews whose application vacancy belongs to this user."""
    return (
        select(Interview)
        .join(Application, Application.id == Interview.application_id)
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(Vacancy.user_id == user_id)
    )


async def _owned_application(
    session: AsyncSession,
    application_id: UUID,
    user_id: UUID,
) -> Application | None:
    """Return the application whose vacancy belongs to this user."""
    result = await session.execute(
        select(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(
            Application.id == application_id,
            Vacancy.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Scheduled time must be a datetime.")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("Scheduled time must include a timezone.")
    return value.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise TypeError("Text must be a string or null.")


def _optional_status(value: object) -> str | None:
    """Return null or an exact approved status."""
    if value is None:
        return None
    if isinstance(value, str) and value in _STATUSES:
        return value
    raise UnsupportedInterviewStatusError()


def _optional_interview_type(value: object) -> str | None:
    """Return null or an exact approved interview type."""
    if value is None:
        return None
    if isinstance(value, str) and value in _INTERVIEW_TYPES:
        return value
    raise UnsupportedInterviewTypeError()


def _require_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID.")
    return value


async def _rollback(session: AsyncSession) -> None:
    if session.in_transaction():
        await session.rollback()
