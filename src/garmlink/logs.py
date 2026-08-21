"""Structured logging for the Garmin MCP server.

Named `logs` rather than `logging` so that `import logging` inside this package
keeps meaning the standard library.

Two formatters, chosen by environment. On Cloud Run (`K_SERVICE` is set in every
container) we emit one JSON object per line: the platform parses it and lifts
`severity` into the log viewer, which is what makes "show me only errors" work.
Locally that would be unreadable, so a plain key=value line is used instead.

Everything logged goes through `redact()`. Personal health data is the whole
point of this server, and a Garmin error message can echo credential material,
so the redaction that already guarded /readyz now guards the log stream too.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from fastmcp.server.middleware import Middleware

__all__ = [
    "JsonFormatter",
    "TextFormatter",
    "ToolCallLoggingMiddleware",
    "cache_summary",
    "logger",
    "record_cache",
    "redact",
    "safe_error",
    "setup_logging",
    "track_cache",
]

logger = logging.getLogger("garmlink")

# Long unbroken token-ish runs are stripped from anything we surface, so a
# Garmin error that echoes a credential cannot leak through /readyz or the logs.
_TOKENISH = re.compile(r"[A-Za-z0-9_\-\.]{20,}")

# Attributes present on every LogRecord; anything else was attached by us.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def redact(text: str) -> str:
    """Replace token-shaped runs with a placeholder."""
    return _TOKENISH.sub("[redacted]", text)


def safe_error(exc: BaseException) -> str:
    """Render an exception for reporting without leaking token material."""
    return f"{type(exc).__name__}: {redact(str(exc))[:200]}"


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    """Structured fields attached to a record via `extra={"fields": {...}}`."""
    fields = getattr(record, "fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, shaped for Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # Cloud Logging reads this key to type the entry. Python's level
            # names (DEBUG/INFO/WARNING/ERROR/CRITICAL) are already its vocabulary.
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record),
        }
        payload.update(_fields(record))
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = safe_error(record.exc_info[1])
        # A field we cannot encode must not cost us the line: `default=repr`
        # stringifies the offender instead of raising into the logging handler.
        return json.dumps(payload, default=repr)


class TextFormatter(logging.Formatter):
    """Human-readable `LEVEL message key=value` for local development."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [f"{record.levelname:<7}", record.getMessage()]
        parts.extend(f"{k}={v}" for k, v in _fields(record).items())
        line = " ".join(parts)
        if record.exc_info and record.exc_info[1] is not None:
            line += f" exception={safe_error(record.exc_info[1])}"
        return line


# ---------------------------------------------------------------------------
# Per-tool-call cache accounting
# ---------------------------------------------------------------------------

# A single tool call can trigger many cache lookups: the range helpers in
# ranges.py fan out one Garmin call per day, concurrently. So "was this a cache
# hit" has no single answer, and the log line carries counts instead.
#
# The ContextVar holds a *mutable dict*, deliberately. asyncio.gather gives each
# task a copy of the context, so rebinding the variable inside a task would be
# invisible to the caller — but mutating the shared dict it points at is not.
_cache_counts: ContextVar[dict[str, int] | None] = ContextVar(
    "garmlink_cache_counts", default=None
)


@contextmanager
def track_cache() -> Iterator[None]:
    """Scope a fresh set of cache counters to one tool call."""
    token = _cache_counts.set({"hit": 0, "miss": 0})
    try:
        yield
    finally:
        _cache_counts.reset(token)


def record_cache(*, hit: bool) -> None:
    """Record one cache lookup. A no-op outside a `track_cache()` scope."""
    counts = _cache_counts.get()
    if counts is not None:
        counts["hit" if hit else "miss"] += 1


def cache_summary() -> str | None:
    """Render the counters as `<hits>h/<misses>m`, or None if nothing looked up."""
    counts = _cache_counts.get()
    if not counts or not (counts["hit"] or counts["miss"]):
        return None
    return f"{counts['hit']}h/{counts['miss']}m"


_MAX_ARG_CHARS = 200


def _redact_value(value: Any) -> Any:
    """Make one argument value safe and bounded for the log line."""
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    text = value if isinstance(value, str) else repr(value)
    return redact(text)[:_MAX_ARG_CHARS]


def _redact_args(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: _redact_value(v) for k, v in arguments.items()}


class ToolCallLoggingMiddleware(Middleware):
    """One log line per MCP tool call.

    Registered on the parent server, which is enough: `FastMCP.call_tool` runs
    the middleware chain *before* resolving the tool, and mount aggregation
    happens during resolution — so calls into mounted sub-servers pass through
    here. (Verified by test_logging.py; mounted servers do not inherit the
    parent's lifespan, so this is not something to take on faith.)

    This is the only place tool activity is visible at all: to Cloud Run's
    request log all 45 tools are the same `POST /mcp`.

    Arguments are logged, redacted and truncated; **results never are**. Results
    are the health data this server exists to protect, and they are large.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = getattr(context.message, "name", "unknown")
        args = _redact_args(getattr(context.message, "arguments", None) or {})
        started = time.perf_counter()

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started) * 1000, 1)

        def fields(outcome: str, **extra: Any) -> dict[str, Any]:
            out = {
                "name": name, "args": args, "outcome": outcome,
                "dur_ms": elapsed_ms(),
            }
            summary = cache_summary()
            if summary is not None:
                out["cache"] = summary
            out.update(extra)
            return out

        with track_cache():
            try:
                result = await call_next(context)
            except Exception as exc:
                logger.error(
                    "tool.call",
                    extra={"fields": fields("error", error=safe_error(exc))},
                )
                # Re-raised unchanged: logging observes, never alters behaviour.
                raise

            # A tool can also report failure in-band rather than by raising,
            # and that path must not be logged as a success.
            outcome = "error" if getattr(result, "is_error", False) else "ok"
            logger.log(
                logging.ERROR if outcome == "error" else logging.INFO,
                "tool.call",
                extra={"fields": fields(outcome)},
            )
            return result


def setup_logging() -> None:
    """Install a single stdout handler on the `garmlink` logger.

    Idempotent: repeated calls (tests, reloads) replace the handler rather than
    stacking duplicates that would print every line twice.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    on_cloud_run = bool(os.getenv("K_SERVICE"))
    fmt = os.getenv("LOG_FORMAT", "json" if on_cloud_run else "text").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    logger = logging.getLogger("garmlink")
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level, logging.INFO))
    # Ours is the only handler that should render these records; uvicorn's root
    # handler would otherwise print an unformatted copy of every line.
    logger.propagate = False
