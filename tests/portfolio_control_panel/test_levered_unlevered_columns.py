"""Levered and unlevered return columns on the strategy implementation table.

Each test runs the real ``apps/portfolio-control-panel/app.js`` under Node
(``shell_harness.mjs``) against synthetic strategy rows in the shape the LV contract
publishes: ``return_unlevered_*`` and ``return_levered_*`` on ``strategies[]``.  Every
figure, label and reason is printed from the row; the shell derives and defaults nothing.
All ids and numbers are synthetic.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from test_shell_qc1_labels import (
    PANEL_ROOT,
    _capital_payload,
    _cost_strategy,
    _flat,
    _run,
)

CSS = (PANEL_ROOT / "analytics.css").read_text(encoding="utf-8")
APP = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
INDEX = (PANEL_ROOT / "index.html").read_text(encoding="utf-8")

UNLEVERED_LABEL = "synthetic gross sleeve assets, opening NAV date to end"
LEVERED_LABEL = "synthetic equity after margin financing, opening NAV date to end"


def _geared(**fields: Any) -> dict[str, Any]:
    row = _cost_strategy(
        strategy_id="synthetic-geared",
        strategy_name="Synthetic Geared",
        account_live_cumulative_return="0.02",
        account_live_max_drawdown="-0.11",
        return_unlevered_cumulative="0.10",
        return_unlevered_annualised="0.20",
        return_unlevered_max_drawdown="-0.05",
        return_unlevered_current_drawdown="-0.01",
        return_unlevered_window_days=200,
        return_unlevered_basis_label=UNLEVERED_LABEL,
        return_unlevered_na_reason=None,
        return_levered_cumulative="0.13",
        return_levered_annualised="0.27",
        return_levered_max_drawdown="-0.07",
        return_levered_current_drawdown="-0.02",
        return_levered_window_days=200,
        return_levered_basis_label=LEVERED_LABEL,
        return_levered_peak_gross_to_equity="1.60",
        return_levered_current_gross_to_equity="1.30",
        return_levered_na_reason=None,
    )
    row.update(fields)
    return row


def _plain(reason: str | None, **fields: Any) -> dict[str, Any]:
    row = _cost_strategy(strategy_id="synthetic-plain", strategy_name="Synthetic Plain")
    for kind in ("unlevered", "levered"):
        for name in (
            "cumulative",
            "annualised",
            "max_drawdown",
            "current_drawdown",
            "window_days",
            "basis_label",
        ):
            row[f"return_{kind}_{name}"] = None
        row[f"return_{kind}_na_reason"] = reason
    row["return_levered_peak_gross_to_equity"] = None
    row["return_levered_current_gross_to_equity"] = None
    row.update(fields)
    return row


def _render(tmp_path: Path, *strategies: dict[str, Any]) -> dict[str, Any]:
    payload = _capital_payload(*strategies)
    payload["reconciliation_checks"] = []
    return _run(tmp_path, payload, "renderSignals")


def _rows(table_html: str) -> list[str]:
    body = table_html.split("<tbody>")[1].split("</tbody>")[0]
    return re.findall(r"<tr>.*?</tr>", body, flags=re.S)


def _cells(row_html: str) -> list[str]:
    return re.findall(r"<td[^>]*>.*?</td>", row_html, flags=re.S)


def _text(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def test_every_strategy_row_carries_all_new_columns(tmp_path: Path) -> None:
    dom = _render(tmp_path, _geared(), _plain("SYNTHETIC_NO_LOAN"), _plain(None))
    table = dom["#implementation-table"]["innerHTML"]
    rows = _rows(table)
    head = table.split("<thead>")[1].split("</thead>")[0]
    header_cells = re.findall(r"<th [^>]*>.*?</th>", head.split("</tr>")[1], flags=re.S)

    assert len(rows) == 3
    assert len(header_cells) == 14
    for row in rows:
        assert len(_cells(row)) == len(header_cells)
    # The group row spans exactly the 14 columns and names both bases.
    group = head.split("</tr>")[0]
    assert sum(int(n) for n in re.findall(r'colspan="(\d+)"', group)) == 14
    assert "Unlevered" in group and "Levered" in group
    # Unit-suffixed headers on both groups.
    headers = _text(" ".join(header_cells))
    assert headers.count("Cumulative (% total)") == 2
    assert headers.count("Annualized (% per year)") == 2
    assert headers.count("Max drawdown (%)") == 2
    assert "Gross ÷ equity (x)" in headers


def test_geared_row_prints_each_published_figure_in_its_own_group(
    tmp_path: Path,
) -> None:
    table = _render(tmp_path, _geared())["#implementation-table"]["innerHTML"]
    cells = [_text(cell) for cell in _cells(_rows(table)[0])]

    # columns: 0 name, 1 evidence, 2 account cumulative, 3 account drawdown,
    # 4-6 unlevered, 7-10 levered, 11-13 comparison / cost / version
    assert cells[4].startswith("10.00%") and "200 day window" in cells[4]
    assert cells[5] == "20.00%"
    assert cells[6].startswith("-5.00%") and "current -1.00%" in cells[6]
    assert cells[7].startswith("13.00%")
    assert cells[8] == "27.00%"
    assert cells[9].startswith("-7.00%") and "current -2.00%" in cells[9]
    assert cells[10].startswith("1.60x") and "now 1.30x" in cells[10]
    # Existing figures are not displaced.
    assert cells[2].startswith("2.0%")
    assert cells[3] == "-11.0%"


def test_not_applicable_reason_is_the_rows_own_field(tmp_path: Path) -> None:
    reason = "SYNTHETIC_REASON_FROM_DATA"
    table = _render(tmp_path, _plain(reason))["#implementation-table"]["innerHTML"]
    cells = [_text(cell) for cell in _cells(_rows(table)[0])]

    for index in (4, 5, 6, 7, 8, 9, 10):
        assert cells[index].startswith("—")
        assert "Synthetic reason from data" in cells[index]
    # Levered is null with a reason; the unlevered figure is never copied across.
    both = _render(
        tmp_path,
        _geared(
            return_levered_cumulative=None,
            return_levered_annualised=None,
            return_levered_max_drawdown=None,
            return_levered_peak_gross_to_equity=None,
            return_levered_na_reason="SYNTHETIC_LEVERED_ONLY",
        ),
    )["#implementation-table"]["innerHTML"]
    cells = [_text(cell) for cell in _cells(_rows(both)[0])]
    assert cells[4].startswith("10.00%")
    assert cells[7].startswith("—") and "Synthetic levered only" in cells[7]


def test_absent_fields_say_not_published_and_blank_reason_says_so(
    tmp_path: Path,
) -> None:
    legacy = _cost_strategy(strategy_name="Synthetic Legacy")
    table = _render(tmp_path, legacy, _plain(None))["#implementation-table"][
        "innerHTML"
    ]
    legacy_cells, blank_cells = (
        [_text(cell) for cell in _cells(row)] for row in _rows(table)
    )

    assert "Not published in this payload" in legacy_cells[4]
    assert "No reason published" in blank_cells[4]
    assert "undefined" not in table and "NaN" not in table


def test_legend_comes_from_the_rows_basis_labels_and_hides_without_them(
    tmp_path: Path,
) -> None:
    with_labels = _render(tmp_path, _geared(), _plain("X"))
    legend = _flat(with_labels, "#return-basis-legend")
    assert UNLEVERED_LABEL in legend and LEVERED_LABEL in legend
    assert legend.count(UNLEVERED_LABEL) == 1  # one legend, not one per row

    without = _render(tmp_path, _plain("X"))
    assert "#return-basis-legend" not in without or not _flat(
        without, "#return-basis-legend"
    )


def test_no_status_or_reason_string_is_hardcoded_in_the_shell() -> None:
    # The producer's reasons and labels are data; the shell must not contain any.
    for forbidden in (
        "LEVERAGE_NOT_DERIVED",
        "SYNTHETIC_",
        "SLEEVE_EPOCH",
        "NO_LOAN",
        "NOT_LEVERED",
        "Unleveraged",
    ):
        assert forbidden not in APP
    block = APP[
        APP.index("const returnBasisCell") : APP.index("const implementationRows")
    ]
    assert not re.findall(
        r'"[A-Z]{3,}(?:_[A-Z0-9]+)+"',
        block.replace("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH", ""),
    )


def test_basis_columns_are_aligned_and_the_table_scrolls_instead_of_squeezing() -> None:
    match = re.search(
        r"#implementation-table\s*\{\s*min-width:\s*(\d+)px", CSS, flags=re.S
    )
    assert match and int(match.group(1)) >= 1500
    assert "#implementation-table .basis-cell" in CSS
    assert "#implementation-table .group-row" in CSS
    # The table sits in a scrolling wrapper and the legend and definitions are in the panel.
    assert re.search(
        r'<div class="table-wrap wide"><table id="implementation-table"', INDEX
    )
    assert 'id="return-basis-legend"' in INDEX
    assert "basis-definitions" in INDEX


def test_group_row_is_absent_when_no_groups_are_requested(tmp_path: Path) -> None:
    # Every other table keeps its single header row byte for byte.
    payload = _capital_payload(_geared())
    dom = _run(tmp_path, payload, "renderCapital")
    assert "group-row" not in dom["#strategy-table"]["innerHTML"]


def test_leverage_table_renders_sleeve_financing_rows(tmp_path: Path) -> None:
    rows = [
        {
            "account_id": "synthetic-sleeve-account",
            "provider_id": "synthetic",
            "strategy_id": "synthetic-geared",
            "data_class": data_class,
            "classification": classification,
            "measurement_basis": "SLEEVE_LOAN_EXPOSURE_SHARE",
            "coverage_status": "DERIVED",
            "included_in_nav": included,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": value},
        }
        for data_class, classification, included, value in (
            ("SLEEVE_FINANCING_LIABILITY", "DERIVED_SLEEVE_LOAN", False, "12345"),
            ("SLEEVE_EQUITY", "DERIVED_SLEEVE_EQUITY", None, "67890"),
        )
    ]
    payload = _capital_payload(_geared())
    payload["leverage_disclosures"] = rows
    payload["account_summary"] = []
    dom = _run(tmp_path, payload, "renderLeverageDisclosures")
    text = _flat(dom, "#leverage-table")
    assert "Sleeve financing liability" in text and "12,345" in text
    assert "Sleeve equity" in text and "67,890" in text


def test_absent_fields_with_only_reasons_print_the_reasons(tmp_path: Path) -> None:
    # Producer shape for a strategy without a leverage record: only the two reasons are emitted.
    row = _cost_strategy(
        strategy_name="Synthetic Reason Only",
        return_unlevered_na_reason="SYNTHETIC_UNLEVERED_REASON",
        return_levered_na_reason="SYNTHETIC_LEVERED_REASON",
    )
    table = _render(tmp_path, row)["#implementation-table"]["innerHTML"]
    cells = [_text(cell) for cell in _cells(_rows(table)[0])]
    assert "Synthetic unlevered reason" in cells[4]
    assert "Synthetic levered reason" in cells[7]
    assert "Not published" not in table


def test_dense_cell_columns_follow_the_long_text_columns() -> None:
    # Evidence, then provider/model comparison, cost and version: the columns that carry long text.
    table = re.search(r'"implementation-table": \[([\d, ]+)\]', APP)
    assert table
    configured = [int(part) for part in table.group(1).split(",")]
    headers = re.search(
        r'\["Strategy"\], \["Evidence availability"\].*?\["Version"\]\]', APP, re.S
    )
    assert headers
    names = re.findall(r'\["([^"]+)"', headers.group(0))
    assert [names[i] for i in configured] == [
        "Evidence availability",
        "Provider/model comparison",
        "Cost status",
        "Version",
    ]
