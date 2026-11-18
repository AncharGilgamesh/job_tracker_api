"""Application payloads."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

_STATUS = Literal[
    "APPLIED",
    "SCREENING",
    "INTERVIEW",
    "OFFER",
    "REJECTED",
    "WITHDRAWN",
]


class CreateApplicationRequest(BaseModel):
    """Client body for creating an application.

    The server assigns the vacancy, the owner, and the status. No client
    field is accepted.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ApplicationResponse(BaseModel):
    """One application on a vacancy owned by the authenticated user."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    vacancy_id: UUID
    status: str


class UpdateApplicationRequest(BaseModel):
    """The only field a client may send when changing status."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: _STATUS


class ApplicationHistoryItem(BaseModel):
    """One recorded status change."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    application_id: UUID
    from_status: str | None
    to_status: str
    changed_at: datetime


class ApplicationHistoryResponse(BaseModel):
    """Status changes for one application, oldest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[ApplicationHistoryItem]
