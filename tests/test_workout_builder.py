"""The workout builder must emit the shape Garmin actually accepts.

The previous implementation hand-rolled Garmin's workout JSON and got three
things wrong at once: no numeric `stepTypeId`, the end-condition value nested
inside `endCondition` instead of beside it, and — worst — a `repeat` that
wrapped a single step, so `4 x (hard + float)` came out as four hard steps
followed by four float steps. None of it was tested, and all of it is invisible
until a workout lands wrong on the watch.

These tests are offline: the builder is pure, so nothing here needs Garmin. The
one test that goes through the MCP tool patches the client the way
tests/test_tool_dispatch.py does, because real tokens on disk would otherwise
reach the live API.

Runs standalone (`python tests/test_workout_builder.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp import Client  # noqa: E402

import garmlink.deps as deps  # noqa: E402
from garmlink.workout_builder import WorkoutBuildError, build_workout  # noqa: E402


def _steps(workout):
    return workout["workoutSegments"][0]["workoutSteps"]


def _walk(nodes):
    """Every node in the tree, repeat groups and their children alike."""
    for node in nodes:
        yield node
        for child in node.get("workoutSteps", []) or []:
            yield from _walk([child])


INTERVALS = [
    {"type": "warmup", "duration_seconds": 600,
     "target_type": "heart_rate_zone", "target_value": 2},
    {"type": "repeat", "iterations": 4, "steps": [
        {"type": "interval", "duration_seconds": 480,
         "target_type": "heart_rate_zone", "target_value": 4},
        {"type": "recovery", "duration_seconds": 180,
         "target_type": "heart_rate_zone", "target_value": 2}]},
    {"type": "cooldown", "duration_seconds": 600},
]


def test_repeat_group_holds_its_children():
    """The regression that motivated the rewrite: one group, two children."""
    steps = _steps(build_workout("running", "4x8min", INTERVALS))
    groups = [s for s in steps if s["type"] == "RepeatGroupDTO"]
    assert len(groups) == 1, f"expected exactly one repeat group, got {len(groups)}"

    group = groups[0]
    assert group["numberOfIterations"] == 4, group["numberOfIterations"]
    children = group["workoutSteps"]
    assert len(children) == 2, f"repeat must hold interval AND recovery, got {children}"
    assert [c["stepType"]["stepTypeKey"] for c in children] == ["interval", "recovery"]


def test_every_step_carries_a_numeric_step_type_id():
    """The omission that made the old payloads malformed."""
    workout = build_workout("running", "4x8min", INTERVALS)
    for node in _walk(_steps(workout)):
        step_type = node.get("stepType")
        assert step_type, f"step {node.get('stepOrder')} has no stepType"
        assert isinstance(step_type.get("stepTypeId"), int), step_type


def test_step_order_is_unique_and_increasing():
    """Garmin rejects duplicate stepOrder; repeat groups consume one too."""
    orders = [n["stepOrder"] for n in _walk(_steps(build_workout("running", "x", INTERVALS)))]
    assert orders == sorted(orders), orders
    assert len(orders) == len(set(orders)), f"duplicate stepOrder: {orders}"


def test_end_condition_value_sits_beside_the_condition():
    """Not inside it — the old code put it in endCondition.conditionValue."""
    first = _steps(build_workout("running", "x", INTERVALS))[0]
    assert first["endConditionValue"] == 600.0, first
    assert "conditionValue" not in first["endCondition"], first["endCondition"]
    assert first["endCondition"]["conditionTypeId"] == 2  # ConditionType.TIME


def test_distance_steps_use_the_distance_condition():
    swim = build_workout("swimming", "8x50", [
        {"type": "warmup", "distance_meters": 200},
        {"type": "repeat", "iterations": 8, "steps": [
            {"type": "interval", "distance_meters": 50},
            {"type": "rest", "duration_seconds": 30}]},
    ])
    warmup = _steps(swim)[0]
    assert warmup["endCondition"]["conditionTypeId"] == 3, warmup  # DISTANCE
    assert warmup["endConditionValue"] == 200.0, warmup
    # A distance-based warmup must stay a warmup, not become an interval just
    # because the library's distance helper hardcodes that step type.
    assert warmup["stepType"]["stepTypeKey"] == "warmup", warmup["stepType"]


def test_pace_target_converts_to_metres_per_second():
    """Advertised as supported, silently dropped to no.target before this."""
    workout = build_workout("running", "tempo", [
        {"type": "interval", "duration_seconds": 1200,
         "target_type": "pace", "target_value": "4:00"},
    ])
    target = _steps(workout)[0]["targetType"]
    assert target["workoutTargetTypeId"] == 6, target  # TargetType.PACE_ZONE
    assert target["workoutTargetTypeKey"] == "pace.zone", target
    # 4:00/km is 240s per 1000m -> 4.1667 m/s, with a +/-2% band around it.
    assert 4.0 < target["targetValueOne"] < target["targetValueTwo"] < 4.3, target


def test_swim_pace_is_read_as_per_100m():
    """Swimmers speak in per-100m; the same string must not mean per-km here."""
    workout = build_workout("swimming", "threshold", [
        {"type": "interval", "distance_meters": 100,
         "target_type": "pace", "target_value": "2:00"},
    ])
    target = _steps(workout)[0]["targetType"]
    # 2:00 per 100m is 120s per 100m -> ~0.83 m/s, not the 8.3 m/s a per-km
    # reading would give.
    assert 0.8 < target["targetValueOne"] < 0.9, target


def test_estimated_duration_multiplies_through_repeats():
    workout = build_workout("running", "4x8min", INTERVALS)
    # 600 warmup + 4 * (480 + 180) + 600 cooldown
    assert workout["estimatedDurationInSecs"] == 600 + 4 * 660 + 600


def test_zone_and_range_targets_both_work():
    workout = build_workout("cycling", "sweet spot", [
        {"type": "interval", "duration_seconds": 720,
         "target_type": "power_zone", "target_value": [220, 235]},
        {"type": "recovery", "duration_seconds": 300,
         "target_type": "power_zone", "target_value": 1},
    ])
    ranged, zoned = _steps(workout)
    assert ranged["targetType"]["targetValueOne"] == 220.0
    assert ranged["targetType"]["targetValueTwo"] == 235.0
    assert zoned["targetType"]["targetValueOne"] == 1
    assert "targetValueTwo" not in zoned["targetType"], zoned["targetType"]


def test_strength_steps_are_rep_based():
    workout = build_workout("strength_training", "squats", [
        {"type": "repeat", "iterations": 4, "steps": [
            {"type": "interval", "reps": 8, "exercise": "SQUAT", "weight_kg": 60},
            {"type": "rest", "duration_seconds": 120}]},
    ])
    exercise = _steps(workout)[0]["workoutSteps"][0]
    assert exercise["endCondition"]["conditionTypeId"] == 10, exercise  # REPS
    assert exercise["endConditionValue"] == 8.0
    assert exercise["category"] == "SQUAT"
    assert exercise["weightValue"] == 60000.0, "Garmin stores weight in grams"


def test_sport_selects_the_right_sport_type():
    for sport, type_id in [
        ("running", 1), ("cycling", 2), ("swimming", 4), ("strength_training", 5),
    ]:
        workout = build_workout(sport, "x", [{"type": "interval", "duration_seconds": 60}])
        assert workout["sportType"]["sportTypeId"] == type_id, sport
        # Segment and workout must agree, or Garmin files it under the wrong sport.
        assert workout["workoutSegments"][0]["sportType"] == workout["sportType"], sport


def test_malformed_steps_raise_a_fixable_message():
    cases = [
        ({"type": "interval"}, "duration_seconds"),          # no end condition
        ({"type": "repeat", "iterations": 3}, "steps"),      # repeat with no children
        ({"type": "sprint", "duration_seconds": 60}, "step type"),
        ({"type": "interval", "duration_seconds": 60,
          "target_type": "vibes", "target_value": 3}, "target_type"),
        ({"type": "interval", "duration_seconds": 60,
          "distance_meters": 400}, "not both"),
        ({"type": "interval", "reps": 5}, "exercise"),
    ]
    for step, expected in cases:
        try:
            build_workout("running", "x", [step])
        except WorkoutBuildError as exc:
            assert expected in str(exc), f"{step} -> {exc!r}, expected {expected!r}"
        else:
            raise AssertionError(f"{step} should not have built")

    for sport in ("", "triathlon"):
        try:
            build_workout(sport, "x", [{"type": "interval", "duration_seconds": 60}])
        except WorkoutBuildError as exc:
            assert "sport" in str(exc), exc
        else:
            raise AssertionError(f"sport {sport!r} should not have built")


class _FakeClient:
    """Captures the payload the tool uploads."""

    def __init__(self):
        self.uploaded = None

    async def call(self, method_name, *args, ttl=None, cache=True, **kwargs):
        if method_name == "upload_workout":
            self.uploaded = args[0]
        return {"workoutId": 123}

    def invalidate(self, method_name):
        pass

    def close(self):
        pass


def test_create_workout_tool_uploads_and_reports_errors():
    """A bad step is an {"error": ...} result, not a traceback through MCP."""
    import garmlink.server as server

    fake = _FakeClient()
    original = server.GarminClient
    server.GarminClient = lambda **kwargs: fake

    async def run():
        async with Client(server.mcp) as c:
            good = await c.call_tool("create_workout", {
                "sport": "running", "name": "4x8min", "steps": INTERVALS,
            })
            bad = await c.call_tool("create_workout", {
                "sport": "running", "name": "broken",
                "steps": [{"type": "interval"}],
            })
            return good.data, bad.data

    try:
        good, bad = asyncio.run(run())
    finally:
        server.GarminClient = original
        deps.set_client(None)

    assert good == {"workoutId": 123}, good
    assert fake.uploaded is not None, "the tool never uploaded anything"
    assert fake.uploaded["workoutName"] == "4x8min"
    assert isinstance(bad, dict) and "error" in bad, bad
    assert "duration_seconds" in bad["error"], bad


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {str(exc)[:200]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
