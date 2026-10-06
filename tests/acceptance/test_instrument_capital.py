"""Batch V5: strategy capital is the sleeve NAV plus the unallocated cash line.

Owner decision OWNER_318: a strategy's capital is its sleeve account NAV plus the derived
cash line the custody block publishes (``custody_current_capital.strategies[].derived_cash``).
The strategy-versus-accounts and reconciliation recompute checks summed only the accounts
and so called a correct total a defect.  A total that equals neither the accounts nor the
accounts plus the published cash still blocks.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from cash_fixtures import complete_event
from test_validate_generation import Spec, build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

CASH = "93.97"
BETA_WITH_CASH = "5593.97"  # B1 + B2 = 5500 in the good fixture
SYSTEMATIC_WITH_CASH = "6693.97"


def _cash_line(value: str = CASH, status: str = "AVAILABLE") -> dict[str, Any]:
    return {
        "account_id": "CASH1",
        "basis": "DERIVED_ADMITTED_EXTERNAL_FLOWS_MINUS_SLEEVE_ALLOCATIONS_PLUS_REDEMPTIONS",
        "current_value": {"currency": "USD", "status": status, "value": value},
        # The flow arithmetic the line must reproduce (deposits less allocations).
        "events": [
            complete_event(
                {
                    "signed_usd": "1000.00",
                    "event_date": "2025-01-01",
                    "record_id": "cash:a",
                }
            ),
            complete_event(
                {
                    "signed_usd": str(Decimal(value) - Decimal("1000.00")),
                    "event_date": "2025-01-02",
                    "record_id": "cash:b",
                }
            ),
        ],
    }


def _with_cash(shown_beta: str, claim: str, cash: dict[str, Any] | None = None):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "strategies": [
                {"strategy_id": "beta", "derived_cash": [cash or _cash_line()]}
            ]
        }
        for row in report["strategy_summary"]:
            if row["strategy_id"] == "beta":
                row["current_value_usd"] = shown_beta
        report["reconciliation_checks"].append(
            {
                "check_id": "strategy_capital_to_cutoff",
                "status": "PASS",
                "details_json": {"systematic_total_usd": claim},
            }
        )

    return mutate


def _panel_total(panel: dict[str, Any]) -> None:
    panel["capital"]["systematic"] = SYSTEMATIC_WITH_CASH


def _spec(*mutators: Any) -> Spec:
    return Spec(report=list(mutators), panel=[_panel_total])


def _findings(tmp_path: Path, spec: Spec) -> list[checks.Finding]:
    generation = load_generation(build(tmp_path, spec))
    context = checks.Context(
        gen=generation,
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )
    return [
        *checks.check_strategy_vs_cutoff_accounts(context),
        *checks.check_reconciliation_recompute(context),
    ]


def test_v5_sleeve_plus_cash_is_the_strategy_capital(tmp_path: Path) -> None:
    spec = _spec(_with_cash(BETA_WITH_CASH, SYSTEMATIC_WITH_CASH))
    assert _findings(tmp_path, spec) == []


def test_v5_a_strategy_capital_that_is_neither_still_blocks(tmp_path: Path) -> None:
    spec = _spec(_with_cash("5600", SYSTEMATIC_WITH_CASH))
    findings = _findings(tmp_path, spec)
    assert [(f.check_id, f.entity) for f in findings] == [
        ("capital.strategy_vs_cutoff_accounts", "beta")
    ]
    assert Decimal(
        str(findings[0].expected["sum_of_account_values_at_cutoff"])
    ) == Decimal(BETA_WITH_CASH)


def test_v5_a_reconciliation_total_that_ties_to_nothing_still_blocks(
    tmp_path: Path,
) -> None:
    spec = _spec(_with_cash(BETA_WITH_CASH, "6700.00"))
    findings = _findings(tmp_path, spec)
    assert [(f.check_id, f.klass) for f in findings] == [
        ("capital.reconciliation_recompute", checks.BLOCKER_INVARIANT)
    ]


def test_v5_cash_that_is_not_available_is_not_added(tmp_path: Path) -> None:
    spec = _spec(
        _with_cash(
            BETA_WITH_CASH, SYSTEMATIC_WITH_CASH, _cash_line(status="UNAVAILABLE")
        )
    )
    checked = {f.check_id for f in _findings(tmp_path, spec)}
    assert checked == {
        "capital.strategy_vs_cutoff_accounts",
        "capital.reconciliation_recompute",
    }


def test_v5_a_panel_systematic_total_without_the_cash_blocks(tmp_path: Path) -> None:
    def sleeves_only(panel: dict[str, Any]) -> None:
        panel["capital"]["systematic"] = "6600"

    spec = Spec(
        report=[_with_cash(BETA_WITH_CASH, SYSTEMATIC_WITH_CASH)],
        panel=[sleeves_only],
    )
    assert [(f.check_id, f.entity) for f in _findings(tmp_path, spec)] == [
        ("capital.strategy_vs_cutoff_accounts", "portfolio.systematic")
    ]
