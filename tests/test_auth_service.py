"""Authentication orchestration that does not need PostgreSQL."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.auth_support import (
    clear_settings_caches,
    configure_test_environment,
)

from job_tracker.services import auth as auth_module
from job_tracker.services.passwords import _hasher as password_hasher


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
