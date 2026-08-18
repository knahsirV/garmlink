"""Cycling-specific tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import HEALTH_TTL
from ..deps import get_garmin

mcp = FastMCP("cycling")


@mcp.tool()
async def get_cycling_ftp(ctx: Context) -> dict:
    """
    Use when user asks about their FTP (functional threshold power) or cycling power capacity.
    Returns: Current FTP value from Garmin.
    """
    client = get_garmin(ctx)
    return await client.call("get_cycling_ftp", ttl=HEALTH_TTL)


@mcp.tool()
async def get_ftp_history(start_date: str, end_date: str, ctx: Context) -> dict:
    """
    Use when user asks how their FTP has changed over time or about FTP trends.

    Args:
        start_date: Start date in YYYY-MM-DD format (inclusive).
        end_date:   End date in YYYY-MM-DD format (inclusive).
    """
    client = get_garmin(ctx)
    return await client.call("get_functional_threshold_power_range", start_date, end_date, ttl=HEALTH_TTL)


@mcp.tool()
async def get_cycling_power_zones(ctx: Context) -> dict:
    """
    Compute standard 7 cycling power zones based on current FTP.
    Use when user asks about their power zones or wants to know what watts to target.
    Returns: zone name, watts range (low-high), and percentage of FTP for each zone.
    """
    client = get_garmin(ctx)
    ftp_data = await client.call("get_cycling_ftp", ttl=HEALTH_TTL)
    # Extract FTP value — garminconnect returns it in different shapes
    ftp = None
    if isinstance(ftp_data, dict):
        ftp = ftp_data.get("functionalThresholdPower") or ftp_data.get("ftp") or ftp_data.get("value")
    if not ftp:
        return {"error": "Could not determine FTP from Garmin data", "raw": ftp_data}
    zones = [
        {"zone": 1, "name": "Active Recovery",  "min_pct": 0,   "max_pct": 55},
        {"zone": 2, "name": "Endurance",         "min_pct": 55,  "max_pct": 75},
        {"zone": 3, "name": "Tempo",             "min_pct": 75,  "max_pct": 90},
        {"zone": 4, "name": "Threshold",         "min_pct": 90,  "max_pct": 105},
        {"zone": 5, "name": "VO2 Max",           "min_pct": 105, "max_pct": 120},
        {"zone": 6, "name": "Anaerobic",         "min_pct": 120, "max_pct": 150},
        {"zone": 7, "name": "Neuromuscular",     "min_pct": 150, "max_pct": 999},
    ]
    for z in zones:
        z["min_watts"] = round(ftp * z["min_pct"] / 100)
        z["max_watts"] = round(ftp * z["max_pct"] / 100) if z["max_pct"] < 999 else None
    return {"ftp_watts": ftp, "zones": zones}
