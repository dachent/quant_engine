"""The weekly strategy view pins the benchmarks the performance charts compare against."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

import portfolio_control_panel as panel
from test_shell_qc1_labels import _run
from test_weekly_partial_visibility import _weekly_payload

WEEK = "2026-09-14"


def _payload(pinned: list[str]) -> dict[str, object]:
    weekly = _weekly_payload("2026-09-18", False)
    series = list(weekly["series"])  # type: ignore[arg-type]
    for name in ("SPY", "SPXL", "ITOT"):
        series.append(
            {
                "display_groups": ["STYLE_MULTI_ASSET", "CORE_BENCHMARK"],
                "display_name": name,
                "observation_meta": {
                    WEEK: [
                        5,
                        WEEK,
                        "2026-09-18",
                        "COMPLETE_WITHIN_ACCEPTED_CALENDAR",
                        [],
                    ]
                },
                "primary_group": "STYLE_MULTI_ASSET",
                "return_basis": "TOTAL_RETURN_ETF_PROXY",
                "series_id": f"market:{name}",
                "source_key": "core-benchmark-market",
                "values": {WEEK: "0.01"},
            }
        )
    weekly["series"] = series
    return {
        "weekly_returns": weekly,
        "performance_tab": {
            "schema_version": 3,
            "benchmarks": [{"series_id": item} for item in pinned],
        },
    }


def _rows(dom: dict[str, object]) -> list[str]:
    html = str(dom["#weekly-table"]["innerHTML"])  # type: ignore[index]
    return re.findall(
        r'<th scope="row"><span class="cell-primary">([^<]*)</span>', html
    )


def test_the_default_strategy_view_shows_the_named_benchmarks_under_the_strategies(
    tmp_path: Path,
) -> None:
    dom = _run(tmp_path, _payload(["market:SPY", "market:SPXL"]), "renderWeeklyTable")
    names = _rows(dom)

    assert names[-2:] == ["SPY", "SPXL"]
    assert "ITOT" not in names
    assert len([name for name in names if name.startswith("Strategy")]) == 7
    assert "pinned benchmark" in str(dom["#weekly-table"]["innerHTML"])  # type: ignore[index]


def test_without_named_benchmarks_nothing_is_pinned(tmp_path: Path) -> None:
    dom = _run(tmp_path, _payload([]), "renderWeeklyTable")

    assert not {"SPY", "SPXL", "ITOT"} & set(_rows(dom))
    assert "pinned benchmark" not in str(dom["#weekly-table"]["innerHTML"])  # type: ignore[index]


@pytest.mark.parametrize(
    ("as_of", "partial", "accepted"),
    [
        ("2026-09-18", False, True),
        ("2026-09-18", True, False),
        ("2026-09-17", True, True),
        ("2026-09-17", False, False),
        ("2026-09-20", False, True),
    ],
)
def test_the_panel_validator_follows_the_last_trading_day_rule(
    as_of: str, partial: bool, accepted: bool
) -> None:
    weekly = _weekly_payload(as_of, partial)
    arguments = (weekly, {"generation_id": "a" * 64, "sha256": "b" * 64})

    if accepted:
        assert (
            panel._weekly_return_analysis(*arguments)["current_week"][  # noqa: SLF001
                "is_partial"
            ]
            is partial
        )
    else:
        with pytest.raises(panel.ControlPanelValidationError):
            panel._weekly_return_analysis(*arguments)  # noqa: SLF001
