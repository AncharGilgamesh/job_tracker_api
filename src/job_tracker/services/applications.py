"""Applications and status history."""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.application import Application
from job_tracker.db.models.application_status_history import ApplicationStatusHistory
from job_tracker.db.models.interview import Interview
from job_tracker.db.models.vacancy import Vacancy

_VACANCY_UNIQUE_CONSTRAINT = "applications_vacancy_id_key"
_UNIQUE_VIOLATION = "23505"
_INITIAL_STATUS = "APPLIED"


class ApplicationNotFoundError(Exception):
    """The vacancy is not available to this user."""

    def __init__(self) -> None:
        super().__init__("The requested resource was not found")


class ApplicationHasInterviewsError(Exception):
    """The owned application still has an interview."""

    def __init__(self) -> None:
        super().__init__("The application has an interview")


class ApplicationAlreadyExistsError(Exception):
    """The vacancy already has an application."""

    def __init__(self) -> None:
        super().__init__("An application already exists for this vacancy")


class InvalidApplicationStatusError(Exception):
    """The status is outside the approved set."""

    def __init__(self) -> None:
        super().__init__("The value is not an allowed choice")


_APPROVED_STATUSES = frozenset(
    {
        "APPLIED",
        "SCREENING",
        "INTERVIEW",
        "OFFER",
        "REJECTED",
        "WITHDRAWN",
    }
)


async def create_application(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> Application:
    """Insert one APPLIED application and its initial history row."""
    try:
        _require_uuid(vacancy_id, "Vacancy id")
        _require_uuid(user_id, "User id")
        vacancy = await _owned_vacancy(session, vacancy_id, user_id)
        if vacancy is None:
            raise ApplicationNotFoundError()
        await _before_application_insert()
        changed_at = datetime.now(UTC)
        application = Application(status=_INITIAL_STATUS, vacancy_id=vacancy.id)
        session.add(application)
        await session.flush()
        history = ApplicationStatusHistory(
            application_id=application.id,
            from_status=None,
            to_status=_INITIAL_STATUS,
            changed_at=changed_at,
        )
        session.add(history)
        await session.flush()
        await session.commit()
    except ApplicationNotFoundError:
        await _rollback(session)
        raise
    except IntegrityError as exc:
        await _rollback(session)
        if _is_applications_vacancy_unique_violation(exc):
            raise ApplicationAlreadyExistsError() from exc
        raise
    except Exception:
        await _rollback(session)
        raise
    return application


async def get_application(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> Application | None:
    """Return the application whose vacancy belongs to this user."""
    _require_uuid(vacancy_id, "Vacancy id")
    _require_uuid(user_id, "User id")
    result = await session.execute(
        select(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(
            Vacancy.id == vacancy_id,
            Vacancy.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def update_application_status(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
    status: str,
) -> Application:
    """Change the owner's application status, or leave it when it already matches."""
    try:
        _require_uuid(vacancy_id, "Vacancy id")
        _require_uuid(user_id, "User id")
        requested = _require_status(status)
        application = await _lock_owned_application(session, vacancy_id, user_id)
        if application is None:
            raise ApplicationNotFoundError()
        if application.status == requested:
            await session.commit()
            return application
        changed_at = datetime.now(UTC)
        previous = application.status
        application.status = requested
        await session.flush()
        session.add(
            ApplicationStatusHistory(
                application_id=application.id,
                from_status=previous,
                to_status=requested,
                changed_at=changed_at,
            )
        )
        await session.flush()
        await session.commit()
    except (ApplicationNotFoundError, InvalidApplicationStatusError):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise
    return application


async def list_status_history(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> list[ApplicationStatusHistory] | None:
    """Return the owner's status history, oldest change first."""
    application = await get_application(session, vacancy_id, user_id)
    if application is None:
        return None
    result = await session.scalars(
        select(ApplicationStatusHistory)
        .where(ApplicationStatusHistory.application_id == application.id)
        .order_by(
            ApplicationStatusHistory.changed_at.asc(),
            ApplicationStatusHistory.id.asc(),
        )
    )
    return list(result.all())


async def delete_application(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> None:
    """Delete the owned application when it has no interview, then commit."""
    try:
        _require_uuid(vacancy_id, "Vacancy id")
        _require_uuid(user_id, "User id")
        application = await get_application(session, vacancy_id, user_id)
        if application is None:
            raise ApplicationNotFoundError()
        interview_id = await session.scalar(
            select(Interview.id)
            .where(Interview.application_id == application.id)
            .limit(1)
        )
        if interview_id is not None:
            raise ApplicationHasInterviewsError()
        history = list(
            (
                await session.scalars(
                    select(ApplicationStatusHistory).where(
                        ApplicationStatusHistory.application_id == application.id
                    )
                )
            ).all()
        )
        for row in history:
            await session.delete(row)
        await session.flush()
        await session.delete(application)
        await session.flush()
        await session.commit()
    except (ApplicationNotFoundError, ApplicationHasInterviewsError):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise


async def _lock_owned_application(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> Application | None:
    """Lock the application whose vacancy belongs to this user."""
    result = await session.execute(
        select(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(
            Vacancy.id == vacancy_id,
            Vacancy.user_id == user_id,
        )
        .with_for_update(of=Application)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _require_status(status: object) -> str:
    if not isinstance(status, str) or status not in _APPROVED_STATUSES:
        raise InvalidApplicationStatusError()
    return status


async def _before_application_insert() -> None:
    """Return immediately before the application insert."""
    return


async def _owned_vacancy(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> Vacancy | None:
    result = await session.execute(
        select(Vacancy).where(
            Vacancy.id == vacancy_id,
            Vacancy.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def _is_applications_vacancy_unique_violation(exc: IntegrityError) -> bool:
    """True only for PostgreSQL unique violation of applications.vacancy_id."""
    if not isinstance(exc, IntegrityError):
        return False
    for current in _exception_chain(exc):
        if getattr(current, "constraint_name", None) != _VACANCY_UNIQUE_CONSTRAINT:
            continue
        sqlstate = getattr(current, "sqlstate", None)
        pgcode = getattr(current, "pgcode", None)
        if sqlstate == _UNIQUE_VIOLATION or pgcode == _UNIQUE_VIOLATION:
            return True
    return False


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    stack = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            stack.append(orig)
        cause = current.__cause__
        if isinstance(cause, BaseException):
            stack.append(cause)


def _require_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID.")
    return value


async def _rollback(session: AsyncSession) -> None:
    if session.in_transaction():
        await session.rollback()
