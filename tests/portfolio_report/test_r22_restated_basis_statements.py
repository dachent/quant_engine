"""Statements of a restated panel that named the wrong basis (R2.2 review findings B1-B4).

Each test states what the payload must say about the sleeve-restated AI Factor row and
fails on the text or the field the R2.2 candidate shipped.
"""

from __future__ import annotations

from typing import Any

import portfolio_implementation_sleeve_basis as sbm
import portfolio_performance_tab as perf
import portfolio_report as reporter
import strategy_basis

PARENT = strategy_basis.BASIS_PARENT_ACCOUNT


def test_b1_a_restated_residual_is_the_closed_epochs_cash_quoted_from_the_data() -> (
    None
):
    text = perf._residual_treatment(True, 381, True)
    assert (
        "value of closed epochs that no plotted slice holds (3.81 at the cutoff)"
        in text
    )
    assert "liability line of strategy_basis_disclosure" in text
    # the old sentence called the residual the parent's unallocated cash and margin
    assert "value outside the plotted epochs" not in text
    assert "(0.00 at the cutoff)" in perf._residual_treatment(True, 0, True)


def test_b1_the_unrestated_text_is_unchanged() -> None:
    for sleeves, cents, restated in (
        (True, 381, False),
        (False, 381, True),
        (True, None, True),
    ):
        assert (
            perf._residual_treatment(sleeves, cents, restated)
            == perf.SLICE_RESIDUAL_TREATMENT
        )


def _cost() -> dict[str, Any]:
    return {
        "execution_cost": {
            "basis": "LEDGER_TRANSACTION_COMMISSIONS_NATIVE_CURRENCY",
            "by_currency": [
                {"amount": "2227.51", "currency": "USD", "record_count": 4117}
            ],
            "record_count": 4117,
            "status": "AVAILABLE",
            "window_end": "2026-09-14",
            "window_start": "2026-01-26",
        }
    }


def test_b2_a_restated_row_names_its_execution_cost_as_the_parents() -> None:
    tagged = strategy_basis.tag_parent_execution_cost(_cost())
    execution = tagged["execution_cost"]
    assert execution["scope"] == PARENT
    assert "4117 records, 2026-01-26..2026-09-14" in execution["scope_label"]
    (component,) = reporter.cost_components(tagged)
    assert component["scope"] == PARENT
    assert component["source_label"].startswith("parent account ")
    # an untagged row (every unrestated strategy) is unchanged
    (plain,) = reporter.cost_components(_cost())
    assert "scope" not in plain and not plain["source_label"].startswith("parent")
    assert strategy_basis.tag_parent_execution_cost({"execution_cost": None}) == {
        "execution_cost": None
    }


def test_b2_the_not_restated_list_and_label_name_the_execution_cost() -> None:
    assert strategy_basis.NOT_RESTATED == (
        "execution_cost",
        "model_vs_actual",
        "implementation_reconciliation_trades",
    )
    sentence = strategy_basis.not_restated_sentence()
    assert "the model-versus-actual windows, epochs and portfolio bridge" in sentence
    assert "commissions, broker execution join and trade reconciliation" in sentence
    assert strategy_basis.not_restated_sentence().startswith(
        "Measurements still on the parent account basis: the execution cost"
    )


def test_b3_the_parent_live_summary_and_overlap_carry_their_basis() -> None:
    reference = {
        "attribution_contract_sha256": "a" * 64,
        "reconciliation_contract_sha256": "b" * 64,
    }
    block = {
        "account_basis_ref": reference,
        "basis_label": "sleeve",
        "model_leg": {},
        "sleeve_leg": {},
        "strategy_id": "s",
        "return_reconciliation": {"status": "AVAILABLE"},
    }
    summary = {
        "cumulative_return": 0.0245,
        "observations": 171,
        "observed_start": "2026-01-22",
    }
    _, reconciled = sbm.apply_to_rows(
        block,
        None,
        {
            "strategy_id": "s",
            "account_live_return_summary": summary,
            "return_reconciliation": {"status": "AVAILABLE"},
        },
    )
    assert reconciled is not None
    assert reconciled["account_live_return_summary"] == {**summary, "basis": PARENT}
    assert "basis" not in summary
    _, without = sbm.apply_to_rows(block, None, {"strategy_id": "s"})
    assert without is not None and "account_live_return_summary" not in without


def test_b3_the_provider_overlap_of_a_restated_row_is_named_the_parents() -> None:
    restatement = strategy_basis.sleeve_restatement(
        strategy_id="s",
        account_id="acct-s2",
        parent_account_id="acct",
        provider_id="p",
        links=[
            {
                "start_date": "2026-07-02",
                "end_date": "2026-07-03",
                "factor": "1.02",
                "ending_nav": "102",
                "ending_flow": "0",
            }
        ],
    )
    overlap = {
        "account_cumulative_return": "0.001976",
        "status": "TESTED_EXACT_DATE_PATHS",
    }
    evidence = [
        {
            "strategy_id": "s",
            "evidence_layer": "ACCOUNT_LIVE",
            "account": {"account_scopes": [{"account_id": "acct"}]},
            "overlap": overlap,
        },
        {"strategy_id": "t", "evidence_layer": "ACCOUNT_LIVE", "overlap": overlap},
    ]
    result = strategy_basis.restate_provider_evidence(evidence, {"s": restatement})
    assert result[0]["overlap"] == {**overlap, "basis": PARENT}
    assert result[1] == evidence[1] and "basis" not in overlap


def _retained_row() -> dict[str, Any]:
    return {
        "strategy_id": "s",
        "costs": {"account_commissions_and_taxes_usd": 2227.5, "status": "PRE_TAX"},
        "broker_execution_join": {"accepted_execution_rows": 4117},
        "trade_reconciliation_union": {"actual_event_count": 3593},
        "trade_classification": {"UNEXPLAINED": 3593},
        "trade_reconciliation_by_book": {"PRIMARY": {"actual_event_count": 3593}},
        "model_book_inventory": [{"name": "book", "model_event_count": 50}],
        "root_causes": [
            {"evidence": "at least 3593 broker events remain unexplained."},
            {"evidence": "unrelated"},
        ],
    }


def test_b2_the_parent_ledger_and_broker_blocks_of_a_sleeve_row_name_the_parent() -> (
    None
):
    row = _retained_row()
    block = {
        "account_basis_ref": {
            "attribution_contract_sha256": "a" * 64,
            "reconciliation_contract_sha256": "b" * 64,
        },
        "basis_label": "sleeve",
        "model_leg": {},
        "sleeve_leg": {},
        "strategy_id": "s",
        "return_reconciliation": {},
    }
    _, reconciled = sbm.apply_to_rows(block, None, row)
    assert reconciled is not None
    for key in ("costs", "broker_execution_join", "trade_reconciliation_union"):
        assert reconciled[key]["basis"] == PARENT
    assert reconciled["parent_account_blocks"] == {
        "basis": PARENT,
        "blocks": [
            "costs",
            "broker_execution_join",
            "trade_reconciliation_union",
            "trade_classification",
            "trade_reconciliation_by_book",
            "model_book_inventory",
        ],
    }
    assert (
        "parent-account broker events remain"
        in (reconciled["root_causes"][0]["evidence"])
    )
    # the retained row is untouched
    assert row == _retained_row()


def test_b4_a_restated_strategys_evidence_layers_carry_its_sleeve_dates() -> None:
    layers = [
        {
            "evidence_layer": "ACCOUNT_LIVE",
            "observed_start": "2026-01-22",
            "observed_end": "2026-07-14",
            "artifact_sha256": "old",
        },
        {
            "evidence_layer": "BACKTEST",
            "observed_start": "2020-05-13",
            "observed_end": "2026-01-21",
            "artifact_sha256": "bt",
        },
        {"evidence_layer": "PROVIDER_LIVE", "observed_start": None},
    ]
    restated = reporter._restated_layer_inventory(
        layers,
        start="2026-02-02",
        end="2026-09-17",
        series_sha256="new",
        backtest_end="2026-01-30",
    )
    live, backtest, provider = restated
    assert (live["observed_start"], live["observed_end"]) == (
        "2026-02-02",
        "2026-09-17",
    )
    assert live["artifact_sha256"] == "new"
    assert backtest["observed_end"] == "2026-01-30"
    assert backtest["artifact_sha256"] == "bt"
    assert provider == layers[2]
    assert layers[0]["observed_start"] == "2026-01-22"


def test_b4_the_prehistory_end_is_the_last_admitted_backtest_day_before_live() -> None:
    version = "v"
    history = {
        "segments": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "mode_admission": "INCLUDE_SELECTED_LIVE",
                "strategy_version": version,
                "observed_start": "2026-02-02",
                "observed_end": "2026-09-17",
                "return_observations": 10,
            },
            {
                "evidence_layer": "BACKTEST",
                "mode_admission": "INCLUDE_VERSION_COMPATIBLE_PREHISTORY",
                "strategy_version": version,
                "observed_start": "2020-05-13",
                "observed_end": "2026-01-30",
                "return_observations": 1493,
            },
        ]
    }
    assert reporter._accepted_pre_live_end(history, version) == "2026-01-30"
    assert reporter._accepted_pre_live_end(history, "other") is None


def test_gb1_the_internal_cancellation_identity_closes_on_a_rounding_boundary() -> None:
    from datetime import date
    from decimal import Decimal

    day = date(2026, 6, 22)
    epoch = {
        "flow": {},
        "first_end": day,
        "skip_opening": False,
        "first": day,
        "opening": Decimal(0),
        "last": date(2026, 6, 21),
        "nav": {},
        # in, out and net each round to cents on their own: 0.006 -> 1, 0.004 -> 0, 0.002 -> 0
        "legs": [
            {"class": "INTERNAL_IN", "amount": "0.006"},
            {"class": "INTERNAL_OUT", "amount": "0.004"},
        ],
    }
    flows = perf._build_flows([epoch], [day], {"s": [0]})
    cancel = flows["internal_cancellation"]
    assert (cancel["internal_in_cents"], cancel["internal_out_cents"]) == (1, 0)
    assert cancel["net_cents"] == 0
    assert cancel["rounding_cents"] == -1
    assert (
        cancel["internal_in_cents"]
        - cancel["internal_out_cents"]
        + cancel["rounding_cents"]
        == cancel["net_cents"]
    )
