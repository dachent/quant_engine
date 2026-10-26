"""The holdings identity sidecar: symbol and asset category per held instrument.

Derived from retained ledger rows only.  Where no retained row proves a symbol the
row says UNAVAILABLE with a reason and the exact pull; nothing is guessed.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import portfolio_report as reporter
from holdings_identity import build_holdings_identity
from verify_holdings_identity import HoldingsIdentityError, verify_holdings_identity

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "portfolio"
        / "report-data.schema.json"
    ).read_text(encoding="utf-8")
)
SECTION_SCHEMA = {**SCHEMA, "required": []}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


IBKR_ID = "security-ibkr-" + _h("265598")[:24]
IBKR_ID_2 = "security-ibkr-" + _h("756733")[:24]
SCHWAB_ID = "security-schwab-" + _h("SPY")[:24]
ALPACA_ID = "security-alpaca-" + _h("QQQ")[:24]
LYING_ID = "security-fidelity-" + _h("IWM")[:24]


def _catalog() -> dict[str, Any]:
    def position(account: str, security: str, **extra: Any) -> dict[str, Any]:
        return {
            "account_id": account,
            "as_of_date": "2026-09-17",
            "mapping_status": "MAPPED",
            "market_value": _amount("10"),
            "quantity": "1",
            "security_id": security,
            "strategy_id": "strategy-1",
            **extra,
        }

    return {
        "as_of_date": "2026-09-17",
        "positions": [
            position("ibkr-1", IBKR_ID, asset_category="STK", sub_category="ETF"),
            position("ibkr-1", IBKR_ID_2),
            position("schwab-1", SCHWAB_ID, symbol="SPY"),
            position("alpaca-1", ALPACA_ID),
            position("fid-1", LYING_ID, symbol="XYZ"),
        ],
        "securities": [
            {
                "currency": "USD",
                "provider": "IBKR",
                "provider_id": "ibkr",
                "provider_identifier_sha256": _h("265598"),
                "security_id": IBKR_ID,
            },
            {
                "currency": "USD",
                "provider_id": "alpaca",
                "security_id": ALPACA_ID,
                "symbol": "QQQ",
            },
        ],
        "transactions": [],
    }


def _rows(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    positions = reporter._positions(catalog)
    section = build_holdings_identity(positions, catalog)
    return {row["security_id"]: row for row in section["rows"]}


def test_ibkr_symbol_is_unavailable_with_the_exact_pull_not_guessed() -> None:
    row = _rows(_catalog())[IBKR_ID]

    assert row["symbol"] is None
    assert row["symbol_status"] == "UNAVAILABLE"
    assert row["symbol_unavailable_reason"] == "NO_SYMBOL_IN_RETAINED_LEDGER_ROWS"
    assert row["provider"] == "ibkr"
    assert row["identity"]["provider_identifier_sha256"] == _h("265598")
    assert row["asset_category"] == "STK"
    assert row["asset_category_status"] == "AVAILABLE"
    assert row["identity"]["sub_category"] == "ETF"
    section = build_holdings_identity(reporter._positions(_catalog()), _catalog())
    pulls = {item["reason"]: item for item in section["required_pulls"]}
    assert "Flex statement" in pulls["NO_SYMBOL_IN_RETAINED_LEDGER_ROWS"]["pull"]


def test_position_row_and_security_table_symbols_are_proven_by_the_id_hash() -> None:
    rows = _rows(_catalog())

    assert rows[SCHWAB_ID]["symbol"] == "SPY"
    assert rows[SCHWAB_ID]["symbol_source"] == "POSITION_ROW"
    assert rows[SCHWAB_ID]["symbol_proof"] == "ID_HASH_VERIFIED"
    assert rows[ALPACA_ID]["symbol"] == "QQQ"
    assert rows[ALPACA_ID]["symbol_source"] == "SECURITY_TABLE"
    assert rows[ALPACA_ID]["asset_category"] is None
    assert rows[ALPACA_ID]["asset_category_unavailable_reason"] == (
        "ASSET_CATEGORY_NOT_CARRIED_BY_LEDGER_POSITION_ROW"
    )


def test_a_symbol_that_does_not_hash_to_its_security_id_is_refused() -> None:
    row = _rows(_catalog())[LYING_ID]

    assert row["symbol"] is None
    assert row["symbol_unavailable_reason"] == "SYMBOL_DOES_NOT_HASH_TO_SECURITY_ID"


def test_transaction_symbol_resolves_a_security_and_conflicts_are_refused() -> None:
    catalog = _catalog()
    catalog["transactions"] = [
        {"security_id": IBKR_ID_2, "symbol": "VTI"},
        {"security_id": IBKR_ID, "symbol": "AAA"},
        {"security_id": IBKR_ID, "symbol": "BBB"},
    ]
    rows = _rows(catalog)

    assert rows[IBKR_ID_2]["symbol"] == "VTI"
    assert rows[IBKR_ID_2]["symbol_source"] == "TRANSACTION_ROW"
    assert rows[IBKR_ID_2]["symbol_proof"] == "LEDGER_ROW_CARRIED"
    assert rows[IBKR_ID]["symbol"] is None
    assert rows[IBKR_ID]["symbol_unavailable_reason"] == (
        "CONFLICTING_SYMBOLS_IN_RETAINED_LEDGER_ROWS"
    )


def test_summary_counts_and_the_empty_position_source() -> None:
    catalog = _catalog()
    section = build_holdings_identity(reporter._positions(catalog), catalog)

    assert section["summary"] == {
        "asset_category_available_count": 1,
        "asset_category_unavailable_count": 4,
        "row_count": 5,
        "symbol_available_count": 2,
        "symbol_unavailable_count": 3,
    }
    empty = build_holdings_identity(reporter._positions({"positions": []}), {})
    assert empty["rows"] == [] and empty["summary"]["row_count"] == 0


def _report(catalog: dict[str, Any]) -> dict[str, Any]:
    positions = reporter._positions(catalog)
    return {
        "positions": positions,
        "holdings_identity": build_holdings_identity(positions, catalog),
    }


def test_independent_verifier_agrees_with_the_builder() -> None:
    catalog = _catalog()
    receipt = verify_holdings_identity(_report(catalog), catalog)

    assert receipt["holdings_identity_rows"] == 5
    assert receipt["holdings_identity_symbol_available"] == 2


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: r["holdings_identity"]["rows"][0].update(symbol="AAPL"),
        lambda r: r["holdings_identity"]["rows"][0].update(asset_category="OPT"),
        lambda r: r["holdings_identity"]["rows"].pop(),
        lambda r: r["holdings_identity"]["summary"].update(row_count=99),
    ],
)
def test_verifier_rejects_a_hand_typed_or_dropped_value(tamper: Any) -> None:
    catalog = _catalog()
    report = _report(catalog)
    tamper(report)

    with pytest.raises(HoldingsIdentityError):
        verify_holdings_identity(report, catalog)


def test_verifier_requires_the_sidecar() -> None:
    with pytest.raises(HoldingsIdentityError):
        verify_holdings_identity({"positions": []}, {})


def test_schema_admits_the_sidecar_and_old_reports_without_it() -> None:
    validator = Draft202012Validator(SECTION_SCHEMA, format_checker=FormatChecker())
    report = _report(_catalog())
    document = {
        "report_schema_version": 4,
        "holdings_identity": report["holdings_identity"],
    }

    assert [e.message[:100] for e in validator.iter_errors(document)] == []
    assert [
        e.message[:100] for e in validator.iter_errors({"report_schema_version": 4})
    ] == []
    forged = deepcopy(document)
    forged["holdings_identity"]["rows"][0]["symbol_status"] = "MAYBE"
    assert list(validator.iter_errors(forged))
