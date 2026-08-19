"""Activity tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL
from ..deps import get_garmin

mcp = FastMCP("garmin-activities")


@mcp.tool()
async def get_activities(ctx: Context, limit: int = 10, start: int = 0) -> dict | list:
    """
    Return a paginated list of recent activities (runs, rides, swims, etc.)
    with summary metrics such as distance, duration, pace, and heart rate.
    Use this when the user asks about recent workouts or wants to browse their
    activity history.

    Args:
        limit: Maximum number of activities to return (default 10).
        start: Zero-based offset for pagination (default 0).
    """
    client = get_garmin(ctx)
    return await client.call("get_activities", start, limit, ttl=ACTIVITY_TTL)


@mcp.tool()
async def get_activity(activity_id: int, ctx: Context) -> dict:
    """
    Return full details for a single activity by its ID, including splits,
    heart rate zones, pace, elevation, and all recorded metrics.  Use this
    after get_activities to drill into a specific workout.

    Args:
        activity_id: Numeric Garmin activity ID.
    """
    client = get_garmin(ctx)
    return await client.call("get_activity", activity_id, ttl=ACTIVITY_TTL)


@mcp.tool()
async def get_activity_splits(activity_id: int, ctx: Context) -> dict:
    """
    Return lap/split data for a single activity, showing per-lap pace, heart
    rate, cadence, and other metrics.  Use this when the user wants to analyse
    how their performance varied across laps or segments of a workout.

    Args:
        activity_id: Numeric Garmin activity ID.
    """
    client = get_garmin(ctx)
    return await client.call("get_activity_splits", activity_id, ttl=ACTIVITY_TTL)


@mcp.tool()
async def set_activity_name(activity_id: int, name: str, ctx: Context) -> dict:
    """
    Rename an existing activity on Garmin Connect.  After a successful rename
    the cached activity list is invalidated so subsequent calls reflect the
    updated name.  Use this when the user wants to give a workout a custom
    title.

    Args:
        activity_id: Numeric Garmin activity ID.
        name:        New display name for the activity.
    """
    client = get_garmin(ctx)
    result = await client.call("set_activity_name", activity_id, name, cache=False)
    client.invalidate("get_activities")
    return result


@mcp.tool()
async def create_manual_activity(
    name: str,
    activity_type: str,
    start_time: str,
    duration_seconds: int,
    ctx: Context,
    distance_meters: float | None = None,
    timezone: str = "UTC",
) -> dict:
    """
    Create a manual (non-device) activity on Garmin Connect.  Use this when
    the user wants to log a workout that was not recorded by a Garmin device,
    such as a gym session or a forgotten run.

    Args:
        name:             Display name for the activity.
        activity_type:    Garmin activity type string (e.g. "running",
                          "cycling", "strength_training").
        start_time:       Activity start time in ISO-8601 format
                          (YYYY-MM-DDTHH:MM:SS).
        duration_seconds: Duration of the activity in seconds.
        distance_meters:  Optional distance in metres.
        timezone:         IANA timezone name for start_time (e.g.
                          "America/Chicago"). Defaults to UTC.
    """
    client = get_garmin(ctx)
    # garminconnect wants (start_datetime, time_zone, type_key, distance_km,
    # duration_min, activity_name) — kilometres and minutes, not metres and
    # seconds.
    distance_km = (distance_meters or 0) / 1000
    duration_min = round(duration_seconds / 60)
    result = await client.call(
        "create_manual_activity",
        start_time,
        timezone,
        activity_type,
        distance_km,
        duration_min,
        name,
        cache=False,
    )
    client.invalidate("get_activities")
    return result
