"""The OAuth allowlist is the only thing between a stranger and the health data.

After the cutover, eight routes are public: /health plus the seven OAuth
endpoints that discovery and the flow require. `GitHubTokenVerifier` accepts any
token minted for our OAuth app, which means every GitHub user on earth completes
the flow successfully. The allowlist is not defense-in-depth here — it is the
defense — so it fails closed and is checked on every request.

Runs standalone (`python tests/test_auth_provider.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp.server.auth.auth import AccessToken  # noqa: E402
from fastmcp.server.auth.providers.github import GitHubTokenVerifier  # noqa: E402
from google.auth.credentials import AnonymousCredentials  # noqa: E402

from garmlink import deps  # noqa: E402
from garmlink.auth_provider import (  # noqa: E402
    AllowlistedGitHubTokenVerifier,
    build_auth_provider,
    build_oauth_store,
    resolve_readyz_token,
)

ALLOWED = "knahsirV"


class _CapturedLogs:
    """Collects `garmlink` LogRecords for the duration of a with-block."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []

    def __enter__(self):
        self._handler = logging.Handler()
        self._handler.emit = self.records.append  # type: ignore[method-assign]
        self._logger = logging.getLogger("garmlink")
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)
        return False

    def with_message(self, msg: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == msg]


def _token(login: str | None) -> AccessToken:
    claims = {"sub": "1"}
    if login is not None:
        claims["login"] = login
    return AccessToken(
        token="upstream-token", client_id="1", scopes=["user"],
        expires_at=None, claims=claims,
    )


def _parent_returns(value):
    """Stub GitHubTokenVerifier.verify_token so no GitHub call is made."""
    async def _fake(self, token):  # noqa: ANN001
        return value
    return patch.object(GitHubTokenVerifier, "verify_token", new=_fake)


def _verify(verifier, token="tok"):
    return asyncio.run(verifier.verify_token(token))


def test_allowlisted_login_passes_through():
    v = AllowlistedGitHubTokenVerifier(allowed_logins=frozenset({ALLOWED}))
    with _parent_returns(_token(ALLOWED)):
        result = _verify(v)
    assert result is not None
    assert result.claims["login"] == ALLOWED


def test_non_allowlisted_login_is_rejected():
    v = AllowlistedGitHubTokenVerifier(allowed_logins=frozenset({ALLOWED}))
    with _parent_returns(_token("someone-else")), _CapturedLogs() as logs:
        result = _verify(v)
    assert result is None, "a valid GitHub token is not an authorised one"
    rejects = logs.with_message("auth.reject")
    assert rejects, "a rejection must be explained in the logs"
    assert rejects[0].fields["reason"] == "not_allowlisted", rejects[0].fields
    assert rejects[0].fields["login"] == "someone-else", rejects[0].fields


def test_rejection_log_never_contains_the_presented_token():
    v = AllowlistedGitHubTokenVerifier(allowed_logins=frozenset({ALLOWED}))
    presented = "gho_" + "b" * 36
    with _parent_returns(_token("someone-else")), _CapturedLogs() as logs:
        _verify(v, token=presented)
    for record in logs.records:
        assert presented not in str(getattr(record, "fields", {})), record.fields
        assert presented not in record.getMessage()


def test_invalid_token_stays_rejected():
    # The parent already returned None: bad token, expired, or GitHub is down.
    v = AllowlistedGitHubTokenVerifier(allowed_logins=frozenset({ALLOWED}))
    with _parent_returns(None):
        assert _verify(v) is None


def test_missing_login_claim_fails_closed():
    # A shape change upstream must deny, not raise and not admit.
    v = AllowlistedGitHubTokenVerifier(allowed_logins=frozenset({ALLOWED}))
    with _parent_returns(_token(None)):
        assert _verify(v) is None


def test_empty_allowlist_admits_nobody():
    v = AllowlistedGitHubTokenVerifier(allowed_logins=frozenset())
    with _parent_returns(_token(ALLOWED)):
        assert _verify(v) is None


# ---------------------------------------------------------------------------
# Fail-closed configuration
# ---------------------------------------------------------------------------

GOOD_ENV = {
    "GITHUB_CLIENT_ID": "Ov23liTEST",
    "GITHUB_CLIENT_SECRET": "s" * 40,
    "PUBLIC_BASE_URL": "https://garmlink-moz6szqd6q-uc.a.run.app",
    "GITHUB_ALLOWED_USERS": ALLOWED,
    "READYZ_TOKEN": "r" * 64,
}


@contextmanager
def _env(**overrides):
    """Set exactly the given environment, restoring whatever was there."""
    keys = set(GOOD_ENV) | {"ALLOW_UNAUTHENTICATED"} | set(overrides)
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            if v is not None:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_each_missing_variable_aborts_startup():
    for missing in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
                    "PUBLIC_BASE_URL", "GITHUB_ALLOWED_USERS"):
        env = dict(GOOD_ENV)
        env.pop(missing)
        with _env(**env):
            try:
                build_auth_provider()
            except RuntimeError as exc:
                assert missing in str(exc), (missing, str(exc))
            else:
                raise AssertionError(f"missing {missing} must abort startup")


def test_blank_variable_aborts_startup():
    with _env(**{**GOOD_ENV, "GITHUB_ALLOWED_USERS": "   "}):
        try:
            build_auth_provider()
        except RuntimeError as exc:
            assert "GITHUB_ALLOWED_USERS" in str(exc)
        else:
            raise AssertionError("a blank allowlist must abort startup")


def test_comma_only_allowlist_aborts_startup():
    # ",,," is non-empty but names nobody. Serving nobody is fine; serving
    # everybody would not be, so this must not fall through to an empty set.
    with _env(**{**GOOD_ENV, "GITHUB_ALLOWED_USERS": " , , "}):
        try:
            build_auth_provider()
        except RuntimeError as exc:
            assert "GITHUB_ALLOWED_USERS" in str(exc)
        else:
            raise AssertionError("an allowlist naming nobody must abort startup")


def test_allow_unauthenticated_returns_no_provider():
    with _env(ALLOW_UNAUTHENTICATED="1"):
        assert build_auth_provider() is None
        assert resolve_readyz_token() is None


def test_missing_readyz_token_aborts_startup():
    env = dict(GOOD_ENV)
    env.pop("READYZ_TOKEN")
    with _env(**env):
        try:
            resolve_readyz_token()
        except RuntimeError as exc:
            assert "READYZ_TOKEN" in str(exc)
        else:
            raise AssertionError("missing READYZ_TOKEN must abort startup")


@contextmanager
def _no_gcp_credentials():
    """Stand in for Application Default Credentials during store construction.

    `FirestoreStore.__init__` resolves credentials synchronously (via
    `google.auth.default()`) but makes no network call in doing so — the brief
    is correct that construction is local. It does, however, need *some*
    credentials object to resolve to, and a CI runner (or this sandbox) has no
    ADC configured, unlike a developer machine with `gcloud auth
    application-default login` already run. Anonymous credentials satisfy the
    interface without touching disk or network, keeping this test hermetic
    rather than dependent on the ambient environment.
    """
    with patch("google.auth.default", return_value=(AnonymousCredentials(), "garmlink-test")):
        with warnings.catch_warnings():
            # key_value's FirestoreStore warns on every construction that the
            # store is unstable — noise unrelated to what this test checks.
            warnings.filterwarnings("ignore", message="A configured store is unstable")
            yield


def test_complete_config_builds_a_provider_with_the_allowlist():
    try:
        with _env(**{**GOOD_ENV, "GITHUB_ALLOWED_USERS": f"{ALLOWED}, second-user "}):
            with _no_gcp_credentials():
                provider = build_auth_provider()
        assert provider is not None
        # Reaching for private attributes, which the production code deliberately
        # avoids. In a test that is the right trade: if a fastmcp bump renames one,
        # this fails in CI rather than the wiring breaking silently in production.
        verifier = provider._token_validator
        assert isinstance(verifier, AllowlistedGitHubTokenVerifier)
        # Whitespace around comma-separated logins must not create phantom entries.
        assert verifier._allowed == frozenset({ALLOWED, "second-user"})
        # server.py's lifespan closes whatever deps.get_oauth_store() returns on
        # shutdown, so the builder registering anything other than the exact
        # store it wired into the proxy would leak the real one or close a
        # decoy — nothing else covered this wiring.
        assert deps.get_oauth_store() is provider._client_storage
    finally:
        # build_auth_provider() registers the store as a side effect before
        # constructing OAuthProxy. Production never undoes this (the process
        # dies instead), but leaving it set here would leak a live FirestoreStore
        # into every test that runs after this one in the same process.
        deps.set_oauth_store(None)


def test_oauth_store_accepts_a_url_shaped_client_id():
    """Claude identifies itself with a CIMD URL, not a DCR-issued UUID.

    Our metadata advertises `client_id_metadata_document_supported`, so clients
    may present a client_id like
    `https://claude.ai/oauth/claude-code-client-metadata`. OAuthProxy keys its
    client records by that string, and a Firestore document ID cannot contain
    `/` — so an unsanitized store raises InvalidArgument on the first lookup and
    the browser flow dies with a 500. That is exactly what happened in
    production on the first connection attempt after the cutover; the DCR path
    hid it, because a UUID has no slashes.
    """
    with _no_gcp_credentials():
        store = build_oauth_store()

    cimd = "https://claude.ai/oauth/claude-code-client-metadata"
    sanitized = store._sanitize_key(cimd)  # noqa: SLF001 — the path the store itself takes
    assert "/" not in sanitized, f"Firestore would reject the document ID {sanitized!r}"

    # A DCR-issued UUID must pass through untouched: changing its key would
    # orphan every client registration already written under the old ID.
    uuid_id = "bc3c2163-b42f-4f50-8e85-f724f0f008d0"
    assert store._sanitize_key(uuid_id) == uuid_id  # noqa: SLF001

    # The collection name is a document path segment too, and takes the same
    # treatment — a caller passing "a/b" would otherwise fail the same way.
    assert "/" not in store._sanitize_collection("oauth/nested")  # noqa: SLF001


# ---------------------------------------------------------------------------
# Public route surface
# ---------------------------------------------------------------------------

# Everything reachable without a token once OAuth is on. The seven OAuth routes
# MUST be public — discovery and the flow itself run before the client holds a
# token — and /health must stay public for the platform's liveness check.
# /readyz must NOT be here: it reports Garmin session state, and FastMCP appends
# custom routes outside RequireAuthMiddleware, so it goes public by default.
EXPECTED_PUBLIC = {
    "/health",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp",
    "/authorize",
    "/token",
    "/register",
    "/auth/callback",
    "/consent",
}


def test_public_route_surface_is_exactly_what_we_expect():
    from unittest.mock import patch

    from garmlink.client import GarminClient

    # Building the app never runs the lifespan, so no GarminClient is
    # constructed here. The patch is belt-and-braces against gotcha 2: if this
    # test ever grows to exercise the lifespan, it must not reach the live
    # Garmin API using the real tokens sitting at ~/.garminconnect.
    with patch.object(GarminClient, "__init__", return_value=None):
        import garmlink.server as srv

    try:
        with _env(**GOOD_ENV), _no_gcp_credentials():
            provider = build_auth_provider()

        original = srv.mcp.auth
        try:
            srv.mcp.auth = provider
            app = srv.mcp.http_app()
            paths = {getattr(r, "path", None) for r in app.routes}
        finally:
            srv.mcp.auth = original

        # Exact equality, not a subset check: a subset check catches a route
        # disappearing but never a NEW public route silently appearing, which is
        # the exact failure mode this test exists for (a future fastmcp bump
        # exposing something). "/mcp" carries RequireAuthMiddleware and "/readyz"
        # is guarded separately by READYZ_TOKEN, so both are public at the route
        # level and belong in the comparison set alongside EXPECTED_PUBLIC.
        assert paths - {None} == EXPECTED_PUBLIC | {"/mcp", "/readyz"}, (
            paths - {None}
        )
        # `|` makes the equality above insensitive to /mcp or /readyz *also*
        # being listed in EXPECTED_PUBLIC (a redundant add is not a set change).
        # EXPECTED_PUBLIC's own comment says /readyz belongs here as a route
        # public for a *different* reason (READYZ_TOKEN, not the OAuth flow) —
        # this catches that distinction eroding, which the equality above can't.
        assert EXPECTED_PUBLIC.isdisjoint({"/mcp", "/readyz"}), EXPECTED_PUBLIC
    finally:
        deps.set_oauth_store(None)


def test_readyz_is_not_wrapped_by_the_oauth_guard():
    # This is the trap, stated as an assertion: FastMCP wraps ONLY /mcp in
    # RequireAuthMiddleware and appends custom routes after it, so /readyz
    # carries no OAuth enforcement and must rely on its own READYZ_TOKEN guard.
    # If a future fastmcp starts wrapping custom routes, this fails loudly and
    # the READYZ_TOKEN guard can be reconsidered — it does not fail silently.
    from unittest.mock import patch

    from fastmcp.server.auth.middleware import RequireAuthMiddleware
    from garmlink.client import GarminClient

    with patch.object(GarminClient, "__init__", return_value=None):
        import garmlink.server as srv

    try:
        with _env(**GOOD_ENV), _no_gcp_credentials():
            provider = build_auth_provider()

        original = srv.mcp.auth
        try:
            srv.mcp.auth = provider
            app = srv.mcp.http_app()
            wrapped = {
                getattr(r, "path", None)
                for r in app.routes
                if isinstance(getattr(r, "endpoint", None), RequireAuthMiddleware)
            }
        finally:
            srv.mcp.auth = original

        assert wrapped == {"/mcp"}, wrapped
    finally:
        deps.set_oauth_store(None)


def test_readyz_401s_on_the_real_app_without_a_token():
    # test_logging.py's readyz tests build a bare Starlette app with just the
    # /readyz route — none of mcp.http_app()'s middleware stack — and the route
    # surface tests above only check that /readyz is *mounted*, not what it
    # answers. So nothing exercised the guard on the app actually served in
    # production; a 200 here would mean Garmin session state is public.
    from unittest.mock import patch

    from starlette.testclient import TestClient

    from garmlink.client import GarminClient

    with patch.object(GarminClient, "__init__", return_value=None):
        import garmlink.server as srv

    try:
        with _env(**GOOD_ENV), _no_gcp_credentials():
            provider = build_auth_provider()
            readyz_token = resolve_readyz_token()

        original_auth = srv.mcp.auth
        original_token = srv._readyz_token
        try:
            srv.mcp.auth = provider
            srv._readyz_token = readyz_token
            client = TestClient(srv.mcp.http_app())

            unauthenticated = client.get("/readyz")
            assert unauthenticated.status_code == 401, unauthenticated.text

            authenticated = client.get(
                "/readyz", headers={"Authorization": f"Bearer {readyz_token}"}
            )
            assert authenticated.status_code != 401, authenticated.text
        finally:
            srv.mcp.auth = original_auth
            srv._readyz_token = original_token
    finally:
        deps.set_oauth_store(None)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
