"""The Schwab roll-back reproduces earlier retained position snapshots from later ones.

Each later csv export (positions and the cash row) is rolled back, with the retained
generation's own transaction rows, to the market date of an earlier export, and the result is
compared with that earlier export, per brokerage account: exact Decimal quantities (symbols
joined by ``symbol_key``) and cash within half a cent. The exports and the generation are
independent captures, so agreement is evidence, not a tautology. Account sections and
transaction accounts are joined at run time on the last three digits of the account number
shown in the section header; nothing account specific is written here.

Known exceptions: none for the pairs below. (The 2026-07-31 and 2026-08-14 exports are
intraday and are deliberately not used: a sweep-fund reinvestment posted after the 07-31
capture breaks them, which is a capture-timing fact, not a roll-back defect.)
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from schwab_rollback import RollbackError, roll_back, symbol_key


RETAINED = Path(
    "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataSchwab"
)
RUNS = RETAINED / "source-runs"
OFFICIAL = RUNS / "r21-schwab-0917-official/current-official"
EXPORTS = RUNS / "legacy-20260731/Strategies_dataSchwab"
SNAPSHOT_FILES = {
    "0119": EXPORTS / "SchwabExport_20260119/-Positions-2026-01-19-154611.csv",
    "0412": EXPORTS
    / "SchwabExport_20260412/All-Accounts-Positions-2026-04-12-193025.csv",
    "0717": EXPORTS
    / "SchwabExport_20260717/All-Accounts-Positions-2026-07-17-160221.csv",
    "0904": RUNS
    / "refresh-20260903-supplement/manual_verification"
    / "All-Accounts-Positions-2026-09-04-000241.csv",
}
# Market date each export prices (the 01-19 holiday and 04-12 Sunday exports are the prior
# close; the 09-04 00:02 export is the state after the 09-03 close).
MARKET_DATE = {
    "0119": date(2026, 1, 16),
    "0412": date(2026, 4, 10),
    "0717": date(2026, 7, 17),
    "0904": date(2026, 9, 3),
}
PAIRS = [
    ("0412", "0119"),
    ("0717", "0412"),
    ("0904", "0717"),
    ("0904", "0119"),
    ("0717", "0119"),
]
CASH_ROW = "Cash & Cash Investments"
ZERO = Decimal(0)

# Explicit, named exceptions: (later, earlier, account index, symbol key) -> reason. Empty:
# every compared position of every pair closes exactly.
KNOWN_EXCEPTIONS: dict[tuple[str, str, int, str], str] = {}

GENERATION_DIRS = sorted((RETAINED / "generations").glob("f81b*"))
GENERATION = GENERATION_DIRS[0] / "schwab-generation.json" if GENERATION_DIRS else None

if (
    GENERATION is None
    or not GENERATION.is_file()
    or not OFFICIAL.is_dir()
    or not all(path.is_file() for path in SNAPSHOT_FILES.values())
):
    pytest.skip("retained Schwab source roots are not mounted", allow_module_level=True)


def _decimal(text: str) -> Decimal | None:
    raw = text.strip().replace("$", "").replace(",", "")
    negative = raw.startswith("(") or raw.startswith("-")
    raw = raw.strip("()-")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return -value if negative else value


def _parse_snapshot(path: Path) -> dict[str, tuple[dict[str, Decimal], Decimal]]:
    """Per section tag (last three digits of the header): (quantities, cash)."""

    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    sections: dict[str, list[dict[str, str]]] = {}
    current: str | None = None
    header: list[str] | None = None
    for index, row in enumerate(rows):
        if index == 0 or not row or not any(row):
            continue
        if len(row) == 1 or not any(row[1:]):
            tag = re.search(r"\.\.\.(\d{3})\b", row[0])
            if tag:
                current = tag.group(1)
                sections.setdefault(current, [])
            continue
        if row[0] == "Symbol":
            header = row
            continue
        if current is not None and header is not None:
            sections[current].append(dict(zip(header, row, strict=False)))
    assert header is not None
    market_value = next(name for name in header if name.startswith("Mkt Val"))
    quantity_column = header[2]
    parsed: dict[str, tuple[dict[str, Decimal], Decimal]] = {}
    for tag, section in sections.items():
        quantities: dict[str, Decimal] = {}
        cash: Decimal | None = None
        for row in section:
            symbol = row.get("Symbol", "")
            if symbol == CASH_ROW:
                cash = _decimal(row.get(market_value, ""))
                continue
            if symbol in ("Positions Total", "Account Total"):
                continue
            quantity = _decimal(row.get(quantity_column, ""))
            if quantity is not None:
                key = symbol_key(symbol)
                quantities[key] = quantities.get(key, ZERO) + quantity
        assert cash is not None, "a snapshot section has no cash row"
        parsed[tag] = ({s: q for s, q in quantities.items() if q != 0}, cash)
    return parsed


def _account_tags() -> dict[str, str]:
    """Generation account key -> last three digits of the account number, from the retained
    per-account NAV exports (each file is named by the generation account key)."""

    tags: dict[str, str] = {}
    for path in sorted(OFFICIAL.glob("account-*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            first = next(csv.DictReader(handle))
        tags[path.stem] = str(first["account_id"])[-3:]
    return tags


def _generation_rows() -> dict[str, list[dict[str, Any]]]:
    """Generation transactions in the retained raw-row shape, per account tag."""

    assert GENERATION is not None
    catalog = json.loads(GENERATION.read_text(encoding="utf-8"))
    tags = _account_tags()
    per_account: dict[str, list[dict[str, Any]]] = {}
    for item in catalog["transactions"]:
        amount = item["amount"]
        value = amount.get("value") if isinstance(amount, dict) else amount
        row = {
            "action": item["kind"],
            "transactionDate": item["event_date"],
            "effectiveDate": item["effective_date"],
            "shareQuantity": item["quantity"],
            "amount": "" if value in (None, "") else str(value),
            "symbol": item["symbol"],
        }
        per_account.setdefault(tags[item["account_id"]], []).append(row)
    return per_account


Snapshots = dict[str, dict[str, tuple[dict[str, Decimal], Decimal]]]


@pytest.fixture(scope="module")
def snapshots() -> Snapshots:
    return {name: _parse_snapshot(path) for name, path in SNAPSHOT_FILES.items()}


@pytest.fixture(scope="module")
def transactions() -> dict[str, list[dict[str, Any]]]:
    return _generation_rows()


def _in_window(rows: list[dict[str, Any]], newest: date) -> list[dict[str, Any]]:
    return [r for r in rows if date.fromisoformat(r["transactionDate"]) <= newest]


def test_accounts_join_one_to_one(
    snapshots: Snapshots, transactions: dict[str, list[dict[str, Any]]]
) -> None:
    assert len(transactions) == 8, "the generation carries eight brokerage accounts"
    assert len(set(_account_tags().values())) == len(_account_tags()), "tags are unique"
    for name, sections in snapshots.items():
        assert set(transactions) <= set(sections), (
            f"{name}: a transaction account has no section"
        )


@pytest.mark.parametrize(("later", "earlier"), PAIRS)
def test_roll_back_reproduces_earlier_snapshot(
    later: str,
    earlier: str,
    snapshots: Snapshots,
    transactions: dict[str, list[dict[str, Any]]],
) -> None:
    day = MARKET_DATE[earlier]
    newest = MARKET_DATE[later]
    problems: list[str] = []
    compared_positions = 0
    for index, tag in enumerate(sorted(transactions)):
        quantities, cash = snapshots[later][tag]
        expected_quantities, expected_cash = snapshots[earlier][tag]
        rolled = roll_back(
            f"account {index}",
            _in_window(transactions[tag], newest),
            quantities,
            cash,
            day,
        )
        got = {symbol_key(s): q for s, q in rolled.quantities.items()}
        for symbol in sorted(set(got) | set(expected_quantities)):
            compared_positions += 1
            if (later, earlier, index, symbol) in KNOWN_EXCEPTIONS:
                continue
            if got.get(symbol, ZERO) != expected_quantities.get(symbol, ZERO):
                problems.append(f"account {index} {symbol}: quantity differs")
        if abs(rolled.cash - expected_cash) >= Decimal("0.005"):
            problems.append(f"account {index}: cash differs")
    assert compared_positions > 0
    assert not problems, f"{later} -> {earlier}: " + "; ".join(problems)


@pytest.mark.parametrize(
    ("symbol", "action", "day"),
    [
        ("XLU", "Stock Split", date(2025, 11, 30)),
        ("VSNT", "Spin-off", date(2025, 12, 31)),
    ],
)
def test_roll_back_across_a_corporate_action_is_refused(
    symbol: str,
    action: str,
    day: date,
    snapshots: Snapshots,
    transactions: dict[str, list[dict[str, Any]]],
) -> None:
    tags = [
        tag
        for tag, rows in transactions.items()
        if any(r["action"] == action and r["symbol"] == symbol for r in rows)
    ]
    if not tags:
        pytest.skip(f"the retained {action} rows for {symbol} are not present")
    for tag in tags:
        quantities, cash = snapshots["0119"][tag]
        with pytest.raises(RollbackError, match=action):
            roll_back(
                "account",
                _in_window(transactions[tag], MARKET_DATE["0119"]),
                quantities,
                cash,
                day,
            )
