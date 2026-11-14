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
# a refusal about the VALUE of a dummy argument is not an argv mismatch; these are about the SHAPE of the command
STRUCTURAL = re.compile(
    r"unrecognized arguments|the following arguments are required|expected (one|at least one|at most one|\d+) arguments?"
    r"|ambiguous option|invalid choice: 'x'|not allowed with argument|one of the arguments .* is required"
)
# a typed option takes the first of these its `type=` accepts (so int/float/date options are not value refusals)
PROBES = ("x", "1", "1.0", "2026-01-01")


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


def _typed(parser: argparse.ArgumentParser, args: list[str]) -> list[str]:
    """Replace each option's dummy "x" with a value its own choices/type accept (registries, ints, dates)."""
    options = {o: act for act in parser._actions for o in act.option_strings}
    out = list(args)
    for i, token in enumerate(out[:-1]):
        action = options.get(token)
        if action is None or out[i + 1] != "x":
            continue
        if action.choices:
            out[i + 1] = str(next(iter(action.choices)))
        elif callable(action.type):
            for probe in PROBES:
                try:
                    action.type(probe)
                except Exception:
                    continue
                out[i + 1] = probe
                break
            else:
                raise _Mismatch(
                    f"{token}: its type accepts none of the probe values {PROBES}"
                )
    return out


def _module_file(module: str) -> Path:
    """The source file of ``python -m <module>`` in one of the repo's packages."""
    relative = Path(*module.split(".")).with_suffix(".py")
    found = [
        p / relative
        for p in REPO_ROOT.glob("packages/*/src")
        if (p / relative).is_file()
    ]
    assert found, f"no source file for module {module}"
    return found[0]


def _command(argv: list[str]) -> tuple[Path, list[str]] | None:
    if len(argv) < 3 and argv[:2] == ["{python}", "-m"]:
        return None
    if len(argv) >= 3 and argv[:2] == ["{python}", "-m"]:
        return _module_file(argv[2]), [_dummy(x) for x in argv[3:]]
    if len(argv) < 2 or argv[0] != "{python}":
        return None
    root = next((r for r in ROOTS if argv[1].startswith(r + "/")), None)
    if root is None:
        return None  # a script outside this repo's tool roots
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


def _check(
    stage_id: str, script: Path, args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    if "argparse" not in script.read_text(encoding="utf-8"):
        pytest.skip(f"{script.name} does not use argparse")
    original_known = argparse.ArgumentParser.parse_known_args
    original_args = argparse.ArgumentParser.parse_args
    active: list[
        bool
    ] = []  # nesting: parse_args and subparsers call parse_known_args inside

    def known(self: argparse.ArgumentParser, a: Any = None, ns: Any = None) -> Any:
        nested = bool(active)
        active.append(True)
        try:
            result = original_known(
                self, _typed(self, list(sys.argv[1:] if a is None else a)), ns
            )
        finally:
            active.pop()
        if nested:
            return (
                result  # parse_args reports leftover arguments itself, through error()
            )
        if result[1]:
            raise _Mismatch(f"unrecognized arguments: {' '.join(result[1])}")
        raise _Parsed(result)

    def parse_args(self: argparse.ArgumentParser, a: Any = None, ns: Any = None) -> Any:
        active.append(True)
        try:
            result = original_args(self, a, ns)
        finally:
            active.pop()
        raise _Parsed(result)

    def error(self: argparse.ArgumentParser, message: str) -> Any:
        if STRUCTURAL.search(message):
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
    pytest.fail(f"stage {stage_id}: {script.name} returned without parsing its argv")


@pytest.mark.parametrize(("stage_id", "script", "args"), _stages())
def test_stage_argv_parses_under_its_tools_parser(
    stage_id: str,
    script: Path,
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _check(stage_id, script, args, monkeypatch)


TOY = {
    "known_leftover": ("p.parse_known_args()", ["--a", "x", "--nope"]),
    "exclusive": (
        "g = p.add_mutually_exclusive_group(required=True)\n"
        "g.add_argument('--b')\ng.add_argument('--c')\np.parse_args()",
        ["--a", "x", "--b", "x", "--c", "x"],
    ),
    "one_of_required": (
        "g = p.add_mutually_exclusive_group(required=True)\n"
        "g.add_argument('--b')\ng.add_argument('--c')\np.parse_args()",
        ["--a", "x"],
    ),
    "arity": (
        "p.add_argument('--d', nargs=2)\np.parse_args()",
        ["--a", "x", "--d", "x"],
    ),
    "no_parse": ("pass", ["--a", "x"]),
}


def _toy(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "toy.py"
    script.write_bytes(
        (
            "import argparse\np = argparse.ArgumentParser()\n"
            f"p.add_argument('--a', type=int)\n{body}\n"
        ).encode("utf-8")
    )
    return script


@pytest.mark.parametrize("case", sorted(TOY))
def test_the_argv_test_rejects_a_mismatched_toy_script(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body, args = TOY[case]
    with pytest.raises(pytest.fail.Exception):
        _check(case, _toy(tmp_path, body), args, monkeypatch)


def test_a_typed_option_gets_a_value_its_type_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "p.add_argument('--m', choices=['q'])\np.parse_args()"
    _check("typed", _toy(tmp_path, body), ["--a", "x", "--m", "x"], monkeypatch)


def _node_stages() -> list[tuple[str, Path, list[str]]]:
    found = []
    for stage in GRAPH["stages"]:
        argv = _flatten(stage.get("argv", []))
        if argv and argv[0] == "{node}":
            script = REPO_ROOT / argv[1].replace("{repo_root}/", "")
            found.append((stage["id"], script, [_dummy(x) for x in argv[2:]]))
    return found


@pytest.mark.parametrize(("stage_id", "script", "args"), _node_stages())
def test_node_stage_argv_is_name_value_pairs_the_script_reads(
    stage_id: str, script: Path, args: list[str]
) -> None:
    """The node QA scripts take ``--name value`` pairs and read ``options.name``."""
    assert script.is_file(), f"stage {stage_id}: {script} does not exist"
    source = script.read_text(encoding="utf-8")
    assert len(args) % 2 == 0, f"stage {stage_id}: {args} are not name/value pairs"
    for name, value in zip(args[::2], args[1::2], strict=True):
        assert name.startswith("--") and value, (
            f"stage {stage_id}: bad pair {name} {value}"
        )
        assert re.search(rf"[.'\"\[]{re.escape(name[2:])}(?![A-Za-z0-9_])", source), (
            f"stage {stage_id}: {script.name} never reads {name}"
        )


def test_module_and_node_stages_are_covered() -> None:
    ids = {stage_id for stage_id, _, _ in _stages()}
    assert {"attest", "replica-attest"} <= ids
    assert {i for i, _, _ in _node_stages()} == {"qa-browser", "qa-interaction"}


def test_the_argv_test_rejects_an_option_whose_type_accepts_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "p.add_argument('--k', type=lambda v: 1 / 0)\np.parse_args()"
    with pytest.raises(pytest.fail.Exception):
        _check("untypable", _toy(tmp_path, body), ["--a", "x", "--k", "x"], monkeypatch)


def test_at_most_one_argument_is_a_structural_error() -> None:
    assert STRUCTURAL.search("argument --o: expected at most one argument")
