"""The panel consumes the report's current capital (cluster 2, T2: B4, B6, B13, M3).

The panel ``capital`` block keeps decimal-or-null fields, adds ``retained_excluded``,
and carries ``capital_status`` and ``capital_as_of`` siblings.  NOT_READY components
are null in the payload, never painted, and the verifier no longer compares the
report's capital rows with themselves: it recomputes the capital identity from the
panel's own strategy rows and reconciles the block with the governed baseline.
"""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


panel = _load(
    "portfolio_control_panel_current_capital",
    REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py",
)
fx = _load(
    "current_capital_fixture_panel",
    REPOSITORY_ROOT / "tests" / "portfolio_report" / "current_capital_fixture.py",
)


def _cr2_d1_status_schema() -> dict[str, Any]:
    """Exercise the checked-in root schema's versioned status conditions."""

    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    schema["required"] = ["panel_schema_version", "invested_capital"]
    schema["additionalProperties"] = False
    schema["properties"] = {
        "panel_schema_version": schema["properties"]["panel_schema_version"],
        "invested_capital": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "policy_readiness_provenance"],
            "properties": {
                "status": {"enum": ["AVAILABLE", "REQUIRED_NOT_READY"]},
                "policy_readiness_provenance": {"type": "string"},
            },
        },
    }
    return schema


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        (
            {
                "panel_schema_version": 21,
                "invested_capital": {
                    "status": "AVAILABLE",
                    "policy_readiness_provenance": "LEGACY",
                },
            },
            True,
        ),
        (
            {
                "panel_schema_version": 21,
                "invested_capital": {
                    "status": "REQUIRED_NOT_READY",
                    "policy_readiness_provenance": "REQUIRED_NOT_READY",
                },
            },
            False,
        ),
        (
            {
                "panel_schema_version": 22,
                "invested_capital": {
                    "status": "AVAILABLE",
                    "policy_readiness_provenance": "READY",
                },
            },
            True,
        ),
        (
            {
                "panel_schema_version": 22,
                "invested_capital": {
                    "status": "REQUIRED_NOT_READY",
                    "policy_readiness_provenance": "REQUIRED_NOT_READY",
                },
            },
            True,
        ),
        (
            {
                "panel_schema_version": 22,
                "invested_capital": {
                    "status": "AVAILABLE",
                    "policy_readiness_provenance": "REQUIRED_NOT_READY",
                },
            },
            False,
        ),
        (
            {
                "panel_schema_version": 22,
                "invested_capital": {
                    "status": "REQUIRED_NOT_READY",
                    "policy_readiness_provenance": "READY",
                },
            },
            False,
        ),
    ],
    ids=[
        "v21-legacy",
        "v21-blocked",
        "v22-ready",
        "v22-blocked",
        "v22-blocked-mismatch",
        "v22-ready-mismatch",
    ],
)
def test_cr2_d1_panel_schema_versions_preserve_legacy_and_bind_status(
    payload: dict[str, Any], valid: bool
) -> None:
    errors = list(Draft202012Validator(_cr2_d1_status_schema()).iter_errors(payload))

    assert bool(errors) is not valid


def _row(
    metric: str,
    value: str | None,
    *,
    as_of: str | None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "as_of_date": as_of,
        "metric": metric,
        "status": status
        or ("AVAILABLE" if value is not None else "NOT_READY_AT_CUTOFF"),
        "unit": "USD",
        "value": value,
    }


def _summary(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = {
        "Total liquid": _row("Total liquid", None, as_of=None),
        "Systematic capital": _row("Systematic capital", "500", as_of="2026-01-02"),
        "Reserve": _row("Reserve", None, as_of=None),
        "Retained excluded systematic balance": _row(
            "Retained excluded systematic balance", "0.19", as_of="2025-11-24"
        ),
    }
    rows.update(overrides)
    return rows


def _report(summary: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "account_summary": [{"account_id": "account-00"}],
        "allocation_history": [{"allocation_id": "allocation-0"}],
        "portfolio_summary": list(summary.values()),
        "strategy_summary": [{"strategy_id": "s-alpha"}],
    }


# ---- the capital block, its statuses and its dates ----------------------------


def test_capital_block_is_decimal_or_null_with_status_and_as_of_siblings() -> None:
    parts = panel._capital_summary(_summary())

    assert parts["capital"] == {
        "reserve": None,
        "retained_excluded": "0.19",
        "systematic": "500",
        "total_liquid": None,
    }
    assert parts["capital_status"] == {
        "reserve": "NOT_READY_AT_CUTOFF",
        "retained_excluded": "AVAILABLE",
        "systematic": "AVAILABLE",
        "total_liquid": "NOT_READY_AT_CUTOFF",
    }
    assert parts["capital_as_of"] == {
        "reserve": None,
        "retained_excluded": "2025-11-24",
        "systematic": "2026-01-02",
        "total_liquid": None,
    }


def test_all_current_components_publish_values_and_a_total() -> None:
    parts = panel._capital_summary(
        _summary(
            **{
                "Reserve": _row("Reserve", "1000", as_of="2026-01-06"),
                "Total liquid": _row("Total liquid", "1500.19", as_of="2025-11-24"),
            }
        )
    )

    assert parts["capital"]["total_liquid"] == "1500.19"
    assert set(parts["capital_status"].values()) == {"AVAILABLE"}


def test_expectations_carry_the_block_and_the_counts() -> None:
    expectations = panel._report_panel_expectations(_report(_summary()))

    assert expectations["capital"]["retained_excluded"] == "0.19"
    assert expectations["capital_status"]["reserve"] == "NOT_READY_AT_CUTOFF"
    assert expectations["capital_as_of"]["systematic"] == "2026-01-02"
    assert expectations["counts"] == {"accounts": 1, "allocations": 1, "strategies": 1}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"Reserve": _row("Reserve", None, as_of=None, status="AVAILABLE")},
            "capital status contradicts its value",
        ),
        (
            {
                "Reserve": _row(
                    "Reserve", "5", as_of="2026-01-06", status="NOT_READY_AT_CUTOFF"
                )
            },
            "capital status contradicts its value",
        ),
        (
            {"Reserve": _row("Reserve", None, as_of=None, status="STALE")},
            "capital status is invalid",
        ),
    ],
    ids=["available-without-value", "not-ready-with-value", "unknown-status"],
)
def test_capital_status_must_agree_with_its_value(
    overrides: dict[str, dict[str, Any]], message: str
) -> None:
    with pytest.raises(panel.ControlPanelValidationError, match=message):
        panel._capital_summary(_summary(**overrides))


def test_the_retained_excluded_row_is_required() -> None:
    summary = _summary()
    del summary["Retained excluded systematic balance"]
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="portfolio summary lacks Retained excluded systematic balance",
    ):
        panel._capital_summary(summary)


# ---- the identity is recomputed from the panel's own rows ----------------------


def _data(**capital: Any) -> dict[str, Any]:
    base = {
        "reserve": "1000",
        "retained_excluded": "0.19",
        "systematic": "500",
        "total_liquid": "1500.19",
    }
    status = {key: "AVAILABLE" for key in base}
    return {
        "capital": {**base, **capital},
        "capital_as_of": dict.fromkeys(base, "2026-01-06"),
        "capital_status": status,
        "strategies": [
            {"current_value_usd": "150", "strategy_id": "s-alpha"},
            {"current_value_usd": "200", "strategy_id": "s-beta"},
            {"current_value_usd": "150", "strategy_id": "s-gamma"},
        ],
    }


def test_a_sound_capital_block_has_no_identity_problems() -> None:
    assert panel._capital_identity_problems(_data()) == []

    not_ready = _data(reserve=None, total_liquid=None)
    not_ready["capital_status"]["reserve"] = "NOT_READY_AT_CUTOFF"
    not_ready["capital_status"]["total_liquid"] = "NOT_READY_AT_CUTOFF"
    not_ready["capital_as_of"]["reserve"] = None
    not_ready["capital_as_of"]["total_liquid"] = None
    assert panel._capital_identity_problems(not_ready) == []


def test_systematic_must_equal_the_sum_of_the_strategy_rows() -> None:
    problems = panel._capital_identity_problems(_data(systematic="499"))

    assert any("systematic" in problem for problem in problems)


def test_total_liquid_must_include_the_retained_excluded_balance() -> None:
    # 500 + 1000 = 1500: the identity without the retained 0.19 is not the identity.
    problems = panel._capital_identity_problems(_data(total_liquid="1500"))

    assert any("total liquid" in problem for problem in problems)


def test_total_liquid_is_not_painted_while_a_component_is_not_ready() -> None:
    painted = _data(reserve=None)
    painted["capital_status"]["reserve"] = "NOT_READY_AT_CUTOFF"
    painted["capital_as_of"]["reserve"] = None

    problems = panel._capital_identity_problems(painted)

    assert any("not ready component" in problem for problem in problems)


def test_a_status_that_contradicts_its_value_is_an_identity_problem() -> None:
    data = _data()
    data["capital_status"]["systematic"] = "NOT_READY_AT_CUTOFF"

    problems = panel._capital_identity_problems(data)

    assert any("status" in problem for problem in problems)


# ---- the report block is reconciled with the governed baseline ----------------


def _baseline_capital() -> dict[str, Any]:
    block = fx.block(
        [
            fx.strategy("s-alpha", [fx.member("a", "150", "2026-01-06")]),
            fx.strategy("s-beta", [fx.member("b", "200", "2026-01-06")]),
            fx.strategy("s-gamma", [fx.member("g", "150", "2026-01-02")]),
            fx.strategy(
                "s-retired",
                [fx.member("r", "0.19", "2025-11-24")],
                scope="RETAINED_EXCLUDED",
            ),
        ],
        valuation_date="2026-01-06",
    )
    return block


def test_the_report_capital_must_equal_the_governed_baseline_block() -> None:
    block = _baseline_capital()
    parts = panel._capital_summary(_summary())

    panel._reconcile_capital_with_baseline(parts, {"current_capital": block})

    stale = deepcopy(parts)
    stale["capital"]["systematic"] = "445477.45"
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="report capital differs from the governed baseline current capital",
    ):
        panel._reconcile_capital_with_baseline(stale, {"current_capital": block})

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="baseline catalog lacks current_capital",
    ):
        panel._reconcile_capital_with_baseline(parts, {})


def test_keys_the_panel_does_not_know_are_ignored_in_the_report_and_the_block() -> None:
    """Lane T1b adds a strategy cash line and reserve rows to the baseline block."""

    block = _baseline_capital()
    extended = deepcopy(block)
    extended["strategy_cash_lines"] = [{"strategy_id": "s-alpha", "cash": "1"}]
    for name in ("systematic", "reserve", "retained_excluded", "total_liquid"):
        extended[name]["future_field"] = {"anything": True}
    for row in extended["strategies"]:
        row["cash_line"] = {"status": "AVAILABLE", "value": "5"}
        for member in row["members"]:
            member["future_field"] = 1
    summary = _summary()
    for row in summary.values():
        row["future_field"] = "not read"

    parts = panel._capital_summary(summary)

    assert parts == panel._capital_summary(_summary())
    panel._reconcile_capital_with_baseline(parts, {"current_capital": extended})


def _uncovered_hwm_summary() -> dict[str, Any]:
    return {
        "excluded_details": [],
        "excluded_strategies": [],
        "uncovered_details": [
            {
                "current_value_usd": "100",
                "strategy_id": "s-alpha",
                "strategy_name": "S Alpha",
                "systematic_weight": "0.12",
            }
        ],
        "uncovered_strategies": ["S Alpha"],
        "uncovered_value_usd": "100",
    }


@pytest.mark.parametrize("has_exception_row", [True, False], ids=["row", "appended"])
def test_the_hwm_exception_coverage_row_says_it_shows_current_cutoff_weights(
    has_exception_row: bool,
) -> None:
    """Its uncovered details repeat the current weights of ``strategy_summary``."""

    coverage: list[dict[str, Any]] = [
        {"category": "FACT_COVERAGE", "item": "nav", "status": "PARTIAL"}
    ]
    if has_exception_row:
        coverage.append(
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": "full-systematic-hwm-unavailable",
            }
        )

    rows = panel._reconcile_hwm_coverage(coverage, _uncovered_hwm_summary())

    exception = next(
        row for row in rows if row.get("item") == "full-systematic-hwm-unavailable"
    )
    assert exception["uncovered_details"][0]["systematic_weight"] == "0.12"
    assert exception["weight_basis"] == "CURRENT_CUTOFF"
    assert fx.unlabeled_weight_paths(rows) == []
