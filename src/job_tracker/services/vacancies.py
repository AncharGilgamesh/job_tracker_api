"""Personal vacancies.

Write operations commit their own success and roll back a failure. Read
operations do not commit. This module does not decide HTTP status codes.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.application import Application
from job_tracker.db.models.vacancy import Vacancy
from job_tracker.services import companies as company_service
from job_tracker.services.companies import CompanyNotFoundError


class InvalidVacancyTitleError(Exception):
    """The title is missing or only whitespace."""

    def __init__(self) -> None:
        super().__init__("A title is required")


class CompanyArchivedError(Exception):
    """The vacancy cannot be assigned to an archived company."""

    def __init__(self) -> None:
        super().__init__("The company is archived")


class VacancyNotFoundError(Exception):
    """No vacancy row exists for this id and owner."""

    def __init__(self) -> None:
        super().__init__("The requested resource was not found")


class VacancyHasApplicationError(Exception):
    """The owned vacancy still has an application."""

    def __init__(self) -> None:
        super().__init__("The vacancy has an application")


class EmptyVacancyUpdateError(Exception):
    """The update did not include a field."""

    def __init__(self) -> None:
        super().__init__("At least one field is required")


_UPDATE_FIELDS = (
    "company_id",
    "title",
    "description",
    "source_url",
    "location",
)


async def create_vacancy(
    session: AsyncSession,
    user_id: UUID,
    company_id: UUID,
    title: str,
    description: str | None,
    source_url: str | None,
    location: str | None,
) -> Vacancy:
    """Insert a vacancy for this user and commit it.

    ``user_id`` is stored as given. The company row is locked, then its
    archive state is read. A missing company and an archived company are
    rejected before an insert. Nullable fields may be ``None``. The title is
    stored unchanged when it contains a non-whitespace character. Domain
    errors and unexpected errors roll back before they propagate.
    """
    try:
        _require_uuid(user_id, "User id")
        _require_uuid(company_id, "Company id")
        _require_title(title)
        company = await company_service.lock_company_by_id(session, company_id)
        if company is None:
            raise CompanyNotFoundError()
        if company.archived_at is not None:
            raise CompanyArchivedError()
        vacancy = Vacancy(
            user_id=user_id,
            company_id=company_id,
            title=title,
            description=description,
            source_url=source_url,
            location=location,
        )
        session.add(vacancy)
        await session.flush()
        await session.commit()
    except (
        InvalidVacancyTitleError,
        CompanyNotFoundError,
        CompanyArchivedError,
    ):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise
    return vacancy


async def get_vacancy(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> Vacancy | None:
    """Return the caller's vacancy, or ``None`` when it is absent.

    The query matches both the vacancy id and the owner. A missing vacancy
    and another user's vacancy are both absent. This read does not commit
    or roll back.
    """
    _require_uuid(vacancy_id, "Vacancy id")
    _require_uuid(user_id, "User id")
    result = await session.execute(
        select(Vacancy).where(
            Vacancy.id == vacancy_id,
            Vacancy.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_vacancies(session: AsyncSession, user_id: UUID) -> list[Vacancy]:
    """Return the caller's vacancies in id order.

    This read does not commit or roll back.
    """
    _require_uuid(user_id, "User id")
    result = await session.scalars(
        select(Vacancy).where(Vacancy.user_id == user_id).order_by(Vacancy.id.asc())
    )
    return list(result.all())


async def update_vacancy(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
    changes: dict[str, object],
) -> Vacancy:
    """Apply the present fields and commit them.

    The vacancy is loaded with its id and owner together. A company change
    locks only the new company. The current company is not locked and is not
    checked again when the id is unchanged or absent. Nullable fields become
    null when the present value is ``None``. Domain errors and unexpected
    errors roll back before they propagate.
    """
    try:
        _require_uuid(vacancy_id, "Vacancy id")
        _require_uuid(user_id, "User id")
        vacancy = await get_vacancy(session, vacancy_id, user_id)
        if vacancy is None:
            raise VacancyNotFoundError()
        present = {
            field: changes[field] for field in _UPDATE_FIELDS if field in changes
        }
        if len(present) == 0:
            raise EmptyVacancyUpdateError()
        if "company_id" in present:
            new_company_id = _require_uuid(present["company_id"], "Company id")
            if new_company_id != vacancy.company_id:
                company = await company_service.lock_company_by_id(
                    session,
                    new_company_id,
                )
                if company is None:
                    raise CompanyNotFoundError()
                if company.archived_at is not None:
                    raise CompanyArchivedError()
                vacancy.company_id = new_company_id
        if "title" in present:
            _require_title(present["title"])
            title = present["title"]
            if isinstance(title, str):
                vacancy.title = title
        for field in ("description", "source_url", "location"):
            if field in present:
                setattr(vacancy, field, present[field])
        await session.flush()
        await session.commit()
    except (
        VacancyNotFoundError,
        EmptyVacancyUpdateError,
        InvalidVacancyTitleError,
        CompanyNotFoundError,
        CompanyArchivedError,
    ):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise
    return vacancy


async def delete_vacancy(
    session: AsyncSession,
    vacancy_id: UUID,
    user_id: UUID,
) -> None:
    """Delete the owned vacancy when it has no application, then commit.

    The vacancy is loaded with its id and owner together. An existing
    application is rejected before the vacancy is deleted. This write does
    not lock a company. Domain errors and unexpected errors roll back before
    they propagate.
    """
    try:
        _require_uuid(vacancy_id, "Vacancy id")
        _require_uuid(user_id, "User id")
        vacancy = await get_vacancy(session, vacancy_id, user_id)
        if vacancy is None:
            raise VacancyNotFoundError()
        application_id = await session.scalar(
            select(Application.id).where(Application.vacancy_id == vacancy.id)
        )
        if application_id is not None:
            raise VacancyHasApplicationError()
        await session.delete(vacancy)
        await session.flush()
        await session.commit()
    except (VacancyNotFoundError, VacancyHasApplicationError):
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise


def _require_title(title: object) -> None:
    if not isinstance(title, str) or title.strip() == "":
        raise InvalidVacancyTitleError()


def _require_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID.")
    return value


async def _rollback(session: AsyncSession) -> None:
    if session.in_transaction():
        await session.rollback()
