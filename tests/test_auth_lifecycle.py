"""Lazy, self-healing Garmin authentication.

Auth must not happen at construction or at server startup: on Cloud Run the
service scales to zero, so a network call in the startup path is paid on every
cold start and a Garmin outage would fail the deploy. Instead the client
authenticates on first use and re-authenticates once when a session dies.

Runs standalone (`python tests/test_auth_lifecycle.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import garmin_mcp.client as client_mod  # noqa: E402
from garminconnect import GarminConnectAuthenticationError  # noqa: E402
from garmin_mcp.client import GarminClient  # noqa: E402


class _FakeGarmin:
    """Stands in for garminconnect.Garmin, counting logins and calls."""

    logins = 0
    login_failures = 0        # how many of the next logins should fail
    call_auth_failures = 0    # how many of the next get_workouts should 401

    def __init__(self, **kwargs):
        pass

    @classmethod
    def reset(cls):
        cls.logins = 0
        cls.login_failures = 0
        cls.call_auth_failures = 0

    def login(self, tokenstore=None):
        type(self).logins += 1
        if type(self).login_failures > 0:
            type(self).login_failures -= 1
            raise GarminConnectAuthenticationError("login rejected")
        return ("oauth1", "oauth2")

    def get_workouts(self, start=0, limit=100):
        if type(self).call_auth_failures > 0:
            type(self).call_auth_failures -= 1
            raise GarminConnectAuthenticationError("session expired")
        return [{"workoutId": 1}]


def _client() -> GarminClient:
    _FakeGarmin.reset()
    client_mod.Garmin = _FakeGarmin
    return GarminClient(email="e", password="p", tokenstore_path="/nonexistent")


def test_no_auth_at_construction():
    c = _client()
    assert _FakeGarmin.logins == 0, "constructing the client must not hit Garmin"
    assert c._api is None


def test_first_call_authenticates_once():
    c = _client()

    async def run():
        for _ in range(3):
            # cache=False so every call reaches the API layer
            await c.call("get_workouts", cache=False)

    asyncio.run(run())
    assert _FakeGarmin.logins == 1, f"expected 1 login, got {_FakeGarmin.logins}"


def test_concurrent_first_calls_authenticate_once():
    """A cold start bursts several tool calls at once; only one may log in."""
    c = _client()

    async def run():
        await asyncio.gather(*[c.call("get_workouts", cache=False) for _ in range(8)])

    asyncio.run(run())
    assert _FakeGarmin.logins == 1, (
        f"login stampede: {_FakeGarmin.logins} logins for 8 concurrent calls"
    )


def test_dead_session_reauthenticates_once_and_succeeds():
    c = _client()
    _FakeGarmin.call_auth_failures = 1  # first call 401s, then recovers

    async def run():
        return await c.call("get_workouts", cache=False)

    result = asyncio.run(run())
    assert result == [{"workoutId": 1}], result
    assert _FakeGarmin.logins == 2, (
        f"expected initial login + one re-auth, got {_FakeGarmin.logins}"
    )


def test_failed_reauth_raises_and_does_not_loop():
    c = _client()
    _FakeGarmin.call_auth_failures = 99   # never recovers
    _FakeGarmin.login_failures = 0

    async def run():
        await c.call("get_workouts", cache=False)

    try:
        asyncio.run(run())
    except GarminConnectAuthenticationError:
        pass
    else:
        raise AssertionError("a permanently dead session must raise, not hang")
    assert _FakeGarmin.logins <= 2, (
        f"re-auth loop: {_FakeGarmin.logins} logins for one failing call"
    )


def test_readiness_state_is_tracked():
    c = _client()
    assert c.auth_status()["garmin"] == "never"

    async def run():
        await c.call("get_workouts", cache=False)

    asyncio.run(run())
    st = c.auth_status()
    assert st["garmin"] == "authenticated", st
    assert st["authenticated_at"] is not None
    assert st["last_error"] is None


def test_readiness_reports_auth_failure():
    c = _client()
    _FakeGarmin.login_failures = 99

    async def run():
        await c.call("get_workouts", cache=False)

    try:
        asyncio.run(run())
    except GarminConnectAuthenticationError:
        pass
    st = c.auth_status()
    assert st["garmin"] == "error", st
    assert st["last_error"], "a failed login must be reported"


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
