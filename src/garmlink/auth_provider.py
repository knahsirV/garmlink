"""GitHub OAuth for the claude.ai connector.

claude.ai's custom-connector UI accepts only OAuth 2.0 — there is no field for a
static bearer token — so this module replaces the bearer auth that `server.py`
used to own.

The important thing to understand here: FastMCP's bundled `GitHubProvider`
applies NO identity restriction. Its verifier accepts any token minted for our
OAuth app, so with stock settings every GitHub user on earth completes the flow
and reaches the health data. The allowlist below is the whole of the access
control, which is why it fails closed in three separate ways: an unset variable
aborts startup, an unknown login is denied, and a missing `login` claim is
denied rather than raising.
"""

from __future__ import annotations

import os

from fastmcp.server.auth.auth import AccessToken, AuthProvider
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from key_value.aio.protocols import AsyncKeyValue

from . import deps
from .logs import logger


class AllowlistedGitHubTokenVerifier(GitHubTokenVerifier):
    """A GitHub token verifier that also checks *who* the token belongs to.

    `OAuthProxy` delegates to this on every request rather than only at token
    exchange, so removing a login from the allowlist takes effect on the next
    call instead of whenever the token happens to expire.
    """

    def __init__(self, *, allowed_logins: frozenset[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._allowed = allowed_logins

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            # Invalid, expired, or GitHub is unreachable. Already the 401 path.
            return None

        login = (result.claims or {}).get("login")
        if not login or login not in self._allowed:
            # The login is a public GitHub username, not a credential, and it is
            # the single most useful field when a connector stops working. The
            # presented token is deliberately never logged.
            logger.warning("auth.reject", extra={"fields": {
                "path": "/mcp",
                "reason": "not_allowlisted",
                "login": login or "unknown",
            }})
            return None

        return result


# GitHub's OAuth endpoints. Specified here rather than reached through
# `GitHubProvider` because that class constructs its own token verifier and
# exposes no injection point for ours — and reassigning its private
# `_token_validator` afterwards would be a private attribute under a floating
# `fastmcp>=3.4,<4` pin.
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

_REQUIRED = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
             "PUBLIC_BASE_URL", "GITHUB_ALLOWED_USERS")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required. Set it on the service "
            f"(`gcloud run services update garmlink --set-env-vars/--set-secrets`). "
            f"To run without auth on localhost, set ALLOW_UNAUTHENTICATED=1."
        )
    return value


def build_oauth_store() -> AsyncKeyValue:
    """Firestore-backed store for OAuth state.

    The default store is a file tree on local disk. This service scales to
    zero, so a cold start would wipe every DCR client registration and
    refresh-token mapping — which claude.ai surfaces as "randomly asks me to
    reconnect", from a server that looks perfectly healthy.

    Credentials come from Application Default Credentials, which on Cloud Run
    is the runtime service account: no key material anywhere.

    The sanitization strategies are not optional. OAuthProxy keys client records
    by client_id, and we advertise `client_id_metadata_document_supported`, so
    Claude presents a URL — `https://claude.ai/oauth/claude-code-client-metadata`
    — rather than a DCR-issued UUID. A Firestore document ID cannot contain `/`,
    so the store's default passthrough strategy turns the first lookup into
    `InvalidArgument: ... lacks a collection id` and the browser flow dies with a
    500. The DCR path hides this, because a UUID has no slashes. The strategies
    leave slash-free IDs untouched, so registrations already written keep theirs.
    """
    from key_value.aio.stores.firestore import FirestoreStore
    from key_value.aio.stores.firestore.store import (
        FirestoreV1CollectionSanitizationStrategy,
        FirestoreV1KeySanitizationStrategy,
    )

    return FirestoreStore(
        project=os.getenv("GCP_PROJECT") or None,
        default_collection="oauth",
        key_sanitization_strategy=FirestoreV1KeySanitizationStrategy(),
        collection_sanitization_strategy=FirestoreV1CollectionSanitizationStrategy(),
    )


def build_auth_provider() -> AuthProvider | None:
    """Return the OAuth provider, or None when explicitly unauthenticated.

    Fails closed, exactly as the bearer token's `resolve_auth_token()` did:
    missing configuration aborts startup rather than silently serving personal
    health data to anyone who finds the URL. ALLOW_UNAUTHENTICATED=1 remains
    the loud, deliberate local-development escape hatch.
    """
    if os.getenv("ALLOW_UNAUTHENTICATED") == "1":
        return None

    values = {name: _require(name) for name in _REQUIRED}

    allowed = frozenset(
        login.strip() for login in values["GITHUB_ALLOWED_USERS"].split(",")
        if login.strip()
    )
    if not allowed:
        raise RuntimeError(
            "GITHUB_ALLOWED_USERS must name at least one GitHub login. "
            "It is the only access control on this server."
        )

    store = build_oauth_store()
    deps.set_oauth_store(store)

    return OAuthProxy(
        upstream_authorization_endpoint=GITHUB_AUTHORIZE_URL,
        upstream_token_endpoint=GITHUB_TOKEN_URL,
        upstream_client_id=values["GITHUB_CLIENT_ID"],
        upstream_client_secret=values["GITHUB_CLIENT_SECRET"],
        token_verifier=AllowlistedGitHubTokenVerifier(
            allowed_logins=allowed,
            required_scopes=["user"],
            # Without this, `verify_token` hits api.github.com/user and
            # .../user/repos on *every* request (OAuthProxy.load_access_token
            # runs it per call), which is two round-trips per tool call and
            # caps the service at ~2,500 MCP requests/hour against GitHub's
            # 5,000/hr limit. Caching does NOT weaken the allowlist: `_allowed`
            # is re-checked in `verify_token` above on every call regardless of
            # whether the parent returned a cached `AccessToken`, so a login
            # removed from the allowlist is still denied on its very next
            # request. Do not "fix" this back to None for security reasons.
            cache_ttl_seconds=300,
        ),
        base_url=values["PUBLIC_BASE_URL"],
        client_storage=store,
    )


def resolve_readyz_token() -> str | None:
    """Secret guarding /readyz, or None when running unauthenticated.

    Deliberately NOT the OAuth token. An OAuth access token can only be
    obtained by completing a browser flow, and /readyz has to stay reachable
    from a terminal precisely when the OAuth layer is what has broken — it is
    the diagnostic that separates "the service is sick" from "OAuth is sick".
    """
    if os.getenv("ALLOW_UNAUTHENTICATED") == "1":
        return None
    return _require("READYZ_TOKEN")
