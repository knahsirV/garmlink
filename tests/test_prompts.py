"""Coaching workflows exposed as MCP prompts.

Prompts are how the coaching workflows reach every client — Claude Desktop
surfaces them in its prompt menu, Claude Code as /mcp__garmin__<name>. They
used to live in .claude/skills/*.md, which only Claude Code reads and which
were in the wrong layout to load at all.

The contract check matters: those skill files referenced get_training_load_trend
for months while that tool was dead. A prompt that names a tool the server does
not expose is the same rot, and nothing else would catch it.

Runs standalone (`python tests/test_prompts.py`) or under pytest.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp import Client  # noqa: E402

EXPECTED = {"morning_check", "analyze_week", "race_readiness", "create_workout_guide"}


def _server():
    import garmin_mcp.server as server
    return server.mcp


def test_prompts_are_exposed():
    async def run():
        async with Client(_server()) as c:
            return {p.name for p in await c.list_prompts()}

    names = asyncio.run(run())
    missing = EXPECTED - names
    assert not missing, f"prompts not exposed: {sorted(missing)}"


def test_prompts_render_with_arguments():
    async def run():
        rendered = {}
        async with Client(_server()) as c:
            for p in await c.list_prompts():
                if p.name not in EXPECTED:
                    continue
                args = {a.name: "2026-08-20" for a in (p.arguments or []) if a.required}
                got = await c.get_prompt(p.name, args)
                text = "\n".join(
                    m.content.text for m in got.messages
                    if getattr(m.content, "text", None)
                )
                assert text.strip(), f"{p.name} rendered empty"
                rendered[p.name] = text
        return rendered

    rendered = asyncio.run(run())
    assert set(rendered) == EXPECTED, sorted(rendered)


def test_prompts_only_reference_tools_that_exist():
    """A prompt naming a nonexistent tool is the rot that killed the skills."""
    async def run():
        async with Client(_server()) as c:
            tool_names = {t.name for t in await c.list_tools()}
            problems = []
            for p in await c.list_prompts():
                if p.name not in EXPECTED:
                    continue
                args = {a.name: "2026-08-20" for a in (p.arguments or []) if a.required}
                got = await c.get_prompt(p.name, args)
                text = "\n".join(
                    m.content.text for m in got.messages
                    if getattr(m.content, "text", None)
                )
                # Tools are referenced in backticks: `get_wellness_snapshot`
                for ref in re.findall(r"`([a-z][a-z0-9_]{3,})`", text):
                    if ref.startswith("get_") or ref in {"create_workout", "suggest_recovery"}:
                        if ref not in tool_names:
                            problems.append(f"{p.name} references unknown tool `{ref}`")
            return problems

    problems = asyncio.run(run())
    assert not problems, "prompts reference tools the server does not expose:\n  " + "\n  ".join(problems)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {str(exc)[:160]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
