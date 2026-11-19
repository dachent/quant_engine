"""A Schwab account's positions at an earlier cutoff, derived from a later export.

The Schwab "Export Positions Data" file lists the non-cash holdings, a symbol-less
``Cash & Cash Investments`` row and a ``Positions Total`` row.  A position set captured
after the cutoff stands for the cutoff only when nothing moved in the account between
them, which is checked here and refused otherwise:

* the export closes: the holdings plus the cash row equal the printed Positions Total;
* optionally the account value of the balances page equals that total;
* no transaction of the account is dated after the cutoff (history is read through the
  capture day, ``history_through``);
* an earlier independent observation of the market value (the balances page of a
  prior capture) equals the export's, so the holdings did not change in between.

The rows are typed ``DERIVED_AT_CUTOFF_FROM_LATER_SNAPSHOT`` and carry the capture date;
nothing is valued at the cutoff by price movement: a holding whose value moves (anything
but a $1.00 money-market fund) cannot be carried back and refuses.
"""

from __future__ import annotations

import csv
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
import re
from typing import Any, Final

from quant_engine.core import canonical_json_bytes

DERIVED_BASIS: Final = "DERIVED_AT_CUTOFF_FROM_LATER_SNAPSHOT"
_CASH_ROW: Final = "Cash & Cash Investments"
_TOTAL_ROW: Final = "Positions Total"
_MONEY_MARKET: Final = "Cash and Money Market"


class SchwabDerivedPositionError(ValueError):
    """The later snapshot cannot stand for the cutoff, or the export does not close."""


def _money(value: str, name: str) -> Decimal:
    text = value.strip().replace("$", "").replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise SchwabDerivedPositionError(f"{name} is not a number") from exc
    if not number.is_finite():
        raise SchwabDerivedPositionError(f"{name} is not finite")
    return -number if negative else number


def _security_id(symbol: str) -> str:
    return f"security-schwab-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


_EXPORT_HEADER: Final = re.compile(
    r'^"?Positions for account .*\.\.\.(\d{3}) as of [^,]*, (\d{4})/(\d{2})/(\d{2})',
    re.M,
)


def _read_export(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.replace("\r\n", "\n").split("\n")]
    start = next(
        (n for n, line in enumerate(lines) if line.startswith('"Symbol"')), None
    )
    if start is None:
        raise SchwabDerivedPositionError("positions export header is absent")
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    if not rows:
        raise SchwabDerivedPositionError("positions export has no rows")
    return rows


def derive_positions_at_cutoff(
    *,
    account_id: str,
    export_text: str,
    cutoff: date,
    capture_date: date,
    history_transactions: Sequence[Mapping[str, Any]],
    history_through: date,
    earlier_market_value: Mapping[str, Decimal],
    account_suffix: str,
    valuation_cutoff: date,
    history_bound: date | None,
    balances_account_value: Decimal | None = None,
) -> list[dict[str, Any]]:
    """The cutoff position rows of ``account_id``, or an error saying why not.

    ``account_suffix``, ``valuation_cutoff`` and ``history_bound`` come from the
    generation's own bytes (crosswalk, current snapshot, transaction coverage), never
    from the spec: the export's own header must name that account and the capture day,
    the cutoff must be the generation's valuation day, and ``history_through`` must not
    claim more days than the transactions actually cover.
    """

    header = _EXPORT_HEADER.search(export_text)
    if header is None:
        raise SchwabDerivedPositionError("positions export header names no account/day")
    if header.group(1) != account_suffix:
        raise SchwabDerivedPositionError(
            "positions export is for a different account than the derived row"
        )
    if date(int(header.group(2)), int(header.group(3)), int(header.group(4))) != (
        capture_date
    ):
        raise SchwabDerivedPositionError(
            "positions export day differs from the declared capture date"
        )
    if cutoff != valuation_cutoff:
        raise SchwabDerivedPositionError(
            "derived cutoff is not the generation valuation day"
        )
    if history_bound is None or history_through > history_bound:
        raise SchwabDerivedPositionError(
            "history_through is later than the transactions the generation covers"
        )
    if not cutoff <= history_through:
        raise SchwabDerivedPositionError("history does not reach the cutoff")
    later = [
        t
        for t in history_transactions
        if str(t.get("account_id")) == account_id
        and cutoff < date.fromisoformat(str(t["event_date"])) <= history_through
    ]
    if later:
        raise SchwabDerivedPositionError(
            "the account has transactions after the cutoff: the snapshot is not the cutoff"
        )
    if history_through < capture_date and not earlier_market_value:
        raise SchwabDerivedPositionError(
            "no independent observation covers the days after the history"
        )
    rows = _read_export(export_text)
    holdings: list[dict[str, str]] = []
    cash = Decimal(0)
    total: Decimal | None = None
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if symbol == _TOTAL_ROW:
            total = _money(row["Mkt Val (Market Value)"], "positions total")
        elif symbol == _CASH_ROW:
            cash += _money(row["Mkt Val (Market Value)"], "cash market value")
        elif symbol:
            holdings.append(row)
    if total is None:
        raise SchwabDerivedPositionError("positions export carries no total row")
    held = sum(
        (_money(h["Mkt Val (Market Value)"], "holding market value") for h in holdings),
        Decimal(0),
    )
    if held + cash != total:
        raise SchwabDerivedPositionError("positions export does not close to its total")
    if balances_account_value is not None and balances_account_value != total:
        raise SchwabDerivedPositionError(
            "positions total differs from the balances account value"
        )
    for observed_day, value in earlier_market_value.items():
        if value != held:
            raise SchwabDerivedPositionError(
                f"market value on {observed_day} differs from the snapshot: "
                "the holdings moved"
            )
    out: list[dict[str, Any]] = []
    for holding in holdings:
        symbol = holding["Symbol"].strip()
        price = _money(holding["Price"], "price")
        quantity = _money(holding["Qty (Quantity)"], "quantity")
        value = _money(holding["Mkt Val (Market Value)"], "holding market value")
        if holding.get("Asset Type", "").strip() != _MONEY_MARKET or price != 1:
            raise SchwabDerivedPositionError(
                f"{symbol} is not a $1.00 money-market fund: its value moves, so the "
                "snapshot cannot be carried back"
            )
        if quantity != value:
            raise SchwabDerivedPositionError(
                f"{symbol} quantity differs from its value at $1.00"
            )
        out.append(
            {
                "account_id": account_id,
                "as_of_date": cutoff.isoformat(),
                "as_of_basis": DERIVED_BASIS,
                "capture_date": capture_date.isoformat(),
                "closure": {
                    "cash_usd": format(cash, "f"),
                    "positions_total_usd": format(total, "f"),
                    "holdings_usd": format(held, "f"),
                    "history_through": history_through.isoformat(),
                    "earlier_market_value_days": sorted(earlier_market_value),
                },
                "evidence_class": DERIVED_BASIS,
                "mapping_status": "UNMAPPED",
                "market_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": format(value, "f"),
                },
                "quantity": format(quantity, "f"),
                "security_id": _security_id(symbol),
                "strategy_id": None,
                "symbol": symbol,
            }
        )
    return out


_BALANCE_LABELS: Final = ("Account Value", "Market Value")


def parse_balances_values(page_text: str) -> tuple[Decimal, Decimal]:
    """(Account Value, Market Value) of a Schwab Balances page text capture."""

    lines = page_text.replace("\r\n", "\n").split("\n")
    found: dict[str, Decimal] = {}
    for index, line in enumerate(lines):
        label = line.strip()
        if label in _BALANCE_LABELS and label not in found:
            for follower in lines[index + 1 : index + 4]:
                stripped = follower.strip()
                if stripped.startswith("$"):
                    found[label] = _money(stripped.split()[0], label)
                    break
    if set(found) != set(_BALANCE_LABELS):
        raise SchwabDerivedPositionError(
            "balances page lacks Account Value or Market Value"
        )
    return found["Account Value"], found["Market Value"]


_CLOSURE_KEYS: Final = frozenset(
    {
        "cash_usd",
        "earlier_market_value_days",
        "history_through",
        "holdings_usd",
        "positions_total_usd",
    }
)
_ROW_KEYS: Final = frozenset(
    {
        "account_id",
        "as_of_basis",
        "as_of_date",
        "capture_date",
        "closure",
        "evidence_class",
        "mapping_status",
        "market_value",
        "quantity",
        "record_id",
        "security_id",
        "source_artifact_id",
        "strategy_id",
        "symbol",
    }
)
_VALUE_KEYS: Final = frozenset({"currency", "status", "value"})
_EXPORT_SCHEMA: Final = "schwab-positions-export"
_FOLDER: Final = re.compile(r"(?:^|/)schwab-([^/-]+)-(\d{4}-\d{2}-\d{2})/[^/]+$")
_BALANCES_SCHEMA: Final = "schwab-live-balances-evidence"
_TRANSACTION_DATE_FIELDS: Final = (
    "event_date",
    "settle_date",
    "effective_date",
    "value_date",
    "accounting_date",
    "trade_date",
)


def _decimal_text(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise SchwabDerivedPositionError(f"{name} is not a decimal text")
    return _money(value, name)


def _day(value: object, name: str) -> date:
    """A canonical ISO date text (``YYYY-MM-DD``), nothing looser."""

    if not isinstance(value, str):
        raise SchwabDerivedPositionError(f"{name} is not a date text")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SchwabDerivedPositionError(f"{name} is not a date") from exc
    if parsed.isoformat() != value:
        raise SchwabDerivedPositionError(f"{name} is not a canonical ISO date")
    return parsed


def residual_window_days(row: Mapping[str, Any]) -> list[str]:
    """The days no transaction page proves for a derived row (a boundary, not the whole doubt).

    Page-verified boundary only: it names where the history ends and the capture begins;
    a same-total reallocation inside it is undetectable, and a page equal in total after
    it does not shrink it.

    The history ends at ``history_through``.  Up to the capture a reallocation that keeps
    the account total unchanged (a purchase of a fund with cash, a switch between funds)
    cannot be seen: a later page equal in total does not rule it out.  Empty when the
    history reaches the capture day.
    """

    closure = row["closure"]
    through, capture = closure["history_through"], row["capture_date"]
    return [through, capture] if through < capture else []


def admit_derived_position_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    accepted_accounts: Collection[str],
    transactions: Sequence[Mapping[str, Any]],
    valuation_cutoff: date,
    snapshot_day: date,
    artifact_schemas: Mapping[str, str],
    transaction_history: Mapping[str, Any],
    nav_observations: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Re-verify catalog position rows derived at the cutoff from a later snapshot.

    The generation derived each row once; a consumer of the catalog re-checks what the
    catalog bytes alone can show and refuses on any difference:

    * the row has exactly the derived key set (no observation clock, no average price) and
      canonical text for ids and dates;
    * a $1.00 USD holding valued at its quantity; unmapped;
    * the export closes (holdings + cash == positions total) and the holdings of the
      account sum to it;
    * the catalog's transaction history of the account spans the cutoff to
      ``history_through``, and no transaction of the account is dated (any of its date
      fields) after the cutoff;
    * exactly one available NAV observation of the account exists on the cutoff day and it
      equals the positions total;
    * the source artifact is a positions export; every earlier day has a balances page in
      the catalog whose id carries that day, inside ``(history_through, capture]``.

    The claim is a net zero change in the account total over ``(cutoff, capture]``;
    offsetting flows that cancel are not excluded by it.  The cutoff-day NAV fixes the
    total, not its split: the holdings/cash split rests on the export and the earlier page.
    What stays unproven is a reallocation inside the account between ``history_through``
    and the first earlier day that keeps its total: ``residual_window_days`` names it.
    Returns the admitted rows by record id (empty when ``rows`` is empty).
    """

    accepted = set(accepted_accounts)
    admitted: dict[str, Mapping[str, Any]] = {}
    by_account: dict[str, list[Mapping[str, Any]]] = {}
    captures: dict[str, date] = {}
    export_owner: dict[str, str] = {}
    for row in rows:
        if set(row) != _ROW_KEYS:
            raise SchwabDerivedPositionError("derived position key set differs")
        record_id = row["record_id"]
        account_id = row["account_id"]
        symbol = row["symbol"]
        if (
            not isinstance(record_id, str)
            or not isinstance(account_id, str)
            or not isinstance(symbol, str)
            or not symbol
            or account_id != account_id.strip()
            or symbol != symbol.strip()
        ):
            raise SchwabDerivedPositionError("derived position identity is invalid")
        if account_id not in accepted:
            raise SchwabDerivedPositionError(
                "derived position account is not an accepted account"
            )
        if (
            row["evidence_class"] != DERIVED_BASIS
            or row["as_of_basis"] != DERIVED_BASIS
        ):
            raise SchwabDerivedPositionError("derived position evidence class differs")
        if row["as_of_date"] != valuation_cutoff.isoformat():
            raise SchwabDerivedPositionError(
                "derived position date is not the valuation cutoff"
            )
        capture = _day(row["capture_date"], "derived position capture_date")
        if not (valuation_cutoff < capture and snapshot_day <= capture):
            raise SchwabDerivedPositionError(
                "derived position capture day is not later than the cutoff and the snapshot"
            )
        if captures.setdefault(account_id, capture) != capture:
            raise SchwabDerivedPositionError(
                "derived positions of an account disagree on their capture day"
            )
        if row["security_id"] != _security_id(symbol):
            raise SchwabDerivedPositionError("derived position security differs")
        if row["mapping_status"] != "UNMAPPED" or row["strategy_id"] is not None:
            raise SchwabDerivedPositionError("derived position must be unmapped")
        source = row["source_artifact_id"]
        if artifact_schemas.get(source) != _EXPORT_SCHEMA:
            raise SchwabDerivedPositionError(
                "derived position source is not a catalog positions export"
            )
        if export_owner.setdefault(source, account_id) != account_id:
            raise SchwabDerivedPositionError(
                "one positions export cannot stand for two accounts"
            )
        identity = {
            "account_id": account_id,
            "as_of_date": valuation_cutoff.isoformat(),
            "basis": DERIVED_BASIS,
            "security_id": row["security_id"],
        }
        expected_id = (
            "position-"
            + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
        )
        if record_id != expected_id or record_id in admitted:
            raise SchwabDerivedPositionError("derived position record id differs")
        value = row["market_value"]
        quantity = _decimal_text(row["quantity"], "derived position quantity")
        if (
            not isinstance(value, Mapping)
            or set(value) != _VALUE_KEYS
            or value["status"] != "AVAILABLE"
            or value["currency"] != "USD"
            or _decimal_text(value["value"], "derived position value") != quantity
        ):
            raise SchwabDerivedPositionError(
                "derived position is not a $1.00 USD holding valued at its quantity"
            )
        admitted[record_id] = row
        by_account.setdefault(account_id, []).append(row)
    for account_id, group in by_account.items():
        closure = group[0]["closure"]
        if not isinstance(closure, Mapping) or set(closure) != _CLOSURE_KEYS:
            raise SchwabDerivedPositionError("derived position closure is invalid")
        if any(item["closure"] != closure for item in group):
            raise SchwabDerivedPositionError(
                "derived positions of an account disagree on their closure"
            )
        cash = _decimal_text(closure["cash_usd"], "closure cash")
        holdings = _decimal_text(closure["holdings_usd"], "closure holdings")
        total = _decimal_text(closure["positions_total_usd"], "closure total")
        held = sum(
            (_decimal_text(item["quantity"], "quantity") for item in group), Decimal(0)
        )
        if held != holdings or holdings + cash != total:
            raise SchwabDerivedPositionError(
                "derived positions do not close to the captured positions total"
            )
        capture_date = captures[account_id]
        through = _day(closure["history_through"], "closure history_through")
        if not valuation_cutoff <= through <= capture_date:
            raise SchwabDerivedPositionError(
                "derived position history does not reach the cutoff or passes the capture"
            )
        accounts = transaction_history.get("accounts")
        history = accounts.get(account_id) if isinstance(accounts, Mapping) else None
        if not isinstance(history, Mapping):
            raise SchwabDerivedPositionError(
                "the catalog states no transaction history for the account"
            )
        if not (
            _day(history.get("effective_start"), "history start") <= valuation_cutoff
            and through <= _day(history.get("effective_end"), "history end")
        ):
            raise SchwabDerivedPositionError(
                "the account's transaction history does not span the cutoff to history_through"
            )
        for transaction in transactions:
            owner = transaction.get("account_id")
            if not isinstance(owner, str):
                continue
            if owner != owner.strip() and owner.strip() == account_id:
                raise SchwabDerivedPositionError(
                    "a transaction account id is not canonical"
                )
            if owner != account_id:
                continue
            for field in _TRANSACTION_DATE_FIELDS:
                if transaction.get(field) in {None, ""}:
                    if field == "event_date":
                        raise SchwabDerivedPositionError(
                            "a transaction of the account has no event date"
                        )
                    continue
                dated = _day(transaction[field], f"transaction {field}")
                if valuation_cutoff < dated:
                    raise SchwabDerivedPositionError(
                        "the account has transactions after the cutoff: "
                        "the snapshot is not the cutoff"
                    )
        for observation in nav_observations:
            stated = observation.get("as_of_date")
            if (
                observation.get("account_id") == account_id
                and isinstance(stated, str)
                and stated != stated.strip()
                and stated.strip() == valuation_cutoff.isoformat()
            ):
                raise SchwabDerivedPositionError(
                    "a NAV observation date is not canonical"
                )
        navs = [
            observation
            for observation in nav_observations
            if observation.get("account_id") == account_id
            and observation.get("as_of_date") == valuation_cutoff.isoformat()
        ]
        if len(navs) != 1:
            raise SchwabDerivedPositionError(
                "the account must have exactly one NAV observation on the cutoff day"
            )
        nav = navs[0].get("nav")
        if (
            not isinstance(nav, Mapping)
            or nav.get("status") != "AVAILABLE"
            or _decimal_text(nav.get("value"), "NAV value") != total
        ):
            raise SchwabDerivedPositionError(
                "the account value observed at the cutoff differs from the positions total"
            )
        days = closure["earlier_market_value_days"]
        if not isinstance(days, list):
            raise SchwabDerivedPositionError("closure earlier days are invalid")
        observed = [_day(item, "closure earlier day") for item in days]
        if through < capture_date and not observed:
            raise SchwabDerivedPositionError(
                "no independent observation covers the days after the history"
            )
        if any(not through < day < capture_date for day in observed):
            raise SchwabDerivedPositionError(
                "an earlier observation day lies outside the history-to-capture window"
            )
        # The pages of one account sit in folders named for it: the export's folder
        # carries the account tag and the capture day, an earlier page's folder the same
        # tag and its own day.  Another account's page, or the capture's own, does not tie.
        folder = _FOLDER.search(group[0]["source_artifact_id"])
        if folder is None or folder.group(2) != capture_date.isoformat():
            raise SchwabDerivedPositionError(
                "the positions export folder does not name its account tag and capture day"
            )
        tag = re.escape(folder.group(1))
        for day in observed:
            pattern = re.compile(rf"(?:.*/)?schwab-{tag}-{day.isoformat()}/[^/]+")
            if not any(
                schema == _BALANCES_SCHEMA and pattern.fullmatch(artifact_id)
                for artifact_id, schema in artifact_schemas.items()
            ):
                raise SchwabDerivedPositionError(
                    f"no catalog balances page of this account carries the earlier day {day.isoformat()}"
                )
    return admitted
