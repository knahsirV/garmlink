"""Daily health and wellness tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import HEALTH_TTL
from ..deps import get_garmin

mcp = FastMCP("garmin-daily")


@mcp.tool()
async def get_daily_summary(date: str, ctx: Context) -> dict:
    """
    Return a full daily health summary for the given date (steps, calories,
    floors, active minutes, heart rate stats, stress, etc.).  Use this as the
    first call when the user asks for a general overview of their day.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_stats", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_sleep_data(date: str, ctx: Context) -> dict:
    """
    Return detailed sleep data for the night ending on the given date,
    including sleep stages (light, deep, REM, awake), duration, and score.
    Use this when the user asks about sleep quality or duration.

    Args:
        date: Date in YYYY-MM-DD format (the morning the user woke up).
    """
    client = get_garmin(ctx)
    return await client.call("get_sleep_data", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_heart_rates(date: str, ctx: Context) -> dict:
    """
    Return minute-by-minute heart rate readings for the given date along with
    resting heart rate.  Use this when the user asks about heart rate trends,
    resting HR, or wants to inspect HR spikes throughout the day.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_heart_rates", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_body_battery(start_date: str, end_date: str, ctx: Context) -> list:
    """
    Return Body Battery energy levels over a date range.  Body Battery
    reflects energy reserves based on stress, sleep, and activity.  Use this
    when the user asks how their energy levels changed over time or wants to
    find the best time of day for workouts.

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date:   End date in YYYY-MM-DD format (inclusive).
    """
    client = get_garmin(ctx)
    return await client.call("get_body_battery", start_date, end_date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_stress_data(date: str, ctx: Context) -> dict:
    """
    Return stress level readings for the given date, including average,
    maximum, and rest vs. activity stress breakdown.  Use this when the user
    asks about their stress levels or wants to correlate stress with other
    health metrics.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_stress_data", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_steps_data(date: str, ctx: Context) -> list:
    """
    Return step-count data broken down by time of day for the given date,
    including total steps, goal, and distance.  Use this when the user asks
    about daily step counts or progress toward their step goal.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_steps_data", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_body_composition(date: str, ctx: Context) -> dict:
    """
    Return body composition measurements (weight, BMI, body fat %, muscle
    mass, bone mass, etc.) recorded on or around the given date.  Use this
    when the user asks about their weight or body composition trends.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_body_composition", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_hrv_data(date: str, ctx: Context) -> dict:
    """
    Return Heart Rate Variability (HRV) data for the given date, including
    overnight HRV readings, average HRV, and HRV status.  Use this when the
    user asks about recovery quality, HRV trends, or overall readiness derived
    from HRV.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_hrv_data", date, ttl=HEALTH_TTL)
