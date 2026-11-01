"""p27: a strategy row's window opens at the opening NAV date, not the first return date.

A return belongs to its end date and is measured from the prior close, so the window
of a path whose first return is dated D opens at the close before D, the manifest's
``return_window_start``.  Before the fix the strategy row was handed D, so
``window_days`` and every figure annualised over it were one day short.

Synthetic series only: the seven rows carry the dates of the live strategies.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
END = "2026-09-17"
# (strategy, first return date, opening NAV date, where the opening date is governed)
SEVEN = (
    ("volatis-strategy", "2024-10-16", "2024-10-15", "epoch"),
    ("p123-eu", "2025-11-21", "2025-11-20", "epoch"),
    ("tt-portfolio", "2025-11-25", "2025-11-24", "epoch"),
    ("qsi", "2026-01-05", "2026-01-04", "epoch"),
    ("grizzlybears-vtma", "2026-01-03", "2026-01-02", "epoch"),
    ("p123-ai-factor", "2026-01-22", "2026-01-21", "anchor"),
    ("robuxio-crypto", "2025-12-07", "2025-12-06", "epoch"),
)


@pytest.fixture(scope="module")
def reporter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "portfolio_report_window_opening",
        REPOSITORY_ROOT / "tools" / "portfolio_report.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _series(first: str, end: str) -> list[dict[str, str]]:
    day, last = date.fromisoformat(first), date.fromisoformat(end)
    rows = []
    while day <= last:
        rows.append(
            {
                "date": day.isoformat(),
                "return": "0.0012" if day.toordinal() % 2 else "-0.0004",
            }
        )
        day += timedelta(days=1)
    return rows


def _cumulative(rows: list[dict[str, str]]) -> str:
    wealth = Decimal(1)
    for item in rows:
        wealth *= 1 + Decimal(item["return"])
    return format(wealth - 1, "f")


def _row(strategy: str, first: str, opening: str) -> dict[str, Any]:
    rows = _series(first, END)
    return {
        "strategy_id": strategy,
        "account_live_return_authority": "CANONICAL_STRATEGY_RETURNS",
        "account_live_observed_start": first,
        "account_live_observed_end": END,
        "account_live_return_observations": len(rows),
        "account_live_cumulative_return": _cumulative(rows),
        "account_live_cagr": "0.5",
        "account_live_cagr_status": "AVAILABLE_DERIVED_SAME_CANONICAL_PATH",
        "mwr_xirr": "0.2",
        "mwr_opening_date": opening,
        "mwr_terminal_date": END,
    }


def _world(strategy: str, opening: str, source: str) -> tuple[dict[str, Any], list]:
    """The catalog facts and epoch rows that govern one strategy's opening date."""

    later = (date.fromisoformat(opening) + timedelta(days=8)).isoformat()
    # a sibling epoch that opens after the path's first return never moves it
    performance: list[dict[str, Any]] = [
        {
            "strategy_id": strategy,
            "return_window_start": later,
            "annualisation_basis": {},
        },
        {
            "strategy_id": "other",
            "return_window_start": "2020-01-01",
            "annualisation_basis": {},
        },
    ]
    catalog: dict[str, Any] = {"capital_anchors": []}
    if source == "epoch":
        performance.append(
            {
                "strategy_id": strategy,
                "return_window_start": opening,
                "annualisation_basis": {},
            }
        )
    else:
        catalog["capital_anchors"] = [
            {
                "strategy_id": strategy,
                "evidence_layer": "ACCOUNT_LIVE",
                "capital_type": "OPENING_CAPITAL_ANCHOR",
                "event_date": "2026-01-05",
                "nav_recognition_date": opening,
            }
        ]
    return catalog, performance


@pytest.fixture
def era(reporter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reporter, "_uses_cutoff_basis", lambda catalog: True)


@pytest.mark.parametrize(("strategy", "first", "opening", "source"), SEVEN)
def test_window_opens_at_the_opening_nav_date_and_annualises_over_it(
    reporter: Any, era: None, strategy: str, first: str, opening: str, source: str
) -> None:
    catalog, performance = _world(strategy, opening, source)
    row = _row(strategy, first, opening)
    resolved = reporter._row_opening_nav_date(row, catalog, performance)
    assert resolved == opening

    pieces = reporter._daily_period_pieces(_series(first, END), [])
    before = reporter._strategy_period_fields(row, pieces)
    after = reporter._strategy_period_fields(row, pieces, resolved)
    days = (date.fromisoformat(END) - date.fromisoformat(opening)).days
    assert before["window_days"] == days - 1
    assert after["window_days"] == days == before["window_days"] + 1
    assert after["return_window_start"] == opening
    cumulative = row["account_live_cumulative_return"]
    # the repo's own _cagr over the opening date is the published annualisation
    if days >= 365:
        assert after["twr_annualised"] == reporter._cagr(cumulative, opening, END)
        assert after["twr_annualised"] != before["twr_annualised"]
    else:
        assert after["twr_annualised"] is None
    cagr_label = reporter._account_annualization_labels(row, resolved)[
        "account_live_cagr_annualization"
    ]
    mwr_label = reporter._account_annualization_labels(row, resolved)[
        "mwr_annualization"
    ]
    # every place that states this window's length agrees
    basis = after["annualisation_basis"]
    assert cagr_label["window_start"] == opening
    assert (
        after["window_days"]
        == basis["twr"]["window_days"]
        == basis["account_live_cagr"]["window_days"]
        == cagr_label["window_days"]
        == basis["mwr_xirr"]["window_days"]
        == mwr_label["window_days"]
    )
    # a first calendar year that is the whole path opens where the path opens
    first_year = min(after["calendar_year_windows"])
    assert after["calendar_year_windows"][first_year]["start"] == opening
    assert (
        after["calendar_year_windows"][first_year]["days"]
        == (
            date.fromisoformat(after["calendar_year_windows"][first_year]["end"])
            - date.fromisoformat(opening)
        ).days
    )
    # the annualised CAGR the report publishes over that window
    assert reporter._cagr(cumulative, opening, END) != reporter._cagr(
        cumulative, first, END
    )


def test_a_monday_first_return_opens_at_friday_not_the_day_before(
    reporter: Any, era: None
) -> None:
    catalog, performance = _world("qsi", "2026-01-02", "epoch")  # a Friday
    assert date(2026, 1, 5).weekday() == 0
    assert (
        reporter._opening_nav_date("qsi", "2026-01-05", catalog, performance)
        == "2026-01-02"
    )


def test_outside_version_five_the_window_keeps_the_first_return_date(
    reporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reporter, "_uses_cutoff_basis", lambda catalog: False)
    catalog, performance = _world("volatis-strategy", "2024-10-15", "epoch")
    row = _row("volatis-strategy", "2024-10-16", "2024-10-15")
    assert reporter._row_opening_nav_date(row, catalog, performance) is None


def test_a_canonical_path_with_no_governed_opening_date_fails_closed(
    reporter: Any, era: None
) -> None:
    row = _row("volatis-strategy", "2024-10-16", "2024-10-15")
    with pytest.raises(reporter.ReportValidationError, match="no opening NAV date"):
        reporter._row_opening_nav_date(
            row, {}, [{"strategy_id": "volatis-strategy", "annualisation_basis": {}}]
        )


def test_a_non_canonical_row_has_no_opening_date(reporter: Any, era: None) -> None:
    row = {
        **_row("volatis-strategy", "2024-10-16", "2024-10-15"),
        "account_live_return_authority": "AUDIT_STRATEGY_LIVE_METRICS",
    }
    assert reporter._row_opening_nav_date(row, {}, []) is None


# ---- the real builder ---------------------------------------------------------------


def _load_consumers() -> Any:
    path = (
        REPOSITORY_ROOT
        / "tests"
        / "portfolio_report"
        / "test_current_capital_consumers.py"
    )
    spec = importlib.util.spec_from_file_location("window_opening_consumers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_consumers = _load_consumers()
cr2_custody_structural_complete_world = _consumers.cr2_custody_structural_complete_world


def _epoch_starts(report: dict[str, Any], strategy: str) -> list[str]:
    return [
        row["return_window_start"]
        for row in report["performance"]
        if row["strategy_id"] == strategy and row.get("return_window_start")
    ]


def test_strategy_rows_open_where_their_manifest_rows_open(
    cr2_custody_structural_complete_world: Any,
) -> None:
    """Guard: a strategy-row window start never differs from its manifest window start."""

    _root, admitted, _recipe, _template, output, _catalog, clock, epoch = (
        cr2_custody_structural_complete_world
    )
    report, _raw, _path = _consumers.reporter.build_report_data(
        output,
        provider_ledger_root=admitted["ledger_root"],
        source_clock_path=clock,
        **epoch,
    )
    reporter = _consumers.reporter
    checked = 0
    for row in report["strategy_summary"]:
        if row["account_live_return_authority"] != "CANONICAL_STRATEGY_RETURNS":
            continue
        strategy = row["strategy_id"]
        first, end = (
            row["account_live_observed_start"],
            row["account_live_observed_end"],
        )
        starts = [s for s in _epoch_starts(report, strategy) if s < first]
        assert starts, f"{strategy}: no manifest row opens before the first return"
        assert row["return_window_start"] == max(starts) < first
        assert row["return_window_start"] == min(_epoch_starts(report, strategy))
        days = (
            date.fromisoformat(end) - date.fromisoformat(row["return_window_start"])
        ).days
        assert row["window_days"] == days
        assert row["account_live_cagr_annualization"]["window_days"] == days
        assert (
            row["account_live_cagr_annualization"]["window_start"]
            == row["return_window_start"]
        )
        assert row["annualisation_basis"]["twr"]["window_days"] == days
        assert row["annualisation_basis"]["account_live_cagr"]["window_days"] == days
        cumulative = row["account_live_cumulative_return"]
        assert row["account_live_cagr"] == reporter._cagr(
            cumulative, row["return_window_start"], end
        )
        if days >= 365:
            assert row["twr_annualised"] == row["account_live_cagr"]
        checked += 1
    assert checked
