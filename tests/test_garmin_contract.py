"""Contract test: every garminconnect method referenced by a tool must exist
and accept the arguments the tool passes.

This catches upstream renames and signature changes at CI time rather than in
production. Runs standalone (`python tests/test_garmin_contract.py`) or under
pytest.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from garminconnect import Garmin

TOOLS_DIR = Path(__file__).resolve().parent.parent / "src" / "garmlink" / "tools"

# Kwargs consumed by GarminClient.call() itself, never forwarded to Garmin.
_CLIENT_KWARGS = {"ttl", "cache"}


def iter_call_sites():
    """Yield (file, lineno, method_name, n_positional, kwarg_names) per client.call()."""
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "call"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "client"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue  # dynamic method name — checked separately below
            method_name = node.args[0].value
            n_positional = len(node.args) - 1
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue  # can't statically count
            kwargs = {k.arg for k in node.keywords if k.arg} - _CLIENT_KWARGS
            yield path.name, node.lineno, method_name, n_positional, kwargs


def iter_dynamic_method_names():
    """Yield (file, lineno, method_name) for names held in method_map dicts."""
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "method_map" for t in node.targets
            ):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for v in node.value.values:
                # Either "name" or ("name", extractor)
                const = None
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    const = v.value
                elif isinstance(v, ast.Tuple) and v.elts:
                    first = v.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        const = first.value
                if const:
                    yield path.name, node.lineno, const


def iter_passthrough_tools():
    """Yield (file, lineno, tool_name, tool_return_src, garmin_method).

    Only covers tools whose body is a single `return await client.call("X", ...)`,
    i.e. the ones that hand a Garmin payload straight back to the client.
    """
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.returns is None:
                continue
            ret = node.body[-1]
            if not isinstance(ret, ast.Return) or not isinstance(ret.value, ast.Await):
                continue
            call = ret.value.value
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            if not (
                isinstance(fn, ast.Attribute)
                and fn.attr == "call"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "client"
            ):
                continue
            if not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            yield (
                path.name,
                node.lineno,
                node.name,
                ast.unparse(node.returns),
                call.args[0].value,
            )


def check_return_annotations() -> list[str]:
    """A tool annotated `-> dict` that returns a list makes FastMCP raise.

    FastMCP builds the output schema from the annotation; with `-> dict` it
    passes a list straight into ToolResult, which rejects non-dict structured
    content. Any upstream method that can return a list needs `list` in the
    tool's annotation.
    """
    errors = []
    for fname, lineno, tool, annotation, method in iter_passthrough_tools():
        fn = getattr(Garmin, method, None)
        if fn is None:
            continue  # already reported by check()
        upstream = str(inspect.signature(fn).return_annotation)
        if "list" in upstream and "list" not in annotation:
            errors.append(
                f"{fname}:{lineno}: tool {tool}() is annotated `-> {annotation}` but "
                f"{method} returns {upstream}; FastMCP rejects a list under a dict "
                f"output schema"
            )
    return errors


# Top-level activity types, from a live get_activity_types() call. Anything with
# a parent other than the root (17) is a sub-type, and get_activities_by_date
# rejects sub-types with "Activity type cannot be an activity sub type" — a 400,
# not an empty list.
TOP_LEVEL_ACTIVITY_TYPES = {
    "running", "cycling", "hiking", "other", "walking", "swimming",
    "fitness_equipment", "multi_sport", "steps", "diving", "safety",
    "winter_sports", "para_sports", "team_sports", "racket_sports",
    "water_sports",
}


def check_activity_types() -> list[str]:
    """get_activities_by_date must be given a top-level activity type."""
    errors = []
    for fname, lineno, method, n_pos, _kwargs in iter_call_sites():
        if method != "get_activities_by_date":
            continue
        for path in TOOLS_DIR.glob(fname):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "call"):
                    continue
                if node.lineno != lineno or not node.args:
                    continue
                if not (isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "get_activities_by_date"):
                    continue
                # positional: (method, startdate, enddate, activitytype)
                if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
                    value = node.args[3].value
                    if value not in TOP_LEVEL_ACTIVITY_TYPES:
                        errors.append(
                            f"{fname}:{lineno}: {value!r} is not a top-level activity "
                            f"type; get_activities_by_date returns 400 for sub-types"
                        )
    return errors


def check() -> list[str]:
    errors: list[str] = []

    for fname, lineno, method, n_pos, kwargs in iter_call_sites():
        fn = getattr(Garmin, method, None)
        if fn is None or not callable(fn):
            errors.append(f"{fname}:{lineno}: Garmin has no method {method!r}")
            continue
        sig = inspect.signature(fn)
        try:
            # `self` is bound at call time; stand in with None.
            sig.bind(None, *([object()] * n_pos), **{k: object() for k in kwargs})
        except TypeError as exc:
            errors.append(
                f"{fname}:{lineno}: {method}({n_pos} positional"
                + (f", kwargs={sorted(kwargs)}" if kwargs else "")
                + f") does not fit {method}{sig}: {exc}"
            )

    errors.extend(check_return_annotations())
    errors.extend(check_activity_types())

    for fname, lineno, method in iter_dynamic_method_names():
        if not hasattr(Garmin, method):
            errors.append(
                f"{fname}:{lineno}: method_map references nonexistent Garmin method {method!r}"
            )

    return errors


def test_garmin_api_contract():
    errors = check()
    assert not errors, "Garmin API contract violations:\n  " + "\n  ".join(errors)


if __name__ == "__main__":
    errs = check()
    if errs:
        print(f"{len(errs)} contract violation(s):\n")
        for e in errs:
            print(f"  {e}")
        raise SystemExit(1)
    print("OK: all client.call() sites match the installed garminconnect API")
