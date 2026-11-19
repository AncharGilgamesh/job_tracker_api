"""Application routes for a vacancy."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.api.deps import get_current_user, get_session
from job_tracker.api.exception_handlers import ApiError
from job_tracker.api.schemas.applications import (
    ApplicationHistoryItem,
    ApplicationHistoryResponse,
    ApplicationResponse,
    CreateApplicationRequest,
    UpdateApplicationRequest,
)
from job_tracker.api.schemas.errors import ErrorDetail
from job_tracker.db.models.user import User
from job_tracker.services import applications as application_service
from job_tracker.services.applications import (
    ApplicationAlreadyExistsError,
    ApplicationHasInterviewsError,
    ApplicationNotFoundError,
    InvalidApplicationStatusError,
)

router = APIRouter(prefix="/vacancies")


@router.post(
    "/{vacancy_id}/applications",
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    vacancy_id: UUID,
    _body: Annotated[CreateApplicationRequest, Body()],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ApplicationResponse:
    """Create the authenticated user's application for this vacancy."""
    try:
        application = await application_service.create_application(
            session,
            vacancy_id,
            current_user.id,
        )
    except ApplicationNotFoundError:
        raise _not_found() from None
    except ApplicationAlreadyExistsError:
        raise _already_exists() from None
    return ApplicationResponse.model_validate(application)


@router.get("/{vacancy_id}/applications")
async def get_application(
    vacancy_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ApplicationResponse:
    """Return the authenticated user's application for this vacancy."""
    application = await application_service.get_application(
        session,
        vacancy_id,
        current_user.id,
    )
    if application is None:
        raise _not_found()
    return ApplicationResponse.model_validate(application)


@router.patch("/{vacancy_id}/applications")
async def update_application(
    vacancy_id: UUID,
    body: UpdateApplicationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ApplicationResponse:
    """Change the authenticated user's application status."""
    try:
        application = await application_service.update_application_status(
            session,
            vacancy_id,
            current_user.id,
            body.status,
        )
    except ApplicationNotFoundError:
        raise _not_found() from None
    except InvalidApplicationStatusError:
        raise _invalid_status() from None
    return ApplicationResponse.model_validate(application)


@router.delete(
    "/{vacancy_id}/applications",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_application(
    vacancy_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete the authenticated user's application when it has no interview."""
    try:
        await application_service.delete_application(
            session,
            vacancy_id,
            current_user.id,
        )
    except ApplicationNotFoundError:
        raise _not_found() from None
    except ApplicationHasInterviewsError:
        raise _has_interviews() from None
    return None


@router.get("/{vacancy_id}/applications/history")
async def list_application_history(
    vacancy_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ApplicationHistoryResponse:
    """Return the authenticated user's status history for this vacancy."""
    history = await application_service.list_status_history(
        session,
        vacancy_id,
        current_user.id,
    )
    if history is None:
        raise _not_found()
    return ApplicationHistoryResponse(
        items=[ApplicationHistoryItem.model_validate(row) for row in history]
    )


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        message="The requested resource was not found",
        details=[],
    )


def _has_interviews() -> ApiError:
    return ApiError(
        status_code=409,
        code="APPLICATION_HAS_INTERVIEWS",
        message="The application has an interview",
        details=[],
    )


def _already_exists() -> ApiError:
    return ApiError(
        status_code=409,
        code="APPLICATION_ALREADY_EXISTS",
        message="An application already exists for this vacancy",
        details=[],
    )


def _invalid_status() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body.status",
                code="INVALID_VALUE",
                message="The value is not an allowed choice",
            )
        ],
    )
