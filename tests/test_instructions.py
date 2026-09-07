"""The resident server INSTRUCTIONS in server.py.

Unlike prompts, this text applies to every request without being invoked, so
it is the only guidance a casual ("swap today's run") request is guaranteed to
see — the structured `adapt_plan` prompt has to be explicitly selected, and
most workout-change requests never go through it. A Garmin-side calendar
change (schedule_workout, update_workout, create_workout, unschedule_workout)
has no code path back to the plan document at all: this string is what has to
carry that reminder, so a regression here is a silent regression in behavior,
not just a docs typo.

Runs standalone (`python tests/test_instructions.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_instructions_link_garmin_writes_back_to_the_plan() -> None:
    from garmlink.server import INSTRUCTIONS

    for tool in ("schedule_workout", "update_workout", "create_workout"):
        assert tool in INSTRUCTIONS, (
            f"INSTRUCTIONS must name {tool} — it is the tool a casual "
            "'change my workout' request actually calls, with no other path "
            "back to the plan document"
        )
    assert "adapt_plan" in INSTRUCTIONS, (
        "INSTRUCTIONS should point at the structured prompt that already "
        "does this properly, for the cases that do go through it"
    )
    assert "update_training_plan" in INSTRUCTIONS, (
        "the instructions must name the actual write tool, not just gesture "
        "at 'the plan'"
    )
    assert "separate" in INSTRUCTIONS.lower(), (
        "the plan write must be framed as its own confirmation, matching "
        "adapt_plan's 'second write to a second system' — folding it into "
        "the calendar confirmation is how this got missed in the first place"
    )


def test_instructions_are_attached_to_the_server() -> None:
    from garmlink.server import INSTRUCTIONS, mcp

    # instructions is stored on the low-level MCP server FastMCP wraps, not as
    # a plain attribute — guard against a refactor detaching the string from
    # the object clients actually see.
    assert mcp.instructions == INSTRUCTIONS


if __name__ == "__main__":
    test_instructions_link_garmin_writes_back_to_the_plan()
    print("instructions: Garmin write tools are linked back to the plan")
    try:
        test_instructions_are_attached_to_the_server()
    except ImportError as e:
        print(f"skipped server attachment check (no fastmcp env): {e}")
    else:
        print("instructions: attached to the running server")
