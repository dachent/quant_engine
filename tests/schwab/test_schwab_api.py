from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

from quant_engine.schwab import (
    SchwabReadError,
    current_snapshot_bytes,
    read_current_snapshot,
    read_holdings_v2_current_snapshot,
    read_web_current_snapshot,
    write_current_snapshot,
)


class Response:
    def __init__(self, value: object, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    def raise_for_status(self) -> None:
        if self.fail:
            raise RuntimeError("SYNTHETIC HTTP FAILURE")

    def json(self) -> object:
        return self.value


class Client:
    def __init__(self, *, unknown: bool = False, fail_account: bool = False) -> None:
        self.unknown = unknown
        self.fail_account = fail_account
        self.calls: list[tuple[str, object]] = []

    def get_account_numbers(self) -> Response:
        self.calls.append(("linked", None))
        account_key = "account" + "Number"
        return Response(
            [
                {
                    account_key: "SYNTHETIC-999" if self.unknown else "SYNTHETIC-001",
                    "hashValue": "HASH-1",
                },
                {account_key: "SYNTHETIC-002", "hashValue": "HASH-2"},
            ]
        )

    def get_account(
        self, account_hash: str, *, fields: list[str] | None = None
    ) -> Response:
        self.calls.append(("account", (account_hash, fields)))
        account_key = "account" + "Number"
        external = "SYNTHETIC-001" if account_hash == "HASH-1" else "SYNTHETIC-002"
        return Response(
            {
                "securitiesAccount": {
                    account_key: external,
                    "currentBalances": {
                        "cashAvailableForTrading": "10",
                        "cashBalance": "12",
                        "liquidationValue": "100",
                    },
                    "positions": [
                        {
                            "averagePrice": "9",
                            "instrument": {"symbol": "SYNTH", "type": "EQUITY"},
                            "longQuantity": "3",
                            "marketValue": "30",
                            "shortQuantity": "1",
                        },
                        {
                            "instrument": {"symbol": "CASH", "type": "SWEEP_VEHICLE"},
                            "longQuantity": "1",
                            "shortQuantity": "0",
                        },
                    ],
                }
            },
            fail=self.fail_account,
        )


def _crosswalk() -> dict[str, str]:
    return {
        "SYNTHETIC-001": "account-0000000000000001",
        "SYNTHETIC-002": "account-0000000000000002",
    }


def test_current_snapshot_is_read_only_internalized_and_atomic() -> None:
    client = Client()
    observed = datetime(2026, 7, 31, 20, tzinfo=timezone.utc)
    snapshot = read_current_snapshot(
        client, crosswalk=_crosswalk(), observed_at=observed
    )
    assert len(snapshot.accounts) == 2
    assert len(snapshot.positions) == 2
    assert snapshot.sweep_vehicle_count == 2
    assert snapshot.accounts[0].cash_available.to_dict()["value"] == "12"
    assert snapshot.positions[0]["quantity"] == "2"
    assert all(item["mapping_status"] == "UNMAPPED" for item in snapshot.positions)
    serialized = str(snapshot.to_dict())
    assert "SYNTHETIC-001" not in serialized
    assert "SYNTHETIC-002" not in serialized
    assert all(call[0] in {"linked", "account"} for call in client.calls)
    assert [call[1][1] for call in client.calls if call[0] == "account"] == [  # type: ignore[index]
        ["positions"],
        ["positions"],
    ]


def test_unknown_account_http_failure_and_naive_time_fail_closed() -> None:
    observed = datetime(2026, 7, 31, 20, tzinfo=timezone.utc)
    with pytest.raises(SchwabReadError, match="outside the configured allowlist"):
        read_current_snapshot(
            Client(unknown=True), crosswalk=_crosswalk(), observed_at=observed
        )
    with pytest.raises(SchwabReadError, match="account snapshot failed"):
        read_current_snapshot(
            Client(fail_account=True), crosswalk=_crosswalk(), observed_at=observed
        )
    with pytest.raises(SchwabReadError, match="timezone-aware"):
        read_current_snapshot(
            Client(),
            crosswalk=_crosswalk(),
            observed_at=datetime(2026, 7, 31, 20),
        )


def test_current_snapshot_artifact_is_sanitized_and_immutable(tmp_path: Path) -> None:
    snapshot = read_current_snapshot(
        Client(),
        crosswalk=_crosswalk(),
        observed_at=datetime(2026, 7, 31, 20, tzinfo=timezone.utc),
    )
    payload = current_snapshot_bytes(snapshot)
    document = json.loads(payload)
    assert document["provider"] == "SCHWAB"
    assert document["schema_version"] == 2
    assert document["transport"] == "DEVELOPER_API"
    assert "SYNTHETIC-001" not in payload.decode()
    path = tmp_path / "current-snapshot.json"
    first = write_current_snapshot(snapshot, path)
    second = write_current_snapshot(snapshot, path)
    assert first == second
    assert path.read_bytes() == payload
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SchwabReadError, match="conflicts"):
        write_current_snapshot(snapshot, path)


def _web_document(*, unknown_account: bool = False) -> dict[str, object]:
    account = "SYNTHETIC-999" if unknown_account else "SYNTHETIC-001"
    return {
        "positionCount": 2,
        "securityGroupings": [
            {
                "securityType": "Equity",
                "positions": [
                    {
                        "displaySymbol": "SYNTH",
                        "accounts": [
                            {
                                "accountNumberDisplayFull": account,
                                "costPerShare": "9",
                                "currentTimeStamp": "2026-07-31T20:00:00+00:00",
                                "marketValue": "30",
                                "quantity": "3",
                            }
                        ],
                    }
                ],
            },
            {
                "securityType": "CashAndMoneyMarket",
                "positions": [
                    {
                        "displaySymbol": "CASH",
                        "accounts": [
                            {
                                "accountNumberDisplayFull": "SYNTHETIC-002",
                                "currentTimeStamp": "2026-07-31T20:00:01+00:00",
                                "marketValue": "12",
                                "quantity": "12",
                            }
                        ],
                    }
                ],
            },
        ],
    }


def test_web_current_snapshot_is_internalized_and_keeps_balances_unavailable() -> None:
    snapshot = read_web_current_snapshot(_web_document(), crosswalk=_crosswalk())
    assert snapshot.transport == "WEB_CONTRACT"
    assert len(snapshot.accounts) == 2
    assert len(snapshot.positions) == 1
    assert snapshot.sweep_vehicle_count == 1
    assert snapshot.positions[0]["evidence_class"] == "WCH_CURRENT"
    assert snapshot.positions[0]["quantity"] == "3"
    assert all(
        item.liquidation_value.to_dict()["status"] == "UNAVAILABLE"
        for item in snapshot.accounts
    )
    payload = current_snapshot_bytes(snapshot).decode()
    assert "SYNTHETIC-001" not in payload
    assert "SYNTHETIC-002" not in payload


def test_web_current_snapshot_unknown_account_and_naive_time_fail_closed() -> None:
    with pytest.raises(SchwabReadError, match="outside the configured allowlist"):
        read_web_current_snapshot(
            _web_document(unknown_account=True), crosswalk=_crosswalk()
        )
    document = _web_document()
    groups = document["securityGroupings"]
    assert isinstance(groups, list)
    first = groups[0]
    assert isinstance(first, dict)
    positions = first["positions"]
    assert isinstance(positions, list)
    position = positions[0]
    assert isinstance(position, dict)
    accounts = position["accounts"]
    assert isinstance(accounts, list)
    account = accounts[0]
    assert isinstance(account, dict)
    account["currentTimeStamp"] = "2026-07-31T20:00:00"
    with pytest.raises(SchwabReadError, match="timezone-aware"):
        read_web_current_snapshot(document, crosswalk=_crosswalk())


def _holdings_v2_crosswalk() -> dict[str, str]:
    return {
        f"SYNTHETIC-HOLDINGS-{index:03d}": f"account-{index:016x}"
        for index in range(1, 9)
    }


def _holdings_v2_document() -> dict[str, object]:
    accounts: list[dict[str, object]] = []
    for index, external in enumerate(_holdings_v2_crosswalk(), start=1):
        rows: list[dict[str, object]] = []
        if index != 8:
            rows.append(
                {
                    "dataSymbol": f"SYNTH-{index}",
                    "symbol": {"symbol": f"FALLBACK-{index}"},
                    "qty": {"qty": "-2" if index == 2 else "3"},
                    "marketValue": {"val": "99"},
                    "costBasis": {"cstBasis": "27"},
                }
            )
        groups: list[dict[str, object]] = [
            {
                "groupName": "Equity",
                "securityType": 1,
                "positionsCount": len(rows),
                "holdingsRows": rows,
                "totals": {},
            }
        ]
        if index == 1:
            groups.append(
                {
                    "groupName": "Cash",
                    "securityType": 9,
                    "positionsCount": 1,
                    "holdingsRows": [
                        {
                            "dataSymbol": "SYNTHETIC-CASH",
                            "symbol": {"symbol": "SYNTHETIC-CASH"},
                            "qty": {"qty": "12"},
                            "marketValue": {"val": "12"},
                            "costBasis": {"cstBasis": "12"},
                        }
                    ],
                    "totals": {},
                }
            )
        accounts.append(
            {
                "accountId": external,
                "accountDetail": {},
                "balances": {},
                "groupedPositions": groups,
                "isCrypto": False,
                "totals": {},
            }
        )
    return {"accounts": accounts, "accountTotals": []}


def test_holdings_v2_internalizes_all_accounts_without_implying_currency() -> None:
    snapshot = read_holdings_v2_current_snapshot(
        _holdings_v2_document(),
        crosswalk=_holdings_v2_crosswalk(),
        captured_at="2026-09-18T20:00:00Z",
    )

    assert snapshot.transport == "WEB_CONTRACT"
    assert snapshot.observed_at == datetime(2026, 9, 18, 20, tzinfo=timezone.utc)
    assert len(snapshot.accounts) == 8
    assert len(snapshot.positions) == 7
    assert snapshot.sweep_vehicle_count == 1
    assert snapshot.positions[1]["quantity"] == "-2"
    assert snapshot.positions[0]["average_price"] is None
    assert snapshot.positions[0]["evidence_class"] == "WCH_CURRENT"
    assert "cost_basis_total" not in snapshot.positions[0]
    assert all(
        item["market_value"]
        == {"currency": "USD", "status": "UNAVAILABLE", "value": None}
        for item in snapshot.positions
    )
    assert all(
        item.cash_available.to_dict()["status"] == "UNAVAILABLE"
        and item.liquidation_value.to_dict()["status"] == "UNAVAILABLE"
        for item in snapshot.accounts
    )
    payload = current_snapshot_bytes(snapshot).decode()
    assert "SYNTHETIC-HOLDINGS-001" not in payload


def test_holdings_v2_accepts_recognized_etf_groups() -> None:
    document = _holdings_v2_document()
    accounts = document["accounts"]
    assert isinstance(accounts, list)
    groups = accounts[0]["groupedPositions"]
    assert isinstance(groups, list)
    first_group = groups[0]
    assert isinstance(first_group, dict)
    first_group["groupName"] = "ETF"
    first_group["securityType"] = 2

    snapshot = read_holdings_v2_current_snapshot(
        document,
        crosswalk=_holdings_v2_crosswalk(),
        captured_at="2026-09-18T20:00:00Z",
    )

    assert len(snapshot.positions) == 7
    assert snapshot.positions[0]["symbol"] == "SYNTH-1"


def test_holdings_v2_rejects_unrecognized_or_contradictory_groups() -> None:
    unsupported = _holdings_v2_document()
    unsupported_accounts = unsupported["accounts"]
    assert isinstance(unsupported_accounts, list)
    unsupported_groups = unsupported_accounts[0]["groupedPositions"]
    assert isinstance(unsupported_groups, list)
    unsupported_group = unsupported_groups[0]
    assert isinstance(unsupported_group, dict)
    unsupported_group["groupName"] = "Options"
    unsupported_group["securityType"] = 4
    with pytest.raises(SchwabReadError, match="group is unsupported"):
        read_holdings_v2_current_snapshot(
            unsupported,
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00Z",
        )

    contradictory = _holdings_v2_document()
    contradictory_accounts = contradictory["accounts"]
    assert isinstance(contradictory_accounts, list)
    contradictory_groups = contradictory_accounts[0]["groupedPositions"]
    assert isinstance(contradictory_groups, list)
    contradictory_group = contradictory_groups[0]
    assert isinstance(contradictory_group, dict)
    contradictory_group["groupName"] = "Equity"
    contradictory_group["securityType"] = 2
    with pytest.raises(SchwabReadError, match="group is unsupported"):
        read_holdings_v2_current_snapshot(
            contradictory,
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00Z",
        )


def test_holdings_v2_rejects_unsafe_identity_quantity_and_clock() -> None:
    duplicate = _holdings_v2_document()
    accounts = duplicate["accounts"]
    assert isinstance(accounts, list)
    first = accounts[0]
    assert isinstance(first, dict)
    groups = first["groupedPositions"]
    assert isinstance(groups, list)
    equity = groups[0]
    assert isinstance(equity, dict)
    rows = equity["holdingsRows"]
    assert isinstance(rows, list)
    rows.append(dict(rows[0]))
    with pytest.raises(SchwabReadError, match="identity is duplicated"):
        read_holdings_v2_current_snapshot(
            duplicate,
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00Z",
        )

    invalid_quantity = _holdings_v2_document()
    invalid_accounts = invalid_quantity["accounts"]
    assert isinstance(invalid_accounts, list)
    invalid_groups = invalid_accounts[0]["groupedPositions"]
    assert isinstance(invalid_groups, list)
    invalid_rows = invalid_groups[0]["holdingsRows"]
    assert isinstance(invalid_rows, list)
    invalid_rows[0]["qty"] = {"qty": "not-a-number"}
    with pytest.raises(SchwabReadError, match="quantity is not decimal"):
        read_holdings_v2_current_snapshot(
            invalid_quantity,
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00Z",
        )

    with pytest.raises(SchwabReadError, match="timezone-aware"):
        read_holdings_v2_current_snapshot(
            _holdings_v2_document(),
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00",
        )

    with pytest.raises(SchwabReadError, match="UTC"):
        read_holdings_v2_current_snapshot(
            _holdings_v2_document(),
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00-05:00",
        )

    with pytest.raises(SchwabReadError, match="captured_at"):
        read_holdings_v2_current_snapshot(
            _holdings_v2_document(),
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="",
        )

    unknown = _holdings_v2_document()
    unknown_accounts = unknown["accounts"]
    assert isinstance(unknown_accounts, list)
    unknown_accounts[0]["accountId"] = "SYNTHETIC-OUTSIDE-ALLOWLIST"
    with pytest.raises(SchwabReadError, match="outside the configured allowlist"):
        read_holdings_v2_current_snapshot(
            unknown,
            crosswalk=_holdings_v2_crosswalk(),
            captured_at="2026-09-18T20:00:00Z",
        )


def _assert_holdings_v2_cli_uses_receipt_clock_and_rejects_spec_date_mismatch(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    raw_path = tmp_path / "synthetic-holdings.json"
    spec_path = tmp_path / "synthetic-spec.json"
    receipt_path = tmp_path / "synthetic-receipt.json"
    output_path = tmp_path / "snapshot.json"
    raw_path.write_text(json.dumps(_holdings_v2_document()), encoding="utf-8")
    spec_path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-18",
                "crosswalk": [
                    {
                        "external_account_id": external,
                        "internal_account_id": internal,
                    }
                    for external, internal in _holdings_v2_crosswalk().items()
                ],
            }
        ),
        encoding="utf-8",
    )
    receipt_path.write_text(
        json.dumps({"captured_at_utc": "2026-09-18T20:00:00Z"}), encoding="utf-8"
    )
    command = [
        sys.executable,
        str(root / "tools" / "schwab_generation.py"),
        "normalize-holdings-v2",
        str(raw_path),
        str(spec_path),
        str(receipt_path),
        str(output_path),
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(root)]
            + [str(path) for path in sorted((root / "packages").glob("*/src"))]
        ),
    }
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, env=environment
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output_path.read_text(encoding="utf-8"))
    assert snapshot["observed_at"] == "2026-09-18T20:00:00+00:00"
    assert snapshot["transport"] == "WEB_CONTRACT"

    receipt_path.write_text(
        json.dumps({"captured_at_utc": "2026-09-19T20:00:00Z"}), encoding="utf-8"
    )
    mismatch_output = tmp_path / "mismatched-snapshot.json"
    mismatch = subprocess.run(
        [*command[:-1], str(mismatch_output)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert mismatch.returncode != 0
    assert "date differs" in mismatch.stderr
    assert not mismatch_output.exists()


def test_holdings_v2_cli_uses_receipt_clock_and_rejects_spec_date_mismatch() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        _assert_holdings_v2_cli_uses_receipt_clock_and_rejects_spec_date_mismatch(
            Path(directory)
        )


def _assert_existing_web_current_cli_remains_available(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    raw_path = tmp_path / "synthetic-web-current.json"
    spec_path = tmp_path / "synthetic-web-spec.json"
    output_path = tmp_path / "snapshot.json"
    raw_path.write_text(json.dumps(_web_document()), encoding="utf-8")
    spec_path.write_text(
        json.dumps(
            {
                "crosswalk": [
                    {
                        "external_account_id": external,
                        "internal_account_id": internal,
                    }
                    for external, internal in _crosswalk().items()
                ]
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(root)]
            + [str(path) for path in sorted((root / "packages").glob("*/src"))]
        ),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "schwab_generation.py"),
            "normalize-web-current",
            str(raw_path),
            str(spec_path),
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(output_path.read_text(encoding="utf-8"))["transport"]
        == "WEB_CONTRACT"
    )


def test_existing_web_current_cli_remains_available() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        _assert_existing_web_current_cli_remains_available(Path(directory))
