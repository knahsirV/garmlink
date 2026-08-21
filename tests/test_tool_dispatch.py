"""Tools must actually reach the GarminClient when dispatched.

The tool modules are separate FastMCP servers mounted onto the main app. A
mounted server's `lifespan_context` is its own empty dict, not the parent's, so
reading the client off the context raised KeyError('garmin') for every one of
the 45 tools — the server could serve /health and list tools while no tool could
run. Nothing else in the suite exercises real dispatch, so nothing caught it.

Runs standalone (`python tests/test_tool_dispatch.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp import Client  # noqa: E402

import garmlink.deps as deps  # noqa: E402


class _FakeClient:
    """Records dispatched calls and returns plausibly-shaped payloads."""

    def __init__(self):
        self.calls: list[str] = []

    async def call(self, method_name, *args, ttl=None, cache=True, **kwargs):
        self.calls.append(method_name)
        # Mirror the real shapes: some endpoints return lists, some dicts.
        if method_name in {
            "get_devices", "get_workouts", "get_activities",
            "get_activities_by_date", "get_steps_data", "get_body_battery",
        }:
            return [{"id": 1}]
        return {"ok": True, "method": method_name}

    def invalidate(self, method_name):
        pass

    def close(self):
        pass


# One tool per mounted sub-server, so a mount that stops forwarding is caught.
TOOLS = [
    ("get_user_profile", {}),                                   # profile
    ("get_daily_summary", {"date": "2026-08-20"}),              # daily
    ("get_activities", {"limit": 5}),                           # activities
    ("get_training_status", {"date": "2026-08-20"}),            # training
    ("get_race_predictions", {}),                               # running
    ("get_cycling_ftp", {}),                                    # cycling
    ("get_swim_activities", {"start_date": "2026-08-01",
                             "end_date": "2026-08-20"}),        # swimming
    ("get_strength_activities", {"start_date": "2026-08-01",
                                 "end_date": "2026-08-20"}),    # strength
    ("get_workouts", {}),                                       # workouts
    ("get_wellness_snapshot", {"date_str": "2026-08-20"}),      # insights
]


def test_every_mounted_router_dispatches():
    import garmlink.server as server

    fake = _FakeClient()
    original = server.GarminClient
    # The lifespan constructs a real GarminClient and registers it, which would
    # overwrite anything set here — and with real tokens on disk that means the
    # suite would hit the live Garmin API. Patch the constructor instead.
    server.GarminClient = lambda **kwargs: fake

    async def run():
        failures = []
        async with Client(server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
            assert len(names) >= 40, f"expected the full tool set, got {len(names)}"
            for tool, args in TOOLS:
                assert tool in names, f"{tool} is not exposed"
                try:
                    await c.call_tool(tool, args)
                except Exception as exc:
                    failures.append(f"{tool}: {type(exc).__name__}: {exc}")
        return failures

    try:
        failures = asyncio.run(run())
    finally:
        server.GarminClient = original
        deps.set_client(None)

    assert not failures, "tools failed to dispatch:\n  " + "\n  ".join(failures)
    assert fake.calls, "no tool reached the GarminClient"


def test_missing_client_gives_a_clear_error():
    """Before the lifespan runs there is no client; say so, don't KeyError."""
    deps.set_client(None)
    try:
        deps.get_garmin(None)
    except RuntimeError as exc:
        assert "lifespan" in str(exc).lower(), exc
    else:
        raise AssertionError("expected a RuntimeError naming the cause")


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
