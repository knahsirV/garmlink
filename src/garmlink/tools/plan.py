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

import asyncio
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

# Keyed by path so each part caches and expires independently. The first
# element stays "get_training_plan" because TTLCache.invalidate() matches on it,
# so one invalidate() after a write still clears every part.
def _cache_key(path: str) -> tuple[str, tuple, frozenset]:
    return ("get_training_plan", (path,), frozenset())


_cache = TTLCache()

# The document is split by volatility: the block changes weekly, the reference
# rarely, the log only ever grows. They are concatenated in this order on read,
# which matters to the PWA — render.js takes the *first* table matching a set of
# column names, so the current block's tables must come before the reference's.
_DEFAULT_PATHS = "content/plan.md,content/reference.md,content/log.md"

_API = "https://api.github.com"
_TIMEOUT = httpx.Timeout(10.0)

# A >30% shrink is truncation until proven otherwise. A write replaces the whole
# file, so the realistic failure is a model reproducing most of the document and
# quietly dropping the tail.
_SHRINK_FLOOR = 0.7

# The plan lives in a public repository. It once carried the athlete's birth
# date, height and weight in every commit since the first, and removing them
# took a history rewrite that still left orphaned commits reachable — a write
# here is effectively permanent, which is why these block rather than warn the
# way the renderer checks do.
#
# Re-adding is a live risk, not a hypothetical one: `get_user_profile` hands the
# model Garmin's profile, birth date and weight included, and the Athlete
# Snapshot now has a conspicuous gap where those lines used to be. Restoring
# them reads as filling in missing detail (W/kg is a normal cycling metric, age
# drives max-HR estimates) rather than as undoing a deliberate redaction.
_PERSONAL_DATA_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bborn\b", "a birth date"),
    (r"\b(?:date of birth|d\.?o\.?b\.?)\b", "a birth date"),
    # A bare month-day-year, which is how a birth date is usually written out.
    (r"\b(?:january|february|march|april|may|june|july|august|september|october"
     r"|november|december)\s+\d{1,2},\s*(?:19|20)\d{2}\b", "a full date"),
    (r"\b\d{1,3}\s*(?:lb|lbs|kg)\b", "a body weight"),
    # Feet-and-inches, e.g. 5'7" — triple-quoted so both quote characters sit
    # inside the literal without escaping.
    (r'''\b\d\s*'\s*\d{1,2}\s*"''', "a height"),
    (r"\b\d{1,3}\s*(?:cm)\b", "a height"),
    (r"\b(?:aged?|years old|yrs old)\b", "an age"),
)


class PlanError(Exception):
    """Anything that should reach the user as a plain message, not a traceback."""


def _config() -> dict:
    """Resolve configuration at call time, not import time, so tests can patch env.

    Deliberately not named `GITHUB_*`: `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`
    already exist for the OAuth provider and are an unrelated credential with a
    different scope. Reusing them would hand the MCP server's login app write
    access to a repository.
    """
    paths = [
        part.strip()
        for part in os.getenv("TRAINING_PLAN_PATHS", _DEFAULT_PATHS).split(",")
        if part.strip()
    ]
    # TRAINING_PLAN_PATH (singular) predates the split and still wins if set, so
    # an existing deployment keeps working without touching its environment.
    single = os.getenv("TRAINING_PLAN_PATH", "").strip()
    if single:
        paths = [single]
    if not paths:
        paths = [_DEFAULT_PATHS.split(",")[0]]

    return {
        "repo": os.getenv("TRAINING_PLAN_REPO", "knahsirV/training-plan"),
        "paths": paths,
        # The part carrying the document's H1. Only this one is required to
        # exist, and only this one is checked for a top-level heading.
        "path": paths[0],
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

def _new_personal_data(new: str, current: str) -> list[str]:
    """Personal-data patterns that `new` introduces and `current` does not have.

    Comparison is per *pattern*, not per matched string. A plan that already
    records a lifting load in pounds must stay able to record a different one —
    matching on the exact string would block `185lb` because only `135lb` was
    there before, which makes the guard turn the document read-only for its own
    content.

    The cost is honest and worth stating: once the document contains any match
    for a pattern, that pattern stops guarding. Introducing the first one takes a
    deliberate `allow_personal_data=true`, so the weakening is a choice rather
    than an accident, and the plan currently matches none of these patterns.
    """
    found: list[str] = []
    for pattern, describes in _PERSONAL_DATA_PATTERNS:
        # Already present in some form: the document has accepted this kind of
        # content, so new instances of it are not the model reintroducing PII.
        if re.search(pattern, current, re.IGNORECASE):
            continue
        for added in sorted(
            set(m.group(0) for m in re.finditer(pattern, new, re.IGNORECASE))
        ):
            found.append(f"{added!r} looks like {describes}")
    return found


def validate_plan_update(
    new: str,
    current: str,
    *,
    allow_shrink: bool = False,
    allow_personal_data: bool = False,
    require_heading: bool = True,
) -> None:
    """Raise `PlanError` if `new` looks like a damaged replacement for `current`.

    Every check here is either structure-agnostic or relative to the current
    file. Nothing hardcodes a heading, a section name or a table shape — the
    plan gets reorganised as training changes, and a validator that knew the
    document's shape would have to be edited every time it did. What it does
    know is that a write replaces the whole file, so the failure worth catching
    is losing part of it.

    `require_heading` is off for the parts of a split document that do not carry
    the H1. The reference and log files legitimately start at `##`, and a check
    written for a single-file plan would refuse every write to them.
    """
    if not new.strip():
        raise PlanError("Refusing to write an empty plan.")

    if require_heading and not re.search(r"^#\s+\S", new, re.MULTILINE):
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

    if not allow_personal_data:
        added = _new_personal_data(new, current)
        if added:
            raise PlanError(
                "Refusing to write: this adds what looks like personal data to a "
                "public repository — " + "; ".join(added) + ". The plan documents "
                "training, not identity: keep FTP, VO2max, threshold HR and the "
                "zones, and leave out birth date, age, height and body weight. "
                "Remove it and resend. If this is a false positive (a lifting "
                "load, a race date), pass allow_personal_data=true."
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

async def _fetch_part(path: str) -> dict | None:
    """GET one part of the plan, uncached. `None` if it does not exist yet.

    A missing part is not an error for anything but the primary file. The
    document is split across several files and they arrive one commit at a
    time, so during a reorganisation the reference or log may not exist yet;
    hard-failing the whole read because of that would make the tool useless
    exactly when the plan is being restructured.
    """
    cfg = _config()
    url = f"{_API}/repos/{cfg['repo']}/contents/{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        response = await http.get(
            url, params={"ref": cfg["branch"]}, headers=_headers(cfg["token"])
        )

    if response.status_code == 404:
        if path == cfg["path"]:
            raise PlanError(
                f"No plan at {cfg['repo']}/{path} on branch {cfg['branch']}. "
                "Check TRAINING_PLAN_REPO, TRAINING_PLAN_PATHS and "
                "TRAINING_PLAN_BRANCH."
            )
        return None
    if response.status_code in (401, 403):
        raise PlanError(
            "GitHub refused the read "
            f"({response.status_code}). Without TRAINING_PLAN_GITHUB_TOKEN this "
            "endpoint is rate-limited to 60 requests an hour and cannot see "
            "private repositories."
        )
    if response.status_code != 200:
        raise PlanError(f"GitHub returned {response.status_code} reading {path}.")

    body = response.json()
    try:
        markdown = base64.b64decode(body["content"]).decode("utf-8")
    except (KeyError, binascii.Error, UnicodeDecodeError) as exc:
        raise PlanError(f"Could not decode {path}: {exc}") from exc

    return {
        "path": path,
        "markdown": markdown,
        "sha": body["sha"],
    }


async def _fetch_plan() -> dict:
    """GET every configured part and assemble the tool's payload shape.

    Parts are fetched concurrently but assembled in configured order, because
    the join order is part of the contract with the PWA's renderer.
    """
    cfg = _config()
    fetched = await asyncio.gather(*(_fetch_part(path) for path in cfg["paths"]))
    parts = [part for part in fetched if part is not None]

    joined = "\n\n".join(part["markdown"] for part in parts)
    primary = next(
        (part for part in parts if part["path"] == cfg["path"]), parts[0]
    )

    return {
        # The whole document, in render order. Prompts that just want to read
        # the plan use this and never need to know it is split.
        "markdown": joined,
        "parts": parts,
        # The primary part's sha and path, so a caller written against the
        # single-file shape still gets a usable pair.
        "sha": primary["sha"],
        "path": primary["path"],
        "repo": cfg["repo"],
        "branch": cfg["branch"],
    }


@mcp.tool()
async def get_training_plan() -> dict:
    """
    Fetch the athlete's training plan document — the source of truth for their goal
    race, current training block, weekly template, training zones and adjustment log.
    Call this before any coaching analysis, workout design or plan adjustment.

    Call it even if you believe you already know the athlete's goals: remembered or
    summarized training facts are stale by construction, because the plan changes and
    the copy does not. This tool is also the only correct way to read the document —
    fetching the repository over the web returns an HTML page, not the plan.

    The document is split across several files by how often they change. `markdown`
    is all of them joined in render order and is what you want for reading. `parts`
    lists each file with its own `path` and `sha`; update_training_plan writes one
    part at a time and needs the `sha` of the part being changed.
    """
    cfg = _config()
    keys = [_cache_key(path) for path in cfg["paths"]]
    if all(_cache.contains(key) for key in keys):
        cached = [_cache.get(key) for key in keys]
        parts = [part for part in cached if part is not None]
        if parts:
            primary = next(
                (part for part in parts if part["path"] == cfg["path"]), parts[0]
            )
            return {
                "markdown": "\n\n".join(part["markdown"] for part in parts),
                "parts": parts,
                "sha": primary["sha"],
                "path": primary["path"],
                "repo": cfg["repo"],
                "branch": cfg["branch"],
            }

    try:
        result = await _fetch_plan()
    except PlanError as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach GitHub to read the plan: {exc}"}

    by_path = {part["path"]: part for part in result["parts"]}
    for path in cfg["paths"]:
        _cache.set(_cache_key(path), by_path.get(path), PLAN_TTL)
    return result


@mcp.tool()
async def update_training_plan(
    markdown: str,
    base_sha: str,
    message: str,
    path: str = "",
    allow_shrink: bool = False,
    allow_personal_data: bool = False,
) -> dict:
    """
    Write a new version of ONE part of the training plan document, replacing that
    file entirely. Use after the athlete has explicitly approved the change — always
    show the exact text that will change and wait for a yes, because this replaces
    the whole file.

    The plan is split across several files. Send only the part you are changing:
    rewriting the whole document into one part would destroy the split. To change
    two parts, call this twice.

    Args:
        markdown:     The complete new content for this part. Not a fragment or a
                      diff — whatever is sent becomes the entire file.
        base_sha:     The `sha` of THIS PART, from the `parts` list returned by
                      get_training_plan. If it changed since then, the write is
                      rejected rather than silently overwriting the other change.
                      Pass "" to create a part that does not exist yet.
        message:      Commit message describing the adjustment.
        path:         Which part to write, e.g. "content/reference.md". Defaults
                      to the primary part (the one carrying the document title).
        allow_shrink: Permit a new version more than 30% shorter than the current
                      one. Off by default, because that is normally truncation.
        allow_personal_data: Permit text that looks like a birth date, age, height
                      or body weight. Off by default — this plan is in a PUBLIC
                      repository, so never record those; keep FTP, VO2max,
                      threshold HR and the zones, which the coaching needs and
                      which identify far less. Only set this for a genuine false
                      positive, such as a lifting load in pounds.

    Returns the commit URL and any render warnings for the plan's PWA.
    """
    cfg = _config()
    if not cfg["token"]:
        return {
            "error": "The plan is readable but not writable: "
            "TRAINING_PLAN_GITHUB_TOKEN is not configured on this server."
        }

    target = path.strip() or cfg["path"]
    if target not in cfg["paths"]:
        return {
            "error": f"{target!r} is not one of this plan's parts: "
            + ", ".join(cfg["paths"])
            + ". Call get_training_plan to see them."
        }

    try:
        current = await _fetch_plan()
        existing = next(
            (part for part in current["parts"] if part["path"] == target), None
        )
        validate_plan_update(
            markdown,
            existing["markdown"] if existing else "",
            allow_shrink=allow_shrink,
            # Checked below against the whole document instead of this part
            # alone, so the validator must not also check it against the part.
            allow_personal_data=True,
            # Only the primary part carries the document's H1; the reference and
            # log legitimately start at '##'.
            require_heading=(target == cfg["path"]),
        )
        # Personal data is judged against the WHOLE document, not just this
        # part. The guard's rule is "a pattern the plan does not already carry",
        # and moving a line from one part to another must not read as newly
        # introducing it.
        if not allow_personal_data:
            added = _new_personal_data(markdown, current["markdown"])
            if added:
                raise PlanError(
                    "Refusing to write: this adds what looks like personal data "
                    "to a public repository — " + "; ".join(added) + ". The plan "
                    "documents training, not identity: keep FTP, VO2max, "
                    "threshold HR and the zones, and leave out birth date, age, "
                    "height and body weight. Remove it and resend. If this is a "
                    "false positive (a lifting load, a race date), pass "
                    "allow_personal_data=true."
                )
    except PlanError as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach GitHub to read the plan: {exc}"}

    existing_sha = existing["sha"] if existing else ""
    if existing_sha != base_sha:
        if not existing:
            return {
                "error": f"{target} does not exist yet, so it can only be "
                "created — pass base_sha=\"\" to create it."
            }
        return {
            "error": "The plan changed since it was read (base_sha is stale). "
            "Call get_training_plan again, re-apply the edit to the current "
            "text, and retry — otherwise this write would discard whatever "
            "changed in between."
        }

    url = f"{_API}/repos/{cfg['repo']}/contents/{target}"
    payload = {
        "message": message,
        "content": base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
        "branch": cfg["branch"],
    }
    # GitHub creates the file when `sha` is absent and updates it when present.
    if base_sha:
        payload["sha"] = base_sha
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
        return {
            "error": f"GitHub returned {response.status_code} writing {target}."
        }

    # The document just changed; the next reader must not get the old one.
    _cache.invalidate("get_training_plan")

    commit = response.json().get("commit", {})
    result = {
        "status": "written",
        "commit_sha": commit.get("sha"),
        "commit_url": commit.get("html_url"),
        "repo": cfg["repo"],
        "path": target,
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
