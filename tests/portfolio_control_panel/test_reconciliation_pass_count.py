"""B11: the panel's "Reconciliations passed" counter counts every passing status.

The baseline publishes reconciliation checks whose status starts with ``PASS``:
``PASS`` and also ``PASS_WITH_EXPLICIT_RECONCILIATION`` (the provider-ledger to baseline
check, which passes with its reconciliation stated).  The shell paints every ``PASS*``
status as accepted, the acceptance validator counts them, and the manifest check
accepts them, but the counter counted the exact word ``PASS`` only, so the same rows
read "1 of 4" in the counter and passed in the table.
"""

from __future__ import annotations

from typing import Any

import portfolio_control_panel as panel


def _rows(*statuses: str) -> list[dict[str, Any]]:
    return [{"check_id": f"check-{n}", "status": s} for n, s in enumerate(statuses)]


def test_a_check_that_passes_with_an_explicit_reconciliation_is_counted() -> None:
    rows = _rows(
        "PASS",
        "PASS_WITH_EXPLICIT_RECONCILIATION",
        "NOT_READY",
        "FAIL",
    )

    assert panel._reconciliation_passed(rows) == 2


def test_only_statuses_that_start_with_pass_are_counted() -> None:
    rows = _rows("PASS", "PASS_WITH_DECLARED_VARIANCE", "NOT_PASS", "UNAVAILABLE", "")

    assert panel._reconciliation_passed(rows) == 2
    assert panel._reconciliation_passed([]) == 0
