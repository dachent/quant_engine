"""Independent verifier of the report's ``holdings_identity`` sidecar.

It shares no code with ``holdings_identity.py``.  From the retained ledger
catalog (securities, positions and transactions rows) and the report's own
``positions`` rows it re-derives each sidecar row and the summary, then compares
the whole section with the one the report carries.  Any difference, including a
row that is present or missing without a position behind it, is an error.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


class HoldingsIdentityError(ValueError):
    """The sidecar differs from what the retained ledger rows prove."""


_PASSTHROUGH = (
    "cusip",
    "expiry",
    "figi",
    "isin",
    "multiplier",
    "provider_identifier_sha256",
    "put_call",
    "strike",
    "sub_category",
    "underlying_conid_sha256",
)


def _clean(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _expected_row(
    position: Mapping[str, Any],
    raw: Mapping[str, Any],
    security: Mapping[str, Any],
    tx_symbols: set[str],
) -> dict[str, Any]:
    sid = str(position["security_id"])
    provider = None
    for key in ("provider_id", "provider"):
        if isinstance(security.get(key), str) and security[key]:
            provider = security[key].lower()
            break
    if provider is None:
        segments = sid.split("-")
        if segments[0] == "security" and len(segments) >= 3:
            provider = segments[1].lower()
    carried: dict[str, str] = {}
    for name, row in (("POSITION_ROW", raw), ("SECURITY_TABLE", security)):
        text = _clean(row.get("symbol"))
        if text is not None:
            carried[name] = text
    pool = set(carried.values()) | tx_symbols
    symbol = source = proof = reason = None
    if not pool:
        reason = "NO_SYMBOL_IN_RETAINED_LEDGER_ROWS"
    elif len(pool) != 1:
        reason = "CONFLICTING_SYMBOLS_IN_RETAINED_LEDGER_ROWS"
    else:
        symbol = pool.pop()
        source = next(iter(carried), "TRANSACTION_ROW")
        digest = hashlib.sha256(symbol.encode()).hexdigest()[:24]
        matches = sid == "security-" + str(provider) + "-" + digest
        proof = "ID_HASH_VERIFIED" if matches else "LEDGER_ROW_CARRIED"
    category = _clean(raw.get("asset_category"))
    identity = {}
    for key in sorted(_PASSTHROUGH):
        value = raw.get(key, security.get(key))
        if value is not None:
            identity[key] = value
    return {
        "account_id": position.get("account_id"),
        "as_of_date": position.get("as_of_date"),
        "asset_category": category,
        "asset_category_status": "UNAVAILABLE" if category is None else "AVAILABLE",
        "asset_category_unavailable_reason": (
            "ASSET_CATEGORY_NOT_CARRIED_BY_LEDGER_POSITION_ROW"
            if category is None
            else None
        ),
        "currency": security.get("currency"),
        "identity": identity,
        "mapping_status": position.get("status"),
        "provider": provider,
        "quantity": position.get("quantity"),
        "security_id": sid,
        "snapshot_status": position.get("snapshot_status"),
        "strategy_id": position.get("strategy_id"),
        "symbol": symbol,
        "symbol_proof": proof,
        "symbol_source": source,
        "symbol_status": "UNAVAILABLE" if symbol is None else "AVAILABLE",
        "symbol_unavailable_reason": reason,
    }


def derive_holdings_rows(
    report_positions: Sequence[Mapping[str, Any]], catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    by_security: dict[str, Mapping[str, Any]] = {}
    for row in catalog.get("securities") or []:
        by_security[str(row.get("security_id"))] = row
    raw_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in catalog.get("positions") or []:
        key = (
            str(row.get("account_id")),
            str(row.get("as_of_date")),
            str(row.get("security_id")),
        )
        raw_by_key[key] = row
    symbols_by_security: dict[str, set[str]] = {}
    for row in catalog.get("transactions") or []:
        symbol = _clean(row.get("symbol"))
        if symbol is not None and row.get("security_id") is not None:
            symbols_by_security.setdefault(str(row["security_id"]), set()).add(symbol)
    derived = []
    for position in report_positions:
        if not isinstance(position.get("security_id"), str):
            continue
        sid = position["security_id"]
        derived.append(
            _expected_row(
                position,
                raw_by_key.get(
                    (
                        str(position.get("account_id")),
                        str(position.get("as_of_date")),
                        sid,
                    ),
                    {},
                ),
                by_security.get(sid, {}),
                symbols_by_security.get(sid, set()),
            )
        )
    return sorted(
        derived,
        key=lambda r: (str(r["account_id"]), str(r["as_of_date"]), r["security_id"]),
    )


def verify_holdings_identity(
    report: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Re-derive the sidecar and compare; return a receipt, or raise."""

    section = report.get("holdings_identity")
    if not derive_holdings_rows(report.get("positions") or [], catalog):
        if section is not None:
            raise HoldingsIdentityError(
                "holdings_identity present without any held instrument"
            )
        return {
            "holdings_identity_rows": 0,
            "holdings_identity_symbol_available": 0,
            "holdings_identity_symbol_unavailable": 0,
        }
    if not isinstance(section, Mapping):
        raise HoldingsIdentityError("report has no holdings_identity sidecar")
    if section.get("schema") != "portfolio-holdings-identity/1":
        raise HoldingsIdentityError("holdings_identity schema is not recognised")
    expected = derive_holdings_rows(report.get("positions") or [], catalog)
    actual = section.get("rows")
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise HoldingsIdentityError(
            "holdings_identity rows differ from the independent derivation"
        )
    available = sum(1 for row in expected if row["symbol_status"] == "AVAILABLE")
    summary = section.get("summary")
    if not isinstance(summary, Mapping) or (
        summary.get("row_count"),
        summary.get("symbol_available_count"),
        summary.get("symbol_unavailable_count"),
    ) != (len(expected), available, len(expected) - available):
        raise HoldingsIdentityError("holdings_identity summary differs")
    return {
        "holdings_identity_rows": len(expected),
        "holdings_identity_symbol_available": available,
        "holdings_identity_symbol_unavailable": len(expected) - available,
    }
