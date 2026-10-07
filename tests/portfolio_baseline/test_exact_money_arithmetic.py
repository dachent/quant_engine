"""Class sweep (Astra rg4): published and compared money is added exactly.

The default 28-digit Decimal context rounds a sum of wide amounts, and the rounding
depends on the order the terms arrive in.  The baseline compares its allocation
total, its audit replay and the materializer's identity checks for equality, so a
sum rounded in one place and exact in another rejects a valid input, or admits an
invalid one, depending on row order.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
from typing import Any

import portfolio_baseline as baseline

# Thirty significant digits once the two fractions are added: the ambient context
# rounds the sum to 28 and drops them, in either order.
WIDE = "10000000000000000000000000000"
VALUES = [WIDE, "0.4", "0.4", "0", "0", "0", "0", "0", "0"]
EXACT_TOTAL = Decimal("10000000000000000000000000000.8")


def _write_csv(path: Path, source_key: str, rows: list[dict[str, str]]) -> None:
    fields = baseline._CSV_FIELDS[source_key]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _paths(tmp_path: Path, values: list[str]) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    component = tmp_path / "component.csv"
    coverage = tmp_path / "coverage.csv"
    costs = tmp_path / "costs.csv"
    _write_csv(
        component,
        "component-decision-table",
        [
            {
                "component": f"Synthetic Strategy {index}",
                "current_value": value,
                "systematic_weight": "0.1111111111111111",
            }
            for index, value in enumerate(values, start=1)
        ],
    )
    _write_csv(
        coverage,
        "data-coverage-matrix",
        [
            {
                "component": f"Synthetic Strategy {index}",
                "current_value": value,
                "systematic_weight": "0.1111111111111111",
                "account_specific_daily_series": "yes" if index <= 3 else "no",
                "daily_start": "2026-07-01" if index <= 3 else "",
                "daily_end": "2026-07-06" if index <= 3 else "",
                "return_observations": "2" if index <= 3 else "",
            }
            for index, value in enumerate(values, start=1)
        ],
    )
    _write_csv(
        costs,
        "cost-economics",
        [
            {
                "component": f"Synthetic Strategy {index}",
                "annual_external_cost_dollars": "1",
                "cost_status": "KNOWN",
            }
            for index in range(1, len(values) + 1)
        ],
    )
    return {
        "component-decision-table": component,
        "data-coverage-matrix": coverage,
        "cost-economics": costs,
    }


ALIASES = {f"synthetic strategy {index}": f"strategy-{index}" for index in range(1, 10)}


def _allocation_totals(tmp_path: Path, values: list[str]) -> tuple[Decimal, Decimal]:
    _a, _c, _k, systematic_total, nav_covered = baseline._allocations_and_coverage(
        _paths(tmp_path, values), ALIASES, date(2026, 7, 25), date(2026, 7, 30)
    )
    return systematic_total, nav_covered


def test_allocation_total_is_exact_in_either_row_order(tmp_path: Path) -> None:
    forward, _ = _allocation_totals(tmp_path / "forward", VALUES)
    reverse, _ = _allocation_totals(tmp_path / "reverse", list(reversed(VALUES)))

    assert forward == EXACT_TOTAL
    assert reverse == EXACT_TOTAL


def test_nav_covered_numerator_is_exact_in_either_row_order(tmp_path: Path) -> None:
    _, forward = _allocation_totals(tmp_path / "forward", VALUES)
    # The first three rows carry a daily series: WIDE, 0.4 and 0.4.
    assert forward == EXACT_TOTAL
    _, reverse = _allocation_totals(
        tmp_path / "reverse", [*VALUES[:3][::-1], *VALUES[3:]]
    )
    assert reverse == EXACT_TOTAL


def _replay_inputs(
    tmp_path: Path, total_liquid: str
) -> tuple[dict[str, Any], dict[str, Path]]:
    (tmp_path / "summary.json").write_text(
        '{"systematic_total": 10000000000000000000000000000.8, "reserve_total": 0.4}',
        encoding="utf-8",
    )
    (tmp_path / "evidence.json").write_text(
        json.dumps({"current_correlation_one_underwriting_loss": -0.3725}),
        encoding="utf-8",
    )
    (tmp_path / "control.json").write_text(
        json.dumps({"portfolio_action": "HOLD"}), encoding="utf-8"
    )
    (tmp_path / "hwm.json").write_text(
        json.dumps({"full_systematic_hwm_status": "NOT_AUTOMATABLE"}), encoding="utf-8"
    )
    spec = {
        "correlation_one_stress": {"tolerance": "0.0001", "value": "0.3725"},
        "full_systematic_hwm_status": "NOT_AUTOMATABLE",
        "nav_coverage": {"tolerance": "0.0001", "value": "1"},
        "portfolio_action": "HOLD",
        "reserve_total_usd": {"tolerance": "0", "value": "0.4"},
        "systematic_total_usd": {"tolerance": "0", "value": str(EXACT_TOTAL)},
        "total_liquid_usd": {"tolerance": "0", "value": total_liquid},
    }
    paths = {
        "current-portfolio-summary": tmp_path / "summary.json",
        "phase2-evidence-summary": tmp_path / "evidence.json",
        "combined-control-status": tmp_path / "control.json",
        "hwm-status": tmp_path / "hwm.json",
    }
    return spec, paths


def test_audit_replay_total_liquid_is_added_exactly(tmp_path: Path) -> None:
    spec, paths = _replay_inputs(tmp_path, "10000000000000000000000000001.2")

    replay, _reserve = baseline._replay(spec, paths, EXACT_TOTAL, EXACT_TOTAL)

    assert replay["total_liquid_usd"] == "10000000000000000000000000001.2"
    assert replay["systematic_total_usd"] == str(EXACT_TOTAL)


def test_audit_replay_does_not_depend_on_the_callers_precision(tmp_path: Path) -> None:
    spec, paths = _replay_inputs(tmp_path, "10000000000000000000000000001.2")

    rendered = []
    for precision in (28, 60):
        with localcontext() as context:
            context.prec = precision
            replay, _ = baseline._replay(spec, paths, EXACT_TOTAL, EXACT_TOTAL)
        rendered.append(replay["total_liquid_usd"])

    assert rendered == ["10000000000000000000000000001.2"] * 2


def test_alpaca_fill_amount_is_the_exact_product_of_quantity_and_price(
    tmp_path: Path,
) -> None:
    """Crypto fills carry nine decimals each way: the product exceeds 28 digits."""

    path = tmp_path / "alpaca_events.csv"
    _write_csv(
        path,
        "alpaca-events",
        [
            {
                "date": "2026-07-01",
                "type": "buy_fill",
                "symbol": "BTC/USD",
                "qty": "1234.123456789012",
                "price": "67890.123456789012",
            },
            {
                "date": "2026-07-02",
                "type": "sell_fill",
                "symbol": "BTC/USD",
                "qty": "1234.123456789012",
                "price": "67890.123456789012",
            },
        ],
    )
    with localcontext() as context:
        context.prec = 100
        exact = Decimal("1234.123456789012") * Decimal("67890.123456789012")
    assert len(exact.as_tuple().digits) > 28

    transactions, _securities = baseline._normalize_transactions(
        path, {"alpaca": "account-alpaca"}, []
    )

    amounts = [Decimal(row["amount"]["value"]) for row in transactions]
    assert amounts == [exact.copy_negate(), exact]


def test_schwab_position_market_value_total_is_exact_in_either_order() -> None:
    def position(index: int, value: str) -> dict[str, Any]:
        return {
            "account_id": "account-schwab",
            "as_of_date": "2026-07-30",
            "evidence_class": "WCH_CURRENT",
            "market_value": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "record_id": f"position-{index}",
            "security_id": f"security-{index}",
            "symbol": f"SYN{index}",
        }

    def facts(values: list[str]) -> str:
        catalog = {
            "positions": [position(i, value) for i, value in enumerate(values)],
            "transactions": [],
            "nav_observations": [
                {
                    "account_id": "account-schwab",
                    "as_of_date": "2026-07-01",
                    "nav": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
                }
            ],
            "coverage": {"known_gaps": []},
        }
        reconciliation = baseline._schwab_facts(
            catalog,
            [{"account_id": "account-schwab", "provider": "SCHWAB"}],
            [],
            [
                {
                    "account_id": "account-schwab",
                    "as_of_date": "2026-07-01",
                    "nav": {"value": "1"},
                }
            ],
            date(2026, 7, 30),
        )[4]
        return str(reconciliation["market_value_usd"])

    values = [WIDE, "0.4", "0.4"]
    assert facts(values) == str(EXACT_TOTAL)
    assert facts(list(reversed(values))) == str(EXACT_TOTAL)
