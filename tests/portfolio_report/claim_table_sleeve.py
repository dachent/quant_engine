"""Claim-table rows for the AI Factor sleeve restatement (#624, option B).

The sentences the restatement adds to the report and the panel that make a universal,
a count or a basis claim, each stated over the payload.  A payload that restates no strategy
carries nothing these sentences describe, so every predicate SKIPs there; the synthetic
``pass_fixture`` of each row restates one strategy, and ``break_fixture`` breaks the claim.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from claim_table import (
    PANEL_HTML,
    REPORT_PY,
    Payload,
    Result,
    Row,
    bad,
    ok,
    skip,
)

TOTALS_PY = "tools/portfolio_totals.py"
SLEEVE = "SLEEVE_EPOCH"
_APPLIES = (
    "twr",
    "account_live_max_drawdown",
    "current_value",
    "weekly_series",
    "systematic_weight",
)


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _restated_rows(payload: Payload) -> list[dict[str, Any]]:
    return [
        row
        for row in payload.report.get("strategy_summary", [])
        if isinstance(row, dict)
        and isinstance(row.get("return_scope"), dict)
        and row["return_scope"].get("basis") == SLEEVE
    ]


def _restated_fixture(payload: Payload) -> None:
    """One restated strategy with the blocks the sentences describe (synthetic numbers)."""

    scope = {
        "basis": SLEEVE,
        "applies_to": list(_APPLIES),
        "mwr_and_governed_capital_basis": SLEEVE,
        "account_basis_ref": {
            "account_id": "acct-parent",
            "block": "account_summary.account_basis",
            "disclosure_block": "strategy_basis_disclosure",
            "label": "whole account",
            "series_sha256": "a" * 64,
        },
    }
    payload.report["strategy_summary"] = [
        {"strategy_id": "strategy:A", "return_scope": scope}
    ]
    payload.report["account_summary"] = [
        {
            "account_id": "acct-parent",
            "account_basis": {
                "strategy_id": "strategy:A",
                "series": {"schema": "ordered-date-return/1", "sha256": "a" * 64},
                "figures": {"account_live_cumulative_return": "0.0245"},
            },
        }
    ]
    payload.report["strategy_basis_disclosure"] = {
        "schema": "strategy-basis-disclosure/1",
        "strategies": [
            {
                "strategy_id": "strategy:A",
                "margin_liability": {"liability_line": {"value_usd": "-20"}},
                "bridges": {"account_to_sleeve": {}, "sleeve_to_audit": {}},
                "account_basis_ref": {"block": "account_summary.account_basis"},
            }
        ],
    }
    payload.report["strategy_attribution"] = {
        "benchmark_comparisons": [
            {
                "strategy_id": "strategy:A",
                "sleeve_basis": {
                    "metrics": {"observations": 100},
                    "closure": {
                        "common_observations": 100,
                        "sleeve_observations_in_window": 104,
                    },
                },
            }
        ],
        "comparisons": [],
    }
    payload.report["capital_reconciliation"] = {
        "residual_usd": "0",
        "memo": {
            "perimeter_restatement": {
                "in_difference_usd": False,
                "difference_usd": "20",
                "lines": [
                    {
                        "strategy_id": "strategy:A",
                        "sleeve_value_usd": "100",
                        "account_basis_value_usd": "80",
                        "difference_usd": "20",
                    }
                ],
            }
        },
    }
    payload.panel["strategies"] = [
        {
            "strategy_id": "strategy:A",
            "return_unlevered_cumulative": "0.15",
            "return_levered_cumulative": "0.17",
            "return_levered_peak_gross_to_equity": "1.6",
        }
    ]


# ---- the unlevered figure is never copied into the levered column -------------------


def p_levered_not_copied(payload: Payload) -> Result:
    rows = [
        row
        for row in payload.panel.get("strategies", [])
        if isinstance(row, dict)
        and row.get("return_levered_cumulative") is not None
        and row.get("return_unlevered_cumulative") is not None
    ]
    if not rows:
        return skip("no strategy row publishes a levered figure")
    copied = [
        str(row.get("strategy_id"))
        for row in rows
        if row["return_levered_cumulative"] == row["return_unlevered_cumulative"]
        and (_dec(row.get("return_levered_peak_gross_to_equity")) or Decimal(0)) > 1
    ]
    if copied:
        return bad(f"levered equals unlevered on a levered strategy: {copied}")
    return ok(
        f"{len(rows)} strategy rows publish distinct levered and unlevered figures"
    )


def b_levered_not_copied(payload: Payload) -> None:
    for row in payload.panel.get("strategies", []):
        row["return_levered_cumulative"] = row["return_unlevered_cumulative"]


# ---- the reference series are read on the dates they share with the sleeve path -------


def _lanes(payload: Payload) -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = []
    attribution = payload.report.get("strategy_attribution")
    if isinstance(attribution, dict):
        for key in ("benchmark_comparisons", "comparisons"):
            for row in attribution.get(key, []):
                lane = row.get("sleeve_basis") if isinstance(row, dict) else None
                if isinstance(lane, dict):
                    lanes.append(lane)
    return lanes


def p_shared_dates(payload: Payload) -> Result:
    lanes = _lanes(payload)
    if not lanes:
        return skip("no sleeve-basis comparison lane in the payload")
    off = [
        lane
        for lane in lanes
        if lane["metrics"].get("observations")
        != lane["closure"].get("common_observations")
        or lane["closure"].get("common_observations", 0)
        > lane["closure"].get("sleeve_observations_in_window", 0)
    ]
    if off:
        return bad(f"{len(off)} lanes count observations other than the shared dates")
    return ok(
        f"{len(lanes)} lanes: observations equal the shared dates inside the window"
    )


def b_shared_dates(payload: Payload) -> None:
    for lane in _lanes(payload):
        lane["metrics"]["observations"] = lane["closure"]["common_observations"] + 1


# ---- the row's basis fields are the sleeve's ----------------------------------------


def p_sleeve_scope(payload: Payload) -> Result:
    rows = _restated_rows(payload)
    if not rows:
        return skip("no restated strategy row")
    off = [
        str(row.get("strategy_id"))
        for row in rows
        if not set(_APPLIES) <= set(row["return_scope"].get("applies_to", []))
        or row["return_scope"].get("mwr_and_governed_capital_basis") != SLEEVE
    ]
    if off:
        return bad(f"restated rows whose scope omits a sleeve field: {off}")
    return ok(f"{len(rows)} restated rows name every sleeve field in their scope")


def b_sleeve_scope(payload: Payload) -> None:
    for row in _restated_rows(payload):
        row["return_scope"]["applies_to"] = ["twr"]


# ---- the parent-account figures stay published in account_summary.account_basis -------


def p_account_basis_published(payload: Payload) -> Result:
    rows = _restated_rows(payload)
    if not rows:
        return skip("no restated strategy row")
    blocks = {
        (item.get("account_id"), item["account_basis"].get("strategy_id")): item[
            "account_basis"
        ]
        for item in payload.report.get("account_summary", [])
        if isinstance(item, dict) and isinstance(item.get("account_basis"), dict)
    }
    missing = []
    for row in rows:
        ref = row["return_scope"].get("account_basis_ref") or {}
        block = blocks.get((ref.get("account_id"), row.get("strategy_id")))
        if (
            block is None
            or block.get("series", {}).get("sha256") != ref.get("series_sha256")
            or not block.get("figures")
        ):
            missing.append(str(row.get("strategy_id")))
    if missing:
        return bad(f"no account-basis block with figures for: {missing}")
    return ok(
        f"{len(rows)} restated rows resolve to a parent-account block with figures"
    )


def b_account_basis_published(payload: Payload) -> None:
    for item in payload.report.get("account_summary", []):
        item.pop("account_basis", None)


# ---- the disclosure holds the margin line and both bridges ---------------------------


def p_disclosure_holds(payload: Payload) -> Result:
    rows = _restated_rows(payload)
    if not rows:
        return skip("no restated strategy row")
    disclosure = payload.report.get("strategy_basis_disclosure") or {}
    held = {
        item.get("strategy_id"): item
        for item in disclosure.get("strategies", [])
        if isinstance(item, dict)
    }
    missing = [
        str(row.get("strategy_id"))
        for row in rows
        if not all(
            held.get(row.get("strategy_id"), {}).get(key)
            for key in ("margin_liability", "bridges", "account_basis_ref")
        )
    ]
    if missing:
        return bad(f"the disclosure lacks a block for: {missing}")
    return ok(
        f"{len(rows)} restated rows have margin line, bridges and account-basis ref"
    )


def b_disclosure_holds(payload: Payload) -> None:
    payload.report["strategy_basis_disclosure"]["strategies"][0].pop("bridges")


# ---- the perimeter memo: sleeve value minus the parent value, not a remainder --------


def _memo(payload: Payload) -> dict[str, Any] | None:
    reconciliation = payload.report.get("capital_reconciliation")
    memo = reconciliation.get("memo") if isinstance(reconciliation, dict) else None
    value = memo.get("perimeter_restatement") if isinstance(memo, dict) else None
    return value if isinstance(value, dict) else None


def p_perimeter_amount(payload: Payload) -> Result:
    memo = _memo(payload)
    if memo is None:
        return skip("no perimeter restatement memo")
    lines = memo.get("lines") or []
    total = _dec(memo.get("difference_usd"))
    parts = [
        (_dec(line.get("sleeve_value_usd")), _dec(line.get("account_basis_value_usd")))
        for line in lines
    ]
    if total is None or not lines or any(a is None or b is None for a, b in parts):
        return bad("the memo carries no complete line")
    expected = sum(
        (a - b for a, b in parts if a is not None and b is not None), Decimal(0)
    )
    line_sums = [
        _dec(line.get("difference_usd")) == (a - b)
        for line, (a, b) in zip(lines, parts, strict=True)
        if a is not None and b is not None
    ]
    if abs(total - expected) > Decimal("1e-9") or not all(line_sums):
        return bad(
            f"memo amount {total} is not the sleeve value minus the parent value"
        )
    return ok(
        f"{len(lines)} lines: amount {total} equals sleeve value minus parent value"
    )


def b_perimeter_amount(payload: Payload) -> None:
    memo = _memo(payload)
    assert memo is not None
    memo["difference_usd"] = "21"


def p_not_a_remainder(payload: Payload) -> Result:
    memo = _memo(payload)
    if memo is None:
        return skip("no perimeter restatement memo")
    residual = _dec(
        (payload.report.get("capital_reconciliation") or {}).get("residual_usd")
    )
    if memo.get("in_difference_usd") is not False or residual != Decimal(0):
        return bad(
            f"memo in_difference_usd={memo.get('in_difference_usd')!r}, residual={residual}"
        )
    return ok(
        "the memo is outside the reconciliation difference and the residual is zero"
    )


def b_not_a_remainder(payload: Payload) -> None:
    payload.report["capital_reconciliation"]["residual_usd"] = "5"


SLEEVE_ROWS: tuple[Row, ...] = (
    Row(
        "R22-1",
        "universal",
        "Strategy underwriting",
        PANEL_HTML,
        (r"the unlevered figure is never copied into the levered column",),
        "a levered strategy's levered return is not its unlevered return",
        p_levered_not_copied,
        b_levered_not_copied,
        pass_fixture=_restated_fixture,
    ),
    Row(
        "R22-2",
        "basis",
        "Comparisons",
        REPORT_PY,
        (
            r"read on the exact dates they share with the sleeve path",
            r"f.Sleeve basis: sleeve \{benchmark_lane",
        ),
        "a sleeve comparison lane counts the dates it shares with the reference, inside its window",
        p_shared_dates,
        b_shared_dates,
        pass_fixture=_restated_fixture,
    ),
    Row(
        "R22-3",
        "basis",
        "Methodology",
        REPORT_PY,
        (
            r"the governed perimeter are the sleeve's",
            r".. Return, drawdown, value, weight, the weekly series and",
        ),
        "a restated row's scope names return, drawdown, value, weight and weekly series as the sleeve's",
        p_sleeve_scope,
        b_sleeve_scope,
        pass_fixture=_restated_fixture,
    ),
    Row(
        "R22-4",
        "basis",
        "Methodology",
        REPORT_PY,
        (
            r"published in account_summary\.account_basis from the unrestated",
            r".The parent-account figures a restated row used to carry are",
        ),
        "each restated row's parent-account figures are published in account_summary.account_basis",
        p_account_basis_published,
        b_account_basis_published,
        pass_fixture=_restated_fixture,
    ),
    Row(
        "R22-5",
        "universal",
        "Methodology",
        REPORT_PY,
        (r"parent series; strategy_basis_disclosure holds the margin",),
        "the disclosure holds the margin line, the account bridge and the audit bridge of every restated strategy",
        p_disclosure_holds,
        b_disclosure_holds,
        pass_fixture=_restated_fixture,
    ),
    Row(
        "R22-6",
        "basis",
        "Capital reconciliation",
        TOTALS_PY,
        (
            r"the amount is the sleeve value minus the parent account value the strategy",
            r".Strategies valued on their own sleeve \(gross sleeve NAV\)",
        ),
        "the perimeter memo amount is the sleeve value minus the parent account value, per strategy and in total",
        p_perimeter_amount,
        b_perimeter_amount,
        pass_fixture=_restated_fixture,
    ),
    Row(
        "R22-7",
        "universal",
        "Capital reconciliation",
        TOTALS_PY,
        (r"It is not a remainder: the governed perimeter now",),
        "the perimeter memo is outside the reconciliation difference and the residual is zero",
        p_not_a_remainder,
        b_not_a_remainder,
        pass_fixture=_restated_fixture,
    ),
)
