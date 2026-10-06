"""The weekly-return producer and published schema share one version."""

import json
from pathlib import Path

import portfolio_weekly_returns as weekly


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_weekly_returns_schema_version_is_bumped_for_visible_partial_week() -> None:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    weekly_returns = schema["properties"]["weekly_returns"]

    assert weekly.SCHEMA_VERSION == 2
    assert weekly_returns["properties"]["analytics_schema_version"]["const"] == 2
