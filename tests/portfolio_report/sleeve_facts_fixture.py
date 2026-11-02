"""A synthetic ``sleeve-account-facts`` artifact for the derived-sleeve report fixture (#624).

The fixture of ``test_derived_sleeve_report`` has a parent account ``account-a`` with a
retired sleeve s1 (strategy-9) and the open sleeve s2 (strategy-1) whose observation
trace opens on 2026-07-02 at NAV 100 and closes on 2026-07-30 at NAV 104 with no flow.
These facts carry the same sleeve in the facts' own columns (entry-day own P&L, allocated
costs folded into ``nav_end_after_alloc``) so that every disclosure derivation closes, and
a perimeter pseudo-account whose NAV makes ``sleeve NAVs + bucket = account NAV``.
Nothing here is real data.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

PARENT = "account-a"
S1 = f"{PARENT}-s1"
S2 = f"{PARENT}-s2"
UNALLOCATED = f"{PARENT}-unallocated"
CUTOFF = "2026-07-30"
ENTRY = "2026-07-02"
SLEEVE_CLOSING = Decimal("104")


def _day(
    day: str,
    *,
    nav_prev: str,
    flow_in: str,
    flow_out: str,
    nav_end: str,
    after: str,
    own_pnl: str,
    alloc: str,
    event: str = "",
) -> dict[str, Any]:
    return {
        "day": day,
        "event": event,
        "event_evidence": "DERIVED" if event else "",
        "evidence_ref": None,
        "live": True if event else None,
        "counterparty": None,
        "nav_prev": nav_prev,
        "flow_in": flow_in,
        "flow_out": flow_out,
        "nav_end": nav_end,
        "nav_end_after_alloc": after,
        "value": nav_end,
        "price_pnl": own_pnl,
        "commission": "0",
        "dividend": "0",
        "own_pnl": own_pnl,
        "exposure": "100",
        "exposure_share": "1",
        "alloc_total": alloc,
    }


def _leg(
    account: str, other: str, cls: str, day: str, amount: str, pair: str
) -> dict[str, Any]:
    return {
        "pair_id": pair,
        "account_id": account,
        "class": cls,
        "flow_type": "TRANSFER:INTERNAL:SLEEVE",
        "event_date": day,
        "nav_recognition_date": day,
        "amount": amount,
        "counterparty_account_id": other,
        "trace": {
            "kind": "UNALLOCATED_CASH",
            "pairing": "DECLARED_UNALLOCATED",
            "from_sleeve": None,
            "to_sleeve": None,
            "evidence_refs": ["synthetic"],
        },
    }


def build_facts(
    *,
    account_value: Decimal,
    tag_map_sha256: str,
    ledger_generation_id: str = "a" * 64,
    provider_id: str = "synthetic",
) -> dict[str, Any]:
    """Facts whose sleeves and bucket close on ``account_value`` at the cutoff."""

    bucket = account_value - SLEEVE_CLOSING
    return {
        "schema_version": 1,
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": PARENT,
        "provider_id": provider_id,
        "base_currency": "USD",
        "window": {"first_day": "2026-07-01", "last_day": CUTOFF},
        "bindings": {
            "tag_map_sha256": tag_map_sha256,
            "parent_ledger_generation_id": ledger_generation_id,
            "declared_inputs_sha256": "1" * 64,
            "account_rows_sha256": "2" * 64,
            "builder_code": {
                "files": {"tools/sleeve_nav.py": "3" * 64},
                "same_second_tie_break": {"rule": "SYNTHETIC", "sha256": "4" * 64},
                "sha256": "5" * 64,
            },
        },
        "closure_policy": {
            "tolerance_per_day": "1",
            "tolerance_window": "1",
            "max_abs_unexplained": "0",
            "window_unexplained": "0",
        },
        "sleeves": [
            {
                "sleeve_id": "s1",
                "account_id": S1,
                "strategy_id": "strategy-9",
                "first_day": "2026-07-01",
                "last_day": "2026-07-20",
                "days": [
                    _day(
                        "2026-07-01",
                        nav_prev="0",
                        flow_in="60",
                        flow_out="0",
                        nav_end="59",
                        after="59",
                        own_pnl="-1",
                        alloc="0",
                        event="IN",
                    ),
                    _day(
                        "2026-07-20",
                        nav_prev="59",
                        flow_in="0",
                        flow_out="-61",
                        nav_end="0",
                        after="0",
                        own_pnl="2",
                        alloc="0",
                        event="EXIT",
                    ),
                ],
            },
            {
                "sleeve_id": "s2",
                "account_id": S2,
                "strategy_id": "strategy-1",
                "first_day": ENTRY,
                "last_day": CUTOFF,
                "days": [
                    _day(
                        ENTRY,
                        nav_prev="0",
                        flow_in="102",
                        flow_out="0",
                        nav_end="99.5",
                        after="100",
                        own_pnl="-2.5",
                        alloc="0.5",
                        event="IN",
                    ),
                    _day(
                        CUTOFF,
                        nav_prev="99.5",
                        flow_in="0",
                        flow_out="0",
                        nav_end="104",
                        after=str(SLEEVE_CLOSING),
                        own_pnl="4.5",
                        alloc="-0.5",
                    ),
                ],
            },
        ],
        "perimeter_account": {
            "account_id": UNALLOCATED,
            "opening_nav": "0",
            "days": [
                {
                    "day": CUTOFF,
                    "nav_end": str(bucket),
                    "external_flow": "0",
                    "internal_flow_net": "0",
                    "residual_pnl": "0",
                }
            ],
        },
        "flows": [
            _leg(
                UNALLOCATED, S1, "INTERNAL_OUT", "2026-07-01", "60", "2026-07-01:0001"
            ),
            _leg(S1, UNALLOCATED, "INTERNAL_IN", "2026-07-01", "60", "2026-07-01:0001"),
            _leg(
                S1, UNALLOCATED, "INTERNAL_OUT", "2026-07-20", "61", "2026-07-20:0001"
            ),
            _leg(UNALLOCATED, S1, "INTERNAL_IN", "2026-07-20", "61", "2026-07-20:0001"),
            _leg(UNALLOCATED, S2, "INTERNAL_OUT", ENTRY, "102", "2026-07-02:0001"),
            _leg(S2, UNALLOCATED, "INTERNAL_IN", ENTRY, "102", "2026-07-02:0001"),
        ],
        "allocated_cost": {
            "convention": "EXPOSURE_SHARE",
            "folded_into": "nav_end_after_alloc",
            "account_costs": [
                {
                    "day": ENTRY,
                    "interest": "0",
                    "fees": "0",
                    "dividend_accrual": "0.5",
                },
                {
                    "day": CUTOFF,
                    "interest": "0",
                    "fees": "0",
                    "dividend_accrual": "-0.5",
                },
            ],
            "sleeve_allocations": [
                {
                    "day": ENTRY,
                    "sleeve_id": "s2",
                    "alloc_interest": "0",
                    "alloc_fees": "0",
                    "alloc_dividend_accrual": "0.5",
                    "alloc_total": "0.5",
                },
                {
                    "day": CUTOFF,
                    "sleeve_id": "s2",
                    "alloc_interest": "0",
                    "alloc_fees": "0",
                    "alloc_dividend_accrual": "-0.5",
                    "alloc_total": "-0.5",
                },
            ],
        },
        "closure": [
            {
                "day": CUTOFF,
                "account_pnl": "0",
                "sleeves_own_plus_alloc": "0",
                "residual_and_post_exit": "0",
                "unexplained": "0",
                "unallocated_cash_flow": "0",
            }
        ],
        "disclosures": {
            "manual_assignments": [],
            "reconciliation_gaps": [],
            "reconciliation_scalings": [],
            "offsetting_residues": [],
            "unallocated_dividends": [],
        },
    }


def edited(facts: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A deep copy of ``facts`` with top-level keys replaced."""

    result = deepcopy(facts)
    result.update(changes)
    return result
