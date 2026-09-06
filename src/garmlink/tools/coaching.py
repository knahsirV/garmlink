"""Computed coaching primitives: the numbers a coach leads with.

The rest of this server hands Garmin's payloads to the model with a `note`
telling it what to work out. That is the right split for judgement and the wrong
one for arithmetic — a mean is not a matter of opinion, and asking for one
inside 90,000 characters of device metadata gets it wrong quietly.

What lives here is the arithmetic, and only the arithmetic:

- how much is actually being done, against how much was planned;
- how the work is distributed across intensity;
- how far a single session may safely step;
- and whether recovery supports progressing at all.

The interpretation stays in `prompts.py`. Nothing here reads the plan document:
the plan gets reorganised as training changes, so these tools return actuals and
the model compares them to the template it already fetched.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

from fastmcp import Context, FastMCP

from ..cache import ACTIVITY_TTL, HEALTH_TTL
from ..deps import get_garmin
from ..ranges import MAX_RANGE_DAYS, fetch_per_day
from ..sports import covered_window, is_cardio, miles, normalize_sport

mcp = FastMCP("coaching")

# Under 7 hours is associated with roughly 51% higher injury risk in endurance
# athletes, and poorer sleep quality with about 36% more running injuries in
# recreational runners. Reported as a count of nights rather than an average
# because the effect is a threshold: 8h and 6h average to a passing 7h and are
# not equivalent.
SLEEP_THRESHOLD_SECONDS = 7 * 3600

# The single-session form of the ten-percent rule — the one with cohort evidence
# behind it. The weekly-volume form does not have any.
PROGRESSION_CEILING = 1.10


def _window(days: int) -> tuple[str, str, int]:
    days = max(1, min(days, MAX_RANGE_DAYS))
    end = date.today()
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat(), days


async def _activities(client, start: str, end: str) -> list[dict]:
    result = await client.call(
        "get_activities_by_date", start, end, ttl=ACTIVITY_TTL
    )
    return result or []


@mcp.tool()
async def get_session_counts(
    ctx: Context, days: int = 28, target_weekly_miles: float = 0
) -> dict:
    """
    Sessions and weekly running volume — the "actual" half of an adherence check,
    and the number that most predicts a race result.

    Weekly running mileage is the strongest modifiable predictor of half-marathon
    time; weekly distance together with BMI and VO2max explains roughly 63% of the
    variation between runners. Nothing else in this server computes it, so a block
    can be far too small for its own goal without anything noticing.

    Counts strength as a session but reports no distance for it, and folds indoor
    rides (Garmin files them as `virtual_ride`) into cycling — read literally,
    that key makes bike volume read far too low.

    Args:
        days: Lookback window, default 28. Capped at 90.
        target_weekly_miles: The plan's weekly running target, if it states one.
                             Read it from get_training_plan; do not invent it.
                             Given, the result reports shortfall against it.

    Returns per-sport session counts and rates, running miles per ISO week with a
    trailing mean and direction, and the window actually covered — which is not
    always the window requested.
    """
    client = get_garmin(ctx)
    start, end, days = _window(days)
    activities = await _activities(client, start, end)

    buckets: dict[str, dict] = defaultdict(
        lambda: {"sessions": 0, "duration_min": 0.0, "distance_mi": 0.0}
    )
    seen_dates: list[str] = []
    for act in activities:
        sport = normalize_sport((act.get("activityType") or {}).get("typeKey"))
        bucket = buckets[sport]
        bucket["sessions"] += 1
        bucket["duration_min"] += (act.get("duration") or 0) / 60
        if is_cardio(sport):
            bucket["distance_mi"] += (miles(act.get("distance")) or 0)
        start_local = act.get("startTimeLocal")
        if start_local:
            seen_dates.append(start_local[:10])

    # Bucketed by ISO week, because "miles per week" is the unit the evidence is
    # in and a 28-day mean hides a ramp or a collapse inside it.
    weekly: dict[str, float] = defaultdict(float)
    for act in activities:
        if normalize_sport((act.get("activityType") or {}).get("typeKey")) != "running":
            continue
        start_local = act.get("startTimeLocal")
        distance = miles(act.get("distance"))
        if not start_local or not distance:
            continue
        iso = date.fromisoformat(start_local[:10]).isocalendar()
        weekly[f"{iso[0]}-W{iso[1]:02d}"] += distance

    weeks = days / 7
    by_sport = {
        sport: {
            "sessions": v["sessions"],
            "sessions_per_week": round(v["sessions"] / weeks, 1),
            "duration_min": round(v["duration_min"], 1),
            **({"distance_mi": round(v["distance_mi"], 2)} if is_cardio(sport) else {}),
        }
        for sport, v in sorted(buckets.items())
    }
    total = sum(v["sessions"] for v in by_sport.values())

    result: dict = {
        "window": covered_window(seen_dates, start, end),
        "days": days,
        "by_sport": by_sport,
        "total_sessions": total,
        "sessions_per_week": round(total / weeks, 1),
        "running_volume": _running_volume(weekly, target_weekly_miles),
        "note": (
            "Compare against the plan's weekly template. A sport the plan "
            "schedules and that has no sessions here is a finding, not an "
            "empty section — say so explicitly."
        ),
        "warning": _VOLUME_WARNING,
    }
    return result


# The mirror of _CEILING_WARNING. That one stops a ramp limit being read as a
# capability ceiling; this one stops a volume shortfall being read as a verdict
# on the athlete.
_VOLUME_WARNING = (
    "A weekly volume below target is a finding about the PLAN, not about the "
    "athlete. The response is to raise the prescription, not to lower the goal "
    "or conclude the goal is unrealistic. Note also that higher volume and "
    "longer long runs associate with faster finish times AND with no increase "
    "in injury risk — so risk is not a reason to prescribe less by default. Cut "
    "volume when get_recovery_trend says to, not pre-emptively."
)


def _running_volume(weekly: dict[str, float], target: float) -> dict:
    """Running miles per ISO week, with the trend and any shortfall."""
    if not weekly:
        return {
            "by_week": {},
            "note": "No running in this window — see the window warning before "
                    "reading that as low fitness.",
        }
    ordered = dict(sorted(weekly.items()))
    values = [round(v, 1) for v in ordered.values()]
    # The first and last buckets are usually partial weeks clipped by the window,
    # so the mean is taken over whole weeks where there are any.
    whole = values[1:-1] if len(values) > 2 else values
    out: dict = {
        "by_week": {k: round(v, 1) for k, v in ordered.items()},
        "mean_miles_per_week": round(sum(whole) / len(whole), 1),
        "peak_week_miles": max(values),
    }
    if len(values) >= 4:
        first, second = values[: len(values) // 2], values[len(values) // 2 :]
        change = sum(second) / len(second) - sum(first) / len(first)
        out["direction"] = (
            "rising" if change > 1 else ("falling" if change < -1 else "flat")
        )
    if target:
        mean = out["mean_miles_per_week"]
        out["target_weekly_miles"] = target
        out["weeks_at_or_above_target"] = sum(1 for v in values if v >= target)
        out["weeks_counted"] = len(values)
        if mean < target:
            out["shortfall_miles_per_week"] = round(target - mean, 1)
            out["verdict"] = (
                f"UNDER TARGET by {round(target - mean, 1)} mi/wk. The plan is "
                "not prescribing enough running for its own goal."
            )
        else:
            out["verdict"] = "At or above the plan's weekly target."
    return out


@mcp.tool()
async def get_progression_check(
    ctx: Context, sport: str = "running", days: int = 30
) -> dict:
    """
    The longest single session of the trailing window, and the ceiling for the
    next one. Use before scheduling or approving a long run.

    A single session should not exceed roughly 10% beyond the longest of the
    previous 30 days. That is the form of the ten-percent rule with cohort
    evidence behind it; the weekly-volume form widely quoted has none.

    Args:
        sport: 'running', 'cycling', 'swimming' or 'hiking'. Default 'running'.
        days: Trailing window, default 30 — matching the evidence. Capped at 90.

    Returns the trailing longest, the ceiling it implies, and every session in
    the window so the ramp can be read rather than asserted.
    """
    client = get_garmin(ctx)
    start, end, days = _window(days)

    # Capability is fetched alongside the trailing distance, not on request.
    # The trailing longest run is a tempting number to reason from and a
    # misleading one on its own: it describes what was recently DONE, which in a
    # sparse logging period says very little about what can be done. Shipping
    # the race predictions in the same payload is what stops the ramp ceiling
    # from being read as a capability ceiling.
    activities, predictions, status = await asyncio.gather(
        _activities(client, start, end),
        client.call("get_race_predictions", ttl=HEALTH_TTL),
        client.call("get_training_status", end, ttl=HEALTH_TTL),
        return_exceptions=True,
    )
    if isinstance(activities, BaseException):
        activities = []

    target = sport.lower().strip()
    sessions = []
    for act in activities:
        if normalize_sport((act.get("activityType") or {}).get("typeKey")) != target:
            continue
        distance = miles(act.get("distance"))
        if not distance:
            continue
        sessions.append({
            "date": (act.get("startTimeLocal") or "")[:10],
            "distance_mi": distance,
            "duration_min": round((act.get("duration") or 0) / 60, 1),
            "avg_hr": act.get("averageHR"),
        })
    sessions.sort(key=lambda s: s["date"])

    capability = _capability(predictions, status)

    if not sessions:
        return {
            "sport": target,
            "window": covered_window([], start, end),
            "days": days,
            "longest_mi": None,
            "next_session_ceiling_mi": None,
            "capability": capability,
            "note": (
                f"No {target} sessions in the last {days} days, so there is no "
                "trailing anchor to progress from. That is a gap in the record, "
                "NOT evidence of low fitness — read `capability` before drawing "
                "any conclusion about what this athlete can do. Restart at a "
                "distance that is comfortably repeatable and say so."
            ),
            "warning": _CEILING_WARNING,
        }

    longest = max(s["distance_mi"] for s in sessions)
    return {
        "sport": target,
        "window": covered_window([s["date"] for s in sessions], start, end),
        "days": days,
        "sessions": sessions,
        "longest_mi": longest,
        "next_session_ceiling_mi": round(longest * PROGRESSION_CEILING, 2),
        "capability": capability,
        "note": (
            f"The next {target} long session should not exceed "
            f"{round(longest * PROGRESSION_CEILING, 2)} miles. Below about 5 "
            "miles, step by a fixed half mile instead — 10% of two miles is 350 "
            "yards, and the cohort evidence comes from runners with real "
            "mileage. A cutback week does not reset this anchor. Re-attaining a "
            "distance held within the last several months is not the same as "
            "exceeding a lifetime maximum, and can step faster."
        ),
        "warning": _CEILING_WARNING,
    }


# Attached to every response, because the failure it describes has already
# happened: a 2.06mi trailing longest was read as proof that a half marathon
# 14 weeks out needed a run/walk finish, while the same day's race prediction
# said 1:54. The arithmetic was right and the premise was wrong.
_CEILING_WARNING = (
    "next_session_ceiling_mi is a safe RATE OF INCREASE for one session. It is "
    "NOT a ceiling on what this athlete can race, and it must never be "
    "extrapolated forward to argue a goal is unreachable. Those are different "
    "questions with different evidence: judge feasibility from `capability` — "
    "race predictions, VO2max, best recent efforts — and judge the ramp from "
    "this number. A sparse training log means sparse logging, which is usually "
    "a busy life rather than lost fitness."
)


def _capability(predictions, status) -> dict:
    """What this athlete can currently do, as distinct from what they did do."""
    out: dict = {}
    if isinstance(predictions, dict):
        for key, label in (
            ("time5K", "5k"), ("time10K", "10k"),
            ("timeHalfMarathon", "half_marathon"), ("timeMarathon", "marathon"),
        ):
            seconds = predictions.get(key)
            if seconds:
                out[f"predicted_{label}"] = (
                    f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:"
                    f"{int(seconds % 60):02d}"
                )
    if isinstance(status, dict):
        vo2 = (status.get("mostRecentVO2Max") or {}).get("generic") or {}
        value = vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue")
        if value:
            out["vo2max"] = value
    if not out:
        return {
            "note": "No capability data available — say so rather than "
                    "inferring fitness from training volume alone."
        }
    out["note"] = (
        "Garmin's predictions are modelled from VO2max and recent training, so "
        "they describe the aerobic engine rather than durability for a distance "
        "never yet run. Read them as the fitness ceiling and the trailing long "
        "run as the durability floor — a goal between the two is a training "
        "problem, not an impossibility."
    )
    return out


@mcp.tool()
async def get_intensity_distribution(
    ctx: Context,
    days: int = 28,
    easy_max_hr: int = 0,
    hard_min_hr: int = 0,
) -> dict:
    """
    How training time is split across intensities — the check that catches a base
    phase quietly being run at tempo.

    Reports two independent readings. Garmin's own monthly aerobic-low /
    aerobic-high / anaerobic load needs no zones at all. Passing the athlete's
    zone boundaries additionally splits session time by average heart rate.

    Args:
        days: Lookback window, default 28. Capped at 90.
        easy_max_hr: Top of the easy zone, from the plan's Training Zones. Read
                     it from get_training_plan; do not guess it.
        hard_min_hr: Bottom of the hard zone, likewise. Anything between the two
                     counts as moderate.

    Returns percentages by band, the shape they describe, and Garmin's load
    balance as a cross-check.
    """
    client = get_garmin(ctx)
    start, end, days = _window(days)

    activities, status = await asyncio.gather(
        _activities(client, start, end),
        client.call("get_training_status", end, ttl=HEALTH_TTL),
        return_exceptions=True,
    )
    if isinstance(activities, BaseException):
        activities = []
    balance = None
    if not isinstance(status, BaseException) and isinstance(status, dict):
        raw = (status.get("mostRecentTrainingLoadBalance") or {}).get(
            "metricsTrainingLoadBalanceDTOMap"
        ) or {}
        for entry in raw.values():
            balance = {
                "aerobic_low": round(entry.get("monthlyLoadAerobicLow") or 0, 1),
                "aerobic_high": round(entry.get("monthlyLoadAerobicHigh") or 0, 1),
                "anaerobic": round(entry.get("monthlyLoadAnaerobic") or 0, 1),
                "garmin_verdict": entry.get("trainingBalanceFeedbackPhrase"),
            }
            break

    result: dict = {
        "window": covered_window(
            [(a.get("startTimeLocal") or "")[:10] for a in activities if a.get("startTimeLocal")],
            start, end,
        ),
        "days": days,
        "garmin_load_balance": balance,
    }

    if balance and balance["aerobic_high"] > balance["aerobic_low"]:
        result["load_balance_reading"] = (
            "Inverted: more high-aerobic load than low-aerobic. Both the "
            "pyramidal and polarized models require the opposite. This is the "
            "signature of easy sessions being run too hard rather than of too "
            "many hard sessions."
        )

    if not easy_max_hr or not hard_min_hr:
        result["note"] = (
            "Heart-rate banding was skipped: pass easy_max_hr and hard_min_hr "
            "from the plan's Training Zones to get it. Garmin's load balance "
            "above stands on its own."
        )
        return result

    bands = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    unbanded = 0.0
    for act in activities:
        sport = normalize_sport((act.get("activityType") or {}).get("typeKey"))
        minutes = (act.get("duration") or 0) / 60
        if not minutes or not is_cardio(sport):
            continue
        hr = act.get("averageHR")
        if not hr:
            unbanded += minutes
            continue
        band = "easy" if hr <= easy_max_hr else ("hard" if hr >= hard_min_hr else "moderate")
        bands[band] += minutes

    total = sum(bands.values())
    if total:
        pct = {k: round(v / total * 100) for k, v in bands.items()}
        result["by_band"] = {
            k: {"minutes": round(bands[k], 1), "percent": pct[k]} for k in bands
        }
        result["shape"] = _shape(pct)
    result["unbanded_minutes"] = round(unbanded, 1)
    result["thresholds"] = {"easy_max_hr": easy_max_hr, "hard_min_hr": hard_min_hr}
    result["note"] = (
        "Banding uses each session's AVERAGE heart rate, so an interval session "
        "lands in one band rather than being split across them. Read it as the "
        "shape of the week, not as time-in-zone."
    )
    return result


def _shape(pct: dict[str, int]) -> str:
    easy, moderate, hard = pct["easy"], pct["moderate"], pct["hard"]
    if easy < 50:
        return (
            "inverted — less than half the time is easy, which is the pattern "
            "that stalls a base phase"
        )
    if easy >= 75 and moderate <= 10:
        return "polarized — mostly easy, a little hard, almost nothing between"
    if easy >= 60 and moderate >= hard:
        return (
            "pyramidal — mostly easy, then moderate, least hard. For a "
            "recreational athlete this is the better-supported shape"
        )
    return "threshold-heavy — a large middle, which costs recovery without the adaptation of either end"


@mcp.tool()
async def get_recovery_trend(ctx: Context, days: int = 28) -> dict:
    """
    Sleep and stress as trends rather than as today's number — the inputs that
    decide whether a planned progression should happen at all.

    Sleep is the best-evidenced modifiable injury signal available here: under
    7 hours a night is associated with roughly 51% higher injury risk in
    endurance athletes. That is a larger and better-supported effect than any
    training-load ratio, so read this before load.

    Args:
        days: Lookback window, default 28. Capped at 90.

    Returns nights under 7h over the last 7 and the full window, mean sleep,
    stress against the athlete's own baseline, and how many nights actually
    carried data — which is not the same as how many days were requested.
    """
    client = get_garmin(ctx)
    start, end, days = _window(days)

    sleep_days, stress_days = await asyncio.gather(
        fetch_per_day(client, "get_sleep_data", start, end, ttl=HEALTH_TTL),
        fetch_per_day(client, "get_stress_data", start, end, ttl=HEALTH_TTL),
        return_exceptions=True,
    )

    nights = []
    if not isinstance(sleep_days, BaseException) and "days" in sleep_days:
        for entry in sleep_days["days"]:
            dto = (entry.get("data") or {}).get("dailySleepDTO") or {}
            seconds = dto.get("sleepTimeSeconds") or 0
            # A day Garmin answered for but has no sleep on returns an empty
            # DTO. Counting it as covered is how "30 of 30 successful days"
            # gets reported over a window that is a third empty.
            if not seconds:
                continue
            scores = (dto.get("sleepScores") or {}).get("overall") or {}
            nights.append({
                "date": entry["date"],
                "hours": round(seconds / 3600, 1),
                "score": scores.get("value"),
            })
    nights.sort(key=lambda n: n["date"])

    stress = []
    if not isinstance(stress_days, BaseException) and "days" in stress_days:
        for entry in stress_days["days"]:
            data = entry.get("data") or {}
            avg = data.get("avgStressLevel")
            if avg is None or avg < 0:
                continue
            stress.append({
                "date": entry["date"],
                "avg": avg,
                "max": data.get("maxStressLevel"),
            })
    stress.sort(key=lambda s: s["date"])

    result: dict = {
        "window": covered_window([n["date"] for n in nights], start, end),
        "days": days,
        "nights_with_data": len(nights),
        "days_requested": days,
    }

    if nights:
        last7 = nights[-7:]
        short_recent = sum(1 for n in last7 if n["hours"] * 3600 < SLEEP_THRESHOLD_SECONDS)
        short_all = sum(1 for n in nights if n["hours"] * 3600 < SLEEP_THRESHOLD_SECONDS)
        result["sleep"] = {
            "mean_hours": round(sum(n["hours"] for n in nights) / len(nights), 1),
            "nights_under_7h_last_7": short_recent,
            "nights_under_7h_in_window": short_all,
            "share_under_7h_percent": round(short_all / len(nights) * 100),
            "recent": last7,
        }
        result["progression_gate"] = "HOLD" if short_recent >= 3 else "OK"
        result["progression_gate_reason"] = (
            f"{short_recent} of the last {len(last7)} nights were under 7 hours. "
            "Hold the long run at last week's distance and repeat the week rather "
            "than progressing; do not rewrite the block."
            if short_recent >= 3 else
            f"{short_recent} of the last {len(last7)} nights were under 7 hours. "
            "Sleep does not block a planned progression."
        )

    if stress:
        avgs = [s["avg"] for s in stress]
        baseline = sorted(avgs)[len(avgs) // 2]
        recent = avgs[-7:]
        result["stress"] = {
            "baseline_avg": baseline,
            "recent_avg": round(sum(recent) / len(recent), 1),
            "elevated_days_last_7": sum(1 for v in recent if v > baseline * 1.5),
            "recent": stress[-7:],
        }
        result["stress_note"] = (
            "Baseline is this athlete's own median over the window, not an "
            "absolute. A sustained run above it is load, not background: treat "
            "it the way added training load would be treated."
        )

    result["note"] = (
        "Act on trends, not single readings. Three consecutive low mornings is "
        "a signal; one is not. No wearable composite score — readiness, Body "
        "Battery — has independent peer-reviewed validation as an absolute "
        "number; they are trend instruments."
    )
    return result
