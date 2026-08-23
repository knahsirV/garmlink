"""Coaching workflows, exposed as MCP prompts.

These lived in `.claude/skills/*.md`, which only Claude Code reads — and in a
layout it does not load, so they never ran. As prompts they ship with the
server: Claude Desktop surfaces them in its prompt menu, Claude Code as
`/mcp__garmin__<name>`, and anything else speaking MCP gets them too. They also
cost nothing until invoked, unlike tool descriptions, which are resident on
every request.

Two shapes live here. Cross-sport prompts (`analyze_week`, `load_check`,
`build_training_block`, `race_readiness`) always report swim, bike and run
together, because the thing they measure does not partition by sport — a hard
bike week is why the run legs are flat, and an acute:chronic ratio is one
whole-body number. Per-session prompts (`session_debrief`,
`create_workout_guide`) take a sport, because the *tools* differ: a swim needs
`get_swim_activity_detail`, a run needs `get_running_dynamics`.

Anything that writes to Garmin shows the change and waits for an explicit yes.

Keep tool references in backticks — tests/test_prompts.py checks that every
backticked tool name is one the server actually exposes.
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("coaching")


# Repeated verbatim into every prompt that reports across sports. The user is
# training for triathlon but has not started swimming yet, so an empty swim
# section is the single most informative thing these reports can say — as long
# as it says it out loud instead of rendering a blank line.
_SWIM_GAP = """
## Reporting the swim gap

If there are no swim sessions in the window, do not render an empty section and
do not silently drop it. Say so explicitly — "Swim: no sessions in this window"
— and treat it as a finding, because triathlon is the goal and swimming is the
sport not yet started. Mention it once, in its own line. Do not lecture.
"""


# Verified against real payloads from this account. `get_volume_by_sport` keys
# by Garmin's activity type, which does not match how a triathlete counts weeks:
# indoor rides come back as `virtual_ride`, outdoor as `cycling`, and a
# brick or race is a single `multi_sport` entry that contains all of its legs.
# Reading those keys literally reports one 31-minute bike week when the real
# number is five sessions and nearly four hours.
_SPORT_KEYS = """
## Reading the sport keys

`get_volume_by_sport` splits by Garmin's raw activity type. Before reporting,
combine them the way the training actually works:

- **Bike** = `cycling` (outdoor) + `virtual_ride` (indoor/Zwift). Report the
  combined figure. Split it out only if the indoor/outdoor mix is the point.
- **Multisport** = a `multi_sport` entry is one brick or race containing a run,
  a bike and their transitions. Count it once as a session, but do not file its
  time under a single sport — call it out separately.
- **Swim** = `swimming` or `lap_swimming`. Both, if both appear.

Never report bike volume from the `cycling` key alone. It will be wrong, and
wrong low.
"""


# Indoor rides are executed in Zwift under ERG, not off the watch, so a custom
# Garmin workout for one is dead weight. Zwift's own library is the right source
# — but it churns: the v1.49 reorg (October 2023) cut 58 workout collections to
# 14 and deleted the rest. A model naming a workout from memory has a real
# chance of naming one Zwift removed years ago, which is the whole reason the
# confirmation step below is not optional.
_ZWIFT = """
## Indoor bike sessions: use Zwift's library, and confirm the workout is current

Indoor rides are done in Zwift with ERG holding the power, so do **not** build a
custom Garmin workout for one. Recommend a real Zwift workout instead. Build a
Garmin workout only for outdoor rides. If it is not clear which one this is,
ask before designing anything.

**Confirm the workout exists before naming it.** Search the web for it on
whatsonzwift.com — search rather than fetching the page, which refuses direct
requests. Then check two things:

1. The workout genuinely exists under that exact name.
2. The collection or plan holding it is **not** marked `(legacy)`. On
   whatsonzwift, `(legacy)` means Zwift deleted that collection in the October
   2023 library reorg — those workouts are gone from the game. The same name can
   appear in both a current and a legacy collection ("SST (Med)" is exactly this
   case), so confirming the name alone proves nothing. Confirm the collection.

**Never name a workout you could not confirm.** Recalling a Zwift workout is not
evidence it still ships. If the search does not confirm a current collection,
pick a different workout and confirm that one instead.

When recommending, give: the exact workout name, the current collection or
training plan it lives in, its duration, and one line on what it actually does
(the interval structure and the percentage of FTP it targets) so it can be
judged against the session's purpose.

Past Zwift rides are a good source of candidates: `get_activities` returns them
as `virtual_ride` entries named "Zwift - <workout>", so the workout names the
user has already ridden are visible there. Confirm those are still current too —
having ridden one in the past is not proof it survived a reorg.

**If web search is unavailable**, say so plainly and do not guess a name.
Describe the session's structure instead (duration, interval shape, target
percentage of FTP) and let the user pick the matching workout inside Zwift.
"""


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
**Next Week Suggestion**: [1-2 sentences based on load status]
{_SPORT_KEYS}{_SWIM_GAP}"""


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

**Gaps to Address**: [any sport or metric that looks underprepared]
{_SWIM_GAP}"""


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@mcp.prompt(
    description="Deep single-session review — how well did I actually execute that workout?"
)
def session_debrief(activity_id: str = "", sport: str = "") -> str:
    """Split-by-split review of one activity, branching on its sport."""
    if activity_id:
        find = f"""1. The activity is {activity_id}. Call `get_activity` with that ID."""
    else:
        find = """1. Call `get_activities` with limit 5 and let the user pick, or take the
   most recent one if the intent is obviously "my last session". Then call
   `get_activity` with that ID."""

    sport_hint = (
        f"\nThe user says this is a {sport} session; still confirm against the "
        "activity's own type before branching.\n"
        if sport
        else ""
    )

    return f"""Review one training session in depth — not a summary of what the watch
already shows, but a read on how well it was executed.
{sport_hint}
## Steps

{find}
2. Read `activityType.typeKey` off that result and branch on it. Do not ask the
   user what sport it was — the activity says so.
3. Call `get_activity_splits` with the same ID. This is the core of the
   analysis; everything below reads off the split list.
4. Then, by sport:
   - **Running**: `get_running_dynamics` for cadence, ground contact time,
     vertical oscillation and stride length. Also call `get_lactate_threshold`
     so intensity can be judged against a real threshold rather than a guess.
   - **Cycling**: `get_cycling_ftp` and `get_cycling_power_zones`, so power is
     read against the user's own zones.
   - **Swimming**: `get_swim_activity_detail` for per-length stroke counts,
     SWOLF and rest intervals.
   - **Strength**: `get_strength_sets` for the set/rep/weight breakdown.
   - **Multisport** (`multi_sport`): this is a brick or a race, and it is a
     *parent* activity — its own summary has no splits, but `get_activity_splits`
     returns each leg as a lap, transitions included. Expect a pattern like
     run / T1 / bike / T2 / run, where the transitions are the very short,
     slow laps. Identify each leg by its distance and speed, then analyse the
     legs separately. Also call `get_brick_analysis` over a window containing
     this date for context on how bike-to-run has been trending.

     The run **after** the bike against the run **before** it is the single most
     informative comparison in a brick — that fade is what triathlon is actually
     about. Report both paces and both average HRs, and name the drop.

## What to analyse

**Pacing shape.** Compare first half against second half across the splits.
Name it: negative split (second half faster — well judged), positive split
(faded), or even. Give the actual numbers, not just the label.

**Aerobic decoupling.** For any steady effort longer than about 30 minutes,
compute the pace-to-heart-rate ratio for the first half and the second half,
then the percentage drift between them. Under ~5% means aerobic fitness held.
Over ~5% means either the effort was above the aerobic ceiling, or fatigue or
heat was already in the system. State the number and say which reading fits.
Skip this for interval sessions — the ratio is meaningless when the effort is
deliberately varying.

**Interval consistency.** If the splits show repeats, compare them to each
other. Was rep 5 slower than rep 1, and by how much? Consistency across reps is
the point of an interval session; a big fade means it started too hard.

**Execution vs intent.** If a workout was scheduled for this day, call
`get_scheduled_workouts` for that month and compare what was planned against
what happened. If nothing was planned, infer the intent from the shape of the
session and say you are inferring it.

**Sport-specific reads.**
- Running: did cadence hold as pace dropped? Rising ground contact time late in
  a session is a fatigue signature worth naming.
- Cycling: time in each power zone, and whether that matches the session's
  apparent intent.
- Swimming: did SWOLF climb across the set? That is technique falling apart
  under fatigue, which matters more than the pace at this stage.
- Strength: volume load per exercise (sets x reps x weight), and whether reps
  held across sets.

## Output format

**Session Debrief — [activity name], [date]**
[one line: distance, time, average pace or power, average HR]

**Pacing**: [shape, with first-half vs second-half numbers]
**Decoupling**: [percentage and what it means — or "n/a, interval session"]
**Consistency**: [rep-by-rep read, if intervals]
**[Sport] specifics**: [the sport-specific read]

**What went well**: [one or two concrete things]
**What to fix next time**: [one or two concrete, actionable things]

Be direct about a session that was executed badly. A debrief that praises
everything is useless. Ground every claim in a number from the splits."""


@mcp.prompt(
    description="Training load and injury-risk check — am I ramping too fast?"
)
def load_check(date: str = "today") -> str:
    """Acute:chronic load, ramp rate and tolerance check across all three sports."""
    return f"""Assess training load and injury risk as of {date}. The question being
answered is "am I building sustainably, or am I about to get hurt?"

## Steps

1. Resolve {date} to YYYY-MM-DD, and derive a 28-day window ending there.
2. Call `get_training_load_trend` with that date — acute (7-day) vs chronic
   (28-day) load. This is the primary signal.
3. Call `get_training_status` with that date — Garmin's own verdict
   (productive / maintaining / overreaching / unproductive / detraining).
4. Call `get_running_tolerance` for the 28-day window — how much running load
   the user is currently adapted to absorb. Garmin only populates this once it
   has enough running history, so an empty result is normal, not an error: say
   "not yet established" and lean on the acute:chronic ratio instead. Do not
   stall or retry.
5. Call `get_volume_by_sport` twice: once for the most recent 7 days, once for
   the 7 days before that. The ratio between them is the week-over-week ramp.
6. Call `get_hrv_trend` and `get_vo2max_trend` for the 28-day window — the
   confirming signals. Load rising while HRV falls is the combination that
   matters.
7. Call `get_endurance_score` with that date for aerobic context.

## How to read it

**Acute:chronic ratio** — acute load divided by chronic load:
- below 0.8: detraining, or a deliberate taper
- 0.8 to 1.3: the productive band
- 1.3 to 1.5: elevated; sustainable only briefly and only if recovery holds
- above 1.5: spike. This is where injury risk climbs sharply.

**Ramp rate** — week-over-week volume increase. Around 10% is the conventional
ceiling. Report the actual percentage. A jump from a low base is less alarming
than the same percentage from a high one, so read it against the chronic load
rather than in isolation.

**Corroboration.** A high ratio with stable HRV and a rising VO2max is a hard
block being absorbed. The same ratio with falling HRV and a flat or falling
VO2max is overreaching. Say which one this is — that distinction is the entire
value of this report.

Load is cross-sport: report the combined picture first, then which sport is
driving it. Do not analyse one sport in isolation.

## Output format

**Load Check — [date]**

**Verdict**: [sustainable / elevated / spike — one line, with the number that drove it]

**The numbers**
- Acute load: [value] | Chronic load: [value] | Ratio: [value]
- Week-over-week ramp: [percentage]
- Garmin training status: [status]
- Running tolerance: [current vs tolerated load]

**Corroborating signals**
- HRV over 28 days: [rising / stable / falling]
- VO2max over 28 days: [rising / stable / falling]
- Endurance score: [value and direction]

**Load by sport**: [which sport is contributing what — name the driver]

**What to do**: [concrete — hold, back off by roughly X%, or safe to build.
Give a number, not "listen to your body".]
{_SPORT_KEYS}{_SWIM_GAP}"""


# ---------------------------------------------------------------------------
# Workout creation and adjustment
# ---------------------------------------------------------------------------

# The step schema, written once. Every prompt that calls `create_workout` or
# `update_workout` needs it, and a stale copy in one of them is exactly the rot
# the contract test exists to catch.
_STEP_SCHEMA = """
## The step schema

`create_workout` and `update_workout` both take sport, name and a list of step
dicts. Each step has:

- `type`: warmup | interval | recovery | cooldown | rest | repeat
- exactly one of:
  - `duration_seconds` — step ends after a time
  - `distance_meters` — step ends after a distance
  - `reps` — strength only, and also needs `exercise`
- `target_type`: heart_rate_zone | power_zone | pace | cadence | open
- `target_value`:
  - heart_rate_zone: 1-5, or [low_bpm, high_bpm]
  - power_zone: 1-7, or [low_watts, high_watts]
  - pace: "mm:ss" per km, or per 100m for swimming, or [fast, slow]
  - cadence: a number, or [low, high]

To repeat a **group** of steps, use one step of type `repeat` with `iterations`
and a nested `steps` list. This is the important one — an interval and its
recovery repeat together:

    {"type": "repeat", "iterations": 4, "steps": [
        {"type": "interval", "duration_seconds": 480,
         "target_type": "heart_rate_zone", "target_value": 4},
        {"type": "recovery", "duration_seconds": 180,
         "target_type": "heart_rate_zone", "target_value": 2}]}

Do not emit four interval steps followed by four recovery steps. That is a
different, wrong workout.
"""


@mcp.prompt(
    description="Guided structured workout builder — design a session around the user's own zones and push it to Garmin"
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

    head = f"""Design a structured workout around the user's actual physiology and push it
to Garmin Connect.
{prefill}
## Steps

1. Ask for anything not already specified:
   - What sport? (running / cycling / swimming / strength_training)
   - **If cycling: indoor or outdoor?** This changes what gets built — see the
     Zwift section below. Ask before designing.
   - What is the goal? (e.g. threshold intervals, Z2 endurance, technique)
   - How long total? (approximate duration in minutes)

2. **Fetch the user's own numbers before designing anything.** A workout built
   on generic zones is a worse workout. Which calls depend on the sport:
   - Always: `get_user_profile` — heart rate zones and max HR.
   - Running: `get_lactate_threshold` for threshold pace and HR, and
     `get_race_predictions` to sanity-check that interval paces are realistic.
   - Cycling: `get_cycling_ftp` and `get_cycling_power_zones`, so intervals are
     prescribed in the user's real watts.
   - Swimming: `get_swim_activities` for the last 30 days to find a current
     100m pace. If there are none, say so and prescribe by effort and distance
     rather than inventing a pace target.

3. Check today's context before prescribing intensity: `suggest_recovery` and
   `get_training_load_trend` for today. If readiness is poor, say so and offer
   the easier version rather than quietly building the hard session anyway.

4. Design the session. Warmup, main set, cooldown. Prescribe targets in the
   units that sport is actually trained in — pace or HR for running, power for
   cycling, distance and interval for swimming, reps and load for strength.

5. **Show the user the full structure and wait for an explicit yes.** Present
   it as readable prose ("10 min easy, then 4 x 8 min at threshold with 3 min
   float, 10 min easy"), not as raw JSON.

6. Only after they confirm, call `create_workout`.

7. Confirm it was created and remind them to sync their watch.
"""

    examples = """
## Worked examples

**Running — threshold intervals**, built on the user's threshold HR:

    [{"type": "warmup", "duration_seconds": 900,
      "target_type": "heart_rate_zone", "target_value": 2},
     {"type": "repeat", "iterations": 4, "steps": [
        {"type": "interval", "duration_seconds": 480,
         "target_type": "heart_rate_zone", "target_value": 4},
        {"type": "recovery", "duration_seconds": 180,
         "target_type": "heart_rate_zone", "target_value": 2}]},
     {"type": "cooldown", "duration_seconds": 600}]

**Cycling (outdoor only) — sweet spot**, prescribed in real watts from FTP
(88-94% of FTP). For an indoor version of this, name a confirmed current Zwift
sweet spot workout instead of building this:

    [{"type": "warmup", "duration_seconds": 900,
      "target_type": "power_zone", "target_value": 2},
     {"type": "repeat", "iterations": 3, "steps": [
        {"type": "interval", "duration_seconds": 720,
         "target_type": "power_zone", "target_value": [220, 235]},
        {"type": "recovery", "duration_seconds": 300,
         "target_type": "power_zone", "target_value": 1}]},
     {"type": "cooldown", "duration_seconds": 600}]

**Swimming — beginner technique set**, distance-based, generous rest, no pace
target because there is no baseline yet:

    [{"type": "warmup", "distance_meters": 200, "target_type": "open"},
     {"type": "repeat", "iterations": 8, "steps": [
        {"type": "interval", "distance_meters": 50, "target_type": "open"},
        {"type": "rest", "duration_seconds": 30}]},
     {"type": "cooldown", "distance_meters": 100, "target_type": "open"}]

**Strength — a squat block**:

    [{"type": "warmup", "duration_seconds": 600},
     {"type": "repeat", "iterations": 4, "steps": [
        {"type": "interval", "reps": 8, "exercise": "SQUAT", "weight_kg": 60},
        {"type": "rest", "duration_seconds": 120}]}]

Swim sets are written in distance, not time — 8 x 50m, not 8 x 45 seconds. Use
`distance_meters` for them.
"""
    return head + _ZWIFT + _STEP_SCHEMA + examples


@mcp.prompt(
    description="Reconcile today's planned session against how recovered you actually are"
)
def adapt_plan(date: str = "today") -> str:
    """Keep, soften, swap or move the planned session based on readiness."""
    head = f"""Reconcile what is planned for {date} against how recovered the user
actually is, and adjust the plan if the data says to.

## Steps

1. Resolve {date} to YYYY-MM-DD.
2. Call `get_scheduled_workouts` for that month and find what is planned for
   that date. Keep both IDs from the result: the **workout ID** (the session
   itself) and the **scheduled workout ID** (the calendar entry). They are
   different, and the move path below needs the second one.
3. If something is scheduled, call `get_workout_by_id` to see its actual
   structure — you cannot judge whether a session is too hard without knowing
   what is in it.
4. Assess readiness: `suggest_recovery`, `get_training_readiness` and
   `get_wellness_snapshot` for that date.
5. Get load context: `get_training_load_trend` for that date. A single bad
   night on top of a light block reads differently from the same night at the
   end of a heavy one.

If nothing is scheduled, say so and suggest what would fit today given
readiness and recent load — then stop, unless the user asks you to build it.

## The decision

Choose exactly one, and name it:

- **Keep** — readiness is fine, or the session is already easy. The default.
  Do not adjust a plan because of one mediocre night.
- **Soften** — the session is right but the dose is too high. Cut intervals,
  drop a zone, or shorten it. Use `update_workout` with the same workout ID:
  it replaces the contents in place and keeps the ID, so the calendar entry
  stays valid. Do not create a new workout for this — that leaves the original
  still sitting on the calendar.
- **Swap** — the session is wrong for today, but the day should still be
  trained. Typically a hard run becomes easy aerobic work, or moves to a
  lower-impact sport. Build the replacement with `create_workout`, remove the
  old calendar entry with `unschedule_workout`, then place the new one with
  `schedule_workout`.
- **Move** — the session is right but today is wrong. Find a better day in the
  week, then `unschedule_workout` followed by `schedule_workout`. Check what is
  already on the target day first, and say what the knock-on effect is.

Bias toward **keep**. Readiness scores are noisy, and a plan that gets rewritten
every time HRV dips is not a plan. Soften or move on a clear signal — poor
readiness plus a high acute:chronic ratio, or a genuinely bad night before a
key session — not on a single soft number.

## Before writing anything

State the change, the reason, and the specific number behind it, then wait for
an explicit yes. Never write to the calendar without confirmation.

## Output format

**Plan Check — [date]**

**Planned**: [session name and what it actually contains]
**Readiness**: [score, HRV status, sleep, body battery]
**Load context**: [acute:chronic ratio and trend]

**Recommendation**: [KEEP / SOFTEN / SWAP / MOVE]
**Why**: [one or two sentences, naming the number that drove it]
**Proposed change**: [exactly what would change — the before and the after]

Then ask whether to apply it.
"""
    return head + _ZWIFT + _STEP_SCHEMA


@mcp.prompt(
    description="Design and schedule a multi-week triathlon base block from current load"
)
def build_training_block(
    weeks: str = "4", weekly_hours: str = "", race_date: str = ""
) -> str:
    """Build a progressive base block and put it on the Garmin calendar."""
    target = (
        f"The user is aiming for about {weekly_hours} hours per week."
        if weekly_hours
        else "Ask what weekly hours are realistic, and how many days per week they "
        "can train, before designing anything."
    )
    if race_date:
        goal = f"""There is a target event on {race_date}. Work backwards from it: build
phase first, then sharpen, then taper into the event. Call `get_race_predictions`
and `get_triathlon_fitness_snapshot` to judge how far current fitness sits from
what that event needs."""
    else:
        goal = """There is no race on the calendar. Do not ask for one and do not stall
waiting for a goal event — build an aerobic base block. The aim is consistency,
durable aerobic fitness, and establishing swimming as a habit. That is the right
objective for an athlete with no event booked, and it is what everything below
assumes."""

    head = f"""Design a {weeks}-week triathlon training block and put it on the Garmin
calendar.

{goal}

{target}

## Steps

1. **Measure the baseline before planning anything.** A block that ramps from an
   assumed starting point is how people get hurt.
   - `get_volume_by_sport` for the last 28 days — the actual current volume,
     per sport.
   - `get_training_load_trend` for today — current acute and chronic load.
   - `get_running_tolerance` for the last 28 days — how much running the user is
     adapted to absorb. Often empty until there is enough run history; if so,
     treat the last 28 days of actual run volume as the baseline instead.
   - `get_triathlon_fitness_snapshot` — cross-sport fitness.
   - `get_brick_analysis` for the last 28 days — whether bike-to-run work is
     happening at all.
   - `get_user_profile` for HR zones, plus `get_cycling_ftp` and
     `get_lactate_threshold` so every prescribed target is in the user's own
     numbers.

2. **Structure the block.** Three build weeks then one recovery week, for a
   four-week block; scale that pattern for other lengths. Recovery week is
   roughly 60% of the preceding week's volume — it is not optional, and it is
   where the adaptation actually lands.

3. **Ramp from the measured baseline**, not from the target. Around 10% total
   volume per week is the ceiling. Check the resulting run volume against
   `get_running_tolerance` where it is populated — if the plan exceeds it, cut
   the run volume and put the hours on the bike, which absorbs load at far lower
   injury cost.

4. **Weekly shape.** Most of the week is easy aerobic work — roughly 80% of
   volume in zones 1-2, with the hard days genuinely hard. Two easy days either
   side of every hard day. One long session per week, usually the bike.
   Include at least one brick if bike and run volume both support it.

   Mark each bike session indoor or outdoor. For the indoor ones, name a
   confirmed current Zwift workout rather than creating a Garmin workout — see
   the Zwift section below. Only the outdoor rides go through `create_workout`.

5. **Swimming.** The user does not swim yet and wants to start. Build this in
   deliberately:
   - Frequency over duration. Three short swims a week beats one long one —
     swimming is a technique sport and it is learned by repetition.
   - Start around 800-1200m per session, structured as short distance-based
     intervals (8 x 50m, 6 x 100m) with generous rest. Continuous swimming is
     the wrong tool for a beginner: form falls apart and the reps then reinforce
     it.
   - Prescribe by distance and effort, not pace. There is no pace baseline yet,
     and inventing one produces a target that is either meaningless or
     discouraging.
   - Do not ramp swim volume for the first two weeks. Consistency is the whole
     goal; volume can come once the habit and the stroke exist.

6. **Present the whole block before writing anything.** Week by week, with
   total hours and the session list per week. Wait for an explicit yes.

7. Only after they confirm: call `create_workout` for each session that needs a
   Garmin workout, then `schedule_workout` to place it on its date. Skip that
   for indoor bike sessions — those are ridden in Zwift, so list the workout
   name for each instead of creating anything. Work through them in order and
   report what was created. If any call fails, stop and say so rather than
   pressing on — a half-built plan is worse than none.

## Output format

**[N]-Week Base Block — [start date] to [end date]**

**Baseline**: [current weekly volume by sport, current chronic load]
**Target**: [where the block ends up, and the weekly ramp]

**Week 1 — [dates] — [total hours]**
| Day | Session | Detail |
|---|---|---|
| Mon | [sport / rest] | [duration, target zones; for indoor bike, the Zwift workout name] |
| ... | | |

[repeat per week, marking the recovery week clearly]

**Rationale**: [2-3 sentences: why this ramp, why this sport balance, what the
block is meant to produce]
**Watch for**: [the specific thing most likely to go wrong — usually run volume
or a swim habit that does not stick]
{_SPORT_KEYS}{_SWIM_GAP}"""
    return head + _ZWIFT + _STEP_SCHEMA
