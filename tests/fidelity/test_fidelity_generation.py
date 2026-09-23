from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.fidelity import (
    FidelityGenerationError,
    build_generation,
    stage_generation_candidate,
)


HEADER = [
    "Run Date",
    "Account",
    "Account Number",
    "Action",
    "Symbol",
    "Description",
    "Type",
    "Exchange Quantity",
    "Exchange Currency",
    "Currency",
    "Price",
    "Quantity",
    "Exchange Rate",
    "Commission",
    "Fees",
    "Accrued Interest",
    "Amount",
    "Settlement Date",
]


def _activity(
    run_date: str,
    account: str,
    action: str,
    *,
    symbol: str = "",
    amount: str = "0",
    quantity: str = "0",
    settlement_date: str = "",
) -> list[str]:
    return [
        run_date,
        "SYNTHETIC ACCOUNT",
        account,
        action,
        symbol,
        "SYNTHETIC DESCRIPTION",
        "Cash",
        "",
        "",
        "USD",
        "10" if symbol else "",
        quantity,
        "",
        "0",
        "0",
        "0",
        amount,
        settlement_date,
    ]


def _write_export(path: Path, rows: list[list[str]]) -> None:
    footer = [
        [],
        ["Fidelity synthetic footer"],
        ["Synthetic legal line 1"],
        ["Synthetic legal line 2"],
        ["Synthetic legal line 3"],
        ["Synthetic legal line 4"],
        [],
        ["Synthetic legal line 5"],
        ["Synthetic legal line 6"],
        ["Synthetic legal line 7"],
        [],
        ["Date downloaded 07/31/2026 01:00 pm"],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([])
        writer.writerow([])
        writer.writerow(HEADER)
        writer.writerows(rows)
        writer.writerows(footer)


def _source(path: Path, root: Path, index: int, as_of: str) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "artifact_id": f"legacy/accounts-history-{index}.csv",
        "as_of_date": as_of,
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "relative_path": path.relative_to(root).as_posix(),
        "root_id": "legacy",
        "schema_name": "fidelity-accounts-history-csv",
        "schema_version": "1",
    }


def _spec(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    first = root / "Accounts_History.csv"
    _write_export(
        first,
        [
            _activity(
                "05/31/2022",
                "SYNTHETIC-001",
                "YOU BOUGHT SYNTHETIC SECURITY",
                symbol="SYNTH",
                amount="-10",
                quantity="1",
                settlement_date="06/02/2022",
            ),
            _activity(
                "06/01/2022",
                "SYNTHETIC-001",
                "DIVIDEND RECEIVED SYNTHETIC SECURITY",
                symbol="SYNTH",
                amount="1",
            ),
            _activity(
                "06/02/2022",
                "SYNTHETIC-001",
                "TRANSFERRED TO SYNTHETIC ACCOUNT",
                amount="-5",
            ),
        ],
    )
    second = root / "Accounts_History (1).csv"
    _write_export(
        second,
        [
            _activity(
                "06/03/2022",
                "SYNTHETIC-002",
                "TRANSFERRED FROM SYNTHETIC ACCOUNT",
                symbol="MOVE",
                amount="5",
                quantity="-1",
            ),
            _activity(
                "06/04/2022",
                "SYNTHETIC-002",
                "TRANSFER OF ASSETS ACAT DELIVER SYNTHETIC SECURITY",
                symbol="ACAT",
                amount="-5",
                quantity="-1",
            ),
            _activity(
                "06/05/2022",
                "SYNTHETIC-002",
                "CASH CONTRIBUTION CURRENT YEAR",
                amount="5",
            ),
        ],
    )
    external_key = "external_" + "account_id"
    spec = {
        "as_of_date": "2022-06-05",
        "baseline": {
            "account_count": 2,
            "activity_count": 6,
            "cash_flow_count": 2,
            "corporate_action_count": 2,
            "security_count": 3,
            "source_file_count": 2,
            "transaction_count": 2,
        },
        "coverage_end": "2022-06-05",
        "coverage_start": "2022-05-31",
        "crosswalk": [
            {
                external_key: "SYNTHETIC-001",
                "internal_account_id": "account-0000000000000001",
            },
            {
                external_key: "SYNTHETIC-002",
                "internal_account_id": "account-0000000000000002",
            },
        ],
        "roots": {"legacy": str(root.resolve())},
        "schema_version": 1,
        "sources": [
            _source(first, root, 0, "2022-06-02"),
            _source(second, root, 1, "2022-06-05"),
        ],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_static_generation_is_complete_deterministic_and_reconciled(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    first, first_manifest, first_bytes = build_generation(spec)
    second, second_manifest, second_bytes = build_generation(spec)
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first_manifest.coverage.status.value == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert len(first["accounts"]) == 2
    assert len(first["transactions"]) == 2
    assert len(first["cash_flows"]) == 2
    assert len(first["corporate_actions"]) == 2
    assert first["activity_disposition"]["unclassified"] == 0
    assert first["reconciliation"]["status"] == "PASS_EXACT"
    serialized = first_bytes.decode()
    assert "SYNTHETIC-001" not in serialized
    assert "SYNTHETIC-002" not in serialized
    assert "TRANSFERRED TO SYNTHETIC" not in serialized


def test_stage_is_immutable_and_does_not_publish(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    root = tmp_path / "dataFidelity"
    first = stage_generation_candidate(spec, root)
    second = stage_generation_candidate(spec, root)
    assert first == second
    assert first["published"] is False
    assert not (root / "CURRENT.json").exists()


def test_source_drift_footer_and_unknown_action_fail_closed(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    source = document["sources"][0]
    source_path = Path(document["roots"]["legacy"]) / source["relative_path"]
    source_path.write_text("", encoding="utf-8")
    with pytest.raises(FidelityGenerationError, match="source hash differs"):
        build_generation(spec)

    footer = tmp_path / "footer"
    footer.mkdir()
    spec = _spec(footer)
    document = json.loads(spec.read_text(encoding="utf-8"))
    source = document["sources"][0]
    source_path = Path(document["roots"]["legacy"]) / source["relative_path"]
    lines = source_path.read_text(encoding="utf-8").splitlines()
    source_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    payload = source_path.read_bytes()
    source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FidelityGenerationError, match="footer differs"):
        build_generation(spec)

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    spec = _spec(unknown)
    document = json.loads(spec.read_text(encoding="utf-8"))
    source = document["sources"][0]
    source_path = Path(document["roots"]["legacy"]) / source["relative_path"]
    rows = list(csv.reader(source_path.open("r", encoding="utf-8", newline="")))
    rows[3][3] = "SYNTHETIC UNKNOWN ACTION"
    with source_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    payload = source_path.read_bytes()
    source["expected_sha256"] = hashlib.sha256(payload).hexdigest()
    source["expected_size_bytes"] = len(payload)
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FidelityGenerationError, match="unclassified"):
        build_generation(spec)
