# garmin-mcp

A privacy-focused Garmin Connect MCP server for triathlon training. ~47 tools covering daily health metrics, activity analysis, training load, running, cycling, swimming, strength training, and workout creation — plus triathlon-specific analysis (brick workouts, sport volume balance, cross-sport fitness snapshots).

Deployed as a remote MCP server over HTTPS. Connects to Claude Desktop or Claude Code via the streamable-HTTP transport.

## One-Time Auth Setup

Run this locally once to generate tokens:

1. Install locally:
   ```bash
   pip install -e .
   ```

2. Authenticate with Garmin:
   ```bash
   garmin-mcp-auth
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

2. Run the one-time setup — enables APIs, stores your three secrets in Secret
   Manager, creates a deploy service account, and wires up keyless GitHub auth
   via Workload Identity Federation:
   ```bash
   ./scripts/setup-cloudrun.sh
   ```
   It prompts for `GARMIN_EMAIL`, `GARMIN_TOKENS_JSON`, and `MCP_AUTH_TOKEN`
   (generate one with `openssl rand -hex 32` — it must be at least 32
   characters, and the server refuses to start without it). Save that token for
   the Claude Desktop config below.

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

## Auto-Deploy via GitHub Actions

Every push to `main` deploys via `.github/workflows/deploy.yml`. Authentication
is keyless — GitHub mints a short-lived OIDC token that Google exchanges for
credentials, so there is no long-lived service-account key in your repo secrets.
The setup script sets the three repo variables the workflow reads
(`GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`).

## Claude Desktop Config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "url": "https://<your-cloud-run-url>/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

Replace `<your-cloud-run-url>` with the URL printed at the end of the deploy
workflow (or from step 4 above), and `<MCP_AUTH_TOKEN>` with the token you set
during setup.

## Coaching Skills

Run these slash commands from this project directory in Claude Code:

| Command | Purpose |
|---|---|
| `/morning-check` | Daily readiness briefing (HRV, sleep, body battery) |
| `/analyze-week` | Weekly training load and sport balance review |
| `/race-readiness` | Pre-race fitness assessment across all disciplines |
| `/create-workout` | Guided structured workout builder → pushes to Garmin |

## Environment Variables

| Variable | Description |
|---|---|
| `GARMIN_EMAIL` | Your Garmin Connect email |
| `GARMIN_TOKENS_JSON` | Base64-encoded token file (from `garmin-mcp-auth`) |
| `MCP_AUTH_TOKEN` | **Required.** Bearer token protecting the MCP endpoint; must be at least 32 characters. The server refuses to start without it. |
| `GARMIN_PASSWORD` | Optional. Only used to re-authenticate if the stored tokens expire. |
| `ALLOW_UNAUTHENTICATED` | Set to `1` to run with no authentication. Localhost development only — never on a public address. |
| `PORT` | Server port (default: 8000; Cloud Run injects 8080) |
