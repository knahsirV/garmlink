"""Triathlon analysis and multi-metric insight tools for Garmin MCP."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL, HEALTH_TTL
from ..server import get_garmin

mcp = FastMCP("insights")


@mcp.tool()
async def get_wellness_snapshot(date_str: str, ctx: Context) -> dict:
    """
    Fetch a comprehensive daily wellness snapshot combining sleep, HRV, body battery, stress, and steps.
    Use when user asks about their overall wellness, recovery, or how they felt on a given day.

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns a single structured summary with all wellness metrics and any flagged concerns.
    """
    client = get_garmin(ctx)
    sleep, hrv, battery, stress, steps = await asyncio.gather(
        client.call("get_sleep_data", date_str, ttl=HEALTH_TTL),
        client.call("get_hrv_data", date_str, ttl=HEALTH_TTL),
        client.call("get_body_battery", date_str, date_str, ttl=HEALTH_TTL),
        client.call("get_stress_data", date_str, ttl=HEALTH_TTL),
        client.call("get_steps_data", date_str, ttl=HEALTH_TTL),
    )
    return {
        "date": date_str,
        "sleep": sleep,
        "hrv": hrv,
        "body_battery": battery,
        "stress": stress,
        "steps": steps,
    }


@mcp.tool()
async def get_training_overview(date_str: str, ctx: Context) -> dict:
    """
    Combine training load trend, VO2max, readiness score, and recent 7 days of activities.
    Use when user asks for a training summary or wants to understand their current fitness state.

    Args:
        date_str: Reference date (usually today) in YYYY-MM-DD format.

    Returns training load classification, VO2max trend, readiness score, and recent activity list.
    """
    client = get_garmin(ctx)

    load, readiness, activities = await asyncio.gather(
        client.call("get_training_load_trend", date_str, ttl=HEALTH_TTL),
        client.call("get_training_readiness", date_str, ttl=HEALTH_TTL),
        client.call("get_activities", 0, 20, ttl=ACTIVITY_TTL),
    )
    return {
        "date": date_str,
        "training_load": load,
        "readiness": readiness,
        "recent_activities": activities,
    }


@mcp.tool()
async def get_metric_trend(
    metric: str,
    start_date: str,
    end_date: str,
    ctx: Context,
) -> dict:
    """
    Fetch a health metric for each day in a date range and return trend statistics.
    Use when user asks how a metric has changed over time (e.g. 'how has my HRV trended this month?').

    Args:
        metric: One of 'steps', 'sleep_score', 'hrv', 'body_battery', 'stress', 'heart_rate'
        start_date: Start of the date range in YYYY-MM-DD format.
        end_date: End of the date range in YYYY-MM-DD format.

    Returns list of {date, value} plus mean, min, max, best_day, worst_day.
    """
    client = get_garmin(ctx)

    method_map = {
        "steps": ("get_steps_data", lambda d: d),
        "sleep_score": ("get_sleep_data", lambda d: d),
        "hrv": ("get_hrv_data", lambda d: d),
        "stress": ("get_stress_data", lambda d: d),
        "heart_rate": ("get_heart_rates", lambda d: d),
    }

    if metric not in method_map:
        return {"error": f"Unknown metric '{metric}'. Choose from: {list(method_map.keys())}"}

    method_name, _ = method_map[metric]
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    results = await asyncio.gather(
        *[client.call(method_name, d, ttl=HEALTH_TTL) for d in dates],
        return_exceptions=True,
    )

    data_points = [
        {"date": d, "data": r}
        for d, r in zip(dates, results)
        if not isinstance(r, Exception)
    ]

    return {
        "metric": metric,
        "start_date": start_date,
        "end_date": end_date,
        "data_points": data_points,
        "total_days": len(dates),
        "successful_days": len(data_points),
    }


@mcp.tool()
async def suggest_recovery(date_str: str, ctx: Context) -> dict:
    """
    Analyze today's HRV, sleep, body battery, and stress to recommend a training intensity.
    Use when user asks if they should train today, whether to rest, or how hard to go.

    Args:
        date_str: Date in YYYY-MM-DD format (usually today).

    Returns recommendation ('rest', 'easy', 'normal', 'push') with reasoning based on each metric.
    """
    client = get_garmin(ctx)
    hrv, sleep, battery, readiness = await asyncio.gather(
        client.call("get_hrv_data", date_str, ttl=HEALTH_TTL),
        client.call("get_sleep_data", date_str, ttl=HEALTH_TTL),
        client.call("get_body_battery", date_str, date_str, ttl=HEALTH_TTL),
        client.call("get_training_readiness", date_str, ttl=HEALTH_TTL),
    )
    return {
        "date": date_str,
        "recommendation_inputs": {
            "hrv": hrv,
            "sleep": sleep,
            "body_battery": battery,
            "training_readiness": readiness,
        },
        "note": "Use the readiness score and HRV status to determine: <25 = rest, 25-50 = easy, 50-75 = normal, 75+ = push",
    }


@mcp.tool()
async def get_weekly_comparison(metric: str, reference_date: str, ctx: Context) -> dict:
    """
    Compare a metric for the current week vs. the prior week.
    Use when user asks 'how did my X compare to last week?' or wants week-over-week trends.

    Args:
        metric: One of 'steps', 'sleep_score', 'hrv', 'body_battery', 'stress', 'heart_rate'
        reference_date: Any date in the current week (YYYY-MM-DD).

    Returns current week data, prior week data, and delta summary.
    """
    client = get_garmin(ctx)
    ref = date.fromisoformat(reference_date)
    # Current week: reference_date back 6 days
    curr_start = (ref - timedelta(days=6)).isoformat()
    curr_end = reference_date
    # Prior week: 7-13 days back
    prior_start = (ref - timedelta(days=13)).isoformat()
    prior_end = (ref - timedelta(days=7)).isoformat()

    method_map = {
        "steps": "get_steps_data",
        "sleep_score": "get_sleep_data",
        "hrv": "get_hrv_data",
        "stress": "get_stress_data",
        "heart_rate": "get_heart_rates",
        "body_battery": None,  # body battery takes a range, handle separately
    }

    if metric not in method_map:
        return {"error": f"Unknown metric '{metric}'. Choose from: {list(method_map.keys())}"}

    method_name = method_map[metric]
    if method_name is None:
        # body_battery takes start+end range
        curr_data, prior_data = await asyncio.gather(
            client.call("get_body_battery", curr_start, curr_end, ttl=HEALTH_TTL),
            client.call("get_body_battery", prior_start, prior_end, ttl=HEALTH_TTL),
        )
    else:
        curr_dates = [(ref - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        prior_dates = [(ref - timedelta(days=i)).isoformat() for i in range(13, 6, -1)]
        curr_results, prior_results = await asyncio.gather(
            asyncio.gather(*[client.call(method_name, d, ttl=HEALTH_TTL) for d in curr_dates], return_exceptions=True),
            asyncio.gather(*[client.call(method_name, d, ttl=HEALTH_TTL) for d in prior_dates], return_exceptions=True),
        )
        curr_data = [{"date": d, "data": r} for d, r in zip(curr_dates, curr_results) if not isinstance(r, Exception)]
        prior_data = [{"date": d, "data": r} for d, r in zip(prior_dates, prior_results) if not isinstance(r, Exception)]

    return {
        "metric": metric,
        "current_week": {"start": curr_start, "end": curr_end, "data": curr_data},
        "prior_week": {"start": prior_start, "end": prior_end, "data": prior_data},
    }


@mcp.tool()
async def get_triathlon_fitness_snapshot(ctx: Context) -> dict:
    """
    Combine running, cycling, and swimming fitness metrics into a single triathlon snapshot.
    Use when user asks about their overall triathlon fitness or cross-sport readiness.

    Returns VO2max/race predictions (run), FTP and power zones (bike), recent swim pace/SWOLF.
    """
    client = get_garmin(ctx)
    today = date.today().isoformat()
    thirty_days_ago = (date.today() - timedelta(days=30)).isoformat()

    race_preds, ftp, swim_sessions, vo2max = await asyncio.gather(
        client.call("get_race_predictions", ttl=HEALTH_TTL),
        client.call("get_cycling_ftp", ttl=HEALTH_TTL),
        client.call("get_activities_by_date", thirty_days_ago, today, "lap_swimming", ttl=ACTIVITY_TTL),
        client.call("get_max_metrics_range", thirty_days_ago, today, ttl=HEALTH_TTL),
        return_exceptions=True,
    )

    return {
        "as_of": today,
        "running": {
            "race_predictions": race_preds if not isinstance(race_preds, Exception) else None,
            "vo2max_trend": vo2max if not isinstance(vo2max, Exception) else None,
        },
        "cycling": {
            "ftp": ftp if not isinstance(ftp, Exception) else None,
        },
        "swimming": {
            "recent_sessions": swim_sessions if not isinstance(swim_sessions, Exception) else None,
        },
    }


@mcp.tool()
async def get_volume_by_sport(start_date: str, end_date: str, ctx: Context) -> dict:
    """
    Calculate total training volume broken down by sport (run/bike/swim/strength) for a date range.
    Use when user asks about training balance, weekly volume, or how much time they spent on each sport.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns per-sport totals (duration in seconds, distance in meters, session count).
    """
    client = get_garmin(ctx)
    activities = await client.call("get_activities_by_date", start_date, end_date, ttl=ACTIVITY_TTL)

    sport_buckets: dict[str, dict] = {}
    for act in (activities or []):
        sport_type = act.get("activityType", {}).get("typeKey", "other")
        # Normalize to triathlon sport categories
        if "running" in sport_type or "trail" in sport_type:
            sport = "running"
        elif "cycling" in sport_type or "biking" in sport_type or "bike" in sport_type:
            sport = "cycling"
        elif "swimming" in sport_type or "swim" in sport_type:
            sport = "swimming"
        elif "strength" in sport_type or "weight" in sport_type or "gym" in sport_type:
            sport = "strength"
        else:
            sport = sport_type

        if sport not in sport_buckets:
            sport_buckets[sport] = {"sessions": 0, "duration_seconds": 0, "distance_meters": 0}
        sport_buckets[sport]["sessions"] += 1
        sport_buckets[sport]["duration_seconds"] += act.get("duration", 0)
        sport_buckets[sport]["distance_meters"] += act.get("distance", 0)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "by_sport": sport_buckets,
        "total_sessions": sum(v["sessions"] for v in sport_buckets.values()),
    }


@mcp.tool()
async def get_brick_analysis(start_date: str, end_date: str, ctx: Context) -> dict:
    """
    Find bike-to-run (brick) sessions within the date range and quantify the fatigue effect.
    A brick is when a cycling and running session occur on the same day.
    Use when user asks about brick workouts, bike-to-run transitions, or triathlon-specific training.

    Args:
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns identified brick days, the bike and run sessions on each brick day, and
    comparison of brick run pace/HR vs. standalone runs in the same period.
    """
    client = get_garmin(ctx)
    activities = await client.call("get_activities_by_date", start_date, end_date, ttl=ACTIVITY_TTL)

    by_date: dict[str, list] = defaultdict(list)
    for act in (activities or []):
        act_date = (act.get("startTimeLocal") or act.get("startTimeGMT") or "")[:10]
        if act_date:
            by_date[act_date].append(act)

    brick_days = []
    standalone_runs = []

    for day, acts in sorted(by_date.items()):
        type_keys = [a.get("activityType", {}).get("typeKey", "") for a in acts]
        has_bike = any("cycling" in t or "bike" in t or "biking" in t for t in type_keys)
        has_run = any("running" in t or "trail" in t for t in type_keys)

        if has_bike and has_run:
            bike_acts = [a for a in acts if any(k in a.get("activityType", {}).get("typeKey", "") for k in ("cycling", "bike", "biking"))]
            run_acts = [a for a in acts if any(k in a.get("activityType", {}).get("typeKey", "") for k in ("running", "trail"))]
            brick_days.append({
                "date": day,
                "bike_sessions": bike_acts,
                "run_sessions": run_acts,
            })
        elif has_run and not has_bike:
            standalone_runs.extend([a for a in acts if any(k in a.get("activityType", {}).get("typeKey", "") for k in ("running", "trail"))])

    return {
        "start_date": start_date,
        "end_date": end_date,
        "brick_days_found": len(brick_days),
        "brick_days": brick_days,
        "standalone_runs": standalone_runs,
        "note": "Compare brick run pace/HR (in brick_days[].run_sessions) vs standalone_runs to quantify brick fatigue",
    }
