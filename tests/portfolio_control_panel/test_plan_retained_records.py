"""The decomposition plan's retained history-only records are read through one rule (#567).

The plan names the contract's retained records beside the product strategies.  Every
panel check of the plan against the product perimeter must drop exactly the contract's
EXCLUDED and DEPRECATED_HISTORY_ONLY strategies, through ``_plan_without_retained_records``
and nothing else.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import portfolio_control_panel as panel

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "contracts" / "portfolio-financial-mvp-source-v1.json"
PLAN = REPO / "docs" / "analytics" / "current-strategy-decomposition-plan.json"


def _plan_mapping() -> dict[str, str]:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    return {item["strategy_id"]: item["series_id"] for item in plan["strategies"]}


def test_retained_records_leave_the_plan_mapping_and_the_product_stays() -> None:
    mapping = _plan_mapping()
    product = panel._plan_without_retained_records(mapping, CONTRACT.read_bytes())
    retained = set(mapping) - set(product)
    assert retained, "the committed plan carries retained history-only records"
    assert set(product) == set(panel.CURRENT_PRODUCT_STRATEGY_IDS)
    controls = panel._financial_mvp_product_controls(CONTRACT.read_bytes())
    assert retained == set(panel._history_only_strategy_ids(controls))
    assert all(product[key] == mapping[key] for key in product)


def test_a_plan_without_retained_records_is_unchanged_and_default_contract_is_used() -> (
    None
):
    only_product = {
        strategy_id: strategy_id for strategy_id in panel.CURRENT_PRODUCT_STRATEGY_IDS
    }
    assert panel._plan_without_retained_records(only_product, None) == only_product


def test_an_unknown_strategy_is_never_dropped_by_name() -> None:
    mapping = _plan_mapping()
    mapping["not-a-contract-strategy [retained history-only record]"] = "x"
    product = panel._plan_without_retained_records(mapping, CONTRACT.read_bytes())
    assert "not-a-contract-strategy [retained history-only record]" in product


def test_both_panel_checks_read_the_plan_through_the_one_helper() -> None:
    tree = ast.parse(
        (REPO / "tools" / "portfolio_control_panel.py").read_text(encoding="utf-8")
    )
    users = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        and any(
            isinstance(node, ast.Name) and node.id == "_plan_without_retained_records"
            for node in ast.walk(function)
        )
    }
    # the lens plan validation (D10) and the weekly/correlation identity mapping (D11)
    assert {"_decomposition_lenses_for_mode", "_rendered_files"} <= users
    # no other place derives the retained ids from the plan: the helper is the only
    # reader of the contract's history-only rule for a plan
    assert users == {
        "_decomposition_lenses_for_mode",
        "_rendered_files",
    }
