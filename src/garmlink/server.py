"""FastMCP server entry point for Garmin MCP."""

from __future__ import annotations

import base64
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .client import GarminClient
from .deps import get_garmin_or_none, set_client
from .logs import ToolCallLoggingMiddleware, logger, setup_logging
from .tools.daily import mcp as daily_mcp
from .tools.activities import mcp as activities_mcp
from .tools.training import mcp as training_mcp
from .tools.running import mcp as running_mcp
from .tools.cycling import mcp as cycling_mcp
from .tools.swimming import mcp as swimming_mcp
from .tools.strength import mcp as strength_mcp
from .tools.workouts import mcp as workouts_mcp
from .tools.profile import mcp as profile_mcp
from .tools.insights import mcp as insights_mcp
from .prompts import mcp as coaching_mcp

load_dotenv()


# ---------------------------------------------------------------------------
# Bearer-auth HTTP middleware
# ---------------------------------------------------------------------------

MIN_TOKEN_LENGTH = 32


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the expected bearer token.

    The token is required: `resolve_auth_token()` refuses to start the server
    without one, so this middleware never runs in an open mode. Only /health is
    exempt, so Fly's health check works without credentials.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        if not token:
            raise RuntimeError("BearerAuthMiddleware requires a non-empty token")
        self._token = token

    async def dispatch(self, request: Request, call_next):
        # Always allow the health-check endpoint through.
        if request.url.path == "/health":
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        # compare_digest keeps the comparison time independent of how many
        # leading bytes the presented token got right.
        if not auth.startswith("Bearer ") or not hmac.compare_digest(
            auth[7:], self._token
        ):
            # The presented credential is deliberately never logged: it is
            # either a near-miss of the real secret or somebody else's, and a
            # log stream is a bad place for both. Only the reason is recorded.
            reason = "missing_bearer_prefix" if not auth.startswith("Bearer ") else "bad_token"
            logger.warning("auth.reject", extra={"fields": {
                "path": request.url.path, "reason": reason,
            }})
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


def resolve_auth_token() -> str | None:
    """Return the bearer token, or None when explicitly running unauthenticated.

    Fails closed: a missing or too-short MCP_AUTH_TOKEN aborts startup rather
    than silently serving personal health data to anyone who finds the URL.
    Local development can opt out with ALLOW_UNAUTHENTICATED=1, which is loud
    and deliberate.
    """
    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    if not token:
        if os.getenv("ALLOW_UNAUTHENTICATED") == "1":
            return None
        raise RuntimeError(
            "MCP_AUTH_TOKEN is required. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set it "
            "(Fly: `flyctl secrets set MCP_AUTH_TOKEN=...`). "
            "To run without auth on localhost, set ALLOW_UNAUTHENTICATED=1."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"MCP_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters "
            f"(got {len(token)})."
        )
    return token


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialise the GarminClient and expose it via the lifespan context."""
    email = os.getenv("GARMIN_EMAIL", "")
    if not email:
        raise RuntimeError("GARMIN_EMAIL environment variable is required")
    password = os.getenv("GARMIN_PASSWORD", "")

    tokens_b64 = os.getenv("GARMIN_TOKENS_JSON", "")
    if tokens_b64:
        # Production: tokens are base64-encoded and stored in an env var.
        token_dir = Path("/tmp/garmin_tokens")
        token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file = token_dir / "garmin_tokens.json"
        try:
            token_data = base64.b64decode(tokens_b64)
        except Exception as e:
            raise RuntimeError(f"GARMIN_TOKENS_JSON is not valid base64: {e}") from e
        token_file.write_bytes(token_data)
        tokenstore = str(token_dir)
    else:
        # Local dev: read tokens from the default garminconnect directory.
        tokenstore = str(Path.home() / ".garminconnect")

    client = GarminClient(
        email=email,
        password=password,
        tokenstore_path=tokenstore,
    )
    # Deliberately NOT authenticating here. The service scales to zero, so a
    # Garmin round-trip in the startup path is paid on every cold start and a
    # Garmin outage would fail the deploy. The client authenticates on first
    # use and re-authenticates itself when a session dies; /readyz reports
    # where it stands. Config that can be checked without the network (email
    # present, tokens decodable) is still validated above.
    set_client(client)

    logger.info("startup", extra={"fields": {
        "tools": len(await server.list_tools()),
        "prompts": len(await server.list_prompts()),
        # Which token source won matters: a deploy that silently fell back to
        # the local tokenstore would authenticate as nobody and fail lazily.
        "token_source": "secret" if tokens_b64 else "local_tokenstore",
        "auth": "disabled" if os.getenv("ALLOW_UNAUTHENTICATED") == "1" else "bearer",
    }})

    try:
        yield {"garmin": client}
    finally:
        logger.info("shutdown")
        set_client(None)
        client.close()


# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP("garmlink", lifespan=lifespan)

# Mount all tool sub-servers onto the main app.
mcp.mount(daily_mcp)
mcp.mount(activities_mcp)
mcp.mount(training_mcp)
mcp.mount(running_mcp)
mcp.mount(cycling_mcp)
mcp.mount(swimming_mcp)
mcp.mount(strength_mcp)
mcp.mount(workouts_mcp)
mcp.mount(profile_mcp)
mcp.mount(insights_mcp)

# Coaching workflows, exposed as MCP prompts rather than tools.
mcp.mount(coaching_mcp)

# Registered on the parent, after the mounts. FastMCP runs the parent's
# middleware chain before resolving a tool, and mount aggregation happens during
# resolution — so this one registration covers all 45 mounted tools. (Verified
# in test_logging.py rather than assumed: mounted servers do *not* inherit the
# parent's lifespan, so sub-server inheritance is not something to trust here.)
mcp.add_middleware(ToolCallLoggingMiddleware())


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness only — deliberately does not touch Garmin.

    This is the one route the auth middleware lets through, and nothing about
    Garmin's availability should be able to make the platform cycle the
    container. For Garmin session state, use /readyz.
    """
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(request: Request) -> JSONResponse:
    """Garmin session state, for diagnosing a deploy.

    Sits behind the bearer token (only /health is exempt) because it exposes
    internal state. Reports only — it never triggers an authentication attempt,
    so before the first tool call it honestly says "never". Returns 503 when the
    last attempt failed so `curl -f` is meaningful; nothing gates on it.
    """
    client = get_garmin_or_none()
    if client is None:
        return JSONResponse({"garmin": "unavailable"}, status_code=503)
    status = client.auth_status()
    code = 503 if status["garmin"] == "error" else 200
    return JSONResponse(status, status_code=code)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn

    setup_logging()

    port = int(os.getenv("PORT", "8000"))
    auth_token = resolve_auth_token()

    if auth_token is None:
        logger.warning(
            "ALLOW_UNAUTHENTICATED=1 — serving with NO authentication. "
            "Never do this on a public address."
        )
        middleware = []
    else:
        middleware = [Middleware(BearerAuthMiddleware, token=auth_token)]
    http_app = mcp.http_app(middleware=middleware)

    uvicorn.run(http_app, host="0.0.0.0", port=port)
