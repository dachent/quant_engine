"""Percent-based cost rows: documented rates, no fabricated dollar cost.

A Volatis-like row (tiered fee table, tier selected by data, placement not stated),
a Robuxio-like row (single rate over a high-water mark, redemption fee outside the
NAV, lock) and the owner-declared basis for a fixed amount.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import importlib.util
import json
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
    "portfolio_baseline_cost_rates", ROOT / "tools" / "portfolio_baseline.py"
)
docs = _load("cost_rate_documents_baseline", ROOT / "tests/core/cost_rate_documents.py")
ALIASES = {"volatis": "volatis", "robuxio": "robuxio", "ai factor": "ai-factor"}


def _costs(volatis_status: str, volatis_amount: str | None = None):
    return [
        {
            "annual_external_cost_usd": volatis_amount,
            "cost_status": volatis_status,
            "strategy_id": "volatis",
        },
        {
            "annual_external_cost_usd": None,
            "cost_status": baseline.COST_STATUS_DOCUMENTED_RATE,
            "strategy_id": "robuxio",
        },
    ]


def _attach(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    costs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    path = tmp_path / "cost_evidence.json"
    path.write_text(
        json.dumps({"rows": rows, "schema": baseline.COST_EVIDENCE_SCHEMA}),
        encoding="utf-8",
    )
    return baseline._attach_cost_evidence(costs, path, ALIASES, date(2026, 9, 17))


def _rows(tier: str) -> list[dict[str, Any]]:
    return [
        docs.evidence_row("Volatis", docs.volatis_rates(tier)),
        docs.evidence_row("Robuxio", docs.robuxio_rates()),
    ]


@pytest.mark.parametrize(
    "tier, status",
    [
        ("QUALIFIED_0_1M", "documented_rate_percent"),
        ("NON_QUALIFIED_FLAT", "documented_rate_percent"),
        ("TIER_UNSETTLED", "documented_rate_tier_unsettled"),
    ],
)
def test_the_volatis_tier_is_selected_by_data_and_no_dollar_amount_exists(
    tmp_path: Path, tier: str, status: str
) -> None:
    costs = _attach(tmp_path, _rows(tier), _costs(status))
    volatis = next(row for row in costs if row["strategy_id"] == "volatis")

    assert volatis["annual_external_cost_usd"] is None
    assert volatis["cost_status"] == status
    assert volatis["cost_class"] == "PERCENT_RATE"
    rates = volatis["fee_rates"]
    assert rates["tier"] == tier
    assert rates["placement"] == "NOT_STATED_IN_DOCUMENTATION"
    assert rates["placement_source"] is None
    # The whole tier table stays in the data whichever tier is selected.
    assert {row["tier"] for row in rates["tier_table"]} == {
        "QUALIFIED_0_1M",
        "QUALIFIED_1_5M",
        "NON_QUALIFIED_FLAT",
    }
    qualified = rates["tier_table"][0]
    assert qualified["management_rate"] == "0"
    assert qualified["performance_rate"] == "0.2"
    assert qualified["performance_mechanism"] is None
    assert qualified["sources"][0]["page"] == "1"
    assert len(qualified["sources"][0]["sha256"]) == 64


def test_the_robuxio_row_states_rates_placement_redemption_and_lock(
    tmp_path: Path,
) -> None:
    costs = _attach(
        tmp_path, _rows("QUALIFIED_0_1M"), _costs("documented_rate_percent")
    )
    robuxio = next(row for row in costs if row["strategy_id"] == "robuxio")
    rates = robuxio["fee_rates"]

    assert robuxio["annual_external_cost_usd"] is None
    assert rates["placement"] == "INSIDE_VEHICLE_NAV"
    assert rates["redemption"]["rate"] == "0.005"
    assert rates["redemption"]["placement"] == "OUTSIDE_BILLED"
    assert rates["lock"]["days"] == 30
    only = rates["tier_table"][0]
    assert only["performance_rate"] == "0.3"
    assert only["performance_mechanism"] == "over the high-water mark"
    assert [source["page"] for source in only["sources"]] == ["27", "6"]


def test_a_percent_row_with_a_dollar_amount_is_refused(tmp_path: Path) -> None:
    with pytest.raises(baseline.BaselineValidationError, match="no dollar amount"):
        _attach(
            tmp_path,
            _rows("QUALIFIED_0_1M"),
            _costs("documented_rate_percent", "664.57"),
        )


@pytest.mark.parametrize(
    "tier, status",
    [
        ("QUALIFIED_0_1M", "documented_rate_tier_unsettled"),
        ("TIER_UNSETTLED", "documented_rate_percent"),
        ("QUALIFIED_0_1M", "conservative_1pct_contract_cap"),
    ],
)
def test_the_cost_status_must_match_the_tier_state(
    tmp_path: Path, tier: str, status: str
) -> None:
    with pytest.raises(baseline.BaselineValidationError, match="must carry status"):
        _attach(tmp_path, _rows(tier), _costs(status))


def _mutate(tmp_path: Path, change: Any, match: str) -> None:
    rows = _rows("QUALIFIED_0_1M")
    change(rows[0]["rates"])
    with pytest.raises(baseline.BaselineValidationError, match=match):
        _attach(tmp_path, rows, _costs("documented_rate_percent"))


def test_a_rate_without_a_source_or_out_of_range_is_refused(tmp_path: Path) -> None:
    _mutate(
        tmp_path,
        lambda rates: rates["tier_table"][0].update(sources=[]),
        "without a source",
    )
    _mutate(
        tmp_path,
        lambda rates: rates["tier_table"][0].update(performance_rate="20"),
        "between 0 and 1",
    )
    _mutate(tmp_path, lambda rates: rates.update(tier="GOLD"), "not in the tier table")


def test_placement_and_its_source_are_both_stated_or_both_absent(
    tmp_path: Path,
) -> None:
    _mutate(
        tmp_path,
        lambda rates: rates.update(placement="INSIDE_VEHICLE_NAV"),
        "both be stated or both absent",
    )
    _mutate(tmp_path, lambda rates: rates.update(placement="SOMEWHERE"), "placement")


def test_rates_require_the_percent_rate_class_and_a_cost_row(tmp_path: Path) -> None:
    rows = _rows("QUALIFIED_0_1M")
    rows[0]["cost_class"] = "SUBSCRIPTION"
    with pytest.raises(baseline.BaselineValidationError, match="require cost_class"):
        _attach(tmp_path, rows, _costs("documented_rate_percent"))
    with pytest.raises(baseline.BaselineValidationError, match="without a cost row"):
        _attach(
            tmp_path, _rows("QUALIFIED_0_1M"), _costs("documented_rate_percent")[:1]
        )


def test_a_documented_rate_status_without_an_evidence_row_is_refused(
    tmp_path: Path,
) -> None:
    """The gate in the cost-row builder: no fee_rates, no documented-rate claim."""

    bare = _costs("documented_rate_percent")
    with pytest.raises(baseline.BaselineValidationError, match="no fee-rate evidence"):
        baseline._require_fee_rate_evidence(bare)
    attached = _attach(tmp_path, _rows("QUALIFIED_0_1M"), bare)
    baseline._require_fee_rate_evidence(attached)
    # An unsettled tier claims no rate, so it needs no evidence row.
    baseline._require_fee_rate_evidence(_costs("documented_rate_tier_unsettled")[:1])


def test_an_owner_declared_fixed_amount_is_an_admitted_basis(tmp_path: Path) -> None:
    sha = docs.digest("owner message")
    row = {
        "component": "AI Factor",
        "cost_class": "SUBSCRIPTION",
        "corroboration": [],
        "declared": {
            "amount": "500",
            "basis": "OWNER_DECLARED",
            "currency": "USD",
            "period": "MONTH",
            "source": {
                "artifact": "owner message 2026-10-15T17:02:58Z",
                "field": "transcript line 3077",
                "sha256": sha,
            },
        },
    }
    costs = _attach(
        tmp_path,
        [row],
        [
            {
                "annual_external_cost_usd": "6000",
                "cost_status": "known_owner_declared",
                "strategy_id": "ai-factor",
            }
        ],
    )

    assert costs[0]["declared"]["basis"] == "OWNER_DECLARED"
    assert costs[0]["declared"]["amount"] == "500"
    bad = deepcopy(row)
    bad["declared"]["basis"] = "GUESSED"
    with pytest.raises(
        baseline.BaselineValidationError, match="basis is not recognised"
    ):
        _attach(tmp_path, [bad], costs)


def _settled_rows() -> list[dict[str, Any]]:
    rates = docs.volatis_rates("NON_QUALIFIED_FLAT")
    rates["derivation"] = docs.volatis_derivation(rates)
    return [
        docs.evidence_row("Volatis", rates),
        docs.evidence_row("Robuxio", docs.robuxio_rates()),
    ]


def _attach_settled(tmp_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    costs = _attach(tmp_path, rows, _costs("documented_rate_percent"))
    return next(row for row in costs if row["strategy_id"] == "volatis")


def test_a_closed_derivation_settles_the_non_qualified_rate(tmp_path: Path) -> None:
    volatis = _attach_settled(tmp_path, _settled_rows())
    rates = volatis["fee_rates"]
    derivation = rates["derivation"]

    assert volatis["annual_external_cost_usd"] is None
    assert volatis["cost_status"] == "documented_rate_percent"
    assert rates["tier"] == "NON_QUALIFIED_FLAT"
    selected = next(row for row in rates["tier_table"] if row["tier"] == rates["tier"])
    assert selected["management_rate"] == "0.035"
    assert derivation["periods_per_year"] == 12
    assert derivation["periodic_rate"] == "0.0029167"
    assert derivation["performance_fee"] == "NONE_BILLED"
    assert derivation["hwm"] == "NONE_BILLED"
    assert derivation["rows"] == 6
    assert derivation["first_date"] == "2025-01-01"
    assert derivation["last_date"] == "2025-06-01"
    assert derivation["max_abs_residual_usd"] == "0"
    # The published form is bound to its rows by hash but does not carry them.
    assert "closure" not in derivation
    assert len(derivation["closure_sha256"]) == 64


def test_a_row_without_a_derivation_is_carried_as_before(tmp_path: Path) -> None:
    costs = _attach(
        tmp_path, _rows("NON_QUALIFIED_FLAT"), _costs("documented_rate_percent")
    )
    volatis = next(row for row in costs if row["strategy_id"] == "volatis")

    assert "derivation" not in volatis["fee_rates"]


def test_a_posted_fee_that_does_not_close_is_refused(tmp_path: Path) -> None:
    rows = _settled_rows()
    derivation = rows[0]["rates"]["derivation"]
    derivation["closure"][3]["posted_fee"] = "27.60"
    docs.rehash(derivation)

    with pytest.raises(baseline.BaselineValidationError, match="does not close"):
        _attach_settled(tmp_path, rows)


def test_a_one_cent_rounding_difference_closes_and_two_cents_do_not(
    tmp_path: Path,
) -> None:
    rows = _settled_rows()
    derivation = rows[0]["rates"]["derivation"]
    # 9383.39 x 0.035 / 12 = 27.3669..., 27.37 to the cent; 27.36 is one cent away.
    derivation["closure"][2]["posted_fee"] = "27.36"
    docs.rehash(derivation)
    settled = _attach_settled(tmp_path, rows)
    assert settled["fee_rates"]["derivation"]["max_abs_residual_usd"] == "0.01"

    derivation["closure"][2]["posted_fee"] = "27.35"
    docs.rehash(derivation)
    with pytest.raises(baseline.BaselineValidationError, match="does not close"):
        _attach_settled(tmp_path, rows)


def test_the_closure_rows_are_bound_to_their_hash(tmp_path: Path) -> None:
    rows = _settled_rows()
    rows[0]["rates"]["derivation"]["closure"][0]["fund_value"] = "10201.31"

    with pytest.raises(baseline.BaselineValidationError, match="closure_sha256"):
        _attach_settled(tmp_path, rows)


def test_the_proved_rate_is_the_tier_rows_own_not_the_records(tmp_path: Path) -> None:
    # The same closure rows no longer close when the documented rate is 4%: the rate
    # is read from the tier table, never from the derivation record.
    rows = _settled_rows()
    table = rows[0]["rates"]["tier_table"]
    flat = next(row for row in table if row["tier"] == "NON_QUALIFIED_FLAT")
    flat["management_rate"] = "0.04"

    with pytest.raises(baseline.BaselineValidationError, match="does not close"):
        _attach_settled(tmp_path, rows)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.update(tier="QUALIFIED_0_1M"),
        lambda d: d.update(performance_fee="20_PERCENT"),
        lambda d: d.update(hwm="OVER_HIGH_WATER_MARK"),
        lambda d: d.update(tolerance_usd="0.02"),
        lambda d: d.update(periods_per_year=7),
        lambda d: d.update(kind="OWNER_STATEMENT"),
        lambda d: d.update(evidence=[]),
        lambda d: d.update(closure=[], closure_sha256="0" * 64),
    ],
)
def test_a_derivation_that_does_not_settle_the_selected_tier_is_refused(
    tmp_path: Path, edit: Any
) -> None:
    rows = _settled_rows()
    edit(rows[0]["rates"]["derivation"])

    with pytest.raises(baseline.BaselineValidationError):
        _attach_settled(tmp_path, rows)


def test_a_derivation_cannot_claim_no_performance_fee_where_one_is_documented(
    tmp_path: Path,
) -> None:
    rates = docs.volatis_rates("QUALIFIED_0_1M")
    rates["derivation"] = docs.volatis_derivation(rates, "QUALIFIED_0_1M")
    rows = [docs.evidence_row("Volatis", rates)]

    with pytest.raises(baseline.BaselineValidationError, match="documents one"):
        _attach(tmp_path, rows, _costs("documented_rate_percent")[:1])


def test_an_unsettled_tier_cannot_carry_a_derivation(tmp_path: Path) -> None:
    rates = docs.volatis_rates("TIER_UNSETTLED")
    rates["derivation"] = docs.volatis_derivation(rates, "NON_QUALIFIED_FLAT")
    rows = [docs.evidence_row("Volatis", rates)]

    with pytest.raises(baseline.BaselineValidationError, match="settle the selected"):
        _attach(tmp_path, rows, _costs("documented_rate_tier_unsettled")[:1])
