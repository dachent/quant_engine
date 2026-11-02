"""Pass 30: sentences that quoted a universal or a count, now derived from the rows they describe or scoped.

Synthetic values only.  One test per sentence; each names the pass-29 finding it pins.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_panel_render_audit_543 import base, call, run, text

PANEL = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"
CUTOFF = "2030-06-30"


def _app() -> str:
    return (PANEL / "app.js").read_text(encoding="utf-8")


def _html() -> str:
    return (PANEL / "index.html").read_text(encoding="utf-8")


def _flat(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(markup))).strip()


def _epoch(**fields: Any) -> dict[str, Any]:
    row = {
        "status": "OK",
        "end": CUTOFF,
        "gain": "10.00",
        "current_value": "100.00",
        "lifecycle_state": "ACTIVE",
    }
    row.update(fields)
    return row


def test_B1_the_command_view_scopes_its_separation_sentence_to_the_other_views() -> (
    None
):
    app = _app()
    old = 'Provider, predecessor, shadow, backtest, and account-live evidence remain separate.",'
    assert old not in app
    scoped = "remain separate, except in the LIVE_PLUS_BACKTEST view, which joins version-matched backtest history before live inception to the live line and labels it by origin."
    assert app.count(scoped) == 2


def test_B2_the_totals_note_counts_active_and_closed_epochs_from_the_rows(
    tmp_path: Path,
) -> None:
    payload = base(account_manifest={"calculation_cutoff_date": CUTOFF})
    rows = [
        _epoch(),
        _epoch(),
        _epoch(lifecycle_state="CLOSED", current_value="3.81", gain="0.00"),
        _epoch(end="2030-01-15", current_value="0.00", lifecycle_state="CLOSED"),
    ]
    note = _flat(call(tmp_path, payload, "manifestFooter", rows))
    assert (
        "the 3 epochs whose window ends at the cutoff (2 active and 1 closed holding"
        in note
    )
    assert "3.81" in note and "and Gain for every epoch" in note
    assert "Closed epochs hand their closing value" not in note

    states = _flat(
        call(
            tmp_path,
            payload,
            "manifestFooter",
            [
                _epoch(lifecycle_state="INACTIVE"),
                _epoch(),
                _epoch(lifecycle_state="INACTIVE"),
            ],
        )
    )
    assert "(2 inactive and 1 active)" in states

    no_closed = _flat(call(tmp_path, payload, "manifestFooter", [_epoch(), _epoch()]))
    assert (
        "the 2 epochs whose window ends at the cutoff (2 active) and Gain" in no_closed
    )


def test_B3_the_weekly_boundary_describes_a_mixed_week_by_its_contents() -> None:
    html = _html()
    assert "the week containing inception is labelled mixed" not in html
    assert (
        "a week containing both pre-live history and live observations is labelled MIXED."
    ) in html


def test_V1_the_epoch_total_row_is_scoped_to_its_own_table() -> None:
    assert "epochs in this table" in _app()


def test_V2_the_implementation_gap_tile_names_what_the_untested_strategies_are(
    tmp_path: Path,
) -> None:
    strategies = [
        {
            "strategy_id": f"s{i}",
            "strategy_name": f"S{i}",
            "implementation_gap_status": status,
        }
        for i, status in enumerate(
            ["tested", "not_same_vintage", "not_same_vintage", "unavailable"]
        )
    ]
    payload = base(strategies=strategies, counts={"strategies": 4})
    kpis = _flat(text(run(tmp_path, payload, "renderSignals"), "#signal-kpis"))
    assert "1/4" in kpis
    assert "not tested: not same vintage 2, unavailable 1" in kpis


def test_V3_the_page_spells_annualized_one_way() -> None:
    for source in (_app(), _html()):
        assert not re.search(r"(?<![A-Za-z_])[Aa]nnualised(?![A-Za-z_])", source)
