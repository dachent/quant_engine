"""Approved Flex Web Service query catalog (what a capture may ask for).

The catalog names *saved* Activity Flex queries by role only.  The Flex query
id and token are never part of the catalog or of any manifest: they come from
the credential resolver and only a SHA-256 of the query id is retained.

The date window is a property of the saved query configured in Client Portal
(this driver sends only ``q``, ``t`` and ``v``).  A request therefore records
the window the operator *expects*, and the normalizer and verifier check that
the returned statements stay inside it and report any uncovered weekday.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._util import canonical_json_bytes, sha256_hex

SERVICE_VERSION = "3"
OUTPUT_FORMAT = "XML"
STATEMENT_TYPE = "AF"
ALLOWED_HOSTS = frozenset(
    {"ndcdyn.interactivebrokers.com", "gdcdyn.interactivebrokers.com"}
)

KIND_ACTIVITY = "activity-statement"
KIND_SECURITY = "security-reference"
WINDOW_RANGE = "range"
WINDOW_SINGLE_DAY = "single_day"

# Sections the existing strict reader (flex.parse_flex_file) requires.
ACTIVITY_SECTIONS = (
    "AccountInformation",
    "CashTransactions",
    "ChangeInNAV",
    "ConversionRates",
    "CorporateActions",
    "EquitySummaryByReportDateInBase",
    "OpenPositions",
    "Trades",
    "Transfers",
)
SECURITY_SECTIONS = ("OpenPositions", "Trades")


@dataclass(frozen=True)
class FlexQuery:
    """One approved saved-query role."""

    name: str
    kind: str
    description: str
    sections: tuple[str, ...]
    window_kind: str
    max_window_days: int
    query_field: str

    def row(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "statement_type": STATEMENT_TYPE,
            "output_format": OUTPUT_FORMAT,
            "service_version": SERVICE_VERSION,
            "sections": list(self.sections),
            "window_kind": self.window_kind,
            "max_window_days": self.max_window_days,
            "query_field": self.query_field,
        }


_QUERIES = (
    FlexQuery(
        name="activity-history",
        kind=KIND_ACTIVITY,
        description="Daily Activity Flex statements for a bounded history window.",
        sections=ACTIVITY_SECTIONS,
        window_kind=WINDOW_RANGE,
        max_window_days=31,
        query_field="query_activity_history",
    ),
    FlexQuery(
        name="activity-current-increment",
        kind=KIND_ACTIVITY,
        description="One finalized business day (current-increment/<date>.xml).",
        sections=ACTIVITY_SECTIONS,
        window_kind=WINDOW_SINGLE_DAY,
        max_window_days=1,
        query_field="query_activity_increment",
    ),
    FlexQuery(
        name="security-reference",
        kind=KIND_SECURITY,
        description=(
            "conid, symbol and assetCategory rows used to resolve security ids "
            "whose retained ledger rows carry no symbol."
        ),
        sections=SECURITY_SECTIONS,
        window_kind=WINDOW_RANGE,
        max_window_days=31,
        query_field="query_security_reference",
    ),
)


def catalog_rows() -> list[dict[str, object]]:
    return [query.row() for query in _QUERIES]


def catalog_sha256() -> str:
    return sha256_hex(canonical_json_bytes(catalog_rows()))


def get_query(name: str) -> FlexQuery:
    for query in _QUERIES:
        if query.name == name:
            return query
    raise KeyError(name)
