"""Schwab source-branch finality: the cutoff-day NAV is a declared derivation or not a stale carry."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "provider_ledger_schwab_source_patch_test", ROOT / "tools" / "provider_ledger.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load provider-ledger builder")
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)

END = date(2026, 11, 6)
PRIOR = date(2026, 11, 5)
CATALOG = {
    "journal_counterparty_crosswalk": {
        "entries": [{"account_id": "account-a", "native_suffix": "123"}]
    }
}


def _pin(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _write(path: Path, value: object) -> Path:
    path.write_bytes(json.dumps(value, sort_keys=True).encode("utf-8"))
    return path


def _declaration(tmp_path: Path, **overrides: object) -> dict[str, Any]:
    """Account A holds 10 AAA (95.50 on 11-06) and 100.50 cash: official 11-06 NAV 1055.50."""

    closes = tmp_path / "AAA_yahoo_daily.csv"
    closes.write_text("date,close\n2026-11-05,90\n2026-11-06,95.5\n", encoding="utf-8")
    holdings = _write(
        tmp_path / "holdings.json",
        {
            "accounts": [
                {
                    "accountId": "EXT123",
                    "groupedPositions": [
                        {
                            "securityType": 1,
                            "holdingsRows": [
                                {"symbol": {"symbol": "AAA"}, "qty": {"qty": 10}}
                            ],
                        },
                        {
                            "securityType": 9,
                            "holdingsRows": [{"marketValue": {"val": 100.5}}],
                        },
                    ],
                }
            ]
        },
    )
    record = _write(
        tmp_path / "record.json", {"account-a": {"official_1106": "1055.50"}}
    )
    native = _write(
        tmp_path / "native.json",
        {
            "histories": {
                "11/05/2026": {"AccountValue": 1000.5},
                "11/06/2026": {"AccountValue": 1000.5},
            }
        },
    )
    declaration: dict[str, Any] = {
        "schema": "schwab-nav-source-derived-patch/1",
        "derivation_basis": "DERIVED_FROM_OFFICIAL_CLOSES",
        "patched_dates": ["2026-11-06"],
        "derivation_record": _pin(record),
        "replay": {
            "positions_capture": _pin(holdings),
            "closes": [{"symbol": "AAA", **_pin(closes)}],
            "cash_security_type": 9,
            "rounding": ledger._SCHWAB_REPLAY_ROUNDING,
        },
        "native_balances": [{"account_id": "account-a", **_pin(native)}],
        "patches": [
            {
                "account_id": "account-a",
                "original_values": {"2026-11-06": "1000.5"},
                "patched_values": {"2026-11-06": "1055.50"},
            }
        ],
    }
    declaration.update(overrides)
    return declaration


def _patch(tmp_path: Path, **overrides: object) -> Any:
    return ledger._schwab_source_derived_patch(
        {"derived_patch": _declaration(tmp_path, **overrides)}, END, CATALOG
    )


def _check(patch: Any, account: str, values: dict[date, Decimal]) -> None:
    ledger._check_schwab_source_cutoff_row(patch, account, values, END)


PATCHED = {PRIOR: Decimal("1000.5"), END: Decimal("1055.50")}


def test_a_declared_derivation_is_replayed_and_matches_the_selected_row(
    tmp_path: Path,
) -> None:
    patch = _patch(tmp_path)
    _check(patch, "account-a", PATCHED)
    assert patch.used == {"account-a"}


def test_no_declaration_is_an_empty_patch() -> None:
    patch = ledger._schwab_source_derived_patch({}, END, CATALOG)
    assert not patch.entries and not patch.used


def test_a_stale_carry_without_a_patch_is_refused() -> None:
    patch = ledger._schwab_source_derived_patch({}, END, CATALOG)
    with pytest.raises(ledger.ProviderLedgerError, match="repeats its prior day"):
        _check(patch, "account-a", {PRIOR: Decimal("1000.5"), END: Decimal("1000.5")})


def test_a_moved_row_without_a_patch_is_accepted() -> None:
    patch = ledger._schwab_source_derived_patch({}, END, CATALOG)
    _check(patch, "account-a", {PRIOR: Decimal("1000.5"), END: Decimal("1001")})


def test_a_flat_account_repeating_its_cash_is_accepted_only_with_the_record(
    tmp_path: Path,
) -> None:
    record = _write(
        tmp_path / "flat-record.json", {"account-f": {"official_1106": "3.81"}}
    )
    declaration = _declaration(tmp_path)
    declaration["derivation_record"] = _pin(record)
    declaration["patches"] = []
    declaration["native_balances"] = []
    declaration["replay"]["positions_capture"] = _pin(
        _write(tmp_path / "none.json", {"accounts": []})
    )
    patch = ledger._schwab_source_derived_patch(
        {"derived_patch": declaration}, END, CATALOG
    )
    flat = {PRIOR: Decimal("3.81"), END: Decimal("3.81")}
    _check(patch, "account-f", flat)
    with pytest.raises(ledger.ProviderLedgerError, match="repeats its prior day"):
        _check(patch, "account-g", flat)


def test_a_record_that_disagrees_with_the_repeated_value_is_refused(
    tmp_path: Path,
) -> None:
    record = _write(tmp_path / "bad-record.json", {"account-f": {"official_1106": "4"}})
    declaration = _declaration(tmp_path)
    declaration["derivation_record"] = _pin(record)
    declaration["patches"] = []
    declaration["native_balances"] = []
    declaration["replay"]["positions_capture"] = _pin(
        _write(tmp_path / "none.json", {"accounts": []})
    )
    patch = ledger._schwab_source_derived_patch(
        {"derived_patch": declaration}, END, CATALOG
    )
    with pytest.raises(ledger.ProviderLedgerError, match="repeats its prior day"):
        _check(patch, "account-f", {PRIOR: Decimal("3.81"), END: Decimal("3.81")})


def test_the_replay_is_exact_a_changed_patched_value_is_refused(tmp_path: Path) -> None:
    declaration = _declaration(tmp_path)
    declaration["patches"][0]["patched_values"]["2026-11-06"] = "1055.51"
    with pytest.raises(ledger.ProviderLedgerError, match="replay differs"):
        ledger._schwab_source_derived_patch(
            {"derived_patch": declaration}, END, CATALOG
        )


def test_a_selected_row_that_is_not_the_patched_value_is_refused(
    tmp_path: Path,
) -> None:
    patch = _patch(tmp_path)
    with pytest.raises(ledger.ProviderLedgerError, match="differ from selected"):
        _check(patch, "account-a", {PRIOR: Decimal("1000.5"), END: Decimal("1000.5")})


def test_a_patch_that_changes_another_row_is_refused(tmp_path: Path) -> None:
    patch = _patch(tmp_path)
    with pytest.raises(ledger.ProviderLedgerError, match="differ from selected"):
        _check(patch, "account-a", {PRIOR: Decimal("999"), END: Decimal("1055.50")})


def test_a_patched_row_equal_to_the_native_row_is_refused(tmp_path: Path) -> None:
    native = _write(
        tmp_path / "native2.json",
        {
            "histories": {
                "11/05/2026": {"AccountValue": 1000.5},
                "11/06/2026": {"AccountValue": 1055.5},
            }
        },
    )
    declaration = _declaration(tmp_path)
    declaration["native_balances"] = [{"account_id": "account-a", **_pin(native)}]
    declaration["patches"][0]["original_values"]["2026-11-06"] = "1055.5"
    patch = ledger._schwab_source_derived_patch(
        {"derived_patch": declaration}, END, CATALOG
    )
    with pytest.raises(ledger.ProviderLedgerError, match="differ from selected"):
        _check(patch, "account-a", PATCHED)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="other/1"),
        lambda d: d.update(derivation_basis="OTHER"),
        lambda d: d.update(patched_dates=["2026-11-05"]),
        lambda d: d.update(patched_dates=["2026-11-05", "2026-11-06"]),
        lambda d: d.update(unknown=1),
        lambda d: d["patches"][0].update(extra=1),
        lambda d: d["patches"].append(copy.deepcopy(d["patches"][0])),
    ],
)
def test_a_malformed_declaration_is_refused(tmp_path: Path, mutate: Any) -> None:
    declaration = _declaration(tmp_path)
    mutate(declaration)
    with pytest.raises(ledger.ProviderLedgerError):
        ledger._schwab_source_derived_patch(
            {"derived_patch": declaration}, END, CATALOG
        )


def test_a_declared_account_that_no_source_used_is_left_unused(tmp_path: Path) -> None:
    patch = _patch(tmp_path)
    assert set(patch.entries) != patch.used


# -- end to end through the source branch of the finality check ---------------------------------


def _source_run(
    tmp_path: Path, values: dict[str, str], declaration: object
) -> dict[str, Any]:
    from types import SimpleNamespace

    nav = tmp_path / "account-a.csv"
    nav.write_text(
        "account_id,account_name,date,account_value\n"
        + "".join(f"account-a,account-a,{d},{v}\n" for d, v in values.items()),
        encoding="utf-8",
    )
    data = nav.read_bytes()
    manifest = SimpleNamespace(
        artifacts=[
            SimpleNamespace(
                artifact_id="ingest/nav/account-a.csv",
                schema_name="schwab-wch-historical-balance",
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        ]
    )
    catalog = {
        **CATALOG,
        "accounts": [{"account_id": "account-a"}],
        "nav_observations": [
            {
                "account_id": "account-a",
                "as_of_date": d,
                "nav": {"value": v},
                "source_artifact_id": "ingest/nav/account-a.csv",
            }
            for d, v in values.items()
        ],
    }
    evidence: dict[str, Any] = {
        "roots": {"ingest-nav": str(tmp_path)},
        "sources": [
            {
                "account_id": "account-a",
                "artifact_id": "ingest/nav/account-a.csv",
                "expected_sha256": hashlib.sha256(data).hexdigest(),
                "expected_size_bytes": len(data),
                "relative_path": "account-a.csv",
                "role": "WCH_NAV_CSV",
                "root_id": "ingest-nav",
            }
        ],
    }
    if declaration is not None:
        evidence["derived_patch"] = declaration
    ctx = ledger._FinalityContext(
        {}, Path("."), b"", "", 0, manifest, catalog, Path("."), END,
        {"basis": "SELECTED_SOURCE_DATE_LABELS", "provider_finality_guarantee": False},
        evidence,
    )  # fmt: skip
    return ledger._finality_schwab(ctx)


def test_the_source_branch_accepts_a_derived_cutoff_row_and_discloses_the_basis(
    tmp_path: Path,
) -> None:
    result = _source_run(
        tmp_path,
        {"2026-11-05": "1000.5", "2026-11-06": "1055.50"},
        _declaration(tmp_path),
    )
    assert (
        result["basis"] == "SELECTED_HISTORICAL_NAV_LABEL_WITH_DECLARED_DERIVED_PATCH"
    )
    assert result["provider_finality_guarantee"] is False


def test_the_source_branch_refuses_a_stale_carry_cutoff_row_without_a_declaration(
    tmp_path: Path,
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="repeats its prior day"):
        _source_run(tmp_path, {"2026-11-05": "1000.5", "2026-11-06": "1000.5"}, None)


def test_the_source_branch_keeps_its_basis_when_no_cutoff_row_is_a_carry(
    tmp_path: Path,
) -> None:
    result = _source_run(
        tmp_path, {"2026-11-05": "1000.5", "2026-11-06": "1001.5"}, None
    )
    assert result["basis"] == "SELECTED_HISTORICAL_NAV_LABEL_CAPTURE_TIME_UNKNOWN"


def test_the_source_branch_refuses_a_declared_patch_the_selected_row_does_not_carry(
    tmp_path: Path,
) -> None:
    declaration = _declaration(tmp_path)
    with pytest.raises(ledger.ProviderLedgerError, match="differ from selected"):
        _source_run(
            tmp_path, {"2026-11-05": "1000.5", "2026-11-06": "1001.5"}, declaration
        )
