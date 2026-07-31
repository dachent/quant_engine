from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.schwab import (
    SchwabGenerationError,
    build_generation,
    stage_generation_candidate,
)


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _source(
    path: Path,
    *,
    root: Path,
    artifact_id: str,
    role: str,
    account_id: str | None,
    schema_name: str,
) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "account_id": account_id,
        "artifact_id": artifact_id,
        "as_of_date": "2026-07-31",
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "relative_path": path.relative_to(root).as_posix(),
        "role": role,
        "root_id": "source",
        "schema_name": schema_name,
        "schema_version": "1",
    }


def _spec(tmp_path: Path) -> Path:
    source_root = tmp_path / "source"
    source_root.mkdir()
    sources: list[dict[str, object]] = []
    crosswalk: list[dict[str, str]] = []
    position_rows: list[list[object]] = [
        ["Positions for All-Accounts as of 01:41 PM ET, 07/31/2026"],
        [],
    ]
    position_header = [
        "Symbol",
        "Description",
        "Qty (Quantity)",
        "Prev Close",
        "Cost Basis",
        "Price",
        "Price Chng $ (Price Change $)",
        "Price Chng % (Price Change %)",
        "Mkt Val (Market Value)",
        "Day Chng $ (Day Change $)",
        "Day Chng % (Day Change %)",
        "Gain $ (Gain/Loss $)",
        "Gain % (Gain/Loss %)",
        "Ratings",
        "Reinvest?",
        "Reinvest Capital Gains?",
        "% of Acct (% of Account)",
        "Cost/Share",
        "Last Tr (ET)",
        "Exchange",
        "Asset Type",
        "",
    ]
    for index in range(8):
        suffix = f"{index + 1:03d}"
        external = f"SYNTHETIC-{suffix}"
        account_id = f"account-{index:016x}"
        crosswalk.append(
            {
                "external_" + "account_id": external,
                "internal_account_id": account_id,
            }
        )
        transaction = {
            "action": "Buy",
            "amount": "-10.00",
            "description": "SYNTHETIC PURCHASE",
            "effectiveDate": "07/30/2026",
            "executionPrice": "10.00",
            "feesAndCommission": "0.00",
            "shareQuantity": "1",
            "symbol": "SYNTH",
            "transactionDate": "07/30/2026",
            "transactionDetail": {"settleDate": "07/31/2026"},
        }
        wch_transaction = source_root / f"wch-transaction-{index}.json"
        wch_transaction.write_text(json.dumps([transaction]), encoding="utf-8")
        sources.append(
            _source(
                wch_transaction,
                root=source_root,
                artifact_id=f"wch/transaction-{index}.json",
                role="WCH_TRANSACTION_JSON",
                account_id=account_id,
                schema_name="synthetic-wch-transaction",
            )
        )
        manual_transaction = source_root / f"manual-transaction-{index}.json"
        manual_transaction.write_text(
            json.dumps(
                {
                    "BrokerageTransactions": [
                        {
                            "Action": "Buy",
                            "Amount": "-10.00",
                            "Date": "07/30/2026",
                            "Description": "SYNTHETIC PURCHASE",
                            "Fees & Comm": "0.00",
                            "Price": "10.00",
                            "Quantity": "1",
                            "Symbol": "SYNTH",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        sources.append(
            _source(
                manual_transaction,
                root=source_root,
                artifact_id=f"manual/transaction-{index}.json",
                role="MANUAL_TRANSACTION_JSON",
                account_id=account_id,
                schema_name="synthetic-manual-transaction",
            )
        )
        wch_nav = source_root / f"wch-nav-{index}.csv"
        _write_csv(
            wch_nav,
            ["account_id", "account_name", "date", "account_value"],
            [
                [external, "SYNTHETIC", "07/29/2026", 100 + index],
                [external, "SYNTHETIC", "07/30/2026", 101 + index],
            ],
        )
        sources.append(
            _source(
                wch_nav,
                root=source_root,
                artifact_id=f"wch/nav-{index}.csv",
                role="WCH_NAV_CSV",
                account_id=account_id,
                schema_name="synthetic-wch-nav",
            )
        )
        manual_nav = source_root / f"manual-nav-{index}.csv"
        _write_csv(
            manual_nav,
            ["Date", "Amount"],
            [
                ["07/29/2026", 100 + index],
                ["07/30/2026", 101 + index],
                ["07/31/2026", 102 + index],
            ],
        )
        sources.append(
            _source(
                manual_nav,
                root=source_root,
                artifact_id=f"manual/nav-{index}.csv",
                role="MANUAL_NAV_CSV",
                account_id=account_id,
                schema_name="synthetic-manual-nav",
            )
        )
        position_rows.extend(
            [
                [f"SYNTHETIC ...{suffix}"],
                position_header,
                [
                    "SYNTH",
                    "SYNTHETIC SECURITY",
                    "1",
                    "10",
                    "10",
                    "11",
                    "1",
                    "10%",
                    "11",
                    "1",
                    "10%",
                    "1",
                    "10%",
                    "",
                    "No",
                    "No",
                    "100%",
                    "10",
                    "01:40 PM",
                    "SYNTHETIC",
                    "Equity",
                    "",
                ],
                [],
            ]
        )
    position_path = source_root / "positions.csv"
    _write_csv(position_path, position_rows[0], position_rows[1:])
    sources.append(
        _source(
            position_path,
            root=source_root,
            artifact_id="manual/current-positions.csv",
            role="MANUAL_POSITION_CSV",
            account_id=None,
            schema_name="synthetic-position-export",
        )
    )
    workbook = source_root / "reference.xlsx"
    workbook.write_bytes(b"SYNTHETIC WORKBOOK REFERENCE")
    sources.append(
        _source(
            workbook,
            root=source_root,
            artifact_id="manual/workbook-reference.xlsx",
            role="WORKBOOK_REFERENCE",
            account_id=None,
            schema_name="synthetic-workbook-reference",
        )
    )
    spec = {
        "as_of_date": "2026-07-31",
        "baseline": {
            "account_count": 8,
            "nav_count": 16,
            "position_count": 8,
            "transaction_count": 8,
        },
        "crosswalk": crosswalk,
        "roots": {"source": str(source_root.resolve())},
        "schema_version": 1,
        "sources": sources,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_build_is_deterministic_partial_and_reconciled(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    first, first_manifest, first_bytes = build_generation(spec)
    second, second_manifest, second_bytes = build_generation(spec)
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first_manifest.coverage.status.value == "PARTIAL"
    assert len(first["accounts"]) == 8
    assert len(first["transactions"]) == 8
    assert len(first["nav_observations"]) == 16
    assert len(first["positions"]) == 8
    assert first["position_quantity_unavailable_count"] == 0
    assert {item["status"] for item in first["reconciliation"]} == {
        "PASS_EXACT",
        "PASS_WITH_LATER_MANUAL_DATE",
    }


def test_stage_is_immutable_and_does_not_publish(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    root = tmp_path / "dataSchwab"
    first = stage_generation_candidate(spec, root)
    second = stage_generation_candidate(spec, root)
    assert first == second
    assert first["published"] is False
    assert not (root / "CURRENT.json").exists()
    catalog = (
        root / "generations" / str(first["generation_id"]) / "schwab-generation.json"
    )
    assert catalog.is_file()


def test_source_drift_and_transaction_variance_fail_closed(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    source = document["sources"][0]
    source_path = Path(document["roots"]["source"]) / source["relative_path"]
    source_path.write_text("[]", encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="source hash differs"):
        build_generation(spec)

    variance = tmp_path / "variance"
    variance.mkdir()
    spec = _spec(variance)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual = next(
        item
        for item in document["sources"]
        if item["role"] == "MANUAL_TRANSACTION_JSON"
    )
    source_path = Path(document["roots"]["source"]) / manual["relative_path"]
    value = json.loads(source_path.read_text(encoding="utf-8"))
    value["BrokerageTransactions"][0]["Amount"] = "-11.00"
    source_path.write_text(json.dumps(value), encoding="utf-8")
    payload = source_path.read_bytes()
    manual["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="transaction histories differ"):
        build_generation(spec)


def test_account_role_duplication_fails_closed(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    nav = [item for item in document["sources"] if item["role"] == "WCH_NAV_CSV"]
    nav[-1]["account_id"] = nav[0]["account_id"]
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="8 unique accounts"):
        build_generation(spec)
