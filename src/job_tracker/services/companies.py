"""Shared company catalog.

Write operations commit their own success and roll back a failure. Read
operations do not commit. This module does not decide HTTP status codes.
"""

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from job_tracker.db.models.company import Company


class InvalidCompanyNameError(Exception):
    """The name is empty after the approved normalization."""

    def __init__(self) -> None:
        super().__init__("A company name is required")


class CompanyNotFoundError(Exception):
    """No company row exists for this id."""

    def __init__(self) -> None:
        super().__init__("The requested resource was not found")


class CompanyForbiddenError(Exception):
    """The actor is not the company creator."""

    def __init__(self) -> None:
        super().__init__("The request is forbidden")


@dataclass(frozen=True)
class NormalizedCompanyName:
    """Display name and the casefolded value used for exact comparison."""

    name: str
    normalized_name: str


def normalize_company_name(name: str) -> NormalizedCompanyName:
    """Return the display name and its casefolded comparison value.

    Display ``name`` is Unicode NFKC, stripped, with repeated internal
    whitespace collapsed to one space. The original letter casing and
    punctuation are preserved. ``normalized_name`` is ``str.casefold`` of
    that display name. A name that becomes empty is rejected.
    """
    if not isinstance(name, str):
        raise TypeError("Company name must be a string.")
    display_name = " ".join(unicodedata.normalize("NFKC", name).split())
    if display_name == "":
        raise InvalidCompanyNameError()
    return NormalizedCompanyName(
        name=display_name,
        normalized_name=display_name.casefold(),
    )


async def list_companies(session: AsyncSession) -> list[Company]:
    """Return every company, including archived rows.

    This read does not commit or roll back.
    """
    result = await session.scalars(select(Company))
    return list(result.all())


async def suggest_companies(session: AsyncSession, name: str) -> list[Company]:
    """Return companies whose normalized name equals the normalized query.

    Comparison is exact. Archived rows stay eligible. This read does not
    commit or roll back.
    """
    normalized = normalize_company_name(name)
    result = await session.scalars(
        select(Company).where(Company.normalized_name == normalized.normalized_name)
    )
    return list(result.all())


async def get_company(session: AsyncSession, company_id: UUID) -> Company | None:
    """Return one company, archived or active, or ``None`` when it is absent.

    This read does not lock the row, and it does not commit or roll back.
    """
    _require_uuid(company_id, "Company id")
    result = await session.execute(select(Company).where(Company.id == company_id))
    return result.scalar_one_or_none()


async def lock_company_by_id(
    session: AsyncSession,
    company_id: UUID,
) -> Company | None:
    """Lock one company row with ``SELECT ... FOR UPDATE`` and return it.

    The returned object reflects the database row. The caller's session owns
    the transaction. This helper does not commit, roll back, or begin one.
    """
    _require_uuid(company_id, "Company id")
    result = await session.execute(
        select(Company)
        .where(Company.id == company_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def create_company(
    session: AsyncSession,
    created_by_user_id: UUID,
    name: str,
) -> Company:
    """Insert a company for this creator and commit it.

    Duplicate normalized names are stored as separate rows. ``archived_at``
    stays null. A failed insert is rolled back.
    """
    try:
        normalized = normalize_company_name(name)
        _require_uuid(created_by_user_id, "Creator id")
        company = Company(
            created_by_user_id=created_by_user_id,
            name=normalized.name,
            normalized_name=normalized.normalized_name,
            archived_at=None,
        )
        session.add(company)
        await session.flush()
        await session.commit()
    except InvalidCompanyNameError:
        raise
    except Exception:
        await _rollback(session)
        raise
    return company


async def rename_company(
    session: AsyncSession,
    company_id: UUID,
    actor_id: UUID,
    name: str,
) -> Company:
    """Rename a company when the actor is its creator, then commit.

    A missing company and a different creator are reported before the name
    changes. ``created_by_user_id`` and ``archived_at`` stay as they are.
    """
    try:
        normalized = normalize_company_name(name)
        company = await _company_for_creator(session, company_id, actor_id)
        company.name = normalized.name
        company.normalized_name = normalized.normalized_name
        await session.flush()
        await session.commit()
    except (InvalidCompanyNameError, CompanyNotFoundError, CompanyForbiddenError):
        raise
    except Exception:
        await _rollback(session)
        raise
    return company


async def archive_company(
    session: AsyncSession,
    company_id: UUID,
    actor_id: UUID,
) -> Company:
    """Archive a company for its creator and commit.

    The company row is locked before ``archived_at`` is read or written. A
    missing company is reported before the creator check. The first archive
    stores the current timezone-aware UTC time. A later archive keeps that
    original timestamp and does not read the clock again. There is no
    unarchive. A missing company, a different creator, and an unexpected
    failure roll back so the row lock is released.
    """
    try:
        company = await lock_company_by_id(session, company_id)
        if company is None:
            raise CompanyNotFoundError()
        _require_uuid(actor_id, "Actor id")
        if company.created_by_user_id != actor_id:
            raise CompanyForbiddenError()
        if company.archived_at is None:
            company.archived_at = datetime.now(UTC)
        await session.flush()
        await session.commit()
    except (CompanyNotFoundError, CompanyForbiddenError):
        # These reads succeeded, so loaded ids are still present. Detach them
        # before rollback, which would otherwise expire every attached object.
        session.expunge_all()
        await _rollback(session)
        raise
    except Exception:
        await _rollback(session)
        raise
    return company


async def _company_for_creator(
    session: AsyncSession,
    company_id: UUID,
    actor_id: UUID,
) -> Company:
    """Return the company or raise when it is missing or not owned.

    Existence is decided before the creator check. This helper does not
    commit or roll back.
    """
    _require_uuid(actor_id, "Actor id")
    company = await get_company(session, company_id)
    if company is None:
        raise CompanyNotFoundError()
    if company.created_by_user_id != actor_id:
        raise CompanyForbiddenError()
    return company


def _require_uuid(value: UUID, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID.")


async def _rollback(session: AsyncSession) -> None:
    if session.in_transaction():
        await session.rollback()
