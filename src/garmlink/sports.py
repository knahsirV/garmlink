"""Garmin activity vocabulary, units, and the compact shapes coaching reads.

Two things live here because they were previously written down as prose in
`prompts.py` and re-derived, differently, in `tools/insights.py`.

The sport mapping is the first. Garmin files an indoor ride under
`virtual_ride`, which contains none of "cycling", "bike" or "biking", so a
substring test on those keys reports a five-session bike week as one 31-minute
session — wrong, and wrong low. A prompt can tell the model that; it cannot stop
a tool from computing it. Now the tools share this table.

The units are the second. The server instructions require miles and min/mile,
because Garmin answers in metres and metres per second. Strength has the same
problem and no rule: set weight comes back in grams, so a 50lb curl reads 22687.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "METRES_PER_MILE",
    "GRAMS_PER_POUND",
    "normalize_sport",
    "is_cardio",
    "miles",
    "pace_per_mile",
    "pounds",
    "summarize_activity",
    "covered_window",
]

METRES_PER_MILE = 1609.34
GRAMS_PER_POUND = 453.59237

# Matched against Garmin's `activityType.typeKey`, longest first so that
# `virtual_ride` and `indoor_cycling` cannot be shadowed by a looser rule. The
# left-hand strings are substrings, because Garmin varies the prefix
# (`lap_swimming` vs `swimming`, `trail_running` vs `running`).
_SPORT_KEYS: tuple[tuple[str, str], ...] = (
    ("virtual_ride", "cycling"),
    ("indoor_cycling", "cycling"),
    ("road_biking", "cycling"),
    ("mountain_biking", "cycling"),
    ("cycling", "cycling"),
    ("biking", "cycling"),
    ("bike", "cycling"),
    ("lap_swimming", "swimming"),
    ("open_water_swimming", "swimming"),
    ("swimming", "swimming"),
    ("swim", "swimming"),
    ("trail_running", "running"),
    ("treadmill_running", "running"),
    ("running", "running"),
    ("strength_training", "strength"),
    ("strength", "strength"),
    ("weight", "strength"),
    ("gym", "strength"),
    ("hiking", "hiking"),
    ("walking", "walking"),
    # A brick or a race. One session, but it is not filed under a single sport
    # and its legs have to be read from the splits, so it stays distinct.
    ("multi_sport", "multi_sport"),
)

# Distance and intensity distribution are meaningless for these, but they are
# still sessions and still cost recovery, so they count toward adherence.
_NON_DISTANCE = {"strength"}


def normalize_sport(type_key: str | None) -> str:
    """Map a Garmin `typeKey` onto a coaching sport name."""
    key = (type_key or "").lower()
    for needle, sport in _SPORT_KEYS:
        if needle in key:
            return sport
    return key or "other"


def is_cardio(sport: str) -> bool:
    """Whether distance and pace mean anything for this sport."""
    return sport not in _NON_DISTANCE


def miles(metres: float | None) -> float | None:
    return None if not metres else round(metres / METRES_PER_MILE, 2)


def pace_per_mile(speed_ms: float | None) -> str | None:
    """Metres per second to a 'm:ss' mile pace."""
    if not speed_ms:
        return None
    seconds = METRES_PER_MILE / speed_ms
    return f"{int(seconds // 60)}:{int(round(seconds % 60)):02d}"


def pounds(grams: float | None) -> float | None:
    return None if not grams else round(grams / GRAMS_PER_POUND, 1)


def summarize_activity(act: dict) -> dict:
    """Project one Garmin activity onto the fields coaching actually reads.

    Garmin returns 81 fields per activity. A 20-activity call is ~88,000
    characters of mostly device metadata, which buries the handful of numbers a
    coach leads with and crowds out the rest of the analysis.
    """
    sport = normalize_sport((act.get("activityType") or {}).get("typeKey"))
    duration = act.get("duration") or 0
    summary = {
        "activity_id": act.get("activityId"),
        "start": act.get("startTimeLocal"),
        "sport": sport,
        "name": act.get("activityName"),
        "duration_min": round(duration / 60, 1) if duration else None,
        "avg_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "training_load": _round(act.get("activityTrainingLoad")),
        "aerobic_te": _round(act.get("aerobicTrainingEffect")),
        "anaerobic_te": _round(act.get("anaerobicTrainingEffect")),
    }
    if is_cardio(sport):
        summary["distance_mi"] = miles(act.get("distance"))
        summary["avg_pace_per_mile"] = pace_per_mile(act.get("averageSpeed"))
    return {k: v for k, v in summary.items() if v is not None}


def _round(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)


def covered_window(dates: list[str], requested_start: str, requested_end: str) -> dict:
    """Describe what a lookback actually covered, versus what it asked for.

    Garmin holds nothing before the watch was first worn, and it answers a
    request that reaches further back with fewer activities rather than with an
    error. A sessions-per-week figure computed over the requested window is then
    confidently wrong and low. Every tool that looks back reports this.
    """
    if not dates:
        return {
            "requested": {"start": requested_start, "end": requested_end},
            "covered": None,
            "warning": (
                "No data in this window. Garmin holds nothing from before the "
                "device was first used — a wider lookback returns less, not an "
                "error, so treat any rate computed here as unknown, not zero."
            ),
        }
    first, last = min(dates), max(dates)
    window = {
        "requested": {"start": requested_start, "end": requested_end},
        "covered": {"start": first, "end": last},
    }
    if first > requested_start:
        window["warning"] = (
            f"Data starts at {first}, after the requested {requested_start}. "
            "Garmin holds nothing from before the device was first used, so "
            "rates over the requested window understate reality. Report the "
            "covered window, not the requested one."
        )
    return window
