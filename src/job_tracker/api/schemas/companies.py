"""Company catalog payloads."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CreateCompanyRequest(BaseModel):
    """Company name as supplied. The service normalizes and stores it."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str


class RenameCompanyRequest(BaseModel):
    """Replacement display name. Server-controlled fields are rejected."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str


class CompanyResponse(BaseModel):
    """One company row visible to any authenticated user."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    created_by_user_id: UUID
    name: str
    normalized_name: str
    archived_at: datetime | None


class CompanyListResponse(BaseModel):
    """Company rows for the list and exact-name suggestion endpoints."""

    model_config = ConfigDict(extra="forbid")

    items: list[CompanyResponse]
