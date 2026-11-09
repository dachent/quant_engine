"""The driver's date handling over REAL retained Schwab transaction bytes (offline rehearsal).

A live capture's first governed request is the first time the driver sees Schwab's bytes, so
parse and date handling is rehearsed here over every retained page: the refresh-20260902 raw
pages and, when present, the staged normalized rows of a late-pull run (set
``QE_SCHWAB_STAGED_RUN`` to a staging run directory). Skipped where the retained evidence is
not mounted; synthetic coverage of the same shapes lives in test_schwab_make_evidence.py.
"""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path

import pytest

from tools import schwab_capture_driver as driver
from tools import schwab_rollback as rollback

RETAINED_PAGES = Path(
    "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/"
    "dataSchwab/source-runs/refresh-20260902/pages"
)
STAGED = os.environ.get("QE_SCHWAB_STAGED_RUN")


def _rows_of(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("brokerageTransactions") or []
    return [row for row in rows if isinstance(row, dict)]


def _check(rows: list[dict[str, object]]) -> tuple[int, int]:
    as_of = 0
    for row in rows:
        text = str(row["transactionDate"])
        posted = driver._txn_date(text)
        assert isinstance(posted, date)
        assert rollback._date("account", text) == posted  # one rule in both tools
        if " as of " in text:
            as_of += 1
            assert posted == driver._txn_date(text.split(" as of ")[0])
    return len(rows), as_of


@pytest.mark.skipif(not RETAINED_PAGES.is_dir(), reason="retained pages not mounted")
def test_every_retained_raw_page_row_has_a_readable_posting_date() -> None:
    total = as_of = 0
    for page in sorted(RETAINED_PAGES.glob("account-*/[0-9][0-9][0-9][0-9].json")):
        count, flagged = _check(_rows_of(page))
        total += count
        as_of += flagged
    assert total > 3000
    assert as_of > 0  # the "MM/DD/YYYY as of MM/DD/YYYY" shape is in the retained bytes


@pytest.mark.skipif(not STAGED, reason="QE_SCHWAB_STAGED_RUN not set")
def test_every_staged_late_pull_row_has_a_readable_posting_date() -> None:
    assert STAGED is not None
    rows_dir = Path(STAGED) / "transactions" / "normalized" / "wch" / "transactions"
    total = 0
    for path in sorted(rows_dir.glob("*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        count, _ = _check(rows)
        total += count
    assert total > 0
