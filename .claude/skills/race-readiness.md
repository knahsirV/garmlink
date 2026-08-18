---
name: race-readiness
description: Pre-race fitness assessment — call when user asks if they're ready for a race or wants to assess fitness against a target event
---

# Race Readiness Assessment

Assess current fitness against a target race or event.

## Steps

1. Ask the user (if not provided): What race/event? What distance? When is it?
2. Call `get_triathlon_fitness_snapshot` — cross-sport fitness overview
3. Call `get_race_predictions` — predicted running race times
4. Call `get_training_load_trend` with today's date — current load status
5. Call `get_cycling_ftp` — current cycling power
6. Call `get_endurance_score` with today's date — aerobic capacity

## Output format

**Race Readiness: [Event Name]**

**Current Fitness**
- Running: [VO2max estimate, predicted race times]
- Cycling: [FTP watts]
- Swimming: [most recent session pace/SWOLF if available]
- Endurance score: [Garmin score]

**Training Load**
- Current status: [productive/maintaining/peaking/recovery]
- Recommendation: [whether to taper, maintain, or build before the race]

**Gaps to Address**: [any sport or metric that looks underprepared]
