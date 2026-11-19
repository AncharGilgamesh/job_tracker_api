"""Interview payloads."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError

_STATUSES = frozenset({"SCHEDULED", "COMPLETED", "CANCELLED", "NO_SHOW"})
_INTERVIEW_TYPES = frozenset({"PHONE", "VIDEO", "ONSITE"})


def _reject_null_scheduled_at(value: object) -> object:
    """Reject explicit null without treating an omitted time as null.

    This validator does not run for the default that fills an omitted key.
    """
    if value is None:
        raise PydanticCustomError(
            "datetime_type",
            "Input should be a valid datetime",
        )
    return value


def _require_aware_scheduled_at(value: datetime) -> datetime:
    """Reject a timestamp that has no offset."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise PydanticCustomError(
            "timezone_required",
            "A valid timestamp is required",
        )
    return value


def _optional_status(value: object) -> str | None:
    """Allow null or an exact approved status."""
    if value is None:
        return None
    if isinstance(value, str) and value in _STATUSES:
        return value
    raise PydanticCustomError(
        "interview_status_unsupported",
        "The interview status is not supported",
    )


def _optional_interview_type(value: object) -> str | None:
    """Allow null or an exact approved interview type."""
    if value is None:
        return None
    if isinstance(value, str) and value in _INTERVIEW_TYPES:
        return value
    raise PydanticCustomError(
        "interview_type_unsupported",
        "The interview type is not supported",
    )


class CreateInterviewRequest(BaseModel):
    """Fields the client may send when scheduling an interview.

    ``application_id`` comes from the path. ``user_id`` is not accepted.
    Omitted notes, outcome, status, and interview type are stored as null.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    scheduled_at: datetime
    notes: str | None = None
    outcome: str | None = None
    status: str | None = None
    interview_type: str | None = None

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def reject_null_scheduled_at(cls, value: object) -> object:
        """Reject null. A missing key stays a required-field error."""
        return _reject_null_scheduled_at(value)

    @field_validator("scheduled_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Accept any offset. A naive timestamp is rejected."""
        return _require_aware_scheduled_at(value)

    @field_validator("status", mode="before")
    @classmethod
    def reject_unsupported_status(cls, value: object) -> str | None:
        """Allow null or an approved status. Any other value is rejected."""
        return _optional_status(value)

    @field_validator("interview_type", mode="before")
    @classmethod
    def reject_unsupported_interview_type(cls, value: object) -> str | None:
        """Allow null or an approved type. Any other value is rejected."""
        return _optional_interview_type(value)


class UpdateInterviewRequest(BaseModel):
    """Fields present on a partial interview update.

    A missing key stays unchanged. Explicit null clears notes, outcome,
    status, and interview type. ``scheduled_at`` cannot be null.
    ``user_id`` is not accepted.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    scheduled_at: datetime | None = None
    notes: str | None = None
    outcome: str | None = None
    status: str | None = None
    interview_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty(cls, data: object) -> object:
        """Reject a body that did not include any field."""
        if isinstance(data, dict) and len(data) == 0:
            raise PydanticCustomError(
                "at_least_one_field",
                "At least one field is required",
            )
        return data

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def reject_null_scheduled_at(cls, value: object) -> object:
        """Reject explicit null without treating an omitted time as null."""
        return _reject_null_scheduled_at(value)

    @field_validator("scheduled_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Accept any offset when the client sent a timestamp."""
        if value is None:
            return None
        return _require_aware_scheduled_at(value)

    @field_validator("status", mode="before")
    @classmethod
    def reject_unsupported_status(cls, value: object) -> str | None:
        """Allow null or an approved status. Any other value is rejected."""
        return _optional_status(value)

    @field_validator("interview_type", mode="before")
    @classmethod
    def reject_unsupported_interview_type(cls, value: object) -> str | None:
        """Allow null or an approved type. Any other value is rejected."""
        return _optional_interview_type(value)


class InterviewResponse(BaseModel):
    """One interview on an application owned by the authenticated user."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    application_id: UUID
    scheduled_at: datetime
    outcome: str | None
    notes: str | None
    status: str | None
    interview_type: str | None


class InterviewListResponse(BaseModel):
    """Interviews for one application owned by the authenticated user."""

    model_config = ConfigDict(extra="forbid")

    items: list[InterviewResponse]
