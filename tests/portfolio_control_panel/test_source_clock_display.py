from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "source_clock_panel", ROOT / "tools" / "portfolio_control_panel.py"
)
assert SPEC is not None and SPEC.loader is not None
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)


def _clock() -> dict[str, object]:
    return {
        "selected_financial_cutoff": "2026-09-17",
        "latest_common_eod": "2026-09-18",
        "limiting_inputs": ["matrixport", "quantbase"],
        "source_data_as_of": [
            {
                "source_id": "ibkr",
                "applies_to_latest_common_eod": False,
                "provider_data_date": "2026-09-18",
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "native_observed_at": "2026-09-18T20:15:00+00:00",
                "timezone": "America/Chicago",
                "precision": "SECOND",
            },
            {
                "source_id": "matrixport",
                "applies_to_latest_common_eod": True,
                "provider_data_date": "2026-09-18",
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "timezone": "UTC",
                "precision": "DATE",
            },
            {
                "source_id": "quantbase",
                "applies_to_latest_common_eod": True,
                "provider_data_date": "2026-09-18",
                "finality": {
                    "basis": "NATIVE_LEDGER",
                    "provider_finality_guarantee": True,
                },
                "timezone": "UTC",
                "precision": "DATE",
            },
        ],
    }


def test_panel_clock_display_keeps_selected_and_common_dates_distinct_without_midnight() -> (
    None
):
    """The existing Data view must render the accepted clock rather than inventing times."""

    lines = panel._source_clock_display_lines(_clock())

    assert lines[0] == (
        "Source clock — selected financial cutoff 2026-09-17; latest common "
        "finalized EOD 2026-09-18; limiting inputs matrixport, quantbase."
    )
    matrixport = next(
        line for line in lines if line.startswith("Source clock — matrixport:")
    )
    assert "data as-of 2026-09-18" in matrixport
    assert "T00:00:00" not in matrixport
    assert any("Actual execution timestamp is not declared" in line for line in lines)


def test_panel_clock_display_rejects_a_tampered_common_eod() -> None:
    """Changing a panel clock declaration must fail its consumer semantic check."""

    clock = _clock()
    clock["latest_common_eod"] = "2026-09-17"

    with pytest.raises(panel.ControlPanelValidationError, match="latest common EOD"):
        panel._source_clock_display_lines(clock)


def test_panel_clock_display_preserves_a_full_native_observation() -> None:
    """A stated source timestamp, timezone, and precision reach the display intact."""

    lines = panel._source_clock_display_lines(_clock())
    ibkr = next(line for line in lines if "ibkr" in line)

    assert "native observation 2026-09-18T20:15:00+00:00" in ibkr
    assert "native timezone America/Chicago" in ibkr
    assert "native precision SECOND" in ibkr


def test_panel_clock_display_rejects_missing_precision_for_a_timed_observation() -> (
    None
):
    """A timestamp cannot be recast as date-only by dropping its precision."""

    clock = _clock()
    source_rows = clock["source_data_as_of"]
    assert isinstance(source_rows, list)
    timed_row = source_rows[0]
    assert isinstance(timed_row, dict)
    timed_row.pop("precision")

    with pytest.raises(panel.ControlPanelValidationError, match="native precision"):
        panel._source_clock_display_lines(clock)
