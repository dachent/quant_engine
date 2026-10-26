"""Holdings identity sidecar: one row per held instrument (report payload).

The control panel shows "symbol pending" because the report's ``positions`` rows
carry no ``symbol`` and no ``asset_category``.  This module builds the sidecar
``holdings_identity`` that states, for every position row, the symbol and asset
category the retained provider-ledger rows prove, and otherwise says
``UNAVAILABLE`` with a reason and the exact pull that would resolve it.

Derivation uses retained governed rows only; nothing is typed in, guessed or
fetched.  A symbol comes from, in order: the position row, the ledger
``securities`` row, then the ``transactions`` rows of the same security.  Every
source found must agree.  For providers whose security id is
``sha256(symbol)[:24]`` (Schwab, Alpaca, Fidelity) the symbol must also hash to
the id.  IBKR ids are ``sha256(conid)[:24]``: no symbol can be derived from the id,
so a symbol exists only if a ledger row carries it.

``verify_holdings_identity.py`` derives the same section independently.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
from typing import Any, Final, Mapping, Sequence

HOLDINGS_IDENTITY_SCHEMA: Final = "portfolio-holdings-identity/1"
HOLDINGS_IDENTITY_BASIS: Final = "RETAINED_PROVIDER_LEDGER_ROWS_ONLY"

_SYMBOL_HASH_PROVIDERS: Final = frozenset({"schwab", "alpaca", "fidelity"})
_IDENTITY_KEYS: Final = (
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

NO_SYMBOL_IN_LEDGER: Final = "NO_SYMBOL_IN_RETAINED_LEDGER_ROWS"
CONFLICTING_SYMBOLS: Final = "CONFLICTING_SYMBOLS_IN_RETAINED_LEDGER_ROWS"
SYMBOL_FAILS_HASH: Final = "SYMBOL_DOES_NOT_HASH_TO_SECURITY_ID"
CATEGORY_NOT_CARRIED: Final = "ASSET_CATEGORY_NOT_CARRIED_BY_LEDGER_POSITION_ROW"

REQUIRED_PULLS: Final[dict[str, str]] = {
    "IBKR:" + NO_SYMBOL_IN_LEDGER: (
        "IBKR Flex statement OpenPosition, Trade and CashTransaction rows (conid, "
        "symbol, assetCategory) for the account and date, joined where "
        "sha256(conid)[:24] equals the security id; or an IBKR generation whose "
        "securities rows carry symbol"
    ),
    "OTHER:" + NO_SYMBOL_IN_LEDGER: (
        "the provider statement row for the security carrying its symbol"
    ),
    CONFLICTING_SYMBOLS: "the provider statement row that settles which symbol is current",
    SYMBOL_FAILS_HASH: "the provider statement row for the security",
    CATEGORY_NOT_CARRIED: (
        "the provider statement position row carrying its asset category "
        "(IBKR Flex OpenPosition assetCategory)"
    ),
}


def provider_of(security_id: str, security: Mapping[str, Any]) -> str | None:
    for key in ("provider_id", "provider"):
        value = security.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    parts = security_id.split("-")
    if len(parts) >= 3 and parts[0] == "security":
        return parts[1].lower()
    return None


def _id_hash(symbol: str) -> str:
    return hashlib.sha256(symbol.encode()).hexdigest()[:24]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def build_holdings_identity(
    report_positions: Sequence[Mapping[str, Any]], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the sidecar for the report's ``positions`` rows from ledger rows."""

    securities = {
        str(item["security_id"]): item
        for item in catalog.get("securities", [])
        if isinstance(item, Mapping) and item.get("security_id") is not None
    }
    raw_positions = {
        (
            str(item.get("account_id")),
            str(item.get("as_of_date")),
            str(item.get("security_id")),
        ): item
        for item in catalog.get("positions", [])
        if isinstance(item, Mapping)
    }
    transaction_symbols: dict[str, set[str]] = defaultdict(set)
    for item in catalog.get("transactions", []):
        if not isinstance(item, Mapping):
            continue
        tx_text = _text(item.get("symbol"))
        if tx_text is not None and item.get("security_id") is not None:
            transaction_symbols[str(item["security_id"])].add(tx_text)
    rows: list[dict[str, Any]] = []
    for position in report_positions:
        security_id = position.get("security_id")
        if not isinstance(security_id, str):
            continue
        raw = raw_positions.get(
            (
                str(position.get("account_id")),
                str(position.get("as_of_date")),
                security_id,
            ),
            {},
        )
        security = securities.get(security_id, {})
        provider = provider_of(security_id, security)
        found: dict[str, str] = {}
        for origin, carried in (
            ("POSITION_ROW", _text(raw.get("symbol"))),
            ("SECURITY_TABLE", _text(security.get("symbol"))),
        ):
            if carried is not None:
                found[origin] = carried
        tx = transaction_symbols.get(security_id, set())
        if len(tx) == 1:
            found["TRANSACTION_ROW"] = next(iter(tx))
        distinct = set(found.values()) | (tx if len(tx) > 1 else set())
        symbol: str | None = None
        source: str | None = None
        proof: str | None = None
        reason: str | None = None
        if not distinct:
            reason = NO_SYMBOL_IN_LEDGER
        elif len(distinct) > 1:
            reason = CONFLICTING_SYMBOLS
        else:
            candidate = next(iter(distinct))
            if provider in _SYMBOL_HASH_PROVIDERS:
                if security_id == f"security-{provider}-{_id_hash(candidate)}":
                    symbol, source = candidate, next(iter(found))
                    proof = "ID_HASH_VERIFIED"
                else:
                    reason = SYMBOL_FAILS_HASH
            else:
                symbol, source = candidate, next(iter(found))
                proof = "LEDGER_ROW_CARRIED"
        category = _text(raw.get("asset_category"))
        identity = {
            key: value
            for key in _IDENTITY_KEYS
            for value in (raw.get(key, security.get(key)),)
            if value is not None
        }
        rows.append(
            {
                "account_id": position.get("account_id"),
                "as_of_date": position.get("as_of_date"),
                "asset_category": category,
                "asset_category_status": (
                    "AVAILABLE" if category is not None else "UNAVAILABLE"
                ),
                "asset_category_unavailable_reason": (
                    None if category is not None else CATEGORY_NOT_CARRIED
                ),
                "currency": security.get("currency"),
                "identity": dict(sorted(identity.items())),
                "mapping_status": position.get("status"),
                "provider": provider,
                "quantity": position.get("quantity"),
                "security_id": security_id,
                "snapshot_status": position.get("snapshot_status"),
                "strategy_id": position.get("strategy_id"),
                "symbol": symbol,
                "symbol_proof": proof,
                "symbol_source": source,
                "symbol_status": "AVAILABLE" if symbol is not None else "UNAVAILABLE",
                "symbol_unavailable_reason": reason,
            }
        )
    rows.sort(
        key=lambda r: (str(r["account_id"]), str(r["as_of_date"]), r["security_id"])
    )
    return {
        "as_of_date": catalog.get("as_of_date"),
        "basis": HOLDINGS_IDENTITY_BASIS,
        "required_pulls": _required_pulls(rows),
        "rows": rows,
        "schema": HOLDINGS_IDENTITY_SCHEMA,
        "summary": _summary(rows),
    }


def _pull_key(row: Mapping[str, Any]) -> str | None:
    reason = row.get("symbol_unavailable_reason")
    if not isinstance(reason, str):
        return None
    if reason == NO_SYMBOL_IN_LEDGER:
        return ("IBKR:" if row.get("provider") == "ibkr" else "OTHER:") + reason
    return reason


def _required_pulls(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        key = _pull_key(row)
        if key is not None:
            counts[key] += 1
    if any(r["asset_category_status"] == "UNAVAILABLE" for r in rows):
        counts[CATEGORY_NOT_CARRIED] = sum(
            1 for r in rows if r["asset_category_status"] == "UNAVAILABLE"
        )
    return [
        {
            "pull": REQUIRED_PULLS[key],
            "reason": key.split(":", 1)[-1],
            "row_count": count,
        }
        for key, count in sorted(counts.items())
    ]


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "asset_category_available_count": sum(
            1 for r in rows if r["asset_category_status"] == "AVAILABLE"
        ),
        "asset_category_unavailable_count": sum(
            1 for r in rows if r["asset_category_status"] == "UNAVAILABLE"
        ),
        "row_count": len(rows),
        "symbol_available_count": sum(
            1 for r in rows if r["symbol_status"] == "AVAILABLE"
        ),
        "symbol_unavailable_count": sum(
            1 for r in rows if r["symbol_status"] == "UNAVAILABLE"
        ),
    }
