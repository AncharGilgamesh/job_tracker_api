"""Process liveness endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    """Liveness response. This check does not contact the database."""

    status: str


@router.get("/health")
async def health() -> HealthResponse:
    """Report that the process is running."""
    return HealthResponse(status="ok")
