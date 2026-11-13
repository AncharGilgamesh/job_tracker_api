"""Refresh and logout decisions that do not need PostgreSQL."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.auth_support import clear_settings_caches, configure_test_environment

from job_tracker.services import auth as auth_module
from job_tracker.services.auth import (
    RefreshOutcome,
    RefreshResult,
    logout_account,
    rotate_refresh_token,
)
from job_tracker.services.refresh_tokens import (
    REFRESH_TOKEN_TTL,
    create_replacement_refresh_token,
    get_refresh_token_by_hash,
    hash_refresh_token,
    revoke_active_family_tokens,
)
from job_tracker.services.users import lock_user_by_id

USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
FAMILY_ID = UUID("123e4567-e89b-12d3-a456-426614174001")
OTHER_FAMILY_ID = UUID("123e4567-e89b-12d3-a456-426614174002")
RAW_TOKEN = "presented-refresh-token"


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.open = False
        self.fail_commit = False

    def begin(self) -> None:
        raise AssertionError("session.begin was called")

    def in_transaction(self) -> bool:
        return self.open

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.commits += 1
        self.open = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.open = False


class _Token:
    def __init__(self, **kwargs: object) -> None:
        self.id = kwargs.get("id", uuid4())
        self.user_id = kwargs["user_id"]
        self.family_id = kwargs["family_id"]
        self.expires_at = kwargs["expires_at"]
        self.revoked_at = kwargs.get("revoked_at")
        self.replaced_by_id = kwargs.get("replaced_by_id")


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> object:
        if len(self._rows) != 1:
            return None
        return self._rows[0]

    def scalars(self) -> "_Rows":
        return self

    def __iter__(self):
        return iter(self._rows)


class _Recording:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = [] if rows is None else rows
        self.statements: list[object] = []
        self.added: list[object] = []
        self.flushes = 0

    def begin(self) -> None:
        raise AssertionError("session.begin was called")

    async def execute(self, statement: object) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        raise AssertionError("helper committed")

    async def rollback(self) -> None:
        raise AssertionError("helper rolled back")


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=5)


def _token(**overrides: object) -> _Token:
    values: dict[str, object] = {
        "user_id": USER_ID,
        "family_id": FAMILY_ID,
        "expires_at": _future(),
        "revoked_at": None,
        "replaced_by_id": None,
    }
    values.update(overrides)
    return _Token(**values)


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_test_environment(monkeypatch)
    yield
    clear_settings_caches()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    lookups: list[object],
    *,
    locked: object | None = object(),
) -> dict[str, list[object]]:
    seen: dict[str, list[object]] = {
        "hashes": [],
        "locks": [],
        "families": [],
        "replacements": [],
    }

    async def lookup(session: _Session, token_hash: str) -> object:
        session.open = True
        seen["hashes"].append(token_hash)
        if not lookups:
            return None
        return lookups.pop(0)

    async def lock(session: _Session, user_id: UUID) -> object | None:
        session.open = True
        seen["locks"].append(user_id)
        return locked

    async def revoke(session: _Session, family_id: UUID, revoked_at: datetime) -> None:
        session.open = True
        seen["families"].append((family_id, revoked_at))

    async def replace(
        session: _Session,
        *,
        user_id: UUID,
        family_id: UUID,
        issued_at: datetime,
    ) -> tuple[str, _Token]:
        session.open = True
        replacement = _token(user_id=user_id, family_id=family_id)
        seen["replacements"].append((user_id, family_id, issued_at, replacement))
        return "replacement-raw-token", replacement

    monkeypatch.setattr(auth_module, "get_refresh_token_by_hash", lookup)
    monkeypatch.setattr(auth_module, "lock_user_by_id", lock)
    monkeypatch.setattr(auth_module, "revoke_active_family_tokens", revoke)
    monkeypatch.setattr(auth_module, "create_replacement_refresh_token", replace)
    return seen


def test_refresh_result_repr_hides_tokens() -> None:
    result = RefreshResult(
        RefreshOutcome.ROTATED,
        access_token="header.payload.sig",
        refresh_token="raw-refresh-token",
    )

    rendered = repr(result)
    assert "raw-refresh-token" not in rendered
    assert "header.payload.sig" not in rendered
    assert result.outcome.name in rendered


@pytest.mark.asyncio
async def test_unknown_refresh_token_revokes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, [])
    session = _Session()

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.INVALID
    assert seen["hashes"] == [hash_refresh_token(RAW_TOKEN)]
    assert RAW_TOKEN not in seen["hashes"]
    assert seen["locks"] == []
    assert seen["families"] == []
    assert session.rollbacks == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_decisions_use_the_row_reread_after_the_user_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _token(revoked_at=None, expires_at=_past())
    fresh = _token(revoked_at=datetime.now(UTC), expires_at=_past())
    seen = _install(monkeypatch, [stale, fresh])
    session = _Session()

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.REPLAY
    assert seen["locks"] == [USER_ID]
    assert len(seen["families"]) == 1
    assert seen["families"][0][0] == FAMILY_ID
    assert stale.revoked_at is None
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_expired_unrevoked_token_does_not_revoke_the_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _token(revoked_at=None, expires_at=_past())
    seen = _install(monkeypatch, [_token(), fresh])
    session = _Session()

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.INVALID
    assert seen["families"] == []
    assert seen["replacements"] == []
    assert session.rollbacks == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_revoked_expired_token_commits_family_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _token(revoked_at=_past(), expires_at=_past())
    seen = _install(monkeypatch, [fresh, fresh])
    session = _Session()

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.REPLAY
    assert seen["replacements"] == []
    assert session.commits == 1


@pytest.mark.asyncio
async def test_active_token_links_the_replacement_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _token()
    fresh = _token()
    seen = _install(monkeypatch, [stale, fresh])
    session = _Session()

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.ROTATED
    assert result.token_type == "bearer"
    assert result.expires_in == 900
    assert result.refresh_token == "replacement-raw-token"
    assert result.refresh_token not in (result.access_token or "")
    replacement = seen["replacements"][0][3]
    assert isinstance(replacement, _Token)
    assert fresh.replaced_by_id == replacement.id
    assert fresh.revoked_at is not None
    assert stale.replaced_by_id is None
    assert seen["families"] == []
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_rotation_failure_before_commit_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _token()
    seen = _install(monkeypatch, [_token(), fresh])

    async def fail(
        session: _Session,
        *,
        user_id: UUID,
        family_id: UUID,
        issued_at: datetime,
    ) -> tuple[str, _Token]:
        del user_id, family_id, issued_at
        session.open = True
        raise RuntimeError("replacement failed")

    monkeypatch.setattr(auth_module, "create_replacement_refresh_token", fail)
    session = _Session()

    with pytest.raises(RuntimeError, match="replacement failed"):
        await rotate_refresh_token(session, RAW_TOKEN)

    assert fresh.revoked_at is None
    assert fresh.replaced_by_id is None
    assert seen["families"] == []
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_missing_user_or_reread_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_user = _install(monkeypatch, [_token()], locked=None)
    session = _Session()

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.INVALID
    assert missing_user["families"] == []
    assert session.rollbacks == 1
    session = _Session()
    _install(monkeypatch, [_token(), None])

    result = await rotate_refresh_token(session, RAW_TOKEN)

    assert result.outcome is RefreshOutcome.INVALID
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_logout_missing_cookie_does_not_look_up_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, [])
    session = _Session()

    await logout_account(session, None)
    await logout_account(session, "")

    assert seen["hashes"] == []
    assert session.commits == 0
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_logout_unknown_cookie_revokes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, [None])
    session = _Session()

    await logout_account(session, RAW_TOKEN)

    assert seen["hashes"] == [hash_refresh_token(RAW_TOKEN)]
    assert seen["families"] == []
    assert session.rollbacks == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_logout_revokes_a_known_family_even_when_the_token_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _token(revoked_at=_past())
    seen = _install(monkeypatch, [_token(), fresh])
    session = _Session()

    await logout_account(session, RAW_TOKEN)

    assert seen["families"][0][0] == FAMILY_ID
    assert seen["families"][0][0] != OTHER_FAMILY_ID
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_logout_failure_rolls_back_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [_token(), _token()])

    async def fail(session: _Session, family_id: UUID, revoked_at: datetime) -> None:
        del family_id, revoked_at
        session.open = True
        raise RuntimeError("revoke failed")

    monkeypatch.setattr(auth_module, "revoke_active_family_tokens", fail)
    session = _Session()

    with pytest.raises(RuntimeError, match="revoke failed"):
        await logout_account(session, RAW_TOKEN)

    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_user_lock_query_is_for_update_and_does_not_commit() -> None:
    session = _Recording()

    found = await lock_user_by_id(session, USER_ID)  # type: ignore[arg-type]

    assert found is None
    statement = session.statements[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert statement.get_execution_options()["populate_existing"] is True
    assert session.flushes == 0


@pytest.mark.asyncio
async def test_token_reread_refreshes_existing_state() -> None:
    session = _Recording()

    found = await get_refresh_token_by_hash(session, "a" * 64)  # type: ignore[arg-type]

    assert found is None
    statement = session.statements[0]
    assert statement.get_execution_options()["populate_existing"] is True
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "token_hash" in compiled


@pytest.mark.asyncio
async def test_family_revocation_updates_only_returned_active_rows() -> None:
    active = _token()
    session = _Recording([active])
    revoked_at = datetime.now(UTC)

    await revoke_active_family_tokens(session, FAMILY_ID, revoked_at)  # type: ignore[arg-type]

    assert active.revoked_at == revoked_at
    compiled = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "family_id" in compiled
    assert "revoked_at IS NULL" in compiled
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_replacement_helper_flushes_one_family_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "job_tracker.services.refresh_tokens.secrets.token_urlsafe",
        lambda nbytes: "raw-token-32" if nbytes == 32 else "",
    )
    session = _Recording()
    issued_at = datetime.now(UTC)

    raw_token, replacement = await create_replacement_refresh_token(  # type: ignore[arg-type]
        session,
        user_id=USER_ID,
        family_id=FAMILY_ID,
        issued_at=issued_at,
    )

    assert raw_token == "raw-token-32"
    assert replacement.family_id == FAMILY_ID
    assert replacement.user_id == USER_ID
    assert replacement.revoked_at is None
    assert replacement.replaced_by_id is None
    assert replacement.token_hash == hash_refresh_token(raw_token)
    assert replacement.expires_at - issued_at == REFRESH_TOKEN_TTL
    assert raw_token not in replacement.token_hash
    assert session.flushes == 1
    assert session.added == [replacement]
