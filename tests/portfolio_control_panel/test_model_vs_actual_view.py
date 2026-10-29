"""Model vs actual (mockup C, #595): totals, bridge, notes, epochs and explicit unavailable states."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_shell_qc1_labels import _run


def _plain(dom: dict[str, Any], selector: str) -> str:
    html = dom[selector]["innerHTML"]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _window(gap: float, lev: float, fx: float | None) -> dict[str, Any]:
    neutral = round(gap - lev, 2)
    return {
        "window_start": "2025-11-21",
        "window_end": "2026-09-11",
        "window_days": 211,
        "comparator_label": "P123 model, EUR",
        "account_return": 0.316,
        "account_currency": "USD",
        "model_return": 0.451,
        "model_currency": "EUR",
        "as_is_gap_pp": gap,
        "leverage_line_pp": lev,
        "leverage_term": {
            "evidence": "D",
            "evidence_note": "COMPOSITION_UNVERIFIED",
            "uncertainty_pp": 4.6,
        },
        "leverage_neutral_gap_pp": neutral,
        "fx_line_pp": fx,
        "gap_neutral_pp": None if fx is None else round(neutral - fx, 2),
        "evidence": "D",
        "bridge": [
            {
                "component": "execution",
                "label": "Execution (fills vs same-day close)",
                "pp": -8.17,
                "usd": -1200.0,
                "evidence": "D",
            },
            {
                "component": "commissions",
                "label": "Commissions",
                "pp": -5.48,
                "usd": -800.0,
                "evidence": "D",
                "badges": ["AUDIT_DERIVED"],
            },
            {
                "component": "leverage",
                "label": "Leverage line",
                "pp": lev,
                "usd": -821.0,
                "evidence": "D",
                "sub_components": [
                    {
                        "component": "borrowing",
                        "label": "Stock weight above 1x",
                        "pp": -1.24,
                        "usd": -180.0,
                        "evidence": "D",
                    },
                    {
                        "component": "multiple",
                        "label": "Product multiple",
                        "pp": 0.01,
                        "usd": 1.0,
                        "evidence": "I",
                        "in_leverage_line": False,
                    },
                ],
                "robustness": {"note": "Cash drag is not independently robust."},
            },
        ],
        "epochs": [
            {
                "epoch_id": "E1",
                "label": "E1",
                "account_return": None,
                "model_return": None,
                "gap_pp": -1.65,
                "main_driver": "Execution -0.98",
                "evidence": "D",
            },
        ],
    }


def _block() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "AVAILABLE",
        "window_keys": ["ALIGNED", "ACCOUNT"],
        "window_labels": {"ALIGNED": "Aligned window", "ACCOUNT": "Account window"},
        "evidence_legend": {
            "O": "Observed in a file",
            "D": "Derived by arithmetic",
            "I": "Inferred",
        },
        "reading_note": "Gap = account minus model, percentage points.",
        "strategies": [
            {
                "strategy_id": "eu",
                "display_name": "EU [P123]",
                "windows": {
                    "ALIGNED": _window(-13.49, -5.39, 6.09),
                    "ACCOUNT": _window(-12.0, -5.0, None),
                },
                "notes": {
                    "evidence_level": "D",
                    "comparator": {
                        "description": "P123 Designer Model 1764562.",
                        "publisher": "Portfolio123",
                        "gross_or_net_of_fees": "GROSS",
                        "reconstruction_basis": "Simulated.",
                    },
                    "gap_contents": "Execution -8.17 and commissions -5.48.",
                    "artifact_vs_real": [
                        {
                            "item": "Subscription outside NAV",
                            "kind": "REAL_COST",
                            "usd_or_pp": "-5.19 pp",
                        }
                    ],
                    "actionable": ["Use limit orders."],
                    "open_items": ["Composition of currency of cash."],
                },
                "analysis_ref": "model-vs-actual/eu123/RESULT.md",
            },
            {
                "strategy_id": "ai",
                "display_name": "AI Factor [P123]",
                "windows": {"ALIGNED": _window(-16.86, 0.71, None)},
            },
        ],
        "portfolio": {
            "label": "Portfolio (dollar-weighted)",
            "method_note": "Sum of strategy bridges in dollars.",
            "windows": {
                "ALIGNED": {**_window(-9.0, -1.0, 0.5), "window_start": "2025-11-21"},
                "ACCOUNT": {
                    "unavailable_reason": "NEEDS_COMMON_WINDOW",
                    "required_pull": "per-strategy USD on a common window",
                },
            },
        },
    }


def test_totals_table_shows_every_strategy_and_a_portfolio_total_row(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, {"model_vs_actual": _block()}, "renderModelVsActual")

    text = _plain(dom, "#mva-root")
    assert "EU [P123]" in text and "AI Factor [P123]" in text
    assert "−13.49 pp" in text and "−5.39 pp" in text and "+6.09 pp" in text
    assert "±4.6" in text
    assert "Portfolio (dollar-weighted)" in text and "−9.00 pp" in text
    assert "Window not published for this strategy" not in text


def test_a_strategy_without_a_window_says_so(tmp_path: Path) -> None:
    block = _block()
    block["strategies"][1]["windows"] = {}
    dom = _run(tmp_path, {"model_vs_actual": block}, "renderModelVsActual")

    assert "Window not published for this strategy" in _plain(dom, "#mva-root")


def test_missing_block_and_unavailable_block_name_their_reason_and_pull(
    tmp_path: Path,
) -> None:
    missing = _plain(_run(tmp_path, {}, "renderModelVsActual"), "#mva-root")
    assert "not in this snapshot" in missing and "has not published" in missing

    block = {
        "schema_version": 1,
        "status": "UNAVAILABLE",
        "unavailable_reason": "NO_BRIDGE_INPUTS",
        "required_pull": "IBKR Flex statements",
    }
    text = _plain(
        _run(tmp_path, {"model_vs_actual": block}, "renderModelVsActual"), "#mva-root"
    )
    assert "No bridge inputs" in text and "IBKR Flex statements" in text


def test_the_portfolio_drill_down_reads_the_dollar_weighted_bridge(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, {"model_vs_actual": _block()}, "renderModelVsActual")

    detail = _plain(dom, "#mva-root")
    assert "Where the gap came from" in detail
    assert "Execution (fills vs same-day close)" in detail and "−8.17 pp" in detail
    assert "Sum of strategy bridges in dollars." in detail
    assert "AUDIT DERIVED" in detail or "Audit derived" in detail


def test_bridge_shows_sub_components_robustness_and_memo_lines(tmp_path: Path) -> None:
    dom = _run(tmp_path, {"model_vs_actual": _block()}, "renderModelVsActual")

    html = dom["#mva-root"]["innerHTML"]
    assert "Stock weight above 1x" in html
    assert "memo, not in the line" in html
    assert "Cash drag is not independently robust." in html


def test_evidence_legend_comes_from_the_payload(tmp_path: Path) -> None:
    dom = _run(tmp_path, {"model_vs_actual": _block()}, "renderModelVsActual")

    assert "Derived by arithmetic" in _plain(dom, "#mva-root")


def test_an_unavailable_portfolio_window_names_reason_and_pull(tmp_path: Path) -> None:
    block = _block()
    block["window_keys"] = ["ACCOUNT"]
    dom = _run(tmp_path, {"model_vs_actual": block}, "renderModelVsActual")

    text = _plain(dom, "#mva-root")
    assert "Needs common window" in text
    assert "per-strategy USD on a common window" in text
