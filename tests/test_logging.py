"""The server logs enough to debug a production failure, and nothing sensitive.

Before this, `src/` contained zero logging calls: a failing tool, a rejected
token, and a four-second rate-limit backoff all left identical traces — none.
Cloud Run's own request log cannot fill the gap, because every MCP call is a
`POST /mcp`, so it records 45 different tools as one indistinguishable route.

Two things are load-bearing here and are each pinned by a test below:

  * The tool-call middleware is registered on the *parent* server, but every
    tool lives on a mounted sub-server. Mounted servers famously do not inherit
    the parent's lifespan context (see test_tool_dispatch.py), so "the parent's
    middleware sees mounted tools" is verified, not assumed.
  * Logging observes; it must never alter behaviour. A tool that raises must
    still raise, with the original exception.

Runs standalone (`python tests/test_logging.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp import Client, FastMCP  # noqa: E402
from garminconnect import (  # noqa: E402
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import garmlink.client as client_mod  # noqa: E402
from garmlink.client import GarminClient  # noqa: E402
import garmlink.server as server_mod  # noqa: E402
from garmlink.server import BearerAuthMiddleware  # noqa: E402

from garmlink.logs import (  # noqa: E402
    JsonFormatter,
    TextFormatter,
    ToolCallLoggingMiddleware,
    cache_summary,
    record_cache,
    redact,
    safe_error,
    track_cache,
)


class _CapturedLogs:
    """Collects `garmlink` LogRecords for the duration of a with-block."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []

    def __enter__(self):
        self._handler = logging.Handler()
        self._handler.emit = self.records.append  # type: ignore[method-assign]
        self._logger = logging.getLogger("garmlink")
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)
        return False

    def with_message(self, msg: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == msg]


@contextmanager
def _quiet_fastmcp():
    """Silence FastMCP's own error reporting.

    Two tests below raise from a tool on purpose; without this FastMCP prints a
    traceback panel and the suite's output stops being a clean pass/fail list.
    """
    fastmcp_logger = logging.getLogger("fastmcp")
    previous = fastmcp_logger.level
    fastmcp_logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        fastmcp_logger.setLevel(previous)


def _server_with_mounted_tool() -> FastMCP:
    """A parent server whose only tool lives on a mounted sub-server.

    This mirrors the real topology: garmlink registers no tools directly, it
    mounts eleven routers that own all 45 of them.
    """
    child = FastMCP("child")

    @child.tool()
    async def echo(word: str) -> dict:
        return {"word": word}

    @child.tool()
    async def explode(word: str) -> dict:
        raise ValueError("tool blew up")

    @child.tool()
    async def fan_out(word: str) -> dict:
        # Stands in for a range tool: several cache lookups under one tool call,
        # some concurrent, which is why a single hit/miss flag will not do.
        async def one(hit: bool):
            record_cache(hit=hit)

        await asyncio.gather(one(True), one(False), one(False))
        return {"word": word}

    parent = FastMCP("parent")
    parent.mount(child)
    parent.add_middleware(ToolCallLoggingMiddleware())
    return parent


def _record(level: int = logging.INFO, msg: str = "tool.call", **fields):
    """Build a real LogRecord the way the logging module would."""
    record = logging.LogRecord(
        name="garmlink", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    record.fields = fields
    return record


def test_redact_strips_token_shaped_runs():
    dirty = "login failed for token abcdefghijklmnopqrstuvwxyz123456"
    assert "abcdefghijklmnopqrstuvwxyz123456" not in redact(dirty)
    assert "[redacted]" in redact(dirty)


def test_redact_keeps_ordinary_words():
    # Over-redaction would make the logs useless, so short words must survive.
    assert redact("garmin login failed after 3 attempts") == (
        "garmin login failed after 3 attempts"
    )


def test_safe_error_names_the_exception_type():
    msg = safe_error(ValueError("bad date"))
    assert msg.startswith("ValueError:"), msg
    assert "bad date" in msg


def test_safe_error_redacts_token_material():
    leak = RuntimeError("upstream said Bearer abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in safe_error(leak)


def test_json_formatter_emits_cloud_logging_severity():
    # Cloud Run parses a top-level "severity" key out of JSON on stdout; without
    # it every line is an untyped INFO and filtering by error is impossible.
    payload = json.loads(JsonFormatter().format(_record(level=logging.WARNING)))
    assert payload["severity"] == "WARNING", payload
    assert payload["message"] == "tool.call", payload


def test_json_formatter_promotes_extra_fields():
    payload = json.loads(JsonFormatter().format(_record(name="get_devices", dur_ms=12)))
    assert payload["name"] == "get_devices", payload
    assert payload["dur_ms"] == 12, payload


def test_json_formatter_survives_unserialisable_fields():
    # A field that json cannot encode must not take the log line down with it.
    payload = json.loads(JsonFormatter().format(_record(obj=object())))
    assert payload["message"] == "tool.call", payload


def test_text_formatter_renders_key_value_pairs():
    line = TextFormatter().format(_record(name="get_devices", dur_ms=12))
    assert "tool.call" in line
    assert "name=get_devices" in line
    assert "dur_ms=12" in line


def test_middleware_logs_tool_calls_on_mounted_servers():
    # The middleware is registered on the parent, but every real tool lives on a
    # mounted sub-server. Mounted servers do not inherit the parent's lifespan
    # context (test_tool_dispatch.py), so parent-level coverage of mounted tools
    # is verified here rather than assumed.
    server = _server_with_mounted_tool()

    async def go():
        async with Client(server) as c:
            await c.call_tool("echo", {"word": "hello"})

    with _CapturedLogs() as logs:
        asyncio.run(go())

    calls = logs.with_message("tool.call")
    assert calls, "no tool.call record — parent middleware never saw the mounted tool"
    assert calls[0].fields["name"] == "echo", calls[0].fields


def test_tool_call_log_records_arguments_and_duration():
    server = _server_with_mounted_tool()

    async def go():
        async with Client(server) as c:
            await c.call_tool("echo", {"word": "hello"})

    with _CapturedLogs() as logs:
        asyncio.run(go())

    fields = logs.with_message("tool.call")[0].fields
    assert fields["args"] == {"word": "hello"}, fields
    assert fields["outcome"] == "ok", fields
    assert isinstance(fields["dur_ms"], (int, float)), fields


def test_tool_call_log_redacts_token_shaped_arguments():
    server = _server_with_mounted_tool()
    secret = "abcdefghijklmnopqrstuvwxyz123456"

    async def go():
        async with Client(server) as c:
            await c.call_tool("echo", {"word": secret})

    with _CapturedLogs() as logs:
        asyncio.run(go())

    assert secret not in str(logs.with_message("tool.call")[0].fields)


def test_cache_summary_counts_hits_and_misses():
    with track_cache():
        record_cache(hit=True)
        record_cache(hit=False)
        record_cache(hit=False)
        assert cache_summary() == "1h/2m"


def test_cache_summary_is_none_when_nothing_was_looked_up():
    # Tools that never touch the cache should not grow a meaningless "0h/0m".
    with track_cache():
        assert cache_summary() is None


def test_cache_counts_survive_concurrent_fan_out():
    # Range tools fan out with asyncio.gather. Each task gets a *copy* of the
    # context, so the counter has to be a mutable object they all share rather
    # than a value that each task would increment privately and lose.
    server = _server_with_mounted_tool()

    async def go():
        async with Client(server) as c:
            await c.call_tool("fan_out", {"word": "hi"})

    with _CapturedLogs() as logs:
        asyncio.run(go())

    fields = logs.with_message("tool.call")[0].fields
    assert fields["cache"] == "1h/2m", fields


def test_tool_that_never_touches_the_cache_logs_no_cache_field():
    server = _server_with_mounted_tool()

    async def go():
        async with Client(server) as c:
            await c.call_tool("echo", {"word": "hi"})

    with _CapturedLogs() as logs:
        asyncio.run(go())

    assert "cache" not in logs.with_message("tool.call")[0].fields


def test_failing_tool_logs_an_error():
    server = _server_with_mounted_tool()

    async def go():
        async with Client(server) as c:
            try:
                await c.call_tool("explode", {"word": "hi"})
            except Exception:
                pass

    with _CapturedLogs() as logs, _quiet_fastmcp():
        asyncio.run(go())

    calls = logs.with_message("tool.call")
    assert calls, "a failing tool must still produce a tool.call record"
    assert calls[0].levelno == logging.ERROR, calls[0].levelname
    assert calls[0].fields["outcome"] == "error", calls[0].fields


def test_logging_never_swallows_a_tool_failure():
    # Logging observes; it must not change what the caller sees. A middleware
    # that logged an exception and returned normally would hide every failure.
    server = _server_with_mounted_tool()
    raised = []

    async def go():
        async with Client(server) as c:
            try:
                await c.call_tool("explode", {"word": "hi"})
            except Exception as exc:
                raised.append(exc)

    with _CapturedLogs(), _quiet_fastmcp():
        asyncio.run(go())

    assert raised, "the tool error must still reach the caller"
    assert "blew up" in str(raised[0]), raised[0]


# ---------------------------------------------------------------------------
# GarminClient diagnostics
# ---------------------------------------------------------------------------

class _FakeGarmin:
    """Stands in for garminconnect.Garmin with scriptable failures."""

    rate_limits = 0      # how many of the next calls should be rate-limited
    session_deaths = 0   # how many of the next calls should 401

    def __init__(self, **kwargs):
        pass

    @classmethod
    def reset(cls):
        cls.rate_limits = 0
        cls.session_deaths = 0

    def login(self, tokenstore=None):
        return ("oauth1", "oauth2")

    def get_stats(self, day):
        if type(self).session_deaths > 0:
            type(self).session_deaths -= 1
            raise GarminConnectAuthenticationError("session expired")
        if type(self).rate_limits > 0:
            type(self).rate_limits -= 1
            raise GarminConnectTooManyRequestsError("slow down")
        return {"day": day}


def _fake_client(monkeypatched_delay: float = 0.001) -> GarminClient:
    """A GarminClient wired to _FakeGarmin.

    Patches the *constructor*, per the trap documented in test_tool_dispatch.py:
    real tokens sit in ~/.garminconnect, so a client built any other way reaches
    the live Garmin API from the test suite.
    """
    _FakeGarmin.reset()
    client_mod.Garmin = _FakeGarmin  # type: ignore[assignment]
    client_mod._BASE_DELAY = monkeypatched_delay  # keep backoff tests fast
    return GarminClient(email="x@y.z", password="pw", tokenstore_path="/nonexistent")


def test_garmin_login_is_logged():
    client = _fake_client()

    with _CapturedLogs() as logs:
        asyncio.run(client.call("get_stats", "2026-08-20"))

    logins = logs.with_message("garmin.login")
    assert logins, "a Garmin login must leave a trace"
    assert logins[-1].fields["outcome"] == "ok", logins[-1].fields
    assert "dur_ms" in logins[-1].fields, logins[-1].fields


def test_rate_limit_backoff_is_logged():
    # Today a rate-limited call stalls for seconds in complete silence; that is
    # indistinguishable from a hang when reading production logs.
    client = _fake_client()
    _FakeGarmin.rate_limits = 2

    with _CapturedLogs() as logs:
        asyncio.run(client.call("get_stats", "2026-08-20"))

    retries = logs.with_message("garmin.retry")
    assert len(retries) == 2, [r.fields for r in retries]
    assert retries[0].fields["method"] == "get_stats", retries[0].fields
    assert retries[0].fields["attempt"] == 1, retries[0].fields


def test_session_death_and_reauth_is_logged():
    client = _fake_client()
    _FakeGarmin.session_deaths = 1

    with _CapturedLogs() as logs:
        asyncio.run(client.call("get_stats", "2026-08-20"))

    assert logs.with_message("garmin.reauth"), "a dead session must be recorded"


def test_client_records_cache_hits_and_misses():
    client = _fake_client()

    async def go():
        with track_cache():
            await client.call("get_stats", "2026-08-20")
            await client.call("get_stats", "2026-08-20")
            return cache_summary()

    assert asyncio.run(go()) == "1h/1m"


def test_failed_login_is_logged_without_leaking_credentials():
    client = _fake_client()
    secret = "abcdefghijklmnopqrstuvwxyz123456"

    def boom(self, tokenstore=None):
        raise GarminConnectAuthenticationError(f"rejected token {secret}")

    original_login = _FakeGarmin.login
    _FakeGarmin.login = boom  # type: ignore[assignment]
    try:
        with _CapturedLogs() as logs:
            try:
                asyncio.run(client.call("get_stats", "2026-08-20"))
            except GarminConnectAuthenticationError:
                pass

        failures = [r for r in logs.with_message("garmin.login")
                    if r.fields.get("outcome") == "error"]
        assert failures, "a failed login must be logged"
        assert secret not in str(failures[0].fields), failures[0].fields
    finally:
        _FakeGarmin.login = original_login  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Bearer-auth diagnostics
# ---------------------------------------------------------------------------

def _auth_test_client(token: str) -> TestClient:
    app = Starlette(routes=[
        Route("/mcp", lambda request: JSONResponse({"ok": True})),
        Route("/health", lambda request: JSONResponse({"status": "ok"})),
    ])
    app.add_middleware(BearerAuthMiddleware, token=token)
    return TestClient(app)


def test_rejected_token_is_logged():
    client = _auth_test_client("t" * 40)

    with _CapturedLogs() as logs:
        assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401

    rejects = logs.with_message("auth.reject")
    assert rejects, "a 401 must be explained in the logs"
    assert rejects[0].levelno == logging.WARNING, rejects[0].levelname
    assert rejects[0].fields["path"] == "/mcp", rejects[0].fields


def test_rejection_log_never_contains_the_presented_token():
    # Logging what someone presented would put a near-miss of the real secret,
    # or someone else's credential, into the log stream permanently.
    client = _auth_test_client("t" * 40)
    presented = "abcdefghijklmnopqrstuvwxyz123456"

    with _CapturedLogs() as logs:
        client.get("/mcp", headers={"Authorization": f"Bearer {presented}"})

    assert presented not in str(logs.with_message("auth.reject")[0].fields)


def test_successful_requests_are_not_logged_as_rejections():
    token = "t" * 40
    client = _auth_test_client(token)

    with _CapturedLogs() as logs:
        assert client.get("/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    assert not logs.with_message("auth.reject")


def test_health_check_is_not_logged_as_a_rejection():
    # /health is polled constantly by the platform; logging it would drown
    # everything else.
    client = _auth_test_client("t" * 40)

    with _CapturedLogs() as logs:
        assert client.get("/health").status_code == 200

    assert not logs.with_message("auth.reject")


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------

def test_server_registers_the_tool_logging_middleware():
    # The middleware existing is useless if nothing installs it on the real app.
    installed = [m for m in server_mod.mcp.middleware
                 if isinstance(m, ToolCallLoggingMiddleware)]
    assert installed, "the real server must install ToolCallLoggingMiddleware"


def test_startup_is_logged_with_the_served_surface():
    # A cold start that silently serves the wrong number of tools is exactly the
    # failure this project has already hit once.
    class _FakeGarminClient:
        def close(self):
            pass

    # Patch the *constructor*: the lifespan builds a real GarminClient, and with
    # real tokens in ~/.garminconnect that means the suite hits live Garmin.
    original = server_mod.GarminClient
    server_mod.GarminClient = lambda **kw: _FakeGarminClient()  # type: ignore[assignment]

    async def go():
        async with server_mod.lifespan(server_mod.mcp):
            pass

    try:
        with _CapturedLogs() as logs:
            asyncio.run(go())
    finally:
        server_mod.GarminClient = original  # type: ignore[assignment]

    starts = logs.with_message("startup")
    assert starts, "startup must leave a trace"
    assert starts[0].fields["tools"] == 45, starts[0].fields
    assert starts[0].fields["prompts"] == 4, starts[0].fields


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
