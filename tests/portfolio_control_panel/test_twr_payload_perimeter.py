"""Paired tests: the panel validates the manifest-bound TWR payload (C1-T3).

The report's TWR is the epoch manifest's own engine result; the panel is a
strict projection of it, so an available TWR must arrive with the epoch
observation facts it was linked over and an unavailable TWR must carry no
numeric value.  The browser renders row.twr only, with no return-path
fallback.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
SPEC = importlib.util.spec_from_file_location("portfolio_control_panel", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio control-panel builder")
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)


def _twr_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": "account-owned",
        "flow_count": 0,
        "flow_timing": None,
        "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
        "mwr_xirr": None,
        "nav_observations": 9,
        "observed_end": "2026-01-10",
        "observed_start": "2026-01-02",
        "strategy_id": "strategy-owned",
        "twr": "0.25",
        "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
    }
    row.update(overrides)
    return row


def test_available_twr_requires_its_manifest_epoch_payload() -> None:
    panel._validate_mwr_perimeter([_twr_row()])


def test_available_twr_without_observation_facts_is_rejected() -> None:
    """Paired (fail-before): the old reader accepted an incomplete payload."""

    with pytest.raises(
        panel.ControlPanelValidationError, match="available TWR payload"
    ):
        panel._validate_mwr_perimeter([_twr_row(nav_observations=None)])


def test_available_twr_without_an_observed_interval_is_rejected() -> None:
    with pytest.raises(
        panel.ControlPanelValidationError, match="available TWR payload"
    ):
        panel._validate_mwr_perimeter([_twr_row(observed_start=None)])


def test_unavailable_twr_must_carry_no_numeric_value() -> None:
    with pytest.raises(
        panel.ControlPanelValidationError, match="unavailable TWR must carry no value"
    ):
        panel._validate_mwr_perimeter([_twr_row(twr_status="UNAVAILABLE", twr="0.25")])


def test_blocked_twr_row_is_a_clean_withheld_row() -> None:
    panel._validate_mwr_perimeter([_twr_row(twr_status="BLOCKED_ADMISSION", twr=None)])


def test_browser_performance_rows_render_row_twr_without_return_path_fallback() -> None:
    """Paired (fail-before): the old renderer fell back to return_path values."""

    source = (
        REPOSITORY_ROOT / "apps" / "portfolio-control-panel" / "app.js"
    ).read_text(encoding="utf-8")
    rendering = source[
        source.index("const acceptedReturn") : source.index("const performanceHeaders")
    ]
    assert "const acceptedReturn = row.twr;" in rendering
    assert "row.twr_status" in rendering
    assert "return_path" not in rendering
