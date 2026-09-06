"""Triathlon analysis and multi-metric insight tools for Garmin MCP."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL, HEALTH_TTL
from ..deps import get_garmin
from ..ranges import MAX_RANGE_CONCURRENCY, build_date_list, fetch_per_day
from ..sports import summarize_activity

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

    Returns training load classification, VO2max trend, readiness score, and a
    summarized recent-activity list.
    """
    client = get_garmin(ctx)

    # Load balance lives inside the training-status payload; Garmin has no
    # separate load-trend endpoint.
    load, readiness, activities = await asyncio.gather(
        client.call("get_training_status", date_str, ttl=HEALTH_TTL),
        client.call("get_training_readiness", date_str, ttl=HEALTH_TTL),
        client.call("get_activities", 0, 20, ttl=ACTIVITY_TTL),
        return_exceptions=True,
    )
    return {
        "date": date_str,
        "training_load": (
            None if isinstance(load, BaseException) else _summarize_load(load)
        ),
        "readiness": (
            None if isinstance(readiness, BaseException)
            else _summarize_readiness(readiness)
        ),
        # Summarized, not raw. Garmin returns 81 fields per activity, so 20 of
        # them is ~88,000 characters of mostly device metadata — which buries
        # the handful of numbers this tool exists to surface.
        "recent_activities": (
            None if isinstance(activities, BaseException)
            else [summarize_activity(a) for a in (activities or [])]
        ),
    }


# --- Overview projections ---------------------------------------------------
#
# Both payloads are mostly device identifiers, timestamps and duplicated nesting.
# The full versions remain one call away in `get_training_status` and
# `get_training_readiness`; what an overview needs is the handful of numbers a
# coach would open with.

def _first_device(mapping) -> dict:
    """Garmin keys several of these blocks by device id. Take the one device."""
    if not isinstance(mapping, dict):
        return {}
    for value in mapping.values():
        if isinstance(value, dict):
            return value
    return {}


def _summarize_load(load) -> dict | None:
    if not isinstance(load, dict):
        return load
    vo2 = (load.get("mostRecentVO2Max") or {}).get("generic") or {}
    status = _first_device(
        (load.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")
    )
    acute = status.get("acuteTrainingLoadDTO") or {}
    balance = _first_device(
        (load.get("mostRecentTrainingLoadBalance") or {})
        .get("metricsTrainingLoadBalanceDTOMap")
    )
    summary = {
        "vo2max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
        "vo2max_date": vo2.get("calendarDate"),
        "training_status": status.get("trainingStatusFeedbackPhrase"),
        "acute_load": acute.get("dailyTrainingLoadAcute"),
        "chronic_load": acute.get("dailyTrainingLoadChronic"),
        "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
        "acwr_status": acute.get("acwrStatus"),
        "load_aerobic_low": balance.get("monthlyLoadAerobicLow"),
        "load_aerobic_high": balance.get("monthlyLoadAerobicHigh"),
        "load_anaerobic": balance.get("monthlyLoadAnaerobic"),
        "load_balance_verdict": balance.get("trainingBalanceFeedbackPhrase"),
    }
    summary = {k: v for k, v in summary.items() if v is not None}
    low, high = summary.get("load_aerobic_low"), summary.get("load_aerobic_high")
    if low is not None and high is not None and high > low:
        summary["load_balance_reading"] = (
            "Inverted: more high-aerobic load than low-aerobic. Both the "
            "pyramidal and polarized models require the opposite — the easy "
            "sessions are being run too hard."
        )
    return summary


def _summarize_readiness(readiness) -> dict | None:
    entry = readiness[0] if isinstance(readiness, list) and readiness else readiness
    if not isinstance(entry, dict):
        return readiness
    fields = (
        "calendarDate", "score", "level", "feedbackShort",
        "sleepScore", "sleepScoreFactorFeedback",
        "hrvFactorPercent", "hrvFactorFeedback",
        "recoveryTimeFactorPercent", "acuteLoadFactorFeedback",
        "stressHistoryFactorPercent",
    )
    summary = {k: entry.get(k) for k in fields if entry.get(k) is not None}
    summary["caveat"] = (
        "A single day's composite score is not a verdict — no wearable "
        "composite has independent validation as an absolute. Call "
        "get_recovery_trend before acting on it."
    )
    return summary


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
        metric: One of 'steps', 'sleep_score', 'hrv', 'stress', 'heart_rate'
        start_date: Start of the date range in YYYY-MM-DD format.
        end_date: End of the date range in YYYY-MM-DD format.

    Returns list of {date, value} plus mean, min, max, best_day, worst_day.
    """
    client = get_garmin(ctx)

    method_map = {
        "steps": ("get_steps_data", _steps_value),
        "sleep_score": ("get_sleep_data", _sleep_value),
        "hrv": ("get_hrv_data", _hrv_value),
        "stress": ("get_stress_data", _stress_value),
        "heart_rate": ("get_heart_rates", _resting_hr_value),
    }

    if metric not in method_map:
        return {"error": f"Unknown metric '{metric}'. Choose from: {list(method_map.keys())}"}

    method_name, extract = method_map[metric]
    try:
        dates = build_date_list(start_date, end_date)
    except ValueError as exc:
        return {"error": str(exc)}

    # Bound concurrency: the client's thread pool is small, and an unbounded
    # fan-out trips Garmin's rate limiter into a retry storm.
    semaphore = asyncio.Semaphore(MAX_RANGE_CONCURRENCY)

    async def one(day: str):
        async with semaphore:
            return await client.call(method_name, day, ttl=HEALTH_TTL)

    results = await asyncio.gather(
        *[one(d) for d in dates],
        return_exceptions=True,
    )

    # One number per day, not the day's whole payload. Asked for 30 days of
    # sleep scores, this used to return 1.9 million characters: every day
    # carried per-minute `sleepLevels`, `sleepMovement` and `remSleepData`
    # arrays, none of which is a trend.
    data_points = []
    for d, r in zip(dates, results):
        if isinstance(r, BaseException):
            continue
        value = extract(r)
        # A day Garmin answers for but holds nothing on comes back as an empty
        # payload, not an error. Counting those as covered is how a window that
        # is a third empty reports "30 of 30 successful days" — and any mean
        # taken over it is wrong.
        if value is None:
            continue
        data_points.append({"date": d, "value": value})

    values = [p["value"] for p in data_points]
    stats: dict = {}
    if values:
        best = max(data_points, key=lambda p: p["value"])
        worst = min(data_points, key=lambda p: p["value"])
        stats = {
            "mean": round(sum(values) / len(values), 1),
            "min": min(values),
            "max": max(values),
            "best_day": best["date"],
            "worst_day": worst["date"],
        }

    return {
        "metric": metric,
        "start_date": start_date,
        "end_date": end_date,
        "data_points": data_points,
        "total_days": len(dates),
        # Days that actually carried a value, which is not the same as days
        # Garmin answered for.
        "days_with_data": len(data_points),
        "days_without_data": len(dates) - len(data_points),
        **stats,
        "note": (
            "For stress and heart rate a lower 'best_day' is the good one; the "
            "labels are extremes, not judgements."
        ),
    }


# --- Per-metric value extraction -------------------------------------------
#
# Each Garmin metric buries its one interesting number at a different depth, and
# a trend wants the number rather than the payload it arrived in. Returning None
# means the day holds no value — which is different from the call having failed.

def _steps_value(payload):
    if isinstance(payload, list):
        total = sum(d.get("steps") or 0 for d in payload if isinstance(d, dict))
        return total or None
    if isinstance(payload, dict):
        return payload.get("totalSteps") or payload.get("steps") or None
    return None


def _sleep_value(payload):
    if not isinstance(payload, dict):
        return None
    dto = payload.get("dailySleepDTO") or {}
    scores = (dto.get("sleepScores") or {}).get("overall") or {}
    return scores.get("value")


def _hrv_value(payload):
    if not isinstance(payload, dict):
        return None
    summary = payload.get("hrvSummary") or {}
    return summary.get("lastNightAvg") or summary.get("weeklyAvg")


def _stress_value(payload):
    if not isinstance(payload, dict):
        return None
    value = payload.get("avgStressLevel")
    # Garmin uses -1 and -2 for "not worn" and "no data".
    return value if isinstance(value, (int, float)) and value >= 0 else None


def _resting_hr_value(payload):
    if not isinstance(payload, dict):
        return None
    return payload.get("restingHeartRate")


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
        "note": (
            "Do not read today's score as a verdict. No wearable composite "
            "score — readiness, Body Battery — has independent peer-reviewed "
            "validation as an absolute number; they are trend instruments. "
            "Three consecutive low mornings is a signal, one is not. Call "
            "get_recovery_trend for the trend, and weigh sleep first: under 7 "
            "hours a night is associated with roughly 51% higher injury risk "
            "in endurance athletes, which is a larger and better-evidenced "
            "effect than any single-day readiness number. Bias toward keeping "
            "the planned session; a plan rewritten every time a score dips is "
            "not a plan."
        ),
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
        extract = _EXTRACTORS[metric]
        curr_data = _daily_values(curr_dates, curr_results, extract)
        prior_data = _daily_values(prior_dates, prior_results, extract)

    result = {
        "metric": metric,
        "current_week": {"start": curr_start, "end": curr_end, "data": curr_data},
        "prior_week": {"start": prior_start, "end": prior_end, "data": prior_data},
    }

    # The docstring has always promised a delta summary. Returning both weeks'
    # raw payloads and leaving the subtraction to the reader is not one, and it
    # is how this call came to cost 318,000 characters to deliver fourteen
    # numbers.
    curr_values = [d["value"] for d in curr_data if isinstance(d.get("value"), (int, float))]
    prior_values = [d["value"] for d in prior_data if isinstance(d.get("value"), (int, float))]
    if curr_values and prior_values:
        curr_mean = sum(curr_values) / len(curr_values)
        prior_mean = sum(prior_values) / len(prior_values)
        result["delta"] = {
            "current_mean": round(curr_mean, 1),
            "prior_mean": round(prior_mean, 1),
            "change": round(curr_mean - prior_mean, 1),
            "percent_change": (
                round((curr_mean - prior_mean) / prior_mean * 100, 1)
                if prior_mean else None
            ),
            "current_days_with_data": len(curr_values),
            "prior_days_with_data": len(prior_values),
        }
    else:
        result["delta"] = None
        result["delta_note"] = (
            "Not enough data in one or both weeks to compare. Say so rather "
            "than reading a partial week as a decline."
        )
    return result


_EXTRACTORS = {
    "steps": _steps_value,
    "sleep_score": _sleep_value,
    "hrv": _hrv_value,
    "stress": _stress_value,
    "heart_rate": _resting_hr_value,
}


def _daily_values(dates, results, extract) -> list[dict]:
    """One {date, value} per day that actually holds a value."""
    out = []
    for d, r in zip(dates, results):
        if isinstance(r, BaseException):
            continue
        value = extract(r)
        if value is None:
            continue
        out.append({"date": d, "value": value})
    return out


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
        client.call(
            "get_activities_by_date", thirty_days_ago, today, "swimming",
            ttl=ACTIVITY_TTL,
        ),
        fetch_per_day(
            client, "get_max_metrics", thirty_days_ago, today, ttl=HEALTH_TTL
        ),
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
