# OAuth Connector Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `garmlink`'s static bearer token with GitHub OAuth so the server can be added as a custom connector on claude.ai, and therefore work on mobile.

**Architecture:** A `GitHubTokenVerifier` subclass enforces a fail-closed login allowlist on every request, wired into an `OAuthProxy` constructed directly (not via `GitHubProvider`, which builds its own verifier and offers no injection point). OAuth state lives in Firestore rather than the default ephemeral file store, because the service scales to zero. `/readyz` keeps a separate diagnostic secret so it still answers when OAuth is the broken thing.

**Tech Stack:** Python 3.12, `fastmcp>=3.4,<4` (3.4.7 installed), `google-cloud-firestore`, Cloud Run, Secret Manager, GitHub OAuth.

**Spec:** `docs/superpowers/specs/2026-08-21-oauth-connector-design.md`

## Global Constraints

- **Python 3.12 only.** A 3.11 interpreter silently resolves `garminconnect` to 0.3.2 and tests a library production never runs. Use `.venv/bin/python` for everything.
- **New dependencies are pinned exactly**, never floated. `garminconnect==0.3.11` is the precedent.
- **Tests run standalone**: every test file ends with a `_run_all()` block and is executed as `.venv/bin/python tests/test_x.py`, not via pytest. Each file starts with `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))`.
- **Patch the `GarminClient` constructor** in any test that imports `garmlink.server`. The lifespan builds a real client, and with real tokens at `~/.garminconnect` the suite silently hits the live Garmin API.
- **Never log a presented credential.** Log the reason, and (for OAuth) the GitHub login, which is a public username.
- **Never log tool results.** They are the health data.
- **Fail closed.** Missing or blank auth configuration aborts startup rather than serving personal health data to whoever finds the URL.
- Service URL: `https://garmlink-moz6szqd6q-uc.a.run.app` · GCP project `garmlink` (number `401226208618`) · region `us-central1` · repo `knahsirV/garmlink`.
- Surface to preserve: **45 tools, 4 prompts**.

---

### Task 1: Phase 0 — provision GitHub and GCP while bearer auth is still live

The single most valuable sequencing decision in this plan. Creating the OAuth app, the Firestore database, and the secrets *before* any code changes means a failure here is unambiguous. Do it in the reverse order and "the GCP setup is wrong" and "the auth code is wrong" arrive fused together, on a service you can no longer reach.

**Files:**
- Modify: `scripts/setup-cloudrun.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: Secret Manager entries `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `READYZ_TOKEN`; a Native-mode Firestore database in `us-central1`; `roles/datastore.user` on the runtime service account.

- [ ] **Step 1: Create the GitHub OAuth app**

In a browser: GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.

- Application name: `garmlink`
- Homepage URL: `https://garmlink-moz6szqd6q-uc.a.run.app`
- Authorization callback URL: `https://garmlink-moz6szqd6q-uc.a.run.app/auth/callback`

Generate a client secret. Keep both values in the clipboard/password manager for the next step — the secret is shown once. Do not paste either into a terminal that records history, and do not print them into this session's transcript.

- [ ] **Step 2: Add the Firestore and secret provisioning to the setup script**

In `scripts/setup-cloudrun.sh`, add `firestore.googleapis.com` to the existing `gcloud services enable` list (around line 29):

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  firestore.googleapis.com
```

After the existing `MCP_AUTH_TOKEN` block (around line 95), add the three new secrets using the script's existing `create_secret` / `put_secret` helpers:

```bash
# GitHub OAuth app credentials. Created at
# https://github.com/settings/developers with callback
# https://garmlink-moz6szqd6q-uc.a.run.app/auth/callback
create_secret GITHUB_CLIENT_ID "GITHUB_CLIENT_ID (e.g. Ov23li...)"
create_secret GITHUB_CLIENT_SECRET "GITHUB_CLIENT_SECRET" hidden

# Guards /readyz only. Deliberately NOT the OAuth token: an OAuth access
# token requires a browser flow, and /readyz has to stay reachable from a
# terminal precisely when the OAuth layer is the thing that is broken.
if gcloud secrets describe READYZ_TOKEN >/dev/null 2>&1; then
  echo "    READYZ_TOKEN exists — keeping the current value"
else
  echo "    READYZ_TOKEN generated (64 hex chars)"
  put_secret READYZ_TOKEN "$(openssl rand -hex 32)"
fi
```

Extend the runtime-accessor loop (around line 98) to cover them:

```bash
for s in GARMIN_EMAIL GARMIN_TOKENS_JSON MCP_AUTH_TOKEN GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET READYZ_TOKEN; do
```

Then add the Firestore database and its IAM role, after the secrets section:

```bash
# --- firestore (OAuth state) -------------------------------------------------
# The default OAuth client store is a file tree on local disk. This service
# scales to zero, so a cold start would wipe every DCR registration and
# refresh-token mapping — in claude.ai that surfaces as "randomly asks me to
# reconnect". Firestore survives cold starts and is correct across instances.
echo "==> Creating the Firestore database"
if gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  echo "    (default) exists"
else
  gcloud firestore databases create --location="${REGION}" --type=firestore-native
fi

echo "==> Letting the Cloud Run runtime read and write Firestore"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/datastore.user >/dev/null
```

- [ ] **Step 3: Run the setup script**

Run: `./scripts/setup-cloudrun.sh`

It is idempotent, so re-running over the existing project is safe. Paste the GitHub client ID and secret when prompted.

**Gotcha 3 applies:** GCP is eventually consistent. If the `datastore.user` binding fails with "does not exist", or the first Firestore write 403s, **wait a minute and retry before debugging**. This has already cost real time on this project once.

- [ ] **Step 4: Verify the provisioning independently of any code**

```bash
gcloud secrets versions access latest --secret=GITHUB_CLIENT_ID --project=garmlink | head -c 8; echo
gcloud secrets versions access latest --secret=READYZ_TOKEN --project=garmlink | wc -c   # 64
gcloud firestore databases describe --database='(default)' --project=garmlink --format='value(type,locationId)'
gcloud projects get-iam-policy garmlink --flatten=bindings \
  --filter='bindings.role=roles/datastore.user' --format='value(bindings.members)'
```

Expected: the client ID prefix prints, `READYZ_TOKEN` is 64 characters, the database reports `FIRESTORE_NATIVE us-central1`, and the runtime service account appears in the `datastore.user` binding.

- [ ] **Step 5: Confirm the live service is still healthy on bearer auth**

Nothing deployed has changed, and this is the last moment where that is true. Establish the baseline you will compare against.

```bash
URL=https://garmlink-moz6szqd6q-uc.a.run.app
TOKEN=$(gcloud secrets versions access latest --secret=MCP_AUTH_TOKEN --project=garmlink)
curl -s "$URL/health"                                    # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' "$URL/mcp"      # 401
curl -s -H "Authorization: Bearer $TOKEN" "$URL/readyz"  # authenticated
```

- [ ] **Step 6: Commit**

```bash
git add scripts/setup-cloudrun.sh
git commit -m "chore: provision GitHub OAuth app, Firestore, and readyz secret"
```

---

### Task 2: Route fastmcp's logs through the structured pipeline

Do this before any auth code, because it is what makes the auth code debuggable. `setup_logging()` currently installs its handler on the `garmlink` logger only, with `propagate=False`. After the cutover the entire OAuth flow logs through `fastmcp.server.auth.*` — and would land outside the JSON stream, without `severity`, and without ever passing through `redact()`.

**Files:**
- Modify: `src/garmlink/logs.py`
- Test: `tests/test_logging.py`

**Interfaces:**
- Consumes: existing `redact()` and `_TOKENISH` from `logs.py`.
- Produces: `RedactingFilter` (a `logging.Filter` subclass, exported in `__all__`); `setup_logging()` now configures both the `garmlink` and `fastmcp` loggers.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_logging.py`, and add `RedactingFilter` to the existing `from garmlink.logs import (...)` block at the top of the file:

```python
# ---------------------------------------------------------------------------
# Third-party (fastmcp) log capture and redaction
# ---------------------------------------------------------------------------

def test_fastmcp_logger_reaches_our_handler():
    # After the OAuth cutover, the whole auth flow logs through `fastmcp.*`.
    # If those records do not reach our handler they are not JSON, carry no
    # `severity` for Cloud Logging to lift, and never pass through redact().
    import io
    setup_logging()
    fastmcp_logger = logging.getLogger("fastmcp")
    assert fastmcp_logger.handlers, "setup_logging must configure the fastmcp logger"
    assert fastmcp_logger.propagate is False, "records would otherwise print twice"

    stream = io.StringIO()
    handler = fastmcp_logger.handlers[0]
    original = handler.stream  # type: ignore[attr-defined]
    handler.stream = stream    # type: ignore[attr-defined]
    try:
        fastmcp_logger.warning("oauth flow failed")
    finally:
        handler.stream = original  # type: ignore[attr-defined]
    assert "oauth flow failed" in stream.getvalue()


def test_third_party_token_material_is_redacted():
    # fastmcp's auth paths log upstream response bodies. Our own code redacts
    # at the call site; a third-party logger cannot, so the handler must.
    import io
    setup_logging()
    fastmcp_logger = logging.getLogger("fastmcp")
    secret = "gho_" + "a" * 36

    stream = io.StringIO()
    handler = fastmcp_logger.handlers[0]
    original = handler.stream  # type: ignore[attr-defined]
    handler.stream = stream    # type: ignore[attr-defined]
    try:
        fastmcp_logger.warning("upstream said %s", secret)
    finally:
        handler.stream = original  # type: ignore[attr-defined]

    output = stream.getvalue()
    assert secret not in output, output
    assert "[redacted]" in output, output


def test_redacting_filter_leaves_ordinary_messages_alone():
    record = logging.LogRecord("fastmcp", logging.INFO, "", 0, "starting up", (), None)
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "starting up"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_logging.py`

Expected: FAIL — `ImportError: cannot import name 'RedactingFilter'`.

- [ ] **Step 3: Implement `RedactingFilter` and extend `setup_logging`**

In `src/garmlink/logs.py`, add `"RedactingFilter"` to `__all__`, then add the filter near `redact()`:

```python
class RedactingFilter(logging.Filter):
    """Apply `redact()` to records emitted by code that cannot redact itself.

    Our own call sites redact before logging. Third-party loggers — fastmcp's
    OAuth paths in particular, which log upstream response bodies — have no
    such discipline, so the scrub happens at the handler instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        return True
```

Replace the body of `setup_logging()` (currently `logs.py:214-234`) with:

```python
def setup_logging() -> None:
    """Install a single stdout handler on the `garmlink` and `fastmcp` loggers.

    Idempotent: repeated calls (tests, reloads) replace the handler rather than
    stacking duplicates that would print every line twice.

    `fastmcp` is included because the OAuth flow — consent, registration, token
    exchange, upstream refresh failures — logs there rather than here. Left on
    the root logger it would be unformatted, unqueryable, and unredacted, which
    is exactly the stream you need when the connector misbehaves.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    on_cloud_run = bool(os.getenv("K_SERVICE"))
    fmt = os.getenv("LOG_FORMAT", "json" if on_cloud_run else "text").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())

    for name in ("garmlink", "fastmcp"):
        lg = logging.getLogger(name)
        for existing in list(lg.handlers):
            lg.removeHandler(existing)
        lg.addHandler(handler)
        lg.setLevel(getattr(logging, level, logging.INFO))
        # Ours is the only handler that should render these records; uvicorn's
        # root handler would otherwise print an unformatted copy of every line.
        lg.propagate = False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_logging.py`

Expected: PASS, including every pre-existing test in the file. If `_quiet_fastmcp` tests now behave differently, that is a real interaction — `setup_logging` sets the `fastmcp` level, and `_quiet_fastmcp` sets it to CRITICAL — and the fix is to have `_quiet_fastmcp` save and restore as it already does, not to weaken the new behavior.

- [ ] **Step 5: Commit**

```bash
git add src/garmlink/logs.py tests/test_logging.py
git commit -m "feat: route fastmcp's logs through the structured, redacted pipeline"
```

---

### Task 3: The allowlisted token verifier

The enforcement point. `GitHubProvider` builds its own verifier internally (`providers/github.py:285-291`) and takes no `token_verifier` parameter, so the subclass is wired through `OAuthProxy` instead — see Task 4.

**Files:**
- Create: `src/garmlink/auth_provider.py`
- Test: `tests/test_auth_provider.py`

**Interfaces:**
- Consumes: `logger` from `garmlink.logs`.
- Produces: `AllowlistedGitHubTokenVerifier(*, allowed_logins: frozenset[str], **kwargs)` with `async verify_token(token: str) -> AccessToken | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_provider.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python tests/test_auth_provider.py`

Expected: FAIL — `ModuleNotFoundError: No module named 'garmlink.auth_provider'`.

- [ ] **Step 3: Implement the verifier**

Create `src/garmlink/auth_provider.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python tests/test_auth_provider.py`

Expected: PASS, 6/6.

- [ ] **Step 5: Commit**

```bash
git add src/garmlink/auth_provider.py tests/test_auth_provider.py
git commit -m "feat: allowlisted GitHub token verifier, enforced on every request"
```

---

### Task 4: Provider construction, Firestore storage, and fail-closed configuration

**Files:**
- Modify: `src/garmlink/auth_provider.py`
- Modify: `src/garmlink/deps.py`
- Modify: `pyproject.toml`
- Test: `tests/test_auth_provider.py`

**Interfaces:**
- Consumes: `AllowlistedGitHubTokenVerifier` from Task 3.
- Produces:
  - `build_auth_provider() -> AuthProvider | None`
  - `resolve_readyz_token() -> str | None`
  - `build_oauth_store() -> AsyncKeyValue`
  - `deps.set_oauth_store(store) -> None` / `deps.get_oauth_store() -> Any | None`

Note a small, deliberate departure from the spec's single-table framing: `READYZ_TOKEN` is validated by its own `resolve_readyz_token()` rather than inside `build_auth_provider()`, because the provider does not consume it. Both are called from `main()` and both abort startup when unset, so fail-closed behavior is unchanged.

- [ ] **Step 1: Add the pinned dependency**

In `pyproject.toml`, add to `dependencies`:

```toml
    # OAuth state (DCR registrations, refresh-token mappings) must outlive a
    # cold start: this service scales to zero and the default store is a file
    # tree on ephemeral local disk. Pinned exactly — a floating dep already
    # resolved to two different libraries on two Pythons once on this project.
    "google-cloud-firestore==2.28.1",
```

Run: `.venv/bin/pip install -e .`

Then confirm the store imports — it raises `ImportError` without the extra, and that error is the whole reason this pin exists:

```bash
.venv/bin/python -c "from key_value.aio.stores.firestore import FirestoreStore; print('ok')"
```

Expected: `ok`.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_auth_provider.py`, extending the import block with `build_auth_provider`, `resolve_readyz_token`, and `os`/`contextmanager`:

```python
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


def test_complete_config_builds_a_provider_with_the_allowlist():
    with _env(**{**GOOD_ENV, "GITHUB_ALLOWED_USERS": f"{ALLOWED}, second-user "}):
        provider = build_auth_provider()
    assert provider is not None
    # Reaching for a private attribute, which the production code deliberately
    # avoids. In a test that is the right trade: if a fastmcp bump renames it,
    # this fails in CI rather than the wiring breaking silently in production.
    verifier = provider._token_validator
    assert isinstance(verifier, AllowlistedGitHubTokenVerifier)
    # Whitespace around comma-separated logins must not create phantom entries.
    assert verifier._allowed == frozenset({ALLOWED, "second-user"})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python tests/test_auth_provider.py`

Expected: FAIL — `ImportError: cannot import name 'build_auth_provider'`.

- [ ] **Step 4: Add the store holder to `deps.py`**

Append to `src/garmlink/deps.py`:

```python
# The Firestore-backed OAuth store is built in main(), before the app exists,
# but must be closed by the lifespan. Same reason the GarminClient lives here:
# a mounted server's lifespan context cannot carry it.
_oauth_store: Any = None


def set_oauth_store(store: Any) -> None:
    """Register (or clear) the process-wide OAuth state store."""
    global _oauth_store
    _oauth_store = store


def get_oauth_store() -> Any | None:
    """Registered OAuth store, or None when running unauthenticated."""
    return _oauth_store
```

- [ ] **Step 5: Implement the builders**

Append to `src/garmlink/auth_provider.py`, adding `os` and the FastMCP/Firestore imports at the top:

```python
import os

from fastmcp.server.auth.auth import AuthProvider
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from key_value.aio.protocols import AsyncKeyValue

from . import deps

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
    """
    from key_value.aio.stores.firestore import FirestoreStore

    return FirestoreStore(
        project=os.getenv("GCP_PROJECT") or None,
        default_collection="oauth",
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python tests/test_auth_provider.py`

Expected: PASS, 12/12. `test_complete_config_builds_a_provider_with_the_allowlist` constructs a real `OAuthProxy` but makes no network call — construction is local.

- [ ] **Step 7: Commit**

```bash
git add src/garmlink/auth_provider.py src/garmlink/deps.py pyproject.toml tests/test_auth_provider.py
git commit -m "feat: build the GitHub OAuth provider on Firestore-backed state"
```

---

### Task 5: The cutover in `server.py`

Deletes the bearer path and wires OAuth in. The two test files that import the deleted names must change in the same commit, or the suite fails at import.

**Files:**
- Modify: `src/garmlink/server.py`
- Modify: `tests/test_critical_fixes.py:17-23` (the import block and the `resolve_auth_token` tests)
- Modify: `tests/test_logging.py:44` and its bearer-auth section

**Interfaces:**
- Consumes: `build_auth_provider()`, `resolve_readyz_token()` from Task 4; `deps.get_oauth_store()`.
- Produces: a `/readyz` route guarded by `READYZ_TOKEN`; `mcp.auth` assigned in `main()`.

- [ ] **Step 1: Delete the bearer auth from `server.py`**

Remove `BearerAuthMiddleware` (lines 44-78), `resolve_auth_token()` (lines 81-104), and `MIN_TOKEN_LENGTH` (line 41). Remove the now-unused imports `from starlette.middleware import Middleware` and `from starlette.middleware.base import BaseHTTPMiddleware`. **Keep `import hmac`** — the `/readyz` guard uses it.

Add to the imports:

```python
from .auth_provider import build_auth_provider, resolve_readyz_token
from .deps import get_garmin_or_none, get_oauth_store, set_client, set_oauth_store
```

- [ ] **Step 2: Guard `/readyz`**

`RequireAuthMiddleware` wraps only the `/mcp` route — custom routes are appended outside it (`fastmcp/server/http.py:632-636`) — so `/readyz` becomes public the moment `mcp.auth` is set. Add a module-level holder above the route definitions:

```python
# Set by main(). None means ALLOW_UNAUTHENTICATED=1, and /readyz is open.
_readyz_token: str | None = None
```

Replace the `readyz` handler (lines 209-223) with:

```python
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
        if not presented.startswith("Bearer ") or not hmac.compare_digest(
            presented[7:], _readyz_token
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
```

- [ ] **Step 3: Update the lifespan's startup log and shutdown**

In `lifespan()`, change the `auth` field and add `storage` (replacing lines 148-155):

```python
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
```

And close the store in the `finally` block (replacing lines 159-162):

```python
    finally:
        logger.info("shutdown")
        set_client(None)
        client.close()
        store = get_oauth_store()
        if store is not None:
            # Safe whether or not the store ever opened its client.
            await store.close()
            set_oauth_store(None)
```

- [ ] **Step 4: Rewrite `main()`**

Replace `main()` (lines 230-248) with:

```python
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
```

- [ ] **Step 5: Update the two test files that import the deleted names**

In `tests/test_critical_fixes.py`, replace the import block at lines 17-23 with:

```python
from garmlink.server import readyz  # noqa: E402
```

Delete `GOOD_TOKEN = "t" * MIN_TOKEN_LENGTH` and the whole "Critical 1: auth fails closed" section (`test_missing_token_refuses_to_start` and its siblings that call `resolve_auth_token`). That coverage now lives in `tests/test_auth_provider.py`, which tests the same fail-closed property against the code that actually runs. Leave every other test in the file untouched.

In `tests/test_logging.py`, remove `from garmlink.server import BearerAuthMiddleware` (line 44) and replace the "Bearer-auth diagnostics" section — `_auth_test_client`, `test_rejected_token_is_logged`, and `test_rejection_log_never_contains_the_presented_token` — with the `/readyz` equivalents:

```python
# ---------------------------------------------------------------------------
# /readyz diagnostics
# ---------------------------------------------------------------------------

def _readyz_client(token: str | None) -> TestClient:
    import garmlink.server as srv
    srv._readyz_token = token
    app = Starlette(routes=[Route("/readyz", srv.readyz)])
    return TestClient(app)


def test_readyz_rejects_a_missing_token():
    client = _readyz_client("r" * 64)
    with _CapturedLogs() as logs:
        assert client.get("/readyz").status_code == 401
    rejects = logs.with_message("auth.reject")
    assert rejects, "a 401 must be explained in the logs"
    assert rejects[0].levelno == logging.WARNING, rejects[0].levelname
    assert rejects[0].fields["path"] == "/readyz", rejects[0].fields
    assert rejects[0].fields["reason"] == "bad_readyz_token", rejects[0].fields


def test_readyz_rejection_never_contains_the_presented_token():
    # Logging what someone presented would put a near-miss of the real secret,
    # or someone else's credential, into the log stream permanently.
    client = _readyz_client("r" * 64)
    presented = "abcdefghijklmnopqrstuvwxyz123456"
    with _CapturedLogs() as logs:
        client.get("/readyz", headers={"Authorization": f"Bearer {presented}"})
    for record in logs.records:
        assert presented not in str(getattr(record, "fields", {})), record.fields
        assert presented not in record.getMessage()


def test_readyz_admits_the_correct_token():
    token = "r" * 64
    client = _readyz_client(token)
    response = client.get("/readyz", headers={"Authorization": f"Bearer {token}"})
    # No GarminClient is registered in this harness, so /readyz reports
    # "unavailable" — the point is that it got past the guard rather than 401.
    assert response.status_code == 503, response.text
    assert response.json() == {"garmin": "unavailable"}, response.text


def test_readyz_is_open_when_running_unauthenticated():
    client = _readyz_client(None)
    assert client.get("/readyz").status_code == 503
```

Also update the `startup` assertions near the end of the file: the `auth` field is now `"disabled"` under `ALLOW_UNAUTHENTICATED=1`, and a `storage` field is present.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/python tests/test_garmin_contract.py
.venv/bin/python tests/test_critical_fixes.py
.venv/bin/python tests/test_auth_lifecycle.py
.venv/bin/python tests/test_auth_provider.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_tool_dispatch.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_prompts.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_logging.py
```

Expected: all seven files pass. `test_logging.py` must still report 45 tools and 4 prompts.

- [ ] **Step 7: Commit**

```bash
git add src/garmlink/server.py tests/test_critical_fixes.py tests/test_logging.py
git commit -m "feat!: replace bearer auth with GitHub OAuth, guard /readyz separately"
```

---

### Task 6: Pin the public route surface

The test that would have caught `/readyz` silently going public. It will catch it again on a future `fastmcp` bump, which is the real point — this trap is a property of how FastMCP registers custom routes, not a one-time mistake.

**Files:**
- Modify: `tests/test_auth_provider.py`

**Interfaces:**
- Consumes: `build_auth_provider()` from Task 4; `mcp` from `garmlink.server`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auth_provider.py`:

```python
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

    with _env(**GOOD_ENV):
        provider = build_auth_provider()

    original = srv.mcp.auth
    try:
        srv.mcp.auth = provider
        app = srv.mcp.http_app()
        paths = {getattr(r, "path", None) for r in app.routes}
    finally:
        srv.mcp.auth = original

    assert EXPECTED_PUBLIC <= paths, EXPECTED_PUBLIC - paths
    assert "/readyz" in paths, "the diagnostic route should still be mounted"
    assert "/mcp" in paths


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

    with _env(**GOOD_ENV):
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
```

- [ ] **Step 2: Run the test to verify it fails, then passes**

Run: `.venv/bin/python tests/test_auth_provider.py`

If the assertions pass immediately, that is the expected outcome — this task pins behavior that Task 5 already implemented rather than driving new code. What matters is that you **verify the test is meaningful** by mutating it: temporarily add `"/readyz"` to `EXPECTED_PUBLIC` and confirm nothing fails (proving the first assertion is a subset check), then temporarily change `wrapped == {"/mcp"}` to `wrapped == set()` and confirm it *does* fail. Revert both.

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth_provider.py
git commit -m "test: pin the public route surface and the /readyz auth gap"
```

---

### Task 7: Deploy and verify live

Gotcha 6 is why this task is this long: every one of the seven bugs fixed during the deployment session survived a green test suite at some point. The suite cannot tell you whether OAuth state survives a cold start, and that is the whole risk of this change.

**Files:**
- Modify: `.github/workflows/deploy.yml:90`

**Interfaces:**
- Consumes: everything above.
- Produces: a working claude.ai connector.

- [ ] **Step 1: Update the deploy workflow**

In `.github/workflows/deploy.yml`, replace the `--set-secrets` line (line 90) and add `--set-env-vars`:

```yaml
            --set-secrets=GARMIN_EMAIL=GARMIN_EMAIL:latest,GARMIN_TOKENS_JSON=GARMIN_TOKENS_JSON:latest,GITHUB_CLIENT_ID=GITHUB_CLIENT_ID:latest,GITHUB_CLIENT_SECRET=GITHUB_CLIENT_SECRET:latest,READYZ_TOKEN=READYZ_TOKEN:latest \
            --set-env-vars=PUBLIC_BASE_URL=https://garmlink-moz6szqd6q-uc.a.run.app,GITHUB_ALLOWED_USERS=knahsirV \
```

`PUBLIC_BASE_URL` and `GITHUB_ALLOWED_USERS` are plain env vars, not secrets: a public URL and a public GitHub username are not credentials, and as plain vars they show up in `gcloud run services describe` — where you will look first when a login is being rejected.

- [ ] **Step 2: Commit and push, which deploys**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci: deploy with GitHub OAuth credentials instead of the bearer token"
git push
```

Watch: `gh run watch`

- [ ] **Step 3: Phase 1 — verify the endpoints**

```bash
URL=https://garmlink-moz6szqd6q-uc.a.run.app
READYZ=$(gcloud secrets versions access latest --secret=READYZ_TOKEN --project=garmlink)

curl -s "$URL/health"                                              # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' "$URL/mcp"                # 401
curl -si "$URL/mcp" | grep -i www-authenticate                     # present
curl -s "$URL/.well-known/oauth-protected-resource/mcp" | head -c 200; echo
curl -s -H "Authorization: Bearer $READYZ" "$URL/readyz"           # session state
curl -s -o /dev/null -w '%{http_code}\n' "$URL/readyz"             # 401
```

The last one is the important one: `/readyz` without a token must be 401, not 200. A 200 there means the guard did not take effect and Garmin session state is public.

Then confirm the startup log says what you expect:

```bash
gcloud logging read 'resource.type="cloud_run_revision"
  AND resource.labels.service_name="garmlink"
  AND jsonPayload.message="startup"' --project=garmlink --limit=2 --freshness=10m \
  --format='value(jsonPayload.auth, jsonPayload.storage, jsonPayload.tools, jsonPayload.prompts)'
```

Expected: `github_oauth firestore 45 4`. If `storage` says `file`, stop — the Firestore store did not get built and every cold start will force a reconnect.

- [ ] **Step 4: Phase 2 — connect Claude Code**

Fastest loop and locally controlled, so break it here rather than in claude.ai.

```bash
claude mcp remove garmlink 2>/dev/null || true
claude mcp add --transport http garmlink https://garmlink-moz6szqd6q-uc.a.run.app/mcp
```

Complete the browser flow (GitHub authorize, then the consent screen). Then in a Claude Code session, confirm the tools list and run one real call — `get_devices` — and check that it returns actual device data.

- [ ] **Step 5: Phase 3 — add the connector on claude.ai, then mobile**

claude.ai → Settings → Connectors → Add custom connector. Supply the MCP endpoint URL. The DCR flow means you should not need to paste a client ID or secret; if claude.ai demands them, that is a finding worth stopping on rather than working around.

Verify a real tool call from claude.ai, then open Claude on mobile, confirm the connector synced, and make one call there. **This is the goal of the entire project** — everything else is in service of this step.

- [ ] **Step 6: Phase 4 — the cold-start test**

The check no test suite can perform, and the one that distinguishes Firestore working from Firestore appearing to work.

Force a cold start by deploying a new revision, which is faster and more decisive than waiting out the idle window:

```bash
gcloud run services update garmlink --region us-central1 --project garmlink \
  --update-env-vars=COLD_START_PROBE=$(date +%s)
```

Then, **without reconnecting**, make a tool call from claude.ai.

Expected: it works. If claude.ai asks you to reconnect, Firestore is not actually holding the client registration — check the `storage` field in the new revision's `startup` log and the `datastore.user` binding before anything else.

- [ ] **Step 7: Phase 5 — prove the allowlist rejects**

An allowlist never tested against a rejection is an allowlist you are assuming.

```bash
gcloud run services update garmlink --region us-central1 --project garmlink \
  --update-env-vars=GITHUB_ALLOWED_USERS=definitely-not-you
```

Make a call from claude.ai. Expected: it fails. Confirm the reason:

```bash
gcloud logging read 'resource.type="cloud_run_revision"
  AND resource.labels.service_name="garmlink"
  AND jsonPayload.message="auth.reject"' --project=garmlink --limit=5 --freshness=10m \
  --format='value(jsonPayload.reason, jsonPayload.login)'
```

Expected: `not_allowlisted` with your actual login. Then restore:

```bash
gcloud run services update garmlink --region us-central1 --project garmlink \
  --update-env-vars=GITHUB_ALLOWED_USERS=knahsirV
```

Confirm claude.ai works again before moving on.

- [ ] **Step 8: Commit any fixes**

If any phase required a code change, commit it with a message naming the phase that caught it.

---

### Task 8: Retire `MCP_AUTH_TOKEN`

Last, and only after every phase of Task 7 passed. Keeping the secret an extra hour costs nothing, and until the connector is confirmed working it is the last thing that could distinguish "the service is fine, the OAuth layer is not."

**Files:** none — Secret Manager only.

- [ ] **Step 1: Confirm nothing still references it**

```bash
grep -rn "MCP_AUTH_TOKEN" --exclude-dir=.git --exclude-dir=.venv . || echo "no references"
gcloud run services describe garmlink --region us-central1 --project garmlink \
  --format='value(spec.template.spec.containers[0].env)' | tr ',' '\n' | grep -i mcp_auth || echo "not on the service"
```

Expected: references remain only in `docs/` (historical) and nothing on the running service. If `scripts/setup-cloudrun.sh` still creates it, remove that block and the entry from the accessor loop, then commit.

- [ ] **Step 2: Destroy version 1 and delete the secret**

Version 1 is the leaked value, currently disabled rather than destroyed.

```bash
gcloud secrets versions destroy 1 --secret=MCP_AUTH_TOKEN --project=garmlink --quiet
gcloud secrets delete MCP_AUTH_TOKEN --project=garmlink --quiet
```

- [ ] **Step 3: Verify it is gone and the service is unaffected**

```bash
gcloud secrets describe MCP_AUTH_TOKEN --project=garmlink 2>&1 | tail -1   # NOT_FOUND
curl -s https://garmlink-moz6szqd6q-uc.a.run.app/health                    # {"status":"ok"}
```

Then make one more tool call from claude.ai to confirm the connector is still live.

- [ ] **Step 4: Update the handoff document**

`docs/handoff-oauth-connector.md` describes this work as unstarted. Replace its body with a short note that the cutover shipped, pointing at the spec and this plan, and stating the current auth model in two or three sentences. A stale handoff is worse than no handoff — the next cold session will believe it.

```bash
git add docs/handoff-oauth-connector.md scripts/setup-cloudrun.sh
git commit -m "docs: the OAuth cutover shipped; retire the bearer token"
git push
```

---

## Verification

Full suite, Python 3.12 only:

```bash
.venv/bin/python tests/test_garmin_contract.py
.venv/bin/python tests/test_critical_fixes.py
.venv/bin/python tests/test_auth_lifecycle.py
.venv/bin/python tests/test_auth_provider.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_tool_dispatch.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_prompts.py
GARMIN_EMAIL=x@y.z ALLOW_UNAUTHENTICATED=1 .venv/bin/python tests/test_logging.py
```

Add `tests/test_auth_provider.py` to `.github/workflows/deploy.yml`'s test step alongside the existing five — otherwise the allowlist, the fail-closed configuration, and the route surface are unprotected in CI. Do this in Task 7 Step 1, with the workflow's other change.

End-to-end MCP check after the cutover (expects 45 tools, 4 prompts). Unlike the bearer version in the handoff, this needs an OAuth flow, so the simplest real check is Claude Code itself (Task 7 Step 4) rather than a script.

Done means: the connector works on claude.ai and on mobile, a forced cold start does not demand a reconnect, a non-allowlisted login is refused with `not_allowlisted` in the logs, `/readyz` answers with `READYZ_TOKEN` and 401s without it, and `MCP_AUTH_TOKEN` no longer exists.
