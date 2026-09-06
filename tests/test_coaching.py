"""The computed coaching primitives, and the mistakes they exist to prevent.

Every test here pins a specific way the old prose-and-raw-payload approach got a
number wrong: bike volume read low because `virtual_ride` matched no bike
substring, a sleep window reported as fully covered when a third of it was
empty, and a threshold effect averaged away into a passing figure.

Runs standalone (`python tests/test_coaching.py`) or under pytest. Fixtures
carry invented physiology: this is a public repository.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from garmlink.sports import (  # noqa: E402
    covered_window,
    normalize_sport,
    pounds,
    summarize_activity,
)
from garmlink.tools.coaching import (  # noqa: E402
    SLEEP_THRESHOLD_SECONDS,
    _CEILING_WARNING,
    _VOLUME_WARNING,
    _capability,
    _running_volume,
    _shape,
)


def _activity(type_key, start, *, minutes=30.0, metres=None, hr=None):
    act = {
        "activityId": 1,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start,
        "duration": minutes * 60,
    }
    if metres is not None:
        act["distance"] = metres
    if hr is not None:
        act["averageHR"] = hr
    return act


# ---------------------------------------------------------------------------
# Sport vocabulary
# ---------------------------------------------------------------------------

def test_indoor_rides_count_as_cycling():
    """Read literally, `virtual_ride` contains none of cycling/bike/biking, and
    a five-session bike week reports as one 31-minute session."""
    assert normalize_sport("virtual_ride") == "cycling"
    assert normalize_sport("indoor_cycling") == "cycling"
    assert normalize_sport("cycling") == "cycling"


def test_sport_variants_normalise():
    assert normalize_sport("lap_swimming") == "swimming"
    assert normalize_sport("trail_running") == "running"
    assert normalize_sport("strength_training") == "strength"
    assert normalize_sport("hiking") == "hiking"
    # A brick is one session but is not filed under a single sport.
    assert normalize_sport("multi_sport") == "multi_sport"
    assert normalize_sport(None) == "other"


def test_strength_carries_no_distance():
    """Strength counts toward adherence but has no miles; reporting 0.0 miles
    for it drags any pace or distance total sideways."""
    summary = summarize_activity(_activity("strength_training", "2026-09-01 18:00:00"))
    assert summary["sport"] == "strength"
    assert "distance_mi" not in summary


def test_summary_converts_to_miles_not_metres():
    summary = summarize_activity(
        _activity("running", "2026-08-21 07:00:00", metres=3318.0, hr=145)
    )
    assert summary["distance_mi"] == 2.06, summary
    assert summary["avg_hr"] == 145


def test_summary_drops_the_device_metadata():
    """Garmin returns 81 fields per activity; 20 of them is ~88,000 characters."""
    raw = _activity("running", "2026-08-21 07:00:00", metres=3318.0, hr=145)
    raw.update({f"deviceField{i}": i for i in range(60)})
    summary = summarize_activity(raw)
    assert len(summary) < 12, summary
    assert not any(k.startswith("deviceField") for k in summary)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def test_set_weight_converts_from_grams():
    """Garmin stores lifting load in grams: a 50lb curl arrives as 22687, which
    looks like a plausible weight in no unit at all."""
    assert pounds(22687) == 50.0
    assert pounds(54437) == 120.0
    assert pounds(None) is None
    assert pounds(0) is None


# ---------------------------------------------------------------------------
# The data horizon
# ---------------------------------------------------------------------------

def test_a_lookback_past_the_first_data_is_flagged():
    """Garmin answers a lookback reaching before the device with less data, not
    an error, so a rate over the requested window is confidently wrong and low."""
    window = covered_window(
        ["2026-08-03", "2026-09-06"], "2026-06-08", "2026-09-06"
    )
    assert window["covered"]["start"] == "2026-08-03"
    assert "warning" in window, window
    assert "2026-08-03" in window["warning"]


def test_a_fully_covered_window_carries_no_warning():
    window = covered_window(
        ["2026-08-10", "2026-09-06"], "2026-08-10", "2026-09-06"
    )
    assert "warning" not in window, window


def test_an_empty_window_is_unknown_not_zero():
    window = covered_window([], "2026-06-01", "2026-06-30")
    assert window["covered"] is None
    assert "not zero" in window["warning"]


# ---------------------------------------------------------------------------
# Intensity distribution
# ---------------------------------------------------------------------------

def test_mostly_hard_training_reads_as_inverted():
    assert _shape({"easy": 30, "moderate": 40, "hard": 30}).startswith("inverted")


def test_pyramidal_and_polarized_are_distinguished():
    assert _shape({"easy": 80, "moderate": 5, "hard": 15}).startswith("polarized")
    assert _shape({"easy": 70, "moderate": 20, "hard": 10}).startswith("pyramidal")


def test_a_large_middle_reads_as_threshold_heavy():
    assert _shape({"easy": 55, "moderate": 15, "hard": 30}).startswith("threshold")


# ---------------------------------------------------------------------------
# Sleep as a threshold, not an average
# ---------------------------------------------------------------------------

def _short_nights(hours: list[float]) -> int:
    return sum(1 for h in hours if h * 3600 < SLEEP_THRESHOLD_SECONDS)


def test_an_average_hides_the_threshold_effect():
    """8h and 6h average to a passing 7h and are not equivalent — the evidence
    is a threshold, which is why the count is reported rather than the mean."""
    hours = [8.0, 6.0, 8.0, 6.0, 8.0, 6.0]
    assert sum(hours) / len(hours) == 7.0, "fixture must average exactly 7h"
    assert _short_nights(hours) == 3


def test_three_short_nights_in_seven_holds_a_progression():
    assert _short_nights([6.5, 6.0, 6.9, 7.5, 8.0, 7.2, 7.4]) >= 3
    assert _short_nights([7.5, 7.1, 6.9, 7.5, 8.0, 7.2, 7.4]) < 3


def test_the_real_short_stretch_is_counted():
    """The Acadia nights, as Garmin recorded them."""
    assert _short_nights([3.0, 5.3, 4.4, 6.7]) == 4


# ---------------------------------------------------------------------------
# Capability is not volume
# ---------------------------------------------------------------------------

def test_the_ramp_ceiling_ships_with_its_own_warning():
    """The trailing longest run is a tempting number to reason from and a
    misleading one alone. It is not allowed to travel without the caveat."""
    assert "RATE OF INCREASE" in _CEILING_WARNING
    assert "NOT a ceiling on what this athlete can race" in _CEILING_WARNING
    assert "sparse logging" in _CEILING_WARNING.lower()


def test_capability_reads_race_predictions_not_volume():
    cap = _capability(
        {"time5K": 1356, "time10K": 2981, "timeHalfMarathon": 6853,
         "timeMarathon": 15533},
        {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 51.9}}},
    )
    assert cap["predicted_half_marathon"] == "1:54:13", cap
    assert cap["predicted_5k"] == "0:22:36", cap
    assert cap["vo2max"] == 51.9, cap


def test_missing_capability_data_is_said_rather_than_inferred():
    """Absent predictions must not license falling back to volume."""
    cap = _capability(None, None)
    assert "say so" in cap["note"]
    assert "volume alone" in cap["note"]


def test_prompts_require_race_predictions_before_a_feasibility_claim():
    """The rule that would have caught the run/walk mistake, pinned in the text
    every cross-sport coaching prompt shares."""
    from garmlink.prompts import _PLAN
    assert "get_race_predictions" in _PLAN
    assert "Never judge what the athlete is capable of from training volume" in _PLAN
    # The two questions have to stay named and separate.
    assert "safe rate of increase" in _PLAN.lower()
    assert "capability ceiling" in _PLAN.lower()


# ---------------------------------------------------------------------------
# Weekly running volume — the number that predicts the result
# ---------------------------------------------------------------------------

# The block as rewritten, first eight weeks, as if executed.
_REBUILT = {"2026-W37": 16.7, "2026-W38": 17.7, "2026-W39": 18.3, "2026-W40": 16.3,
            "2026-W41": 18.9, "2026-W42": 21.7, "2026-W43": 22.4, "2026-W44": 19.8}
# The block before this session: 3 runs a week, peaking at 18.5 mi.
_UNDER_DOSED = {"2026-W37": 11.7, "2026-W38": 12.7, "2026-W39": 13.7, "2026-W40": 11.7,
                "2026-W41": 14.2, "2026-W42": 14.7, "2026-W43": 15.2, "2026-W44": 13.2}


def test_volume_is_bucketed_by_week_in_miles():
    """Garmin answers in metres; the evidence is in miles per week."""
    out = _running_volume(dict(_REBUILT), target=0)
    assert out["peak_week_miles"] == 22.4, out
    assert set(out["by_week"]) == set(_REBUILT), out
    # No target passed means no verdict invented.
    assert "verdict" not in out, out


def test_an_under_dosed_plan_is_caught():
    """The failure this exists to prevent: a block ~40% short of its own goal,
    presented as finished because nothing computed weekly mileage."""
    out = _running_volume(dict(_UNDER_DOSED), target=20.0)
    assert out["verdict"].startswith("UNDER TARGET"), out
    assert out["shortfall_miles_per_week"] > 5, out
    assert out["weeks_at_or_above_target"] == 0, out


def test_the_shortfall_names_the_plan_not_the_athlete():
    out = _running_volume(dict(_UNDER_DOSED), target=20.0)
    assert "plan is not prescribing enough" in out["verdict"], out


def test_a_sufficient_plan_is_not_flagged():
    out = _running_volume({f"2026-W{i}": 26.0 for i in range(37, 45)}, target=20.0)
    assert out["verdict"] == "At or above the plan's weekly target.", out
    assert "shortfall_miles_per_week" not in out, out


def test_direction_separates_a_ramp_from_a_collapse():
    rising = _running_volume(dict(_REBUILT), target=0)
    falling = _running_volume(
        {k: v for k, v in zip(_REBUILT, reversed(list(_REBUILT.values())))}, target=0
    )
    assert rising["direction"] == "rising", rising
    assert falling["direction"] == "falling", falling


def test_no_running_is_not_reported_as_a_number():
    out = _running_volume({}, target=20.0)
    assert out["by_week"] == {}
    assert "verdict" not in out, out
    assert "low fitness" in out["note"]


def test_volume_shortfall_ships_with_its_own_warning():
    """Mirrors _CEILING_WARNING: the number must not travel without the caveat
    that a shortfall indicts the plan rather than the athlete."""
    assert "finding about the PLAN" in _VOLUME_WARNING
    assert "not to lower the goal" in _VOLUME_WARNING
    assert "no increase in injury risk" in _VOLUME_WARNING


def test_prompts_lead_with_effectiveness_not_risk():
    """The framing error that produced an under-dosed block: every heuristic was
    about avoiding harm and none about what makes training work."""
    from garmlink.prompts import _PLAN
    assert "What makes training effective" in _PLAN
    assert "strongest modifiable predictor" in _PLAN
    assert "finding about the plan, not the athlete" in _PLAN.lower()
    # And it has to come before the risk material, or it is decoration.
    assert _PLAN.index("What makes training effective") < _PLAN.index(
        "Never judge what the athlete is capable of"
    )


def _run_all() -> bool:
    passed = failed = 0
    for name in sorted(globals()):
        if not name.startswith("test_"):
            continue
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {name}: {exc}")
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
    print(f"\n{passed}/{passed + failed} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if _run_all() else 1)
