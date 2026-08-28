"""Garmin token rotation must outlive a cold start, and a dead session must fail fast.

Two failures that produced one symptom in production ("the connector is spotty"):

1. Garmin rotates `di_refresh_token` on every refresh and garminconnect writes
   the new value straight back to its tokenstore path. On Cloud Run that path is
   `/tmp`, which dies with the instance, so the next cold start re-read the
   original blob from `GARMIN_TOKENS_JSON` — a refresh token Garmin had already
   invalidated. Every login after the first rotation failed.

2. A failed login left `_api` as None, so *every* subsequent call retried a
   ~3 s login. `_auth_lock` serialises those, so a 90-day range tool spent
   ~9 minutes failing one day at a time and returned an empty result.

Runs standalone (`python tests/test_token_persistence.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import garmlink.client as client_mod  # noqa: E402
from garminconnect import GarminConnectAuthenticationError  # noqa: E402
from garmlink.client import GarminClient  # noqa: E402
from garmlink.tokens import TOKEN_COLLECTION, TOKEN_KEY, GarminTokenStore  # noqa: E402


class _FakeInner:
    """Stands in for garminconnect's `Garmin.client`, which owns the tokens."""

    def __init__(self) -> None:
        self.di_token = "t0"
        self.di_refresh_token = "r0"
        self.di_client_id = "c0"

    def dumps(self) -> str:
        return json.dumps({
            "di_token": self.di_token,
            "di_refresh_token": self.di_refresh_token,
            "di_client_id": self.di_client_id,
        })

    def loads(self, blob: str) -> None:
        data = json.loads(blob)
        self.di_token = data["di_token"]
        self.di_refresh_token = data["di_refresh_token"]
        self.di_client_id = data["di_client_id"]


class _FakeGarmin:
    """Counts logins, records what tokenstore it was handed, and can rotate."""

    logins = 0
    login_failures = 0
    rotate_on_call = False
    last_tokenstore: str | None = None

    def __init__(self, **kwargs) -> None:
        self.client = _FakeInner()

    @classmethod
    def reset(cls) -> None:
        cls.logins = 0
        cls.login_failures = 0
        cls.rotate_on_call = False
        cls.last_tokenstore = None

    def login(self, tokenstore=None):
        type(self).logins += 1
        type(self).last_tokenstore = tokenstore
        if type(self).login_failures > 0:
            type(self).login_failures -= 1
            raise GarminConnectAuthenticationError("login rejected")
        if tokenstore and tokenstore.strip().startswith("{"):
            self.client.loads(tokenstore)
        return (None, None)

    def get_workouts(self, start=0, limit=100):
        if type(self).rotate_on_call:
            # Exactly what Garmin does on refresh: a brand new refresh token,
            # which invalidates the one we were holding.
            self.client.di_token = "t-rotated"
            self.client.di_refresh_token = "r-rotated"
        return [{"workoutId": 1}]


class _FakeKV:
    """Minimal AsyncKeyValue: enough for GarminTokenStore."""

    def __init__(self) -> None:
        self.data: dict[tuple[str | None, str], dict] = {}
        self.puts = 0
        self.fail = False

    async def get(self, key: str, *, collection: str | None = None):
        return self.data.get((collection, key))

    async def put(self, key: str, value, *, collection: str | None = None, ttl=None):
        if self.fail:
            raise RuntimeError("firestore unavailable")
        self.puts += 1
        self.data[(collection, key)] = dict(value)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        return self.data.pop((collection, key), None) is not None


def _client(kv: _FakeKV | None = None) -> GarminClient:
    _FakeGarmin.reset()
    client_mod.Garmin = _FakeGarmin
    store = GarminTokenStore(kv) if kv is not None else None
    return GarminClient(
        email="e", password="p", tokenstore_path="/nonexistent", token_store=store
    )


# --------------------------------------------------------------------------
# 1. Rotation survives the instance
# --------------------------------------------------------------------------

def test_rotated_token_is_persisted():
    kv = _FakeKV()
    c = _client(kv)
    _FakeGarmin.rotate_on_call = True

    asyncio.run(c.call("get_workouts", cache=False))

    stored = kv.data.get((TOKEN_COLLECTION, TOKEN_KEY))
    assert stored is not None, "a rotated token was never written to durable storage"
    blob = json.loads(stored["tokens"])
    assert blob["di_refresh_token"] == "r-rotated", (
        f"stored the pre-rotation token: {blob['di_refresh_token']}"
    )


def test_cold_start_prefers_stored_tokens_over_the_seed():
    """The whole point: a new instance must use the rotated token, not the seed."""
    kv = _FakeKV()
    kv.data[(TOKEN_COLLECTION, TOKEN_KEY)] = {
        "tokens": json.dumps({
            "di_token": "t-rotated",
            "di_refresh_token": "r-rotated",
            "di_client_id": "c0",
        })
    }
    c = _client(kv)

    asyncio.run(c.call("get_workouts", cache=False))

    handed = _FakeGarmin.last_tokenstore
    assert handed is not None and handed.strip().startswith("{"), (
        f"cold start fell back to the stale seed path: {handed!r}"
    )
    assert json.loads(handed)["di_refresh_token"] == "r-rotated"


def test_unchanged_tokens_are_not_rewritten():
    """Firestore writes are per-rotation, not per-call."""
    kv = _FakeKV()
    c = _client(kv)

    async def run():
        for _ in range(5):
            await c.call("get_workouts", cache=False)

    asyncio.run(run())
    assert kv.puts == 1, f"expected one write for the initial token, got {kv.puts}"


def test_persistence_failure_does_not_break_the_call():
    """A Firestore hiccup must not turn a working tool call into an error."""
    kv = _FakeKV()
    kv.fail = True
    c = _client(kv)
    _FakeGarmin.rotate_on_call = True

    result = asyncio.run(c.call("get_workouts", cache=False))
    assert result == [{"workoutId": 1}], result


def test_no_store_still_uses_the_tokenstore_path():
    """Local dev has a real persistent tokenstore; keep the existing behaviour."""
    c = _client(None)
    asyncio.run(c.call("get_workouts", cache=False))
    assert _FakeGarmin.last_tokenstore == "/nonexistent", _FakeGarmin.last_tokenstore


# --------------------------------------------------------------------------
# 2. A dead session fails fast instead of grinding
# --------------------------------------------------------------------------

def test_auth_failure_does_not_relogin_on_every_call():
    """The 9-minute range tool: 90 calls must not mean 90 failing logins."""
    c = _client(None)
    _FakeGarmin.login_failures = 999

    async def run():
        for _ in range(20):
            try:
                await c.call("get_workouts", cache=False)
            except GarminConnectAuthenticationError:
                pass

    asyncio.run(run())
    assert _FakeGarmin.logins == 1, (
        f"login storm: {_FakeGarmin.logins} Garmin logins for 20 calls with a dead session"
    )


def test_auth_is_retried_once_the_cooldown_expires():
    """Fail-fast must not become fail-forever — the session has to be able to recover."""
    c = _client(None)
    _FakeGarmin.login_failures = 1  # first login fails, the next would succeed
    original = client_mod._AUTH_FAILURE_COOLDOWN
    client_mod._AUTH_FAILURE_COOLDOWN = 0.0
    try:
        async def run():
            try:
                await c.call("get_workouts", cache=False)
            except GarminConnectAuthenticationError:
                pass
            return await c.call("get_workouts", cache=False)

        result = asyncio.run(run())
    finally:
        client_mod._AUTH_FAILURE_COOLDOWN = original

    assert result == [{"workoutId": 1}], result
    assert _FakeGarmin.logins == 2, (
        f"expected the cooldown to expire and allow a retry, got {_FakeGarmin.logins} logins"
    )


def test_cooldown_error_names_the_original_failure():
    """/readyz and the tool error must still say what actually went wrong."""
    c = _client(None)
    _FakeGarmin.login_failures = 999

    async def run():
        for _ in range(2):
            try:
                await c.call("get_workouts", cache=False)
            except GarminConnectAuthenticationError as exc:
                last = exc
        return last

    exc = asyncio.run(run())
    assert "login rejected" in str(exc), (
        f"the cooldown error hid the root cause: {exc}"
    )
    assert c.auth_status()["garmin"] == "error"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
