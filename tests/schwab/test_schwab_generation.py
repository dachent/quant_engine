from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.schwab import (
    SchwabGenerationError,
    build_generation,
    publish_generation_candidate,
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
        ext = f"SYNTHETIC-{suffix}"
        account_id = f"account-{index:016x}"
        crosswalk.append(
            {
                "external_" + "account_id": ext,
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
                [ext, "SYNTHETIC", "07/29/2026", 100 + index],
                [ext, "SYNTHETIC", "07/30/2026", 101 + index],
            ],
        )
        sources.append(
            _source(
                wch_nav,
                root=source_root,
                artifact_id=f"wch/nav-{index}.csv",
                role="WCH_NAV_CSV",
                account_id=account_id,
                schema_name="schwab-wch-historical-balance",
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


def _spec_with_current(tmp_path: Path) -> Path:
    path = _spec(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    observed_at = "2026-07-31T20:00:00+00:00"
    accounts: list[dict[str, object]] = []
    positions: list[dict[str, object]] = []
    for item in spec["crosswalk"]:
        account_id = item["internal_account_id"]
        security_id = f"security-schwab-{hashlib.sha256(b'SYNTH').hexdigest()[:24]}"
        identity = {
            "account_id": account_id,
            "as_of_date": "2026-07-31",
            "security_id": security_id,
        }
        record_id = (
            "position-"
            + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
        )
        accounts.append(
            {
                "account_id": account_id,
                "cash_available": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "12",
                },
                "liquidation_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "100",
                },
                "observed_at": observed_at,
            }
        )
        positions.append(
            {
                "account_id": account_id,
                "as_of_date": "2026-07-31",
                "average_price": "10",
                "evidence_class": "DEVELOPER_API_CURRENT",
                "mapping_status": "UNMAPPED",
                "market_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "11",
                },
                "observed_at": observed_at,
                "quantity": "1",
                "record_id": record_id,
                "security_id": security_id,
                "strategy_id": None,
                "symbol": "SYNTH",
            }
        )
    snapshot = {
        "accounts": accounts,
        "observed_at": observed_at,
        "positions": positions,
        "provider": "SCHWAB",
        "schema_version": 1,
        "sweep_vehicle_count": 8,
    }
    source_root = Path(spec["roots"]["source"])
    snapshot_path = source_root / "current-snapshot.json"
    snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    payload = snapshot_path.read_bytes()
    spec.update(
        {
            "current_snapshot": {
                "artifact_id": "developer/current-snapshot.json",
                "as_of_date": "2026-07-31",
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_size_bytes": len(payload),
                "relative_path": snapshot_path.relative_to(source_root).as_posix(),
                "root_id": "source",
                "schema_name": "schwab-developer-current-snapshot",
                "schema_version": "1",
            },
            "schema_version": 2,
        }
    )
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
    assert first["catalog_schema_version"] == 2
    assert {item["value_basis"] for item in first["nav_observations"]} == {
        "ACCOUNT_VALUE"
    }
    assert {item["source_artifact_id"] for item in first["nav_observations"]} == {
        f"wch/nav-{index}.csv" for index in range(8)
    }
    assert len(first["positions"]) == 8
    assert first["position_quantity_unavailable_count"] == 0
    assert {item["status"] for item in first["reconciliation"]} == {
        "PASS_EXACT",
        "PASS_WITH_LATER_MANUAL_DATE",
    }


def test_wch_nav_schema_is_bound_exclusively_to_its_source_role(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    nav = next(item for item in document["sources"] if item["role"] == "WCH_NAV_CSV")
    nav["schema_name"] = "synthetic-wch-nav"
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError, match="source schema is invalid for role"
    ):
        build_generation(spec)

    reserved_root = tmp_path / "reserved-schema"
    reserved_root.mkdir()
    spec = _spec(reserved_root)
    document = json.loads(spec.read_text(encoding="utf-8"))
    transaction = next(
        item for item in document["sources"] if item["role"] == "WCH_TRANSACTION_JSON"
    )
    transaction["schema_name"] = "schwab-wch-historical-balance"
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError, match="source schema is invalid for role"
    ):
        build_generation(spec)


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


def test_current_snapshot_supersedes_manual_current_reference(tmp_path: Path) -> None:
    spec = _spec_with_current(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status.value == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert len(catalog["current_accounts"]) == 8
    assert len(catalog["positions"]) == 8
    assert catalog["position_quantity_unavailable_count"] == 0
    assert catalog["sweep_vehicle_count"] == 8
    assert all(
        item["evidence_class"] == "DEVELOPER_API_CURRENT"
        for item in catalog["positions"]
    )
    assert not any(
        "current account and position snapshot is not present" in item
        for item in catalog["coverage"]["known_gaps"]
    )
    current_reference = next(
        item
        for item in catalog["reconciliation"]
        if item.get("type") == "CURRENT_POSITION_REFERENCE"
    )
    assert current_reference["status"] == "PASS_EXACT"
    assert "current_balances" in manifest.coverage.fact_types


def test_current_snapshot_cannot_claim_wch_nav_schema(tmp_path: Path) -> None:
    spec = _spec_with_current(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["current_snapshot"]["schema_name"] = "schwab-wch-historical-balance"
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError, match="source schema is invalid for role"
    ):
        build_generation(spec)


def test_complete_current_candidate_publishes_only_exact_rebuild(
    tmp_path: Path,
) -> None:
    spec = _spec_with_current(tmp_path)
    root = tmp_path / "dataSchwab"
    staged = stage_generation_candidate(spec, root)
    accepted = publish_generation_candidate(spec, root, str(staged["generation_id"]))
    assert accepted["published"] is True
    pointer = json.loads((root / "CURRENT.json").read_text(encoding="utf-8"))
    assert pointer["generation_id"] == staged["generation_id"]
    with pytest.raises(SchwabGenerationError, match="differs from accepted"):
        publish_generation_candidate(spec, root, "0" * 64)


def test_current_snapshot_hash_and_scope_fail_closed(tmp_path: Path) -> None:
    spec = _spec_with_current(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    snapshot_path = (
        Path(document["roots"]["source"])
        / document["current_snapshot"]["relative_path"]
    )
    snapshot_path.write_text("{}", encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="current snapshot hash differs"):
        build_generation(spec)

    scoped = tmp_path / "scoped"
    scoped.mkdir()
    spec = _spec_with_current(scoped)
    document = json.loads(spec.read_text(encoding="utf-8"))
    snapshot_path = (
        Path(document["roots"]["source"])
        / document["current_snapshot"]["relative_path"]
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["accounts"].pop()
    snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    payload = snapshot_path.read_bytes()
    document["current_snapshot"]["expected_sha256"] = hashlib.sha256(
        payload
    ).hexdigest()
    document["current_snapshot"]["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="scope differs from crosswalk"):
        build_generation(spec)


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


# ---------------------------------------------------------------------------
# All-accounts aggregate (MANUAL_TRANSACTION_JSON_ALL / MANUAL_NAV_CSV_ALL)
# ---------------------------------------------------------------------------


def _spec_aggregate(tmp_path: Path) -> Path:
    """Build an all-accounts aggregate spec.

    Two accounts have transactions within a declared window; the other six
    have empty WCH transaction arrays.  NAV is populated for all eight
    accounts and the all-accounts manual NAV CSV sums their values.
    """
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

    # Transactions in the manual window: accounts 0 and 1 each have one.
    manual_transactions: list[dict[str, object]] = []
    nav_date_rows: list[
        tuple[str, int, int]
    ] = []  # date, wch_value_per_acct, manual_sum

    for index in range(8):
        suffix = f"{index + 1:03d}"
        ext = f"SYNTHETIC-{suffix}"
        account_id = f"account-{index:016x}"
        crosswalk.append(
            {
                "external_account_id": ext,
                "internal_account_id": account_id,
            }
        )

        # WCH transactions: accounts 0 and 1 get one; others get none.
        wch_txns: list[dict[str, object]] = []
        if index == 0:
            txn: dict[str, object] = {
                "action": "Buy",
                "amount": "-10.00",
                "description": "SYNTHETIC PURCHASE A",
                "effectiveDate": "07/30/2026",
                "executionPrice": "10.00",
                "feesAndCommission": "0.00",
                "shareQuantity": "1",
                "symbol": "SYNTH",
                "transactionDate": "07/30/2026",
                "transactionDetail": {"settleDate": "07/31/2026"},
            }
            wch_txns = [txn]
            manual_transactions.append(
                {
                    "Action": "Buy",
                    "Amount": "-10.00",
                    "Date": "07/30/2026",
                    "Description": "SYNTHETIC PURCHASE A",
                    "Fees & Comm": "0.00",
                    "Price": "10.00",
                    "Quantity": "1",
                    "Symbol": "SYNTH",
                }
            )
        elif index == 1:
            txn = {
                "action": "Sell",
                "amount": "15.50",
                "description": "SYNTHETIC SALE B",
                "effectiveDate": "07/29/2026",
                "executionPrice": "15.50",
                "feesAndCommission": "0.00",
                "shareQuantity": "1",
                "symbol": "SYNTHB",
                "transactionDate": "07/29/2026",
                "transactionDetail": {"settleDate": "07/30/2026"},
            }
            wch_txns = [txn]
            manual_transactions.append(
                {
                    "Action": "Sell",
                    "Amount": "15.50",
                    "Date": "07/29/2026",
                    "Description": "SYNTHETIC SALE B",
                    "Fees & Comm": "0.00",
                    "Price": "15.50",
                    "Quantity": "1",
                    "Symbol": "SYNTHB",
                }
            )

        wch_transaction = source_root / f"wch-transaction-{index}.json"
        wch_transaction.write_text(json.dumps(wch_txns), encoding="utf-8")
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

        # WCH NAV
        wch_nav = source_root / f"wch-nav-{index}.csv"
        date_a, date_b = "07/29/2026", "07/30/2026"
        val_a = 100 + index
        val_b = 101 + index
        _write_csv(
            wch_nav,
            ["account_id", "account_name", "date", "account_value"],
            [
                [ext, "SYNTHETIC", date_a, val_a],
                [ext, "SYNTHETIC", date_b, val_b],
            ],
        )
        sources.append(
            _source(
                wch_nav,
                root=source_root,
                artifact_id=f"wch/nav-{index}.csv",
                role="WCH_NAV_CSV",
                account_id=account_id,
                schema_name="schwab-wch-historical-balance",
            )
        )
        nav_date_rows.append((date_a, val_a, 0))
        nav_date_rows.append((date_b, val_b, 0))

        # Position section
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

    # All-accounts manual transaction JSON
    manual_all_txn = source_root / "manual-transaction-all.json"
    manual_all_txn.write_text(
        json.dumps(
            {
                "FromDate": "07/01/2026",
                "ToDate": "08/14/2026",
                "BrokerageTransactions": manual_transactions,
            }
        ),
        encoding="utf-8",
    )
    source = _source(
        manual_all_txn,
        root=source_root,
        artifact_id="manual/transaction-all.json",
        role="MANUAL_TRANSACTION_JSON_ALL",
        account_id=None,
        schema_name="synthetic-manual-transaction-all",
    )
    source["expected_record_count"] = 2
    sources.append(source)

    # All-accounts manual NAV CSV (multi-section single-date format)
    manual_all_nav = source_root / "manual-nav-all.csv"
    # Sum per-account values for 07/30/2026 (the date WCH has data for)
    total_value = sum(101 + i for i in range(8))  # val_b for each account
    with manual_all_nav.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Balances for All-Accounts as of 07/30/2026 03:50 PM ET"])
        writer.writerow([])
        writer.writerow(["Total Accounts Value", f"${total_value}"])
        writer.writerow(["Total Day Change", "$0.00"])
        writer.writerow(["Total Day Change %", "0%"])
        writer.writerow(["Cash & Cash Investments Total", "$0.00"])
        writer.writerow(["Total Market Value", f"${total_value}"])
        writer.writerow([])
        for index in range(8):
            suffix_label = f"9{index + 1:03d}"  # 4-digit: "9001", regex last-3 → "001"
            writer.writerow([f"SYNTHETIC XXXX-{suffix_label}"])
            writer.writerow(["Account Value", f"${101 + index}"])
            writer.writerow(["Day Change", "$0.00"])
            writer.writerow(["Day Change %", "0%"])
            writer.writerow(["Cash & Cash Investments", "$0.00"])
            writer.writerow(["Market Value", f"${101 + index}"])
            writer.writerow([])
    sources.append(
        _source(
            manual_all_nav,
            root=source_root,
            artifact_id="manual/nav-all.csv",
            role="MANUAL_NAV_CSV_ALL",
            account_id=None,
            schema_name="synthetic-manual-nav-all",
        )
    )

    # Position CSV
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

    # Workbook reference
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
            "transaction_count": 2,
        },
        "crosswalk": crosswalk,
        "roots": {"source": str(source_root.resolve())},
        "schema_version": 1,
        "sources": sources,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


# -- Aggregate transaction tests -------------------------------------------------


def test_aggregate_transactions_pass(tmp_path: Path) -> None:
    """Two accounts each have one matching transaction; build succeeds."""
    spec = _spec_aggregate(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status.value == "PARTIAL"
    assert len(catalog["transactions"]) == 2
    assert len(catalog["accounts"]) == 8
    statuses = {item["status"] for item in catalog["reconciliation"]}
    assert statuses == {"PASS_EXACT"}


def test_aggregate_transactions_extra_manual_fails(tmp_path: Path) -> None:
    """An extra transaction in the all-accounts manual that is not in WCH fails."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = json.loads(manual_path.read_text(encoding="utf-8"))
    value["BrokerageTransactions"].append(
        {
            "Action": "Buy",
            "Amount": "-99.00",
            "Date": "07/28/2026",
            "Description": "EXTRA",
            "Fees & Comm": "0.00",
            "Price": "99.00",
            "Quantity": "1",
            "Symbol": "EXTRA",
        }
    )
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="manual-only records"):
        build_generation(spec)


def test_aggregate_transactions_wch_only_accepted_with_gap(
    tmp_path: Path,
) -> None:
    """WCH-only records within the manual window are accepted with a declared
    boundary gap (subset-exact verification)."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    # Account 2 has an empty WCH array by default — add a transaction to it.
    wch_source = next(
        s
        for s in document["sources"]
        if s["role"] == "WCH_TRANSACTION_JSON"
        and s["account_id"] == f"account-{2:016x}"
    )
    wch_path = Path(document["roots"]["source"]) / wch_source["relative_path"]
    wch_path.write_text(
        json.dumps(
            [
                {
                    "action": "Buy",
                    "amount": "-5.00",
                    "description": "WCH ONLY EXTRA",
                    "effectiveDate": "07/25/2026",
                    "executionPrice": "5.00",
                    "feesAndCommission": "0.00",
                    "shareQuantity": "1",
                    "symbol": "WCHX",
                    "transactionDate": "07/25/2026",
                    "transactionDetail": {"settleDate": "07/26/2026"},
                }
            ]
        ),
        encoding="utf-8",
    )
    payload = wch_path.read_bytes()
    wch_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    wch_source["expected_size_bytes"] = len(payload)
    document["baseline"]["transaction_count"] = (
        document["baseline"]["transaction_count"] + 1
    )
    spec.write_text(json.dumps(document), encoding="utf-8")
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status.value == "PARTIAL"
    # Catalog coverage documents the partial-export gap
    gaps = catalog["coverage"]["known_gaps"]
    assert any("manual_export_partial:" in g for g in gaps)
    # Reconciliation status is PASS_EXACT
    txn_recon = [
        r for r in catalog["reconciliation"] if r["fact_type"] == "TRANSACTION"
    ][0]
    assert txn_recon["status"] == "PASS_EXACT"


def test_aggregate_transactions_changed_amount_fails(tmp_path: Path) -> None:
    """Manual transaction amount differs from WCH — build fails."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = json.loads(manual_path.read_text(encoding="utf-8"))
    # Change the amount of the first transaction.
    value["BrokerageTransactions"][0]["Amount"] = "-99.00"
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="manual-only records"):
        build_generation(spec)


def test_aggregate_transactions_window_edge(tmp_path: Path) -> None:
    """Transactions exactly at FromDate/ToDate are compared; outside-window
    WCH transactions are ignored."""
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
        ext = f"SYNTHETIC-{suffix}"
        account_id = f"account-{index:016x}"
        crosswalk.append(
            {"external_account_id": ext, "internal_account_id": account_id}
        )

        wch_txns: list[dict[str, object]] = []
        if index == 0:
            # outside-window (before)
            wch_txns.append(
                {
                    "action": "Buy",
                    "amount": "-1.00",
                    "description": "OUTSIDE BEFORE",
                    "effectiveDate": "01/01/2026",
                    "executionPrice": "1.00",
                    "feesAndCommission": "0.00",
                    "shareQuantity": "1",
                    "symbol": "OUT",
                    "transactionDate": "01/01/2026",
                    "transactionDetail": {"settleDate": "01/02/2026"},
                }
            )
            # exactly at FromDate
            wch_txns.append(
                {
                    "action": "Buy",
                    "amount": "-2.00",
                    "description": "EDGE FROM",
                    "effectiveDate": "07/01/2026",
                    "executionPrice": "2.00",
                    "feesAndCommission": "0.00",
                    "shareQuantity": "1",
                    "symbol": "EDG",
                    "transactionDate": "07/01/2026",
                    "transactionDetail": {"settleDate": "07/02/2026"},
                }
            )
        elif index == 1:
            # exactly at ToDate
            wch_txns.append(
                {
                    "action": "Sell",
                    "amount": "3.00",
                    "description": "EDGE TO",
                    "effectiveDate": "08/14/2026",
                    "executionPrice": "3.00",
                    "feesAndCommission": "0.00",
                    "shareQuantity": "1",
                    "symbol": "EDG",
                    "transactionDate": "08/14/2026",
                    "transactionDetail": {"settleDate": "08/15/2026"},
                }
            )
            # outside-window (after)
            wch_txns.append(
                {
                    "action": "Sell",
                    "amount": "4.00",
                    "description": "OUTSIDE AFTER",
                    "effectiveDate": "09/01/2026",
                    "executionPrice": "4.00",
                    "feesAndCommission": "0.00",
                    "shareQuantity": "1",
                    "symbol": "OUT",
                    "transactionDate": "09/01/2026",
                    "transactionDetail": {"settleDate": "09/02/2026"},
                }
            )

        wch_txn_path = source_root / f"wch-transaction-{index}.json"
        wch_txn_path.write_text(json.dumps(wch_txns), encoding="utf-8")
        sources.append(
            _source(
                wch_txn_path,
                root=source_root,
                artifact_id=f"wch/transaction-{index}.json",
                role="WCH_TRANSACTION_JSON",
                account_id=account_id,
                schema_name="synthetic-wch-transaction",
            )
        )

        # NAV
        wch_nav = source_root / f"wch-nav-{index}.csv"
        _write_csv(
            wch_nav,
            ["account_id", "account_name", "date", "account_value"],
            [
                [ext, "SYNTHETIC", "07/29/2026", 100 + index],
                [ext, "SYNTHETIC", "07/30/2026", 101 + index],
            ],
        )
        sources.append(
            _source(
                wch_nav,
                root=source_root,
                artifact_id=f"wch/nav-{index}.csv",
                role="WCH_NAV_CSV",
                account_id=account_id,
                schema_name="schwab-wch-historical-balance",
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

    # Manual all-accounts transactions: only the 2 in-window ones.
    manual_all = source_root / "manual-transaction-all.json"
    manual_all.write_text(
        json.dumps(
            {
                "FromDate": "07/01/2026",
                "ToDate": "08/14/2026",
                "BrokerageTransactions": [
                    {
                        "Action": "Buy",
                        "Amount": "-2.00",
                        "Date": "07/01/2026",
                        "Description": "EDGE FROM",
                        "Fees & Comm": "0.00",
                        "Price": "2.00",
                        "Quantity": "1",
                        "Symbol": "EDG",
                    },
                    {
                        "Action": "Sell",
                        "Amount": "3.00",
                        "Date": "08/14/2026",
                        "Description": "EDGE TO",
                        "Fees & Comm": "0.00",
                        "Price": "3.00",
                        "Quantity": "1",
                        "Symbol": "EDG",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    source = _source(
        manual_all,
        root=source_root,
        artifact_id="manual/transaction-all.json",
        role="MANUAL_TRANSACTION_JSON_ALL",
        account_id=None,
        schema_name="synthetic-manual-transaction-all",
    )
    source["expected_record_count"] = 2
    sources.append(source)

    # All-accounts NAV with per-account sections
    manual_nav = source_root / "manual-nav-all.csv"
    total_value = sum(101 + i for i in range(8))  # 07/30/2026 WCH values
    with manual_nav.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Balances for All-Accounts as of 07/30/2026 03:50 PM ET"])
        writer.writerow([])
        writer.writerow(["Total Accounts Value", f"${total_value}"])
        writer.writerow(["Total Day Change", "$0.00"])
        writer.writerow(["Total Day Change %", "0%"])
        writer.writerow(["Cash & Cash Investments Total", "$0.00"])
        writer.writerow(["Total Market Value", f"${total_value}"])
        writer.writerow([])
        for i in range(8):
            suffix_label = f"9{i + 1:03d}"
            writer.writerow([f"SYNTHETIC XXXX-{suffix_label}"])
            writer.writerow(["Account Value", f"${101 + i}"])
            writer.writerow(["Day Change", "$0.00"])
            writer.writerow(["Day Change %", "0%"])
            writer.writerow(["Cash & Cash Investments", "$0.00"])
            writer.writerow(["Market Value", f"${101 + i}"])
            writer.writerow([])
    sources.append(
        _source(
            manual_nav,
            root=source_root,
            artifact_id="manual/nav-all.csv",
            role="MANUAL_NAV_CSV_ALL",
            account_id=None,
            schema_name="synthetic-manual-nav-all",
        )
    )

    # Position CSV
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

    # Workbook
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
            "transaction_count": 4,  # all 4 WCH transactions (in + out of window)
        },
        "crosswalk": crosswalk,
        "roots": {"source": str(source_root.resolve())},
        "schema_version": 1,
        "sources": sources,
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    catalog, manifest, _ = build_generation(spec_path)
    assert len(catalog["transactions"]) == 4
    assert manifest.coverage.status.value == "PARTIAL"


# -- Aggregate NAV test -----------------------------------------------------------


def test_aggregate_nav_pass(tmp_path: Path) -> None:
    """All-accounts manual NAV CSV reconciled per-account against WCH NAV passes."""
    spec = _spec_aggregate(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    # 8 accounts × 2 NAV entries each
    assert len(catalog["nav_observations"]) == 16
    assert {item["value_basis"] for item in catalog["nav_observations"]} == {
        "ACCOUNT_VALUE"
    }
    assert all(item["source_artifact_id"] for item in catalog["nav_observations"])
    nav_recon = [
        item for item in catalog["reconciliation"] if item["fact_type"] == "NAV"
    ]
    # Per-account reconciliation: one finding per account
    assert len(nav_recon) == 8
    for entry in nav_recon:
        assert entry["account_id"] is not None
        assert entry["status"] == "PASS_EXACT"


# -- Mutual exclusion test -------------------------------------------------------


def test_aggregate_spec_rejects_both_roles(tmp_path: Path) -> None:
    """A spec that includes both per-account and aggregate manual roles fails."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    source_root = Path(document["roots"]["source"])

    # Add a per-account MANUAL_TRANSACTION_JSON source for account 0
    manual_per_acct = source_root / "manual-transaction-0.json"
    manual_per_acct.write_text(
        json.dumps({"BrokerageTransactions": []}), encoding="utf-8"
    )
    account_0 = f"account-{0:016x}"
    document["sources"].append(
        _source(
            manual_per_acct,
            root=source_root,
            artifact_id="manual/transaction-per-acct.json",
            role="MANUAL_TRANSACTION_JSON",
            account_id=account_0,
            schema_name="synthetic-manual-transaction",
        )
    )
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError,
        match="cannot use both per-account and aggregate manual transaction roles",
    ):
        build_generation(spec)


# -- Backward-compatibility test --------------------------------------------------


def test_per_account_07_31_still_works(tmp_path: Path) -> None:
    """The original per-account _spec helper still builds successfully."""
    spec = _spec(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status.value == "PARTIAL"
    assert len(catalog["accounts"]) == 8
    assert len(catalog["transactions"]) == 8


# -- Reversed window test --------------------------------------------------------


def test_window_reversed_fails(tmp_path: Path) -> None:
    """A transaction export with FromDate > ToDate must fail."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = json.loads(manual_path.read_text(encoding="utf-8"))
    value["FromDate"] = "08/14/2026"
    value["ToDate"] = "07/01/2026"
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="window is reversed"):
        build_generation(spec)


# -- NAV cross-account pair test --------------------------------------------------


def test_nav_cross_account_pair_fails(tmp_path: Path) -> None:
    """A +$5 shift on account 0 and -$5 on account 1 vs WCH must fail."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_NAV_CSV_ALL"
    )
    nav_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    # Rewrite NAV CSV with +$5 on account 0 and -$5 on account 1
    total_value = sum(101 + i for i in range(8))
    with nav_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Balances for All-Accounts as of 07/30/2026 03:50 PM ET"])
        writer.writerow([])
        writer.writerow(["Total Accounts Value", f"${total_value}"])
        writer.writerow(["Total Day Change", "$0.00"])
        writer.writerow(["Total Day Change %", "0%"])
        writer.writerow(["Cash & Cash Investments Total", "$0.00"])
        writer.writerow(["Total Market Value", f"${total_value}"])
        writer.writerow([])
        for i in range(8):
            suffix_label = f"9{i + 1:03d}"
            writer.writerow([f"SYNTHETIC XXXX-{suffix_label}"])
            if i == 0:
                writer.writerow(["Account Value", f"${101 + i + 5}"])
            elif i == 1:
                writer.writerow(["Account Value", f"${101 + i - 5}"])
            else:
                writer.writerow(["Account Value", f"${101 + i}"])
            writer.writerow(["Day Change", "$0.00"])
            writer.writerow(["Day Change %", "0%"])
            writer.writerow(["Cash & Cash Investments", "$0.00"])
            writer.writerow(["Market Value", f"${101 + i}"])
            writer.writerow([])
    payload = nav_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="NAV value differs for account"):
        build_generation(spec)


# -- NAV missing section test -----------------------------------------------------


def test_nav_missing_section_fails(tmp_path: Path) -> None:
    """An all-accounts NAV CSV missing one account section must fail."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_NAV_CSV_ALL"
    )
    nav_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    # Rewrite NAV CSV with only 7 accounts (omit account 0)
    total_value = sum(101 + i for i in range(8))
    with nav_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Balances for All-Accounts as of 07/30/2026 03:50 PM ET"])
        writer.writerow([])
        writer.writerow(["Total Accounts Value", f"${total_value}"])
        writer.writerow(["Total Day Change", "$0.00"])
        writer.writerow(["Total Day Change %", "0%"])
        writer.writerow(["Cash & Cash Investments Total", "$0.00"])
        writer.writerow(["Total Market Value", f"${total_value}"])
        writer.writerow([])
        for i in range(1, 8):  # skip account 0
            suffix_label = f"9{i + 1:03d}"
            writer.writerow([f"SYNTHETIC XXXX-{suffix_label}"])
            writer.writerow(["Account Value", f"${101 + i}"])
            writer.writerow(["Day Change", "$0.00"])
            writer.writerow(["Day Change %", "0%"])
            writer.writerow(["Cash & Cash Investments", "$0.00"])
            writer.writerow(["Market Value", f"${101 + i}"])
            writer.writerow([])
    payload = nav_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError, match="account coverage differs from crosswalk"
    ):
        build_generation(spec)


# -- Per-account catalog digest golden test ---------------------------------------


def test_per_account_07_31_catalog_digest(tmp_path: Path) -> None:
    """The per-account path produces a deterministic catalog digest."""
    spec = _spec(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    catalog_bytes = canonical_json_bytes(catalog)
    actual_digest = hashlib.sha256(catalog_bytes).hexdigest()
    assert manifest.coverage.status.value == "PARTIAL"
    assert len(catalog["transactions"]) == 8
    # Golden digest — must match exactly as long as the per-account code path
    # is unchanged from the reference build.
    GOLDEN = "fd7a6e9cd67ac138c4d3fe4175fa24c08b2e87865ef75b389072c56bf31fdde4"
    assert actual_digest == GOLDEN, (
        f"per-account catalog digest changed: {actual_digest} != {GOLDEN}"
    )


# -- Export record count tests ---------------------------------------------------


def test_export_record_count_drop_fails(tmp_path: Path) -> None:
    """When expected_record_count > actual rows, build fails."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_source["expected_record_count"] = 3
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="fewer than the declared expected"):
        build_generation(spec)


def test_export_record_count_zero_rows_fails(tmp_path: Path) -> None:
    """Zero rows with expected_record_count=1 fails."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_source["expected_record_count"] = 1
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = {
        "FromDate": "07/01/2026",
        "ToDate": "08/14/2026",
        "BrokerageTransactions": [],
    }
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="fewer than the declared expected"):
        build_generation(spec)


def test_export_record_count_extra_row_passes(tmp_path: Path) -> None:
    """Extra row above declared expected passes; transaction_count is total WCH rows."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    # Phase 1: expected_record_count matches actual rows
    manual_source["expected_record_count"] = 2
    spec.write_text(json.dumps(document), encoding="utf-8")
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status.value == "PARTIAL"
    assert len(catalog["transactions"]) == 2

    # Phase 2: add a third matching row, keep expected at 2
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = json.loads(manual_path.read_text(encoding="utf-8"))
    value["BrokerageTransactions"].append(
        {
            "Action": "Buy",
            "Amount": "-15.00",
            "Date": "07/15/2026",
            "Description": "SYNTHETIC PURCHASE C",
            "Fees & Comm": "0.00",
            "Price": "15.00",
            "Quantity": "1",
            "Symbol": "SYNTHC",
        }
    )
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)

    # Add corresponding WCH transaction to account 2
    account_2_id = f"account-{2:016x}"
    wch_source = next(
        s
        for s in document["sources"]
        if s["role"] == "WCH_TRANSACTION_JSON" and s["account_id"] == account_2_id
    )
    wch_path = Path(document["roots"]["source"]) / wch_source["relative_path"]
    wch_path.write_text(
        json.dumps(
            [
                {
                    "action": "Buy",
                    "amount": "-15.00",
                    "description": "SYNTHETIC PURCHASE C",
                    "effectiveDate": "07/15/2026",
                    "executionPrice": "15.00",
                    "feesAndCommission": "0.00",
                    "shareQuantity": "1",
                    "symbol": "SYNTHC",
                    "transactionDate": "07/15/2026",
                    "transactionDetail": {"settleDate": "07/16/2026"},
                }
            ]
        ),
        encoding="utf-8",
    )
    wch_payload = wch_path.read_bytes()
    wch_source["expected_sha256"] = hashlib.sha256(wch_payload).hexdigest()
    wch_source["expected_size_bytes"] = len(wch_payload)

    document["baseline"]["transaction_count"] = 3
    spec.write_text(json.dumps(document), encoding="utf-8")
    catalog, manifest, _ = build_generation(spec)
    assert len(catalog["transactions"]) == 3
    assert manifest.coverage.status.value == "PARTIAL"


# -- Date validation tests -------------------------------------------------------


def test_window_date_rejects_trailing_junk(tmp_path: Path) -> None:
    """A FromDate with trailing junk characters raises an error."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = json.loads(manual_path.read_text(encoding="utf-8"))
    value["FromDate"] = "07/01/2026junk"
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="trailing content"):
        build_generation(spec)


def test_window_date_rejects_restatement_suffix(tmp_path: Path) -> None:
    """The WCH restatement suffix is valid on transactionDate only, never on
    declared window boundaries (QC3 finding QC3-NEW-1)."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_path = Path(document["roots"]["source"]) / manual_source["relative_path"]
    value = json.loads(manual_path.read_text(encoding="utf-8"))
    value["FromDate"] = "07/01/2026 as of 08/14/2026"
    manual_path.write_text(json.dumps(value), encoding="utf-8")
    payload = manual_path.read_bytes()
    manual_source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    manual_source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="trailing content"):
        build_generation(spec)


# -- Mandatory role validation tests -----------------------------------------------


def test_missing_txn_role_fails(tmp_path: Path) -> None:
    """A spec with no MANUAL_TRANSACTION_JSON source must fail."""
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["sources"] = [
        s
        for s in document["sources"]
        if s["role"] not in ("MANUAL_TRANSACTION_JSON", "MANUAL_TRANSACTION_JSON_ALL")
    ]
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="manual transaction role"):
        build_generation(spec)


def test_missing_workbook_role_fails(tmp_path: Path) -> None:
    """A spec with no WORKBOOK_REFERENCE source must fail."""
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["sources"] = [
        s for s in document["sources"] if s["role"] != "WORKBOOK_REFERENCE"
    ]
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError, match="mandatory role WORKBOOK_REFERENCE"
    ):
        build_generation(spec)


# -- expected_record_count validation tests ---------------------------------------


def test_expected_record_count_missing_fails(tmp_path: Path) -> None:
    """MANUAL_TRANSACTION_JSON_ALL without expected_record_count must fail."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    del manual_source["expected_record_count"]
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="requires expected_record_count"):
        build_generation(spec)


def test_expected_record_count_zero_fails(tmp_path: Path) -> None:
    """expected_record_count of 0 must fail as not strictly positive."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_source["expected_record_count"] = 0
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="strictly positive"):
        build_generation(spec)


def test_expected_record_count_negative_fails(tmp_path: Path) -> None:
    """expected_record_count of -1 must fail as not strictly positive."""
    spec = _spec_aggregate(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    manual_source = next(
        s for s in document["sources"] if s["role"] == "MANUAL_TRANSACTION_JSON_ALL"
    )
    manual_source["expected_record_count"] = -1
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="strictly positive"):
        build_generation(spec)
