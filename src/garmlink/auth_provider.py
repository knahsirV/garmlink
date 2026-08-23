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

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

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
