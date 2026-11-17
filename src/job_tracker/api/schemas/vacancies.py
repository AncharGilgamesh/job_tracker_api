"""Vacancy payloads."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError


class CreateVacancyRequest(BaseModel):
    """Fields the client may send when saving a vacancy.

    ``user_id`` is not accepted. ``source_url`` is plain text.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    company_id: UUID
    title: str
    description: str | None = None
    source_url: str | None = None
    location: str | None = None

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, title: str) -> str:
        """Reject a title that has no non-whitespace character.

        A title that contains other text is returned unchanged, including
        its surrounding spaces.
        """
        if title.strip() == "":
            raise PydanticCustomError("title_required", "A title is required")
        return title


class UpdateVacancyRequest(BaseModel):
    """Fields present on a partial vacancy update.

    A missing key stays unchanged. Explicit null is kept for the nullable
    fields. ``user_id`` is not accepted.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    company_id: UUID | None = None
    title: str | None = None
    description: str | None = None
    source_url: str | None = None
    location: str | None = None

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

    @field_validator("company_id", mode="before")
    @classmethod
    def reject_null_company_id(cls, company_id: object) -> object:
        """Reject explicit null without treating an omitted company as null.

        This validator does not run for the default that fills an omitted key.
        """
        if company_id is None:
            raise PydanticCustomError(
                "uuid_type",
                "UUID input should be a string, bytes or UUID object",
            )
        return company_id

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, title: object) -> str:
        """Reject null or whitespace without rewriting a real title.

        This validator does not run when title was omitted.
        """
        if not isinstance(title, str) or title.strip() == "":
            raise PydanticCustomError("title_required", "A title is required")
        return title


class VacancyResponse(BaseModel):
    """One vacancy owned by the authenticated user."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    user_id: UUID
    company_id: UUID
    title: str
    description: str | None
    source_url: str | None
    location: str | None


class VacancyListResponse(BaseModel):
    """Vacancy rows owned by the authenticated user."""

    model_config = ConfigDict(extra="forbid")

    items: list[VacancyResponse]
