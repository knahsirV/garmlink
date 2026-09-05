# garmlink

A privacy-focused Garmin Connect MCP server for triathlon training. ~50 tools covering daily health metrics, activity analysis, training load, running, cycling, swimming, strength training, and workout creation — plus triathlon-specific analysis (brick workouts, sport volume balance, cross-sport fitness snapshots).

Deployed as a remote MCP server over HTTPS. Connects to Claude Desktop or Claude Code via the streamable-HTTP transport.

## One-Time Auth Setup

Run this locally once to generate tokens:

1. Install locally:
   ```bash
   pip install -e .
   ```

2. Authenticate with Garmin:
   ```bash
   garmlink-auth
   ```
   Saves tokens to `~/.garminconnect/garmin_tokens.json` and prints the base64 export command.

3. Copy the printed `GARMIN_TOKENS_JSON=...` value — you'll need it for the secrets step below.

## Deploy to Google Cloud Run

Runs on Cloud Run's perpetual free tier. The service scales to zero when idle, so
the first request after a quiet period takes ~1-3s to wake — no dashboard step,
it just waits. `--min-instances=0` is deliberate: one always-warm instance would
far exceed the free vCPU-second allowance.

Prerequisites: [gcloud](https://cloud.google.com/sdk/docs/install) and
[gh](https://cli.github.com/) installed.

1. Log in as yourself and create (or pick) a project:
   ```bash
   gcloud auth login
   gcloud projects create garmlink        # skip if you already have one
   ```
   Cloud Run's free tier requires billing to be enabled on the project. You are
   not charged inside the free limits, but a card must be on file.

2. Run the one-time setup — enables APIs, stores your Garmin secrets in Secret
   Manager, creates a deploy service account, and wires up keyless GitHub
   Actions auth via Workload Identity Federation:
   ```bash
   ./scripts/setup-cloudrun.sh
   ```
   It prompts for `GARMIN_EMAIL` and `GARMIN_TOKENS_JSON`. The OAuth variables
   in the table below — `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
   `GITHUB_ALLOWED_USERS`, `PUBLIC_BASE_URL`, `READYZ_TOKEN` — aren't managed
   by this script yet, so set them by hand before deploying, with
   `gcloud run services update garmlink --set-env-vars/--set-secrets`. The same
   applies to `TRAINING_PLAN_GITHUB_TOKEN` — without it the plan document is
   readable but not writable. Create the secret and grant the runtime service
   account access to it:

   ```bash
   gcloud secrets create TRAINING_PLAN_GITHUB_TOKEN --replication-policy=automatic
   printf %s "<your PAT>" | \
     gcloud secrets versions add TRAINING_PLAN_GITHUB_TOKEN --data-file=-

   PROJECT_NUMBER=$(gcloud projects describe garmlink --format='value(projectNumber)')
   gcloud secrets add-iam-policy-binding TRAINING_PLAN_GITHUB_TOKEN \
     --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
     --role=roles/secretmanager.secretAccessor
   ```

   Do **not** attach it with `gcloud run services update --update-secrets`. The
   deploy workflow uses `--set-secrets`, which replaces the service's entire
   secret mapping — a hand-attached secret survives until the next push to
   `main` and then disappears. `deploy.yml` lists this one, so creating the
   secret is all that is needed; `printf` rather than `echo` because a trailing
   newline in the value corrupts the `Authorization` header.

   Edit the variables at the top of the script first if you want a different
   project id, region, or service name.

3. Deploy — push to `main`, or trigger the workflow by hand:
   ```bash
   gh workflow run "Deploy to Cloud Run"
   ```

4. Verify:
   ```bash
   URL=$(gcloud run services describe garmlink --region us-central1 --format='value(status.url)')
   curl "$URL/health"          # {"status":"ok"}
   curl -o /dev/null -w '%{http_code}\n' "$URL/mcp"   # 401 - auth is working
   ```

5. Check the Garmin session. The server no longer logs in to Garmin at startup —
   it authenticates on the first tool call and re-authenticates itself if the
   session dies. That means expired tokens show up as failing tool calls rather
   than a failed deploy, so check readiness explicitly:
   ```bash
   curl -H "Authorization: Bearer $READYZ_TOKEN" "$URL/readyz"
   ```
   Reports `never` until the first tool call, then `authenticated`. A `503` with
   `"garmin": "error"` means the tokens are bad — re-run `garmlink-auth` and
   update the `GARMIN_TOKENS_JSON` secret.

   Garmin rotates the DI refresh token on every refresh, which invalidates the
   value that was presented. Those rotations are persisted to Firestore
   (collection `garmin-tokens`), because `/tmp` is wiped on every cold start of
   a scale-to-zero service — without that, the seed goes stale the first time a
   token rotates and every subsequent cold start fails with
   `Failed to retrieve social profile`. The `startup` log line reports
   `"garmin_tokens":"firestore"` when this is wired up; `"ephemeral"` means it
   is not, and the deploy will work until the first rotation and then break.

## Auto-Deploy via GitHub Actions

Every push to `main` deploys via `.github/workflows/deploy.yml`. Authentication
is keyless — GitHub mints a short-lived OIDC token that Google exchanges for
credentials, so there is no long-lived service-account key in your repo secrets.
The setup script sets the three repo variables the workflow reads
(`GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`).

## Connecting a Client

Auth is GitHub OAuth now — there is no bearer token to paste into a client.

**claude.ai (web and mobile):** Settings → Connectors → Add custom connector,
then paste `https://<your-cloud-run-url>/mcp`. claude.ai drives the GitHub
OAuth flow itself; sign in with a GitHub account listed in
`GITHUB_ALLOWED_USERS`.

**Claude Desktop / Claude Code:** add the server with no `headers` field —
the client opens a browser for the same OAuth flow on first use. Add to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmlink": {
      "url": "https://<your-cloud-run-url>/mcp"
    }
  }
}
```

Replace `<your-cloud-run-url>` with the URL printed at the end of the deploy
workflow (or from step 4 above).

## Coaching Workflows

Eight guided workflows ship with the server as MCP prompts, so they work in any
MCP client rather than only in this project directory.

| Prompt | Purpose |
|---|---|
| `morning_check` | Daily readiness briefing (HRV, sleep, body battery) |
| `analyze_week` | Weekly training load and sport balance review |
| `load_check` | Acute:chronic ratio, ramp rate and injury risk |
| `session_debrief` | Split-by-split review of one session — pacing, decoupling, execution |
| `race_readiness` | Pre-race fitness assessment across all disciplines |
| `adapt_plan` | Reconcile today's planned session against actual readiness |
| `build_training_block` | Design a multi-week base block → schedules it on Garmin |
| `create_workout_guide` | Guided structured workout builder → pushes to Garmin |

The cross-sport ones (`analyze_week`, `load_check`, `race_readiness`,
`build_training_block`) always report swim, bike and run together, because
training load does not partition by sport. The per-session ones
(`session_debrief`, `create_workout_guide`) branch on sport, because the tools
do — a swim needs stroke and SWOLF data, a run needs running dynamics.

`adapt_plan` and `build_training_block` can write to the Garmin calendar. Both
show the proposed change and wait for an explicit yes first.

Indoor bike sessions are not built as Garmin workouts — they are ridden in Zwift
under ERG, so the prompts recommend a workout from Zwift's own library instead.
They must confirm by web search that the workout is in a **current** collection
first: whatsonzwift.com marks collections Zwift deleted in its October 2023
library reorg as `(legacy)`, and the same workout name can appear in both a
current and a legacy collection. Outdoor rides still go through `create_workout`.

How they surface depends on the client: Claude Desktop lists them in its prompt
menu, and Claude Code exposes them as `/mcp__garmlink__morning_check` and so on.

## The Training Plan Document

The prompts hold no athlete facts. Goals, race dates, the current block, the
weekly template and the training zones live in a separate repo —
[`knahsirV/training-plan`](https://github.com/knahsirV/training-plan), where
`content/plan.md` is rendered by a static PWA on GitHub Pages — and are read at
run time by `get_training_plan`.

They used to be hardcoded in `prompts.py`, and the copy went stale exactly the
way duplicated facts do: the prompts asserted "no race booked" and "swimming not
yet started" for months after a half marathon was booked and swim sessions
began, so `load_check` and `build_training_block` were coaching toward the wrong
block. Method belongs in the prompts; facts belong in the plan.

`update_training_plan` closes the loop, so an adjustment made from any MCP client
— including a phone with no repo checked out — lands in the document as well as
on the Garmin calendar. It is deliberately conservative:

- **The whole file is replaced**, so a replacement more than 30% shorter than the
  current one is refused as truncation unless `allow_shrink` is passed.
- **Writes are optimistically locked** on the blob `sha` returned by
  `get_training_plan`. An edit based on a stale read is refused rather than
  silently discarding whatever landed in between.
- **Render warnings, not render errors.** The PWA uses a hand-rolled markdown
  subset (headings, bold, italic, inline code, tables, `-` lists, `---`), so a
  write introducing ordered lists, links, blockquotes, nested lists or fenced
  code blocks reports which lines will show as literal text — without blocking,
  since the right fix is sometimes to extend the PWA's `render.js` instead.

Nothing here knows the plan's structure. Sections can be reordered, the block
renamed, tables restructured — `tests/test_training_plan.py` pins that with a
deliberately reorganised document that must still validate.

Without `TRAINING_PLAN_GITHUB_TOKEN` the plan is still readable (it is a public
repo, rate-limited to 60 requests an hour) and writes refuse cleanly. The
`startup` log line reports which mode is live:

```
INFO startup tools=50 prompts=8 ... training_plan=readwrite
```

`readonly` there means adjustments will reach the Garmin calendar and silently
never reach the plan document.

## Environment Variables

| Variable | Description |
|---|---|
| `GARMIN_EMAIL` | Your Garmin Connect email |
| `GARMIN_TOKENS_JSON` | Base64-encoded token file (from `garmlink-auth`). A **seed**, not the live credential: Garmin rotates the refresh token, and rotations are persisted to Firestore, which then takes precedence. Re-seed only to bootstrap a new deployment or recover a stored blob that has gone bad. |
| `GARMIN_PASSWORD` | Optional. Only used to re-authenticate if the stored tokens expire. |
| `GITHUB_CLIENT_ID` | **Required** (unless `ALLOW_UNAUTHENTICATED=1`). Client ID of the GitHub OAuth App backing the claude.ai connector. |
| `GITHUB_CLIENT_SECRET` | **Required.** That app's client secret. |
| `GITHUB_ALLOWED_USERS` | **Required.** Comma-separated GitHub logins allowed to use the server — the only access control once OAuth is on, so it fails closed: a blank value or a list naming nobody (e.g. `,,`) both abort startup. |
| `PUBLIC_BASE_URL` | **Required.** The service's externally reachable URL, e.g. `https://garmlink-moz6szqd6q-uc.a.run.app`. OAuth callback URLs (`/auth/callback`) are derived from it. |
| `READYZ_TOKEN` | **Required.** Bearer token guarding `/readyz`, checked independently of OAuth so it still answers when the OAuth layer itself is broken. |
| `ALLOW_UNAUTHENTICATED` | Set to `1` to run with no authentication, skipping the five variables above. Localhost development only — never on a public address. |
| `TRAINING_PLAN_GITHUB_TOKEN` | Optional. Fine-grained PAT with **Contents: read and write**, scoped to the plan repo alone. Kept separate from `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` on purpose — those authenticate users into this server and have no business holding repo write access. Unset, the plan is read-only. |
| `TRAINING_PLAN_REPO` | Optional. `owner/repo` holding the plan (default: `knahsirV/training-plan`). |
| `TRAINING_PLAN_PATH` | Optional. Path to the plan file within that repo (default: `content/plan.md`). |
| `TRAINING_PLAN_BRANCH` | Optional. Branch to read and write (default: `main`). |
| `PORT` | Server port (default: 8000; Cloud Run injects 8080) |
| `LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, or `ERROR` |
| `LOG_FORMAT` | `json` or `text`. Defaults to `json` on Cloud Run (detected via `K_SERVICE`), `text` elsewhere. |

## Local Development

Use a **Python 3.12** virtualenv. This is not optional: `garminconnect` 0.3.3+
requires 3.12, so a 3.11 interpreter silently resolves to 0.3.11's predecessor
0.3.2 — a different library from the one CI and production run, with different
return types. Tests then pass against an API that production never executes.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

Run the suite (the same files CI runs):

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

Note that real Garmin tokens in `~/.garminconnect` mean a carelessly constructed
test client will reach the **live** Garmin API. Tests patch the `GarminClient`
constructor to prevent this; follow that pattern.

## Logs

The server emits one structured line per notable event. On Cloud Run these are
JSON, and the platform lifts `severity` into the log viewer, so filtering by
error works:

```
{"severity":"INFO","message":"startup","tools":48,"prompts":8,"token_source":"secret","auth":"github_oauth","storage":"firestore","garmin_tokens":"firestore"}
{"severity":"INFO","message":"garmin.tokens.load","outcome":"ok","source":"store"}
{"severity":"INFO","message":"garmin.tokens.save","outcome":"ok"}
{"severity":"INFO","message":"tool.call","name":"get_daily_summary","args":{"date":"2026-08-20"},"outcome":"ok","dur_ms":214.0,"cache":"0h/1m"}
{"severity":"WARNING","message":"auth.reject","path":"/mcp","reason":"bad_token"}
{"severity":"WARNING","message":"garmin.retry","method":"get_stats","attempt":1,"outcome":"rate_limited"}
```

`tool.call` is the important one: to Cloud Run's own request log every MCP call
is an indistinguishable `POST /mcp`, so this is the only place you can see
*which* of the 45 tools ran, how long it took, and whether it was served from
cache (`cache` counts hits/misses, since range tools make one call per day).

Two things are deliberately never logged: **tool results**, which are the health
data this server exists to protect, and **presented credentials** on a rejected
request. Arguments and error messages are passed through a redactor that strips
token-shaped strings.

Reading them:

```bash
gcloud run services logs read garmlink --region us-central1 --project garmlink --limit 50
```
