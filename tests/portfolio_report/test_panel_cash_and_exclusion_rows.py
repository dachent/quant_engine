"""F1: a strategy row shows both comparisons' cash-drag status, and a retained row its
cost classification with the ruling reference (BF panel gaps 12 and 13)."""

from __future__ import annotations

import inspect

from tools import portfolio_control_panel as panel
from tools import portfolio_report as report


def test_the_benchmark_cash_status_rides_beside_the_provider_one() -> None:
    fields = report._cash_drag_fields(
        {"cash_drag_status": "UNAVAILABLE", "cash_drag_reason": "provider gap"},
        {"cash_drag_status": "EVIDENCED", "cash_drag_reason": "benchmark derived"},
    )
    assert fields == {
        "cash_drag_status": "UNAVAILABLE",
        "cash_drag_reason": "provider gap",
        "benchmark_cash_drag_status": "EVIDENCED",
        "benchmark_cash_drag_reason": "benchmark derived",
    }


def test_a_missing_benchmark_comparison_leaves_its_status_absent_not_invented() -> None:
    fields = report._cash_drag_fields({"cash_drag_status": "EVIDENCED"}, {})
    assert fields["benchmark_cash_drag_status"] is None
    assert fields["cash_drag_status"] == "EVIDENCED"


def test_a_retained_row_carries_its_classification() -> None:
    classification = {
        "classification": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        "ruling_reference": "OWNER_302_COMMENT_5349926811_DEPRECATED",
        "reason": "stated",
    }
    assert report._retained_cost_fields({"cost_classification": classification}) == {
        "cost_classification": classification
    }
    assert report._retained_cost_fields({}) == {}


def test_both_row_builders_use_the_helpers() -> None:
    assert "_cash_drag_fields(comparison, benchmark_comparison)" in inspect.getsource(
        report
    )
    assert "**_retained_cost_fields(cost_row)" in inspect.getsource(report._strategies)


def test_a_withdrawn_row_clears_the_benchmark_cash_fields_with_the_rest() -> None:
    source = inspect.getsource(panel)
    assert '"benchmark_cash_drag_status",' in source
    assert '"benchmark_cash_drag_reason",' in source


def test_the_panel_renders_the_benchmark_cash_status_in_the_benchmark_cell() -> None:
    from pathlib import Path

    app = (
        Path(__file__).resolve().parents[2] / "apps/portfolio-control-panel/app.js"
    ).read_text(encoding="utf-8")
    assert "cash drag ${statusText(row.benchmark_cash_drag_status)}" in app
    assert "row.benchmark_cash_drag_reason" in app


def test_the_data_health_summary_names_a_classified_excluded_strategy() -> None:
    from pathlib import Path

    app = (
        Path(__file__).resolve().parents[2] / "apps/portfolio-control-panel/app.js"
    ).read_text(encoding="utf-8")
    assert "row.cost_classification && row.cost_classification.classification" in app
    assert "Excluded from live costs, history kept:" in app
    assert "${safe(outsideNote)}${safe(classifiedNote)}" in app
