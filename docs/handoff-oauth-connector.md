# Handoff: OAuth for the claude.ai connector — **shipped 2026-08-23**

> This document used to describe the OAuth cutover as unstarted, with four open
> questions. It shipped. The body below is a short statement of what is now
> true; the reasoning, the alternatives, and the traps live in the spec and the
> plan, which remain accurate.
>
> - Design: `docs/superpowers/specs/2026-08-21-oauth-connector-design.md`
> - Plan: `docs/superpowers/plans/2026-08-21-oauth-connector.md`

## The auth model

`garmlink` authenticates with **GitHub OAuth**. An `OAuthProxy` is constructed
directly — not via `GitHubProvider`, which builds its own token verifier and
offers no injection point — wrapping `AllowlistedGitHubTokenVerifier`, a
`GitHubTokenVerifier` subclass that checks the caller's GitHub login against
`GITHUB_ALLOWED_USERS` on **every** request and fails closed. That allowlist is
not defense in depth; it is the defense, because a plain `GitHubTokenVerifier`
accepts any token minted for the OAuth app, which is every GitHub user alive.

OAuth state lives in **Firestore**, not the default on-disk store. The service
scales to zero, and a file-backed store would lose every client registration
and refresh-token mapping on each cold start — which claude.ai surfaces as
"randomly asks me to reconnect" from a server that looks healthy.

`/readyz` is guarded by its own `READYZ_TOKEN`, deliberately not the OAuth
token: an access token needs a browser flow, and `/readyz` has to stay
reachable from a terminal precisely when OAuth is the broken thing. `/health`
is public for the platform's liveness check. Everything else requires OAuth.

`MCP_AUTH_TOKEN` is **gone** — version 1 (the leaked value) destroyed and the
secret deleted on 2026-08-23. There is no bearer path in the code to fall back
to.

## Configuration

Startup aborts if any of these is missing or blank, rather than serving health
data to whoever finds the URL. `ALLOW_UNAUTHENTICATED=1` bypasses the lot and
is for local development only.

| Variable | Source | Notes |
|---|---|---|
| `GITHUB_CLIENT_ID` | Secret Manager | From the GitHub OAuth app |
| `GITHUB_CLIENT_SECRET` | Secret Manager | |
| `READYZ_TOKEN` | Secret Manager | Guards `/readyz` only |
| `PUBLIC_BASE_URL` | Plain env var | Not a credential, and you want it visible in `gcloud run services describe` |
| `GITHUB_ALLOWED_USERS` | Plain env var | Comma-separated logins; a value naming nobody aborts startup |

OAuth callback: `https://garmlink-moz6szqd6q-uc.a.run.app/auth/callback`.

## Verified live, 2026-08-23

Every phase of the plan's Task 7 passed against the deployed service:
`/readyz` 401s without its token and answers with it; `/mcp` 401s with a
correct `WWW-Authenticate`; the startup log reads `github_oauth firestore 45
4`; Claude Code and claude.ai both complete the flow, **including mobile**,
which was the point of the project; a forced cold start did not demand a
reconnect; and setting `GITHUB_ALLOWED_USERS` to a bogus value produced
`not_allowlisted` with the real login in the logs — and nothing else.

## One trap worth carrying forward

The first real connection attempt after the cutover returned a 500. Claude
identifies itself with a **Client ID Metadata Document** — a client_id that is
a URL, `https://claude.ai/oauth/claude-code-client-metadata` — rather than a
DCR-issued UUID. `OAuthProxy` keys client records by client_id, and a Firestore
document ID cannot contain `/`, so the store's default passthrough strategy
turned the first lookup into `InvalidArgument: ... lacks a collection id`.

Nothing caught it beforehand, including a direct `POST /register` probe, for
one reason: DCR issues UUIDs, and a UUID has no slashes. The fix passes
`FirestoreV1KeySanitizationStrategy` and its collection counterpart to
`FirestoreStore`; `tests/test_auth_provider.py` now pins it. **If you swap the
storage backend, check how it handles a URL-shaped key before you ship it.**
