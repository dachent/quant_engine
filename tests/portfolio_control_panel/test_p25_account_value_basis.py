"""Pass 25: an account NAV row declared derived carries the value_basis label the position rows carry."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_panel_render_audit_543 import (
    CUTOFF,
    _allocation_payload,
    _manifest_payload,
    html,
    run,
)

DERIVED_DAY = "2026-09-17"
LABEL = "Derived from official closes"


def _value_basis() -> dict[str, Any]:
    return {
        "basis": "DERIVED_FROM_OFFICIAL_CLOSES",
        "derivation_record_sha256": "d" * 64,
        "finality_evidence_sha256": "e" * 64,
        "official_closes_manifest_sha256": "c" * 64,
        "provider_finality_guarantee": False,
    }


def _summary(account_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "account_label": f"Label {account_id}",
        "as_of_date": DERIVED_DAY,
        "latest_value_usd": "1000.00",
        "provider": "SYNTH",
        "role": "SYSTEMATIC",
        "status": "AVAILABLE_LATEST_OBSERVATION",
        **fields,
    }


def _rows(table: str) -> list[str]:
    return [row for row in table.split("<tr") if 'data-account-id="' in row]


def _manifest_table(tmp_path: Path, summaries: list[dict[str, Any]]) -> str:
    payload = _manifest_payload()
    base_row = payload["account_manifest"]["rows"][0]
    payload["account_manifest"]["rows"] = [
        {**base_row, "account_id": "a0", "end": DERIVED_DAY},
        {**base_row, "account_id": "a1", "end": DERIVED_DAY},
    ]
    payload["account_summary"] = summaries
    return html(
        run(tmp_path, payload, "renderAccountManifest"), "#account-manifest-table"
    )


def test_account_nav_row_with_value_basis_says_so_and_a_plain_row_does_not(
    tmp_path: Path,
) -> None:
    table = _manifest_table(
        tmp_path,
        [
            _summary("a0", value_basis=_value_basis(), capture_date="2026-09-18"),
            _summary("a1"),
        ],
    )
    derived, plain = _rows(table)

    assert LABEL in derived
    assert "as of Sep 17, 2026 (observed Sep 18, 2026)" in derived
    assert "captured" not in derived
    assert "official closes manifest cccccccc; evidence eeeeeeee" in derived
    assert "no provider finality guarantee" in derived
    assert LABEL not in plain and "captured" not in plain


def test_account_nav_date_is_the_rows_own_never_a_literal(tmp_path: Path) -> None:
    table = _manifest_table(
        tmp_path,
        [
            _summary("a0", value_basis=_value_basis(), capture_date="2026-03-04"),
            _summary("a1", value_basis=_value_basis()),
        ],
    )
    first, second = _rows(table)

    assert (
        "as of Sep 17, 2026 (observed Mar 4, 2026)" in first
        and "Sep 18, 2026" not in table
    )
    assert "as of Sep 17, 2026" in second and "observed" not in second


def test_account_nav_label_needs_the_summary_row_of_that_observation(
    tmp_path: Path,
) -> None:
    # A derived summary row for another day than the epoch row's end is not that row's value.
    table = _manifest_table(
        tmp_path,
        [_summary("a0", value_basis=_value_basis(), as_of_date="2026-08-01")],
    )

    assert LABEL not in table


def test_reserve_nav_in_the_allocation_table_carries_the_label(tmp_path: Path) -> None:
    custody = {
        "reserve": {
            "as_of_date": CUTOFF,
            "current_value": {"currency": "USD", "value": "98000.00"},
            "status": "AVAILABLE",
        }
    }

    def reserve_row(summaries: list[dict[str, Any]]) -> str:
        payload = _allocation_payload(custody_current_capital=custody)
        payload["account_summary"] = summaries
        table = html(
            run(tmp_path, payload, "renderAllocationTable"), "#allocation-table"
        )
        return [row for row in table.split("<tr>") if re.search(r"Reserve", row)][0]

    marked = reserve_row(
        [
            _summary(
                "r0",
                role="RESERVE",
                value_basis=_value_basis(),
                capture_date="2026-09-18",
            )
        ]
    )
    unmarked = reserve_row([_summary("r0", role="RESERVE")])

    assert "$98,000" in marked and LABEL in marked
    assert "as of Sep 17, 2026 (observed Sep 18, 2026)" in marked
    assert "captured" not in marked
    assert "$98,000" in unmarked and LABEL not in unmarked

    # the reserve is an aggregate: one derived member among several is not the aggregate's basis
    derived = _summary(
        "r0", role="RESERVE", value_basis=_value_basis(), capture_date="2026-09-18"
    )
    partial = reserve_row([derived, _summary("r1", role="RESERVE")])
    assert "$98,000" in partial and LABEL not in partial
    other_basis = {**_value_basis(), "basis": "SOME_OTHER_BASIS"}
    mixed = reserve_row(
        [derived, _summary("r1", role="RESERVE", value_basis=other_basis)]
    )
    assert "$98,000" in mixed and LABEL not in mixed
    every = reserve_row(
        [derived, _summary("r1", role="RESERVE", value_basis=_value_basis())]
    )
    assert "$98,000" in every and LABEL in every
