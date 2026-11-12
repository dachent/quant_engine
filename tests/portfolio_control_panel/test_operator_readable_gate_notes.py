"""#895 F3: gate notes shown to the operator carry no internal machine tokens."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

from acceptance.validate import checks

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def panel(monkeypatch: pytest.MonkeyPatch) -> Any:
    name = "portfolio_control_panel_for_gate_notes"
    spec = importlib.util.spec_from_file_location(
        name, REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


# The two tokens the real positions gate note carried (value-free shapes).
REAL_NOTE = "transport WEB_CONTRACT; manual_export_partial:2026-02-14-2026-08-14"


def test_real_gate_note_tokens_become_words(panel):
    shown = panel._operator_readable(REAL_NOTE)
    assert shown == "transport web contract; manual export partial (2026-02-14-2026-08-14)"
    assert checks._MACHINE_WORD.findall(shown) == []


def test_the_note_before_the_fix_was_a_validator_finding(panel):
    # Kill case: the unconverted note is exactly what lint.internal_tokens_displayed flags.
    assert checks._MACHINE_WORD.findall(REAL_NOTE) == [
        "WEB_CONTRACT",
        "manual_export_partial:2026-02-14-2026-08-14",
    ]


@pytest.mark.parametrize("value", [None, "", "plain text", 3])
def test_other_values_are_unchanged(panel, value):
    assert panel._operator_readable(value) == value
