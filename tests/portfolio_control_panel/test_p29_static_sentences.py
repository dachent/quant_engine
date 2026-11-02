"""Pass 29: static and templated sentences that were universal or scoped wrongly, pinned to what they describe.

Synthetic values only.  Each test names the audit id (S01..S19) of the sentence it pins.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_panel_render_audit_543 import _leverage_row, base, run, text

PANEL = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"


def _html() -> str:
    return (PANEL / "index.html").read_text(encoding="utf-8")


def _row(amount_value: Any, **fields: Any) -> dict[str, Any]:
    return _leverage_row(
        amount={
            "status": "AVAILABLE" if amount_value is not None else "UNAVAILABLE",
            "value": amount_value,
        },
        **fields,
    )


def test_S01_the_leverage_caption_does_not_promise_a_reason_for_every_row() -> None:
    html = _html()
    assert "Every leverage observation, with the source reason" not in html
    assert "with its coverage status and, where the source gave one, its reason" in html
    assert "keeps the source reason verbatim where the source gave one" in html


def test_S03_S04_the_leverage_tiles_count_rows_without_an_amount(
    tmp_path: Path,
) -> None:
    rows = [
        _row("10.5"),
        _row(None),
        _row(None),
        _row(None, coverage_status="UNAVAILABLE", reason="SYN_REASON"),
    ]
    result = run(tmp_path, base(leverage_disclosures=rows), "renderLeverageDisclosures")
    kpis = re.sub(r"\s+", " ", text(result, "#leverage-kpis"))

    assert "Observed-only coverage: 1 with an amount, 2 without" in kpis
    assert "3 rows in all have no amount" in kpis


def test_S05_the_total_row_names_which_portfolio_return_is_unpublished(
    tmp_path: Path,
) -> None:
    from test_panel_render_audit_543 import call  # noqa: F401  (harness import check)

    html = (PANEL / "app.js").read_text(encoding="utf-8")
    assert "Portfolio-level TWR and MWR are not published" not in html
    assert 'data.portfolio_mwr_annualised?.status === "AVAILABLE"' in html
    assert 'data.portfolio_twr_cumulative?.status === "AVAILABLE"' in html


def test_S17_S18_the_model_vs_actual_and_chart_headings_are_not_universal() -> None:
    html = _html()
    assert "Each strategy with an aligned window" in html
    assert "Each strategy's account return against" not in html
    assert "Seven strategies, an equal-weight portfolio" not in html
    assert "Each strategy, the equal-weight portfolio lines, SPY and SPXL" in html


def test_S15_the_weekly_boundary_says_the_inception_week_is_mixed() -> None:
    assert "the week containing inception is labelled mixed" in _html()


def test_S19_the_matrix_heading_claims_only_the_rows_in_it() -> None:
    html = _html()
    assert "Every current-perimeter account-by-fact path" not in html
    assert "Every account-by-fact row in this matrix, including no-path rows" in html
    assert "declared fact paths" not in (PANEL / "app.js").read_text(encoding="utf-8")
