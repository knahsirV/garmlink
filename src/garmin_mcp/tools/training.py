"""Training and performance tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import HEALTH_TTL
from ..deps import get_garmin
from ..ranges import fetch_per_day

mcp = FastMCP("garmin-training")


@mcp.tool()
async def get_training_status(date: str, ctx: Context) -> dict:
    """
    Return the training status for the given date (e.g. Productive, Maintaining,
    Detraining, Overreaching).  Garmin derives this from recent training load
    and fitness trends.  Use this when the user wants to know whether their
    current training intensity is appropriate.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_training_status", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_training_readiness(date: str, ctx: Context) -> dict:
    """
    Return the training readiness score for the given date, reflecting how
    prepared the body is for a hard workout based on sleep, HRV, recovery,
    and recent load.  Use this when the user asks whether they should train
    hard or take it easy today.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_training_readiness", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_training_load_trend(date: str, ctx: Context) -> dict:
    """
    Return the training load balance for the given date: acute (7-day) load
    versus chronic (28-day) load, plus whether the user sits in the optimal,
    low, or high load zone.  Use this when the user wants to understand their
    overall training volume and balance.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    # Garmin exposes load balance inside the training-status payload; there is
    # no separate load-trend endpoint.
    return await client.call("get_training_status", date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_vo2max_trend(start_date: str, end_date: str, ctx: Context) -> dict:
    """
    Return VO2 max estimates and other max metrics for each day in a date range.
    Use this when the user asks about their aerobic fitness trend, VO2 max
    history, or long-term cardiorespiratory improvement.

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date:   End date in YYYY-MM-DD format (inclusive, max 90 days after
                    start_date).
    """
    client = get_garmin(ctx)
    return await fetch_per_day(
        client, "get_max_metrics", start_date, end_date, ttl=HEALTH_TTL
    )


@mcp.tool()
async def get_hrv_trend(start_date: str, end_date: str, ctx: Context) -> dict:
    """
    Return HRV (Heart Rate Variability) readings for each day in a date range to
    surface recovery and stress trends across multiple days or weeks.  Use this
    when the user wants to see how their HRV has changed over time rather than a
    single-day snapshot.

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date:   End date in YYYY-MM-DD format (inclusive, max 90 days after
                    start_date).
    """
    client = get_garmin(ctx)
    return await fetch_per_day(
        client, "get_hrv_data", start_date, end_date, ttl=HEALTH_TTL
    )


@mcp.tool()
async def get_endurance_score(date: str, ctx: Context) -> dict:
    """
    Return the Garmin Endurance Score for the given date, which measures
    cumulative aerobic fitness built through sustained-effort activities.
    Use this when the user asks about their endurance level, stamina, or
    long-term aerobic capacity.

    Args:
        date: Date in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    return await client.call("get_endurance_score", date, ttl=HEALTH_TTL)
