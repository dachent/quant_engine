from __future__ import annotations

from datetime import date
from typing import Any

import provider_ledger as ledger

RESERVE = "account-" + "a" * 16


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


def _admission_dicts(windows: Any) -> list[dict[str, Any]]:
    out = []
    for a in _admit(windows):
        row = a.to_dict() if hasattr(a, "to_dict") else dict(a.__dict__)
        out.append(
            {
                **row,
                "evidence_layer": getattr(a.evidence_layer, "value", a.evidence_layer),
                "disposition": a.disposition.value,
                "effective_start": a.effective_start.isoformat(),
                "effective_end": a.effective_end.isoformat(),
            }
        )
    return out


def _reserve(windows: Any) -> dict[str, Any]:
    import portfolio_baseline as baseline

    nav = [_nav("2026-01-02"), _nav("2026-01-03")]
    latest = baseline._latest_usd_navs(nav, date(2026, 1, 31))
    return baseline._current_reserve(
        valuation_date=date(2026, 1, 31),
        accounts=[{"account_id": RESERVE, "provider": "ibkr"}],
        assignments=[
            {
                "account_id": RESERVE,
                "pool_id": baseline._RESERVE_POOL_ID,
                "effective_start": "2025-01-01",
                "effective_end": None,
            }
        ],
        nav=nav,
        provider_admissions=_admission_dicts(windows),
        latest=latest,
    )


def test_the_reserve_reads_the_declared_window_and_stays_not_ready_without_one() -> (
    None
):
    """#900 end to end: declared windows => AVAILABLE (a negative dust NAV is a value);
    no window => the reserve is not ready (the pre-#900 behaviour)."""

    declared = _reserve(
        {RESERVE: [(date(2026, 1, 1), date(2026, 1, 31), "EXACT_IDENTITY_CLOSURE")]}
    )
    assert declared["status"] == "AVAILABLE"
    assert declared["members"][0]["authority"] == "AUTHORITATIVE"
    assert declared["members"][0]["value"]["value"] == "-0.0016"
    assert _reserve(None)["status"] == "NOT_READY_AT_CUTOFF"


def _declaration_file(tmp_path: Any) -> tuple[Any, dict[str, Any]]:
    import hashlib
    import json

    raw = json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "account-financing-declaration",
            "as_of_date": "2026-01-31",
            "accounts": [
                {
                    "account_id": RESERVE,
                    "scope": "RESERVE",
                    "facility": "NO_MARGIN_FACILITY",
                    "verification": "VERIFIED",
                    "evidence_ref": "e",
                }
            ],
            "sleeve_roles": [
                {
                    "sleeve_alias": "r-01",
                    "role": "RESERVE",
                    "ledger_account_id": RESERVE,
                    "nav_authority": "ACCOUNT_LIVE",
                    "admit_windows": [
                        {
                            "start": "2026-01-01",
                            "end": "2026-01-31",
                            "basis": "EXACT_IDENTITY_CLOSURE",
                        }
                    ],
                    "evidence_ref": "e",
                }
            ],
        }
    ).encode()
    path = tmp_path / "declaration.json"
    path.write_bytes(raw)
    return path, {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def test_the_spec_pins_the_declaration_the_windows_come_from(tmp_path: Any) -> None:
    import pytest

    _path, ref = _declaration_file(tmp_path)
    windows = ledger._reserve_nav_windows(ref)
    assert windows == {
        RESERVE: [(date(2026, 1, 1), date(2026, 1, 31), "EXACT_IDENTITY_CLOSURE")]
    }
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        ledger._reserve_nav_windows({**ref, "sha256": "0" * 64})
