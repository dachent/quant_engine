from __future__ import annotations

from datetime import date
from typing import Any

import provider_ledger as ledger

RESERVE = "account-" + "r" * 16


def _nav(day: str, status: str = "UNMAPPED") -> dict[str, Any]:
    return {
        "account_id": RESERVE,
        "as_of_date": day,
        "mapping_status": status,
        "provider_id": "ibkr",
        "record_id": f"ibkr:{day}",
        "source_record_id": day,
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": "-0.0016"},
    }


def _admit(windows: Any) -> list[Any]:
    rows = [_nav("2026-01-02"), _nav("2026-01-03"), _nav("2026-03-05")]
    return [
        a
        for a in ledger._admissions(
            {"nav_observations": rows}, [], reserve_nav_windows=windows
        )
        if a.fact_type == "nav_observations"
    ]


def test_a_reserve_nav_is_reference_only_without_a_declared_role() -> None:
    """Before #900: every UNMAPPED reserve NAV fell to REFERENCE_ONLY."""

    admissions = _admit(None)
    assert [a.disposition.value for a in admissions] == ["REFERENCE_ONLY"]


def test_a_declared_reserve_nav_is_authoritative_inside_its_windows_only() -> None:
    windows = {
        RESERVE: [
            (date(2026, 1, 1), date(2026, 1, 31), "EXACT_IDENTITY_CLOSURE"),
            (date(2026, 2, 1), date(2026, 2, 28), "DERIVED_BRIDGE_WITH_CLOSURE"),
        ]
    }
    admissions = _admit(windows)
    spans = [
        (
            a.effective_start.isoformat(),
            a.effective_end.isoformat(),
            a.disposition.value,
        )
        for a in admissions
    ]
    # the exact window is the observed span clipped to the window; the bridge window is admitted as declared;
    # nothing is admitted for the unbridged March row
    assert spans == [
        ("2026-01-02", "2026-01-31", "AUTHORITATIVE"),
        ("2026-02-01", "2026-02-28", "AUTHORITATIVE"),
    ]
    assert all(a.account_id == RESERVE for a in admissions)
