---
name: morning-check
description: Daily readiness check — call this when user wants to know how recovered they are or whether to train today
---

# Morning Check

Run a complete daily readiness assessment using the Garmin MCP tools.

## Steps

1. Get today's date (YYYY-MM-DD format)
2. Call `get_wellness_snapshot` with today's date — this fetches sleep, HRV, body battery, and stress in one call
3. Call `suggest_recovery` with today's date — this returns a training intensity recommendation
4. Call `get_training_readiness` with today's date — Garmin's own readiness score

## Output format

Present the results as a brief, scannable morning report:

**Morning Check — [date]**
- Sleep: [score/duration from sleep data]
- HRV: [status from HRV data]
- Body Battery: [current level]
- Stress: [average stress level]
- Garmin Readiness: [score/100]
- **Recommendation**: [rest / easy / normal / push] — [one sentence reasoning]

Keep it concise. The user wants a quick answer, not a wall of JSON.
