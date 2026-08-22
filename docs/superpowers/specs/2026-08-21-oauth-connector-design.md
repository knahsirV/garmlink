# Design: OAuth for the claude.ai connector

> Status: approved design, ready for an implementation plan.
> Supersedes the open questions in `docs/handoff-oauth-connector.md`.

## Why

`garmlink` authenticates with a static bearer token. claude.ai's custom-connector
UI accepts only OAuth 2.0 — Authorization URL, Token URL, Client ID, Client
Secret — with no field for a bearer token or a custom `Authorization` header. So
the server works from Claude Code and Claude Desktop and cannot be added to
claude.ai at all. Mobile is not separately configurable; it syncs connectors from
claude.ai, so this is also the only path to mobile.

The outcome: `garmlink` is addable as a custom connector on claude.ai, reachable
from mobile, and the static bearer token stops existing.

## Decisions

Settled in brainstorming; treat as given, not as assumptions.

| Question | Decision |
|---|---|
| Dual auth or OAuth-only? | **OAuth-only, one cutover.** The bearer path is deleted in the same change. |
| Identity provider | **GitHub.** Four fields, no consent screen, no publishing status, no verification review. |
| Single-user or shareable? | **Env-var allowlist, fails closed.** `GITHUB_ALLOWED_USERS`; empty or unset aborts startup. |
| `MCP_AUTH_TOKEN` | **Retired.** Version 1 destroyed and the secret deleted — after live verification passes. |
| OAuth state storage | **Firestore.** The default file store cannot survive scale-to-zero. |
| `/readyz` | **Separate diagnostic secret**, not the OAuth token. |

The rollback risk of an OAuth-only cutover was accepted deliberately: if OAuth
misbehaves in production there is no working path to the server, and Claude Code
and Desktop get reconfigured in the same breath. The mitigation is the phased
live verification in "Verification" below, which is the reason that section is
ordered the way it is rather than being a checklist appended at the end.

A hard constraint worth restating, because it forecloses a whole class of future
requests: `deps.py` holds exactly one `GarminClient` for one Garmin account. So
"shareable" can only ever mean *other people read your health data*. Other people
connecting their own Garmin accounts is a multi-tenant redesign, not a config
change.

## Verified findings

Established by inspecting `fastmcp` 3.4.7 in `.venv`. Each one changes the
design; none were assumed.

1. **`GitHubProvider` has no identity restriction and no injection point.**
   `GitHubTokenVerifier.verify_token` accepts any token minted for the OAuth app,
   so by default anyone with a GitHub account who completes the flow reaches the
   health data. Worse for the original plan: `GitHubProvider.__init__`
   *constructs its own* verifier (`providers/github.py:285-291`) and exposes no
   `token_verifier` parameter. `OAuthProxy`, its base class, does take one as a
   public parameter.
2. **`OAuthProxy.verify_token` delegates to the token verifier on every
   request** (`oauth_proxy/proxy.py:1848`), not only at token exchange. A
   verifier returning `None` therefore fails closed on every call, and revoking
   access is an env-var edit plus redeploy rather than a wait for expiry.
3. **Auth wires into the constructor, not as ASGI middleware.** `self.auth` is a
   plain attribute (`server/server.py:424`) and `http_app()` reads it at call
   time (`mixins/transport.py`). So `mcp` can stay module-level and `main()` can
   assign `mcp.auth` — test imports never need GitHub credentials.
4. **`RequireAuthMiddleware` wraps only the `/mcp` route.** Custom routes are
   appended outside it (`server/http.py:632-636`), so switching to
   `FastMCP(auth=...)` silently makes `/readyz` public.
5. **But `auth.get_middleware()` is applied app-wide** (`auth/auth.py:335-341`),
   so `request.scope["user"]` is populated even on custom routes. Only
   *enforcement* is missing there.
6. **`jwt_signing_key` defaults to being derived from the upstream client
   secret** — deterministic across cold starts, so issued tokens survive
   restarts. No action needed.
7. **The default `client_storage` is a file store on ephemeral local disk**,
   holding DCR registrations, OAuth transactions, authorization codes, and
   refresh-token mappings. This service scales to zero, so a cold start wipes all
   of it. In claude.ai this surfaces as "randomly asks me to reconnect."
8. **`FirestoreStore` requires the `firestore` extra**, which resolves to
   `google-cloud-firestore` plus 10 transitive packages including `grpcio` and
   `protobuf`.

## Architecture

### Auth provider and the allowlist

New file `src/garmlink/auth_provider.py`. `server.py` is already the mounts, the
lifespan, the routes and `main()`; the space freed by deleting the bearer code is
not a budget to spend on a larger auth block.

```
AllowlistedGitHubTokenVerifier(GitHubTokenVerifier)
    verify_token(token):
        result = await super().verify_token(token)   # GitHub API round-trip
        if result is None:
            return None                              # invalid token
        login = result.claims.get("login")
        if not login or login not in self._allowed:
            log auth.reject{reason=not_allowlisted, login=login}
            return None
        return result
```

Wired by constructing `OAuthProxy` **directly** — GitHub's two upstream endpoint
URLs, plus our verifier through the public `token_verifier` parameter. The
alternative, subclassing `GitHubProvider` and reassigning the private
`self._token_validator` afterwards, is fewer lines but depends on a private
attribute under a floating `fastmcp>=3.4,<4` pin. That is gotcha 4 in a different
costume; ten explicit constructor arguments are the cheaper trade.

`login` comes from GitHub's `/user` response (`providers/github.py:159`). It is a
public username, not a credential, so logging it is safe — and it is the single
most useful field when the connector misbehaves. The presented token is never
logged.

Because the allowlist runs inside the verifier, a rejected user never receives an
`AccessToken` at all, which means the same check also covers any route that
inspects `request.scope["user"]`.

### Storage

`FirestoreStore(project="garmlink", default_collection="oauth")` using
Application Default Credentials — on Cloud Run that is the runtime service
account, so no key material exists anywhere. Passed as `client_storage=` to the
`OAuthProxy`.

The store lazily runs `_setup()` before first use, so no `async with` is required
for correctness, but it owns a Firestore `AsyncClient` that would otherwise never
be closed. Register it through a `deps.py`-style holder and close it in the
lifespan's `finally`, beside the existing `client.close()`.

**No startup connectivity probe.** That is the same call the lifespan already
makes for Garmin and for the same reason: a network round-trip in the startup
path is paid on every cold start. Instead the `startup` log line gains a
`storage` field (`firestore` / `file`), mirroring `token_source`, so a deploy
that silently fell back to the wrong backend is visible rather than lazily
mysterious.

`google-cloud-firestore` is **pinned exactly** (gotcha 4). Its cold-start cost
should be measured once during implementation; if it degrades badly that is a
data point for revisiting, not a reason to pre-optimize.

### Route surface

`auth.get_routes()` adds seven routes, all necessarily public — discovery and the
flow itself must work before the client holds a token:

```
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource/mcp
/authorize   /token   /register   /auth/callback   /consent
```

Plus `/health` (public, unchanged) and `/mcp` (wrapped in
`RequireAuthMiddleware`).

This inverts the current threat model and the inversion should be explicit. Today
exactly one route is public and secrecy does real work. Afterwards **eight**
routes are public and the only thing between a stranger and the health data is
the allowlist. That is how OAuth works — but it means the allowlist is not
defense-in-depth, it is *the* defense, which is why it fails closed and is
enforced on every request.

`/readyz` gets a `READYZ_TOKEN` guard compared with `hmac.compare_digest`,
rejecting via the same `auth.reject` log line (`reason=bad_readyz_token`) and
never logging the presented value. It is deliberately a fossil of the code being
deleted, kept for one read-only route, because it is the only diagnostic that
still answers when OAuth is the broken thing. An OAuth access token is obtainable
only by completing a browser flow; a secret is obtainable with `gcloud`.

Two provider settings stay at their defaults, deliberately:

- `require_authorization_consent=True` — one extra click per new client
  registration, and the screen is the visible signal if a registration you did
  not initiate ever occurs.
- `allowed_client_redirect_uris=None` — narrowing these to claude.ai's callback
  would break Claude Code, which uses a localhost callback. The allowlist already
  gates who receives a token.

GitHub OAuth app callback URL:
`https://garmlink-moz6szqd6q-uc.a.run.app/auth/callback`, matching the default
`redirect_path`.

### Configuration

`resolve_auth_token()` is replaced by `build_auth_provider() -> AuthProvider |
None`, keeping its exact fail-closed character.

| Var | Source | Missing or blank |
|---|---|---|
| `GITHUB_CLIENT_ID` | Secret Manager | abort startup |
| `GITHUB_CLIENT_SECRET` | Secret Manager | abort startup |
| `READYZ_TOKEN` | Secret Manager | abort startup |
| `PUBLIC_BASE_URL` | plain env var | abort startup |
| `GITHUB_ALLOWED_USERS` | plain env var | abort startup |

`GITHUB_ALLOWED_USERS` and `PUBLIC_BASE_URL` are plain env vars rather than
secrets: GitHub logins and a public URL are not credentials, and as plain vars
they appear directly in `gcloud run services describe` — exactly where someone
looks when wondering why a login is being rejected. Neither weakens fail-closed
startup.

`ALLOW_UNAUTHENTICATED=1` survives unchanged: `build_auth_provider()` returns
`None`, `mcp.auth` stays unset, same loud warning. Three test files depend on it
and it is the only way to run locally without a GitHub app.

**Deleted:** `BearerAuthMiddleware`, `resolve_auth_token()`, `MIN_TOKEN_LENGTH`,
and the now-unused `Middleware` / `BaseHTTPMiddleware` imports. `hmac` stays for
the `/readyz` guard.

**Secret Manager:** add `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
`READYZ_TOKEN`. Then destroy `MCP_AUTH_TOKEN` version 1 and delete the secret —
**only after live verification passes**, never in the same breath as the deploy.
Keeping it an extra hour costs nothing and it is the last thing that can confirm
"the service is fine, the OAuth layer is not."

**`.github/workflows/deploy.yml:90`:** `--set-secrets` swaps `MCP_AUTH_TOKEN` for
the three new entries; `--set-env-vars` carries the two plain ones.

**`scripts/setup-cloudrun.sh`** (already the idempotent one-time-setup script)
gains: enable `firestore.googleapis.com`, create the Native-mode database in
`us-central1`, grant `roles/datastore.user` to the runtime service account.
Gotcha 3 applies — expect eventual consistency, wait and retry before debugging.

### Logging

`setup_logging()` installs its handler on the `garmlink` logger only, with
`propagate=False` (`logs.py:227-234`). Today that is nearly free, because auth is
fifteen lines of our own code and every `auth.reject` is our call site. After the
cutover the **entire OAuth flow logs through `fastmcp.server.auth.*`** — consent,
registration, token exchange, upstream refresh failures — and all of it would
land outside the structured pipeline: not JSON, no `severity` for Cloud Logging
to lift, never passed through `redact()`. The handoff names "Reading the logs" as
the first move when the OAuth flow misbehaves, and as things stand the OAuth logs
would not be in that stream at all.

Two changes to `logs.py`:

1. Attach the same handler to the `fastmcp` logger alongside `garmlink`.
2. Add a `logging.Filter` applying `redact()` to `record.msg` and `record.args`.
   Our own code redacts at the call site, which third-party loggers cannot do,
   and fastmcp's auth paths log upstream response bodies. The existing
   `_TOKENISH` pattern already matches GitHub tokens and JWT segments.

`LOG_LEVEL` stays `INFO` in production — fastmcp's auth modules log
token-adjacent detail at DEBUG.

Event changes: `auth.reject` gains `reason=not_allowlisted` (with `login`) and
`reason=bad_readyz_token`; `startup` swaps `auth: "bearer"` for
`auth: "github_oauth"` and gains `storage`. Both never-logged invariants hold —
no tool results, no presented credentials.

## Verification

### Unit tests

No network. Gotcha 2 applies throughout: patch the `GarminClient` constructor, or
the suite silently hits the live Garmin API using real tokens at
`~/.garminconnect`.

- `AllowlistedGitHubTokenVerifier` with the parent's `verify_token` stubbed:
  allowlisted login passes through; non-allowlisted returns `None` and logs
  `reason=not_allowlisted`; parent-returns-`None` stays `None`; a missing `login`
  claim fails closed rather than raising.
- `build_auth_provider()` fail-closed matrix — each of the five vars missing or
  blank aborts startup; `ALLOW_UNAUTHENTICATED=1` returns `None`. Replaces the
  `resolve_auth_token` block in `tests/test_critical_fixes.py`.
- `/readyz`: no header → 401, wrong token → 401, correct token → 200/503, and the
  presented value never appears in log output. Replaces `tests/test_logging.py:449`.
- The redaction filter: a record logged on the `fastmcp` logger containing a
  token-shaped run comes out `[redacted]`.
- **Route-surface assertion** — build the app with a dummy provider and assert
  the exact set of unauthenticated paths. This is the test that would have caught
  `/readyz` going public, and it will catch it again on a future `fastmcp` bump.

Gotcha 5: after the change, confirm 45 tools still list. Per finding 3 the auth
no longer goes through `BaseHTTPMiddleware` at all, so the original A/B may not
apply — but confirm rather than assume.

### Live verification

Ordered so that failures arrive separable rather than fused.

**Phase 0 — before any auth code changes.** Create the GitHub OAuth app, the
Firestore database, and the IAM binding, and add the three secrets, *while bearer
auth is still live and working*. This is the highest-value sequencing decision
here: it separates "the GCP setup is wrong" from "the auth code is wrong," which
otherwise arrive together on a service that can no longer be reached.

**Phase 1 — deploy the cutover.** `/health` → 200; `/mcp` unauthenticated → 401
carrying a `WWW-Authenticate` header; `/.well-known/oauth-protected-resource/mcp`
→ 200; `/readyz` with `READYZ_TOKEN` → 200.

**Phase 2 — Claude Code first.** Fastest loop and locally controlled. Expect 45
tools, 4 prompts, and one real `get_devices` call.

**Phase 3 — claude.ai connector, then mobile.** The actual goal.

**Phase 4 — the cold-start test.** Force scale-to-zero, then call from claude.ai
*without* reconnecting. This is the only check that distinguishes the Firestore
store working from it appearing to work, and no test suite can perform it. A
demanded reconnect means Firestore is not wired correctly.

**Phase 5 — prove the gate gates.** Temporarily set `GITHUB_ALLOWED_USERS` to a
login that is not yours, redeploy, confirm your own flow fails with
`reason=not_allowlisted` in the logs, then restore. An allowlist never tested
against a rejection is an allowlist you are assuming.

Only after all five phases: destroy `MCP_AUTH_TOKEN` version 1 and delete the
secret.

Gotcha 6 is why this section exists in this form: every one of the seven bugs
fixed in the deployment session survived a green test suite at some point.

## Files

```
src/garmlink/auth_provider.py   NEW — AllowlistedGitHubTokenVerifier,
                                build_auth_provider()
src/garmlink/server.py          delete bearer auth; assign mcp.auth in main();
                                guard /readyz; startup log fields
src/garmlink/deps.py            holder for the Firestore store
src/garmlink/logs.py            fastmcp logger handler + redaction filter
pyproject.toml                  google-cloud-firestore, pinned exactly
scripts/setup-cloudrun.sh       Firestore API, database, IAM role
.github/workflows/deploy.yml    secrets and env vars
tests/test_critical_fixes.py    replace the resolve_auth_token block
tests/test_logging.py           replace the BearerAuthMiddleware block
tests/test_auth_provider.py     NEW — allowlist, /readyz, route surface
```

`tests/test_auth_lifecycle.py` is about the *Garmin* client's session lifecycle
and is untouched by this work.
