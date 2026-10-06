"""Schema 2 requires real dates for available weekly returns."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _weekly(status: str) -> dict[str, object]:
    return {
        "analytics_schema_version": 2,
        "as_of_date": "2026-09-20",
        "current_week": {
            "data_through_date": None,
            "default_visible": True,
            "is_partial": False,
            "week_end": None,
            "week_start": None,
        },
        "default_evidence_mode": "LIVE_ONLY",
        "display_groups": [],
        "generation_id": None,
        "hybrid_mode_reason": "synthetic",
        "hybrid_mode_status": "UNAVAILABLE_NOT_CONFIGURED",
        "observation_meta_fields": [
            "daily_observation_count",
            "actual_observed_start",
            "actual_observed_end",
            "coverage_status",
            "boundary_reasons",
        ],
        "series": (
            []
            if status == "UNAVAILABLE_NOT_CONFIGURED"
            else [{"primary_group": "STRATEGY"} for _ in range(7)]
        ),
        "source_sha256": None,
        "status": status,
        "strategy_evidence_modes": ["LIVE_ONLY"],
        "week_policy": {},
        "weeks": [],
    }


def test_schema_2_rejects_null_week_dates_for_available_data_but_allows_unavailable() -> None:
    schema = json.loads(
        (REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    weekly_schema = {
        **schema["properties"]["weekly_returns"],
        "$id": schema["$id"],
        "$defs": schema["$defs"],
    }
    validator = Draft202012Validator(weekly_schema)

    assert not validator.is_valid(_weekly("AVAILABLE"))
    assert validator.is_valid(_weekly("UNAVAILABLE_NOT_CONFIGURED"))