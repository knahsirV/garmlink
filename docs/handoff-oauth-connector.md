# Handoff: OAuth for the claude.ai connector (mobile + web)

> Written 2026-08-20 at the end of a long session. Everything below the
> "Current state" section is verified fact unless marked as an open question.
>
> **Updated 2026-08-21.** Structured logging landed, the leaked
> `MCP_AUTH_TOKEN` was rotated, and the stale `FLY_API_TOKEN` was deleted — the
> outstanding non-OAuth list is now empty. The OAuth work itself is still
> unstarted and its four open questions are still open.
>
> **Updated again in a design session (env date 2026-08-20 — note this reads as
> earlier than the line above; the 08-21 stamp appears to be ahead of the real
> clock, so trust this ordering, not the dates).** **All four open questions are
> now answered**, and the fastmcp OAuth surface was inspected directly — see
> "Verified findings" for five facts that shape the design, two of which are
> traps (default OAuth state is stored on ephemeral local disk under a
> scale-to-zero service; `/readyz` silently becomes public). Still **no design
> doc, no spec, no code.** Decisions: OAuth-only cutover, GitHub as provider,
> fail-closed env-var allowlist, `MCP_AUTH_TOKEN` retired.

## The task

Replace the static bearer token with OAuth so `garmlink` can be added as a
**custom connector on claude.ai**, which is what makes it work on **mobile**.

Classified **architectural** — it replaces the auth model every client depends
on. The intended path is: clarifying questions → 2-3 approaches → sectioned
design → spec in `docs/superpowers/specs/` → `superpowers:writing-plans`.
Nothing has been designed or built yet; this handoff exists so that work can
start cold.

## Why it's needed

claude.ai's custom-connector UI only accepts **OAuth 2.0** — Authorization URL,
Token URL, Client ID, Client Secret. There is no field for a static bearer token
or a custom `Authorization` header, which is exactly what this server uses
today. Known gaps, still open:
[anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112),
[#411](https://github.com/anthropics/claude-ai-mcp/issues/411).

Mobile isn't separately configurable — it syncs connectors from claude.ai, so
fixing claude.ai fixes mobile.

| Client | Works today | Mechanism |
|---|---|---|
| Claude Code | yes | static bearer header |
| Claude Desktop | yes | static bearer header |
| claude.ai web | **no** | connector UI requires OAuth |
| Mobile | **no** | inherits from claude.ai |

## Research already done

`fastmcp` 3.4.7 (pinned `>=3.4,<4`) ships substantial OAuth support. Verified by
inspecting the installed package:

- `fastmcp.server.auth` submodules: `oauth_proxy`, `oidc_proxy`, `jwt_issuer`,
  `authorization`, `introspection`, `redirect_validation`, `ssrf`, `cimd`
- 16 bundled providers under `fastmcp.server.auth.providers`: `google`,
  `github`, `auth0`, `clerk`, `azure`, `aws`, `descope`, `discord`,
  `huggingface`, `keycloak`, `oci`, `propelauth`, `jwt`, `introspection`,
  `in_memory`, `debug`

**Leading candidate:** Google as the identity provider — the account and GCP
project already exist, and `providers/google` is bundled. Allowlist a single
email so nobody else's Google login reaches the health data.

**Why `OAuthProxy` matters:** MCP clients expect Dynamic Client Registration.
Google doesn't offer DCR. `OAuthProxy` presents DCR to the MCP client while
holding fixed upstream credentials — precisely this gap. Confirm this is still
how FastMCP recommends bridging before designing around it.

## Open questions — ALL FOUR RESOLVED (design session, 2026-08-20)

Answered by the user in a brainstorming session. No longer open; treat these as
decisions, not assumptions.

1. **Dual auth or OAuth-only? → OAuth-only, one cutover.** OAuth lands and the
   bearer path is deleted in the same change. The user was shown the rollback
   risk (no working path to the server if OAuth misbehaves in production, and
   Claude Code + Desktop get reconfigured in the same breath) and chose this
   anyway. **Mitigation owed: front-load live verification in the plan** — see
   gotcha 6, every bug this project has had survived a green test suite.
2. **Which provider? → GitHub.** Not Google. `GitHubProvider` and
   `GoogleProvider` take near-identical constructors, so this is about the
   identity provider's operational friction, not FastMCP code. GitHub: four
   fields, no consent screen, no publishing status, no verification review.
   Google was rejected because the real work is the OAuth consent screen, and
   an app in "Testing" publishing status has Google expire refresh tokens after
   ~7 days — the connector would silently log you out weekly. (That 7-day
   behavior was *not* verified against Google's docs this session; it only
   needs re-checking if someone revisits the provider choice.)
3. **Single-user or shareable? → env-var allowlist, fail closed.**
   `GITHUB_ALLOWED_USERS`, comma-separated logins; empty or unset **aborts
   startup**, mirroring what `resolve_auth_token()` does today. Operationally
   single-user, but adding a reader later is a secret edit, not a code change.
   **Note the hard constraint that settled this:** `deps.py` holds exactly one
   `GarminClient` for one Garmin account, so "shareable" can only ever mean
   *other people read your health data*. Other people connecting their own
   Garmin is a multi-tenant redesign, not a config option.
4. **What happens to `MCP_AUTH_TOKEN`? → retired entirely.** Follows from (1).
   `resolve_auth_token()` and `BearerAuthMiddleware` are deleted, version 1 of
   the secret gets destroyed (it is currently disabled, not destroyed), and the
   secret itself is deleted.

## Verified findings about fastmcp 3.4.7 OAuth

Established by inspecting the installed package this session. Each of these
changes the design; none were assumed.

1. **`GoogleProvider`/`GitHubProvider` have NO built-in identity restriction.**
   `GoogleTokenVerifier.verify_token` calls Google's `tokeninfo` and accepts any
   token minted for your OAuth app. With default settings **anyone with an
   account at the provider who completes the flow reaches the health data.** The
   allowlist is load-bearing, not a nice-to-have, and we implement it ourselves.
   **Planned enforcement point: subclass the GitHub token verifier.** It is the
   narrowest place, fails closed, reuses the existing 401 path, and lets us log
   `auth.reject` with `reason=not_allowlisted` while still never logging the
   presented credential.
2. **Auth wires into the constructor, not as ASGI middleware.**
   `FastMCP(auth=GitHubProvider(...))`. This *replaces* the current
   `mcp.http_app(middleware=[Middleware(BearerAuthMiddleware, ...)])` shape
   rather than sitting beside it. Relevant to gotcha 5: the `BaseHTTPMiddleware`
   A/B test may not even apply, since the auth no longer goes through one.
3. **`jwt_signing_key` defaults to being derived from the upstream client
   secret** — deterministic across instances and cold starts, so issued tokens
   survive restarts. No action needed. (If a future design supplies no client
   secret, the key becomes required.)
4. **Default `client_storage` is a `FileTreeStore` on local disk**
   (`settings.home / "oauth-proxy" / <key-fingerprint>`), holding DCR client
   registrations, OAuth transactions, authorization codes, refresh-token
   mappings, and JTI mappings. **This service scales to zero**, so a cold start
   wipes all of it, and a flow that starts on one instance can complete on
   another and fail. In claude.ai this surfaces as "randomly asks me to
   reconnect." **This is the single biggest architectural risk in the work.**
5. **`RequireAuthMiddleware` wraps ONLY the MCP endpoint route.** In
   `create_streamable_http_app`, custom routes are appended separately via
   `server._get_additional_http_routes()`, outside the auth wrapper. So
   switching to `FastMCP(auth=...)` **silently makes `/readyz` public** — it
   sits behind the bearer today and exposes Garmin session state. Must be
   explicitly guarded; `/health` stays the only open route, as now.

## Storage approach — recommended, NOT yet approved

The user ran out of context before approving. Three options were put up; the
recommendation is **A**.

- **A — Firestore-backed shared storage (recommended).** Wire `FirestoreStore`
  as `client_storage`. Keeps scale-to-zero, survives cold starts, correct across
  instances. Costs a new dependency, enabling the Firestore API, and a role on
  the runtime service account. Free tier is ample for one user. **Verified: the
  backend ships in `key_value.aio.stores.firestore` but raises `ImportError`
  without the `firestore` extra**, so `google-cloud-firestore` becomes a new
  dependency — **pin it exactly** (gotcha 4).
- **B — Pin to a single always-on instance.** `--min-instances=1
  --max-instances=1`, keep the default file store. No new deps or infra, but you
  pay for an idle instance continuously and every deploy or platform recycle
  wipes registrations and forces a reconnect. Trades one-time integration work
  for permanent low-grade flakiness that is annoying to diagnose.
- **C — GCS FUSE volume mount** at the file-store path. Keeps scale-to-zero and
  the default code path with no Python deps. **Argued against:** `FileTreeStore`
  does file locking across many small files, precisely GCS FUSE's weak spot.
  Plausible-looking and subtly broken.

Also still unanswered: whether `/readyz` should be guarded by the OAuth token or
by something simpler.

## Where this work stopped

Brainstorming, mid-flight. The four open questions are answered and the research
above is done; **no design doc, no spec, and no code exist yet.** The remaining
path is unchanged: finish the sectioned design → spec in
`docs/superpowers/specs/` → `superpowers:writing-plans`. Resume by confirming
the storage approach and the `/readyz` question, then write the spec.

## Current state (all verified live)

Deployed, healthy, and serving real Garmin data.

| | |
|---|---|
| Service URL | `https://garmlink-moz6szqd6q-uc.a.run.app` |
| MCP endpoint | `<url>/mcp` (streamable-HTTP) |
| GCP project | `garmlink` (number `401226208618`) |
| Region | `us-central1` |
| GitHub repo | `knahsirV/garmlink` |
| Python | 3.12 (`requires-python = ">=3.12"`) |
| Key deps | `garminconnect==0.3.11`, `fastmcp>=3.4,<4` |
| Surface | 45 tools, 4 prompts |

Endpoints: `/health` (200, the only unauthenticated route), `/mcp` (401 without
a valid bearer), `/readyz` (behind auth; reports Garmin session state, 503 when
the last auth attempt failed).

Secrets in Secret Manager: `GARMIN_EMAIL`, `GARMIN_TOKENS_JSON`,
`MCP_AUTH_TOKEN`.

CI: `.github/workflows/deploy.yml` runs five test files, then deploys on push to
`main`. Keyless auth via Workload Identity Federation — pool `github`, provider
`github-oidc`, service account `gh-deployer@garmlink.iam.gserviceaccount.com`.
**The impersonation binding is scoped to `attribute.repository/knahsirV/garmlink`**,
so renaming the repo again breaks deploys unless the binding is updated first.

## Where the code is

```
src/garmlink/
  server.py     BearerAuthMiddleware, resolve_auth_token(), lifespan,
                mounts, /health, /readyz, main()   ← the file OAuth replaces
  deps.py       set_client() / get_garmin() — the client holder
  client.py     GarminClient: lazy auth, re-auth on dead session, TTL cache
  cache.py      TTLCache
  logs.py       formatters, redaction, ToolCallLoggingMiddleware
  ranges.py     bounded per-day fan-out helper
  prompts.py    the 4 coaching workflows as MCP prompts
  tools/        11 mounted routers, 45 tools
tests/          6 files, all run in CI
scripts/setup-cloudrun.sh   one-time GCP setup (idempotent)
```

Auth today lives entirely in `server.py`: `resolve_auth_token()` reads
`MCP_AUTH_TOKEN` and **fails closed** (missing/blank/<32 chars aborts startup;
`ALLOW_UNAUTHENTICATED=1` is the deliberate local-dev escape hatch), and
`BearerAuthMiddleware.dispatch()` exempts only `/health` and uses
`hmac.compare_digest`. It now logs `auth.reject` at WARNING with the path and a
reason, and deliberately never logs the presented credential — keep that
property in whatever replaces it.

## Gotchas learned the hard way this session

Each of these cost real debugging time. They will bite again.

1. **Mounted sub-servers do not see the parent lifespan.** A mounted FastMCP
   server's `lifespan_context` is its own empty dict. Reading the client from
   `ctx.lifespan_context` raised `KeyError('garmin')` for all 45 tools — the
   server passed health checks and listed tools while nothing could run. Hence
   `deps.py` holding the client. **Any OAuth state must not be stashed in the
   lifespan context and expected to reach tools.**

   **But middleware is the opposite case, and this is now verified.** A
   `Middleware` registered on the *parent* with `mcp.add_middleware()` DOES fire
   for tools on mounted sub-servers: `FastMCP.call_tool` runs the middleware
   chain before resolving the tool, and mount aggregation happens during
   resolution. `ToolCallLoggingMiddleware` relies on this, and
   `tests/test_logging.py` pins it (mutation-checked: the test fails when the
   registration is removed). Registering on a *child* also works — FastMCP fires
   middleware at both levels — so beware double-registering an OAuth middleware
   and running it twice per call.
2. **Test isolation is fragile here.** The lifespan constructs a real
   `GarminClient`, so a fake registered via `deps.set_client()` gets
   overwritten — and with real tokens at `~/.garminconnect`, the suite silently
   hits the live Garmin API. `test_tool_dispatch.py` patches the
   `GarminClient` constructor instead. Do the same for anything auth-related.
3. **GCP is eventually consistent.** Service-account creation then immediate
   role binding fails with "does not exist". The `allUsers` invoker binding took
   ~1 minute to propagate, producing spurious 401s from Cloud Run's front door
   that looked exactly like an application bug. **Wait and retry before
   debugging.**
4. **Floating deps resolved to different libraries on different Pythons.**
   `garminconnect>=0.3.2` with `requires-python>=3.11` gave 0.3.2 locally and
   0.3.11 in CI, because 0.3.3+ requires 3.12. Local tests passed against an API
   production never ran. Now pinned exactly — keep it that way.
5. **`BaseHTTPMiddleware` was suspected and cleared.** A local A/B test showed
   45 tools listing correctly with and without it. It is a known hazard for
   streaming responses, so if OAuth middleware is added, re-run that A/B rather
   than assuming.
6. **Verify against the live service, not just tests.** Every one of the seven
   bugs fixed this session survived a green test suite at some point.

## Outstanding, unrelated to OAuth

Nothing. Every item on this list has been closed — see below.

## Closed since this doc was written (2026-08-21)

- **Structured logging shipped** (`3cd2d12`). `logs.py` holds the formatters,
  the redactor, and `ToolCallLoggingMiddleware`; diagnostics were added at the
  points that previously failed silently. Verified live: Cloud Logging parses
  `severity`, and `jsonPayload.name` / `.dur_ms` / `.cache` / `.reason` are
  queryable. See "Reading the logs" below — **do this first when the OAuth flow
  misbehaves**, since it is the only view of which tool ran.
- **`MCP_AUTH_TOKEN` rotated.** The leaked value was version 1; it is now
  **disabled** (not destroyed) and version 2 is live on revision
  `garmlink-00007-rip`. Confirmed old token → 401, new token → 200. The new
  value was never printed to a transcript; read it with
  `gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN --project=garmlink`.
  **If OAuth ends up retiring bearer auth entirely, destroy version 1 and delete
  the secret.**
- **`FLY_API_TOKEN` deleted.** The repo now has zero stored secrets; CI
  authenticates via Workload Identity Federation. Only old design docs still
  mention Fly.
- **The local working directory rename is done** — it is `~/Comp Sci Projects/garmlink`.
- **`get_swim_activities` returning 0 results was not a bug.** The user has not
  swum in that window. Closed, no action.

## Verification commands

```bash
# Full suite — Python 3.12 only. A 3.11 interpreter silently resolves
# garminconnect to 0.3.2 and tests a library production never runs (gotcha 4).
# Set up once with: python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python tests/test_garmin_contract.py
.venv/bin/python tests/test_critical_fixes.py
.venv/bin/python tests/test_auth_lifecycle.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_tool_dispatch.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_prompts.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_logging.py

# Live
URL=$(gcloud run services describe garmlink --region us-central1 --project garmlink --format='value(status.url)')
TOKEN=$(gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN --project=garmlink)
curl -s "$URL/health"                                    # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' "$URL/mcp"      # 401
curl -s -H "Authorization: Bearer $TOKEN" "$URL/readyz"  # authenticated
```

End-to-end MCP check (expects 45 tools, 4 prompts) — the pattern used
throughout this session:

```python
import asyncio, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    t = StreamableHttpTransport(os.environ["MCP_URL"],
        headers={"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"})
    async with Client(t) as c:
        print(len(await c.list_tools()), "tools", len(await c.list_prompts()), "prompts")
        print(await c.call_tool("get_devices", {}))

asyncio.run(main())
```

## Reading the logs

The server emits one JSON object per line; Cloud Run lifts `severity` into the
log viewer. **Start here when something misbehaves** — Cloud Run's own request
log shows every MCP call as an indistinguishable `POST /mcp`, so `tool.call` is
the only record of which of the 45 tools ran.

```bash
# Everything structured, newest first
gcloud logging read 'resource.type="cloud_run_revision"
  AND resource.labels.service_name="garmlink"
  AND jsonPayload.message!=""' --project=garmlink --limit=20 --freshness=1h \
  --format='value(severity, jsonPayload.message, jsonPayload.name, jsonPayload.dur_ms)'

# Only failures
... AND severity>=ERROR

# One tool's history
... AND jsonPayload.name="get_swim_activities"

# Auth rejections — will matter while bringing OAuth up
... AND jsonPayload.message="auth.reject"
```

Events: `startup` (tools, prompts, token_source, auth), `shutdown`, `tool.call`
(name, args, outcome, dur_ms, cache), `auth.reject` (path, reason),
`garmin.login` (outcome, dur_ms), `garmin.reauth`, `garmin.retry`.

Two things are never logged, and an OAuth implementation must preserve both:
**tool results** (the health data) and **presented credentials** on a rejected
request. Arguments and error strings pass through `redact()` in `logs.py`, which
strips token-shaped runs — reuse it for anything OAuth surfaces.

Set `LOG_LEVEL` (default INFO) and `LOG_FORMAT` (`json` on Cloud Run, `text`
locally) to change verbosity or format.

Two `startup` lines with different `instanceId`s are two container instances,
not a double-run — this was checked.

## Suggested first move in the new session

**Superseded — the four questions are answered.** Resume `superpowers:brainstorming`
at the approaches step: confirm the storage approach (A/B/C above) and whether
`/readyz` is guarded by the OAuth token or something simpler, then present the
sectioned design, write the spec, and hand off to `superpowers:writing-plans`.
Read `src/garmlink/server.py` first; it is the whole of the current auth model
in about 70 lines, and every line of it is being deleted.
