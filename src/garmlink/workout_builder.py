"""Translate plain step dicts into Garmin Connect's workout schema.

Garmin's workout JSON is fussy in ways that are invisible until a workout
lands wrong on the watch: every step carries a numeric `stepTypeId` alongside
its key, the end-condition *value* is a sibling of `endCondition` rather than a
field inside it, and repeats are a distinct `RepeatGroupDTO` node type. An
earlier hand-rolled version of this got all three wrong.

So this builds on `garminconnect.workout`, which ships typed models and the
official ID tables, rather than re-deriving the schema. What is added here is
the part the library does not do: a recursive translation from a flat,
model-friendly step dict into that tree, including nested repeat groups.

The repeat shape is the point. `4 x (8 min hard + 3 min float)` is one repeat
group holding two children — not, as before, four hard steps followed by four
float steps.
"""

from __future__ import annotations

from typing import Any

from garminconnect.workout import (
    ConditionType,
    CyclingWorkout,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    StepType,
    StrengthWorkout,
    SwimmingWorkout,
    TargetType,
    WorkoutSegment,
    create_distance_interval_step,
    create_interval_step,
    create_repeat_group,
    create_strength_exercise_step,
)

__all__ = ["WorkoutBuildError", "build_workout", "SPORTS", "STEP_TYPES"]


class WorkoutBuildError(ValueError):
    """A step dict Garmin could not accept. Raised with a caller-fixable message."""


SPORTS: dict[str, type] = {
    "running": RunningWorkout,
    "cycling": CyclingWorkout,
    "swimming": SwimmingWorkout,
    "strength_training": StrengthWorkout,
}

# Garmin's own displayOrder happens to equal the type id for every step type
# (warmup=1 ... repeat=6), which is why one table serves both fields.
STEP_TYPES: dict[str, int] = {
    "warmup": StepType.WARMUP,
    "cooldown": StepType.COOLDOWN,
    "interval": StepType.INTERVAL,
    "recovery": StepType.RECOVERY,
    "rest": StepType.REST,
}

# Only used to estimate a duration for distance-based steps that carry no pace
# target. Garmin shows estimatedDurationInSecs but does not train off it, so a
# nominal speed is honest enough; a real pace target overrides it below.
_NOMINAL_SPEED_MS: dict[str, float] = {
    "running": 3.0,
    "cycling": 8.0,
    "swimming": 1.0,
    "strength_training": 1.0,
}

# Metres in one unit of the pace denominator, e.g. "4:30" per km -> 1000 m.
_PACE_DENOMINATORS: dict[str, float] = {
    "km": 1000.0,
    "mile": 1609.34,
    "100m": 100.0,
    "100y": 91.44,
}

_SECONDS_PER_REP = 3.0


class _Order:
    """Hands out the strictly increasing stepOrder Garmin requires.

    Every node in the tree — repeat groups included — consumes one number, and
    a repeat group must be numbered before its children (the library's own
    create_strength_set documents that ordering).
    """

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def _target_dict(type_id: int, key: str, one: float, two: float | None = None) -> dict:
    target: dict[str, Any] = {
        "workoutTargetTypeId": type_id,
        "workoutTargetTypeKey": key,
        "displayOrder": type_id,
        "targetValueOne": one,
    }
    if two is not None:
        target["targetValueTwo"] = two
    return target


def _parse_pace(value: Any, denominator: str) -> float:
    """Convert a 'mm:ss' pace into metres per second, which is what Garmin stores.

    A bare number is taken as m/s already, so a caller who knows the unit can
    bypass the conversion.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or ":" not in value:
        raise WorkoutBuildError(
            f"pace target {value!r} must be 'mm:ss' (per {denominator}) or a number in m/s"
        )
    minutes, _, seconds = value.partition(":")
    try:
        total = int(minutes) * 60 + float(seconds)
    except ValueError as exc:
        raise WorkoutBuildError(f"pace target {value!r} is not a valid 'mm:ss' time") from exc
    if total <= 0:
        raise WorkoutBuildError(f"pace target {value!r} must be greater than zero")
    metres = _PACE_DENOMINATORS.get(denominator)
    if metres is None:
        raise WorkoutBuildError(
            f"unknown pace_per {denominator!r}; choose from {sorted(_PACE_DENOMINATORS)}"
        )
    return metres / total


def _build_target(step: dict, sport: str) -> dict | None:
    """Return a targetType dict, or None to let the builder default to no.target."""
    target_type = str(step.get("target_type") or "open").lower()
    value = step.get("target_value")

    if target_type in ("open", "none", "") or value is None:
        return None

    if target_type in ("heart_rate_zone", "power_zone"):
        type_id = (
            TargetType.HEART_RATE_ZONE
            if target_type == "heart_rate_zone"
            else TargetType.POWER_ZONE
        )
        key = "heart.rate.zone" if target_type == "heart_rate_zone" else "power.zone"
        # A list is an explicit bpm/watt range; a bare number is a zone index.
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise WorkoutBuildError(
                    f"{target_type} range must be [low, high], got {value!r}"
                )
            low, high = sorted(float(v) for v in value)
            return _target_dict(type_id, key, low, high)
        return _target_dict(type_id, key, int(value))

    if target_type == "cadence":
        if isinstance(value, (list, tuple)):
            low, high = sorted(float(v) for v in value)
            return _target_dict(TargetType.CADENCE, "cadence", low, high)
        return _target_dict(TargetType.CADENCE, "cadence", float(value))

    if target_type == "pace":
        # Swimmers speak in per-100m, runners in per-km. Either can be overridden.
        default_denominator = "100m" if sport == "swimming" else "km"
        denominator = str(step.get("pace_per") or default_denominator).lower()
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise WorkoutBuildError(f"pace range must be [a, b], got {value!r}")
            low, high = sorted(_parse_pace(v, denominator) for v in value)
        else:
            speed = _parse_pace(value, denominator)
            # Garmin wants a band, not a point. +/-2% is tight enough to hold a
            # target and loose enough that the watch stops nagging on a hill.
            low, high = speed * 0.98, speed * 1.02
        return _target_dict(TargetType.PACE_ZONE, "pace.zone", low, high)

    raise WorkoutBuildError(
        f"unknown target_type {target_type!r}; choose from "
        "heart_rate_zone, power_zone, pace, cadence, open"
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _step_type_dict(key: str) -> dict:
    type_id = STEP_TYPES[key]
    return {"stepTypeId": type_id, "stepTypeKey": key, "displayOrder": type_id}


def _build_step(step: Any, order: _Order, sport: str) -> ExecutableStep | RepeatGroup:
    if not isinstance(step, dict):
        raise WorkoutBuildError(f"each step must be an object, got {type(step).__name__}")

    step_type = str(step.get("type") or "interval").lower()

    if step_type == "repeat":
        children = step.get("steps")
        if not isinstance(children, list) or not children:
            raise WorkoutBuildError("a 'repeat' step needs a non-empty 'steps' list")
        iterations = int(step.get("iterations", step.get("repeat", 1)))
        if iterations < 1:
            raise WorkoutBuildError(f"'iterations' must be at least 1, got {iterations}")
        # The group is numbered before its children, per Garmin's ordering.
        group_order = order.next()
        built = [_build_step(child, order, sport) for child in children]
        return create_repeat_group(iterations, built, group_order)

    if step_type not in STEP_TYPES:
        raise WorkoutBuildError(
            f"unknown step type {step_type!r}; choose from "
            f"{sorted(STEP_TYPES)} or 'repeat'"
        )

    my_order = order.next()
    target = _build_target(step, sport)

    reps = step.get("reps")
    if reps is not None:
        category = step.get("exercise") or step.get("category")
        if not category:
            raise WorkoutBuildError("a rep-based step needs an 'exercise' name")
        built = create_strength_exercise_step(
            str(category).upper().replace(" ", "_"),
            my_order,
            int(reps),
            exercise_name=str(step.get("exercise_name") or ""),
            weight_kg=(
                float(step["weight_kg"]) if step.get("weight_kg") is not None else None
            ),
        )
        built.stepType = _step_type_dict(step_type)
        return built

    distance = step.get("distance_meters")
    duration = step.get("duration_seconds")
    if distance is not None and duration is not None:
        raise WorkoutBuildError(
            "a step takes either 'distance_meters' or 'duration_seconds', not both"
        )

    # Both library helpers hardcode stepType to 'interval'; they are used here
    # for their end-condition wiring, then stamped with the real step type.
    if distance is not None:
        if float(distance) <= 0:
            raise WorkoutBuildError(f"'distance_meters' must be positive, got {distance}")
        built = create_distance_interval_step(float(distance), my_order, target)
    elif duration is not None:
        if float(duration) <= 0:
            raise WorkoutBuildError(
                f"'duration_seconds' must be positive, got {duration}"
            )
        built = create_interval_step(float(duration), my_order, target)
    else:
        raise WorkoutBuildError(
            f"step {step_type!r} needs one of 'duration_seconds', "
            "'distance_meters', or 'reps'"
        )

    built.stepType = _step_type_dict(step_type)
    return built


# ---------------------------------------------------------------------------
# Duration estimate
# ---------------------------------------------------------------------------

def _node_seconds(node: ExecutableStep | RepeatGroup, sport: str) -> float:
    if isinstance(node, RepeatGroup):
        inner = sum(_node_seconds(child, sport) for child in node.workoutSteps)
        return node.numberOfIterations * inner

    value = float(node.endConditionValue or 0)
    condition = (node.endCondition or {}).get("conditionTypeId")

    if condition == ConditionType.TIME:
        return value
    if condition == ConditionType.REPS:
        return value * _SECONDS_PER_REP
    if condition == ConditionType.DISTANCE:
        target = node.targetType or {}
        if target.get("workoutTargetTypeId") == TargetType.PACE_ZONE:
            one = float(target.get("targetValueOne") or 0)
            two = float(target.get("targetValueTwo") or one)
            speed = (one + two) / 2 if (one or two) else 0.0
        else:
            speed = 0.0
        if speed <= 0:
            speed = _NOMINAL_SPEED_MS.get(sport, 1.0)
        return value / speed
    return 0.0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_workout(sport: str, name: str, steps: list[dict]) -> dict:
    """Build a Garmin-ready workout payload from plain step dicts.

    Raises WorkoutBuildError with a message the caller can act on; the MCP tool
    turns that into an {"error": ...} result rather than a traceback.
    """
    sport_key = str(sport or "").lower().strip()
    workout_class = SPORTS.get(sport_key)
    if workout_class is None:
        raise WorkoutBuildError(
            f"unknown sport {sport!r}; choose from {sorted(SPORTS)}"
        )
    if not name or not str(name).strip():
        raise WorkoutBuildError("workout needs a name")
    if not isinstance(steps, list) or not steps:
        raise WorkoutBuildError("workout needs at least one step")

    order = _Order()
    built = [_build_step(step, order, sport_key) for step in steps]
    estimated = int(round(sum(_node_seconds(node, sport_key) for node in built)))

    sport_type = workout_class.model_fields["sportType"].default_factory()
    workout = workout_class(
        workoutName=str(name).strip(),
        estimatedDurationInSecs=estimated,
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=sport_type, workoutSteps=built)
        ],
    )
    return workout.to_dict()
