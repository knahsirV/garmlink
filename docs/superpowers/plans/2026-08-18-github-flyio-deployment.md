# GitHub + Fly.io Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish garmin-mcp to a public GitHub repo and deploy it to Fly.io with always-on free hosting and auto-deploy on push to main.

**Architecture:** The existing Docker-based server is deployed to Fly.io via a `fly.toml` config. GitHub Actions triggers `flyctl deploy --remote-only` on every push to `main`, building the image on Fly's infrastructure. Fly.io secrets hold the three required env vars (GARMIN_EMAIL, GARMIN_TOKENS_JSON, MCP_AUTH_TOKEN).

**Tech Stack:** Fly.io, flyctl CLI, GitHub Actions, gh CLI (GitHub CLI), Docker (already configured)

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `README.md` | Create | Project overview, auth setup, deploy instructions, Claude Desktop config |
| `fly.toml` | Create | Fly.io deployment config (always-on, health check, 256MB VM) |
| `railway.toml` | Delete | No longer needed |
| `.github/workflows/fly.yml` | Create | Auto-deploy to Fly.io on push to main |

---

### Task 1: Rename branch and push to GitHub

**Files:**
- No file changes — git operations only

**Prerequisites:** `gh` CLI must be installed. Check with `gh --version`. If missing, install via `brew install gh` then authenticate with `gh auth login`.

- [ ] **Step 1: Rename local branch from master to main**

```bash
cd "/Users/vrishank/Comp Sci Projects/garmin-mcp"
git branch -m master main
```

Expected: no output (success is silent)

- [ ] **Step 2: Verify branch rename**

```bash
git branch
```

Expected output:
```
* main
```

- [ ] **Step 3: Create public GitHub repo and push**

```bash
gh repo create garmin-mcp --public --source=. --remote=origin --push
```

Expected: output showing repo created at `https://github.com/vrishank/garmin-mcp` and commits pushed.

- [ ] **Step 4: Verify push succeeded**

```bash
git log --oneline -3 && echo "---" && git remote -v
```

Expected: 3 most recent commits shown, and `origin` remote pointing to `github.com:vrishank/garmin-mcp`.

---

### Task 2: Add README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create README.md**

```markdown
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
   curl https://garmin-mcp.fly.dev/health
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
      "url": "https://garmin-mcp.fly.dev/mcp",
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
```

- [ ] **Step 2: Commit README**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs: add README with auth setup and Fly.io deployment instructions

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

Expected: `[main <sha>] docs: add README with auth setup...`

- [ ] **Step 3: Push to GitHub**

```bash
git push origin main
```

Expected: `main -> main` push confirmation.

---

### Task 3: Replace railway.toml with fly.toml

**Files:**
- Create: `fly.toml`
- Delete: `railway.toml`

- [ ] **Step 1: Delete railway.toml**

```bash
rm "/Users/vrishank/Comp Sci Projects/garmin-mcp/railway.toml"
```

- [ ] **Step 2: Create fly.toml**

```toml
app = "garmin-mcp"
primary_region = "ord"

[build]
  dockerfile = "Dockerfile"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false
  auto_start_machines = true
  min_machines_running = 1

[[vm]]
  memory = "256mb"
  cpu_kind = "shared"
  cpus = 1

[[http_service.checks]]
  grace_period = "10s"
  interval = "15s"
  method = "GET"
  path = "/health"
  timeout = "2s"
  type = "http"
```

> **Note:** If you used a custom app name (e.g. `garmin-mcp-yourname`) in Task 4's `flyctl apps create`, update the `app = "garmin-mcp"` line here to match before deploying.

- [ ] **Step 3: Validate fly.toml (requires flyctl installed)**

```bash
cd "/Users/vrishank/Comp Sci Projects/garmin-mcp" && flyctl config validate
```

Expected: `✓ Configuration is valid` (or similar success message). If flyctl is not yet installed, skip this step and validate during Task 4.

- [ ] **Step 4: Commit**

```bash
git add fly.toml && git rm railway.toml
git commit -m "$(cat <<'EOF'
chore: replace railway.toml with fly.toml for Fly.io deployment

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

Expected: commit showing `railway.toml` deleted, `fly.toml` created.

- [ ] **Step 5: Push**

```bash
git push origin main
```

---

### Task 4: Set up Fly.io app and first manual deploy

**Files:**
- No file changes — CLI operations only

**Prerequisites:** flyctl installed (`brew install flyctl`) and authenticated (`flyctl auth login`).

- [ ] **Step 1: Create the Fly.io app**

```bash
flyctl apps create garmin-mcp
```

Expected: `New app created: garmin-mcp`

If you see "name already taken", choose a unique name (e.g. `garmin-mcp-vrishank`) and update `app = "..."` in `fly.toml` to match, then re-commit and push before continuing.

- [ ] **Step 2: Run garmin-mcp-auth locally to get tokens (if not done already)**

```bash
pip install -e "/Users/vrishank/Comp Sci Projects/garmin-mcp" && garmin-mcp-auth
```

Follow the prompts (email + password). The script prints the base64-encoded `GARMIN_TOKENS_JSON` value at the end. Copy it.

- [ ] **Step 3: Set Fly.io secrets**

```bash
flyctl secrets set \
  GARMIN_EMAIL=you@example.com \
  GARMIN_TOKENS_JSON=<paste-base64-from-step-2> \
  MCP_AUTH_TOKEN=$(openssl rand -hex 32)
```

Replace `you@example.com` with your Garmin email and paste the base64 value for `GARMIN_TOKENS_JSON`.

Expected: `Secrets are staged for the first deployment`

**Important:** Copy the `MCP_AUTH_TOKEN` value printed in the command before running — you'll need it for the Claude Desktop config. To retrieve it later: `flyctl secrets list` shows names but not values.

- [ ] **Step 4: First manual deploy**

```bash
cd "/Users/vrishank/Comp Sci Projects/garmin-mcp" && flyctl deploy
```

Expected: Build completes, machine starts, health check passes. Final line: `✓ Deployed` or `==> v1 deployed successfully`.

- [ ] **Step 5: Verify health endpoint**

```bash
curl https://garmin-mcp.fly.dev/health
```

Expected response:
```json
{"status":"ok"}
```

If the app name is different from `garmin-mcp`, the URL is `https://<your-app-name>.fly.dev/health`.

- [ ] **Step 6: Verify MCP endpoint responds**

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer <MCP_AUTH_TOKEN>" \
  https://garmin-mcp.fly.dev/mcp
```

Expected: `200` (the MCP endpoint responds to GET with 200 or 405; anything other than 401/403 confirms auth is working).

---

### Task 5: Add GitHub Actions auto-deploy workflow

**Files:**
- Create: `.github/workflows/fly.yml`

- [ ] **Step 1: Create workflow directory**

```bash
mkdir -p "/Users/vrishank/Comp Sci Projects/garmin-mcp/.github/workflows"
```

- [ ] **Step 2: Create fly.yml**

```yaml
name: Deploy to Fly.io

on:
  push:
    branches: [main]

jobs:
  deploy:
    name: Deploy
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: superfly/flyctl-actions/setup-flyctl@master
      - run: flyctl deploy --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
```

- [ ] **Step 3: Generate a Fly.io deploy token**

```bash
flyctl tokens create deploy
```

Copy the full token output (starts with `FlyV1 ...`).

- [ ] **Step 4: Add FLY_API_TOKEN to GitHub repo secrets**

```bash
gh secret set FLY_API_TOKEN --repo vrishank/garmin-mcp
```

Paste the token when prompted.

Expected: `✓ Set secret FLY_API_TOKEN for vrishank/garmin-mcp`

- [ ] **Step 5: Commit and push the workflow**

```bash
git add .github/workflows/fly.yml
git commit -m "$(cat <<'EOF'
ci: add GitHub Actions workflow for Fly.io auto-deploy on push to main

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
git push origin main
```

Expected: push succeeds and triggers the GitHub Actions workflow.

- [ ] **Step 6: Verify workflow ran successfully**

```bash
gh run list --repo vrishank/garmin-mcp --limit 3
```

Expected: most recent run shows `✓ completed` status for the "Deploy to Fly.io" workflow.

If it shows `✗ failed`, check logs with:
```bash
gh run view --repo vrishank/garmin-mcp --log-failed
```

- [ ] **Step 7: Update Claude Desktop config**

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` to add:

```json
{
  "mcpServers": {
    "garmin": {
      "url": "https://garmin-mcp.fly.dev/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

Replace `<MCP_AUTH_TOKEN>` with the value you set in Task 4 Step 3. Replace `garmin-mcp` in the URL with your actual app name if different.

Restart Claude Desktop after saving.
