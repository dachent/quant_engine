"""Schwab finality: native capture plus a declared, disclosed derived patch."""

from __future__ import annotations

import copy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "provider_ledger_schwab_derived_patch_test", ROOT / "tools" / "provider_ledger.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load provider-ledger builder")
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)

END = date(2026, 9, 17)
SCHEMA = "schwab-wch-historical-balance"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pin(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path), "sha256": _sha(data), "size_bytes": len(data)}


def _write(path: Path, value: object) -> Path:
    path.write_bytes(json.dumps(value, sort_keys=True).encode("utf-8"))
    return path


def _build(tmp_path: Path, *, captured: str = "2026-09-18T04:59:12.765Z"):
    """Account A: native 09-17 is stale, selected is derived. Account B: untouched."""

    nav_rows = []
    artifacts = []
    receipt = []
    checks = []
    patches = []
    for alias, account, native_1, selected_1 in (
        ("aaa", "account-a", "100.00", "110.50"),
        ("bbb", "account-b", "50.00", "50.00"),
    ):
        native = {
            "9/16/2026": {"AccountValue": "100.00" if alias == "aaa" else "50.00"},
            "9/17/2026": {"AccountValue": native_1},
        }
        native_path = _write(tmp_path / f"{alias}-nav.json", {"histories": native})
        original_sha = _sha(("orig-" + alias).encode())
        selected_sha = (
            _sha(("sel-" + alias).encode()) if native_1 != selected_1 else original_sha
        )
        artifact_id = f"wch/nav/{alias}.csv"
        artifacts.append(
            SimpleNamespace(
                artifact_id=artifact_id,
                schema_name=SCHEMA,
                sha256=selected_sha,
                size_bytes=10,
            )
        )
        for day, value in (
            ("2026-09-16", native["9/16/2026"]["AccountValue"]),
            ("2026-09-17", selected_1),
        ):
            nav_rows.append(
                {
                    "account_id": account,
                    "as_of_date": day,
                    "nav": {"value": value},
                    "source_artifact_id": artifact_id,
                }
            )
        receipt.append(
            {
                "alias": alias,
                "captured_at_utc": captured,
                "http_status": 200,
                "date_count": 2,
                "nav_sha256": _sha(native_path.read_bytes()),
            }
        )
        checks.append(
            {
                "native_sha256": _sha(native_path.read_bytes()),
                "normalized_sha256": original_sha,
            }
        )
        if native_1 != selected_1:
            patches.append(
                {
                    "account_id": account,
                    "native_sha256": _sha(native_path.read_bytes()),
                    "original_normalized_sha256": original_sha,
                    "patched_sha256": selected_sha,
                    "original_values": {"2026-09-17": native_1},
                    "patched_values": {"2026-09-17": selected_1},
                }
            )
    receipt_path = _write(tmp_path / "nav-capture-receipt.json", receipt)
    closes = _write(tmp_path / "closes-manifest.json", {"symbols": 1})
    close_csv = tmp_path / "XYZ_yahoo_daily.csv"
    close_csv.write_text("date,close\n2026-09-16,9.90\n2026-09-17,10.05\n")
    positions = _write(
        tmp_path / "positions.json",
        {
            "accounts": [
                {
                    "accountId": "***001",
                    "groupedPositions": [
                        {
                            "securityType": 2,
                            "holdingsRows": [
                                {
                                    "symbol": {"symbol": "XYZ"},
                                    "qty": {"qty": 10},
                                    "marketValue": {"val": 100.5},
                                }
                            ],
                        },
                        {
                            "securityType": 9,
                            "holdingsRows": [
                                {"symbol": {"symbol": ""}, "marketValue": {"val": 10}}
                            ],
                        },
                    ],
                },
                {
                    "accountId": "***002",
                    "groupedPositions": [
                        {
                            "securityType": 9,
                            "holdingsRows": [
                                {"symbol": {"symbol": ""}, "marketValue": {"val": 50}}
                            ],
                        }
                    ],
                },
            ]
        },
    )
    derivation = _write(
        tmp_path / "derivation.json", {"account-a": {"official_0917": "110.50"}}
    )
    evidence = {
        "receipt_path": str(receipt_path),
        "receipt_sha256": _sha(receipt_path.read_bytes()),
        "receipt_size_bytes": receipt_path.stat().st_size,
        "checks": checks,
        "derived_patch": {
            "schema": "schwab-nav-derived-patch/1",
            "derivation_basis": "DERIVED_FROM_OFFICIAL_CLOSES",
            "patched_dates": ["2026-09-17"],
            "official_closes_manifest": _pin(closes),
            "derivation_record": _pin(derivation),
            "replay": {
                "positions_capture": _pin(positions),
                "closes": [{"symbol": "XYZ", **_pin(close_csv)}],
                "cash_security_type": 9,
                "rounding": "SUM_QUANTITY_X_CLOSE_QUANTIZE_0.01_HALF_EVEN_PLUS_CASH",
            },
            "patches": patches,
        },
    }
    manifest = SimpleNamespace(artifacts=artifacts)
    catalog = {
        "nav_observations": nav_rows,
        "journal_counterparty_crosswalk": {
            "entries": [
                {"native_suffix": "001", "account_id": "account-a"},
                {"native_suffix": "002", "account_id": "account-b"},
            ]
        },
    }
    return manifest, catalog, evidence


def _run(manifest: Any, catalog: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    ctx = ledger._FinalityContext(
        {},
        Path("."),
        b"",
        "",
        0,
        manifest,
        catalog,
        Path("."),
        END,
        {
            "basis": "SELECTED_SOURCE_DATE_LABELS",
            "provider_finality_guarantee": False,
            "native_timezone": None,
            "native_calendar": None,
            "capture_start_utc": None,
            "capture_end_utc": None,
        },
        evidence,
    )
    return ledger._finality_schwab(ctx)


def test_declared_derived_patch_is_disclosed_and_not_a_provider_guarantee(
    tmp_path: Path,
) -> None:
    result = _run(*_build(tmp_path))
    assert (
        result["basis"]
        == "CAPTURED_NATIVE_HISTORICAL_NAV_LABEL_WITH_DECLARED_DERIVED_PATCH"
    )
    assert result["provider_finality_guarantee"] is False
    assert result["capture_end_utc"] == "2026-09-18T04:59:12.765Z"


def test_unpatched_capture_keeps_plain_native_basis(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    for art in manifest.artifacts:
        art.sha256 = {_sha(b"sel-aaa"): evidence["checks"][0]["normalized_sha256"]}.get(
            art.sha256, art.sha256
        )
    for row in catalog["nav_observations"]:
        if row["account_id"] == "account-a" and row["as_of_date"] == "2026-09-17":
            row["nav"]["value"] = "100.00"
    del evidence["derived_patch"]
    assert _run(manifest, catalog, evidence)["basis"] == (
        "CAPTURED_NATIVE_HISTORICAL_NAV_LABEL"
    )


def test_patched_selection_without_declaration_is_rejected(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    del evidence["derived_patch"]
    with pytest.raises(ledger.ProviderLedgerError, match="account scope differs"):
        _run(manifest, catalog, evidence)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda e: e["derived_patch"]["patches"][0].update(
                original_normalized_sha256="0" * 64
            ),
            "original binding differs",
        ),
        (
            lambda e: e["derived_patch"]["patches"][0]["patched_values"].update(
                {"2026-09-17": "1.00"}
            ),
            "replay differs",
        ),
        (
            lambda e: e["derived_patch"]["patches"][0]["original_values"].update(
                {"2026-09-17": "1.00"}
            ),
            "rows differ from selected",
        ),
        (
            lambda e: e["derived_patch"].update(derivation_basis="PROVIDER_FINAL"),
            "declaration differs",
        ),
        (
            lambda e: e["derived_patch"]["derivation_record"].update(sha256="0" * 64),
            "hash differs",
        ),
        (
            lambda e: e["derived_patch"]["patches"].append(
                {**copy.deepcopy(e["derived_patch"]["patches"][0])}
            ),
            "patch rows differ",
        ),
    ],
)
def test_declared_patch_must_match_capture_selection_and_inputs(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    mutate(evidence)
    with pytest.raises(ledger.ProviderLedgerError, match=message):
        _run(manifest, catalog, evidence)


def test_undeclared_row_change_is_rejected(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    for row in catalog["nav_observations"]:
        if row["account_id"] == "account-a" and row["as_of_date"] == "2026-09-16":
            row["nav"]["value"] = "99.00"
    with pytest.raises(ledger.ProviderLedgerError, match="undeclared rows"):
        _run(manifest, catalog, evidence)


def test_patch_for_a_row_captured_before_the_new_york_close_is_rejected(
    tmp_path: Path,
) -> None:
    manifest, catalog, evidence = _build(tmp_path, captured="2026-09-17T19:00:00.000Z")
    with pytest.raises(ledger.ProviderLedgerError, match="rows differ from selected"):
        _run(manifest, catalog, evidence)


def test_patch_not_consumed_by_any_capture_is_rejected(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    extra = copy.deepcopy(evidence["derived_patch"]["patches"][0])
    extra["native_sha256"] = "f" * 64
    evidence["derived_patch"]["patches"].append(extra)
    with pytest.raises(ledger.ProviderLedgerError, match="inventory differs"):
        _run(manifest, catalog, evidence)


def test_replay_rejects_wrong_nav_value_wrong_close_and_wrong_account(
    tmp_path: Path,
) -> None:
    # Wrong NAV: patched value, selected catalog and record all say 110.51.
    manifest, catalog, evidence = _build(tmp_path)
    evidence["derived_patch"]["patches"][0]["patched_values"]["2026-09-17"] = "110.51"
    with pytest.raises(ledger.ProviderLedgerError, match="replay differs"):
        _run(manifest, catalog, evidence)


def test_replay_rejects_changed_official_close(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    tampered = tmp_path / "XYZ_tampered.csv"
    tampered.write_text("date,close\n2026-09-17,10.06\n")
    evidence["derived_patch"]["replay"]["closes"][0].update(_pin(tampered))
    with pytest.raises(ledger.ProviderLedgerError, match="replay differs"):
        _run(manifest, catalog, evidence)


def test_replay_rejects_missing_close(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    evidence["derived_patch"]["replay"]["closes"] = []
    with pytest.raises(ledger.ProviderLedgerError, match="lacks an official close"):
        _run(manifest, catalog, evidence)


def test_patch_account_must_be_the_matched_artifact_account(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    for row in catalog["nav_observations"]:
        if row["source_artifact_id"] == "wch/nav/aaa.csv":
            row["account_id"] = "account-b"
    with pytest.raises(ledger.ProviderLedgerError, match="account scope differs"):
        _run(manifest, catalog, evidence)


def test_replay_rejects_swapped_account_ids(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    evidence["derived_patch"]["patches"][0]["account_id"] = "account-b"
    with pytest.raises(ledger.ProviderLedgerError):
        _run(manifest, catalog, evidence)


def test_replay_rejects_tolerant_rounding_declaration(tmp_path: Path) -> None:
    manifest, catalog, evidence = _build(tmp_path)
    evidence["derived_patch"]["replay"]["rounding"] = "TOLERANT"
    with pytest.raises(ledger.ProviderLedgerError, match="replay declaration"):
        _run(manifest, catalog, evidence)
