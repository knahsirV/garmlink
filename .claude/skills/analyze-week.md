---
name: analyze-week
description: Weekly training review — call when user asks how their training week went or wants a week summary
---

# Analyze Week

Provide a structured weekly training review using Garmin data.

## Steps

1. Determine the week's date range (use the past 7 days ending today)
2. Call `get_volume_by_sport` for the 7-day range — returns time/distance/sessions per sport
3. Call `get_training_load_trend` with today's date — acute vs chronic load
4. Call `get_hrv_trend` with start and end of the week — HRV trend over the week
5. Call `get_weekly_comparison` for 'steps' and 'sleep_score' — week-over-week context

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

**Key Insight**: [1-2 sentence observation — e.g. "Heavy bike week, run volume low, good recovery trend"]
**Next Week Suggestion**: [1-2 sentences based on load status]
