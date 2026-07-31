from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from quant_engine.schwab import (
    SchwabReadError,
    current_snapshot_bytes,
    read_current_snapshot,
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
