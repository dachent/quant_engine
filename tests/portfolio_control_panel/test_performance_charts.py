"""Performance charts: both charts render performance_tab schema 3 and say what is missing."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import re
from typing import Any

from test_shell_qc1_labels import _run

DATES = [
    "2025-12-01",
    "2025-12-08",
    "2025-12-15",
    "2025-12-22",
    "2025-12-29",
    "2026-01-05",
]


def _week_ends() -> list[str]:
    return [(date.fromisoformat(d) + timedelta(days=6)).isoformat() for d in DATES]


def _strategy(
    name: str, first: int, levels: list[float], origin: list[list[Any]]
) -> dict[str, Any]:
    tri: list[float | None] = [None] * first + levels
    return {
        "series_id": f"strategy:{name}",
        "display_name": name,
        "kind": "STRATEGY",
        "basis": "STRATEGY_TOTAL_RETURN",
        "definition": "fixture",
        "source_mode": "LIVE_PLUS_BACKTEST",
        "first_week": DATES[first],
        "first_index": first,
        "last_index": len(DATES) - 1,
        "segments": [[first, len(DATES) - 1]],
        "tri": tri,
        "origin": origin,
    }


def _mode() -> dict[str, Any]:
    alpha = _strategy(
        "Alpha",
        0,
        [1.01, 1.03, 1.02, 1.05, 1.06, 1.08],
        [
            [0, 1, "BACKTEST"],
            [2, 3, "DERIVED"],
            [4, 4, "PROVIDER_MODEL_LIVE"],
            [5, 5, "LIVE"],
        ],
    )
    beta = _strategy("Beta", 2, [1.02, 1.04, 1.05, 1.07], [[2, 5, "LIVE"]])
    gamma = _strategy("Gamma", 1, [1.0, 1.01, 1.02, 1.03, 1.04], [[1, 5, "LIVE"]])
    equal = {
        "series_id": "portfolio:equal_weight",
        "display_name": "Equal-weight portfolio",
        "kind": "PORTFOLIO",
        "definition": "Only from the first week all 3 strategies have data.",
        "first_week": DATES[2],
        "first_index": 2,
        "last_index": 5,
        "member_count": 3,
        "tri": [None, None, 1.01, 1.03, 1.04, 1.06],
        "origin": [[2, 5, "MIXED"]],
    }
    available = {
        "series_id": "portfolio:equal_weight_available",
        "display_name": "Equal-weight, available strategies",
        "kind": "PORTFOLIO",
        "member_counts": [1, 2, 3, 3, 3, 3],
        "tri": [1.0, 1.01, 1.02, 1.03, 1.05, 1.06],
        "origin": [[0, 5, "MIXED"]],
    }
    spy = {
        "series_id": "market:SPY",
        "display_name": "SPY",
        "kind": "BENCHMARK",
        "tri": [1.0, 1.01, 1.02, 1.03, 1.04, 1.05],
    }
    return {
        "status": "AVAILABLE",
        "grid": "ISO_WEEK_START_MONDAY",
        "base_point": "START_OF_EACH_LINE_FIRST_WEEK",
        "union_start_date": DATES[0],
        "union_start_set_by": "market:SPY",
        "group_anchor_date": DATES[2],
        "group_anchor_index": 2,
        "group_anchor_set_by": "strategy:Beta",
        "common_start_date": DATES[2],
        "common_start_set_by": "strategy:Beta",
        "first_observation_common_start": DATES[2],
        "gaps_before_common_start": [],
        "dates": DATES,
        "week_end_dates": _week_ends(),
        "last_week_partial": False,
        "data_through_date": "2026-01-11",
        "lines": [alpha, beta, gamma, equal, available, spy],
    }


def _bench_dollars(ticker: str, scale: float) -> dict[str, Any]:
    return {
        "series_id": f"market:{ticker}",
        "ticker": ticker,
        "status": "AVAILABLE",
        "reason": None,
        "method": "FLOW_MATCHED",
        "method_definition": "B(t) sums each flow times the benchmark return since its date.",
        "tri_basis": "DAILY_ADJUSTED_CLOSE",
        "flow_timing": "FLOW_DATE_CLOSE",
        "tri": [1.0] * 4,
        "nav_cents": [0, 10000, int(10000 * scale), int(10500 * scale)],
        "end_value_cents": int(10500 * scale),
        "portfolio_minus_benchmark_cents": 500,
        "trailing": {
            "1w": 0.01,
            "1m": None,
            "3m": None,
            "6m": None,
            "YTD": 0.02,
            "1y": None,
            "SINCE_START": 0.05,
        },
    }


def _slices() -> dict[str, Any]:
    cents = [[0, 10000, 10100, 10200], [0, 0, 5000, 5200]]
    return {
        "status": "AVAILABLE",
        "unit": "USD_CENTS",
        "dates": ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        "rows": [
            {"slice_id": "s1", "strategy_id": "alpha", "nav_cents": cents[0]},
            {"slice_id": "s2", "strategy_id": "beta", "nav_cents": cents[1]},
        ],
        "reconciliation": {
            "reference_date": "2026-01-05",
            "residual_cents": 300,
            "total_cents": [0, 10000, 15100, 15400],
        },
        "flows": {
            "total_contributed_cents": 14000,
            "implied_pnl_cents": 1400,
            "internal_cancellation": {"status": "NOT_PROVED", "residual_cents": 12},
        },
        "benchmarks": [
            _bench_dollars("SPY", 1.0),
            {
                "series_id": "market:SPXL",
                "ticker": "SPXL",
                "status": "UNAVAILABLE",
                "reason": "DAILY_SERIES_NOT_ADMITTED",
                "nav_cents": None,
                "trailing": None,
            },
        ],
    }


def _payload() -> dict[str, Any]:
    mode = _mode()
    return {
        "performance_tab": {
            "schema_version": 3,
            "benchmarks": [
                {"series_id": "market:SPY", "ticker": "SPY", "status": "AVAILABLE"},
                {
                    "series_id": "market:SPXL",
                    "ticker": "SPXL",
                    "status": "UNAVAILABLE",
                    "reason": "NOT_ADMITTED",
                },
            ],
            "modes": {"LIVE_ONLY": mode, "LIVE_PLUS_BACKTEST": mode},
            "slices": _slices(),
            "readout": {},
        },
        "strategies": [
            {"strategy_id": "alpha", "strategy_name": "Alpha"},
            {"strategy_id": "beta", "strategy_name": "Beta"},
        ],
        "hwm_governed_proof": {
            "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
            "index_path": [
                {"as_of": "2026-01-02", "index": "100"},
                {"as_of": "2026-01-05", "index": "105"},
            ],
        },
    }


def _plain(dom: dict[str, Any], selector: str) -> str:
    entry = dom[selector]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", entry["innerHTML"])).strip()


def _polylines(html: str) -> list[tuple[str, str | None, list[float]]]:
    out = []
    for match in re.finditer(r"<polyline ([^>]*)></polyline>", html):
        attrs = match.group(1)
        stroke = re.search(r'stroke="([^"]+)"', attrs)
        dash = re.search(r'stroke-dasharray="([^"]+)"', attrs)
        points = re.search(r'points="([^"]+)"', attrs)
        assert stroke is not None and points is not None
        xs = [float(p.split(",")[0]) for p in points.group(1).split()]
        out.append((stroke.group(1), dash.group(1) if dash else None, xs))
    return out


def test_returns_chart_reads_the_union_grid_and_names_the_anchor(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    legend = _plain(dom, "#perf-returns-legend")
    for name in ("Alpha", "Beta", "Gamma", "Equal-weight portfolio", "SPY"):
        assert name in legend
    # A benchmark without a series is named with the producer's reason, never drawn.
    assert "SPXL unavailable: not admitted" in legend
    assert "not in this snapshot" not in legend
    note = _plain(dom, "#perf-returns-note")
    assert "own first week" in note  # Max view is the default zoom
    assert "Group anchor Dec 15, 2025" in note and "set by Beta" in note
    assert "draws nothing before its own first week" in note


def test_each_line_starts_at_its_own_first_week(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    first_x: dict[str, float] = {}
    for stroke, _dash, xs in _polylines(dom["#perf-returns-chart"]["innerHTML"]):
        first_x[stroke] = min(first_x.get(stroke, 1e9), xs[0])
    alpha, beta, gamma = "#5cc8c0", "#7cb7ff", "#f2bb62"
    # Alpha starts in week 0, Gamma in week 1, Beta in week 2.
    assert first_x[alpha] < first_x[gamma] < first_x[beta]


def test_origin_styles_are_distinct_and_solid_means_live(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    dashes = {
        dash
        for _stroke, dash, _xs in _polylines(dom["#perf-returns-chart"]["innerHTML"])
    }
    assert {"5 4", "2 3", "9 3 2 3"} <= dashes and None in dashes
    note = _plain(dom, "#perf-returns-note")
    assert "dotted = derived" in note and "dash-dot = provider model live" in note


def test_the_available_members_equal_weight_line_is_off_by_default(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    legend = dom["#perf-returns-legend"]["innerHTML"]
    assert re.search(
        r'perf-legend-item is-off"[^>]*data-perf-series="portfolio:equal_weight_available"',
        legend,
    )


def test_a_schema_two_block_is_refused_not_read(tmp_path: Path) -> None:
    payload = _payload()
    payload["performance_tab"]["schema_version"] = 2
    dom = _run(tmp_path, payload, "renderPerformance")

    assert "is not the schema 3 this panel reads" in _plain(dom, "#perf-returns-chart")
    assert "is not the schema 3 this panel reads" in _plain(dom, "#perf-value-chart")


def test_value_chart_overlays_benchmark_dollars_and_fills_the_trailing_table(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    chart = dom["#perf-value-chart"]["innerHTML"]
    assert chart.count("<polygon") == 2
    assert 'stroke-dasharray="6 3"' in chart
    legend = _plain(dom, "#perf-value-legend")
    assert "SPY, same cash flows" in legend
    assert "SPXL, same cash flows unavailable: daily series not admitted" in legend
    assert "internal transfers not proved" in legend
    trailing = _plain(dom, "#perf-trailing-table")
    assert "not in this snapshot" not in trailing
    assert "SPY, total return" in trailing and "5.0%" in trailing
    assert "SPXL, total return Unavailable" in trailing
    assert "daily series not admitted" in trailing
    readout = _plain(dom, "#perf-value-readout")
    assert "candidate (pending owner acceptance)" in readout
    assert "$154" in readout


def test_value_chart_without_published_benchmarks_says_so(tmp_path: Path) -> None:
    payload = _payload()
    del payload["performance_tab"]["slices"]["benchmarks"]
    dom = _run(tmp_path, payload, "renderPerformance")

    assert "no slices.benchmarks" in _plain(dom, "#perf-value-legend")
    assert "published no benchmark series" in _plain(dom, "#perf-trailing-table")


def test_unavailable_modes_and_slices_are_explicit(tmp_path: Path) -> None:
    payload = _payload()
    for mode in payload["performance_tab"]["modes"].values():
        mode.clear()
        mode.update({"status": "UNAVAILABLE", "reason": "WEEKLY_RETURNS_ABSENT"})
    payload["performance_tab"]["slices"] = {
        "status": "UNAVAILABLE",
        "reason": "NO_ADMITTED_EPOCH_TRACES",
    }
    dom = _run(tmp_path, payload, "renderPerformance")

    assert "no usable return lines" in _plain(dom, "#perf-returns-chart")
    assert "WEEKLY_RETURNS_ABSENT" in _plain(dom, "#perf-returns-chart")
    assert "no slice values" in _plain(dom, "#perf-value-chart")


def test_missing_block_is_an_explicit_unavailable_state(tmp_path: Path) -> None:
    payload = _payload()
    del payload["performance_tab"]
    dom = _run(tmp_path, payload, "renderPerformance")

    assert "published no performance block" in _plain(dom, "#perf-returns-chart")
    assert "published no performance block" in _plain(dom, "#perf-value-chart")


def test_lineage_mismatch_withholds_both_charts(tmp_path: Path) -> None:
    payload = _payload()
    payload["meta"] = {
        "performance_lineage": {
            "status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
            "reason": "source lineage differs",
        }
    }
    dom = _run(tmp_path, payload, "renderPerformance")

    assert "Performance withheld" in _plain(dom, "#perf-returns-chart")
    assert "Performance withheld" in _plain(dom, "#perf-value-chart")
    assert "<polyline" not in dom["#perf-returns-chart"]["innerHTML"]
    assert "<polygon" not in dom["#perf-value-chart"]["innerHTML"]


def test_the_printed_residual_equation_foots_with_every_term_it_names(
    tmp_path: Path,
) -> None:
    """T1: the residual line prints each term of the published label, so the parts sum."""

    payload = _payload()
    # synthetic amounts: residual = memo remainder + cash left in liquidated epochs
    residual, remainder, closed = "-1,234.56", "-1,240.01", "+5.45"
    payload["performance_tab"]["slices"]["reconciliation"]["residual_cents"] = -123456
    payload["performance_tab"]["slices"]["reconciliation"]["residual_label"] = (
        f"SYSTEMATIC_CAPITAL_MINUS_SLICE_TOTAL_AT_CUTOFF: residual {residual} = "
        f"capital_reconciliation.memo.perimeter_remainder {remainder} "
        "(the residual parent account's value outside the product sleeve: "
        f"unallocated cash and any other remainder) {closed} cash left in liquidated epochs"
    )
    dom = _run(tmp_path, payload, "renderPerformance")
    text = _plain(dom, "#perf-value-legend")

    match = re.search(
        r"residual (-?[\d,]+\.\d\d) = [^()]*? (-?[\d,]+\.\d\d) ([+-][\d,]+\.\d\d) "
        r"cash left in liquidated epochs",
        text,
    )
    assert match is not None, text
    left, memo, rest = (float(g.replace(",", "")) for g in match.groups())
    assert round(memo + rest, 2) == left
    assert "the residual parent account" not in text
