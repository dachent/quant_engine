"""E8 scope (#894/#895): leverage coverage applies to levered accounts.

An account is outside the check only when the report states ACCOUNT_FINANCING_STATE rows for
it and all read NOT_LEVERED (sprint-ledger's declaration); a missing declaration never narrows.
"""

from __future__ import annotations

from typing import Any

from test_validate_generation import Spec, build, run

STATE = {
    "data_class": "ACCOUNT_FINANCING",
    "measurement_basis": "ACCOUNT_FINANCING_STATE",
}


def _uncovered(report: dict[str, Any]) -> None:
    # Observed only: no account is covered for either measure.
    for row in report["leverage_disclosures"]:
        row["coverage_status"] = "OBSERVED_UNRECONCILED"


def _state(account: str, classification: str, reason: str | None = None):
    def mutate(report: dict[str, Any]) -> None:
        report["leverage_disclosures"].append(
            {
                "account_id": account,
                "strategy_id": None,
                **STATE,
                "classification": classification,
                "coverage_status": "COMPLETE_DECLARED",
                "coverage_reason": reason
                or (
                    "DECLARED_NO_MARGIN_AND_NO_DEBIT_EVIDENCE"
                    if classification == "NOT_LEVERED"
                    else "NET_BASE_CASH_NEGATIVE"
                ),
            }
        )

    return mutate


def _e8(tmp_path, *mutations):
    spec = Spec(report=[_uncovered, *mutations])
    return [
        f
        for f in run(build(tmp_path, spec))
        if f.check_id == "completeness.required_outputs" and f.defect_ids == ("E8",)
    ]


def _uncovered_accounts(findings) -> set[str]:
    return {a for f in findings for a in f.observed["uncovered_accounts"]}


def test_without_a_declaration_every_active_account_is_applicable(tmp_path):
    assert _uncovered_accounts(_e8(tmp_path)) == {"A1", "B1", "B2"}


def test_not_levered_accounts_leave_the_check(tmp_path):
    found = _e8(tmp_path, _state("A1", "NOT_LEVERED"), _state("B1", "NOT_LEVERED"))
    assert _uncovered_accounts(found) == {"B2"}


def test_levered_and_incidental_debit_stay_applicable(tmp_path):
    found = _e8(
        tmp_path,
        _state("A1", "LEVERED"),
        _state("B1", "INCIDENTAL_DEBIT"),
        _state("B2", "NOT_LEVERED"),
    )
    assert _uncovered_accounts(found) == {"A1", "B1"}


def test_all_accounts_not_levered_is_silent(tmp_path):
    found = _e8(tmp_path, *[_state(a, "NOT_LEVERED") for a in ("A1", "B1", "B2")])
    assert found == []


def test_an_unreadable_classification_does_not_narrow(tmp_path):
    found = _e8(
        tmp_path,
        _state("A1", "MAYBE"),
        _state("B1", "NOT_LEVERED"),
        _state("B2", "NOT_LEVERED"),
    )
    assert _uncovered_accounts(found) == {"A1"}


def test_a_not_levered_row_without_evidence_does_not_narrow(tmp_path):
    # An account with no debit evidence defaults to NOT_LEVERED in the builder; the
    # declaration narrows the check only when its own coverage is stated available.
    def unevidenced(report):
        report["leverage_disclosures"].append(
            {
                "account_id": "A1",
                "strategy_id": None,
                **STATE,
                "classification": "NOT_LEVERED",
                "coverage_status": "OBSERVED_UNRECONCILED",
            }
        )

    found = _e8(
        tmp_path, unevidenced, _state("B1", "NOT_LEVERED"), _state("B2", "NOT_LEVERED")
    )
    assert _uncovered_accounts(found) == {"A1"}


def test_a_not_levered_row_with_a_foreign_or_conflicted_reason_does_not_narrow(
    tmp_path,
):
    """Kill test (R6): the validator does not trust a producer's NOT_LEVERED label; the
    reason must be the declaration-plus-observed-no-debit evidence and carry no conflict."""

    found = _e8(
        tmp_path,
        _state("A1", "NOT_LEVERED", "SOMETHING_ELSE"),
        _state(
            "B1", "NOT_LEVERED", "DECLARED_NO_MARGIN_AND_NO_DEBIT_EVIDENCE;CONFLICT=X"
        ),
        _state("B2", "NOT_LEVERED"),
    )
    assert _uncovered_accounts(found) == {"A1", "B1"}


SETTLED = "SETTLED_DEBIT_NO_INTEREST"


def test_a_settled_debit_no_interest_account_leaves_the_check_like_not_levered(
    tmp_path,
):
    """Owner ruling (#894): a retirement settlement float is not borrowing; the one exception
    to the rule that INCIDENTAL_DEBIT stays applicable."""

    found = _e8(
        tmp_path,
        _state("A1", "INCIDENTAL_DEBIT", SETTLED),
        _state("B1", "NOT_LEVERED"),
        _state("B2", "NOT_LEVERED"),
    )
    assert found == []


def test_only_the_exact_settled_debit_state_leaves_the_check(tmp_path):
    found = _e8(
        tmp_path,
        _state("A1", "INCIDENTAL_DEBIT", SETTLED),
        _state(
            "B1", "INCIDENTAL_DEBIT"
        ),  # an incidental debit with another reason stays
        _state("B2", "LEVERED", SETTLED),  # the reason belongs to INCIDENTAL_DEBIT only
    )
    assert _uncovered_accounts(found) == {"B1", "B2"}


def test_a_margin_accounts_debit_makes_it_levered_and_applicable(tmp_path):
    found = _e8(
        tmp_path,
        _state("A1", "INCIDENTAL_DEBIT", SETTLED),
        _state("B1", "LEVERED", "NET_BASE_CASH_NEGATIVE"),
        _state("B2", "NOT_LEVERED"),
    )
    assert _uncovered_accounts(found) == {"B1"}


def test_the_state_does_not_borrow_the_not_levered_reasons(tmp_path):
    found = _e8(
        tmp_path,
        _state("A1", "INCIDENTAL_DEBIT", "DECLARED_NO_MARGIN_AND_NO_DEBIT_EVIDENCE"),
        _state("B1", "NOT_LEVERED"),
        _state("B2", "NOT_LEVERED"),
    )
    assert _uncovered_accounts(found) == {"A1"}


def test_a_conflicted_or_unverified_settled_debit_state_does_not_narrow(tmp_path):
    found = _e8(
        tmp_path,
        _state("A1", "INCIDENTAL_DEBIT", SETTLED + ";CONFLICT=X"),
        _state("B1", "INCIDENTAL_DEBIT", SETTLED + ";VERIFICATION=UNVERIFIED"),
        _state("B2", "NOT_LEVERED"),
    )
    assert _uncovered_accounts(found) == {"A1", "B1"}


def test_an_unavailable_settled_debit_state_row_does_not_narrow(tmp_path):
    def mutate(report):
        report["leverage_disclosures"].append(
            {
                "account_id": "A1",
                "strategy_id": None,
                **STATE,
                "classification": "INCIDENTAL_DEBIT",
                "coverage_status": "OBSERVED_UNRECONCILED",
                "coverage_reason": SETTLED,
            }
        )

    found = _e8(
        tmp_path, mutate, _state("B1", "NOT_LEVERED"), _state("B2", "NOT_LEVERED")
    )
    assert _uncovered_accounts(found) == {"A1"}
