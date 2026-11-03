"""Synthetic fixtures and fakes for the Alpaca capture driver tests.

Every value here is invented.  Response shapes follow the Alpaca documentation
and are UNVERIFIED against a live account.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from quant_engine.alpaca.capture import (
    CaptureConfig,
    GovernedFetcher,
    RawResponse,
    authorize_live_read,
    capture_config_from_mapping,
    catalog_sha256,
)
from quant_engine.alpaca.capture.governance import TERMS_SHA256, TERMS_VERSION
from quant_engine.alpaca.capture.util import sha256_text

ACCOUNT_ID = "SYNTH-ACCOUNT-0001"
NUMBER_KEY = "account_" + "number"
REVIEW = "https://github.com/dachent/quant_engine/issues/1#issuecomment-1"


def config_mapping(**overrides: Any) -> dict[str, Any]:
    table: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "environment": "paper",
        "after": "2024-01-01",
        "until": "2024-12-31",
        "page_size": 2,
        "max_pages": 10,
    }
    table.update(overrides)
    return {
        "alpaca_capture": table,
        "credentials": {
            "alpaca": {
                "key_id": [{"source": "windows-credential-manager", "name": "synth-a"}],
                "key_secret": [
                    {"source": "windows-credential-manager", "name": "synth-b"}
                ],
            }
        },
    }


def capture_config(**overrides: Any) -> CaptureConfig:
    return capture_config_from_mapping(config_mapping(**overrides))


def account_body(**overrides: Any) -> bytes:
    value: dict[str, Any] = {
        "id": ACCOUNT_ID,
        NUMBER_KEY: "SYNTH-NUMBER",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "100.25",
        "portfolio_value": "250.50",
        "equity": "250.50",
        "long_market_value": "150.25",
        "short_market_value": "0",
        "extra_unmodelled_field": "ignored",
    }
    value.update(overrides)
    return json.dumps(value).encode()


def positions_body() -> bytes:
    return json.dumps(
        [
            {
                "symbol": "SYNB",
                "asset_class": "us_equity",
                "qty": "2",
                "side": "long",
                "avg_entry_price": "10.5",
                "market_value": "22",
                "cost_basis": "21",
            },
            {
                "symbol": "SYNA",
                "asset_class": "us_equity",
                "qty": "1",
                "side": "long",
                "avg_entry_price": "5",
                "market_value": "6",
                "cost_basis": "5",
            },
        ]
    ).encode()


def fill(index: int, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": f"20240102000000{index:03d}::synthetic-fill-{index}",
        "activity_type": "FILL",
        "transaction_time": f"2024-01-02T15:00:{index:02d}.5Z",
        "type": "fill",
        "price": "10.50",
        "qty": "2",
        "side": "buy",
        "symbol": "SYNA",
        "leaves_qty": "0",
        "order_id": f"synthetic-order-{index}",
        "cum_qty": "2",
        "order_status": "filled",
    }
    value.update(overrides)
    return value


def nontrade(
    index: int, activity_type: str, amount: str, **extra: Any
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": f"20240201000000{index:03d}::synthetic-{activity_type.lower()}-{index}",
        "activity_type": activity_type,
        "date": "2024-02-01",
        "net_amount": amount,
    }
    value.update(extra)
    return value


def page(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps(rows).encode()


def ok(body: bytes) -> RawResponse:
    return RawResponse(status=200, body=body, headers={})


class ScriptedFetcher:
    """A RawFetcher that replays scripted responses and records every call."""

    def __init__(self, responses: list[RawResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch(self, path: str, query: Mapping[str, str]) -> RawResponse:
        self.calls.append((path, dict(query)))
        return self._responses.pop(0)


def write_evidence(
    directory: Path,
    capture: CaptureConfig,
    name: str = "evidence.json",
    **overrides: Any,
) -> Path:
    document: dict[str, Any] = {
        "schema_version": 1,
        "operation": "capture",
        "environment": capture.environment,
        "account_binding": {"sha256": sha256_text(capture.account_id)},
        "scope": {
            "endpoints": [
                {"method": item.split(" ")[0], "path": item.split(" ")[1]}
                for item in capture.planned_endpoints()
            ]
        },
        "review": {"identity": f"synthetic-reviewer-{name}", "reference": REVIEW},
        "authority": {
            "status": "CURRENT",
            "issued_at": "2020-01-01T00:00:00Z",
            "expires_at": "2999-01-01T00:00:00Z",
        },
        "terms": {"version": TERMS_VERSION, "sha256": TERMS_SHA256},
        "catalog": {"sha256": catalog_sha256()},
        "metering": {"max_requests": 50, "retry_count": 0},
    }
    document.update(overrides)
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def governed(
    tmp_path: Path, capture: CaptureConfig, script: list[RawResponse], **evidence: Any
) -> tuple[GovernedFetcher, ScriptedFetcher, Any]:
    governance = authorize_live_read(
        write_evidence(tmp_path, capture, **evidence),
        config=capture,
        receipt_root=tmp_path / "receipts",
    )
    inner = ScriptedFetcher(script)
    return GovernedFetcher(inner, governance), inner, governance
