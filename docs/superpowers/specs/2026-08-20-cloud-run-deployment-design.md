# Google Cloud Run Deployment Design

## Context

Supersedes [2026-08-18-github-flyio-deployment-design.md](2026-08-18-github-flyio-deployment-design.md),
which assumed a free always-on Fly.io tier that no longer exists. Fly retired its
free resource allowance in October 2024, and trial accounts stop machines after
5 minutes of runtime regardless of `fly.toml` — the deployed machine rebooted at
302s uptime. See that spec's "What actually happened" section for the evidence.

This spec covers moving the same container to Google Cloud Run, whose free tier
is still in force, and keeping auto-deploy on push to `main`.

## Goals

- Run on Cloud Run's perpetual free tier without a paid instance
- Keep auto-deploy on every push to `main`
- No long-lived cloud credential stored in GitHub
- Keep the MCP streamable-HTTP transport working, including long-lived connections

## Non-Goals

- Always-on / zero cold start — explicitly traded away, see §2
- Changing any server code or tool implementations
- Staging or preview environments
- Multi-region deployment

---

## 1. Service Shape

Public Cloud Run service (`--allow-unauthenticated`), one container, scaling to
zero when idle.

Cloud Run's own IAM authentication is deliberately **not** used: it would require
callers to present a Google identity token, which Claude Desktop cannot do. The
service is therefore publicly reachable and the application's own bearer-token
middleware is the only access control. That middleware fails closed — the server
refuses to start without a `MCP_AUTH_TOKEN` of at least 32 characters — so a
misconfiguration cannot silently expose the endpoint.

The container needs no changes: it already reads `PORT` from the environment
(Cloud Run injects `8080`) and binds `0.0.0.0`.

## 2. Staying Inside the Free Tier

`--min-instances=0` is a requirement, not a default.

One always-warm instance is roughly 2.6M instance-seconds per month
(30 × 86,400). The free allowance is on the order of 180,000 vCPU-seconds and
360,000 GiB-seconds per month. Keeping a warm instance would therefore exceed the
free tier by more than an order of magnitude and reintroduce exactly the problem
this migration exists to solve.

The accepted cost is a cold start of roughly 1–3 seconds on the first request
after an idle period. No manual step is involved — the platform starts the
container on demand.

`--max-instances=2` caps a runaway request pattern so it cannot quietly consume
the allowance.

**Consequence for MCP:** session state is in-memory, so scaling to zero drops
active MCP sessions. Clients reconnecting with a stale `Mcp-Session-Id` receive a
404 and must re-initialize. This is normal for the transport and was already
happening on Fly; at a 1–3s cold start it should be unobtrusive.

## 3. Runtime Settings

| Setting | Value | Why |
|---|---|---|
| `--timeout` | `3600` | Cloud Run defaults to 300s, which would sever long-lived MCP streamable-HTTP connections mid-stream. |
| `--memory` | `512Mi` | Was 256Mi on Fly. Cache growth is bounded but not capped by entry count; 512Mi stays well inside the GiB-second allowance at this usage. |
| `--cpu` | `1` | Minimum for the default CPU-during-request allocation. |
| `--min-instances` | `0` | Free-tier requirement — see §2. |
| `--max-instances` | `2` | Ceiling on runaway usage. |

## 4. Secrets

The three values move from Fly secrets to Google Secret Manager and are mounted
as environment variables via `--set-secrets`:

| Secret | Purpose |
|---|---|
| `GARMIN_EMAIL` | Garmin account identity |
| `GARMIN_TOKENS_JSON` | Base64 Garmin OAuth tokens from `garmin-mcp-auth` |
| `MCP_AUTH_TOKEN` | Bearer token protecting the MCP endpoint (32+ chars) |

The Cloud Run runtime service account is granted `roles/secretmanager.secretAccessor`
on each. Secrets are entered interactively by the setup script so they do not
enter shell history.

Note the container filesystem is ephemeral, so `GARMIN_TOKENS_JSON` is
re-materialized on every cold start and garth's in-memory token refresh is
discarded. The underlying OAuth1 refresh token expires after roughly a year, at
which point `garmin-mcp-auth` must be re-run and the secret updated.

## 5. CI Authentication

GitHub Actions authenticates to Google via **Workload Identity Federation**, not
a service-account JSON key. GitHub mints a short-lived OIDC token which Google
exchanges for credentials, so no long-lived secret exists in the repository.

The workload identity provider carries an attribute condition restricting it to
the repository owner, and the deploy service account grants
`roles/iam.workloadIdentityUser` only to the principal set for this specific
repository. Both constraints are necessary: without them, any GitHub repository
could exchange a token for these credentials.

Workflow hardening carried over from code review:
- `permissions:` block limited to `contents: read` and `id-token: write`
- All actions pinned to commit SHAs rather than mutable tags or branches
- `concurrency` group so overlapping pushes cannot race a deploy

Deploy service account roles: `run.admin`, `cloudbuild.builds.editor`,
`artifactregistry.admin`, `storage.admin`, `logging.viewer`,
`iam.serviceAccountUser`.

## 6. Build and Image Storage

`gcloud run deploy --source .` builds via Cloud Build, avoiding a separate
registry setup step. Images land in the `cloud-run-source-deploy` Artifact
Registry repository.

Artifact Registry's free storage is 0.5 GB and the image is roughly 250 MB, so a
cleanup policy retains the two most recent versions and deletes anything older
than 7 days. The policy can only be applied after the first deploy creates the
repository.

## 7. Claude Desktop Config

```json
{
  "mcpServers": {
    "garmin": {
      "url": "https://<cloud-run-url>/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

The service URL is printed at the end of each deploy run.

---

## Implementation Order

1. `gcloud auth login` as a user account, create the project, enable billing
2. Run `scripts/setup-cloudrun.sh` — APIs, secrets, deploy service account, WIF,
   GitHub repo variables
3. Push to `main` — triggers the first deploy
4. Verify `/health` returns 200 and unauthenticated `/mcp` returns 401
5. Re-run the setup script so the Artifact Registry cleanup policy applies
6. Update Claude Desktop config with the new URL
7. `flyctl apps destroy garmlink` once the new deployment is confirmed

## Files Changed

| File | Action |
|---|---|
| `.github/workflows/deploy.yml` | New |
| `scripts/setup-cloudrun.sh` | New |
| `.github/workflows/fly.yml` | Deleted |
| `fly.toml` | Deleted |
| `README.md` | Updated |
| `.env.example` | Updated |

## Open Risks

- **Free-tier figures are point-in-time.** The allowances quoted in §2 come from
  documentation current at time of writing. This project has already been burned
  once by stale pricing; verify before relying on them.
- **Garmin auth runs in the lifespan, before the server listens.** A slow or
  failing Garmin response consumes the container's startup budget on every cold
  start. Scaling to zero makes cold starts routine, so this is more exposed here
  than it was on an always-on host. There is currently no re-authentication path
  and `/health` does not check Garmin session state.
- **CI does not run the test suite before deploying.** The contract and
  behavioural tests added in `9f21955` are not wired into the workflow, so a
  broken push still reaches production.
