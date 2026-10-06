"""Account roles reach the leverage and fact-scope tables, not only the payload.

D6: the report labels every account row of ``leverage_disclosures`` and
``fact_coverage_matrix`` with an ``account_role`` (PRODUCT, RESERVE or
HISTORY_ONLY_NO_CURRENT_ASSIGNMENT) so a history-only account cannot pass for a
current one.  The rows below come from the report producer's own labeller and are
rendered by the real ``app.js`` functions; the role must reach the DOM.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import portfolio_report as report
import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("shell_harness.mjs")
APP = ROOT / "apps" / "portfolio-control-panel" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")


def _leverage_row(account_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "provider_id": "synthetic",
        "strategy_id": None,
        "data_class": "LEVERAGE_EXPOSURE",
        "coverage_status": "OBSERVED_ONLY",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
    }


def _labelled() -> list[dict[str, Any]]:
    return report._label_account_roles(
        [
            _leverage_row("prod-1"),
            _leverage_row("res-1"),
            _leverage_row("hist-1"),
        ],
        product_account_ids={"prod-1"},
        reserve_account_ids={"res-1"},
        excluded_account_ids=set(),
    )


def _render(tmp_path: Path) -> dict[str, str]:
    rows = _labelled()
    payload = {
        "meta": {"performance_lineage": {"status": "PASS"}},
        "strategies": [],
        "account_summary": [],
        "leverage_disclosures": rows,
        "fact_coverage_matrix": [
            {**row, "fact_type": "nav", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"}
            for row in rows
        ],
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        [
            str(shutil.which("node")),
            str(HARNESS),
            str(APP),
            str(path),
            "renderLeverageDisclosures,renderFactScopeTables",
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
    return {
        selector: result["dom"][selector]["innerHTML"]
        for selector in ("#leverage-table", "#fact-coverage-matrix-table")
    }


def _row_html(table: str, account_id: str) -> str:
    rows = [part for part in table.split("<tr") if account_id in part]
    assert len(rows) == 1, (account_id, len(rows))
    return rows[0]


@pytest.mark.parametrize("selector", ["#leverage-table", "#fact-coverage-matrix-table"])
def test_the_producers_account_roles_reach_both_tables(
    tmp_path: Path, selector: str
) -> None:
    table = _render(tmp_path)[selector]
    history = _row_html(table, "hist-1")
    assert 'data-account-role="HISTORY_ONLY_NO_CURRENT_ASSIGNMENT"' in history
    assert "History only" in history
    reserve = _row_html(table, "res-1")
    assert 'data-account-role="RESERVE"' in reserve
    assert "Reserve" in reserve


@pytest.mark.parametrize("selector", ["#leverage-table", "#fact-coverage-matrix-table"])
def test_a_product_account_carries_no_role_note(
    tmp_path: Path, selector: str
) -> None:
    table = _render(tmp_path)[selector]
    assert "data-account-role" not in _row_html(table, "prod-1")
