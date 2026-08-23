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

from garmlink.auth_provider import (  # noqa: E402
    AllowlistedGitHubTokenVerifier,
    build_auth_provider,
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
    with _env(**{**GOOD_ENV, "GITHUB_ALLOWED_USERS": f"{ALLOWED}, second-user "}):
        with _no_gcp_credentials():
            provider = build_auth_provider()
    assert provider is not None
    # Reaching for a private attribute, which the production code deliberately
    # avoids. In a test that is the right trade: if a fastmcp bump renames it,
    # this fails in CI rather than the wiring breaking silently in production.
    verifier = provider._token_validator
    assert isinstance(verifier, AllowlistedGitHubTokenVerifier)
    # Whitespace around comma-separated logins must not create phantom entries.
    assert verifier._allowed == frozenset({ALLOWED, "second-user"})


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
