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


VALUES = {"volatis": (VOLATIS_VALUE, AS_OF), "robuxio": (ROBUXIO_VALUE, AS_OF)}


def _derive(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = baseline._derive_rate_dollar_costs(rows, VALUES, AS_OF)
    return {row["strategy_id"]: row for row in out}


def test_a_settled_management_rate_gives_annual_dollars_with_its_derivation() -> None:
    volatis = _derive(_rows("NON_QUALIFIED_FLAT"))["volatis"]

    assert volatis["derived_annual_cost_usd"] == "2325.99"  # 0.035 x 66456.99, cents
    assert volatis["annual_external_cost_usd"] is None
    assert volatis["cost_derivation"] == {
        "as_of": "2026-09-17",
        "base_value_usd": "66456.99",
        "method": "DERIVED_FROM_DOCUMENTED_RATE",
        "rate": "0.035",
        "rate_kind": "management_rate",
        "tier": "NON_QUALIFIED_FLAT",
        "unpriced_components": [],
        "classified_components": [],
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


def test_an_in_nav_performance_fee_is_classified_never_priced() -> None:
    robuxio = _derive(_rows("NON_QUALIFIED_FLAT"))["robuxio"]
    assert robuxio["derived_annual_cost_usd"] == "0"  # management part stays visible
    derivation = robuxio["cost_derivation"]
    assert derivation["unpriced_components"] == []
    item = derivation["classified_components"][0]
    assert item["classification"] == "IN_NAV_NOT_BILLED"
    assert item["component"] == "performance_rate"
    assert item["rate"] == "0.3"
    assert item["mechanism"] == "over the high-water mark"
    assert item["reason"]
    # the documented redemption fee is billed outside the NAV but only on redemption
    redemption = next(
        part
        for part in derivation["classified_components"]
        if part["component"] == "redemption_fee"
    )
    assert redemption["classification"] == "EVENT_FEE_NOT_ANNUAL"
    assert redemption["rate"] == "0.005"
    assert redemption["placement"] == "OUTSIDE_BILLED"


def test_a_performance_fee_not_stated_inside_the_nav_stays_unpriced() -> None:
    # Volatis qualified tier: 20% performance, placement not stated -> a real gap.
    volatis = _derive(_rows("QUALIFIED_0_1M"))["volatis"]
    assert volatis["cost_derivation"]["unpriced_components"] == ["performance_rate"]
    assert volatis["cost_derivation"]["classified_components"] == []
    assert baseline._costs_claim([volatis])["status"] == "PARTIAL"


def test_a_value_dated_before_the_cutoff_is_refused_as_the_base() -> None:
    stale = {"volatis": (VOLATIS_VALUE, date(2026, 8, 16))}
    with pytest.raises(baseline.BaselineValidationError, match="not the 2026-09-17"):
        baseline._derive_rate_dollar_costs(_rows("NON_QUALIFIED_FLAT"), stale, AS_OF)


def _capital(value: str, day: str, status: str = "AVAILABLE") -> dict[str, Any]:
    return {
        "as_of_date": day,
        "current_value": {"currency": "USD", "status": status, "value": value},
        "status": status,
        "strategy_id": "volatis",
    }


def test_the_base_is_the_cutoff_value_of_the_current_capital_view() -> None:
    capital = {"strategies": [_capital("48057.62", "2026-09-17")]}
    values = baseline._cutoff_capital_values(capital, AS_OF)
    assert values == {"volatis": (Decimal("48057.62"), AS_OF)}
    out = baseline._derive_rate_dollar_costs(_rows("NON_QUALIFIED_FLAT"), values, AS_OF)
    volatis = next(row for row in out if row["strategy_id"] == "volatis")
    assert volatis["derived_annual_cost_usd"] == "1682.02"  # 0.035 x 48057.62
    assert volatis["cost_derivation"]["base_value_usd"] == "48057.62"


def test_the_published_base_is_cents_and_the_amount_follows_from_it() -> None:
    values = {"volatis": (Decimal("49868.14294300700543930275696"), AS_OF)}
    out = baseline._derive_rate_dollar_costs(_rows("NON_QUALIFIED_FLAT"), values, AS_OF)
    volatis = next(row for row in out if row["strategy_id"] == "volatis")
    assert volatis["cost_derivation"]["base_value_usd"] == "49868.14"
    assert volatis["derived_annual_cost_usd"] == "1745.38"  # 0.035 x 49868.14


def test_a_capital_row_not_dated_at_the_cutoff_gives_no_base() -> None:
    old = {"strategies": [_capital("66593.33", "2026-08-16")]}
    assert baseline._cutoff_capital_values(old, AS_OF) == {}
    pending = {"strategies": [_capital("1", "2026-09-17", status="NOT_READY")]}
    assert baseline._cutoff_capital_values(pending, AS_OF) == {}


def test_the_catalog_derives_after_the_current_capital_and_not_in_the_cost_builder() -> (
    None
):
    import inspect

    build = inspect.getsource(baseline.build_catalog)
    assert (
        build.index("_build_current_capital(")
        < build.index("_derive_rate_dollar_costs(")
        < build.index("_fact_coverage(")
    )
    # the allocation snapshot's dated values must not feed the derivation
    assert "_derive_rate_dollar_costs" not in inspect.getsource(
        baseline._allocations_and_coverage
    )


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


def test_the_costs_fact_counts_an_in_nav_classified_row_as_classified() -> None:
    rows = _derive(_rows("NON_QUALIFIED_FLAT"))
    claim = baseline._costs_claim([rows["volatis"], rows["robuxio"]])
    assert claim["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"


def _unknown(strategy: str) -> dict[str, Any]:
    return {
        "annual_external_cost_usd": None,
        "cost_status": "unknown",
        "strategy_id": strategy,
    }


def test_a_deprecated_history_only_strategy_is_excluded_with_the_policy_reason() -> (
    None
):
    (row,) = baseline._classify_excluded_history_only_costs(
        [_unknown("echotrade-growth-pivots")], AS_OF
    )
    classification = row["cost_classification"]
    assert classification["classification"] == "EXCLUDED_DEPRECATED_HISTORY_ONLY"
    assert "2025-11-24" in classification["reason"]
    assert classification["effective_date"] == "2025-11-25"
    assert baseline._costs_claim([row])["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"


def test_an_unknown_cost_of_a_live_strategy_is_not_excluded() -> None:
    (row,) = baseline._classify_excluded_history_only_costs(
        [_unknown("volatis")], AS_OF
    )
    assert "cost_classification" not in row
    assert baseline._costs_claim([row])["status"] == "PARTIAL"


def test_a_de_minimis_exclusion_is_not_a_deprecation() -> None:
    # composer is excluded from the HWM governor as immaterial, not deprecated
    (row,) = baseline._classify_excluded_history_only_costs(
        [_unknown("composer-holy-grail")], AS_OF
    )
    assert "cost_classification" not in row


def test_the_exclusion_applies_only_from_its_effective_date() -> None:
    (row,) = baseline._classify_excluded_history_only_costs(
        [_unknown("echotrade-growth-pivots")], date(2025, 11, 24)
    )
    assert "cost_classification" not in row


def test_a_priced_row_is_never_reclassified() -> None:
    priced = {**_known("echotrade-growth-pivots", "10")}
    (row,) = baseline._classify_excluded_history_only_costs([priced], AS_OF)
    assert "cost_classification" not in row


def test_a_classification_without_its_reason_does_not_count() -> None:
    row = {
        **_unknown("echotrade-growth-pivots"),
        "cost_classification": {
            "classification": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
            "reason": "",
        },
    }
    assert baseline._costs_claim([row])["status"] == "PARTIAL"


def test_the_costs_fact_stays_partial_for_an_unpriced_rate_row() -> None:
    rows = _rows("NON_QUALIFIED_FLAT")  # no derivation applied
    assert baseline._costs_claim(rows)["status"] == "PARTIAL"


@pytest.mark.parametrize("method", ["", "OTHER", None])
def test_a_derived_amount_without_the_exact_method_does_not_count(method: Any) -> None:
    volatis = _derive(_rows("NON_QUALIFIED_FLAT"))["volatis"]
    volatis["cost_derivation"] = {**volatis["cost_derivation"], "method": method}
    assert baseline._costs_claim([volatis])["status"] == "PARTIAL"
