"""Running-specific tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import HEALTH_TTL
from ..deps import get_garmin

mcp = FastMCP("running")


@mcp.tool()
async def get_race_predictions(ctx: Context) -> dict:
    """
    Use when user asks about predicted race times or running fitness projections.
    Returns: Garmin's predicted race times (5K, 10K, half marathon, marathon).
    """
    client = get_garmin(ctx)
    return await client.call("get_race_predictions", ttl=HEALTH_TTL)


@mcp.tool()
async def get_lactate_threshold(ctx: Context) -> dict:
    """
    Use when user asks about lactate threshold, threshold pace, or threshold heart rate.
    Returns: Lactate threshold heart rate and pace.
    """
    client = get_garmin(ctx)
    return await client.call("get_lactate_threshold", ttl=HEALTH_TTL)


@mcp.tool()
async def get_running_tolerance(ctx: Context) -> dict:
    """
    Use when user asks about running readiness, injury risk, or whether they can handle more run volume.
    Returns: Garmin's running load tolerance / injury risk assessment.
    """
    client = get_garmin(ctx)
    return await client.call("get_running_tolerance", ttl=HEALTH_TTL)


@mcp.tool()
async def get_running_dynamics(activity_id: int, ctx: Context) -> dict:
    """
    Extract running dynamics from a specific run activity: cadence, stride length,
    ground contact time, vertical oscillation, vertical ratio.
    Use when user asks about running form or biomechanics for a specific run.
    activity_id: The Garmin activity ID (get it from get_activities first).
    Returns: dict with available dynamics fields extracted from the activity.
    """
    client = get_garmin(ctx)
    activity = await client.call("get_activity", activity_id, ttl=HEALTH_TTL)
    # Extract running dynamics from the activity data
    # These fields are typically under activity["summaryDTO"] or top-level
    dynamics = {}
    for field in [
        "averageRunningCadenceInStepsPerMinute",
        "maxRunningCadenceInStepsPerMinute",
        "averageStrideLength",
        "averageVerticalOscillation",
        "averageGroundContactTime",
        "averageVerticalRatio",
        "averageGroundContactBalance",
    ]:
        summary = activity.get("summaryDTO", activity)
        if field in summary:
            dynamics[field] = summary[field]
    dynamics["activityId"] = activity_id
    dynamics["activityName"] = activity.get("activityName", "")
    return dynamics
