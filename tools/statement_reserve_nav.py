"""Statement NAV points of reserve accounts that are not provider-ledger providers (#900).

John Hancock publishes quarterly statements, not a daily feed, and is not a provider in the
financial MVP source contract.  The release pins the staged statement generation's catalog
(path, sha256, size) and the baseline reads it here: each statement closing is an
account-live NAV point, authoritative from its period end until the day before the next
point (the last point until the cutoff).  A point dated after the cutoff is never used, and
a catalog whose bytes differ from the pin is refused.  A pure function of its inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import hashlib
import json
from typing import Any, Final

RESERVE_POOL_ID: Final = "reserve-capital"
_PIN_KEYS: Final = {"path", "sha256", "size_bytes"}


class StatementReserveError(ValueError):
    """The statement reserve input is not the pinned, well-formed catalog."""


def verified_catalog(raw: bytes, pin: Mapping[str, Any]) -> dict[str, Any]:
    if (
        set(pin) != _PIN_KEYS
        or hashlib.sha256(raw).hexdigest() != pin["sha256"]
        or len(raw) != pin["size_bytes"]
    ):
        raise StatementReserveError("statement reserve catalog differs from its pin")
    try:
        catalog = json.loads(raw)
    except ValueError as exc:
        raise StatementReserveError("statement reserve catalog is not JSON") from exc
    if not isinstance(catalog, dict) or catalog.get("catalog_schema_version") != 1:
        raise StatementReserveError("statement reserve catalog shape differs")
    return catalog


def statement_reserve_facts(
    catalog: Mapping[str, Any], cutoff: date
) -> dict[str, list[dict[str, Any]]]:
    """Accounts, reserve-pool assignments, NAV rows and admissions up to the cutoff."""

    accounts: Sequence[Mapping[str, Any]] = catalog.get("accounts") or []
    if len(accounts) != 1:
        raise StatementReserveError("a statement reserve catalog names one account")
    account = accounts[0]
    account_id = str(account["account_id"])
    points = sorted(
        (
            (date.fromisoformat(str(row["as_of_date"])), row)
            for row in catalog.get("nav_observations", [])
            if row.get("account_id") == account_id
            and (row.get("nav") or {}).get("status") == "AVAILABLE"
            and date.fromisoformat(str(row["as_of_date"])) <= cutoff
        ),
        key=lambda item: item[0],
    )
    if not points:
        return {"accounts": [], "assignments": [], "nav": [], "admissions": []}
    admissions: list[dict[str, Any]] = []
    for index, (day, _row) in enumerate(points):
        end = (
            points[index + 1][0] - timedelta(days=1)
            if index + 1 < len(points)
            else cutoff
        )
        admissions.append(
            {
                "account_id": account_id,
                "disposition": "AUTHORITATIVE",
                "effective_end": end.isoformat(),
                "effective_start": day.isoformat(),
                "evidence_layer": "ACCOUNT_LIVE",
                "fact_type": "nav_observations",
                "pool_id": None,
                "provider_id": str(account.get("provider", "")).lower(),
                "strategy_id": None,
            }
        )
    return {
        "accounts": [{"account_id": account_id, "provider": account.get("provider")}],
        "assignments": [
            {
                "account_id": account_id,
                "effective_end": None,
                "effective_start": points[0][0].isoformat(),
                "pool_id": RESERVE_POOL_ID,
            }
        ],
        "nav": [
            {**dict(row), "provider_id": str(account.get("provider", "")).lower()}
            for _day, row in points
        ],
        "admissions": admissions,
    }
