"""Fail-closed freshness evaluation for dated endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence


NOT_APPLICABLE = "not_applicable_declared"


@dataclass(frozen=True, slots=True)
class FreshnessVerdict:
    object_name: str
    carries_dates: bool
    observed_end_date: str | None
    verdict: str
    detail: str


def observed_dates(object_name: str, rows: Sequence[dict[str, Any]]) -> list[date]:
    """Extract dated observations from normalized rows of a dated endpoint."""
    dates: list[date] = []
    for row in rows:
        raw = row.get("date")
        if not isinstance(raw, str):
            continue
        dates.append(date.fromisoformat(raw))
    return dates


def evaluate(
    object_name: str,
    *,
    carries_dates: bool,
    rows: Sequence[dict[str, Any]],
    run_date: date,
    grace_days: int,
) -> FreshnessVerdict:
    """Return the verdict; ``stale`` or ``empty`` fails the run closed."""
    if not carries_dates:
        return FreshnessVerdict(
            object_name=object_name,
            carries_dates=False,
            observed_end_date=None,
            verdict=NOT_APPLICABLE,
            detail="endpoint is registered as carrying no dates",
        )
    dates = observed_dates(object_name, rows)
    if not dates:
        return FreshnessVerdict(
            object_name=object_name,
            carries_dates=True,
            observed_end_date=None,
            verdict="empty",
            detail="dated endpoint returned zero dated rows",
        )
    observed_end = max(dates)
    floor = run_date - timedelta(days=grace_days)
    if observed_end < floor:
        return FreshnessVerdict(
            object_name=object_name,
            carries_dates=True,
            observed_end_date=observed_end.isoformat(),
            verdict="stale",
            detail=(
                f"observed end {observed_end.isoformat()} is older than "
                f"the freshness floor {floor.isoformat()}"
            ),
        )
    return FreshnessVerdict(
        object_name=object_name,
        carries_dates=True,
        observed_end_date=observed_end.isoformat(),
        verdict="fresh",
        detail=f"observed end {observed_end.isoformat()} meets the freshness floor",
    )
