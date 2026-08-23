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
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp.server.auth.auth import AccessToken  # noqa: E402
from fastmcp.server.auth.providers.github import GitHubTokenVerifier  # noqa: E402

from garmlink.auth_provider import AllowlistedGitHubTokenVerifier  # noqa: E402

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
