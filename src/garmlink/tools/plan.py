"""The training plan document, read from and written back to GitHub.

The plan lives in a separate repo (`knahsirV/training-plan`), where
`content/plan.md` is rendered by a static PWA on GitHub Pages. That file is the
source of truth for the athlete's goals, current block, weekly template and
zones.

Before this module, those facts were *also* hardcoded into `prompts.py`, and the
copy went stale: the prompts still asserted "no race booked" and "swimming not
yet started" months after a half marathon was booked and swim sessions began. A
duplicated fact is a fact that expires. The prompts now call `get_training_plan`
and read the real thing.

`update_training_plan` closes the loop, so an adjustment made from any MCP client
— including a phone, with no repo checked out — lands in the document as well as
on the Garmin calendar.

This is the only tool module that does not go through `GarminClient`; it talks to
the GitHub Contents API instead.
"""

from __future__ import annotations

import base64
import binascii
import os
import re

import httpx
from fastmcp import FastMCP

from ..cache import TTLCache

mcp = FastMCP("plan")

# The document changes on the order of once a week, but a single coaching
# workflow may read it several times. One minute collapses that to one request
# while never serving a stale plan into the *next* conversation.
PLAN_TTL: float = 60

_CACHE_KEY = ("get_training_plan", (), frozenset())
_cache = TTLCache()

_API = "https://api.github.com"
_TIMEOUT = httpx.Timeout(10.0)

# A >30% shrink is truncation until proven otherwise. A write replaces the whole
# file, so the realistic failure is a model reproducing most of the document and
# quietly dropping the tail.
_SHRINK_FLOOR = 0.7


class PlanError(Exception):
    """Anything that should reach the user as a plain message, not a traceback."""


def _config() -> dict[str, str]:
    """Resolve configuration at call time, not import time, so tests can patch env.

    Deliberately not named `GITHUB_*`: `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`
    already exist for the OAuth provider and are an unrelated credential with a
    different scope. Reusing them would hand the MCP server's login app write
    access to a repository.
    """
    return {
        "repo": os.getenv("TRAINING_PLAN_REPO", "knahsirV/training-plan"),
        "path": os.getenv("TRAINING_PLAN_PATH", "content/plan.md"),
        "branch": os.getenv("TRAINING_PLAN_BRANCH", "main"),
        "token": os.getenv("TRAINING_PLAN_GITHUB_TOKEN", ""),
    }


def has_write_token() -> bool:
    """Whether writes are configured. Reported in the startup log line."""
    return bool(_config()["token"])


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_plan_update(
    new: str, current: str, *, allow_shrink: bool = False
) -> None:
    """Raise `PlanError` if `new` looks like a damaged replacement for `current`.

    Every check here is either structure-agnostic or relative to the current
    file. Nothing hardcodes a heading, a section name or a table shape — the
    plan gets reorganised as training changes, and a validator that knew the
    document's shape would have to be edited every time it did. What it does
    know is that a write replaces the whole file, so the failure worth catching
    is losing part of it.
    """
    if not new.strip():
        raise PlanError("Refusing to write an empty plan.")

    if not re.search(r"^#\s+\S", new, re.MULTILINE):
        raise PlanError(
            "Refusing to write: the new plan has no top-level heading, which "
            "usually means the beginning of the document was lost. Any '# Title' "
            "is accepted — the text is not checked."
        )

    if not allow_shrink and len(new) < _SHRINK_FLOOR * len(current):
        # Stated as a fraction of the current length, not as "N% shorter": a
        # 31-character replacement for an 11,000-character plan rounds to
        # "100% shorter", which reads as "empty" and sends the reader looking
        # for the wrong bug.
        pct = round(len(new) / len(current) * 100)
        raise PlanError(
            f"Refusing to write: the new plan is {len(new)} characters, only "
            f"{pct}% of the current {len(current)}, which usually means it was "
            "truncated. Re-read the plan with get_training_plan and resend the "
            "complete document. If the cut really is intended, pass "
            "allow_shrink=true."
        )


# The PWA renders the plan with a hand-rolled markdown subset (`render.js` in the
# training-plan repo) that covers h1-h4, bold, italic, inline code, pipe tables,
# '-' lists, '---' and paragraphs — and nothing else. Anything below renders as
# literal text on the phone. These warn rather than block: garmlink should not
# hard-fail on another repo's renderer, and the right fix is sometimes to extend
# render.js instead.
_RENDER_CHECKS: tuple[tuple[str, str], ...] = (
    (r"^\s*\d+[.)]\s+\S", "ordered list — renders as literal '1.' text"),
    (r"^\s*>", "blockquote — renders as literal '&gt;' text"),
    (r"^\s*```", "fenced code block — renders as literal backticks"),
    (r"^\s+-\s+\S", "nested list — renders flattened into the parent list"),
    (r"\[[^\]]+\]\([^)]+\)", "link — renders as literal '[text](url)' text"),
)


def render_warnings(markdown: str) -> list[str]:
    """Constructs the PWA's renderer cannot display, as one warning per line."""
    warnings: list[str] = []
    for lineno, line in enumerate(markdown.splitlines(), start=1):
        # Table rows are pipe-delimited prose and produce false positives for
        # nearly every check below.
        if line.lstrip().startswith("|"):
            continue
        for pattern, describe in _RENDER_CHECKS:
            if re.search(pattern, line):
                warnings.append(f"line {lineno}: {describe}")
    return warnings


# ---------------------------------------------------------------------------
# GitHub Contents API
# ---------------------------------------------------------------------------

async def _fetch_plan() -> dict:
    """GET the plan, uncached. Returns the tool's payload shape."""
    cfg = _config()
    url = f"{_API}/repos/{cfg['repo']}/contents/{cfg['path']}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        response = await http.get(
            url, params={"ref": cfg["branch"]}, headers=_headers(cfg["token"])
        )

    if response.status_code == 404:
        raise PlanError(
            f"No plan at {cfg['repo']}/{cfg['path']} on branch {cfg['branch']}. "
            "Check TRAINING_PLAN_REPO, TRAINING_PLAN_PATH and TRAINING_PLAN_BRANCH."
        )
    if response.status_code in (401, 403):
        raise PlanError(
            "GitHub refused the read "
            f"({response.status_code}). Without TRAINING_PLAN_GITHUB_TOKEN this "
            "endpoint is rate-limited to 60 requests an hour and cannot see "
            "private repositories."
        )
    if response.status_code != 200:
        raise PlanError(f"GitHub returned {response.status_code} reading the plan.")

    body = response.json()
    try:
        markdown = base64.b64decode(body["content"]).decode("utf-8")
    except (KeyError, binascii.Error, UnicodeDecodeError) as exc:
        raise PlanError(f"Could not decode the plan file: {exc}") from exc

    return {
        "markdown": markdown,
        "sha": body["sha"],
        "repo": cfg["repo"],
        "path": cfg["path"],
        "branch": cfg["branch"],
    }


@mcp.tool()
async def get_training_plan() -> dict:
    """
    Fetch the athlete's training plan document — the source of truth for their goal
    race, current training block, weekly template, training zones and adjustment log.
    Call this before any coaching analysis, workout design or plan adjustment, and
    read the plan rather than assuming what the athlete is training for.

    Returns the plan markdown plus the blob `sha`, which update_training_plan needs
    in order to write safely.
    """
    hit = _cache.get(_CACHE_KEY)
    if hit is not None:
        return hit
    try:
        result = await _fetch_plan()
    except PlanError as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach GitHub to read the plan: {exc}"}
    _cache.set(_CACHE_KEY, result, PLAN_TTL)
    return result


@mcp.tool()
async def update_training_plan(
    markdown: str,
    base_sha: str,
    message: str,
    allow_shrink: bool = False,
) -> dict:
    """
    Write a new version of the training plan document, replacing it entirely.
    Use after the athlete has explicitly approved the change — always show the exact
    text that will change and wait for a yes, because this replaces the whole file.

    Args:
        markdown:     The complete new plan document. Not a fragment or a diff —
                      whatever is sent becomes the entire file.
        base_sha:     The `sha` from the get_training_plan call this edit is based
                      on. If the plan changed since then, the write is rejected
                      rather than silently overwriting the other change.
        message:      Commit message describing the adjustment.
        allow_shrink: Permit a new version more than 30% shorter than the current
                      one. Off by default, because that is normally truncation.

    Returns the commit URL and any render warnings for the plan's PWA.
    """
    cfg = _config()
    if not cfg["token"]:
        return {
            "error": "The plan is readable but not writable: "
            "TRAINING_PLAN_GITHUB_TOKEN is not configured on this server."
        }

    try:
        current = await _fetch_plan()
        validate_plan_update(markdown, current["markdown"], allow_shrink=allow_shrink)
    except PlanError as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach GitHub to read the plan: {exc}"}

    if current["sha"] != base_sha:
        return {
            "error": "The plan changed since it was read (base_sha is stale). "
            "Call get_training_plan again, re-apply the edit to the current "
            "text, and retry — otherwise this write would discard whatever "
            "changed in between."
        }

    url = f"{_API}/repos/{cfg['repo']}/contents/{cfg['path']}"
    payload = {
        "message": message,
        "content": base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
        "sha": base_sha,
        "branch": cfg["branch"],
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            response = await http.put(
                url, json=payload, headers=_headers(cfg["token"])
            )
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach GitHub to write the plan: {exc}"}

    # GitHub uses 409 for a stale sha. The check above catches the common case,
    # but a push landing between that read and this write still ends up here.
    if response.status_code == 409:
        return {
            "error": "The plan changed while this write was in flight. Call "
            "get_training_plan again, re-apply the edit, and retry."
        }
    if response.status_code in (401, 403):
        return {
            "error": "GitHub refused the write "
            f"({response.status_code}). TRAINING_PLAN_GITHUB_TOKEN needs "
            "Contents: read and write on this repository."
        }
    if response.status_code not in (200, 201):
        return {"error": f"GitHub returned {response.status_code} writing the plan."}

    # The document just changed; the next reader must not get the old one.
    _cache.invalidate("get_training_plan")

    commit = response.json().get("commit", {})
    result = {
        "status": "written",
        "commit_sha": commit.get("sha"),
        "commit_url": commit.get("html_url"),
        "repo": cfg["repo"],
        "path": cfg["path"],
    }
    warnings = render_warnings(markdown)
    if warnings:
        result["render_warnings"] = warnings
        result["render_note"] = (
            "The plan was written. These lines use markdown the training-plan "
            "PWA cannot render and will show as literal text on the phone — "
            "either rewrite them in the supported subset (headings, bold, "
            "italic, inline code, tables, '-' lists, '---') or extend render.js."
        )
    return result
