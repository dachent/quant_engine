"""The panel prints a restated row's disclosure as the report states it (#624, option B).

The payload slice comes from the real report builder over the derived-sleeve fixture; the
real ``app.js`` function renders it in the shell harness's stub DOM.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from test_returns_engine_consumption import _full_report_fixture
from test_strategy_basis_disclosure_report import _account, _on

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY_ROOT / "tests" / "portfolio_control_panel" / "shell_harness.mjs"
APP = REPOSITORY_ROOT / "apps" / "portfolio-control-panel" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")

SELECTORS = (
    "#strategy-basis-disclosure",
    "#strategy-basis-statement",
    "#strategy-basis-lines-table",
    "#account-basis-table",
    "#strategy-basis-account-bridge-table",
    "#strategy-basis-audit-bridge-table",
)


def _render(
    tmp_path: Path,
    payload: dict[str, Any],
    function: str = "renderStrategyBasisDisclosure",
    host: str = "#strategy-basis-disclosure",
) -> tuple[dict[str, Any], Any]:
    """The recorded DOM and whether the block's host ended up hidden."""

    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    calls = tmp_path / "calls.json"
    calls.write_text(
        json.dumps([{"eval": f"document.querySelector('{host}').hidden"}]),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(shutil.which("node")),
            str(HARNESS),
            str(APP),
            str(path),
            function,
            str(calls),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["errors"] == []
    dom: dict[str, Any] = result["dom"]
    return dom, result["returns"][0]["value"]


def test_the_block_reaches_the_dom_as_the_report_states_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "report").mkdir()
    report = _on(_full_report_fixture(), tmp_path / "report", monkeypatch)
    dom, hidden = _render(
        tmp_path,
        {
            "account_summary": report["account_summary"],
            "strategy_basis_disclosure": report["strategy_basis_disclosure"],
        },
    )
    assert hidden is False
    statement = dom["#strategy-basis-statement"]["innerHTML"]
    block = report["strategy_basis_disclosure"]["strategies"][0]
    # the report's own statement is printed verbatim (escaped), not rebuilt
    assert block["margin_liability"]["statement"].replace("'", "&#39;") in statement
    lines = dom["#strategy-basis-lines-table"]["innerHTML"]
    assert "Gross sleeve NAV" in lines
    assert "Unallocated cash and margin bucket" in lines
    assert "Net equivalent" in lines
    assert "Holds" in lines
    account = dom["#account-basis-table"]["innerHTML"]
    basis = _account(report)["account_basis"]
    assert basis["label"] in account
    assert "Cumulative return" in account
    assert "Weight on the account perimeter" in account
    bridges = (
        dom["#strategy-basis-account-bridge-table"]["innerHTML"]
        + dom["#strategy-basis-audit-bridge-table"]["innerHTML"]
    )
    for step in block["bridges"]["sleeve_to_audit"]["steps"]:
        assert step["description"] in bridges
    assert "Residual after the three steps" in bridges
    assert "holds no NAV" in bridges
    weights = dom["#strategy-basis-weights-table"]["innerHTML"]
    assert "Weight before" in weights and "restated sleeve" in weights
    assert (
        block["weight_restatement"]["statement"].replace("'", "&#39;")
        in dom["#strategy-basis-weights-note"]["innerHTML"]
    )


def test_a_payload_without_the_block_shows_nothing(tmp_path: Path) -> None:
    dom, hidden = _render(tmp_path, {"account_summary": []})
    assert hidden is True
    for selector in SELECTORS[1:]:
        assert not dom.get(selector, {}).get("innerHTML")


def _sensitivity() -> dict[str, Any]:
    return {
        "classification": "OWNER_APPROVED_POLICY_CONSTANT_NOT_RESTATED",
        "constants": ["allocation_weight", "conservative_loss_fraction"],
        "policy_id": "PORT-DEPENDENCE-1",
        "restated_strategies": ["Strategy One"],
        "policy": {
            "allocation_weight": {"Strategy One": "0.3", "Strategy Two": "0.7"},
            "central_case_loss": "0.12",
            "correlation_one_loss": "0.2",
        },
        "sensitivity": {
            "basis": "RESTATED_CURRENT_CAPITAL_WEIGHT_NORMALIZED_WITHIN_DEPENDENCE_PERIMETER",
            "allocation_weight": {"Strategy One": "0.4", "Strategy Two": "0.6"},
            "central_case_loss": "0.13",
            "correlation_one_loss": "0.22",
            "central_case_loss_difference": "0.01",
            "correlation_one_loss_difference": "0.02",
        },
        "statement": "owner-approved policy constants (PORT-DEPENDENCE-1), not restated.",
    }


def test_the_policy_constant_sensitivity_is_printed_for_each_mode(
    tmp_path: Path,
) -> None:
    payload = {
        "dependence_modes": {
            "LIVE_ONLY": {"policy_constant_sensitivity": _sensitivity()},
            "LIVE_PLUS_BACKTEST": {"policy_constant_sensitivity": _sensitivity()},
        }
    }
    dom, hidden = _render(
        tmp_path,
        payload,
        "renderPolicyConstantSensitivity",
        "#policy-constant-sensitivity",
    )
    assert hidden is False
    note = dom["#policy-constant-note"]["innerHTML"]
    assert "not restated" in note and "PORT-DEPENDENCE-1" in note
    assert "Owner approved policy constant not restated" in note
    table = dom["#policy-constant-table"]["innerHTML"]
    for mode in ("Live only", "Live plus backtest"):
        assert mode in table
    assert "Central-case loss" in table and "Conservative-bound" in table
    assert "12.0000%" in table and "13.0000%" in table and "1.0000%" in table
    assert "Strategy One weight" in table


def test_a_payload_without_the_sensitivity_hides_it(tmp_path: Path) -> None:
    for payload in ({}, {"dependence_modes": {"LIVE_ONLY": {}}}):
        _dom, hidden = _render(
            tmp_path,
            payload,
            "renderPolicyConstantSensitivity",
            "#policy-constant-sensitivity",
        )
        assert hidden is True
