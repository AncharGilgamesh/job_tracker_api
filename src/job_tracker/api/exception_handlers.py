"""D22 error responses.

Public bodies contain only a stable code, a safe message, and a details array.
They never include validation input, Pydantic context, SQL, stack traces,
passwords, password hashes, JWTs, or token hashes.
"""

import re
from collections.abc import Mapping, Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from job_tracker.api.schemas.errors import ErrorBody, ErrorDetail, ErrorResponse

_CODE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")
_EMAIL_MESSAGE_PREFIX = "value is not a valid email address"
_FALLBACK_DETAIL = ("INVALID_VALUE", "The value is invalid")
_VALIDATION_CODE = "VALIDATION_ERROR"
_VALIDATION_MESSAGE = "Request validation failed"
_UNEXPECTED_CODE = "INTERNAL_ERROR"
_UNEXPECTED_MESSAGE = "An unexpected error occurred"

_VALIDATION_DETAILS: dict[str, tuple[str, str]] = {
    "missing": ("REQUIRED", "This field is required"),
    "missing_argument": ("REQUIRED", "This field is required"),
    "extra_forbidden": ("UNEXPECTED_FIELD", "This field is not allowed"),
    "json_invalid": ("INVALID_JSON", "The request body must be valid JSON"),
    "json_type": ("INVALID_JSON", "The request body must be valid JSON"),
    "string_type": ("INVALID_TYPE", "A string is required"),
    "string_too_short": ("INVALID_LENGTH", "The value length is invalid"),
    "string_too_long": ("INVALID_LENGTH", "The value length is invalid"),
    "string_pattern_mismatch": ("INVALID_FORMAT", "The value format is invalid"),
    "string_unicode": ("INVALID_FORMAT", "The value format is invalid"),
    "int_type": ("INVALID_TYPE", "An integer is required"),
    "int_parsing": ("INVALID_TYPE", "An integer is required"),
    "int_parsing_size": ("INVALID_TYPE", "An integer is required"),
    "int_from_float": ("INVALID_TYPE", "An integer is required"),
    "bool_type": ("INVALID_TYPE", "A boolean is required"),
    "bool_parsing": ("INVALID_TYPE", "A boolean is required"),
    "float_type": ("INVALID_TYPE", "A number is required"),
    "float_parsing": ("INVALID_TYPE", "A number is required"),
    "decimal_type": ("INVALID_TYPE", "A number is required"),
    "decimal_parsing": ("INVALID_TYPE", "A number is required"),
    "uuid_type": ("INVALID_FORMAT", "A valid UUID is required"),
    "uuid_parsing": ("INVALID_FORMAT", "A valid UUID is required"),
    "uuid_version": ("INVALID_FORMAT", "A valid UUID is required"),
    "url_type": ("INVALID_FORMAT", "A valid URL is required"),
    "url_parsing": ("INVALID_FORMAT", "A valid URL is required"),
    "url_scheme": ("INVALID_FORMAT", "A valid URL is required"),
    "url_syntax": ("INVALID_FORMAT", "A valid URL is required"),
    "url_too_long": ("INVALID_FORMAT", "A valid URL is required"),
    "date_type": ("INVALID_FORMAT", "A valid date is required"),
    "date_parsing": ("INVALID_FORMAT", "A valid date is required"),
    "time_type": ("INVALID_FORMAT", "A valid time is required"),
    "time_parsing": ("INVALID_FORMAT", "A valid time is required"),
    "datetime_type": ("INVALID_FORMAT", "A valid timestamp is required"),
    "datetime_parsing": ("INVALID_FORMAT", "A valid timestamp is required"),
    "datetime_from_date_parsing": ("INVALID_FORMAT", "A valid timestamp is required"),
    "time_delta_type": ("INVALID_TYPE", "The value has an invalid type"),
    "time_delta_parsing": ("INVALID_FORMAT", "The value format is invalid"),
    "enum": ("INVALID_VALUE", "The value is not an allowed choice"),
    "literal_error": ("INVALID_VALUE", "The value is not an allowed choice"),
    "greater_than": ("OUT_OF_RANGE", "The value is out of range"),
    "greater_than_equal": ("OUT_OF_RANGE", "The value is out of range"),
    "less_than": ("OUT_OF_RANGE", "The value is out of range"),
    "less_than_equal": ("OUT_OF_RANGE", "The value is out of range"),
    "multiple_of": ("INVALID_VALUE", "The value is invalid"),
    "finite_number": ("INVALID_VALUE", "The value is invalid"),
    "too_short": ("INVALID_LENGTH", "The value length is invalid"),
    "too_long": ("INVALID_LENGTH", "The value length is invalid"),
    "list_type": ("INVALID_TYPE", "The value has an invalid type"),
    "dict_type": ("INVALID_TYPE", "The value has an invalid type"),
    "set_type": ("INVALID_TYPE", "The value has an invalid type"),
    "tuple_type": ("INVALID_TYPE", "The value has an invalid type"),
    "frozenset_type": ("INVALID_TYPE", "The value has an invalid type"),
    "model_type": ("INVALID_TYPE", "The value has an invalid type"),
    "model_attributes_type": ("INVALID_TYPE", "A JSON object is required"),
    "bytes_type": ("INVALID_TYPE", "The value has an invalid type"),
    "bytes_too_short": ("INVALID_LENGTH", "The value length is invalid"),
    "bytes_too_long": ("INVALID_LENGTH", "The value length is invalid"),
    "value_error": ("INVALID_VALUE", "The value is invalid"),
    "assertion_error": ("INVALID_VALUE", "The value is invalid"),
}

_HTTP_ERRORS: dict[int, tuple[str, str]] = {
    400: ("BAD_REQUEST", "The request is invalid"),
    401: ("UNAUTHORIZED", "Authentication is required"),
    403: ("FORBIDDEN", "The request is forbidden"),
    404: ("NOT_FOUND", "Not found"),
    405: ("METHOD_NOT_ALLOWED", "The method is not allowed"),
    406: ("NOT_ACCEPTABLE", "The requested representation is not acceptable"),
    409: ("CONFLICT", "The request conflicts with the current resource"),
    413: ("PAYLOAD_TOO_LARGE", "The request is too large"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "The media type is not supported"),
    422: ("UNPROCESSABLE_CONTENT", "The request could not be processed"),
    429: ("TOO_MANY_REQUESTS", "Too many requests"),
    500: (_UNEXPECTED_CODE, _UNEXPECTED_MESSAGE),
}


class ApiError(Exception):
    """A safe application error that handlers render as the D22 envelope.

    ``message`` and every detail message must already be safe for clients.
    This exception does not redact them.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Sequence[ErrorDetail] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise TypeError("API error status must be an integer.")
        if status_code < 400 or status_code > 599:
            raise ValueError("API error status must be an HTTP error status.")
        self.status_code = status_code
        self.code = _require_code(code)
        self.message = _require_message(message)
        self.details = _require_details(details)
        self.headers = _require_headers(headers)
        super().__init__(self.message)


def register_exception_handlers(application: FastAPI) -> None:
    """Install the shared error handlers on this application."""
    application.add_exception_handler(ApiError, api_error_handler)
    application.add_exception_handler(
        RequestValidationError,
        request_validation_handler,
    )
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(Exception, unexpected_error_handler)


def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render an application error, including its response headers."""
    del request
    return _error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


def request_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Render request validation failures with public codes and messages."""
    del request
    details = [_validation_detail(error) for error in exc.errors()]
    return _error_response(
        status_code=422,
        code=_VALIDATION_CODE,
        message=_VALIDATION_MESSAGE,
        details=details,
    )


def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Render an HTTP exception without copying its detail text."""
    del request
    code, message = _HTTP_ERRORS.get(
        exc.status_code,
        ("HTTP_ERROR", "The request could not be completed"),
    )
    return _error_response(
        status_code=exc.status_code,
        code=code,
        message=message,
        headers=exc.headers,
    )


def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unexpected failure without its type, message, or traceback."""
    del request, exc
    return _error_response(
        status_code=500,
        code=_UNEXPECTED_CODE,
        message=_UNEXPECTED_MESSAGE,
    )


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: Sequence[ErrorDetail] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=list(details))
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=None if headers is None else dict(headers),
    )


def _validation_detail(error: object) -> ErrorDetail:
    if not isinstance(error, dict):
        code, message = _FALLBACK_DETAIL
        return ErrorDetail(field=None, code=code, message=message)
    error_type = error.get("type")
    raw_message = error.get("msg")
    if not isinstance(error_type, str):
        error_type = ""
    if not isinstance(raw_message, str):
        raw_message = ""
    code, message = _public_validation(error_type, raw_message)
    return ErrorDetail(field=_field_path(error.get("loc")), code=code, message=message)


def _public_validation(error_type: str, raw_message: str) -> tuple[str, str]:
    if error_type == "value_error" and raw_message.startswith(_EMAIL_MESSAGE_PREFIX):
        return ("INVALID_FORMAT", "A valid email address is required")
    return _VALIDATION_DETAILS.get(error_type, _FALLBACK_DETAIL)


def _field_path(loc: object) -> str | None:
    if not isinstance(loc, list | tuple) or len(loc) == 0:
        return None
    parts: list[str] = []
    for part in loc:
        if isinstance(part, bool) or not isinstance(part, str | int):
            return None
        parts.append(str(part))
    return ".".join(parts)


def _require_code(code: object) -> str:
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        raise ValueError("API error code must be UPPER_SNAKE_CASE.")
    return code


def _require_message(message: object) -> str:
    if not isinstance(message, str) or message.strip() == "":
        raise ValueError("API error message must be a non-empty string.")
    return message


def _require_details(details: Sequence[ErrorDetail] | None) -> tuple[ErrorDetail, ...]:
    if details is None:
        return ()
    checked: list[ErrorDetail] = []
    for detail in details:
        if not isinstance(detail, ErrorDetail):
            raise TypeError("API error details must be ErrorDetail values.")
        _require_code(detail.code)
        _require_message(detail.message)
        if detail.field is not None and not isinstance(detail.field, str):
            raise TypeError("API error detail field must be a string or null.")
        checked.append(detail)
    return tuple(checked)


def _require_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    if headers is None:
        return None
    if not isinstance(headers, Mapping):
        raise TypeError("API error headers must be a mapping.")
    copied: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("API error headers must be strings.")
        copied[key] = value
    return copied
