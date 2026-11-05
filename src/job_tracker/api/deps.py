"""FastAPI dependencies."""

from functools import lru_cache

from pydantic import ValidationError

from job_tracker.config import Settings


@lru_cache
def get_settings() -> Settings:
    """Return validated settings.

    Validation runs when the application is created, so a missing required
    value fails process startup rather than the first HTTP request.
    """
    try:
        return Settings()
    except ValidationError as exc:
        field_names = ", ".join(
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        )
        raise RuntimeError(
            f"Missing or invalid application settings: {field_names}"
        ) from None
