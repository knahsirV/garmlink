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

## Deploy to Fly.io

Prerequisites: [flyctl](https://fly.io/docs/flyctl/install/) installed and authenticated (`flyctl auth login`).

1. Register the app name (first time only):
   ```bash
   flyctl apps create garmin-mcp
   ```
   > If `garmin-mcp` is taken on Fly.io (global namespace), use a unique name like `garmin-mcp-yourname` and update the `app` field in `fly.toml` to match.

2. Set secrets:
   ```bash
   flyctl secrets set \
     GARMIN_EMAIL=you@example.com \
     GARMIN_TOKENS_JSON=<base64-from-auth-step> \
     MCP_AUTH_TOKEN=$(openssl rand -hex 32)
   ```
   Save the generated `MCP_AUTH_TOKEN` value — you'll need it for the Claude Desktop config.

3. Deploy:
   ```bash
   flyctl deploy
   ```

4. Verify:
   ```bash
   curl https://garmlink.fly.dev/health
   ```
   Expected: `{"status":"ok"}`

## Auto-Deploy via GitHub Actions

After the first manual deploy, subsequent pushes to `main` auto-deploy via GitHub Actions.

Add `FLY_API_TOKEN` to your GitHub repo secrets:
```bash
flyctl tokens create deploy
```
Copy the output, then go to: GitHub repo → Settings → Secrets and variables → Actions → New repository secret → name it `FLY_API_TOKEN`.

## Claude Desktop Config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "url": "https://garmlink.fly.dev/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

Replace `<MCP_AUTH_TOKEN>` with the value you set in the secrets step.

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
| `MCP_AUTH_TOKEN` | Bearer token protecting the MCP endpoint |
| `PORT` | Server port (default: 8000, set automatically by Fly.io) |
