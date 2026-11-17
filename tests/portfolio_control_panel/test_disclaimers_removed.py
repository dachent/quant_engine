"""The panel shows data status and dates, not policy slogans; the payload keeps its boundary fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_shell_qc1_labels import _capital_payload, _cost_strategy, _run

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PANEL = REPOSITORY_ROOT / "apps" / "portfolio-control-panel"
INDEX = (PANEL / "index.html").read_text(encoding="utf-8")
APP = (PANEL / "app.js").read_text(encoding="utf-8")
SCHEMA = (
    REPOSITORY_ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json"
).read_text(encoding="utf-8")

REMOVED = (
    "Execution authority",
    "Fail closed",
    "No live market feed, optimizer, or automated drift service is implied",
    "Mandate firewall",
    "Reserve risk budget: $0",
    "No brokerage connection, order path, or trade authorization",
    "Current evidence supports monitoring and reconciliation",
    "denied readiness or execution authority",
    "Allocation / execution authority NONE",
    "Read-only advisory surface",
    "advisoryExecutionEffect",
    "Automated action",
    "automatic drop, readiness, and order authority are policy-fixed",
)


@pytest.mark.parametrize("text", REMOVED)
def test_the_slogan_is_not_in_the_shipped_panel(text: str) -> None:
    assert text not in INDEX
    assert text not in APP


def test_no_advisory_only_badge_is_left() -> None:
    assert "Advisory only</span>" not in INDEX


def test_the_payload_boundary_fields_stay_in_the_schema() -> None:
    schema = json.loads(SCHEMA)
    underwriting = schema["properties"]["strategy_underwriting"]

    assert "authority_boundary" in underwriting["required"]
    assert "allocation_authority" in SCHEMA and "execution_authority" in SCHEMA


def test_the_command_banner_shows_the_data_date_not_a_readiness_slogan(
    tmp_path: Path,
) -> None:
    payload = _capital_payload(_cost_strategy())
    payload["advisory"] = {
        **payload.get("advisory", {}),
        "readiness": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
        "reason": "r",
    }
    payload["meta"] = {**payload.get("meta", {}), "portfolio_as_of": "2026-11-06"}
    dom = _run(tmp_path, payload, "renderCommand")
    heading = str(dom["#command-heading"].get("textContent", ""))

    assert heading == "Data as of Nov 6, 2026"
    assert "EXPAND" not in heading.upper()
