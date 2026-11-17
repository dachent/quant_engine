"""Annual dollars of a documented management rate: rate x cutoff sleeve value.

A percent-based cost used to leave the whole ``costs`` fact PARTIAL because it had no
dollar amount.  The management rate of the selected tier times the sleeve value is a
dollar amount the documentation supports; a performance rate is not a share of the
sleeve value and is never priced.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


baseline = _load(
    "portfolio_baseline_cost_dollar", ROOT / "tools" / "portfolio_baseline.py"
)
docs = _load("cost_rate_documents_dollar", ROOT / "tests/core/cost_rate_documents.py")
AS_OF = date(2026, 9, 17)
VOLATIS_VALUE = Decimal("66456.99")
ROBUXIO_VALUE = Decimal("48348.11")


def _rows(volatis_tier: str, rates_mutator: Any = None) -> list[dict[str, Any]]:
    volatis = docs.volatis_rates(volatis_tier)
    if rates_mutator is not None:
        rates_mutator(volatis)
    return [
        {
            "annual_external_cost_usd": None,
            "cost_status": baseline.COST_STATUS_DOCUMENTED_RATE,
            "strategy_id": "volatis",
            "fee_rates": volatis,
        },
        {
            "annual_external_cost_usd": None,
            "cost_status": baseline.COST_STATUS_DOCUMENTED_RATE,
            "strategy_id": "robuxio",
            "fee_rates": docs.robuxio_rates(),
        },
    ]


VALUES = {"volatis": VOLATIS_VALUE, "robuxio": ROBUXIO_VALUE}


def _derive(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = baseline._derive_rate_dollar_costs(rows, VALUES, AS_OF)
    return {row["strategy_id"]: row for row in out}


def test_a_settled_management_rate_gives_annual_dollars_with_its_derivation() -> None:
    volatis = _derive(_rows("NON_QUALIFIED_FLAT"))["volatis"]

    assert volatis["derived_annual_cost_usd"] == str(Decimal("0.035") * VOLATIS_VALUE)
    assert volatis["annual_external_cost_usd"] is None
    assert volatis["cost_derivation"] == {
        "as_of": "2026-09-17",
        "base_value_usd": "66456.99",
        "method": "DERIVED_FROM_DOCUMENTED_RATE",
        "rate": "0.035",
        "rate_kind": "management_rate",
        "tier": "NON_QUALIFIED_FLAT",
        "unpriced_components": [],
    }
    # the rate row stays visible
    assert volatis["fee_rates"]["tier"] == "NON_QUALIFIED_FLAT"


def test_an_unsettled_tier_is_never_priced() -> None:
    rows = _rows("TIER_UNSETTLED")
    for row in rows:
        if row["strategy_id"] == "volatis":
            row["cost_status"] = baseline.COST_STATUS_TIER_UNSETTLED
    volatis = _derive(rows)["volatis"]
    assert "derived_annual_cost_usd" not in volatis


def test_a_rate_the_documentation_does_not_state_is_not_invented() -> None:
    def blank(rates: dict[str, Any]) -> None:
        for row in rates["tier_table"]:
            row["management_rate"] = None

    volatis = _derive(_rows("NON_QUALIFIED_FLAT", blank))["volatis"]
    assert "derived_annual_cost_usd" not in volatis


def test_a_performance_rate_is_not_priced_as_a_share_of_the_sleeve() -> None:
    robuxio = _derive(_rows("NON_QUALIFIED_FLAT"))["robuxio"]
    assert robuxio["derived_annual_cost_usd"] == "0"
    assert robuxio["cost_derivation"]["unpriced_components"] == ["performance_rate"]


def test_a_strategy_without_a_sleeve_value_is_not_priced() -> None:
    out = baseline._derive_rate_dollar_costs(_rows("NON_QUALIFIED_FLAT"), {}, AS_OF)
    assert all("derived_annual_cost_usd" not in row for row in out)


def _known(strategy: str, usd: str) -> dict[str, Any]:
    return {
        "annual_external_cost_usd": usd,
        "cost_status": "known_fixed",
        "strategy_id": strategy,
    }


def test_the_costs_fact_is_complete_once_every_row_has_a_priced_amount() -> None:
    rows = _derive(_rows("NON_QUALIFIED_FLAT"))
    only_volatis = [rows["volatis"], _known("tt", "798")]
    assert baseline._costs_claim(only_volatis)["status"] == (
        "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    )


def test_the_costs_fact_stays_partial_while_a_performance_fee_is_unpriced() -> None:
    rows = _derive(_rows("NON_QUALIFIED_FLAT"))
    claim = baseline._costs_claim([rows["volatis"], rows["robuxio"]])
    assert claim["status"] == "PARTIAL"


def test_the_costs_fact_stays_partial_for_an_unpriced_rate_row() -> None:
    rows = _rows("NON_QUALIFIED_FLAT")  # no derivation applied
    assert baseline._costs_claim(rows)["status"] == "PARTIAL"


@pytest.mark.parametrize("method", ["", "OTHER", None])
def test_a_derived_amount_without_the_exact_method_does_not_count(method: Any) -> None:
    volatis = _derive(_rows("NON_QUALIFIED_FLAT"))["volatis"]
    volatis["cost_derivation"] = {**volatis["cost_derivation"], "method": method}
    assert baseline._costs_claim([volatis])["status"] == "PARTIAL"
