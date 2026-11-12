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
        ("portfolio_release_builder", "stage"),
        ("tt_source_manifest_builder", "build"),
        ("tt_source_manifest_builder", "verify"),
        ("tt_source_manifest_builder", "read-compat"),
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


@pytest.mark.parametrize(
    ("module", "command"),
    [
        ("portfolio_attribution", "stage"),
        ("portfolio_attribution", "publish"),
        ("portfolio_correlation", "build"),
        ("portfolio_correlation", "stage"),
        ("portfolio_correlation", "publish"),
        ("portfolio_decomposition", "build"),
        ("portfolio_decomposition", "stage"),
        ("portfolio_decomposition", "publish"),
        ("portfolio_diligence_rubric", "build"),
        ("portfolio_diligence_rubric", "verify"),
        ("portfolio_driver_map", "build"),
        ("portfolio_driver_map", "verify"),
        ("portfolio_grizzly_signal_replay", None),
        ("portfolio_replication", "build"),
        ("portfolio_replication", "verify"),
        ("portfolio_replication", "publish"),
        ("portfolio_report", "build"),
        ("portfolio_report", "verify"),
        ("portfolio_report", "canonicalize"),
        ("portfolio_report", "canonicalize-xlsx-result"),
        ("portfolio_report", "bind-workbook-evidence"),
        ("portfolio_report", "publish"),
        ("portfolio_strategy_history", None),
        ("portfolio_target_series", None),
        ("portfolio_control_panel", "build"),
        ("portfolio_control_panel", "verify"),
        ("portfolio_control_panel", "publish"),
        ("portfolio_control_panel", "qa"),
        ("portfolio_control_panel", "qa-inventory"),
        ("portfolio_control_panel", "launcher-qa"),
        ("provider_ledger", "stage"),
        ("provider_ledger", "verify"),
    ],
)
def test_every_other_numeric_entry_point_refuses_another_interpreter(
    module: str,
    command: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool: Any = importlib.import_module(module)
    argv = _argv(tool._parser(), command)
    monkeypatch.setattr(sys, "version_info", (*OTHER, "final", 0))
    monkeypatch.setattr(sys, "argv", [module, *argv])
    code = (
        tool.main(argv) if "argv" in tool.main.__code__.co_varnames[:1] else tool.main()
    )
    assert code == 2
    assert "found Python 3.13.1" in capsys.readouterr().err


# tools that handle floats but reach no pin, with the reason; every other tool that does arithmetic on
# floats must call ``check_entry_interpreter`` (or be a guarded tool above)
EXCUSED = {
    "capture_quantbase_har": "records a browser capture; no figure is derived",
    "codex_scope_guard": "scope checks on diffs",
    "issue_guard": "issue bookkeeping",
    "panel_payload_size": "measures bytes of a built panel",
    "quantbase_cdp_har_recorder": "records a browser capture",
    "quantbase_generation": "publishes vendor bytes verbatim",
    "quantbase_har_to_csv": "converts captured text, no arithmetic on figures",
    "yahoo_fetch_driver": "fetches vendor bytes",
    "release_driver": "orchestrates; its python stages are checked by required_python",
    "release_measurements": "wall-clock and size measurements of the run",
    "release_canary": "filesystem timing and environment probe of the commit path; derives no figure",
    "verify_sleeve_ledger": "re-reads a ledger and compares digests",
}
GUARDED_MARKERS = (
    "check_entry_interpreter",
    "require_current_interpreter",
    "require_interpreter",
)
NUMERIC = ("import math", "import numpy", "import pandas", "float(")


def test_every_tool_that_computes_figures_is_guarded_or_excused() -> None:
    unguarded = []
    for path in sorted(TOOLS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "def main(" not in text or not any(n in text for n in NUMERIC):
            continue
        if path.stem in EXCUSED or any(m in text for m in GUARDED_MARKERS):
            continue
        unguarded.append(path.stem)
    assert not unguarded, (
        f"guard these entry points or excuse them with a reason: {unguarded}"
    )
    assert all(reason for reason in EXCUSED.values())
