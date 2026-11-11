"""Registration and login routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.api.deps import get_session, get_settings, require_csrf
from job_tracker.api.exception_handlers import ApiError
from job_tracker.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegistrationRequest,
    RegistrationResponse,
)
from job_tracker.api.schemas.errors import ErrorDetail
from job_tracker.config import Settings
from job_tracker.services.auth import (
    InvalidCredentialsError,
    InvalidEmailError,
    authenticate,
    register_account,
)
from job_tracker.services.passwords import PasswordPolicyError

router = APIRouter(prefix="/auth")

_REGISTRATION_MESSAGE = "Registration request processed. You can now try signing in."
_REFRESH_COOKIE_NAME = "refresh_token"
_REFRESH_COOKIE_PATH = "/auth"
_REFRESH_COOKIE_MAX_AGE = 2_592_000


@router.post("/register")
async def register(
    body: RegistrationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RegistrationResponse:
    """Create an account without a session cookie or an access token.

    Registration does not read or set the refresh cookie, so it does not use
    the login CSRF check.
    """
    try:
        await register_account(session, body.email, body.password)
    except InvalidEmailError:
        raise _invalid_email() from None
    except PasswordPolicyError:
        raise _password_policy() from None
    return RegistrationResponse(message=_REGISTRATION_MESSAGE)


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_csrf)],
) -> LoginResponse:
    """Verify credentials, then set the refresh cookie after commit."""
    try:
        result = await authenticate(session, body.email, body.password)
    except InvalidEmailError:
        raise _invalid_email() from None
    except InvalidCredentialsError:
        raise _invalid_credentials() from None
    _set_refresh_cookie(
        response,
        result.refresh_token,
        secure=settings.environment != "local",
    )
    return LoginResponse(
        access_token=result.access_token,
        token_type="bearer",
        expires_in=result.expires_in,
    )


def _set_refresh_cookie(response: Response, raw_token: str, *, secure: bool) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_token,
        max_age=_REFRESH_COOKIE_MAX_AGE,
        path=_REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _invalid_email() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body.email",
                code="INVALID_FORMAT",
                message="A valid email address is required",
            )
        ],
    )


def _password_policy() -> ApiError:
    return ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details=[
            ErrorDetail(
                field="body.password",
                code="PASSWORD_POLICY_VIOLATION",
                message="Password must be between 15 and 128 characters",
            )
        ],
    )


def _invalid_credentials() -> ApiError:
    return ApiError(
        status_code=401,
        code="INVALID_CREDENTIALS",
        message="Invalid email or password",
    )
