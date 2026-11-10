"""Public API error envelope."""

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """One safe public detail. ``field`` is null when it does not apply."""

    model_config = ConfigDict(extra="forbid")

    field: str | None
    code: str
    message: str


class ErrorBody(BaseModel):
    """The ``error`` object. ``details`` is an empty array when nothing more applies."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Top-level error response. The HTTP status stays outside this body."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
