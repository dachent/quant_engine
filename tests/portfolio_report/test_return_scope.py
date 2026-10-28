"""A strategy whose product epoch is a derived sleeve states whose series its return is (J1)."""

from __future__ import annotations

from typing import Any

import portfolio_report as report_module

PARENT = "account-1b44c987969b9e47"


def _rows() -> list[dict[str, Any]]:
    def row(account: str, strategy: str, end: str | None) -> dict[str, Any]:
        return {"account_id": account, "strategy_id": strategy, "effective_end": end}

    return [
        row(f"{PARENT}-s2", "p123-ai-factor", None),
        row(f"{PARENT}-s1", "retired-one", "2026-06-23"),
        row(f"{PARENT}-s3", "retired-two", "2026-07-22"),
        row("account-other", "p123-eu", None),
    ]


def test_the_row_names_the_parent_series_the_sleeve_epoch_and_the_retired_sleeves() -> (
    None
):
    report: dict[str, Any] = {
        "strategy_summary": [
            {"strategy_id": "p123-ai-factor"},
            {"strategy_id": "p123-eu"},
        ]
    }
    sleeves = {f"{PARENT}-s{n}": PARENT for n in (1, 2, 3)}
    report_module._apply_return_scope(report, _rows(), sleeves, ["p123-ai-factor"])
    scope = report["strategy_summary"][0]["return_scope"]
    assert scope["basis"] == "PARENT_ACCOUNT_LIVE_SERIES"
    assert scope["parent_account_ids"] == [PARENT]
    assert scope["sleeve_epoch_accounts"] == [f"{PARENT}-s2"]
    assert scope["includes_retired_sleeve_accounts"] == [f"{PARENT}-s1", f"{PARENT}-s3"]
    assert scope["mwr_and_governed_capital_basis"] == "SLEEVE_EPOCH"
    label = report["strategy_summary"][0]["return_scope_label"]
    assert "parent account" in label and "s1, s3" in label and "sleeve s2" in label
    # a strategy that is not a derived product epoch is untouched
    assert "return_scope" not in report["strategy_summary"][1]
