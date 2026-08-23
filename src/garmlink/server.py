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
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth_provider import build_auth_provider, resolve_readyz_token
from .client import GarminClient
from .deps import get_garmin_or_none, get_oauth_store, set_client, set_oauth_store
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
        "auth": "disabled" if os.getenv("ALLOW_UNAUTHENTICATED") == "1" else "github_oauth",
        # Same reasoning as token_source: a deploy that silently fell back to
        # the ephemeral file store looks healthy and then forces a reconnect
        # after every cold start.
        "storage": "file" if get_oauth_store() is None else "firestore",
    }})

    try:
        yield {"garmin": client}
    finally:
        logger.info("shutdown")
        set_client(None)
        client.close()
        store = get_oauth_store()
        if store is not None:
            # Safe whether or not the store ever opened its client.
            await store.close()
            set_oauth_store(None)


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


# Set by main(). None means ALLOW_UNAUTHENTICATED=1, and /readyz is open.
_readyz_token: str | None = None


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness only — deliberately does not touch Garmin.

    Public, like /readyz — FastMCP's RequireAuthMiddleware wraps only /mcp, so
    both custom routes are reachable without a token. Nothing about Garmin's
    availability should be able to make the platform cycle the container. For
    Garmin session state, use /readyz.
    """
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(request: Request) -> JSONResponse:
    """Garmin session state, for diagnosing a deploy.

    Guarded by READYZ_TOKEN rather than by OAuth. FastMCP's RequireAuthMiddleware
    wraps only the /mcp route — custom routes are registered outside it — so this
    would otherwise be public the moment auth moved to OAuth, exposing Garmin
    session state to anyone who guessed the path.

    A separate secret, not the OAuth token, because an OAuth access token needs
    a browser flow to obtain and this endpoint has to answer from a terminal
    exactly when the OAuth layer is what has broken.

    Reports only — it never triggers an authentication attempt, so before the
    first tool call it honestly says "never". Returns 503 when the last attempt
    failed so `curl -f` is meaningful; nothing gates on it.
    """
    if _readyz_token is not None:
        presented = request.headers.get("Authorization", "")
        # Headers arrive as `str` (decoded latin-1 off the wire), so a non-ASCII
        # `Authorization` value is valid input here. `hmac.compare_digest`
        # raises TypeError comparing two non-ASCII `str`s, which would otherwise
        # 500 instead of 401 and land outside the JSON log pipeline. Comparing
        # bytes sidesteps that entirely.
        if not presented.startswith("Bearer ") or not hmac.compare_digest(
            presented[7:].encode(), _readyz_token.encode()
        ):
            # The presented credential is deliberately never logged.
            logger.warning("auth.reject", extra={"fields": {
                "path": "/readyz", "reason": "bad_readyz_token",
            }})
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

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

    global _readyz_token

    setup_logging()

    port = int(os.getenv("PORT", "8000"))

    # Both fail closed on missing configuration, and both run before the app is
    # built so a misconfigured deploy dies at startup rather than serving.
    auth = build_auth_provider()
    _readyz_token = resolve_readyz_token()

    if auth is None:
        logger.warning(
            "ALLOW_UNAUTHENTICATED=1 — serving with NO authentication. "
            "Never do this on a public address."
        )
    # `http_app()` reads `self.auth` at call time, so assigning it here keeps
    # `mcp` importable without GitHub credentials — which the test suite needs.
    mcp.auth = auth

    uvicorn.run(mcp.http_app(), host="0.0.0.0", port=port)
