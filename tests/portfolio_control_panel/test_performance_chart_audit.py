"""Performance charts, visual-audit defects D1-D14: synthetic long-history fixture.

The node shell harness runs the real ``app.js`` against a six-year weekly fixture (three
strategies that start late with backtest history, two benchmarks from week 0) and the tests
read what both charts wrote.  The browser test rotates every control combination in
Chromium and asserts the same properties on the rendered pixels (tick overlap, repeated
labels, legend windows).  All ids and numbers are synthetic.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest
from test_shell_qc1_labels import HARNESS, PANEL_ROOT

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="the shell tests need node"
)

WEEKS = 360
FIRST_MONDAY = date(2019, 1, 7)


def _level(index: int, drift: float, wiggle: float) -> float:
    value = 1.0
    for step in range(index + 1):
        value *= 1 + drift + wiggle * math.sin(step / 3.0)
    return value


def _line(
    series_id: str,
    name: str,
    kind: str,
    first: int,
    drift: float,
    origin: list[list[Any]] | None,
) -> dict[str, Any]:
    tri: list[float | None] = [None] * first
    level = 1.0
    for step in range(first, WEEKS):
        level *= 1 + drift + 0.01 * math.sin(step / 3.0)
        tri.append(round(level, 6))
    line: dict[str, Any] = {
        "series_id": series_id,
        "display_name": name,
        "kind": kind,
        "tri": tri,
    }
    if origin is not None:
        line["origin"] = origin
    return line


def _mode(anchor: int) -> dict[str, Any]:
    starts = [FIRST_MONDAY + timedelta(days=7 * index) for index in range(WEEKS)]
    lines = [
        _line(
            "strategy:Alpha",
            "Alpha",
            "STRATEGY",
            max(0, anchor - 100),
            0.002,
            [[max(0, anchor - 100), 259, "BACKTEST"], [260, WEEKS - 1, "ACCOUNT_LIVE"]],
        ),
        _line(
            "strategy:Beta",
            "Beta",
            "STRATEGY",
            max(0, anchor - 50),
            0.001,
            [[max(0, anchor - 50), 309, "BACKTEST"], [310, WEEKS - 1, "ACCOUNT_LIVE"]],
        ),
        _line(
            "strategy:Gamma",
            "Gamma",
            "STRATEGY",
            anchor,
            0.0015,
            [[anchor, WEEKS - 1, "ACCOUNT_LIVE"]],
        ),
        _line(
            "portfolio:equal_weight",
            "Equal-weight portfolio",
            "PORTFOLIO",
            anchor,
            0.001,
            [[anchor, WEEKS - 1, "MIXED"]],
        ),
        _line("market:SPY", "SPY", "BENCHMARK", 0, 0.002, None),
        _line("market:SPXL", "SPXL", "BENCHMARK", 0, 0.012, None),
    ]
    return {
        "status": "AVAILABLE",
        "grid": "ISO_WEEK_START_MONDAY",
        "union_start_date": starts[0].isoformat(),
        "group_anchor_date": starts[anchor].isoformat(),
        "group_anchor_index": anchor,
        "group_anchor_set_by": "strategy:Gamma",
        "gaps_before_common_start": [],
        "dates": [item.isoformat() for item in starts],
        "week_end_dates": [(item + timedelta(days=6)).isoformat() for item in starts],
        "last_week_partial": False,
        "data_through_date": (starts[-1] + timedelta(days=6)).isoformat(),
        "lines": lines,
    }


def _slices() -> dict[str, Any]:
    days = 140
    first = date(2025, 7, 1)
    dates = [(first + timedelta(days=index)).isoformat() for index in range(days)]
    rows = [
        {
            "slice_id": "s_live",
            "strategy_id": "alpha",
            "nav_cents": [10_000_00 + 5_000 * i for i in range(days)],
        },
        {
            "slice_id": "s_late",
            "strategy_id": "beta",
            "nav_cents": [0 if i < 60 else 5_000_00 + 2_000 * i for i in range(days)],
        },
        {"slice_id": "s_zero", "strategy_id": "gamma", "nav_cents": [0] * days},
    ]
    return {
        "status": "AVAILABLE",
        "unit": "USD_CENTS",
        "dates": dates,
        "rows": rows,
        "reconciliation": {},
        "benchmarks": [
            {
                "series_id": "market:SPY",
                "ticker": "SPY",
                "status": "AVAILABLE",
                "method_definition": "synthetic flow-matched",
                "nav_cents": [10_000_00 + 6_000 * i for i in range(days)],
                "end_value_cents": 10_000_00 + 6_000 * (days - 1),
                "trailing": None,
            }
        ],
    }


def _payload(anchor: int = 300) -> dict[str, Any]:
    mode = _mode(anchor)
    return {
        "performance_tab": {
            "schema_version": 3,
            "benchmarks": [
                {"series_id": "market:SPY", "ticker": "SPY", "status": "AVAILABLE"},
                {"series_id": "market:SPXL", "ticker": "SPXL", "status": "AVAILABLE"},
            ],
            "modes": {"LIVE_ONLY": mode, "LIVE_PLUS_BACKTEST": mode},
            "slices": _slices(),
            "readout": {},
        },
        "strategies": [
            {"strategy_id": "alpha", "strategy_name": "Alpha"},
            {"strategy_id": "beta", "strategy_name": "Beta"},
            {"strategy_id": "gamma", "strategy_name": "Gamma"},
        ],
    }


def _run(
    tmp_path: Path, payload: dict[str, Any], steps: list[str]
) -> dict[str, dict[str, str]]:
    """Render, then apply ``steps`` (javascript run inside the panel closure), each followed by a re-render."""

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    calls = [{"fn": "renderPerformance"}]
    for step in steps:
        calls.append({"eval": step})
        calls.append({"fn": "renderPerformanceReturns"})
        calls.append({"fn": "renderPerformanceValue"})
    calls_path = tmp_path / "calls.json"
    calls_path.write_text(json.dumps(calls), encoding="utf-8")
    completed = subprocess.run(
        [
            str(shutil.which("node")),
            str(HARNESS),
            str(PANEL_ROOT / "app.js"),
            str(payload_path),
            "renderPerformance,renderPerformanceReturns,renderPerformanceValue",
            str(calls_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["errors"] == [], result["errors"]
    assert all("error" not in item for item in result["returns"]), result["returns"]
    return result["dom"]


def _eval(tmp_path: Path, expression: str) -> Any:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
    calls_path = tmp_path / "calls.json"
    calls_path.write_text(json.dumps([{"eval": expression}]), encoding="utf-8")
    completed = subprocess.run(
        [
            str(shutil.which("node")),
            str(HARNESS),
            str(PANEL_ROOT / "app.js"),
            str(payload_path),
            "renderPerformance",
            str(calls_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert "error" not in result["returns"][0], result["returns"][0]
    return result["returns"][0]["value"]


def _text(dom: dict[str, Any], selector: str) -> str:
    entry = dom[selector]
    return re.sub(
        r"\s+", " ", re.sub(r"<[^>]+>", " ", entry["innerHTML"] or entry["textContent"])
    ).strip()


def _axis(html: str) -> tuple[list[str], list[str]]:
    """(x labels, y labels) of the chart svg."""

    xs = re.findall(
        r'text-anchor="(?:start|middle|end)" x="[\d.]+" y="[\d.]+">([^<]+)</text>', html
    )
    ys = re.findall(
        r'class="chart-label axis-label" x="2" y="[\d.]+">([^<]+)</text>', html
    )
    return xs, ys


def _number(label: str) -> float:
    match = re.fullmatch(r"([\d,.]+)([KMBT]?)", label)
    assert match, label
    scale = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[match.group(2)]
    return float(match.group(1).replace(",", "")) * scale


ZOOMS = ["1m", "3m", "6m", "YTD", "1y", "3y", "Max"]


def test_time_ticks_are_calendar_aware_with_exact_edges_and_no_repeats(
    tmp_path: Path,
) -> None:
    day = 86400000
    spans = {
        "week": 7,
        "month": 30,
        "half-year": 183,
        "three-years": 1096,
        "nineteen-years": 6900,
    }
    start = 1_735_689_600_000  # 2025-01-01
    for plot_px in (260, 800):
        for name, days in spans.items():
            ticks = _eval(
                tmp_path, f"perfTimeTicks({start}, {start + days * day}, {plot_px})"
            )
            labels = [tick["label"] for tick in ticks]
            assert len(labels) == len(set(labels)), (name, plot_px, labels)
            assert ticks[0]["time"] == start and ticks[0]["anchor"] == "start", name
            assert (
                ticks[-1]["time"] == start + days * day and ticks[-1]["anchor"] == "end"
            ), name
            assert len(ticks) <= plot_px // 100 + 2, (name, plot_px, labels)
            assert [tick["time"] for tick in ticks] == sorted(
                tick["time"] for tick in ticks
            )
    # Month labels only past the day-level spans, years at the longest span.
    long = _eval(tmp_path, f"perfTimeTicks({start}, {start + 6900 * day}, 800)")
    assert any(re.fullmatch(r"\d{4}", tick["label"]) for tick in long)


def test_geometry_follows_the_container_and_shrinks_on_a_phone(tmp_path: Path) -> None:
    wide = _eval(tmp_path, "perfGeometry({ clientWidth: 1000 })")
    phone = _eval(tmp_path, "perfGeometry({ clientWidth: 360 })")
    assert wide["width"] == 1000 and phone["width"] == 360
    assert phone["height"] < wide["height"] and phone["left"] <= wide["left"]
    assert _eval(tmp_path, "perfGeometry({ clientWidth: 0 })")["width"] == 900


def test_log_axis_ticks_are_thinned_to_eight_or_fewer(tmp_path: Path) -> None:
    ticks = _eval(tmp_path, "perfLogAxisTicks(0.5, 4194304)")
    assert 2 <= len(ticks) <= 8


def test_max_is_the_common_window_on_a_linear_axis_without_huge_ticks(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), [])
    xs, ys = _axis(dom["#perf-returns-chart"]["innerHTML"])
    assert len(ys) >= 4 and max(_number(label) for label in ys) < 20
    assert len(xs) == len(set(xs))
    note = _text(dom, "#perf-returns-note")
    assert "Common window" in note and "group anchor" in note
    chips = re.findall(
        r"<button[^>]*perf-legend-item.*?</button>",
        dom["#perf-returns-legend"]["innerHTML"],
        flags=re.S,
    )
    texts = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chip)) for chip in chips]
    assert len(texts) == 6
    # every chip covers the same common window, and none is a compounded-from-the-past percentage
    starts = {
        re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2}) → ", text).group(1) for text in texts
    }
    assert starts == {_week_start(300)}
    assert all(
        abs(float(re.search(r"[+-][0-9.]+(?=%)", text).group(0))) < 1000
        for text in texts
    )


def test_own_start_view_is_explicit_log_with_few_ticks_and_windows_on_every_chip(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), ['perfBasis = "own"'])
    xs, ys = _axis(dom["#perf-returns-chart"]["innerHTML"])
    assert 2 <= len(ys) <= 9 and len(xs) == len(set(xs))
    assert "Own-start view, log axis" in _text(dom, "#perf-returns-note")
    legend = dom["#perf-returns-legend"]["innerHTML"]
    chips = re.findall(r"<button[^>]*perf-legend-item.*?</button>", legend, flags=re.S)
    assert chips and all("→" in chip for chip in chips)
    assert "Own start" in _text(dom, "#perf-window-note")


def test_each_chip_names_its_window_and_the_live_share_of_a_backtest_line(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(anchor=100), ['perfZoom = "Max"'])
    legend = _text(dom, "#perf-returns-legend")
    assert re.search(
        r"Alpha .*?[0-9]{4}-[0-9]{2}-[0-9]{2} → [0-9]{4}-[0-9]{2}-[0-9]{2} · [+-][0-9.]+% a year · live from [0-9]{4}-[0-9]{2}-[0-9]{2}, live ",
        legend,
    )
    gamma = re.search(
        r"Gamma [^A-Z]*?(\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2})([^A-Z]*)", legend
    )
    assert gamma and "live from" not in gamma.group(3)  # live throughout: no marker
    # The live start is the first day of the first live week (week index 260 and 310 of the grid),
    # the same day the live return is measured from, not that week's end.
    assert f"live from {_week_start(260)}" in legend
    assert f"live from {_week_start(310)}" in legend


def _week_start(index: int) -> str:
    return (FIRST_MONDAY + timedelta(days=7 * index)).isoformat()


def test_a_dot_marks_where_live_evidence_begins(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(anchor=100), ['perfZoom = "Max"'])
    html = dom["#perf-returns-chart"]["innerHTML"]
    assert (
        html.count("live evidence starts") == 2
    )  # Alpha and Beta; Gamma is live throughout
    assert f"live evidence starts {_week_start(260)}" in html
    assert f"live evidence starts {_week_start(310)}" in html


def test_one_year_and_three_year_differ_when_the_anchor_allows(tmp_path: Path) -> None:
    early = {}
    for zoom in ("1y", "3y"):
        dom = _run(tmp_path, _payload(anchor=100), [f'perfZoom = "{zoom}"'])
        early[zoom] = _text(dom, "#perf-window-note")
    assert early["1y"] != early["3y"] and "limited" not in early["3y"]
    late = _run(tmp_path, _payload(anchor=300), ['perfZoom = "3y"'])
    note = _text(late, "#perf-window-note")
    assert "3y is limited by the group anchor: the window starts" in note
    assert re.search(r"\(\d+ days of the 1,096 requested\)", note)


def test_every_zoom_has_unique_axis_labels_and_edge_dates_on_both_charts(
    tmp_path: Path,
) -> None:
    for zoom in ZOOMS:
        value_zoom = "Max" if zoom == "3y" else zoom
        dom = _run(
            tmp_path,
            _payload(),
            [f'perfZoom = "{zoom}"; perfValueZoom = "{value_zoom}"'],
        )
        for selector in ("#perf-returns-chart", "#perf-value-chart"):
            xs, _ys = _axis(dom[selector]["innerHTML"])
            assert len(xs) >= 2 and len(xs) == len(set(xs)), (zoom, selector, xs)
            assert re.fullmatch(
                r"[A-Z][a-z]{2} \d{1,2}, \d{4}", xs[0]
            ) and re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", xs[-1])


def test_value_chart_drops_all_zero_slices_and_labels_both_benchmark_bases(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), [])
    legend = _text(dom, "#perf-value-legend")
    assert "Alpha" in legend and "Beta" in legend and "Gamma" not in legend
    assert dom["#perf-value-chart"]["innerHTML"].count("<polygon") == 2
    note = _text(dom, "#perf-value-window-note")
    assert "flow-matched" in note and "benchmark's own total return" in note
    assert "own total return" in _text(
        dom, "#perf-returns-note"
    ) or "own weekly total return" in _text(dom, "#perf-returns-note")
    assert "Max:" in note and "of its peak value" in note


def test_a_slice_with_value_in_a_short_window_is_kept(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload(), ['perfValueZoom = "1m"'])
    assert "Beta" in _text(dom, "#perf-value-legend")  # beta is live in the last month


def test_tooltip_source_prints_percent_not_raw_levels() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    assert "point.value.toFixed(3)" not in source
    assert "perfPercent(point.value)" in source
    assert "Week starting" in source


def test_zoom_buttons_state_a_limited_window_explicitly() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    assert "Limited by group anchor: starts" in source
    assert "is-limited" in source
    css = (PANEL_ROOT / "analytics.css").read_text(encoding="utf-8")
    assert ".segment.is-limited" in css


def test_dark_only_is_recorded() -> None:
    css = (PANEL_ROOT / "styles.css").read_text(encoding="utf-8") + (
        PANEL_ROOT / "analytics.css"
    ).read_text(encoding="utf-8")
    assert (
        "prefers-color-scheme" not in css
    )  # the panel is dark-only; no light theme exists or is requested


def test_chips_print_a_yearly_rate_over_a_year_and_say_so_under_one(
    tmp_path: Path,
) -> None:
    long_run = _text(_run(tmp_path, _payload(anchor=100), []), "#perf-returns-legend")
    assert re.search(r"Alpha [^A-Z]*?a year", long_run)
    short = _text(
        _run(tmp_path, _payload(), ['perfZoom = "3m"']), "#perf-returns-legend"
    )
    assert "under 1 year, not annualized" in short


def test_ytd_is_a_zoom_on_both_charts(tmp_path: Path) -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    assert '["YTD", "ytd"]' in source
    dom = _run(
        tmp_path, _payload(anchor=100), ['perfZoom = "YTD"; perfValueZoom = "YTD"']
    )
    note = _text(dom, "#perf-window-note")
    assert note.startswith("YTD: ") and "2025-01-" in note.replace(
        "2025-01-06", "2025-01-"
    )
    assert _text(dom, "#perf-value-window-note").startswith("YTD: 2025-")


def test_switching_every_series_off_says_so_instead_of_drawing_a_blank_axis(
    tmp_path: Path,
) -> None:
    keys = [
        "strategy:Alpha",
        "strategy:Beta",
        "strategy:Gamma",
        "portfolio:equal_weight",
        "market:SPY",
        "market:SPXL",
    ]
    hide = "; ".join(f'perfHidden.add("{key}")' for key in keys)
    dom = _run(tmp_path, _payload(), [hide])
    assert "Every series is switched off" in _text(dom, "#perf-returns-chart")
    assert "<svg" not in dom["#perf-returns-chart"]["innerHTML"]
    assert "Alpha" in _text(
        dom, "#perf-returns-legend"
    )  # chips stay to switch them back on


def test_a_slice_that_has_not_started_is_not_a_dollar(tmp_path: Path) -> None:
    payload = _payload()
    rows = payload["performance_tab"]["slices"]["rows"]
    rows[1]["nav_cents"] = [None] * 60 + rows[1]["nav_cents"][60:]
    dom = _run(tmp_path, payload, ['perfValueZoom = "Max"'])
    assert "Beta" in _text(dom, "#perf-value-legend")  # still valued later
    # the tooltip prints a dash for a day before the slice existed
    result = _eval_value(tmp_path, payload)
    assert result is True


def _eval_value(tmp_path: Path, payload: dict[str, Any]) -> bool:
    payload_path = tmp_path / "payload2.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    calls_path = tmp_path / "calls2.json"
    calls_path.write_text(
        json.dumps([{"eval": "perfPublishedValue().table[10].slices.s_late === null"}]),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(shutil.which("node")),
            str(HARNESS),
            str(PANEL_ROOT / "app.js"),
            str(payload_path),
            "renderPerformance",
            str(calls_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["returns"][0]["value"]


def test_spxl_and_the_strategy_palette_do_not_collide() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    colours = re.search(
        r'PERF_BENCHMARK_COLORS = [{] SPY: "(#[0-9a-f]{6})", SPXL: "(#[0-9a-f]{6})" [}]',
        source,
    )
    palette = source.split("const PERF_COLORS = [")[1].split("]")[0]
    assert colours and palette
    strategy = set(re.findall(r"#[0-9a-f]{6}", palette))

    def rgb(value: str) -> tuple[int, int, int]:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]

    for benchmark in colours.groups():
        for other in strategy:
            distance = (
                sum(
                    (a - b) ** 2
                    for a, b in zip(rgb(benchmark), rgb(other), strict=True)
                )
                ** 0.5
            )
            assert distance > 60, (benchmark, other, distance)


def test_value_slices_have_at_least_as_many_colours_as_the_published_slices() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    palette = source.split("const PERF_SLICE_COLORS = [")[1].split("]")[0]
    assert len(set(re.findall(r"#[0-9a-f]{6}", palette))) >= 13


def test_tooltips_are_keyboard_and_touch_reachable_and_do_not_cover_the_chart() -> None:
    source = (PANEL_ROOT / "app.js").read_text(encoding="utf-8")
    for needle in (
        'addEventListener("pointermove"',
        'addEventListener("pointerdown"',
        'addEventListener("keydown"',
        'tabindex="0"',
        "?.focus()",
    ):
        assert needle in source, needle
    assert (
        'addEventListener("mousemove"'
        not in source[
            source.index("function renderPerformanceReturns") : source.index(
                "function bindPerformanceControls"
            )
        ]
    )
    css = (PANEL_ROOT / "analytics.css").read_text(encoding="utf-8")
    assert ".perf-tooltip { position: static; max-width: none;" in css
    assert ".perf-tooltip { max-height: 280px; overflow: auto; }" in css


def test_long_definitions_are_folded_and_the_trailing_table_names_its_calendar(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(), [])
    html = dom["#perf-returns-note"]["innerHTML"]
    assert "<details" not in html or "Line definitions and basis notes" in html
    assert "Calendar windows ending" in dom["#perf-trailing-note"]["textContent"]


def test_benchmark_days_without_a_value_are_named(tmp_path: Path) -> None:
    payload = _payload()
    payload["performance_tab"]["slices"]["benchmarks"][0]["nav_cents"][50:53] = [
        0,
        0,
        0,
    ]
    dom = _run(tmp_path, payload, [])
    assert "3 days in this window have no benchmark value" in _text(
        dom, "#perf-value-legend"
    )


def test_value_chart_ytd_starts_on_the_prior_year_last_day(tmp_path: Path) -> None:
    # The trailing table and the producer measure year to date from the prior year's last day.
    payload = _payload()
    slices = payload["performance_tab"]["slices"]
    first = date(2024, 11, 1)
    slices["dates"] = [(first + timedelta(days=i)).isoformat() for i in range(140)]
    dom = _run(tmp_path, payload, ['perfValueZoom = "YTD"'])
    assert _text(dom, "#perf-value-window-note").startswith("YTD: 2024-12-31 to ")


def test_the_readout_states_where_the_value_change_and_the_return_each_start(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["hwm_governed_proof"] = {
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "index_path": [
            {"as_of": "2025-01-01", "index": "100"},
            {"as_of": "2025-11-17", "index": "110"},
        ],
    }
    dom = _run(tmp_path, payload, ['perfValueZoom = "Max"'])
    readout = _text(dom, "#perf-value-readout")
    assert "2025-07-01 to 2025-11-17; includes deposits and withdrawals" in readout
    # the return tile covers the same dates as the value change beside it
    assert "from Jul 1, 2025;" in readout and "not the start" not in readout
    assert (
        "10.0%" in readout
    )  # index 100 on 2025-01-01 (the last point at or before the start) to 110


def test_linear_ticks_always_give_at_least_four_gridlines(tmp_path: Path) -> None:
    for low, high in ((0.95, 2.1), (1.0, 2.0), (0.8, 1.45), (0.0, 0.3)):
        ticks = _eval(tmp_path, f"perfTicksMin({low}, {high}, 5)")
        assert 4 <= len(ticks) <= 9, (low, high, ticks)
        assert ticks == sorted(ticks) and all(
            low - 1e-9 <= t <= high + 1e-9 for t in ticks
        )


def test_a_ytd_start_after_the_last_week_is_unavailable_not_clamped(
    tmp_path: Path,
) -> None:
    unavailable = _eval(
        tmp_path,
        "perfWindows({ starts: [Date.UTC(2025,11,22), Date.UTC(2025,11,29)], "
        "ends: [Date.UTC(2025,11,28), Date.UTC(2026,0,4)], anchorIndex: 0 })"
        ".find((item) => item.label === 'YTD').unavailable",
    )
    assert unavailable is True


def test_early_january_ytd_says_no_week_has_started_instead_of_showing_december(
    tmp_path: Path,
) -> None:
    # Shift the grid so the last week is 2025-12-29..2026-01-04: no week starts on or after Jan 1.
    payload = _payload()
    shift = (date(2025, 12, 29) - (FIRST_MONDAY + timedelta(days=7 * (WEEKS - 1)))).days
    # both evidence modes share one object in the fixture: shift it once
    for mode in {
        id(m): m for m in payload["performance_tab"]["modes"].values()
    }.values():
        for key in ("dates", "week_end_dates"):
            mode[key] = [
                (date.fromisoformat(item) + timedelta(days=shift)).isoformat()
                for item in mode[key]
            ]
    dom = _run(tmp_path, payload, ['perfZoom = "YTD"'])
    assert "YTD has no full week yet" in _text(dom, "#perf-returns-chart")
    assert "<polyline" not in dom["#perf-returns-chart"]["innerHTML"]
    assert (
        _text(dom, "#perf-window-note")
        == "YTD: no week starts on or after 2026-01-01 yet."
    )


def test_origin_names_from_before_the_producer_rename_still_read_as_live(
    tmp_path: Path,
) -> None:
    payload = _payload(anchor=100)
    for mode in payload["performance_tab"]["modes"].values():
        for line in mode["lines"]:
            line["origin"] = [
                [start, end, "LIVE" if flag == "ACCOUNT_LIVE" else flag]
                for start, end, flag in line.get("origin", [])
            ]
    dom = _run(tmp_path, payload, ['perfZoom = "Max"'])
    html = dom["#perf-returns-chart"]["innerHTML"]
    assert html.count("live evidence starts") == 2
    assert f"live evidence starts {_week_start(260)}" in html
    assert "dashed = live" not in _text(dom, "#perf-returns-note")


def test_gridline_labels_print_every_tick_exactly(tmp_path: Path) -> None:
    assert _eval(tmp_path, "perfTickDecimals([0.975, 1, 1.025, 1.05])") == 3
    assert _eval(tmp_path, "perfTickDecimals([0.8, 1, 1.2])") == 2
    assert _eval(tmp_path, "perfTickDecimals([10, 12.5, 15, 17.5])") == 1
    assert _eval(tmp_path, "perfTickDecimals([10, 20, 30])") == 0


def test_an_unknown_origin_label_is_named_and_not_drawn_as_live(
    tmp_path: Path,
) -> None:
    payload = _payload(anchor=100)
    for mode in {
        id(m): m for m in payload["performance_tab"]["modes"].values()
    }.values():
        for line in mode["lines"]:
            line["origin"] = [
                [start, end, "WEIRD_ORIGIN" if flag == "ACCOUNT_LIVE" else flag]
                for start, end, flag in line.get("origin", [])
            ]
    dom = _run(tmp_path, payload, ['perfZoom = "Max"'])
    note = _text(dom, "#perf-returns-note")
    assert (
        "Unrecognised origin label WEIRD_ORIGIN: drawn dashed, not as live evidence."
        in note
    )
    assert "live evidence starts" not in dom["#perf-returns-chart"]["innerHTML"]


def test_a_flat_week_prints_zero_not_negative_zero(tmp_path: Path) -> None:
    assert _eval(tmp_path, "perfPercent(0.99999)") == "0.0%"
    assert _eval(tmp_path, "perfPercent(1.00001)") == "0.0%"
    assert _eval(tmp_path, "perfPercent(1.05)") == "+5.0%"
    assert _eval(tmp_path, "perfPercent(0.95)") == "-5.0%"


def test_a_mixed_week_before_live_weeks_is_where_live_trading_began(
    tmp_path: Path,
) -> None:
    # BACKTEST, then one MIXED week (the week spanning two layers), then LIVE: the Live-only line starts at the MIXED week.
    payload = _payload(anchor=100)
    for mode in {
        id(m): m for m in payload["performance_tab"]["modes"].values()
    }.values():
        for line in mode["lines"]:
            if line["series_id"] == "strategy:Alpha":
                line["origin"] = [
                    [max(0, 100 - 100), 259, "BACKTEST"],
                    [260, 260, "MIXED"],
                    [261, WEEKS - 1, "ACCOUNT_LIVE"],
                ]
    dom = _run(tmp_path, payload, ['perfZoom = "Max"'])
    legend = _text(dom, "#perf-returns-legend")
    assert f"live from {_week_start(260)}" in legend  # the MIXED week, not week 261
    assert f"live from {_week_start(261)}" not in legend
    assert (
        f"live evidence starts {_week_start(260)}"
        in dom["#perf-returns-chart"]["innerHTML"]
    )


def test_a_portfolio_line_that_is_mixed_throughout_has_no_live_start(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(anchor=100), ['perfZoom = "Max"'])
    chips = re.findall(
        r"<button[^>]*perf-legend-item.*?</button>",
        dom["#perf-returns-legend"]["innerHTML"],
        flags=re.S,
    )
    equal = next(chip for chip in chips if "Equal-weight portfolio" in chip)
    assert "live from" not in equal


def test_a_governed_index_that_starts_after_the_window_says_so(tmp_path: Path) -> None:
    payload = _payload()
    payload["hwm_governed_proof"] = {
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "index_path": [
            {"as_of": "2025-09-01", "index": "100"},
            {"as_of": "2025-11-17", "index": "110"},
        ],
    }
    dom = _run(tmp_path, payload, ['perfValueZoom = "Max"'])
    readout = _text(dom, "#perf-value-readout")
    assert (
        "the governed index starts Sep 1, 2025, after the start of this window"
        in readout
    )
    assert "no governed index in this snapshot" not in readout
