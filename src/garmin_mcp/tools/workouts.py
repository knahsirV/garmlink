"""Workout library and scheduling tools for Garmin MCP."""

from __future__ import annotations

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL
from ..deps import get_garmin

mcp = FastMCP("workouts")


@mcp.tool()
async def get_workouts(ctx: Context) -> dict:
    """List all saved workouts in Garmin Connect. Use when user asks about their workout library."""
    client = get_garmin(ctx)
    return await client.call("get_workouts", ttl=ACTIVITY_TTL)


@mcp.tool()
async def get_scheduled_workouts(ctx: Context) -> dict:
    """List upcoming scheduled workouts on the Garmin calendar. Use when user asks about their planned training."""
    client = get_garmin(ctx)
    return await client.call("get_scheduled_workouts", ttl=ACTIVITY_TTL)


@mcp.tool()
async def schedule_workout(workout_id: int, scheduled_date: str, ctx: Context) -> dict:
    """
    Schedule an existing saved workout onto the Garmin calendar for a specific date.

    Args:
        workout_id: Numeric ID of the saved workout to schedule.
        scheduled_date: Date to schedule the workout in YYYY-MM-DD format.
    """
    client = get_garmin(ctx)
    result = await client.call("schedule_workout", workout_id, scheduled_date)
    client._cache.invalidate("get_scheduled_workouts")
    return result


@mcp.tool()
async def upload_workout(workout: dict, ctx: Context) -> dict:
    """
    Upload a pre-built Garmin workout object to Garmin Connect. Use create_workout to build one first.

    Args:
        workout: The workout object as a dict in Garmin Connect format.
    """
    client = get_garmin(ctx)
    result = await client.call("upload_workout", workout)
    client._cache.invalidate("get_workouts")
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

    sport: One of 'running', 'cycling', 'swimming', 'strength_training'
    name: Workout name (e.g. '5x1km threshold intervals')
    steps: List of workout steps. Each step is a dict with:
      - 'type': 'warmup' | 'interval' | 'recovery' | 'cooldown' | 'rest'
      - 'duration_seconds': int (how long this step lasts)
      - 'target_type': 'heart_rate_zone' | 'power_zone' | 'pace' | 'open' (optional)
      - 'target_value': zone number (1-7) or target pace string (optional)
      - 'repeat': int (number of times to repeat this step, for intervals, default 1)

    Returns: the created workout data from Garmin Connect.
    """
    client = get_garmin(ctx)

    # Map sport string to Garmin activity type key
    sport_map = {
        "running": "running",
        "cycling": "cycling",
        "swimming": "lap_swimming",
        "strength_training": "strength_training",
    }
    activity_type_key = sport_map.get(sport.lower(), sport.lower())

    # Build Garmin workout structure
    workout_steps = []
    step_order = 1
    for step in steps:
        step_type = step.get("type", "interval")
        repeat = step.get("repeat", 1)
        duration_sec = step.get("duration_seconds", 0)
        target_type = step.get("target_type", "open")
        target_value = step.get("target_value")

        # Build end condition
        end_condition = {"conditionTypeKey": "time", "conditionValue": duration_sec}

        # Build target
        if target_type == "heart_rate_zone" and target_value:
            target = {"workoutTargetTypeKey": "heart.rate.zone", "targetValueOne": int(target_value)}
        elif target_type == "power_zone" and target_value:
            target = {"workoutTargetTypeKey": "power.zone", "targetValueOne": int(target_value)}
        else:
            target = {"workoutTargetTypeKey": "no.target"}

        garmin_step = {
            "stepOrder": step_order,
            "stepTypeKey": step_type,
            "endCondition": end_condition,
            "workoutStepTarget": target,
        }

        if repeat > 1:
            # Wrap in repeat group
            workout_steps.append({
                "stepOrder": step_order,
                "stepTypeKey": "repeat",
                "numberOfIterations": repeat,
                "workoutSteps": [garmin_step],
            })
        else:
            workout_steps.append(garmin_step)
        step_order += 1

    workout_obj = {
        "workoutName": name,
        "sportType": {"sportTypeKey": activity_type_key},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeKey": activity_type_key},
            "workoutSteps": workout_steps,
        }],
    }

    result = await client.call("upload_workout", workout_obj, ttl=ACTIVITY_TTL)
    client._cache.invalidate("get_workouts")
    return result
