# GitHub + Fly.io Deployment Design

> **SUPERSEDED 2026-08-20** by
> [2026-08-20-cloud-run-deployment-design.md](2026-08-20-cloud-run-deployment-design.md).
> This design was implemented and deployed, then replaced. It is kept as the
> decision record. See [What actually happened](#what-actually-happened) below
> before reusing anything here — one of its core premises was false.

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

**Key settings:** (neither held in practice — see below)
- `auto_stop_machines = false` — ~~never sleeps, no cold starts~~
- `min_machines_running = 1` — ~~always 1 instance running~~
- `256mb` RAM on `shared-cpu-1x` — ~~well within free allowance (2,340 CPU-hours/month
  free vs. 720 needed for 24/7)~~ **This was wrong.** Fly retired its free resource
  allowance in October 2024; there is no free always-on tier. See below.

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

---

## What actually happened

Deployed successfully on 2026-08-19 (release v5) and served traffic correctly.
The design's hosting premise, however, was false in two ways.

**1. There is no free always-on tier on Fly.io.** The "2,340 CPU-hours/month
free" figure above describes Fly's pre-2024 pricing. Fly retired the free
resource allowance in October 2024. The spec was written against terms that no
longer existed.

**2. Trial accounts stop machines after 5 minutes, overriding `fly.toml`.**
`auto_stop_machines = false` and `min_machines_running = 1` are not honored on a
trial account. From the machine logs:

```
20:52:25Z runner[...] [warn] Trial machine stopping.
  To run for longer than 5m0s, add a credit card by visiting https://fly.io/trial.
20:52:26Z app[...] [302.097093] reboot: Restarting system
```

Kernel uptime at reboot was 302.09 seconds — the 5-minute cap, hit exactly. The
observable symptoms were a Fly Doctor "app is not listening" warning, `curl`
returning `000` against both `/health` and `/mcp`, and `POST /mcp` returning 404
for clients reconnecting with an `Mcp-Session-Id` from before the restart (MCP
session state is in-memory and does not survive a machine stop).

Because `auto_start_machines = true`, the service did wake on demand — it became
reachable again 5.3s after a request arrived — so this degraded to cold starts
plus dropped MCP sessions rather than a hard outage.

**Lesson for future specs:** free-tier terms change, and a platform's own config
file does not necessarily override an account-level limit. Verify current pricing
at design time, and treat "no cold starts" as something to confirm against a
running deployment rather than assert from configuration.
