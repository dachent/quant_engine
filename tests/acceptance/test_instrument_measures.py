"""Batch V11 and V13: validator measures that overstated what the product does.

V11  decomposition completeness counted every non-AVAILABLE cell.  Cells that are
     NOT_APPLICABLE_METHOD_SCOPE (the owner-accepted strategy-to-lens routing) or
     DIAGNOSTIC_ONLY (the model-quality gate working) are not incomplete; a series that is
     genuinely unavailable still is.
V13  the weekly payload check multiplied the payload dimensions (weeks x series) and
     called it an eager render.  The renderer is windowed: the cells painted are the
     default window's weeks times the series.  An unwindowed render (no recognisable
     window, or ``full``) is still measured on the whole payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_generation import GOOD_APP_JS, Spec, build, run

from acceptance.validate import checks

REQUIRED = "completeness.required_outputs"
PAYLOAD = "lint.payload_size"
NOT_APPLICABLE = "NOT_APPLICABLE_METHOD_SCOPE"


def _series(status: str, selection: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"series_id": f"s-{status}-{selection}", "status": status}
    if selection is not None:
        row["selection_status"] = selection
    return row


def _lens_series(*series: dict[str, Any]):
    def mutate(report: dict[str, Any]) -> None:
        lens = report["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]
        lens["series"] = list(series)
        lens["unavailable_count"] = sum(
            1 for row in series if row["status"] != "AVAILABLE"
        )

    return mutate


def _decomposition(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [
        f
        for f in findings
        if f.check_id == REQUIRED and f.entity == "decomposition_lenses"
    ]


# ----------------------------------------------------------------------------- V11


def test_v11_a_lens_without_series_rows_falls_back_to_its_count(tmp_path: Path) -> None:
    def counts_only(report: dict[str, Any]) -> None:
        lens = report["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]
        lens.pop("series")
        lens["unavailable_count"] = 3

    gen_dir = build(tmp_path, Spec(report=[counts_only]))
    findings = _decomposition(run(gen_dir))
    assert [f.observed for f in findings] == [
        {"unavailable_series_by_lens": {"LIVE_ONLY:ECONOMIC_FACTOR": 3}}
    ]


# ----------------------------------------------------------------------------- V13


def _grid(weeks: int, series: int):
    def mutate(panel: dict[str, Any]) -> None:
        panel["weekly_returns"]["weeks"] = [f"w{index}" for index in range(weeks)]
        panel["weekly_returns"]["series"] = [
            {"series_id": f"s{n}"} for n in range(series)
        ]

    return mutate


def _windowed(window: str) -> str:
    return GOOD_APP_JS + f'\nlet selectedWeeklyWindow = "{window}";\n'


def _weekly_payload(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [
        f for f in findings if f.check_id == PAYLOAD and f.entity == "weekly_returns"
    ]


def test_v13_an_unwindowed_render_over_a_large_grid_is_still_a_warning(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(panel=[_grid(400, 300)]))
    findings = _weekly_payload(run(gen_dir))
    assert [f.klass for f in findings] == [checks.WARNING]
    assert findings[0].observed["rendered_cells"] == 120_000


def test_v13_a_full_window_over_a_large_grid_is_still_a_warning(tmp_path: Path) -> None:
    gen_dir = build(tmp_path, Spec(panel=[_grid(400, 300)], app_js=_windowed("full")))
    findings = _weekly_payload(run(gen_dir))
    assert [f.klass for f in findings] == [checks.WARNING]


def test_v13_a_large_window_is_measured_as_the_window_times_the_series(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path, Spec(panel=[_grid(400, 300)], app_js=_windowed("400")))
    findings = _weekly_payload(run(gen_dir))
    assert [f.observed["rendered_cells"] for f in findings] == [120_000]


def test_v13_a_small_grid_is_not_a_finding(tmp_path: Path) -> None:
    gen_dir = build(tmp_path, Spec(panel=[_grid(52, 10)]))
    assert _weekly_payload(run(gen_dir)) == []
