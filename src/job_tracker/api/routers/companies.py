"""Shared company catalog routes.

These routes use Bearer authentication only. They do not use the cookie
CSRF check.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.api.deps import get_current_user, get_session
from job_tracker.api.exception_handlers import ApiError
from job_tracker.api.schemas.companies import (
    CompanyListResponse,
    CompanyResponse,
    CreateCompanyRequest,
    RenameCompanyRequest,
)
from job_tracker.api.schemas.errors import ErrorDetail
from job_tracker.db.models.company import Company
from job_tracker.db.models.user import User
from job_tracker.services import companies as company_service
from job_tracker.services.companies import (
    CompanyForbiddenError,
    CompanyNotFoundError,
    InvalidCompanyNameError,
)

router = APIRouter(prefix="/companies")

_INVALID_NAME_CODE = "INVALID_NAME"
_INVALID_NAME_MESSAGE = "A company name is required"


@router.get("")
async def list_companies(
    _current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyListResponse:
    """Return every company, including archived companies."""
    companies = await company_service.list_companies(session)
    return _company_list(companies)


@router.get("/suggestions")
async def suggest_companies(
    name: Annotated[str, Query()],
    _current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyListResponse:
    """Return companies with this exact normalized name.

    Registered before ``/{company_id}`` so the static path is not parsed as
    an id.
    """
    try:
        companies = await company_service.suggest_companies(session, name)
    except InvalidCompanyNameError:
        raise _invalid_name("query.name") from None
    return _company_list(companies)


@router.get("/{company_id}")
async def get_company(
    company_id: UUID,
    _current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Return one company, whether it is active or archived."""
    company = await company_service.get_company(session, company_id)
    if company is None:
        raise _not_found()
    return _company_response(company)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_company(
    body: CreateCompanyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Create a company for the authenticated user."""
    try:
        company = await company_service.create_company(
            session,
            current_user.id,
            body.name,
        )
    except InvalidCompanyNameError:
        raise _invalid_name("body.name") from None
    return _company_response(company)


@router.patch("/{company_id}")
async def rename_company(
    company_id: UUID,
    body: RenameCompanyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Rename a company when the authenticated user created it."""
    try:
        company = await company_service.rename_company(
            session,
            company_id,
            current_user.id,
            body.name,
        )
    except InvalidCompanyNameError:
        raise _invalid_name("body.name") from None
    except CompanyNotFoundError:
        raise _not_found() from None
    except CompanyForbiddenError:
        raise _forbidden() from None
    return _company_response(company)


@router.post("/{company_id}/archive")
async def archive_company(
    company_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanyResponse:
    """Archive a company when the authenticated user created it."""
    try:
        company = await company_service.archive_company(
            session,
            company_id,
            current_user.id,
        )
    except CompanyNotFoundError:
        raise _not_found() from None
    except CompanyForbiddenError:
        raise _forbidden() from None
    return _company_response(company)


def _company_list(companies: list[Company]) -> CompanyListResponse:
    return CompanyListResponse(
        items=[_company_response(company) for company in companies]
    )


def _company_response(company: Company) -> CompanyResponse:
    return CompanyResponse.model_validate(company)


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="RESOURCE_NOT_FOUND",
        message="The requested resource was not found",
    )


def _forbidden() -> ApiError:
    return ApiError(
        status_code=403,
        code="FORBIDDEN",
        message="The request is forbidden",
    )


def _invalid_name(field: str) -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field=field,
                code=_INVALID_NAME_CODE,
                message=_INVALID_NAME_MESSAGE,
            )
        ],
    )
