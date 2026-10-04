"""Stress callouts require a proven breach of the governed HWM limit."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("shell_harness.mjs")
APP = ROOT / "apps" / "portfolio-control-panel" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")


@pytest.mark.parametrize(
    ("advisory", "breach"),
    [
        ({"correlation_one_stress": "0.10", "hwm_limit": "0.15"}, False),
        ({"correlation_one_stress": "0.15", "hwm_limit": "0.15"}, False),
        ({"correlation_one_stress": "0.20", "hwm_limit": "0.15"}, True),
        ({"hwm_limit": "0.15"}, False),
        ({"correlation_one_stress": None, "hwm_limit": "0.15"}, False),
        ({"correlation_one_stress": "unknown", "hwm_limit": "0.15"}, False),
        ({"correlation_one_stress": "0.20"}, False),
        ({"correlation_one_stress": "0.20", "hwm_limit": None}, False),
        ({"correlation_one_stress": "0.20", "hwm_limit": "unknown"}, False),
        ({"correlation_one_stress": "Infinity", "hwm_limit": "0.15"}, False),
        ({"correlation_one_stress": "0.20", "hwm_limit": "Infinity"}, False),
    ],
)
def test_stress_breach_callout_requires_finite_values_above_governed_limit(
    tmp_path: Path, advisory: dict[str, str | None], breach: bool
) -> None:
    payload = {
        "advisory": advisory,
        "counts": {"twr_available": 0, "twr_rows": 0, "mwr_available": 0},
        "hwm_summary": {"uncovered_value_usd": "0", "uncovered_details": []},
        "meta": {"performance_lineage": {"status": "AVAILABLE"}},
        "performance": [],
        "strategies": [],
        "weekly_returns": {"series": [], "weeks": []},
    }
    payload_path = tmp_path / "synthetic-panel.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        [str(shutil.which("node")), str(HARNESS), str(APP), str(payload_path), "renderRisk"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["errors"] == []
    stress_bar = result["dom"]["#stress-bar"]["innerHTML"]
    assert ("Stress exceeds" in stress_bar) is breach
    assert ("callout-danger" in stress_bar) is breach
    if breach:
        assert "15.0% HWM boundary" in stress_bar
