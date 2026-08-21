"""Dependency injection helpers for Garmin MCP."""

from __future__ import annotations

from typing import Any

from .client import GarminClient

# The tool modules are separate FastMCP servers mounted onto the main app, and a
# mounted server's `lifespan_context` is its own — an empty dict — not the
# parent's. So the client cannot be read off the context the tools receive; the
# server lifespan registers it here instead. One GarminClient per process.
_client: GarminClient | None = None


def set_client(client: GarminClient | None) -> None:
    """Register (or clear) the process-wide client. Called by the lifespan."""
    global _client
    _client = client


def get_garmin(ctx: Any = None) -> GarminClient:
    """Return the GarminClient for the current request.

    Prefers the lifespan context when it actually carries the client, so this
    keeps working if the tools are ever served from the main app directly, and
    falls back to the registered client for the mounted case.
    """
    lifespan_context = getattr(ctx, "lifespan_context", None)
    if isinstance(lifespan_context, dict) and "garmin" in lifespan_context:
        return lifespan_context["garmin"]

    if _client is None:
        raise RuntimeError(
            "GarminClient is not initialised — the server lifespan has not run"
        )
    return _client


def get_garmin_or_none() -> GarminClient | None:
    """Registered client, or None before the lifespan has run. For /readyz."""
    return _client
