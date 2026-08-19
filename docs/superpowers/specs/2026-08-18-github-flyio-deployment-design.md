# GitHub + Fly.io Deployment Design

## Context

The garmin-mcp project is complete (12 commits, ~47 tools). It was originally scaffolded for Railway deployment. This spec covers migrating to Fly.io and publishing to a public GitHub repo with auto-deploy on push.

## Goals

- Public GitHub repo with all existing commit history
- Free, always-on Fly.io deployment (no cold starts)
- Auto-deploy to Fly.io on every push to `main` via GitHub Actions
- Minimal README for the public repo

## Non-Goals

- Changing any server code or tool implementations
- Setting up staging/preview environments
- Multi-region Fly.io deployment

---

## 1. GitHub Repo

- **Name:** `garmin-mcp`
- **Visibility:** Public
- **Remote:** `git@github.com:vrishank/garmin-mcp.git`
- Push all 12 existing commits as-is; no history rewrite
- Add a README.md covering:
  - What the project is (Garmin Connect MCP server for triathlon training)
  - One-time auth setup: `garmin-mcp-auth` → base64 tokens → Fly.io secrets
  - Claude Desktop config snippet with the Fly.io URL

## 2. Fly.io Configuration

Replace `railway.toml` with `fly.toml`:

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

**Key settings:**
- `auto_stop_machines = false` — never sleeps, no cold starts
- `min_machines_running = 1` — always 1 instance running
- `256mb` RAM on `shared-cpu-1x` — well within free allowance (2,340 CPU-hours/month free vs. 720 needed for 24/7)

**Secrets (set via CLI once):**
```
flyctl secrets set \
  GARMIN_EMAIL=... \
  GARMIN_TOKENS_JSON=... \
  MCP_AUTH_TOKEN=...
```

**MCP endpoint:** `https://garmin-mcp.fly.dev/mcp`

## 3. GitHub Actions Auto-Deploy

File: `.github/workflows/fly.yml`

Trigger: push to `main`

Steps:
1. `actions/checkout@v4`
2. `superfly/flyctl-actions/setup-flyctl@master`
3. `flyctl deploy --remote-only` — builds on Fly's infrastructure

Required GitHub secret: `FLY_API_TOKEN`
- Generate with: `flyctl tokens create deploy`
- Add at: GitHub repo → Settings → Secrets and variables → Actions → New repository secret

## 4. Claude Desktop Config Update

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

---

## Implementation Order

1. Create public GitHub repo, add remote, push existing commits
2. Add README.md
3. Replace `railway.toml` with `fly.toml`
4. Add `.github/workflows/fly.yml`
5. Commit and push — triggers first auto-deploy
6. Set Fly.io secrets via `flyctl secrets set`
7. Verify `/health` returns 200 and MCP endpoint responds

## Files Changed

| File | Action |
|---|---|
| `README.md` | New |
| `fly.toml` | New (replaces railway.toml) |
| `railway.toml` | Deleted |
| `.github/workflows/fly.yml` | New |
