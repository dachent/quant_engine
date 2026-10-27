"""Performance charts: both charts render from the payload fields that exist and say what is missing."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_shell_qc1_labels import _run


def _series(
    name: str, first_live: str, values: dict[str, str], hybrid: dict[str, str] | None
) -> dict[str, Any]:
    by_mode: dict[str, Any] = {"LIVE_ONLY": values}
    if hybrid is not None:
        by_mode["LIVE_PLUS_BACKTEST"] = {**hybrid, **values}
    return {
        "series_id": f"strategy:{name}",
        "display_name": name,
        "primary_group": "STRATEGY",
        "return_basis": "STRATEGY_TOTAL_RETURN",
        "values_by_mode": by_mode,
        "mode_coverage": {"LIVE_ONLY": {"first_week": first_live}},
    }


def _payload(with_spy: bool = False) -> dict[str, Any]:
    weeks_live = {"2026-01-05": "0.01", "2026-01-12": "0.02", "2026-01-19": "-0.01"}
    series = [
        _series("Alpha", "2026-01-05", weeks_live, {"2025-12-29": "0.03"}),
        _series("Beta", "2026-01-05", weeks_live, None),
    ]
    if with_spy:
        series.append(
            {
                "series_id": "market:SPY",
                "display_name": "SPY",
                "primary_group": "CORE_BENCHMARK",
                "return_basis": "TOTAL_RETURN_ETF_PROXY",
                "values": weeks_live,
            }
        )
    links = [
        {"end_date": "2026-01-05", "ending_nav": "100.0"},
        {"end_date": "2026-01-06", "ending_nav": "110.0"},
    ]
    return {
        "weekly_returns": {"series": series},
        "strategies": [
            {"strategy_id": "alpha", "strategy_name": "Alpha"},
            {"strategy_id": "beta", "strategy_name": "Beta"},
        ],
        "account_manifest": {
            "rows": [
                {
                    "status": "OK",
                    "strategy_id": "alpha",
                    "observation_trace": {"links": links},
                },
                {
                    "status": "OK",
                    "strategy_id": "beta",
                    "observation_trace": {"links": links},
                },
            ]
        },
        "hwm_governed_proof": {
            "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
            "index_path": [
                {"as_of": "2026-01-05", "index": "100"},
                {"as_of": "2026-01-06", "index": "105"},
            ],
        },
    }


def _plain(dom: dict[str, Any], selector: str) -> str:
    entry = dom[selector]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", entry["innerHTML"])).strip()


def test_returns_chart_lists_strategies_and_names_missing_benchmarks(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    legend = _plain(dom, "#perf-returns-legend")
    assert "Alpha" in legend and "Beta" in legend and "Equal-weight portfolio" in legend
    assert (
        "SPY not in this snapshot" in legend and "SPXL not in this snapshot" in legend
    )
    assert "Window starts" in dom["#perf-returns-note"]["innerHTML"]
    assert dom["#perf-returns-chart"]["innerHTML"].count("<polyline") >= 3


def test_a_present_benchmark_is_drawn_not_reported_missing(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(with_spy=True), "renderPerformance")

    legend = _plain(dom, "#perf-returns-legend")
    assert "SPY not in this snapshot" not in legend
    assert "SPXL not in this snapshot" in legend


def test_backtest_weeks_are_dashed_before_the_first_live_week(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    assert "stroke-dasharray" in dom["#perf-returns-chart"]["innerHTML"]


def test_value_chart_sums_strategy_nav_and_reads_the_governed_index(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), "renderPerformance")

    readout = _plain(dom, "#perf-value-readout")
    assert "$220" in readout
    assert "5.0%" in readout
    assert "candidate" in readout
    assert dom["#perf-value-chart"]["innerHTML"].count("<polygon") == 2


def test_missing_series_are_an_explicit_unavailable_state(tmp_path: Path) -> None:
    payload = _payload()
    payload["weekly_returns"] = {"series": []}
    payload["account_manifest"] = {"rows": []}
    dom = _run(tmp_path, payload, "renderPerformance")

    assert "not in this snapshot" in _plain(dom, "#perf-returns-chart")
    assert "not in this snapshot" in _plain(dom, "#perf-value-chart")


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


def _producer_payload() -> dict[str, Any]:
    """The renderer fed the producer's real output (tools/portfolio_performance_tab.py)."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "producer_performance_tab_tests",
        Path(__file__).with_name("test_performance_tab.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    panel = module._panel()
    block = module.tab.build_performance_tab(panel)
    return {
        "performance_tab": block,
        "strategies": [
            {"strategy_id": "a", "strategy_name": "A one"},
            {"strategy_id": "b", "strategy_name": "B two"},
            {"strategy_id": "c", "strategy_name": "C three"},
        ],
        "hwm_governed_proof": panel["hwm_governed_proof"],
    }


def test_the_renderer_reads_the_producers_weekly_lines(tmp_path: Path) -> None:
    payload = _producer_payload()
    for mode in payload["performance_tab"]["modes"].values():
        mode["last_week_partial"] = True
        mode["gaps_before_common_start"] = [
            {"series_id": "strategy:B two", "missing_weeks": 2}
        ]
    dom = _run(tmp_path, payload, "renderPerformance")

    legend = _plain(dom, "#perf-returns-legend")
    assert "A one" in legend and "Equal-weight portfolio" in legend
    assert "SPY not in this snapshot" in legend
    assert "SPXL not in this snapshot" in legend
    note = _plain(dom, "#perf-returns-note")
    assert "set by" in note and "as published by the producer" in note
    assert "Weeks before the common start with no data" in note
    assert "B two 2 weeks" in note
    assert "The last week is partial" in note
    assert "stroke-dasharray" in dom["#perf-returns-chart"]["innerHTML"]


def test_the_renderer_reads_the_producers_slices_and_readout(tmp_path: Path) -> None:
    dom = _run(tmp_path, _producer_payload(), "renderPerformance")

    assert dom["#perf-value-chart"]["innerHTML"].count("<polygon") == 3
    legend = _plain(dom, "#perf-value-legend")
    assert "Residual against systematic capital at" in legend
    readout = _plain(dom, "#perf-value-readout")
    assert "candidate (pending owner acceptance)" in readout
    assert "Summed-epoch TWR unavailable" in readout


def test_unavailable_producer_modes_and_slices_are_explicit(tmp_path: Path) -> None:
    payload = _producer_payload()
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
