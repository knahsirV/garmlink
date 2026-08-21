"""Coaching workflows, exposed as MCP prompts.

These lived in `.claude/skills/*.md`, which only Claude Code reads — and in a
layout it does not load, so they never ran. As prompts they ship with the
server: Claude Desktop surfaces them in its prompt menu, Claude Code as
`/mcp__garmin__<name>`, and anything else speaking MCP gets them too. They also
cost nothing until invoked, unlike tool descriptions, which are resident on
every request.

Keep tool references in backticks — tests/test_prompts.py checks that every
backticked tool name is one the server actually exposes.
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("coaching")


@mcp.prompt(
    description="Daily readiness briefing — how recovered am I, should I train today?"
)
def morning_check(date: str = "today") -> str:
    """Run a complete daily readiness assessment."""
    return f"""Run a complete daily readiness assessment for {date} using the Garmin tools.

## Steps

1. Resolve {date} to YYYY-MM-DD format.
2. Call `get_wellness_snapshot` with that date — fetches sleep, HRV, body
   battery, and stress in one call.
3. Call `suggest_recovery` with that date — returns a training intensity
   recommendation.
4. Call `get_training_readiness` with that date — Garmin's own readiness score.

## Output format

Present the results as a brief, scannable morning report:

**Morning Check — [date]**
- Sleep: [score/duration from sleep data]
- HRV: [status from HRV data]
- Body Battery: [current level]
- Stress: [average stress level]
- Garmin Readiness: [score/100]
- **Recommendation**: [rest / easy / normal / push] — [one sentence reasoning]

Keep it concise. The user wants a quick answer, not a wall of JSON."""


@mcp.prompt(
    description="Weekly training review — how did my training week go?"
)
def analyze_week(end_date: str = "today") -> str:
    """Structured weekly training review."""
    return f"""Provide a structured weekly training review using Garmin data, for the
seven days ending {end_date}.

## Steps

1. Resolve the date range: the 7 days ending {end_date}, in YYYY-MM-DD format.
2. Call `get_volume_by_sport` for the 7-day range — time, distance and sessions
   per sport.
3. Call `get_training_load_trend` with the end date — acute vs chronic load.
4. Call `get_hrv_trend` with the start and end of the week — HRV over the week.
5. Call `get_weekly_comparison` for 'steps' and 'sleep_score' — week-over-week
   context.

## Output format

**Weekly Training Summary — [start] to [end]**

**Volume by Sport**
- Run: [X sessions, Y km, Z hours]
- Bike: [X sessions, Y km, Z hours]
- Swim: [X sessions, Y km, Z hours]
- Strength: [X sessions]

**Load & Recovery**
- Training load: [acute/chronic values and status]
- HRV trend: [improving / stable / declining]
- Sleep vs last week: [delta]

**Key Insight**: [1-2 sentence observation — e.g. "Heavy bike week, run volume
low, good recovery trend"]
**Next Week Suggestion**: [1-2 sentences based on load status]"""


@mcp.prompt(
    description="Pre-race fitness assessment — am I ready for my target event?"
)
def race_readiness(event: str = "", event_date: str = "") -> str:
    """Assess current fitness against a target race."""
    target = event or "the target event"
    when = f" on {event_date}" if event_date else ""
    ask = (
        ""
        if event and event_date
        else "\n1. Ask the user what race/event, what distance, and when it is.\n"
    )
    return f"""Assess current fitness against {target}{when}.
{ask}
## Steps

1. Call `get_triathlon_fitness_snapshot` — cross-sport fitness overview.
2. Call `get_race_predictions` — predicted running race times.
3. Call `get_training_load_trend` with today's date — current load status.
4. Call `get_cycling_ftp` — current cycling power.
5. Call `get_endurance_score` with today's date — aerobic capacity.

## Output format

**Race Readiness: {target}**

**Current Fitness**
- Running: [VO2max estimate, predicted race times]
- Cycling: [FTP watts]
- Swimming: [most recent session pace/SWOLF if available]
- Endurance score: [Garmin score]

**Training Load**
- Current status: [productive/maintaining/peaking/recovery]
- Recommendation: [whether to taper, maintain, or build before the race]

**Gaps to Address**: [any sport or metric that looks underprepared]"""


@mcp.prompt(
    description="Guided structured workout builder — design a session and push it to Garmin"
)
def create_workout_guide(
    sport: str = "", goal: str = "", duration_minutes: str = ""
) -> str:
    """Guide the user through building a structured workout."""
    known = [
        f"- Sport: {sport}" if sport else None,
        f"- Goal: {goal}" if goal else None,
        f"- Duration: {duration_minutes} minutes" if duration_minutes else None,
    ]
    known = [k for k in known if k]
    prefill = ("\nAlready specified:\n" + "\n".join(known) + "\n") if known else ""

    return f"""Guide the user through building a structured workout and pushing it to
Garmin Connect.
{prefill}
## Steps

1. Ask for anything not already specified:
   - What sport? (running / cycling / swimming / strength_training)
   - What is the goal? (e.g. threshold intervals, Z2 endurance, sprint work)
   - How long total? (approximate duration in minutes)

2. Based on their answers, design the workout steps:
   - **Warmup**: 10-15 min, target_type "heart_rate_zone", target_value 2
   - **Main set**: design based on goal (e.g. 4x8min at zone 4 with 3min
     recovery)
   - **Cooldown**: 5-10 min, target_type "heart_rate_zone", target_value 1

3. Show the user the workout structure and ask for confirmation before pushing.

4. Call `create_workout` with:
   - sport: the sport string
   - name: a descriptive name (e.g. "4x8min Threshold Intervals")
   - steps: list of step dicts with type, duration_seconds, target_type,
     target_value, repeat

5. Confirm the workout was created and tell the user to sync their Garmin
   device.

## Step types

- warmup, interval, recovery, cooldown, rest
- target_type: heart_rate_zone (1-5), power_zone (1-7), or open"""
