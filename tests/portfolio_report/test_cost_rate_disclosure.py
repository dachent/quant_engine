"""The panel's percent-rate sentence is generated from the row's own fields."""

from __future__ import annotations

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


report = _load(
    "portfolio_report_cost_rate_disclosure", ROOT / "tools/portfolio_report.py"
)
docs = _load("cost_rate_documents_report", ROOT / "tests/core/cost_rate_documents.py")


def _row(
    rates: dict[str, Any], status: str = "documented_rate_percent"
) -> dict[str, Any]:
    return {
        "annual_external_cost_usd": None,
        "cost_class": "PERCENT_RATE",
        "cost_status": status,
        "fee_rates": rates,
    }


def test_volatis_qualified_sentence_states_rates_and_what_is_not_stated() -> None:
    sentence = report.cost_rate_disclosure(_row(docs.volatis_rates("QUALIFIED_0_1M")))

    assert sentence == (
        "Documented rates, Qualified $0-1M (qualified clients, total AUM up to $1M): "
        "management fee 0%; performance fee 20% (high-water mark and assessment "
        "period not stated in the documentation); fee placement: not stated in the "
        "documentation; sources: Strategies/Fund/Fact Sheet.pdf p.1; "
        "no dollar cost is published."
    )


def test_volatis_non_qualified_sentence_has_no_performance_fee_claim() -> None:
    sentence = report.cost_rate_disclosure(
        _row(docs.volatis_rates("NON_QUALIFIED_FLAT"))
    )

    assert "management fee 3.5%" in sentence
    assert "performance fee not stated in the documentation" in sentence
    assert "20%" not in sentence
    assert "Non-qualified flat" in sentence


@pytest.mark.parametrize("with_evidence", [True, False])
def test_an_unsettled_tier_claims_no_rate(with_evidence: bool) -> None:
    cost: dict[str, Any] = _row(
        docs.volatis_rates("TIER_UNSETTLED"), "documented_rate_tier_unsettled"
    )
    if not with_evidence:
        cost = {
            "annual_external_cost_usd": None,
            "cost_status": "documented_rate_tier_unsettled",
        }

    sentence = report.cost_rate_disclosure(cost)

    assert sentence == (
        "Percent-based cost: the fee tier is not settled, so no rate is claimed; "
        "no dollar cost is published."
    )
    assert "%" not in sentence
    assert (
        report.fees_status_for_cost(
            cost["cost_status"], cost["annual_external_cost_usd"]
        )
        == "UNAVAILABLE"
    )


def test_robuxio_sentence_has_performance_redemption_lock_and_placement() -> None:
    sentence = report.cost_rate_disclosure(_row(docs.robuxio_rates()))

    assert sentence == (
        "Documented rates, Matrixport route (single rate, no tiers): "
        "management fee 0%; performance fee 30% (high-water mark: over the "
        "high-water mark); fee placement: inside the vehicle NAV; "
        "redemption fee 0.5% (placement: billed outside the NAV); lock 30 days; "
        "sources: Strategies/Crypto/Investment Memo.docx p.6, "
        "Strategies/Crypto/Onboarding Guide.pdf p.27; no dollar cost is published."
    )


def test_a_row_that_is_not_percent_based_has_no_sentence() -> None:
    assert report.cost_rate_disclosure({"cost_status": "known_fixed"}) is None


def test_fees_status_never_calls_a_percent_rate_a_cost_or_a_cap() -> None:
    fees = report.fees_status_for_cost
    assert fees("documented_rate_percent", None) == "DOCUMENTED_RATE_NOT_DOLLAR"
    assert fees("documented_rate_percent", "664.57") is None
    assert fees("documented_rate_tier_unsettled", None) == "UNAVAILABLE"
    # Existing classes are unchanged.
    assert fees("known_fixed", "798") == "EVIDENCED_PRE_TAX"
    assert fees("conservative_1pct_contract_cap", "664.57") == (
        "UPPER_BOUND_CONTRACT_CAP_PRE_TAX"
    )
    assert fees("unknown_subscription_cost", None) == "UNAVAILABLE"


def test_the_cutoff_cost_rate_of_a_percent_row_names_why_there_is_no_ratio() -> None:
    cost = _row(docs.volatis_rates())
    cost["fees_status"] = report.fees_status_for_cost(cost["cost_status"], None)
    result = report._cutoff_cost_rate(
        {
            "current_value_usd": "1000",
            "capital_as_of_date": "2026-09-17",
            "capital_status": "AVAILABLE",
        },
        cost,
        {
            "calculation_cutoff_date": "2026-09-17",
            "cost_schedule_as_of_date": "2026-07-25",
        },
    )

    assert result["fees_status"] == "DOCUMENTED_RATE_NOT_DOLLAR"
    assert result["annual_external_cost_rate_pre_tax"] is None
    assert "documented percent rate" in result["cost_rate_reason"]


def test_the_evidence_keys_stay_equal_across_producers() -> None:
    attribution = _load(
        "portfolio_attribution_cost_rate_keys", ROOT / "tools/portfolio_attribution.py"
    )
    assert tuple(report.COST_EVIDENCE_KEYS) == tuple(attribution.COST_EVIDENCE_KEYS)
    assert tuple(report.COST_RATE_KEYS) == tuple(attribution.COST_RATE_KEYS)
    assert report.COST_RATE_KEYS == ("fee_rates",)
