"""User profile and device tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import STATIC_TTL
from ..server import get_garmin

mcp = FastMCP("profile")


@mcp.tool()
async def get_user_profile(ctx: Context) -> dict:
    """Get the user's Garmin profile: name, age, gender, and heart rate zones. Use when you need to know the user's HR zones or personal details."""
    client = get_garmin(ctx)
    return await client.call("get_user_profile", ttl=STATIC_TTL)


@mcp.tool()
async def get_devices(ctx: Context) -> dict:
    """List connected Garmin devices. Use when user asks about their watch or device."""
    client = get_garmin(ctx)
    return await client.call("get_devices", ttl=STATIC_TTL)


@mcp.tool()
async def get_activity_types(ctx: Context) -> dict:
    """Get all available Garmin activity type keys. Use when you need valid activity type strings for other tools."""
    client = get_garmin(ctx)
    return await client.call("get_activity_types", ttl=STATIC_TTL)
