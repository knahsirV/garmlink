"""Strength training tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL
from ..deps import get_garmin
from ..sports import pounds

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

    Weights are converted to pounds. Garmin stores them in GRAMS, so a 50lb curl
    arrives as 22687 — a number that looks like a plausible weight in no unit at
    all and quietly wrecks any volume total computed from it.

    Two things this data does not support, which should be said rather than
    worked around. Garmin's auto-detection records no exercise NAME, only a
    coarse category: a lat pulldown logs as PULL_UP and an incline machine press
    as BENCH_PRESS. Per-exercise progression is therefore not trackable unless
    the exercises were set explicitly in the Garmin workout — session volume is.
    And whether a dumbbell set records one weight or two is the athlete's
    logging convention, not something in the payload; the training plan states
    which convention is in use.

    Args:
        activity_id: The Garmin activity ID (get it from get_strength_activities first).
    """
    client = get_garmin(ctx)
    result = await client.call(
        "get_activity_exercise_sets", activity_id, ttl=ACTIVITY_TTL
    )
    if not isinstance(result, dict):
        return result

    volume = 0.0
    for entry in result.get("exerciseSets") or []:
        if not isinstance(entry, dict):
            continue
        weight_lb = pounds(entry.get("weight"))
        if weight_lb is not None:
            entry["weight_lb"] = weight_lb
        reps = entry.get("repetitionCount") or 0
        if weight_lb and reps and entry.get("setType") == "ACTIVE":
            volume += weight_lb * reps
    if volume:
        result["session_volume_lb"] = round(volume)
    return result
