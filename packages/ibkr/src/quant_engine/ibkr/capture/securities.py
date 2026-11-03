"""conid -> symbol / assetCategory resolution keyed like the ledger security id.

The ledger names an IBKR security ``security-ibkr-<sha256(conid)[:24]>`` and its
rows carry no symbol, so retained holdings show no symbol.  A Flex statement
row carries ``conid`` together with ``symbol`` and ``assetCategory``; this
module aggregates those rows per hashed key and resolves a key only when the
evidence is unambiguous.  Anything else is reported by key, never guessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree as ET

from ._util import reject_declarations

LEDGER_PREFIX = "security-ibkr-"
KEY_LENGTH = 24
STATUS_RESOLVED = "RESOLVED"
STATUS_MISSING_SYMBOL = "MISSING_SYMBOL"
STATUS_MISSING_ASSET_CATEGORY = "MISSING_ASSET_CATEGORY"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_NOT_OBSERVED = "NOT_OBSERVED"

# Row elements that may carry instrument identity.  Other elements with a
# ``conid`` attribute are ignored, not guessed at.
SECURITY_TAGS = frozenset(
    {
        "OpenPosition",
        "Trade",
        "CashTransaction",
        "CorporateAction",
        "Transfer",
        "SecurityInfo",
        "Lot",
    }
)
_CONID = re.compile(r"^[0-9]{1,18}$")
_KEY = re.compile(rf"^[0-9a-f]{{{KEY_LENGTH}}}$")
_DATE_ATTRIBUTES = ("reportDate", "tradeDate", "dateTime", "date")
_DATE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


class SecurityScanError(ValueError):
    """A Flex file carries an instrument identity this driver will not guess."""


def security_key(conid: str) -> str:
    """``sha256(conid)[:24]`` - the suffix of the ledger security id."""

    if _CONID.fullmatch(conid) is None:
        raise SecurityScanError("conid must be a positive integer string")
    return hashlib.sha256(conid.encode()).hexdigest()[:KEY_LENGTH]


def ledger_security_id(conid: str) -> str:
    return f"{LEDGER_PREFIX}{security_key(conid)}"


def normalize_key(value: str) -> str:
    """Accept a bare 24-hex key or a full ledger security id."""

    candidate = (
        value[len(LEDGER_PREFIX) :] if value.startswith(LEDGER_PREFIX) else value
    )
    if _KEY.fullmatch(candidate) is None:
        raise SecurityScanError("a security key must be 24 lowercase hex characters")
    return candidate


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iso_day(element: ET.Element) -> str | None:
    for name in _DATE_ATTRIBUTES:
        match = _DATE.match((element.attrib.get(name) or "").strip())
        if match is not None:
            return "-".join(match.groups())
    return None


@dataclass
class SecurityAggregate:
    symbols: dict[str, list[str | None]] = field(default_factory=dict)
    categories: set[str] = field(default_factory=set)
    rows: int = 0
    rows_missing_symbol: int = 0
    first_seen: str | None = None
    last_seen: str | None = None

    def add(self, symbol: str | None, category: str | None, day: str | None) -> None:
        self.rows += 1
        if category:
            self.categories.add(category)
        if symbol is None:
            self.rows_missing_symbol += 1
        else:
            span = self.symbols.setdefault(symbol, [day, day])
            if day is not None:
                span[0] = day if span[0] is None else min(span[0], day)
                span[1] = day if span[1] is None else max(span[1], day)
        if day is not None:
            self.first_seen = (
                day if self.first_seen is None else min(self.first_seen, day)
            )
            self.last_seen = day if self.last_seen is None else max(self.last_seen, day)

    def row(self, key: str) -> dict[str, Any]:
        return {
            "key": key,
            "symbols": [
                {"symbol": name, "first_seen": span[0], "last_seen": span[1]}
                for name, span in sorted(self.symbols.items())
            ],
            "asset_categories": sorted(self.categories),
            "rows": self.rows,
            "rows_missing_symbol": self.rows_missing_symbol,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def scan_securities(path: Path) -> dict[str, SecurityAggregate]:
    """Aggregate instrument identity rows of one Flex XML file by hashed key."""

    reject_declarations(path.read_bytes())
    found: dict[str, SecurityAggregate] = {}
    try:
        for _, element in ET.iterparse(path, events=("end",)):
            if _local(element.tag) not in SECURITY_TAGS:
                continue
            conid = (element.attrib.get("conid") or "").strip()
            if conid:
                symbol = (element.attrib.get("symbol") or "").strip() or None
                category = (element.attrib.get("assetCategory") or "").strip() or None
                found.setdefault(security_key(conid), SecurityAggregate()).add(
                    symbol, category, _iso_day(element)
                )
            element.clear()
    except ET.ParseError:
        raise SecurityScanError("Flex XML is malformed or truncated") from None
    return found


def aggregate_rows(found: Mapping[str, SecurityAggregate]) -> list[dict[str, Any]]:
    return [found[key].row(key) for key in sorted(found)]


@dataclass(frozen=True)
class SymbolEntry:
    key: str
    status: str
    symbol: str | None
    asset_category: str | None
    symbols: tuple[str, ...]
    asset_categories: tuple[str, ...]
    rows: int

    def row(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "ledger_security_id": f"{LEDGER_PREFIX}{self.key}",
            "status": self.status,
            "symbol": self.symbol,
            "asset_category": self.asset_category,
            "symbols": list(self.symbols),
            "asset_categories": list(self.asset_categories),
            "rows": self.rows,
        }


def _entry(key: str, symbols: set[str], categories: set[str], rows: int) -> SymbolEntry:
    ordered_symbols = tuple(sorted(symbols))
    ordered_categories = tuple(sorted(categories))
    if len(symbols) > 1 or len(categories) > 1:
        status = STATUS_AMBIGUOUS
    elif not symbols:
        status = STATUS_MISSING_SYMBOL
    elif not categories:
        status = STATUS_MISSING_ASSET_CATEGORY
    else:
        status = STATUS_RESOLVED
    resolved = status == STATUS_RESOLVED
    return SymbolEntry(
        key=key,
        status=status,
        symbol=ordered_symbols[0] if resolved else None,
        asset_category=ordered_categories[0] if resolved else None,
        symbols=ordered_symbols,
        asset_categories=ordered_categories,
        rows=rows,
    )


class SymbolTable:
    """Resolver over normalized security rows from one or more captures."""

    def __init__(self, entries: Mapping[str, SymbolEntry]) -> None:
        self._entries = dict(entries)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> SymbolTable:
        symbols: dict[str, set[str]] = {}
        categories: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for row in rows:
            key = normalize_key(str(row["key"]))
            symbols.setdefault(key, set()).update(
                str(item["symbol"]) for item in row["symbols"]
            )
            categories.setdefault(key, set()).update(
                str(item) for item in row["asset_categories"]
            )
            counts[key] = counts.get(key, 0) + int(row["rows"])
        return cls(
            {
                key: _entry(key, symbols[key], categories[key], counts[key])
                for key in symbols
            }
        )

    def lookup(self, key_or_ledger_id: str) -> SymbolEntry:
        key = normalize_key(key_or_ledger_id)
        found = self._entries.get(key)
        if found is not None:
            return found
        return SymbolEntry(key, STATUS_NOT_OBSERVED, None, None, (), (), 0)

    def report(self, required: Iterable[str]) -> dict[str, Any]:
        """Per-status counts and the exact keys that are not resolved."""

        keys = sorted({normalize_key(item) for item in required})
        entries = [self.lookup(key) for key in keys]
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return {
            "required": len(keys),
            "resolved": counts.get(STATUS_RESOLVED, 0),
            "status_counts": dict(sorted(counts.items())),
            "unresolved": [
                {"key": entry.key, "status": entry.status}
                for entry in entries
                if entry.status != STATUS_RESOLVED
            ],
        }

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": "ibkr-symbol-table-v1",
            "entries": [self._entries[key].row() for key in sorted(self._entries)],
        }
