"""Personal vacancy routes.

These routes use Bearer authentication only. They do not use the cookie
CSRF check.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.api.deps import get_current_user, get_session
from job_tracker.api.exception_handlers import ApiError
from job_tracker.api.schemas.errors import ErrorDetail
from job_tracker.api.schemas.vacancies import (
    CreateVacancyRequest,
    UpdateVacancyRequest,
    VacancyListResponse,
    VacancyResponse,
)
from job_tracker.db.models.user import User
from job_tracker.services import vacancies as vacancy_service
from job_tracker.services.companies import CompanyNotFoundError
from job_tracker.services.vacancies import (
    CompanyArchivedError,
    EmptyVacancyUpdateError,
    InvalidVacancyTitleError,
    VacancyHasApplicationError,
    VacancyNotFoundError,
)

router = APIRouter(prefix="/vacancies")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_vacancy(
    body: CreateVacancyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VacancyResponse:
    """Create a vacancy owned by the authenticated user."""
    try:
        vacancy = await vacancy_service.create_vacancy(
            session,
            current_user.id,
            body.company_id,
            body.title,
            body.description,
            body.source_url,
            body.location,
        )
    except InvalidVacancyTitleError:
        raise _invalid_title() from None
    except CompanyNotFoundError:
        raise _not_found() from None
    except CompanyArchivedError:
        raise _archived() from None
    return VacancyResponse.model_validate(vacancy)


@router.get("")
async def list_vacancies(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VacancyListResponse:
    """Return the authenticated user's vacancies in id order.

    Registered before ``/{vacancy_id}`` so this path is not parsed as an id.
    """
    vacancies = await vacancy_service.list_vacancies(session, current_user.id)
    return VacancyListResponse(
        items=[VacancyResponse.model_validate(vacancy) for vacancy in vacancies]
    )


@router.get("/{vacancy_id}")
async def get_vacancy(
    vacancy_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VacancyResponse:
    """Return one vacancy owned by the authenticated user."""
    vacancy = await vacancy_service.get_vacancy(
        session,
        vacancy_id,
        current_user.id,
    )
    if vacancy is None:
        raise _not_found()
    return VacancyResponse.model_validate(vacancy)


@router.patch("/{vacancy_id}")
async def update_vacancy(
    vacancy_id: UUID,
    body: UpdateVacancyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VacancyResponse:
    """Update the authenticated user's vacancy."""
    try:
        vacancy = await vacancy_service.update_vacancy(
            session,
            vacancy_id,
            current_user.id,
            body.model_dump(exclude_unset=True),
        )
    except EmptyVacancyUpdateError:
        raise _empty_update() from None
    except InvalidVacancyTitleError:
        raise _invalid_title() from None
    except (VacancyNotFoundError, CompanyNotFoundError):
        raise _not_found() from None
    except CompanyArchivedError:
        raise _archived() from None
    return VacancyResponse.model_validate(vacancy)


@router.delete("/{vacancy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vacancy(
    vacancy_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete the authenticated user's vacancy when it has no application."""
    try:
        await vacancy_service.delete_vacancy(
            session,
            vacancy_id,
            current_user.id,
        )
    except VacancyNotFoundError:
        raise _not_found() from None
    except VacancyHasApplicationError:
        raise _has_application() from None
    return None


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        message="The requested resource was not found",
    )


def _archived() -> ApiError:
    return ApiError(
        status_code=409,
        code="COMPANY_ARCHIVED",
        message="The company is archived",
        details=[],
    )


def _has_application() -> ApiError:
    return ApiError(
        status_code=409,
        code="VACANCY_HAS_APPLICATION",
        message="The vacancy has an application",
        details=[],
    )


def _empty_update() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body",
                code="INVALID_VALUE",
                message="At least one field is required",
            )
        ],
    )


def _invalid_title() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body.title",
                code="INVALID_VALUE",
                message="A title is required",
            )
        ],
    )
