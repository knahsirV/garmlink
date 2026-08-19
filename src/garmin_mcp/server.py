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
    client.authenticate()

    try:
        yield {"garmin": client}
    finally:
        client.close()


# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP("garmin-mcp", lifespan=lifespan)

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


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    auth_token = resolve_auth_token()

    if auth_token is None:
        print(
            "WARNING: ALLOW_UNAUTHENTICATED=1 — serving with NO authentication. "
            "Never do this on a public address.",
            flush=True,
        )
        middleware = []
    else:
        middleware = [Middleware(BearerAuthMiddleware, token=auth_token)]
    http_app = mcp.http_app(middleware=middleware)

    uvicorn.run(http_app, host="0.0.0.0", port=port)
