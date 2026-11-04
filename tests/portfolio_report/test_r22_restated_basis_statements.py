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


def test_b1_a_restated_residual_is_the_closed_epochs_cash_quoted_from_the_data() -> None:
    text = perf._residual_treatment(True, 381, True)
    assert "value of closed epochs that no plotted slice holds (3.81 at the cutoff)" in text
    assert "liability line of strategy_basis_disclosure" in text
    # the old sentence called the residual the parent's unallocated cash and margin
    assert "value outside the plotted epochs" not in text
    assert "(0.00 at the cutoff)" in perf._residual_treatment(True, 0, True)


def test_b1_the_unrestated_text_is_unchanged() -> None:
    for sleeves, cents, restated in ((True, 381, False), (False, 381, True), (True, None, True)):
        assert (
            perf._residual_treatment(sleeves, cents, restated)
            == perf.SLICE_RESIDUAL_TREATMENT
        )


def _cost() -> dict[str, Any]:
    return {
        "execution_cost": {
            "basis": "LEDGER_TRANSACTION_COMMISSIONS_NATIVE_CURRENCY",
            "by_currency": [{"amount": "2227.51", "currency": "USD", "record_count": 4117}],
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
    assert strategy_basis.NOT_RESTATED == ("execution_cost",)
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
    summary = {"cumulative_return": 0.0245, "observations": 171, "observed_start": "2026-01-22"}
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
    overlap = {"account_cumulative_return": "0.001976", "status": "TESTED_EXACT_DATE_PATHS"}
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
