"""Weekly partial-period UI keeps the source-selected mode and visible cutoff week."""

from pathlib import Path

import pytest

import portfolio_control_panel as panel

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_weekly_ui_defaults_to_source_partial_week_and_labels_data_through_date() -> None:
    source = (REPOSITORY_ROOT / "apps/portfolio-control-panel/app.js").read_text(
        encoding="utf-8"
    )

    assert "let includeCurrentWeek = weekly?.current_week?.default_visible === true;" in source, (
        "current week should follow the source-selected visibility default"
    )
    assert "currentWeek.data_through_date" in source, (
        "data-through date is absent"
    )
    assert (
        'currentWeek.is_partial === true' in source
        and "PARTIAL" in source
    ), "partial state needs a visible label"
    assert '$("#weekly-current-toggle").checked = includeCurrentWeek;' in source, (
        "visible week default and toggle state must agree"
    )
    assert "const currentWeekVisible = weeks.includes(currentWeek.week_start);" in source
    assert '"Weekly partial disclosure follows the visible current-week column"' in (
        REPOSITORY_ROOT / "apps/portfolio-control-panel/interaction_qa.mjs"
    ).read_text(encoding="utf-8")
    assert "Include current week</label>" in (
        REPOSITORY_ROOT / "apps/portfolio-control-panel/index.html"
    ).read_text(encoding="utf-8")
    assert 'weekly?.default_evidence_mode || "LIVE_ONLY"' in source, (
        "selected mode should follow the source default"
    )


def _weekly_payload(as_of: str, partial: bool) -> dict[str, object]:
    week_start = "2026-09-14"
    week_end = "2026-09-20"
    week = week_start
    strategy_rows = [
        {
            "display_groups": ["STRATEGY"],
            "display_name": f"Strategy {index}",
            "observation_meta_by_mode": {
                "LIVE_ONLY": {week: [1, as_of, as_of, "PARTIAL_BOUNDARY", []]},
                "LIVE_PLUS_BACKTEST": {week: [1, as_of, as_of, "PARTIAL_BOUNDARY", []]},
            },
            "primary_group": "STRATEGY",
            "series_id": f"strategy:Strategy {index}",
            "values_by_mode": {
                "LIVE_ONLY": {week: "0"},
                "LIVE_PLUS_BACKTEST": {week: "0"},
            },
        }
        for index in range(7)
    ]
    benchmark_rows = [
        {
            "display_groups": ["CORE_BENCHMARK"],
            "observation_meta": {week: [1, as_of, as_of, "PARTIAL_BOUNDARY", []]},
            "primary_group": "CORE_BENCHMARK",
            "series_id": f"market:Benchmark {index}",
            "values": {week: "0"},
        }
        for index in range(9)
    ]
    return {
        "analytics_schema_version": 2,
        "as_of_date": as_of,
        "current_week": {
            "default_visible": True,
            "is_partial": partial,
            "week_start": week_start,
            "week_end": week_end,
            "data_through_date": as_of,
        },
        "default_evidence_mode": "LIVE_ONLY",
        "display_groups": [
            "STRATEGY",
            "ECONOMIC_FACTOR_US",
            "ECONOMIC_FACTOR_EUROPE",
            "STYLE_US_EQUITY",
            "STYLE_MULTI_ASSET",
            "SECTOR",
            "INDUSTRY",
            "CORE_BENCHMARK",
        ],
        "observation_meta_fields": [
            "daily_observation_count",
            "actual_observed_start",
            "actual_observed_end",
            "coverage_status",
            "boundary_reasons",
        ],
        "series": [*strategy_rows, *benchmark_rows],
        "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
        "week_policy": {
            "absent_value": "UNAVAILABLE_NOT_ZERO",
            "aggregation": "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE",
        },
        "weeks": [week],
    }


@pytest.mark.parametrize(
    ("as_of", "partial"),
    [("2026-09-17", True), ("2026-09-20", False)],
)
def test_panel_validator_accepts_cutoff_bound_week_and_preserves_live_mode(
    as_of: str, partial: bool
) -> None:
    weekly = _weekly_payload(as_of, partial)

    accepted = panel._weekly_return_analysis(
        weekly,
        {"generation_id": "a" * 64, "sha256": "b" * 64},
    )

    assert accepted["current_week"]["is_partial"] is partial
    assert accepted["current_week"]["default_visible"] is True
    assert accepted["current_week"]["data_through_date"] == as_of
    assert accepted["default_evidence_mode"] == "LIVE_ONLY"


def test_panel_validator_rejects_week_data_through_date_outside_source_cutoff() -> None:
    weekly = _weekly_payload("2026-09-17", True)
    weekly["current_week"]["data_through_date"] = "2026-09-16"

    with pytest.raises(
        panel.ControlPanelValidationError, match="source cutoff"
    ):
        panel._weekly_return_analysis(
            weekly,
            {"generation_id": "a" * 64, "sha256": "b" * 64},
        )
