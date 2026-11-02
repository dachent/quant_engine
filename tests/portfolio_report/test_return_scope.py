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


def test_the_scope_covers_every_field_the_parent_series_or_its_capital_drives() -> None:
    """R-9: drawdown, weight, cost denominator and year returns follow the same series."""

    report: dict[str, Any] = {"strategy_summary": [{"strategy_id": "p123-ai-factor"}]}
    sleeves = {f"{PARENT}-s{n}": PARENT for n in (1, 2, 3)}
    report_module._apply_return_scope(report, _rows(), sleeves, ["p123-ai-factor"])
    applies = report["strategy_summary"][0]["return_scope"]["applies_to"]
    for field in (
        "twr",
        "account_live_max_drawdown",
        "hwm_max_drawdown",
        "hwm_current_drawdown",
        "systematic_weight",
        "cost_rate_denominator_usd",
        "ytd_twr",
        "calendar_year_twr",
        "account_live_cagr",
    ):
        assert field in applies


def test_a_retained_strategy_carries_the_economic_exit_day_and_how_its_end_is_dated() -> (
    None
):
    """R-4: retired_on is the NAV recognition day; the row also says when the capital left."""

    manifest_rows = [
        {
            "strategy_id": "retired-one",
            "economic_end_date": "2024-12-12",
            "end_recognition_basis": "NAV_RECOGNITION_DAY_AFTER_EXIT_FILL",
        },
        {"strategy_id": "retired-one", "economic_end_date": None},
        {"strategy_id": "open", "economic_end_date": None},
    ]
    assert report_module._economic_end_fields("retired-one", manifest_rows) == {
        "economic_end_date": "2024-12-12",
        "end_recognition_basis": "NAV_RECOGNITION_DAY_AFTER_EXIT_FILL",
    }
    assert report_module._economic_end_fields("open", manifest_rows) == {
        "economic_end_date": None,
        "end_recognition_basis": None,
    }


def test_the_mixed_basis_disclosure_is_stated_with_a_bridge_that_foots() -> None:
    """T6: the row says it mixes bases, names the ruling and release, and the printed terms sum."""

    import re
    from decimal import Decimal

    item = {
        "strategy_id": "p123-ai-factor",
        "return_window_start": "2030-01-02",
        "return_close_date": "2030-09-03",
        "window_days": 244,
        "mwr_opening_date": "2030-01-10",
        "mwr_terminal_date": "2030-09-03",
        "annualisation_basis": {"mwr_xirr": {"window_days": 236}},
    }
    report: dict[str, Any] = {"strategy_summary": [item]}
    sleeves = {f"{PARENT}-s{n}": PARENT for n in (1, 2, 3)}
    report_module._apply_return_scope(report, _rows(), sleeves, ["p123-ai-factor"])
    label = item["return_scope_label"]
    disclosure = item["return_scope"]["mixed_basis_disclosure"]

    assert "MIXED BASIS: this row mixes account and sleeve bases" in label
    assert "2030-01-02 to 2030-09-03, 244 days" in label
    assert (
        "parent account's (2030-01-02" in label
        and "sleeve epoch's (2030-01-10" in label
    )
    assert "Epoch rows and the dollar-value chart show the sleeve's own values" in label
    assert "[source: ADR 0002 / #624]" in label
    assert "2030-01-10 to 2030-09-03, 236 days" in label
    assert "owner ruled" in label and "restated to the sleeve basis" in label
    assert f"release {report_module.MIXED_BASIS_SCHEDULED_RELEASE}" in label
    assert disclosure["status"] == "MIXED_BASIS"
    assert disclosure["scheduled_release"] == "R2.2"
    # footing: the terms as printed in the label sum to the printed net
    bridge = re.search(r"points: (.*?) \(net ([+-][\d.]+)\)", label)
    assert bridge is not None
    terms = [Decimal(m) for m in re.findall(r" ([+-]\d+\.\d+)(?:,|$)", bridge.group(1))]
    assert len(terms) == len(report_module.MIXED_BASIS_BRIDGE_POINTS) == 3
    assert sum(terms) == Decimal(bridge.group(2)) == Decimal("1.086")
    # audit figure plus the net lands on the sleeve-basis return to within the terms' 0.001 rounding
    audit = Decimal(report_module.MIXED_BASIS_AUDIT_FIGURE_PERCENT)
    assert abs((audit + sum(terms)) - Decimal("15.435")) <= Decimal("0.001")
    assert [t["points"] for t in disclosure["bridge_points"]] == [
        f"{t:+}" for t in terms
    ]
