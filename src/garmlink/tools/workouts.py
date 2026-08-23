"""Workout library and scheduling tools for Garmin MCP."""

from __future__ import annotations

from datetime import date

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL
from ..deps import get_garmin
from ..workout_builder import WorkoutBuildError, build_workout

mcp = FastMCP("workouts")


@mcp.tool()
async def get_workouts(ctx: Context) -> list:
    """List all saved workouts in Garmin Connect. Use when user asks about their workout library."""
    client = get_garmin(ctx)
    return await client.call("get_workouts", ttl=ACTIVITY_TTL)


@mcp.tool()
async def get_workout_by_id(workout_id: int, ctx: Context) -> dict:
    """
    Fetch one saved workout in full, including its step structure.
    Use before editing a workout, so update_workout can send back a complete object.

    Args:
        workout_id: Numeric ID of the saved workout.
    """
    client = get_garmin(ctx)
    return await client.call("get_workout_by_id", workout_id, ttl=ACTIVITY_TTL)


@mcp.tool()
async def get_scheduled_workouts(
    ctx: Context, year: int | None = None, month: int | None = None
) -> dict:
    """
    List scheduled workouts on the Garmin calendar for one month.
    Use when user asks about their planned training.

    Args:
        year:  Four-digit year. Defaults to the current year.
        month: Month number 1-12. Defaults to the current month.
    """
    client = get_garmin(ctx)
    today = date.today()
    year = year or today.year
    month = month or today.month
    if not 1 <= month <= 12:
        return {"error": f"month must be between 1 and 12 (got {month})"}
    return await client.call("get_scheduled_workouts", year, month, ttl=ACTIVITY_TTL)


@mcp.tool()
async def schedule_workout(workout_id: int, scheduled_date: str, ctx: Context) -> dict:
    """
    Schedule an existing saved workout onto the Garmin calendar for a specific date.

    Args:
        workout_id: Numeric ID of the saved workout to schedule.
        scheduled_date: Date to schedule the workout in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    result = await client.call(
        "schedule_workout", workout_id, scheduled_date, cache=False
    )
    client.invalidate("get_scheduled_workouts")
    return result


@mcp.tool()
async def unschedule_workout(scheduled_workout_id: int, ctx: Context) -> dict:
    """
    Remove a workout from a date on the Garmin calendar. The workout itself is kept.
    Use with schedule_workout to move a planned session to a different day.

    Args:
        scheduled_workout_id: The calendar entry's ID, from get_scheduled_workouts.
            This is NOT the workout_id — one workout can be scheduled many times.
    """
    client = get_garmin(ctx)
    result = await client.call(
        "unschedule_workout", scheduled_workout_id, cache=False
    )
    client.invalidate("get_scheduled_workouts")
    return result


@mcp.tool()
async def upload_workout(workout: dict, ctx: Context) -> dict:
    """
    Upload a pre-built Garmin workout object to Garmin Connect. Use create_workout to build one first.

    Args:
        workout: The workout object as a dict in Garmin Connect format.
    """
    client = get_garmin(ctx)
    result = await client.call("upload_workout", workout, cache=False)
    client.invalidate("get_workouts")
    return result


@mcp.tool()
async def update_workout(
    workout_id: int,
    sport: str,
    name: str,
    steps: list[dict],
    ctx: Context,
) -> dict:
    """
    Replace a saved workout's contents in place, keeping its ID.
    Use when softening or reshaping an already-scheduled session: the workout keeps
    its ID, so every calendar entry pointing at it stays valid. Creating a
    replacement workout instead would leave the old one still on the calendar.

    Takes the same sport/name/steps as create_workout — see that tool for the
    step schema.

    Args:
        workout_id: Numeric ID of the workout to replace.
    """
    client = get_garmin(ctx)
    try:
        workout_obj = build_workout(sport, name, steps)
    except WorkoutBuildError as exc:
        return {"error": str(exc)}

    result = await client.call(
        "update_workout", workout_id, workout_obj, cache=False
    )
    client.invalidate("get_workouts")
    client.invalidate("get_workout_by_id")
    client.invalidate("get_scheduled_workouts")
    return result


@mcp.tool()
async def create_workout(
    sport: str,
    name: str,
    steps: list[dict],
    ctx: Context,
) -> dict:
    """
    Build and upload a structured workout to Garmin Connect.
    Use when user wants to create a new workout with specific intervals or structure.

    sport: 'running' | 'cycling' | 'swimming' | 'strength_training'
    name: Workout name (e.g. '5x1km threshold intervals')
    steps: Ordered list of step dicts. Each step has:
      - 'type': 'warmup' | 'interval' | 'recovery' | 'cooldown' | 'rest' | 'repeat'
      - exactly one of:
        - 'duration_seconds': int  — step ends after a time
        - 'distance_meters': float — step ends after a distance
        - 'reps': int              — strength only; also needs 'exercise'
      - 'target_type': 'heart_rate_zone' | 'power_zone' | 'pace' | 'cadence' | 'open'
      - 'target_value':
        - heart_rate_zone: zone number 1-5, or [low_bpm, high_bpm]
        - power_zone:      zone number 1-7, or [low_watts, high_watts]
        - pace:            'mm:ss' per km (per 100m for swimming), or [fast, slow].
                           Override the unit with 'pace_per': 'km'|'mile'|'100m'|'100y'
        - cadence:         steps or rpm, or [low, high]
      - strength steps also take 'exercise' (e.g. 'BENCH_PRESS') and optional 'weight_kg'

    To repeat a *group* of steps, use a step of type 'repeat' with:
      - 'iterations': int
      - 'steps': the nested list of steps to repeat

    Example — 4 x (8min at zone 4, 3min float):
      [{'type': 'warmup', 'duration_seconds': 600, 'target_type': 'heart_rate_zone', 'target_value': 2},
       {'type': 'repeat', 'iterations': 4, 'steps': [
          {'type': 'interval', 'duration_seconds': 480, 'target_type': 'heart_rate_zone', 'target_value': 4},
          {'type': 'recovery', 'duration_seconds': 180, 'target_type': 'heart_rate_zone', 'target_value': 2}]},
       {'type': 'cooldown', 'duration_seconds': 600}]

    Returns: the created workout data from Garmin Connect.
    """
    client = get_garmin(ctx)
    try:
        workout_obj = build_workout(sport, name, steps)
    except WorkoutBuildError as exc:
        return {"error": str(exc)}

    result = await client.call("upload_workout", workout_obj, cache=False)
    client.invalidate("get_workouts")
    return result
