"""Strength training tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL
from ..deps import get_garmin

mcp = FastMCP("strength")


@mcp.tool()
async def get_strength_activities(start_date: str, end_date: str, ctx: Context) -> list:
    """
    Use when user asks about strength or weight training sessions.

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date:   End date in YYYY-MM-DD format (inclusive).
    """
    client = get_garmin(ctx)
    # strength_training is a sub-type of fitness_equipment, and the API rejects
    # sub-types outright, so fetch the parent type and narrow it here.
    activities = await client.call(
        "get_activities_by_date", start_date, end_date, "fitness_equipment",
        ttl=ACTIVITY_TTL,
    )
    return [
        a for a in (activities or [])
        if "strength" in (a.get("activityType", {}) or {}).get("typeKey", "")
    ]


@mcp.tool()
async def get_strength_sets(activity_id: int, ctx: Context) -> dict:
    """
    Use when user wants sets, reps, weight, and exercise names from a strength session.

    Args:
        activity_id: The Garmin activity ID (get it from get_strength_activities first).
    """
    client = get_garmin(ctx)
    return await client.call("get_activity_exercise_sets", activity_id, ttl=ACTIVITY_TTL)
