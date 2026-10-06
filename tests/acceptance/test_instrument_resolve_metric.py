"""Batch V6: oracle rows the validator could not resolve although the views exist.

``weight.strategy.cutoff`` resolves from ``strategy_summary`` and the panel strategies
(``systematic_weight``), ``capital.strategy_cash.cutoff`` from the custody block's
``derived_cash`` lines, and ``capital.account.cutoff`` also from the custody members (a
retained or excluded account has no ``account_summary`` row).  An observed value that
differs from the oracle is still a regression, and a metric with no view stays NOT_READY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_generation import CUTOFF, W_ALPHA, Spec, build, run

from acceptance.validate import checks


def _custody(report: dict[str, Any]) -> None:
    report["custody_current_capital"] = {
        "strategies": [
            {
                "strategy_id": "beta",
                "as_of_date": CUTOFF,
                "members": [
                    {
                        "account_id": "RETAINED1",
                        "as_of_date": "2026-07-17",
                        "value": {"currency": "USD", "status": "AVAILABLE", "value": "0.19"},
                    }
                ],
                "derived_cash": [
                    {
                        "account_id": "CASH1",
                        "current_value": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "93.97474435567453281",
                        },
                    }
                ],
            }
        ]
    }


def _row(metric: str, entity: str, expected: str, unit: str, **extra: Any) -> dict:
    return {
        "metric_id": metric,
        "entity": entity,
        "expected": expected,
        "unit": unit,
        "as_of": CUTOFF,
        **extra,
    }


def _expected_findings(tmp_path: Path, rows: list[dict]) -> list[checks.Finding]:
    gen_dir = build(tmp_path, Spec(report=[_custody]))
    return [f for f in run(gen_dir, rows) if f.check_id.startswith("expected.")]


def test_v6_strategy_weight_resolves_from_the_published_views(tmp_path: Path) -> None:
    row = _row("weight.strategy.cutoff", "alpha", W_ALPHA, "ratio", tolerance="0.00000001")
    assert _expected_findings(tmp_path, [row]) == []


def test_v6_a_wrong_strategy_weight_is_still_a_regression(tmp_path: Path) -> None:
    row = _row("weight.strategy.cutoff", "alpha", "0.5", "ratio", tolerance="0.00000001")
    findings = _expected_findings(tmp_path, [row])
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.weight.strategy.cutoff", checks.BLOCKER_REGRESSION),
        ("expected.weight.strategy.cutoff", checks.BLOCKER_REGRESSION),
    ]


def test_v6_strategy_cash_resolves_from_the_derived_cash_lines(tmp_path: Path) -> None:
    row = _row("capital.strategy_cash.cutoff", "beta", "93.97", "USD", tolerance="0.01")
    assert _expected_findings(tmp_path, [row]) == []


def test_v6_a_wrong_strategy_cash_is_still_a_regression(tmp_path: Path) -> None:
    row = _row("capital.strategy_cash.cutoff", "beta", "100.00", "USD", tolerance="0.01")
    findings = _expected_findings(tmp_path, [row])
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.capital.strategy_cash.cutoff", checks.BLOCKER_REGRESSION)
    ]


def test_v6_a_strategy_without_a_cash_line_stays_not_ready(tmp_path: Path) -> None:
    row = _row("capital.strategy_cash.cutoff", "alpha", "10.00", "USD", tolerance="0.01")
    findings = _expected_findings(tmp_path, [row])
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.capital.strategy_cash.cutoff", checks.NOT_READY)
    ]


def test_v6_a_retained_account_resolves_from_the_custody_members(tmp_path: Path) -> None:
    row = _row("capital.account.cutoff", "RETAINED1", "0.19", "USD", tolerance="0.01")
    row["as_of"] = "2026-07-17"
    assert _expected_findings(tmp_path, [row]) == []


def test_v6_a_wrong_retained_account_value_is_still_a_regression(tmp_path: Path) -> None:
    row = _row("capital.account.cutoff", "RETAINED1", "5.00", "USD", tolerance="0.01")
    row["as_of"] = "2026-07-17"
    findings = _expected_findings(tmp_path, [row])
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.capital.account.cutoff", checks.BLOCKER_REGRESSION)
    ]


def test_v6_a_member_dated_differently_from_the_oracle_is_still_reported(
    tmp_path: Path,
) -> None:
    """N59: the value matches, the custody member's date differs from the oracle's."""

    row = _row("capital.account.cutoff", "RETAINED1", "0.19", "USD", tolerance="0.01")
    row["as_of"] = "2025-11-24"
    findings = _expected_findings(tmp_path, [row])
    assert [f.message for f in findings] == ["value matches but is dated differently"]


def test_v6_an_account_no_view_carries_stays_not_ready(tmp_path: Path) -> None:
    row = _row("capital.account.cutoff", "NOPE", "1.00", "USD", tolerance="0.01")
    findings = _expected_findings(tmp_path, [row])
    assert [(f.check_id, f.klass) for f in findings] == [
        ("expected.capital.account.cutoff", checks.NOT_READY)
    ]
