"""R3 (3): the weekly source floor follows whether the cutoff closes its own week.

The week closes on its Friday.  For a Monday to Thursday cutoff the last completed week is
the previous one (its Friday is the floor); for a Friday, Saturday or Sunday cutoff the
cutoff's own week is complete, so a source that ends a week earlier is stale.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from test_instrument_weekly import _context, _weekly

from acceptance.validate import checks

FRIDAY, SATURDAY, SUNDAY = date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)


def _klasses(tmp_path: Path, cutoff: date, source_as_of: str) -> list[str]:
    context = _context(tmp_path, cutoff=cutoff, source_as_of=source_as_of)
    return [f.klass for f in _weekly(list(checks.check_weekly_current_week(context)))]


@pytest.mark.parametrize(
    ("cutoff", "source", "blocks"),
    [
        (FRIDAY, "2026-09-18", False),
        (FRIDAY, "2026-09-17", True),
        (FRIDAY, "2026-09-11", True),
        (SATURDAY, "2026-09-18", False),
        (SATURDAY, "2026-09-19", False),
        (SATURDAY, "2026-09-17", True),
        (SATURDAY, "2026-09-11", True),
        (SUNDAY, "2026-09-18", False),
        (SUNDAY, "2026-09-20", False),
        (SUNDAY, "2026-09-17", True),
        (SUNDAY, "2026-09-11", True),
    ],
)
def test_a_cutoff_that_closes_its_week_needs_that_weeks_data(
    tmp_path: Path, cutoff: date, source: str, blocks: bool
) -> None:
    expected = [checks.BLOCKER_AUTHORITY] if blocks else []
    assert _klasses(tmp_path, cutoff, source) == expected


@pytest.mark.parametrize(
    ("cutoff", "source", "blocks"),
    [
        (date(2026, 9, 21), "2026-09-18", False),
        (date(2026, 9, 21), "2026-09-17", True),
        (date(2026, 9, 17), "2026-09-11", False),
        (date(2026, 9, 17), "2026-09-10", True),
        (date(2026, 9, 17), "2026-09-17", False),
    ],
)
def test_a_cutoff_inside_its_week_keeps_the_previous_friday_floor(
    tmp_path: Path, cutoff: date, source: str, blocks: bool
) -> None:
    expected = [checks.BLOCKER_AUTHORITY] if blocks else []
    assert _klasses(tmp_path, cutoff, source) == expected
