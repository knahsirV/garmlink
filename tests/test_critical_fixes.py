"""Behavioural tests for the fixes to the six critical review findings.

Runs standalone (`python tests/test_critical_fixes.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from garmlink.cache import TTLCache  # noqa: E402
from garmlink.client import GarminClient  # noqa: E402


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
