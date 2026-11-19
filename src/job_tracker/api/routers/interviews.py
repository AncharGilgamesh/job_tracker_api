"""Interview routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.api.deps import get_current_user, get_session
from job_tracker.api.exception_handlers import ApiError
from job_tracker.api.schemas.errors import ErrorDetail
from job_tracker.api.schemas.interviews import (
    CreateInterviewRequest,
    InterviewListResponse,
    InterviewResponse,
    UpdateInterviewRequest,
)
from job_tracker.db.models.user import User
from job_tracker.services import interviews as interview_service
from job_tracker.services.interviews import (
    EmptyInterviewUpdateError,
    InterviewNotFoundError,
    UnsupportedInterviewStatusError,
    UnsupportedInterviewTypeError,
)

router = APIRouter()


@router.post(
    "/applications/{application_id}/interviews",
    status_code=status.HTTP_201_CREATED,
)
async def create_interview(
    application_id: UUID,
    body: CreateInterviewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InterviewResponse:
    """Create an interview on the authenticated user's application."""
    try:
        interview = await interview_service.create_interview(
            session,
            application_id,
            current_user.id,
            body.scheduled_at,
            body.notes,
            body.outcome,
            body.status,
            body.interview_type,
        )
    except InterviewNotFoundError:
        raise _not_found() from None
    except UnsupportedInterviewStatusError:
        raise _unsupported_status() from None
    except UnsupportedInterviewTypeError:
        raise _unsupported_type() from None
    return InterviewResponse.model_validate(interview)


@router.get("/applications/{application_id}/interviews")
async def list_interviews(
    application_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InterviewListResponse:
    """Return the authenticated user's interviews for this application."""
    interviews = await interview_service.list_interviews(
        session,
        application_id,
        current_user.id,
    )
    if interviews is None:
        raise _not_found()
    return InterviewListResponse(
        items=[InterviewResponse.model_validate(row) for row in interviews]
    )


@router.get("/interviews/{interview_id}")
async def get_interview(
    interview_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InterviewResponse:
    """Return one interview owned through the authenticated user's vacancy."""
    interview = await interview_service.get_interview(
        session,
        interview_id,
        current_user.id,
    )
    if interview is None:
        raise _not_found()
    return InterviewResponse.model_validate(interview)


@router.patch("/interviews/{interview_id}")
async def update_interview(
    interview_id: UUID,
    body: UpdateInterviewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InterviewResponse:
    """Update the authenticated user's interview."""
    try:
        interview = await interview_service.update_interview(
            session,
            interview_id,
            current_user.id,
            body.model_dump(exclude_unset=True),
        )
    except InterviewNotFoundError:
        raise _not_found() from None
    except EmptyInterviewUpdateError:
        raise _empty_update() from None
    except UnsupportedInterviewStatusError:
        raise _unsupported_status() from None
    except UnsupportedInterviewTypeError:
        raise _unsupported_type() from None
    return InterviewResponse.model_validate(interview)


@router.delete("/interviews/{interview_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_interview(
    interview_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete the authenticated user's interview."""
    try:
        await interview_service.delete_interview(
            session,
            interview_id,
            current_user.id,
        )
    except InterviewNotFoundError:
        raise _not_found() from None
    return None


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        message="The requested resource was not found",
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


def _unsupported_status() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body.status",
                code="INVALID_VALUE",
                message="The interview status is not supported",
            )
        ],
    )


def _unsupported_type() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body.interview_type",
                code="INVALID_VALUE",
                message="The interview type is not supported",
            )
        ],
    )
