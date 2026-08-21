# Handoff: OAuth for the claude.ai connector (mobile + web)

> Written 2026-08-20 at the end of a long session. Everything below the
> "Current state" section is verified fact unless marked as an open question.

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

## Open questions — resolve these first

1. **Dual auth or OAuth-only?** Claude Code and Desktop work today on the static
   bearer. Both also support OAuth for remote MCP, so an OAuth-only server is
   coherent and retires the static token entirely — but it means reconfiguring
   the working clients. Supporting both is more code and more attack surface.
2. **Which provider?** Google is the assumption, not a decision. GitHub is also
   bundled and the account exists.
3. **Single-user or shareable?** Everything so far assumes one user. An email
   allowlist is the simplest enforcement.
4. **What happens to `MCP_AUTH_TOKEN`?** If OAuth-only, the secret and
   `resolve_auth_token()` go away. If dual, decide precedence.

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
  ranges.py     bounded per-day fan-out helper
  prompts.py    the 4 coaching workflows as MCP prompts
  tools/        11 mounted routers, 45 tools
tests/          5 files, all run in CI
scripts/setup-cloudrun.sh   one-time GCP setup (idempotent)
```

Auth today lives entirely in `server.py`: `resolve_auth_token()` reads
`MCP_AUTH_TOKEN` and **fails closed** (missing/blank/<32 chars aborts startup;
`ALLOW_UNAUTHENTICATED=1` is the deliberate local-dev escape hatch), and
`BearerAuthMiddleware.dispatch()` exempts only `/health` and uses
`hmac.compare_digest`.

## Gotchas learned the hard way this session

Each of these cost real debugging time. They will bite again.

1. **Mounted sub-servers do not see the parent lifespan.** A mounted FastMCP
   server's `lifespan_context` is its own empty dict. Reading the client from
   `ctx.lifespan_context` raised `KeyError('garmin')` for all 45 tools — the
   server passed health checks and listed tools while nothing could run. Hence
   `deps.py` holding the client. **Any OAuth state must not be stashed in the
   lifespan context and expected to reach tools.**
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

- **`MCP_AUTH_TOKEN` was pasted into a chat transcript and never rotated.**
  Rotate it, or let the OAuth work retire it. Rotating:
  `NEW=$(openssl rand -hex 32); printf '%s' "$NEW" | gcloud secrets versions add MCP_AUTH_TOKEN --project=garmlink --data-file=-`
  then force a new revision so the running instance picks it up.
- **`FLY_API_TOKEN` is still a GitHub repo secret** for a destroyed Fly account:
  `gh secret delete FLY_API_TOKEN --repo knahsirV/garmlink`
- **The local working directory is still `~/Comp Sci Projects/garmin-mcp`.**
  `mv ../garmin-mcp ../garmlink` when no session is running in it.
- **No logging anywhere in the server.** The next production issue will leave no
  trace. Worth doing before adding an auth flow that can fail in new ways.
- **`get_swim_activities` returned 0 results over 30 days.** Plausible, but that
  tool was broken until this session — sanity-check it against a window with
  known swims.

## Verification commands

```bash
# Full suite — use Python 3.12
python tests/test_garmin_contract.py
python tests/test_critical_fixes.py
python tests/test_auth_lifecycle.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 python tests/test_tool_dispatch.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 python tests/test_prompts.py

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

## Suggested first move in the new session

Invoke `superpowers:brainstorming`, state the architectural classification, and
work the four open questions above — starting with dual-auth vs OAuth-only,
since it determines everything else. Read `src/garmlink/server.py` first; it is
the whole of the current auth model in about 70 lines.
