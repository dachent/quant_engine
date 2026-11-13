"""Every stage argv in release-stages.json parses under its own tool's argparse parser.

The graph, the tool parsers and their tests can disagree (lane-real passed a flag its parser lacks and failed in
0.9 s at release time). This parses each stage's command line, every conditional group included, with a dummy value
for each placeholder, and fails on any flag the tool does not know or any required argument the stage omits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import runpy
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPH = json.loads(
    (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_text(
        encoding="utf-8"
    )
)
ROOTS = {
    "{tools}": REPO_ROOT / "tools",
    "{mvp2}": REPO_ROOT / "ops" / "mvp2-release" / "scripts",
}
# a refusal about the VALUE of a dummy argument is not an argv mismatch
STRUCTURAL = (
    "unrecognized arguments",
    "the following arguments are required",
    "expected one argument",
    "expected at least one argument",
    "ambiguous option",
    "invalid choice: 'x'",
)


class _Parsed(BaseException):
    pass


class _Mismatch(BaseException):
    pass


def _flatten(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        else:
            out.extend(_flatten(item["args"]))
    return out


def _dummy(arg: str) -> str:
    return re.sub(r"\{[^{}]+\}", "x", arg)


def _command(argv: list[str]) -> tuple[Path, list[str]] | None:
    if len(argv) < 2 or argv[0] != "{python}":
        return None
    root = next((r for r in ROOTS if argv[1].startswith(r + "/")), None)
    if root is None:
        return None  # -m modules and node scripts are not argparse scripts of this repo
    return ROOTS[root] / argv[1][len(root) + 1 :], [_dummy(x) for x in argv[2:]]


def _stages() -> list[tuple[str, Path, list[str]]]:
    found = []
    for stage in GRAPH["stages"]:
        argv = _flatten(stage.get("argv", []))
        command = _command(argv)
        if command is not None:
            found.append((stage["id"], *command))
        if (
            "--" in argv
        ):  # a command passed through after "--" (post-commit-verify's validator)
            tail = _command(argv[argv.index("--") + 1 :])
            if tail is not None:
                found.append((f"{stage['id']} (after --)", *tail))
    return found


@pytest.mark.parametrize(("stage_id", "script", "args"), _stages())
def test_stage_argv_parses_under_its_tools_parser(
    stage_id: str,
    script: Path,
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "argparse" not in script.read_text(encoding="utf-8"):
        pytest.skip(f"{script.name} does not use argparse")
    original_known = argparse.ArgumentParser.parse_known_args
    original_args = argparse.ArgumentParser.parse_args
    inside_parse_args = []

    def known(self: argparse.ArgumentParser, a: Any = None, ns: Any = None) -> Any:
        result = original_known(self, a, ns)
        if inside_parse_args:
            return (
                result  # parse_args reports leftover arguments itself, through error()
            )
        raise _Parsed(result)

    def parse_args(self: argparse.ArgumentParser, a: Any = None, ns: Any = None) -> Any:
        inside_parse_args.append(True)
        try:
            result = original_args(self, a, ns)
        finally:
            inside_parse_args.pop()
        raise _Parsed(result)

    def error(self: argparse.ArgumentParser, message: str) -> Any:
        if any(token in message for token in STRUCTURAL):
            raise _Mismatch(message)
        raise _Parsed(message)  # a value refusal: the flags themselves were accepted

    monkeypatch.setattr(argparse.ArgumentParser, "parse_known_args", known)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_args)
    monkeypatch.setattr(argparse.ArgumentParser, "error", error)
    monkeypatch.setattr(sys, "argv", [str(script), *args])
    monkeypatch.syspath_prepend(str(script.parent))
    monkeypatch.syspath_prepend(str(REPO_ROOT / "tools"))
    try:
        runpy.run_path(str(script), run_name="__main__")
    except _Parsed:
        return
    except _Mismatch as exc:
        pytest.fail(f"stage {stage_id}: {script.name} refuses its argv: {exc}")
    except SystemExit as exc:
        pytest.fail(f"stage {stage_id}: {script.name} exited {exc.code} before parsing")
