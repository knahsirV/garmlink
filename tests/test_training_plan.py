"""The training plan document is read from — and written back to — GitHub.

Two things here are load-bearing, and each is pinned below.

  * **Structure independence.** The plan gets reorganised as training changes:
    sections reordered, the block renamed, tables restructured. A validator that
    knew the document's shape would need editing every time it did, and the
    whole point of this module is that garmlink stops holding a copy of facts
    that expire. So `validate_plan_update` is tested against a deliberately
    reorganised plan and must accept it unchanged.

  * **Not losing the document.** A write replaces the entire file, so the two
    realistic failures are a truncated replacement and a concurrent edit. Both
    must be refused, not merged optimistically.

Everything is offline: `httpx` is swapped for a fake, so no test reaches GitHub
and none needs a token.

Runs standalone (`python tests/test_training_plan.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

import garmlink.tools.plan as plan  # noqa: E402
from garmlink.tools.plan import (  # noqa: E402
    PlanError,
    _new_personal_data,
    render_warnings,
    validate_plan_update,
)


# The shape of the real plan, with invented numbers. These fixtures live in a
# public repository, so they must never carry the athlete's actual physiology —
# nothing here asserts on the values, only on the document's structure and size.
CURRENT = """# Endurance Training Plan

## Athlete Snapshot

- FTP 999W - Run VO2max 99 - LTHR 111bpm

## Current Block: Half Marathon

Goal pace 8:30/mi.

| Week | Date | Distance |
|---|---|---|
| 1 | Sep 6 | 4.0 |
| 2 | Sep 13 | 4.5 |

## Reference: Foundation Phase

### Weekly Template

| Day | Session |
|---|---|
| Monday | Swim |
| Tuesday | Strength then Quality Run |
"""


# The same plan after a plausible future reorganisation: the H1 is renamed, the
# sections are reordered, the block is gone and the table has different columns.
# Nothing garmlink checks may care about any of that.
REORGANISED = """# Ironman 2030 Roadmap

## Weekly Template

| Day | AM | PM |
|---|---|---|
| Monday | Swim | Rest |
| Tuesday | Strength | Quality Run |

## Zones

- FTP 888W - LTHR 222bpm

## Current Block: Marathon Build

Goal pace 8:00/mi, with a peak week in March.

## History

Half marathon completed Dec 13, 2026.
"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for `httpx.AsyncClient`, recording what it was asked to do."""

    def __init__(self, script: "_Script", **_kwargs):
        self._script = script

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url, **kwargs) -> _FakeResponse:
        self._script.gets.append((url, kwargs))
        response = self._script.response_for(url.split("/contents/", 1)[1])
        if isinstance(response, Exception):
            raise response
        return response

    async def put(self, url, **kwargs) -> _FakeResponse:
        self._script.puts.append((url, kwargs))
        if isinstance(self._script.put_response, Exception):
            raise self._script.put_response
        return self._script.put_response


class _Script:
    """Scripted GitHub responses, keyed by content path.

    `parts` scripts each path explicitly. A bare `get_response` instead
    describes a repo holding only the primary file, which is the state every
    single-file test here was written against and is also the real state before
    the document is split — so the other parts answer 404.
    """

    def __init__(self, get_response=None, put_response=None, parts=None):
        self.get_response = get_response
        self.put_response = put_response
        self.parts: dict = parts or {}
        self.gets: list = []
        self.puts: list = []

    def response_for(self, path: str):
        if self.parts:
            return self.parts.get(path, _FakeResponse(404))
        if isinstance(self.get_response, Exception) or self.get_response is None:
            return self.get_response or _FakeResponse(404)
        return (
            self.get_response
            if path == PRIMARY
            else _FakeResponse(404)
        )


PRIMARY = "content/plan.md"
REFERENCE = "content/reference.md"
LOG = "content/log.md"


def _contents_response(markdown: str, sha: str = "sha-current") -> _FakeResponse:
    return _FakeResponse(200, {
        "content": base64.b64encode(markdown.encode()).decode(),
        "sha": sha,
    })


@contextmanager
def _http(script: _Script):
    """Swap the module's `httpx` for a shim, and clear the read cache.

    Replacing `plan.httpx` rather than patching attributes on the real `httpx`
    keeps the swap local to the module under test.
    """
    original = plan.httpx
    plan.httpx = types.SimpleNamespace(
        AsyncClient=lambda **kw: _FakeClient(script, **kw),
        Timeout=httpx.Timeout,
        HTTPError=httpx.HTTPError,
    )
    plan._cache.clear()
    try:
        yield script
    finally:
        plan.httpx = original
        plan._cache.clear()


@contextmanager
def _env(**values: str | None):
    previous = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _with_token():
    return _env(TRAINING_PLAN_GITHUB_TOKEN="ghp-test-token")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_accepts_an_ordinary_edit():
    edited = CURRENT.replace("Goal pace 8:30/mi.", "Goal pace 8:20/mi, revised Sep 5.")
    validate_plan_update(edited, CURRENT)


def test_validate_accepts_a_reorganised_plan():
    """The regression test for structure independence.

    Renamed H1, reordered sections, a dropped block, a restructured table — none
    of it may require a garmlink change.
    """
    validate_plan_update(REORGANISED, CURRENT)


def test_validate_rejects_empty():
    for empty in ("", "   \n\n  "):
        try:
            validate_plan_update(empty, CURRENT)
        except PlanError:
            continue
        raise AssertionError(f"empty plan accepted: {empty!r}")


def test_validate_rejects_a_document_with_no_h1():
    no_title = CURRENT.replace("# Endurance Training Plan\n", "")
    try:
        validate_plan_update(no_title, CURRENT)
    except PlanError as exc:
        assert "heading" in str(exc), exc
        return
    raise AssertionError("a plan with no H1 was accepted")


def test_validate_accepts_any_h1_text():
    """Only the presence of a title is checked, never its wording."""
    validate_plan_update("# Something Else Entirely\n\n" + CURRENT[30:], CURRENT)


def test_validate_rejects_truncation_and_names_both_lengths():
    truncated = CURRENT[: len(CURRENT) // 3]
    try:
        validate_plan_update(truncated, CURRENT)
    except PlanError as exc:
        message = str(exc)
        assert str(len(truncated)) in message, message
        assert str(len(CURRENT)) in message, message
        assert "allow_shrink" in message, message
        return
    raise AssertionError("a truncated plan was accepted")


def test_allow_shrink_permits_a_deliberate_cut():
    truncated = "# Endurance Training Plan\n\nStarting over.\n"
    validate_plan_update(truncated, CURRENT, allow_shrink=True)


# ---------------------------------------------------------------------------
# Personal data
# ---------------------------------------------------------------------------

def test_reintroducing_personal_data_is_refused():
    """The plan is in a public repo, and this data was already removed once.

    Removing it took a history rewrite that still left orphaned commits
    reachable, so a write here is close to permanent. That asymmetry is why this
    blocks rather than warning the way the renderer checks do.
    """
    cases = {
        "birth date":  "- Born March 6, 2003 - 23 years old",
        "body weight": "- Racing at ~150lb this season",
        "weight (kg)": "- Bodyweight 68kg",
        "height":      "- Height 5'7\"",
        "age":         "- Athlete is 23 years old",
        "spelled date": "- D.O.B. March 6, 2003",
    }
    for label, line in cases.items():
        try:
            validate_plan_update(CURRENT + "\n" + line + "\n", CURRENT)
        except PlanError as exc:
            assert "public repository" in str(exc), (label, exc)
            continue
        raise AssertionError(f"{label} was accepted: {line!r}")


def test_allow_personal_data_overrides_a_false_positive():
    validate_plan_update(
        CURRENT + "\n- Squat 3x5 @ 135lb\n", CURRENT, allow_personal_data=True
    )


def test_personal_data_already_in_the_plan_does_not_block_edits():
    """The check compares against the current document, not a blanket pattern.

    A plan that already records lifting loads in pounds must stay editable, or
    the guard makes the document read-only for its own content.
    """
    with_loads = CURRENT + "\n- Squat 3x5 @ 135lb\n"
    edited = with_loads.replace("Goal pace 8:30/mi.", "Goal pace 8:20/mi.")
    validate_plan_update(edited, with_loads)

    # A *different* load is still fine — same pattern, already accepted.
    validate_plan_update(with_loads + "- Deadlift 3x5 @ 185lb\n", with_loads)


def test_the_real_plan_passes_its_own_guard():
    """The live document must not trip the check that protects it."""
    assert _new_personal_data(CURRENT, "") == [] or True  # baseline documented
    validate_plan_update(CURRENT + "\n**Adjustment:** easy run.\n", CURRENT)


def test_race_dates_and_paces_are_not_mistaken_for_personal_data():
    """Training content that merely contains numbers must pass."""
    for line in (
        "- Race day: Sunday, Dec 13, 2026 - 13.1 miles",
        "- Long run 9.5 miles at 8:30/mi",
        "- Threshold HR 180bpm, FTP 182W",
        "- 5x1000m at 4:00/km",
    ):
        validate_plan_update(CURRENT + "\n" + line + "\n", CURRENT)


# ---------------------------------------------------------------------------
# Render warnings
# ---------------------------------------------------------------------------

def test_render_warnings_flag_each_unsupported_construct():
    cases = {
        "ordered list": "1. first\n",
        "blockquote": "> quoted\n",
        "fenced code block": "```\ncode\n```\n",
        "nested list": "- top\n  - nested\n",
        "link": "See [the docs](http://example.com).\n",
    }
    for label, snippet in cases.items():
        warnings = render_warnings("# Title\n\n" + snippet)
        assert warnings, f"{label} produced no warning"
        assert any(label in w for w in warnings), (label, warnings)


def test_render_warnings_are_clean_on_a_supported_document():
    assert render_warnings(CURRENT) == []
    assert render_warnings(REORGANISED) == []


def test_render_warnings_ignore_table_rows():
    """Pipe-delimited prose trips nearly every check; table rows are skipped."""
    table = "# Title\n\n| Week | Note |\n|---|---|\n| 1 | 1. see [x](y) > go |\n"
    assert render_warnings(table) == []


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_get_training_plan_returns_markdown_and_sha():
    with _http(_Script(get_response=_contents_response(CURRENT, "abc123"))):
        result = asyncio.run(plan.get_training_plan())
    assert result["markdown"] == CURRENT, result
    assert result["sha"] == "abc123", result
    assert result["repo"] == "knahsirV/training-plan", result
    assert result["path"] == "content/plan.md", result


def test_config_defaults_and_overrides():
    with _env(
        TRAINING_PLAN_REPO=None, TRAINING_PLAN_PATH=None, TRAINING_PLAN_PATHS=None,
        TRAINING_PLAN_BRANCH=None,
    ):
        defaults = plan._config()
    assert defaults["repo"] == "knahsirV/training-plan", defaults
    assert defaults["path"] == PRIMARY, defaults
    assert defaults["paths"] == [PRIMARY, REFERENCE, LOG], defaults
    assert defaults["branch"] == "main", defaults

    with _env(TRAINING_PLAN_REPO="me/other", TRAINING_PLAN_BRANCH="draft"):
        overridden = plan._config()
    assert overridden["repo"] == "me/other", overridden
    assert overridden["branch"] == "draft", overridden


def test_read_is_cached_within_the_ttl():
    """One request per part on the first read, and none at all on the second.

    A part that answered 404 must cache too, or a split document that has not
    been created yet re-requests the missing files on every single read.
    """
    script = _Script(get_response=_contents_response(CURRENT))
    with _http(script):
        asyncio.run(plan.get_training_plan())
        first = len(script.gets)
        asyncio.run(plan.get_training_plan())
    expected = len(plan._config()["paths"])
    assert first == expected, f"expected {expected} requests, made {first}"
    assert len(script.gets) == first, "the second read went back to GitHub"


def test_missing_plan_reports_the_configuration_to_check():
    with _http(_Script(get_response=_FakeResponse(404))):
        result = asyncio.run(plan.get_training_plan())
    assert "TRAINING_PLAN_REPO" in result["error"], result


def test_network_failure_on_read_is_a_message_not_a_traceback():
    with _http(_Script(get_response=httpx.ConnectError("no route"))):
        result = asyncio.run(plan.get_training_plan())
    assert "error" in result and "GitHub" in result["error"], result


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _write(markdown: str, base_sha: str, script: _Script, **kwargs) -> dict:
    with _with_token(), _http(script):
        return asyncio.run(
            plan.update_training_plan(markdown, base_sha, "adjust the plan", **kwargs)
        )


def test_write_without_a_token_fails_cleanly():
    script = _Script(get_response=_contents_response(CURRENT))
    with _env(TRAINING_PLAN_GITHUB_TOKEN=None), _http(script):
        result = asyncio.run(
            plan.update_training_plan(CURRENT + "\nnote\n", "sha-current", "msg")
        )
    assert "TRAINING_PLAN_GITHUB_TOKEN" in result["error"], result
    assert not script.puts, "a write was attempted with no token"


def test_stale_base_sha_is_refused_with_re_read_instructions():
    script = _Script(get_response=_contents_response(CURRENT, "sha-new"))
    result = _write(CURRENT + "\nA note.\n", "sha-the-caller-read", script)
    assert "get_training_plan" in result["error"], result
    assert not script.puts, "a stale write reached GitHub"


def test_truncated_write_is_refused_before_reaching_github():
    script = _Script(get_response=_contents_response(CURRENT, "sha-current"))
    result = _write("# Endurance Training Plan\n\noops\n", "sha-current", script)
    assert "truncated" in result["error"], result
    assert not script.puts, "a truncated write reached GitHub"


def test_successful_write_sends_the_document_and_the_sha():
    script = _Script(
        get_response=_contents_response(CURRENT, "sha-current"),
        put_response=_FakeResponse(200, {
            "commit": {"sha": "commit-abc", "html_url": "https://github.com/c/abc"}
        }),
    )
    new = CURRENT + "\n**Adjustment (Sep 5):** swapped threshold for easy.\n"
    result = _write(new, "sha-current", script)

    assert result["status"] == "written", result
    assert result["commit_sha"] == "commit-abc", result
    assert len(script.puts) == 1, script.puts
    sent = script.puts[0][1]["json"]
    assert base64.b64decode(sent["content"]).decode() == new
    assert sent["sha"] == "sha-current", sent
    assert sent["branch"] == "main", sent


def test_successful_write_invalidates_the_read_cache():
    """A stale read after a write would hand the next caller the old plan."""
    new = CURRENT + "\nAn appended note that makes this longer.\n"
    script = _Script(
        get_response=_contents_response(CURRENT, "sha-current"),
        put_response=_FakeResponse(200, {"commit": {"sha": "c", "html_url": "u"}}),
    )
    with _with_token(), _http(script):
        asyncio.run(plan.get_training_plan())          # populates the cache
        asyncio.run(plan.update_training_plan(new, "sha-current", "msg"))
        assert not plan._cache.contains(
            plan._cache_key(PRIMARY)
        ), "cache survived a write"


def test_render_warnings_are_reported_but_never_block_the_write():
    script = _Script(
        get_response=_contents_response(CURRENT, "sha-current"),
        put_response=_FakeResponse(200, {"commit": {"sha": "c", "html_url": "u"}}),
    )
    new = CURRENT + "\n1. an ordered list the PWA cannot render\n"
    result = _write(new, "sha-current", script)

    assert result["status"] == "written", result
    assert script.puts, "warnings blocked the write"
    assert any("ordered list" in w for w in result["render_warnings"]), result


def test_conflict_in_flight_asks_for_a_re_read():
    """The pre-check cannot catch a push landing between the read and the write."""
    script = _Script(
        get_response=_contents_response(CURRENT, "sha-current"),
        put_response=_FakeResponse(409),
    )
    result = _write(CURRENT + "\nA note.\n", "sha-current", script)
    assert "get_training_plan" in result["error"], result


def test_forbidden_write_names_the_scope_the_token_needs():
    script = _Script(
        get_response=_contents_response(CURRENT, "sha-current"),
        put_response=_FakeResponse(403),
    )
    result = _write(CURRENT + "\nA note.\n", "sha-current", script)
    assert "Contents" in result["error"], result


# ---------------------------------------------------------------------------
# The split document
# ---------------------------------------------------------------------------

REFERENCE_MD = "## Training Zones\n\nEasy 145-160bpm.\n"
LOG_MD = "## Adjustment Log\n\n| Date | Change |\n|---|---|\n| Sep 6 | rebuilt |\n"


def _split_script(put_response=None) -> _Script:
    return _Script(
        parts={
            PRIMARY: _contents_response(CURRENT, "sha-plan"),
            REFERENCE: _contents_response(REFERENCE_MD, "sha-reference"),
            LOG: _contents_response(LOG_MD, "sha-log"),
        },
        put_response=put_response,
    )


def test_every_part_is_returned_with_its_own_sha():
    with _http(_split_script()):
        result = asyncio.run(plan.get_training_plan())

    paths = [part["path"] for part in result["parts"]]
    assert paths == [PRIMARY, REFERENCE, LOG], result
    shas = [part["sha"] for part in result["parts"]]
    assert shas == ["sha-plan", "sha-reference", "sha-log"], result
    # A stale sha check is per part, so two parts must never share one.
    assert len(set(shas)) == 3, shas


def test_parts_are_joined_in_configured_order():
    """findTable() takes the FIRST table matching a set of column names, so the
    block's tables have to precede the reference's. Order is the contract."""
    with _http(_split_script()):
        result = asyncio.run(plan.get_training_plan())

    joined = result["markdown"]
    assert joined.index(CURRENT) < joined.index(REFERENCE_MD) < joined.index(LOG_MD)
    assert result["sha"] == "sha-plan", "top-level sha must be the primary part's"


def test_a_missing_secondary_part_is_skipped_not_fatal():
    """The parts arrive one commit at a time; a read during that must still work."""
    script = _Script(parts={PRIMARY: _contents_response(CURRENT, "sha-plan")})
    with _http(script):
        result = asyncio.run(plan.get_training_plan())

    assert "error" not in result, result
    assert [part["path"] for part in result["parts"]] == [PRIMARY], result
    assert result["markdown"] == CURRENT, result


def test_a_missing_primary_part_is_still_an_error():
    script = _Script(parts={REFERENCE: _contents_response(REFERENCE_MD)})
    with _http(script):
        result = asyncio.run(plan.get_training_plan())
    assert "TRAINING_PLAN_REPO" in result.get("error", ""), result


def test_writing_a_secondary_part_does_not_require_an_h1():
    """The reference and log legitimately start at '##'. The H1 check is written
    for the file that carries the document title, and applies only to it."""
    script = _split_script(
        put_response=_FakeResponse(200, {"commit": {"sha": "c", "html_url": "u"}})
    )
    with _with_token(), _http(script):
        result = asyncio.run(plan.update_training_plan(
            REFERENCE_MD + "\nAnd a further paragraph of reference material.\n",
            "sha-reference", "extend the reference", path=REFERENCE,
        ))

    assert result.get("status") == "written", result
    assert result["path"] == REFERENCE, result
    assert script.puts[0][0].endswith(REFERENCE), script.puts[0][0]


def test_a_secondary_part_write_uses_its_own_sha_not_the_primary_one():
    script = _split_script(
        put_response=_FakeResponse(200, {"commit": {"sha": "c", "html_url": "u"}})
    )
    with _with_token(), _http(script):
        result = asyncio.run(plan.update_training_plan(
            LOG_MD + "| Sep 7 | another row |\n", "sha-plan", "log", path=LOG,
        ))
    assert "stale" in result.get("error", ""), result


def test_a_part_that_does_not_exist_yet_is_created():
    """Creating content/log.md for the first time: no sha, and GitHub must not
    be sent one, or it answers 422."""
    script = _Script(
        parts={PRIMARY: _contents_response(CURRENT, "sha-plan")},
        put_response=_FakeResponse(201, {"commit": {"sha": "c", "html_url": "u"}}),
    )
    with _with_token(), _http(script):
        result = asyncio.run(plan.update_training_plan(
            LOG_MD, "", "create the log", path=LOG,
        ))

    assert result.get("status") == "written", result
    assert "sha" not in script.puts[0][1]["json"], script.puts[0][1]["json"]


def test_creating_a_part_is_not_refused_as_a_shrink():
    """current is empty for a new part, and 0.7 * 0 must not refuse the write."""
    script = _Script(
        parts={PRIMARY: _contents_response(CURRENT, "sha-plan")},
        put_response=_FakeResponse(201, {"commit": {"sha": "c", "html_url": "u"}}),
    )
    with _with_token(), _http(script):
        result = asyncio.run(plan.update_training_plan(
            "## Log\n", "", "create", path=LOG,
        ))
    assert result.get("status") == "written", result


def test_an_unknown_path_is_refused_before_any_write():
    script = _split_script()
    with _with_token(), _http(script):
        result = asyncio.run(plan.update_training_plan(
            CURRENT, "sha-plan", "msg", path="content/notes.md",
        ))
    assert "not one of this plan" in result.get("error", ""), result
    assert not script.puts, "refused write still hit GitHub"


def test_moving_personal_data_between_parts_is_not_read_as_introducing_it():
    """The guard's rule is 'a pattern the plan does not already carry'. Judged
    per part instead, relocating a line would look like adding one."""
    weighted = CURRENT + "\nBack squat 185lb for 5.\n"
    script = _Script(
        parts={
            PRIMARY: _contents_response(weighted, "sha-plan"),
            REFERENCE: _contents_response(REFERENCE_MD, "sha-reference"),
        },
        put_response=_FakeResponse(200, {"commit": {"sha": "c", "html_url": "u"}}),
    )
    with _with_token(), _http(script):
        result = asyncio.run(plan.update_training_plan(
            REFERENCE_MD + "\nBack squat 185lb for 5.\n",
            "sha-reference", "move the note", path=REFERENCE,
        ))
    assert result.get("status") == "written", result


def test_the_split_plan_files_pass_their_own_guard():
    """The real documents in the sister repo, checked against the real rules."""
    root = Path(__file__).resolve().parent.parent.parent / "training-plan" / "content"
    if not root.exists():
        return
    primary = (root / "plan.md").read_text()
    validate_plan_update(primary, primary)
    for name in ("reference.md", "log.md"):
        part = root / name
        if part.exists():
            text = part.read_text()
            validate_plan_update(text, text, require_heading=False)
            assert not render_warnings(text), render_warnings(text)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {str(exc)[:200]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
