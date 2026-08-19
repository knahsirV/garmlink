"""Swimming-specific tools for Garmin MCP."""

from __future__ import annotations

import asyncio

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL
from ..deps import get_garmin

mcp = FastMCP("swimming")


@mcp.tool()
async def get_swim_activities(start_date: str, end_date: str, ctx: Context) -> list:
    """
    Use when user asks about swim sessions or swim history. Returns swim sessions with SWOLF, pace, stroke rate.

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date:   End date in YYYY-MM-DD format (inclusive).
    """
    client = get_garmin(ctx)
    return await client.call("get_activities_by_date", start_date, end_date, "lap_swimming", ttl=ACTIVITY_TTL)


@mcp.tool()
async def get_swim_activity_detail(activity_id: int, ctx: Context) -> dict:
    """
    Get detailed swim session breakdown: per-length stroke count, SWOLF, pace, stroke type.
    Use when user wants to analyze a specific swim workout in detail.

    Args:
        activity_id: The Garmin activity ID (get it from get_swim_activities first).
    """
    client = get_garmin(ctx)
    activity, splits = await asyncio.gather(
        client.call("get_activity", activity_id, ttl=ACTIVITY_TTL),
        client.call("get_activity_splits", activity_id, ttl=ACTIVITY_TTL),
    )
    return {"summary": activity, "splits": splits}
