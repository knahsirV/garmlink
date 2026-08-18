"""FastMCP server entry point for Garmin MCP."""

from __future__ import annotations

import base64
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

load_dotenv()

# ---------------------------------------------------------------------------
# Bearer-auth HTTP middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the expected bearer token.

    When MCP_AUTH_TOKEN is empty / not set the middleware is a no-op so that
    local development works without any configuration.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        # Always allow the health-check endpoint through.
        if request.url.path == "/health":
            return await call_next(request)

        # If no token is configured, skip auth (local-dev mode).
        if not self._token:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != self._token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


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


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Context helper (used by tool modules)
# ---------------------------------------------------------------------------

def get_garmin(ctx) -> GarminClient:
    """Return the shared GarminClient from the request lifespan context."""
    return ctx.lifespan_context["garmin"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    auth_token = os.getenv("MCP_AUTH_TOKEN", "")

    middleware = [Middleware(BearerAuthMiddleware, token=auth_token)]
    http_app = mcp.http_app(middleware=middleware)

    uvicorn.run(http_app, host="0.0.0.0", port=port)
