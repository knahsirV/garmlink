"""Dependency injection helpers for Garmin MCP."""

from __future__ import annotations

from .client import GarminClient


def get_garmin(ctx) -> GarminClient:
    """Get the GarminClient from the FastMCP request context lifespan state."""
    return ctx.lifespan_context["garmin"]
