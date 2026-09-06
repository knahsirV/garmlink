# garmlink

Privacy-focused Garmin Connect MCP server for triathlon training (~50 tools:
daily health, activity analysis, training load, per-sport analysis, workout
creation). Remote MCP server over HTTPS, streamable-HTTP transport, deployed to
Google Cloud Run (scales to zero). Sister repo: `../training-plan`. See
`../CLAUDE.md` for how they connect.

## Python 3.12 only

Not optional. `garminconnect` 0.3.3+ requires 3.12; a 3.11 interpreter silently
resolves to 0.3.2 — a *different library* with different return types from the
one CI and production run.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

## Tests

CI runs the test files directly as scripts (not `pytest`). Run the same set:

```bash
.venv/bin/python tests/test_garmin_contract.py
.venv/bin/python tests/test_critical_fixes.py
.venv/bin/python tests/test_auth_lifecycle.py
.venv/bin/python tests/test_token_persistence.py
.venv/bin/python tests/test_auth_provider.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_workout_builder.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_tool_dispatch.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_prompts.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_logging.py
```

Real tokens in `~/.garminconnect` mean a careless test client hits the **live**
Garmin API. Tests patch the `GarminClient` constructor to prevent this — follow
that pattern in any new test.

## Dependencies

Every pin in `pyproject.toml` is deliberate and commented — `garminconnect`,
`google-cloud-firestore`, and `py-key-value-aio` are exact-pinned because each
has already broken this project. Don't bump without reading the comment and
checking CI + the Docker image (both 3.12).

## Layout

- `src/garmlink/server.py` — FastMCP app, tool/prompt registration.
- `src/garmlink/tools/` — one module per sport/domain (`running.py`,
  `cycling.py`, `swimming.py`, `strength.py`, `daily.py`, `training.py`,
  `insights.py`, `coaching.py`, `workouts.py`, `plan.py`).
- `tools/coaching.py` — the computed coaching primitives (session counts,
  progression check, intensity distribution, recovery trend). Arithmetic only;
  the judgement stays in `prompts.py`. Everything else in `tools/` hands Garmin's
  payload to the model with a `note` telling it what to work out, which is the
  right split for judgement and the wrong one for a mean.
- `sports.py` — the Garmin activity vocabulary and unit conversions, shared so
  the tools cannot disagree. `virtual_ride` is cycling (read literally it matches
  no bike substring, and bike volume then reads far too low); set weight is in
  grams, distance in metres.
- `tools/plan.py` — reads/writes `knahsirV/training-plan` via the GitHub Contents
  API. The document is **split across three files** (`content/plan.md`,
  `reference.md`, `log.md`); `get_training_plan` returns them joined plus a
  `parts` list carrying each file's own `sha`, and `update_training_plan` takes a
  `path` and writes one part per call. Only the primary part is required to exist
  or to carry an H1.

  **This module deliberately does not parse the plan** (`plan.py:151`). It
  hardcodes no heading, section or table name, so the document can be
  reorganised without touching the server — a validator that knew the shape
  would need editing every time the shape changed. `render.js` in the sister repo
  is the only structural consumer.
- `auth_provider.py` / `auth.py` / `tokens.py` — GitHub OAuth for connector
  users; Garmin token storage (Firestore in prod, file locally).

## Deploy

Push to `main` → `.github/workflows/deploy.yml` → Cloud Run, via Workload
Identity Federation (no stored Google credential). `workflow_dispatch` also
available.

## Logging & privacy

One structured line per event (`tool.call` is the key one). **Never logged:**
tool results (the health data this server protects) and presented credentials on
rejected requests. Args/errors pass through a redactor that strips token-shaped
strings — keep event names short enough to survive it.

Config: copy `.env.example`. `ALLOW_UNAUTHENTICATED=1` skips the five OAuth vars
for localhost only.
