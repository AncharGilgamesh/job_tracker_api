"""Authentication orchestration that does not need PostgreSQL."""

import asyncio
import hashlib
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from tests.auth_support import (
    AuthHarness,
    clear_settings_caches,
    configure_test_environment,
    other_unique_error,
    user_with_hash,
    users_email_unique_error,
)

from job_tracker.services import auth as auth_module
from job_tracker.services import refresh_tokens
from job_tracker.services.auth import (
    InvalidCredentialsError,
    LoginResult,
    _is_users_email_unique_violation,
    authenticate,
    register_account,
)
from job_tracker.services.passwords import _hasher as password_hasher
from job_tracker.services.passwords import hash_password
from job_tracker.services.refresh_tokens import (
    REFRESH_TOKEN_TTL,
    hash_refresh_token,
    issue_initial_refresh_token,
)

PASSWORD = "c" * 15
USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_test_environment(monkeypatch)
    _clear_dummy_hash()
    yield
    _clear_dummy_hash()
    clear_settings_caches()


def _clear_dummy_hash() -> None:
    """Drop cached dummy-hash state so tests do not share it."""
    auth_module._DUMMY_PASSWORD_HASH = None
    auth_module._dummy_hash_lock = None
    auth_module._dummy_hash_loop = None


def _assert_same_secret(left: str, right: str, message: str) -> None:
    if left != right:
        raise AssertionError(message)


def _assert_approved_dummy_hash(encoded: str) -> None:
    pieces = encoded.split("$")
    if (
        len(pieces) != 6
        or pieces[1] != "argon2id"
        or pieces[2] != "v=19"
        or pieces[3] != "m=19456,t=2,p=1"
    ):
        raise AssertionError("dummy hash does not use the approved Argon2id parameters")
    try:
        verified = password_hasher.verify(encoded, auth_module._DUMMY_PASSWORD)
    except Exception:
        raise AssertionError("dummy hash could not be verified") from None
    if verified is not True:
        raise AssertionError("dummy hash could not be verified")


def test_refresh_lifetime_is_thirty_days() -> None:
    assert int(REFRESH_TOKEN_TTL.total_seconds()) == 2_592_000


@pytest.mark.asyncio
async def test_initial_refresh_token_is_hashed_and_not_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        refresh_tokens.secrets,
        "token_urlsafe",
        lambda nbytes: f"raw-token-{nbytes}",
    )
    harness = AuthHarness()
    session = harness.open_session()

    first = await issue_initial_refresh_token(session, USER_ID)
    second = await issue_initial_refresh_token(session, USER_ID)

    assert first == second == "raw-token-32"
    assert session.commits == 0
    assert session.rollbacks == 0
    assert len(session.pending_tokens) == 2
    families = {token.family_id for token in session.pending_tokens}
    assert len(families) == 2
    now = datetime.now(UTC)
    for token in session.pending_tokens:
        assert token.user_id == USER_ID
        assert token.revoked_at is None
        assert token.replaced_by_id is None
        assert token.created_at is None
        assert token.token_hash == hash_refresh_token(first)
        assert token.token_hash == hashlib.sha256(first.encode("utf-8")).hexdigest()
        assert first not in token.token_hash
        assert token.token_hash.islower()
        remaining = token.expires_at - now
        assert timedelta(days=30) - timedelta(seconds=5) <= remaining
        assert remaining <= timedelta(days=30) + timedelta(seconds=5)


def test_login_result_repr_hides_tokens() -> None:
    result = LoginResult(access_token="header.payload.sig", refresh_token="raw-token")

    rendered = repr(result)
    assert "raw-token" not in rendered
    assert "header.payload.sig" not in rendered


def test_only_users_email_unique_violation_is_recognized() -> None:
    assert _is_users_email_unique_violation(users_email_unique_error()) is True
    assert _is_users_email_unique_violation(other_unique_error()) is False


@pytest.mark.asyncio
async def test_registration_commits_without_lookup_or_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_lookup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("registration looked up the email")

    async def fail_issue(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("registration issued a refresh token")

    monkeypatch.setattr(auth_module, "get_user_by_email", fail_lookup)
    monkeypatch.setattr(auth_module, "issue_initial_refresh_token", fail_issue)
    monkeypatch.setattr(
        auth_module,
        "create_access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("registration issued an access token")
        ),
    )
    harness = AuthHarness()
    session = harness.open_session()

    await register_account(session, "  User@Example.COM ", PASSWORD)

    assert session.commits == 1
    assert session.rollbacks == 0
    assert set(harness.store.hashes) == {"user@example.com"}
    assert PASSWORD not in harness.store.hashes["user@example.com"]
    assert harness.store.tokens == []


@pytest.mark.asyncio
async def test_duplicate_registration_rolls_back_to_the_same_result() -> None:
    harness = AuthHarness()
    password_hash = await hash_password(PASSWORD)
    first = harness.open_session()
    await register_account(first, "User@Example.com", PASSWORD)
    stored = harness.store.hashes["user@example.com"]
    second = harness.open_session()

    await register_account(second, " user@example.com ", PASSWORD)

    assert second.commits == 0
    assert second.rollbacks == 1
    assert harness.store.hashes == {"user@example.com": stored}
    assert stored == password_hash or stored.startswith("$argon2id$")


@pytest.mark.asyncio
async def test_unrelated_integrity_error_propagates() -> None:
    harness = AuthHarness()
    harness.flush_error = other_unique_error()
    session = harness.open_session()

    with pytest.raises(Exception) as exc_info:
        await register_account(session, "other@example.com", PASSWORD)

    assert _is_users_email_unique_violation(exc_info.value) is False
    assert session.rollbacks == 1
    assert session.commits == 0
    assert harness.store.hashes == {}
    assert "other@example.com" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_unknown_and_wrong_password_each_verify_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = await hash_password(PASSWORD)
    events: list[tuple[str, str]] = []
    real_verify = auth_module.verify_password

    async def spy(password: str, encoded_hash: str) -> bool:
        events.append(("verify", encoded_hash))
        return await real_verify(password, encoded_hash)

    monkeypatch.setattr(auth_module, "verify_password", spy)
    harness = AuthHarness()
    unknown = harness.open_session()

    with pytest.raises(InvalidCredentialsError) as unknown_error:
        await authenticate(unknown, "missing@example.com", "d" * 15)

    if len(events) != 1 or events[0][0] != "verify":
        raise AssertionError("unknown email did not verify once")
    _assert_same_secret(
        events[0][1],
        auth_module._DUMMY_PASSWORD_HASH or "",
        "unknown email did not verify the cached dummy hash",
    )
    assert unknown.rollbacks == 1
    assert unknown.commits == 0
    assert "d" * 15 not in str(unknown_error.value)
    events.clear()
    harness.lookup = user_with_hash(USER_ID, "user@example.com", encoded)
    wrong = harness.open_session()

    with pytest.raises(InvalidCredentialsError):
        await authenticate(wrong, "user@example.com", "d" * 15)

    assert events == [("verify", encoded)]
    assert wrong.rollbacks == 1
    assert wrong.commits == 0
    assert harness.store.tokens == []


@pytest.mark.asyncio
async def test_login_commits_one_new_family(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = await hash_password(PASSWORD)
    events: list[str] = []
    real_verify = auth_module.verify_password
    real_hash = auth_module.hash_password

    async def spy_verify(password: str, encoded_hash: str) -> bool:
        events.append("verify")
        return await real_verify(password, encoded_hash)

    async def spy_hash(password: str) -> str:
        events.append("hash")
        return await real_hash(password)

    monkeypatch.setattr(auth_module, "verify_password", spy_verify)
    monkeypatch.setattr(auth_module, "hash_password", spy_hash)
    harness = AuthHarness()
    harness.lookup = user_with_hash(USER_ID, "user@example.com", encoded)
    session = harness.open_session()

    result = await authenticate(session, " User@Example.com ", PASSWORD)
    second = harness.open_session()
    second_result = await authenticate(second, "user@example.com", PASSWORD)

    assert events == ["verify", "verify"]
    assert session.commits == 1
    assert second.commits == 1
    assert result.token_type == "bearer"
    assert result.expires_in == 900
    assert result.refresh_token
    assert second_result.refresh_token
    assert result.refresh_token not in result.access_token
    assert result.refresh_token != second_result.refresh_token
    assert len(harness.store.tokens) == 2
    assert harness.store.tokens[0].family_id != harness.store.tokens[1].family_id
    assert result.refresh_token not in repr(result)


def test_importing_auth_does_not_hash_and_cache_starts_empty() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
from job_tracker.services import passwords

class _RejectingHasher:
    def hash(self, _password: str) -> str:
        raise RuntimeError("hash called during import")

passwords._hasher = _RejectingHasher()
from job_tracker.services import auth

if auth._DUMMY_PASSWORD_HASH is not None:
    raise RuntimeError("dummy hash cache was not empty")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr
        if "$argon2" in detail or "x" * 32 in detail:
            detail = "output omitted because it contained a secret"
        raise AssertionError(detail)


@pytest.mark.asyncio
async def test_dummy_hash_is_created_once_through_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if auth_module._DUMMY_PASSWORD_HASH is not None:
        raise AssertionError("dummy hash cache was not empty")
    recorded: list[object] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        recorded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(auth_module.asyncio, "to_thread", spy_to_thread)

    first = await auth_module._dummy_password_hash()
    if recorded != [auth_module._hasher.hash]:
        raise AssertionError("dummy hash was not created through asyncio.to_thread")
    recorded.clear()
    second = await auth_module._dummy_password_hash()

    _assert_approved_dummy_hash(first)
    _assert_same_secret(first, second, "cached dummy hash was not reused")
    _assert_same_secret(
        first,
        auth_module._DUMMY_PASSWORD_HASH or "",
        "dummy hash was not stored in the cache",
    )
    if recorded:
        raise AssertionError("cached dummy hash was hashed again")


@pytest.mark.asyncio
async def test_first_dummy_hash_uses_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[object] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        recorded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(auth_module.asyncio, "to_thread", spy_to_thread)

    encoded = await auth_module._dummy_password_hash()

    if recorded != [auth_module._hasher.hash]:
        raise AssertionError("dummy hash was not created through asyncio.to_thread")
    _assert_approved_dummy_hash(encoded)


@pytest.mark.asyncio
async def test_concurrent_dummy_hash_initialization_hashes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hasher = auth_module._hasher

    class _CountingHasher:
        calls = 0

        def hash(self, password: str) -> str:
            type(self).calls += 1
            time.sleep(0.05)
            return real_hasher.hash(password)

    monkeypatch.setattr(auth_module, "_hasher", _CountingHasher())

    results = await asyncio.gather(
        *(auth_module._dummy_password_hash() for _ in range(8))
    )

    if _CountingHasher.calls != 1:
        raise AssertionError("concurrent initialization hashed more than once")
    if len(set(results)) != 1:
        raise AssertionError("concurrent initialization returned different hashes")
    _assert_approved_dummy_hash(results[0])
