---
name: create-workout
description: Build and push a structured workout to Garmin Connect — call when user wants to create a training session
---

# Create Workout

Guide the user through building a structured workout and pushing it to Garmin Connect.

## Steps

1. Ask (if not already provided):
   - What sport? (running / cycling / swimming / strength_training)
   - What is the goal? (e.g. threshold intervals, Z2 endurance, sprint work)
   - How long total? (approximate duration in minutes)

2. Based on their answers, design the workout steps:
   - **Warmup**: 10-15 min, target_type: "heart_rate_zone", target_value: 2
   - **Main set**: design based on goal (e.g. 4x8min at zone 4 with 3min recovery)
   - **Cooldown**: 5-10 min, target_type: "heart_rate_zone", target_value: 1

3. Show the user the workout structure and ask for confirmation before pushing

4. Call `create_workout` with:
   - sport: the sport string
   - name: a descriptive name (e.g. "4x8min Threshold Intervals")
   - steps: list of step dicts with type, duration_seconds, target_type, target_value, repeat

5. Confirm the workout was created and tell the user to sync their Garmin device

## Step types
- warmup, interval, recovery, cooldown, rest
- target_type: heart_rate_zone (1-5), power_zone (1-7), or open
