"""Behavioural tests for the fixes to the six critical review findings.

Runs standalone (`python tests/test_critical_fixes.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from garmlink.cache import TTLCache  # noqa: E402
from garmlink.client import GarminClient  # noqa: E402
from garmlink.server import (  # noqa: E402
    MIN_TOKEN_LENGTH,
    BearerAuthMiddleware,
    resolve_auth_token,
)

GOOD_TOKEN = "t" * MIN_TOKEN_LENGTH


class _FakeApi:
    """Stands in for garminconnect.Garmin; counts how often each method ran."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upload_workout(self, workout_json):
        self.calls.append(("upload_workout", workout_json))
        return {"workoutId": len(self.calls)}

    def get_workouts(self, start=0, limit=100):
        self.calls.append(("get_workouts", start, limit))
        return [{"workoutId": 1}]


def _client() -> tuple[GarminClient, _FakeApi]:
    c = GarminClient(email="e", password="p", tokenstore_path="/nonexistent")
    api = _FakeApi()
    c._api = api
    return c, api


# --- Critical 1: auth fails closed -----------------------------------------

def test_missing_token_refuses_to_start(monkeypatch=None):
    old = {k: os.environ.get(k) for k in ("MCP_AUTH_TOKEN", "ALLOW_UNAUTHENTICATED")}
    try:
        os.environ.pop("MCP_AUTH_TOKEN", None)
        os.environ.pop("ALLOW_UNAUTHENTICATED", None)
        try:
            resolve_auth_token()
        except RuntimeError as exc:
            assert "MCP_AUTH_TOKEN is required" in str(exc)
        else:
            raise AssertionError("missing MCP_AUTH_TOKEN must abort startup")

        # Empty / whitespace-only is treated as missing, not as "open mode".
        os.environ["MCP_AUTH_TOKEN"] = "   "
        try:
            resolve_auth_token()
        except RuntimeError:
            pass
        else:
            raise AssertionError("blank MCP_AUTH_TOKEN must abort startup")

        # Short tokens are rejected.
        os.environ["MCP_AUTH_TOKEN"] = "short"
        try:
            resolve_auth_token()
        except RuntimeError as exc:
            assert "at least" in str(exc)
        else:
            raise AssertionError("short MCP_AUTH_TOKEN must abort startup")

        # A real token is accepted.
        os.environ["MCP_AUTH_TOKEN"] = GOOD_TOKEN
        assert resolve_auth_token() == GOOD_TOKEN

        # Opting out is explicit and deliberate.
        os.environ.pop("MCP_AUTH_TOKEN")
        os.environ["ALLOW_UNAUTHENTICATED"] = "1"
        assert resolve_auth_token() is None
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_middleware_rejects_empty_token():
    try:
        BearerAuthMiddleware(app=None, token="")
    except RuntimeError:
        return
    raise AssertionError("middleware must refuse to construct without a token")


def test_middleware_auth_decisions():
    mw = BearerAuthMiddleware(app=None, token=GOOD_TOKEN)

    class _URL:
        def __init__(self, path):
            self.path = path

    class _Req:
        def __init__(self, path, auth=None):
            self.url = _URL(path)
            self.headers = {"Authorization": auth} if auth else {}

    async def passthrough(_req):
        return "PASSED"

    async def run():
        # /health is open so the platform's health check works.
        assert await mw.dispatch(_Req("/health"), passthrough) == "PASSED"
        # /readyz is NOT open — it exposes internal Garmin session state.
        resp = await mw.dispatch(_Req("/readyz"), passthrough)
        assert getattr(resp, "status_code", None) == 401, "/readyz must require auth"
        assert await mw.dispatch(
            _Req("/readyz", f"Bearer {GOOD_TOKEN}"), passthrough
        ) == "PASSED"
        # No header, wrong token, and near-miss token are all rejected.
        for auth in (None, "Bearer wrong", f"Bearer {GOOD_TOKEN[:-1]}x", GOOD_TOKEN):
            resp = await mw.dispatch(_Req("/mcp", auth), passthrough)
            assert getattr(resp, "status_code", None) == 401, (auth, resp)
        # Correct token passes.
        assert await mw.dispatch(_Req("/mcp", f"Bearer {GOOD_TOKEN}"), passthrough) == "PASSED"

    asyncio.run(run())


# --- Critical 3: unhashable args must not raise -----------------------------

def test_unhashable_arg_does_not_raise():
    c, api = _client()

    async def run():
        # A dict positional arg is unhashable; the call must still go through.
        r1 = await c.call("upload_workout", {"workoutName": "x"}, cache=False)
        r2 = await c.call("upload_workout", {"workoutName": "x"}, cache=False)
        assert r1 == {"workoutId": 1} and r2 == {"workoutId": 2}

    asyncio.run(run())
    assert len(api.calls) == 2


def test_unhashable_arg_never_stores_a_none_key():
    """The None cache key used to be stored, then crashed invalidate()."""
    c, _ = _client()

    async def run():
        await c.call("upload_workout", {"a": 1})  # cache=True, unhashable
        c.invalidate("get_workouts")  # used to raise TypeError on the None key

    asyncio.run(run())


# --- Critical 6: writes are never served from the read cache ----------------

def test_writes_are_not_cached():
    c, api = _client()

    async def run():
        for _ in range(3):
            await c.call("upload_workout", "same-payload", cache=False)

    asyncio.run(run())
    assert len(api.calls) == 3, f"write was served from cache: {api.calls}"


def test_reads_are_still_cached():
    c, api = _client()

    async def run():
        for _ in range(3):
            await c.call("get_workouts", 0, 100)

    asyncio.run(run())
    assert len(api.calls) == 1, f"read cache not working: {api.calls}"


# --- Cache: sentinel default distinguishes "cached None" from "no entry" ----

def test_cache_get_sentinel():
    cache = TTLCache()
    sentinel = object()
    key = ("m", (), frozenset())
    assert cache.get(key, default=sentinel) is sentinel
    cache.set(key, None, ttl=60)
    assert cache.get(key, default=sentinel) is None


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
