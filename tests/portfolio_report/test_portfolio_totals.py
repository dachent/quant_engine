"""PANEL-VIS-002 (#593): portfolio-level TWR, MWR and the capital reconciliation."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
import pytest

import portfolio_totals as totals
import portfolio_totals_verify as verify

CUTOFF = "2026-02-28"
SCHEMAS = Path(__file__).parents[2] / "schemas" / "portfolio"


def _row(
    account: str,
    strategy: str,
    start: str,
    end: str | None,
    *,
    last_nav: str,
    basis: str,
    opening: str,
    deposits: str = "0",
    current: str,
    links: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    net = Decimal(opening) + Decimal(deposits)
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": start,
        "effective_end": end,
        "start": start,
        "end": last_nav,
        "status": "OK",
        "opening_basis": basis,
        "opening_capital": opening,
        "deposits": deposits,
        "internal_in": "0",
        "external_withdrawals": "0",
        "internal_out": "0",
        "net_capital": str(net),
        "current_value": current,
        "gain": str(Decimal(current) - net),
        "observation_trace": {"links": links or []},
    }


def _event(
    epoch: str, strategy: str, cls: str, amount: str, day: str
) -> dict[str, Any]:
    return {
        "epoch_id": epoch,
        "strategy_id": strategy,
        "classification": cls,
        "date": day,
        "invested_capital_effect_usd": amount,
    }


def _case(*, successor_start: str = "2026-02-01") -> dict[str, Any]:
    """Account A hands epoch 1 (S1) off to epoch 2 (S2); account B is a sleeve."""

    rows = [
        _row(
            "A",
            "S1",
            "2026-01-01",
            "2026-01-31",
            last_nav="2026-01-31",
            basis="FIRST_AVAILABLE_NAV",
            opening="1000",
            current="1050",
        ),
        _row(
            "A",
            "S2",
            successor_start,
            None,
            last_nav=CUTOFF,
            basis="FIRST_AVAILABLE_NAV",
            opening="1040",
            current="1200",
        ),
        _row(
            "B",
            "S3",
            "2026-01-01",
            None,
            last_nav=CUTOFF,
            basis="PROVIDER_CAPITAL_ANCHOR",
            opening="500",
            deposits="100",
            current="700",
        ),
    ]
    rows[0]["effective_end"] = "2026-01-31"
    capital = {
        "required_epochs": [
            {
                "epoch_id": "e1",
                "account_id": "A",
                "strategy_id": "S1",
                "effective_start": "2026-01-01",
                "effective_end": "2026-01-31",
            },
            {
                "epoch_id": "e2",
                "account_id": "A",
                "strategy_id": "S2",
                "effective_start": successor_start,
                "effective_end": None,
            },
            {
                "epoch_id": "e3",
                "account_id": "B",
                "strategy_id": "S3",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
        ],
        "events": [
            _event("e1", "S1", "OPENING_CONTRIBUTED_BASIS", "900", "2026-01-01"),
            _event("e1", "S1", "PREDECESSOR_TRANSITION", "-900", successor_start),
            _event("e2", "S2", "PREDECESSOR_TRANSITION", "900", successor_start),
            _event("e3", "S3", "OPENING_CONTRIBUTED_BASIS", "500", "2026-01-01"),
            _event("e3", "S3", "EXTERNAL_CONTRIBUTION", "90", "2026-01-15"),
        ],
        "current_aggregate_usd": "1490",
    }
    return {"rows": rows, "capital": capital}


def _proof(last_index: str = "110") -> dict[str, Any]:
    return {
        "status": "AVAILABLE",
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "return_basis": "LEVERED_NET_NAV_AFTER_FINANCING_COST_FLOW_NEUTRAL",
        "policy_id": "PORT-HWM-4",
        "receipt_sha256": "0" * 64,
        "current_index": last_index,
        "index_path": [
            {"as_of": "2026-01-01", "index": "100", "hwm": "100", "drawdown": "0"},
            {"as_of": CUTOFF, "index": last_index, "hwm": last_index, "drawdown": "0"},
        ],
    }


def _build(case: dict[str, Any], proof: dict[str, Any] | None = None) -> dict[str, Any]:
    return totals.build_portfolio_totals(
        rows=case["rows"],
        invested_capital=case["capital"],
        governed_hwm_proof=proof if proof is not None else _proof(),
        hwm_summary={"governed_perimeter_value_usd": "1900"},
        calculation_cutoff_date=CUTOFF,
    )


def _amounts(block: dict[str, Any]) -> dict[str, str]:
    return {c["component_id"]: c["amount_usd"] for c in block["components"]}


def test_capital_reconciliation_names_components_summing_to_the_difference() -> None:
    block = _build(_case())["capital_reconciliation"]
    assert block["status"] == "RECONCILED"
    # Current of the open epochs (1200 + 700) minus every gain (50 + 160 + 100).
    assert block["derived_total_net_capital_usd"] == "1590"
    assert block["governed_invested_capital_usd"] == "1490"
    assert block["difference_usd"] == "100"
    assert _amounts(block) == {
        "ACCOUNT_NAV_BASIS_VS_GOVERNED_SLEEVE_CAPITAL": "100",
        "SLEEVE_ALLOCATION_BASIS_DIFFERENCE": "10",
        "OTHER_OPENING_BASIS_DIFFERENCE": "0",
        "EPOCH_HANDOFF_BOUNDARY_NAV_GAP": "-10",
        "CLOSED_EPOCH_VALUE_NOT_HANDED_OFF": "0",
        "OTHER_GOVERNED_EVENT_CLASSES": "0",
    }
    assert sum(Decimal(v) for v in _amounts(block).values()) == Decimal("100")
    assert block["residual_usd"] == "0"
    # The closed epoch's hand-off is not double counted: the naive sum of epoch
    # net capital differs from the derived total by exactly the closed closing value.
    naive = sum(Decimal(row["net_capital"]) for row in _case()["rows"])
    assert naive - Decimal(block["derived_total_net_capital_usd"]) == Decimal("1050")


def test_closed_epoch_without_successor_is_its_own_component() -> None:
    block = _build(_case(successor_start="2026-02-05"))["capital_reconciliation"]
    amounts = _amounts(block)
    assert amounts["CLOSED_EPOCH_VALUE_NOT_HANDED_OFF"] == "-1050"
    assert amounts["EPOCH_HANDOFF_BOUNDARY_NAV_GAP"] == "0"
    assert sum(Decimal(v) for v in amounts.values()) == Decimal(block["difference_usd"])


def test_a_residual_fails_closed_and_names_itself() -> None:
    case = _case()
    case["rows"][2]["gain"] = "101"  # row identity broken: gain != current - net
    with pytest.raises(totals.PortfolioTotalsError, match=r"residual -1 USD"):
        _build(case)


def test_governed_events_that_disagree_with_the_aggregate_fail_closed() -> None:
    case = _case()
    case["capital"]["current_aggregate_usd"] = "1491"
    with pytest.raises(totals.PortfolioTotalsError, match="does not close"):
        _build(case)


def test_unmatched_perimeter_is_unavailable_with_a_reason() -> None:
    case = _case()
    case["capital"]["required_epochs"].pop()
    block = _build(case)["capital_reconciliation"]
    assert block["status"] == "UNAVAILABLE"
    assert block["reason"] == "EPOCH_ROWS_DIFFER_FROM_GOVERNED_EPOCHS"


def test_headline_twr_is_the_governed_hwm_index() -> None:
    block = _build(_case())["portfolio_twr_cumulative"]
    assert block["status"] == "AVAILABLE"
    assert block["basis"] == "GOVERNED_HWM_INDEX"
    assert block["value"] == "0.1"
    assert (block["window_start"], block["window_end"], block["window_days"]) == (
        "2026-01-01",
        CUTOFF,
        58,
    )
    assert block["financial_acceptance"] == "PENDING_OWNER_ACCEPTANCE"
    # No admitted epoch traces here: the summed-epoch TWR says so by name.
    assert block["summed_epoch_twr"]["status"] == "UNAVAILABLE"
    assert block["headline_minus_summed_epoch_twr"] is None


def test_summed_epoch_twr_and_the_difference_when_grids_align() -> None:
    case = _case()
    link = {
        "base_nav": "1000",
        "beginning_flow": "0",
        "factor": "1.1",
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
    }
    link2 = {**link, "start_date": "2026-01-02", "end_date": "2026-01-03"}
    case["rows"][2]["observation_trace"] = {"links": [link, link2]}
    block = _build(case)["portfolio_twr_cumulative"]
    assert block["summed_epoch_twr"]["status"] == "AVAILABLE"
    assert Decimal(block["summed_epoch_twr"]["value"]) == Decimal("0.21")
    assert Decimal(block["headline_minus_summed_epoch_twr"]) == Decimal(
        "0.1"
    ) - Decimal("0.21")


def test_twr_is_unavailable_without_a_governed_proof() -> None:
    block = totals.build_portfolio_totals(
        rows=_case()["rows"],
        invested_capital=_case()["capital"],
        governed_hwm_proof=None,
        hwm_summary={"governed_perimeter_value_usd": "1900"},
        calculation_cutoff_date=CUTOFF,
    )["portfolio_twr_cumulative"]
    assert (
        block["status"] == "UNAVAILABLE" and block["reason"] == "NO_GOVERNED_HWM_PROOF"
    )


def test_mwr_is_not_annualised_under_365_days() -> None:
    block = _build(_case())["portfolio_mwr_annualised"]
    assert block["status"] == "AVAILABLE"
    assert block["value"] is None
    assert block["annualisation_basis"] == "NOT_ANNUALISED_WINDOW_UNDER_365_DAYS"
    assert block["window_days"] == 58
    # 1000 invested on 2026-01-01 (900 + 500 + 90 governed external capital).
    assert Decimal(block["window_equivalent_cumulative"]) > 0


def test_mwr_annualises_from_365_days() -> None:
    capital = {
        "events": [
            _event("e", "S", "OPENING_CONTRIBUTED_BASIS", "1000", "2025-01-01"),
        ],
    }
    block = totals.build_portfolio_mwr(capital, Decimal("1100"), "2026-01-01")
    assert block["window_days"] == 365
    assert block["annualisation_basis"] == "ANNUALISED_XIRR_WINDOW_AT_LEAST_365_DAYS"
    assert abs(Decimal(block["value"]) - Decimal("0.1")) < Decimal("1e-20")
    assert abs(
        Decimal(block["window_equivalent_cumulative"]) - Decimal("0.1")
    ) < Decimal("1e-20")


def test_nothing_is_published_without_epoch_rows_or_capital() -> None:
    assert (
        totals.build_portfolio_totals(
            rows=[],
            invested_capital=_case()["capital"],
            governed_hwm_proof=None,
            hwm_summary=None,
            calculation_cutoff_date=CUTOFF,
        )
        == {}
    )
    assert (
        totals.build_portfolio_totals(
            rows=_case()["rows"],
            invested_capital=None,
            governed_hwm_proof=None,
            hwm_summary=None,
            calculation_cutoff_date=CUTOFF,
        )
        == {}
    )


@pytest.mark.parametrize("schema_file", ["report-data", "control-panel-data"])
def test_blocks_validate_against_both_schemas(schema_file: str) -> None:
    schema = json.loads((SCHEMAS / f"{schema_file}.schema.json").read_text())
    published = _build(_case())
    for name, block in published.items():
        wrapper = {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}
        errors = list(Draft202012Validator(wrapper).iter_errors(block))
        assert errors == [], (name, [e.message for e in errors])
        assert name in schema["properties"]
    unavailable = {"schema_version": 1, "status": "UNAVAILABLE"}
    wrapper = {"$ref": "#/$defs/capital_reconciliation", "$defs": schema["$defs"]}
    assert list(Draft202012Validator(wrapper).iter_errors(unavailable))
    tampered = deepcopy(published["capital_reconciliation"])
    tampered["residual_usd"] = "1"
    assert list(Draft202012Validator(wrapper).iter_errors(tampered))


def _verification_inputs(
    case: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    published = _build(case)
    report = {
        "invested_capital": case["capital"],
        "hwm_summary": {"governed_perimeter_value_usd": "1900"},
        "hwm_governed_proof": _proof(),
        **published,
    }
    manifest = {"rows": case["rows"]}
    crosswalk = {
        "calculation_cutoff_date": CUTOFF,
        "rows": [
            {
                "account_id": r["account_id"],
                "strategy_id": r["strategy_id"],
                "effective_start": r["effective_start"],
                "effective_end": r["effective_end"],
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
            for r in case["rows"]
        ],
    }
    return report, manifest, crosswalk


def test_independent_verifier_accepts_the_producer_output() -> None:
    verify.verify_portfolio_totals(*_verification_inputs(_case()))
    verify.verify_portfolio_totals(
        *_verification_inputs(_case(successor_start="2026-02-05"))
    )


def test_independent_verifier_rejects_each_tampered_total() -> None:
    report, manifest, crosswalk = _verification_inputs(_case())
    for mutate in (
        lambda r: r["capital_reconciliation"]["components"][0].update(amount_usd="101"),
        lambda r: r["capital_reconciliation"].update(difference_usd="99"),
        lambda r: r["capital_reconciliation"]["components"][0]["lines"][0].update(
            amount_usd="1"
        ),
        lambda r: r["portfolio_twr_cumulative"].update(value="0.11"),
        lambda r: r["portfolio_mwr_annualised"].update(
            window_equivalent_cumulative="0.5"
        ),
        lambda r: r.pop("capital_reconciliation"),
    ):
        broken = deepcopy(report)
        mutate(broken)
        with pytest.raises(verify.PortfolioTotalsVerificationError):
            verify.verify_portfolio_totals(broken, manifest, crosswalk)


def test_verifier_solver_reaches_rates_above_one_thousand() -> None:
    from datetime import date

    flows = [
        (date(2026, 1, 1), Decimal("-1000")),
        (date(2026, 1, 31), Decimal("100000")),
    ]
    rate = verify._solve_rate(flows)
    assert rate is not None and rate > Decimal(1000)
    producer = totals.build_portfolio_mwr(
        {
            "events": [
                _event("e", "S", "OPENING_CONTRIBUTED_BASIS", "1000", "2026-01-01")
            ]
        },
        Decimal("100000"),
        "2026-01-31",
    )
    assert producer["status"] == "AVAILABLE"
    assert abs(
        Decimal(producer["window_equivalent_cumulative"]) - Decimal("99")
    ) < Decimal("1e-15")


def test_verifier_rejects_a_suppressed_mwr() -> None:
    report, manifest, crosswalk = _verification_inputs(_case())
    report["portfolio_mwr_annualised"] = {
        "schema_version": 1,
        "status": "UNAVAILABLE",
        "reason": "NO_GOVERNED_PERIMETER_VALUE",
    }
    with pytest.raises(verify.PortfolioTotalsVerificationError, match="unique root"):
        verify.verify_portfolio_totals(report, manifest, crosswalk)


def test_a_derived_sleeve_epoch_stands_for_its_residual_parent_in_the_totals() -> None:
    """#567: sleeves of product strategies join the totals, other sleeves do not."""

    crosswalk = {
        "rows": [
            {
                "account_id": "acct-parent",
                "strategy_id": "alpha",
                "effective_start": "2026-01-22",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
            {
                "account_id": "acct-other",
                "strategy_id": "beta",
                "effective_start": "2026-01-05",
                "effective_end": None,
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            },
        ]
    }

    def row(account: str, strategy: str, start: str, end: str | None) -> dict[str, Any]:
        return {
            "account_id": account,
            "strategy_id": strategy,
            "effective_start": start,
            "effective_end": end,
        }

    manifest = {
        "rows": [
            row("acct-parent-s2", "alpha", "2026-01-30", None),
            row("acct-parent-s1", "history", "2026-01-26", "2026-06-23"),
            row("acct-other", "beta", "2026-01-05", None),
        ]
    }
    rows = verify.included_rows(manifest, crosswalk)
    assert {item["account_id"] for item in rows} == {"acct-parent-s2", "acct-other"}


def test_a_derived_product_epoch_reconciles_and_the_verifier_agrees() -> None:
    """R2: capital is the product epochs' capital, so the totals close over them.

    Account B is a residual parent; its sleeve ``B-s2`` carries strategy S3 as the
    derived product epoch and ``B-s1`` is retained history.  The governed capital
    names the derived epoch (gross allocations into it), the manifest rows the
    producer reads are the product rows, and the independent verifier re-derives
    the same product rows from the manifest and the crosswalk.
    """

    case = _case()
    derived = deepcopy(case["rows"][2])
    derived["account_id"] = "B-s2"
    retained = deepcopy(case["rows"][2])
    retained.update(account_id="B-s1", strategy_id="history", end="2026-01-20")
    retained["effective_end"] = "2026-01-20"
    product_rows = [case["rows"][0], case["rows"][1], derived]
    case["rows"] = product_rows
    case["capital"]["required_epochs"][2]["account_id"] = "B-s2"

    block = _build(case)["capital_reconciliation"]
    assert block["status"] == "RECONCILED"
    assert block["epoch_count"] == 3
    assert {line["account_id"] for c in block["components"] for line in c["lines"]} == {
        "A",
        "B-s2",
    }

    report, _manifest, _crosswalk = _verification_inputs(case)
    manifest = {"rows": [*product_rows, retained]}
    baseline = [
        {
            "account_id": r["account_id"],
            "strategy_id": r["strategy_id"],
            "effective_start": r["effective_start"],
            "effective_end": r["effective_end"],
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        }
        for r in (*case["rows"][:2], {**derived, "account_id": "B"})
    ]
    crosswalk = {"calculation_cutoff_date": CUTOFF, "rows": baseline}
    assert [r["account_id"] for r in verify.included_rows(manifest, crosswalk)] == [
        "A",
        "A",
        "B-s2",
    ]
    verify.verify_portfolio_totals(report, manifest, crosswalk)
