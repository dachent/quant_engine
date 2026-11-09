"""Per-book identity check of a Portfolio123 export set.

A Portfolio123 transaction, performance or holdings export carries no book id: the id is
only the filename. The 2026-09-02 download set held other strategies' rows under seven of
eight ids, so every pull is checked here, book by book, before a generation may use it.

Field definitions (one set per book):

* page record -- the row of ``raw/p123_investment_resource.json`` whose ``assetUid`` is the
  book id: ``assetName``, ``assetType``, ``inceptionDt`` (the page's inception, which for a
  live book is when it went live and is LATER than its strategy's own history),
  ``posCnt``.  Absent for books that are not connected to the account (a shared simulation).
* strategy start string -- ``Start MM/DD/YYYY`` in the page name, when present.
* series start -- date and amount of the first ``CASH`` row of the transactions export.
* perf start -- first date and first ``Total Equity`` of the perf export.

Checks (``I1`` .. ``I9``); a book is PASS (all pass), REVIEW (a warning) or FAIL:

I1 every export is listed in ``capture-manifest.json`` with the same sha256 and length.
I2 transactions and perf exist; holdings exist (a missing holdings file is a warning).
I3 perf start date equals series start date, and perf start equity equals the cash amount.
I4 the last transaction date is not after the last perf date.
I5 the net shares per symbol over all transaction rows (BUY/SELL/SHORT/COVER/SPLIT, signed
   as exported) equal the holdings export exactly: the holdings are reconstructed from the
   transactions, so the two files describe one book.
I6 the last perf ``# Positions`` equals the holdings row count (warning on a difference).
I7 against the page record: series start is not after the page inception; the series start
   is not after, and within 7 days of, the strategy start string; the holdings count is
   within 5 of ``posCnt`` (warning).  A live book whose transactions precede its inception
   is correct and passes.
I8 no two books share trade rows (CASH rows excluded): at least 50% of the smaller set fails
   (a duplicate or permutation), more than 5% is a warning with the counts recorded.
I9 against a reference pull (``--reference-index``): every older row of the same id is in
   the new file with its multiplicity (a row held twice in the reference must be held twice;
   rows are compared on every field except the symbol, which the export retags, and the
   number of rows under another symbol is reported), zero missing unless ``--reference-tolerance`` allows a fraction (recorded in the decisions
   file), and the cash start is equal; ``--require-reference`` makes a missing reference a
   failure.
I8x ``--cross-check DIR`` (other capture directories of the same pull) fails, and
   ``--compare-prior DIR`` (an older or HAR download set) warns, when the new book's trade
   rows appear under ANOTHER id there: the permutation the 2026-09-02 set carried.

Dispositions. PASS: proceed. REVIEW: a warning only the capture operator may accept, in
``portfolio123_generation.py plan --accept-identity-review ID=REASON`` (the reason states
what was seen); it is recorded in the catalog. FAIL: stop before stage; re-pull the book or
exclude its id with ``--untrusted-har-id`` (control decides). ``plan`` is the operator
preflight: it refuses a missing decisions file, a decision for other capture bytes, any
non-PASS decision without an accepted reason, and any HAR download id that is neither
recaptured nor excluded.

``book-identity-decisions-<manifest sha prefix>.json`` (create-only, in ``--out``; a re-run on the same bytes reproduces it) records every field, the row-set
digests, the net-position reconstruction counts and each decision, for the release record.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from quant_engine.core import InterpreterRefusal, require_current_interpreter
from quant_engine.portfolio123 import book_rowset_fingerprint, previous_generation_books

_START_STRING = re.compile(r"Start\s+(\d{1,2})/(\d{1,2})/(\d{4})")
_NON_SHARE_TYPES = {"CASH", "DIV", "INT", "FEE"}
_SHARES_TOLERANCE = 0.5
_OVERLAP_FAIL = 0.05
_OVERLAP_DUPLICATE = 0.5
_REFERENCE_MISSING = 0.0
_PAGE_POSITIONS_TOLERANCE = 5
_START_WINDOW_DAYS = 7
_START_GAP_DAYS = 7


def _number(value: str | None) -> float | None:
    text = (value or "").replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip() or text.strip() == "no rows":
        return []
    return [row for row in csv.DictReader(text.splitlines()) if any(row.values())]


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _page_day(value: Any) -> date | None:
    """Parse a page date: ISO, or the provider's US MM/DD/YYYY."""
    text = str(value or "").strip()
    parsed = _day(text)
    if parsed is not None:
        return parsed
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def _check(identifier: str, status: str, detail: str) -> dict[str, str]:
    return {"check": identifier, "detail": detail, "status": status}


def _trade_keys(rows: Sequence[dict[str, str]]) -> set[str]:
    return {
        json.dumps(
            {k: v for k, v in row.items() if v != ""},
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
        if row.get("Type") != "CASH"
    }


def _manifest(capture: Path) -> dict[str, tuple[str, int]]:
    record = json.loads((capture / "capture-manifest.json").read_text(encoding="utf-8"))
    return {
        item["relative_path"]: (item["sha256"], int(item["observed_length"]))
        for item in record["artifacts"]
    }


def _page_records(raw: Path) -> dict[str, dict[str, Any]]:
    path = raw / "p123_investment_resource.json"
    if not path.is_file():
        return {}
    records = json.loads(path.read_text(encoding="utf-8"))
    return {str(item["assetUid"]): item for item in records if "assetUid" in item}


def _cash_start(rows: Sequence[dict[str, str]]) -> tuple[str, float | None] | None:
    cash = next((r for r in rows if r.get("Type") == "CASH"), None)
    return None if cash is None else (cash["Date"], _number(cash.get("Amount")))


def _trade_counts(
    rows: Sequence[dict[str, str]], *, symbol: bool = True
) -> Counter[str]:
    """Trade-row keys with multiplicity (CASH rows excluded), the symbol optional."""
    return Counter(
        json.dumps(
            {k: v for k, v in row.items() if v != "" and (symbol or k != "Symbol")},
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
        if row.get("Type") != "CASH"
    )


def _reference(
    index: Path | None, book: str
) -> tuple[list[dict[str, str]], tuple[str, float | None] | None] | None:
    """Trade rows and cash start of the same id in an earlier pull (a ``source-index.json`` run)."""

    if index is None:
        return None
    run = index.parent
    for entry in json.loads(index.read_text(encoding="utf-8"))["entries"]:
        if entry["relative_path"] != f"downloads/strategy_{book}_transactions.csv":
            continue
        path = run / "objects" / entry["sha256"][:2] / entry["sha256"]
        rows = _rows(path)
        return rows, _cash_start(rows)
    return None


def check_book(
    book: str,
    raw: Path,
    *,
    listed: dict[str, tuple[str, int]] | None,
    page: dict[str, Any] | None,
    reference_index: Path | None,
    trade_keys: set[str],
    require_reference: bool = False,
    tolerance: float = _REFERENCE_MISSING,
    as_of: date | None = None,
    capture_reach: str | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    names = {
        kind: raw / f"strategy_{book}_{kind}.csv"
        for kind in ("transactions", "perf", "holdings")
    }
    if listed is not None:
        for kind, path in names.items():
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rel = f"raw/{path.name}"
            if listed.get(rel) != (digest, path.stat().st_size):
                checks.append(
                    _check("I1", "FAIL", f"{path.name} is not bound by the manifest")
                )
        if not any(c["check"] == "I1" for c in checks):
            checks.append(_check("I1", "PASS", "exports bound by the capture manifest"))
    else:
        checks.append(
            _check("I1", "WARN", "no capture manifest: sources not hash-bound")
        )
    missing = [kind for kind in ("transactions", "perf") if not names[kind].is_file()]
    if missing:
        checks.append(_check("I2", "FAIL", "missing: " + ", ".join(missing)))
        return {"book": book, "checks": checks, "decision": "FAIL"}
    checks.append(
        _check(
            "I2",
            "PASS" if names["holdings"].is_file() else "WARN",
            "transactions and perf present"
            + ("" if names["holdings"].is_file() else "; holdings missing"),
        )
    )
    try:
        tx_print = book_rowset_fingerprint(names["transactions"], book, "transactions")
        perf_print = book_rowset_fingerprint(names["perf"], book, "equity_curve")
    except ValueError as error:
        checks.append(_check("I3", "FAIL", f"unreadable export: {error}"))
        return {"book": book, "checks": checks, "decision": "FAIL"}
    tx = _rows(names["transactions"])
    perf = _rows(names["perf"])
    holdings = _rows(names["holdings"]) if names["holdings"].is_file() else []
    cash = next((r for r in tx if r.get("Type") == "CASH"), None)
    cash_day = _day(cash["Date"]) if cash else None
    cash_amount = _number(cash.get("Amount")) if cash else None
    perf_day = _day(perf[0]["Date"]) if perf else None
    perf_equity = _number(perf[0].get("Total Equity")) if perf else None
    # I3
    i3_gap: int | None = None
    first_row = min((d for d in (_day(r.get("Date")) for r in tx) if d), default=None)
    if cash is None or not perf:
        checks.append(_check("I3", "FAIL", "no CASH start row or no perf rows"))
    elif (
        perf_day != cash_day
        and any(r.get("Type") != "CASH" for r in tx)
        and not _start_gap_ok(perf_day, cash_day, first_row, perf_equity, cash_amount)
    ) or (
        cash_amount is not None
        and perf_equity is not None
        and abs(perf_equity - cash_amount) > 0.01
    ):
        checks.append(
            _check(
                "I3",
                "FAIL",
                f"perf starts {perf_day} with equity {perf_equity}; "
                f"transactions start {cash_day} with cash {cash_amount}; "
                f"earliest transaction row {first_row}",
            )
        )
    elif perf_day != cash_day and any(r.get("Type") != "CASH" for r in tx):
        # The perf curve starts on the last trading day before the first transaction row
        # (a year-end boundary: Thu 1998-12-31 curve start, first row Sat 1999-01-02), with
        # the identical start amount.
        i3_gap = (cash_day - perf_day).days  # type: ignore[operator]
        checks.append(
            _check(
                "I3",
                "PASS",
                f"perf starts {perf_day}, transactions start {cash_day} ({i3_gap} days "
                f"later, within {_START_GAP_DAYS}); equal start amount {cash_amount}",
            )
        )
    elif perf_day != cash_day:
        checks.append(
            _check(
                "I3",
                "WARN",
                f"cash-only book: perf starts {perf_day}, cash row dated {cash_day}, "
                "equity equals the cash amount",
            )
        )
    else:
        checks.append(_check("I3", "PASS", f"series and perf both start {cash_day}"))
    # I4
    last_tx = tx_print["last_date"]
    last_perf = perf_print["last_date"]
    if last_tx and last_perf and last_tx > last_perf:
        checks.append(
            _check(
                "I4", "FAIL", f"last transaction {last_tx} after last perf {last_perf}"
            )
        )
    else:
        checks.append(
            _check("I4", "PASS", f"last transaction {last_tx}, last perf {last_perf}")
        )
    # I5
    net: dict[str, float] = defaultdict(float)
    for row in tx:
        shares = _number(row.get("Shares"))
        if (
            row.get("Type") in _NON_SHARE_TYPES
            or shares is None
            or not row.get("Symbol")
        ):
            continue
        net[row["Symbol"]] += shares
    open_net = {s: v for s, v in net.items() if abs(v) > _SHARES_TOLERANCE}
    held = {r["Ticker"]: _number(r.get("Shares")) or 0.0 for r in holdings}
    differs = sorted(
        s
        for s in set(open_net) | set(held)
        if abs(open_net.get(s, 0.0) - held.get(s, 0.0)) > _SHARES_TOLERANCE
    )
    recon = {
        "holdings_rows": len(held),
        "mismatched_symbols": len(differs),
        "net_open_symbols": len(open_net),
    }
    if names["holdings"].is_file():
        checks.append(
            _check(
                "I5",
                "PASS" if not differs else "FAIL",
                f"net shares vs holdings: {recon['mismatched_symbols']} of "
                f"{len(set(open_net) | set(held))} symbols differ",
            )
        )
    else:
        checks.append(_check("I5", "WARN", "no holdings export to reconstruct against"))
    # I5D
    positions_as_of: dict[str, Any] | None = None
    if as_of is not None:
        positions_as_of, status, detail = _positions_as_of(
            tx,
            as_of,
            holdings_symbols=len(held) if names["holdings"].is_file() else None,
            mismatched=len(differs),
            terminal=perf_print["last_date"],
            cash_only=_cash_only(tx, perf, len(holdings)),
            capture_reach=capture_reach,
        )
        checks.append(_check("I5D", status, detail))
    # I6
    positions = _number(perf[-1].get("# Positions")) if perf else None
    if names["holdings"].is_file() and positions is not None:
        checks.append(
            _check(
                "I6",
                "PASS" if int(positions) == len(held) else "WARN",
                f"perf positions {int(positions)}, holdings rows {len(held)}",
            )
        )
    # I7
    page_facts: dict[str, Any] = {}
    if page is None:
        checks.append(_check("I7", "WARN", "no page record for this book id"))
    else:
        inception = _page_day(page.get("inceptionDt"))
        start = _START_STRING.search(str(page.get("assetName") or ""))
        start_day = date(int(start[3]), int(start[1]), int(start[2])) if start else None
        page_facts = {
            "assetName": page.get("assetName"),
            "assetType": page.get("assetType"),
            "inceptionDt": page.get("inceptionDt"),
            "posCnt": page.get("posCnt"),
            "start_string": start_day.isoformat() if start_day else None,
        }
        problems: list[str] = []
        warns: list[str] = []
        if inception is None:
            warns.append(
                f"page inceptionDt {page.get('inceptionDt')!r} is missing or unparseable;"
                " the inception check was not run"
            )
        if cash_day and inception and cash_day > inception:
            problems.append(
                f"series starts {cash_day} after page inception {inception}"
            )
        if (
            cash_day
            and start_day
            and not (
                start_day - timedelta(days=_START_WINDOW_DAYS) <= cash_day <= start_day
            )
        ):
            problems.append(f"series starts {cash_day}, name says Start {start_day}")
        pos = page.get("posCnt")
        if isinstance(pos, int) and names["holdings"].is_file():
            if abs(len(held) - pos) > _PAGE_POSITIONS_TOLERANCE:
                warns.append(f"holdings rows {len(held)} vs page posCnt {pos}")
        checks.append(
            _check(
                "I7",
                "FAIL" if problems else ("WARN" if warns else "PASS"),
                "; ".join(problems or warns) or "consistent with the page record",
            )
        )
    # I8 is decided across books by the caller; the keys are returned for it.
    # I9
    reference = _reference(reference_index, book)
    if reference_index is None and require_reference:
        checks.append(
            _check("I9", "FAIL", "a reference pull is required and none was given")
        )
    if reference_index is not None:
        if reference is None:
            checks.append(
                _check(
                    "I9",
                    "FAIL" if require_reference else "WARN",
                    "book absent from the reference pull",
                )
            )
        else:
            reference_rows, reference_cash = reference
            # A multiset comparison on every field except the symbol: a row held twice in
            # the reference must be held twice, and a ticker rename or ``^yy`` retag between
            # the two exports (set at export time) does not make a row absent.
            reference_counts = _trade_counts(reference_rows, symbol=False)
            missing_rows = sum(
                (reference_counts - _trade_counts(tx, symbol=False)).values()
            )
            renamed = sum((_trade_counts(reference_rows) - _trade_counts(tx)).values())
            total = sum(reference_counts.values())
            share = missing_rows / total if total else 0.0
            same_start = reference_cash is None or reference_cash == _cash_start(tx)
            checks.append(
                _check(
                    "I9",
                    "PASS" if share <= tolerance and same_start else "FAIL",
                    f"{missing_rows} of {total} reference trade rows absent "
                    f"({share:.2%}, tolerance {tolerance:.2%}; {renamed} rows carry "
                    f"another symbol); cash start "
                    + ("equals" if same_start else "differs from")
                    + " the reference",
                )
            )
    return {
        "book": book,
        "checks": checks,
        "decision": _decision(checks),
        "fingerprints": {"perf": perf_print, "transactions": tx_print},
        "holdings_reconstruction": recon,
        "page_record": page_facts,
        "positions_as_of": positions_as_of,
        "series_start": {
            "cash_amount": cash_amount,
            "date": cash_day.isoformat() if cash_day else None,
        },
        "i3_start_gap_days": i3_gap,
    }


_SHARE_TYPES = {"BUY", "SELL", "SHORT", "COVER", "SPLIT"}


def _positions_as_of(
    tx: Sequence[dict[str, str]],
    as_of: date,
    *,
    holdings_symbols: int | None,
    mismatched: int,
    terminal: str | None,
    cash_only: dict[str, Any] | None = None,
    capture_reach: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Positions at ``as_of``: the net signed shares of rows dated on or before it.

    The holdings export describes the capture day T; rolling back the rows dated after
    ``as_of`` gives the positions at ``as_of`` exactly when I5 closed at T, because every
    row, a SPLIT included, enters that identity as its exported signed shares.  Refused,
    with the numbers, only when data is missing (no holdings export, an unreadable date,
    an unknown row type among the rolled-back rows), the capture does not reach ``as_of``
    (the last perf date is before it, so the export is not a later state of the book) or the
    closure fails."""
    net: dict[str, float] = defaultdict(float)
    rolled: dict[str, int] = defaultdict(int)
    splits: list[dict[str, str]] = []
    refusals: list[str] = []
    for row in tx:
        kind = row.get("Type") or ""
        day = _day(row.get("Date"))
        if day is None:
            refusals.append(f"unreadable date {row.get('Date')!r}")
            continue
        if day > as_of:
            rolled[kind] += 1
            if kind == "SPLIT":
                splits.append(
                    {
                        "date": day.isoformat(),
                        "shares": row.get("Shares") or "",
                        "symbol": row.get("Symbol") or "",
                    }
                )
            if kind not in _SHARE_TYPES | _NON_SHARE_TYPES:
                refusals.append(f"unknown row type {kind!r} dated {day}")
            continue
        shares = _number(row.get("Shares"))
        if kind in _NON_SHARE_TYPES or shares is None or not row.get("Symbol"):
            continue
        net[row["Symbol"]] += shares
    open_net = {
        s: round(v, 6) for s, v in sorted(net.items()) if abs(v) > _SHARES_TOLERANCE
    }
    facts: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "open_symbols": len(open_net),
        "positions_sha256": hashlib.sha256(
            json.dumps(open_net, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "rolled_back_rows": dict(sorted(rolled.items())),
    }
    facts["splits_after_as_of"] = splits
    facts["capture_last_perf_date"] = terminal
    if cash_only is not None:
        return _cash_only_positions(facts, as_of, cash_only, terminal, capture_reach)
    numbers = f"rolled back {dict(sorted(rolled.items()))}, splits {splits}"
    if terminal is None or terminal < as_of.isoformat():
        return (
            facts,
            "FAIL",
            f"the capture ends {terminal}, before as-of {as_of}: its holdings are not a "
            f"later state of the book; {numbers}",
        )
    if refusals:
        return facts, "FAIL", "; ".join(sorted(set(refusals))[:5]) + f"; {numbers}"
    if holdings_symbols is None:
        return (
            facts,
            "FAIL",
            f"no holdings export at T to close the roll-back; {numbers}",
        )
    if mismatched:
        return (
            facts,
            "FAIL",
            f"the roll-back cannot close: {mismatched} of {holdings_symbols} symbols "
            f"differ at T; {numbers}",
        )
    return (
        facts,
        "PASS",
        f"{sum(rolled.values())} rows after {as_of} rolled back and I5 closed at T over "
        f"{holdings_symbols} symbols ({numbers}); {len(open_net)} open symbols at {as_of}",
    )


def _start_gap_ok(
    perf_day: date | None,
    cash_day: date | None,
    first_row: date | None,
    perf_equity: float | None,
    cash_amount: float | None,
) -> bool:
    """The perf curve starts up to ``_START_GAP_DAYS`` calendar days before the first
    transaction row (never after it: the earliest row of ANY type, not only the CASH row,
    must not precede the curve) and the two start amounts are identical."""
    if perf_day is None or cash_day is None or first_row is None:
        return False
    if first_row < perf_day or first_row != cash_day:
        return False  # the CASH row must itself be the earliest row of any type
    if perf_equity is None or cash_amount is None:
        return False
    return (
        0 < (cash_day - perf_day).days <= _START_GAP_DAYS
        and abs(perf_equity - cash_amount) <= 0.01
    )


def _cash_only(
    tx: Sequence[dict[str, str]], perf: Sequence[dict[str, str]], holdings_rows: int
) -> dict[str, Any] | None:
    """A book with no instruments, defined from its own files: the transactions are exactly
    one CASH row and the holdings export has no rows.  None for any other book."""
    if len(tx) != 1 or tx[0].get("Type") != "CASH" or holdings_rows:
        return None
    equities = [_number(row.get("Total Equity")) for row in perf]
    dates = [row.get("Date") or "" for row in perf]
    return {
        "cash_amount": _number(tx[0].get("Amount")),
        "cash_date": tx[0].get("Date") or "",
        "equities": equities,
        "first_date": min(dates, default=None),
        "last_date": max(dates, default=None),
    }


def _cash_only_positions(
    facts: dict[str, Any],
    as_of: date,
    cash_only: dict[str, Any],
    terminal: str | None,
    capture_reach: str | None,
) -> tuple[dict[str, Any], str, str]:
    """I5D for a cash-only book: closure, not interpolation (PASS when flat); a moving
    curve with no exported instruments is REVIEW; everything else fails.

    With no instruments there is nothing to roll back and nothing accrues, so the book's
    equity can change only through a transaction row.  The transactions export of the same
    pull lists every row to the capture day; it holds the one start CASH row.  That equity
    equals the cash amount through the whole perf history and the capture itself reaches
    ``as_of`` (another book of the pull has a perf curve to or past it), so the equity at
    ``as_of`` is the cash amount.  A cash row after ``as_of``, unreadable equity or a
    capture that does not reach ``as_of`` fails with the numbers."""
    amount = cash_only["cash_amount"]
    equities = [value for value in cash_only["equities"] if value is not None]
    facts["cash_only_flat_through_as_of"] = False
    facts["cash_only"] = {
        "capture_reach": capture_reach,
        "cash_amount": amount,
        "equity_max": max(equities) if equities else None,
        "equity_min": min(equities) if equities else None,
        "first_date": cash_only["first_date"],
        "last_perf_date": cash_only["last_date"],
    }
    numbers = (
        f"cash {amount} dated {cash_only['cash_date']}, perf equity "
        f"{facts['cash_only']['equity_min']}..{facts['cash_only']['equity_max']} "
        f"{cash_only['first_date']}..{cash_only['last_date']}, capture reach {capture_reach}"
    )
    if amount is None or str(cash_only["cash_date"]) > as_of.isoformat():
        return (
            facts,
            "FAIL",
            f"cash-only book: no usable cash row by {as_of}; {numbers}",
        )
    if not equities or len(equities) != len(cash_only["equities"]):
        return facts, "FAIL", f"cash-only book: unreadable perf equity; {numbers}"
    if capture_reach is None or capture_reach < as_of.isoformat():
        return (
            facts,
            "FAIL",
            f"cash-only book: no book of this capture reaches as-of {as_of}; {numbers}",
        )
    if any(abs(value - amount) > 0.01 for value in equities):
        # A simulation book: its curve moves, but no instruments are exported (one CASH row,
        # an empty holdings export), so its positions at as-of are not in the files.  The
        # operator accepts that per book with a reason; nothing reads these positions.
        facts["positions_not_determinable"] = True
        return (
            facts,
            "WARN",
            "no instruments exported; positions at as-of not determinable from the "
            f"files (equity moves, so the book holds something); {numbers}",
        )
    facts["cash_only_flat_through_as_of"] = True
    facts["equity_at_as_of"] = amount
    return (
        facts,
        "PASS",
        f"cash-only book flat through {as_of}: equity {amount}, no instruments; {numbers}",
    )


def _overlap_status(shared: int, smaller: int, duplicate_status: str) -> str | None:
    """Graded I8: a book that shares at least half of the smaller book's trade rows is a
    duplicate or permutation (the 2026-09-02 set shared all 188 rows of two books);
    a small overlap is review (two EU strategy books of the same family shared 9 of 188,
    an AI book and its derivative 51 of 787); at most 5% passes."""
    if not smaller or shared == 0:
        return None
    share = shared / smaller
    if share >= _OVERLAP_DUPLICATE:
        return duplicate_status
    if share > _OVERLAP_FAIL:
        return "WARN"
    return None


def _decision(checks: Sequence[dict[str, str]]) -> str:
    statuses = {c["status"] for c in checks}
    return (
        "FAIL" if "FAIL" in statuses else ("REVIEW" if "WARN" in statuses else "PASS")
    )


def _directory_keys(directory: Path, subdir: str) -> dict[str, set[str]]:
    base = directory / subdir if subdir else directory
    return {
        match.group(1): _trade_keys(_rows(path))
        for path in sorted(base.glob("strategy_*_transactions.csv"))
        if (match := re.match(r"strategy_(\d+)_transactions\.csv$", path.name))
    }


def check_capture(
    capture: Path,
    *,
    books: Sequence[str] | None = None,
    raw_subdir: str = "raw",
    use_manifest: bool = True,
    reference_index: Path | None = None,
    require_reference: bool = False,
    reference_tolerance: float = _REFERENCE_MISSING,
    cross_checks: Sequence[tuple[Path, str]] = (),
    prior_checks: Sequence[tuple[Path, str]] = (),
    as_of: date | None = None,
) -> dict[str, Any]:
    raw = capture / raw_subdir if raw_subdir else capture
    listed = _manifest(capture) if use_manifest else None
    found = sorted(
        {
            m.group(1)
            for path in raw.glob("strategy_*_*.csv")
            if (
                m := re.match(
                    r"strategy_(\d+)_(?:transactions|perf|holdings)\.csv$", path.name
                )
            )
        }
    )
    selected = list(books) if books else found
    reach = max(
        (
            row.get("Date") or ""
            for book in found
            if (raw / f"strategy_{book}_perf.csv").is_file()
            for row in _rows(raw / f"strategy_{book}_perf.csv")
        ),
        default=None,
    )
    pages = _page_records(raw)
    keys = {
        book: _trade_keys(_rows(raw / f"strategy_{book}_transactions.csv"))
        if (raw / f"strategy_{book}_transactions.csv").is_file()
        else set()
        for book in selected
    }
    results = [
        check_book(
            book,
            raw,
            listed=listed,
            page=pages.get(book),
            reference_index=reference_index,
            trade_keys=keys[book],
            require_reference=require_reference,
            tolerance=reference_tolerance,
            as_of=as_of,
            capture_reach=reach or None,
        )
        for book in selected
    ]
    by_book = {result["book"]: result for result in results}
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            smaller = min(len(keys[left]), len(keys[right]))
            shared = len(keys[left] & keys[right])
            status = _overlap_status(shared, smaller, "FAIL")
            if status is not None:
                for book, other in ((left, right), (right, left)):
                    by_book[book]["checks"].append(
                        _check(
                            "I8",
                            status,
                            f"{shared} of {smaller} trade rows ({shared / smaller:.1%} of "
                            f"the smaller book) shared with book {other}",
                        )
                    )
                    by_book[book].setdefault("i8_shared_rows", {})[other] = {
                        "shared": shared,
                        "smaller_book_rows": smaller,
                    }
    for result in results:
        if not any(c["check"] == "I8" for c in result["checks"]):
            result["checks"].append(
                _check("I8", "PASS", "no trade rows shared with another book")
            )
    # Other capture directories fail; an older or HAR download set only warns, because a
    # correct book legitimately appears there under its wrong id (resolved by an exclusion).
    for directory, subdir, status in (
        *((d, sub, "FAIL") for d, sub in cross_checks),
        *((d, sub, "WARN") for d, sub in prior_checks),
    ):
        others = _directory_keys(directory, subdir)
        for book in selected:
            for other, other_keys in sorted(others.items()):
                smaller = min(len(keys[book]), len(other_keys))
                shared = len(keys[book] & other_keys)
                overlap = _overlap_status(shared, smaller, status)
                if status == "FAIL" and overlap == "WARN":
                    overlap = "FAIL"  # same pull: any share above the threshold fails
                if other != book and overlap is not None:
                    by_book[book]["checks"].append(
                        _check(
                            "I8x",
                            overlap,
                            f"{shared} of {smaller} trade rows ({shared / smaller:.1%} of "
                            f"the smaller book) shared with book {other} in "
                            f"{directory.name}",
                        )
                    )
    for result in results:
        result["decision"] = _decision(result["checks"])
    return {
        "settings": {
            "as_of": as_of.isoformat() if as_of else None,
            "reference_tolerance": reference_tolerance,
            "require_reference": require_reference,
        },
        "books": by_book,
        "capture_manifest_sha256": (
            hashlib.sha256((capture / "capture-manifest.json").read_bytes()).hexdigest()
            if use_manifest
            else None
        ),
        "schema": "p123-book-identity-decisions/1",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument(
        "--out", type=Path, required=True, help="directory for the decisions file"
    )
    parser.add_argument(
        "--book", action="append", default=[], help="book id (default: every id found)"
    )
    parser.add_argument("--raw-subdir", default="raw")
    parser.add_argument("--no-manifest", action="store_true")
    parser.add_argument("--reference-index", type=Path)
    parser.add_argument("--require-reference", action="store_true")
    parser.add_argument("--reference-tolerance", type=float, default=_REFERENCE_MISSING)
    parser.add_argument(
        "--cross-check",
        type=Path,
        action="append",
        default=[],
        help="another capture directory of the same pull (a shared trade row under another id fails)",
    )
    parser.add_argument(
        "--compare-prior",
        type=Path,
        action="append",
        default=[],
        help="an older or HAR download set (a shared trade row under another id warns)",
    )
    parser.add_argument("--cross-raw-subdir", default="raw")
    parser.add_argument("--prior-raw-subdir", default="")
    parser.add_argument(
        "--previous-generation-root",
        type=Path,
        help="data root; prints the predecessor fingerprints per book",
    )
    parser.add_argument("--previous-generation-id")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help="cutoff D (YYYY-MM-DD): roll the T holdings back to D from the transactions "
        "and record the positions at D (I5D)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        require_current_interpreter("the P123 book identity check")
    except InterpreterRefusal as exc:
        print(f"p123-book-identity-check: FAIL: {exc}", file=sys.stderr)
        return 2
    report = check_capture(
        args.capture,
        books=args.book or None,
        raw_subdir=args.raw_subdir,
        use_manifest=not args.no_manifest,
        reference_index=args.reference_index,
        require_reference=args.require_reference,
        reference_tolerance=args.reference_tolerance,
        cross_checks=[(path, args.cross_raw_subdir) for path in args.cross_check],
        prior_checks=[(path, args.prior_raw_subdir) for path in args.compare_prior],
        as_of=args.as_of,
    )
    if args.previous_generation_root is not None:
        previous = previous_generation_books(
            args.previous_generation_root, args.previous_generation_id
        )
        report["predecessor"] = previous
    args.out.mkdir(parents=True, exist_ok=True)
    # One file per capture bytes: a re-run on the same bytes must reproduce it exactly.
    tag = str(report["capture_manifest_sha256"] or "no-manifest")[:12]
    if args.as_of is not None:
        tag += f"-asof-{args.as_of.isoformat()}"
    target = args.out / f"book-identity-decisions-{tag}.json"
    content = json.dumps(report, indent=1, sort_keys=True)
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise FileExistsError(f"{target} exists with different decisions")
    if not target.exists():
        with target.open("x", encoding="utf-8") as handle:
            handle.write(content)
    decisions = {book: item["decision"] for book, item in report["books"].items()}
    print(json.dumps(decisions, sort_keys=True))
    return 0 if all(value == "PASS" for value in decisions.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
