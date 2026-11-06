"""ORM models.

Importing this package registers every application table on ``Base.metadata``.
"""

from job_tracker.db.models.application import Application
from job_tracker.db.models.application_status_history import ApplicationStatusHistory
from job_tracker.db.models.company import Company
from job_tracker.db.models.interview import Interview
from job_tracker.db.models.refresh_token import RefreshToken
from job_tracker.db.models.user import User
from job_tracker.db.models.vacancy import Vacancy

__all__ = [
    "Application",
    "ApplicationStatusHistory",
    "Company",
    "Interview",
    "RefreshToken",
    "User",
    "Vacancy",
]
