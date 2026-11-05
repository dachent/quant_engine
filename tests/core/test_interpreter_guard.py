"""The interpreter guard: pin-feeding numbers are computed only on the Python they are reproducible on."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core import interpreter

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

OTHER = (3, 13, 1)


def test_the_required_interpreter_is_accepted_and_any_other_refused_by_name() -> None:
    interpreter.require((3, 11, 15), what="the thing")
    interpreter.require((3, 11, 0, "final", 0), what="the thing")
    for other in ((3, 12, 0), (3, 13, 14), (3, 10, 9)):
        with pytest.raises(interpreter.InterpreterRefusal) as caught:
            interpreter.require(
                other, what="the thing", executable="C:/venv/python.exe"
            )
        message = str(caught.value)
        assert "the thing" in message and "Python 3.11" in message
        assert ".".join(str(p) for p in other) in message  # the interpreter found
        assert "C:/venv/python.exe" in message


def test_the_running_interpreter_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 11, 9, "final", 0))
    interpreter.require_current("x")
    monkeypatch.setattr(sys, "version_info", (*OTHER, "final", 0))
    with pytest.raises(interpreter.InterpreterRefusal, match="found Python 3.13.1"):
        interpreter.require_current("x")


def _argv(parser: argparse.ArgumentParser, command: str | None) -> list[str]:
    """The smallest command line the parser accepts: required options with a dummy value."""

    args: list[str] = []
    if command is not None:
        sub = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        parser = sub.choices[command]
        args.append(command)
    for action in parser._actions:
        if action.required and action.option_strings:
            value = "1" if action.type is int else "x"
            if action.choices:
                value = str(next(iter(action.choices)))
            args += [action.option_strings[0], value]
    return args


@pytest.mark.parametrize(
    ("module", "command"),
    [
        ("portfolio_implementation_reconciliation", None),
        ("portfolio_implementation_return_attribution", None),
        ("portfolio_implementation_sleeve_basis", None),
        ("portfolio_release_builder", "build"),
        ("tt_source_manifest_builder", "build"),
        ("tt_source_manifest_builder", "verify"),
    ],
)
def test_every_tool_that_computes_pinned_numbers_refuses_another_interpreter(
    module: str,
    command: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool: Any = importlib.import_module(module)
    argv = _argv(tool._parser(), command)
    monkeypatch.setattr(sys, "version_info", (*OTHER, "final", 0))
    assert tool.main(argv) == 2
    err = capsys.readouterr().err
    assert "Python 3.11" in err and "found Python 3.13.1" in err


def test_a_run_of_the_attended_pipeline_records_only_the_required_interpreter() -> None:
    import portfolio_pipeline_materialize as materialize

    python = Path(sys.executable)
    root = TOOLS.parent
    if sys.version_info[:2] == (3, 11):
        identity = materialize._runtime_identity(python, root)
        assert identity["version"][:2] == [3, 11]
    else:
        with pytest.raises(materialize.MaterializationError, match="Python 3.11"):
            materialize._runtime_identity(python, root)
