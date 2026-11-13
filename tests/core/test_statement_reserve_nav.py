from __future__ import annotations

from datetime import date
import hashlib
import json
from typing import Any

import pytest

import statement_reserve_nav as srn

ACCOUNT = "account-" + "b" * 16


def _catalog() -> dict[str, Any]:
    def row(day: str, value: str) -> dict[str, Any]:
        return {
            "account_id": ACCOUNT,
            "as_of_date": day,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "record_id": f"john_hancock:{day}",
        }

    return {
        "catalog_schema_version": 1,
        "accounts": [{"account_id": ACCOUNT, "provider": "JOHN_HANCOCK"}],
        "nav_observations": [
            row("2026-06-30", "100.00"),
            row("2026-09-30", "110.00"),
            row("2026-12-31", "999.00"),
        ],
    }


def test_a_statement_point_after_the_cutoff_is_never_used() -> None:
    """Kill test (#900 B): the 2026-12-31 closing is post-cutoff evidence."""

    facts = srn.statement_reserve_facts(_catalog(), date(2026, 11, 6))
    assert [r["as_of_date"] for r in facts["nav"]] == ["2026-06-30", "2026-09-30"]
    windows = [(a["effective_start"], a["effective_end"]) for a in facts["admissions"]]
    # authoritative from its period end to the day before the next point; last to cutoff
    assert windows == [("2026-06-30", "2026-09-29"), ("2026-09-30", "2026-11-06")]
    assert facts["assignments"][0]["pool_id"] == srn.RESERVE_POOL_ID


def test_a_catalog_that_differs_from_its_pin_is_refused() -> None:
    raw = json.dumps(_catalog()).encode()
    pin = {
        "path": "x",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    assert srn.verified_catalog(raw, pin)["catalog_schema_version"] == 1
    with pytest.raises(srn.StatementReserveError, match="differs from its pin"):
        srn.verified_catalog(raw + b" ", pin)
    with pytest.raises(srn.StatementReserveError, match="differs from its pin"):
        srn.verified_catalog(raw, {**pin, "sha256": "0" * 64})


def test_the_reserve_and_total_liquid_inputs_become_available_with_the_statement_pin() -> (
    None
):
    import portfolio_baseline as baseline

    facts = srn.statement_reserve_facts(_catalog(), date(2026, 11, 6))
    latest = baseline._latest_usd_navs(facts["nav"], date(2026, 11, 6))
    reserve = baseline._current_reserve(
        valuation_date=date(2026, 11, 6),
        accounts=facts["accounts"],
        assignments=facts["assignments"],
        nav=facts["nav"],
        provider_admissions=facts["admissions"],
        latest=latest,
    )
    assert reserve["status"] == "AVAILABLE"
    assert reserve["as_of_date"] == "2026-09-30"
    assert reserve["members"][0]["authority"] == "AUTHORITATIVE"
    # without the pin the account has no NAV source and the reserve is not ready
    empty = baseline._current_reserve(
        valuation_date=date(2026, 11, 6),
        accounts=[],
        assignments=facts["assignments"],
        nav=[],
        provider_admissions=[],
        latest={},
    )
    assert empty["status"] == "NOT_READY_AT_CUTOFF"
