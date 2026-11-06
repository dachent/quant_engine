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
I8 no two books share trade rows (CASH rows excluded): more than 5% of the smaller set fails.
I9 against a reference pull (``--reference-index``): every older row of the same id is in
   the new file, up to 1% missing.

``book-identity-decisions.json`` (create-only, in ``--out``) records every field, the row-set
digests, the net-position reconstruction counts and each decision, for the release record.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from quant_engine.portfolio123 import book_rowset_fingerprint, previous_generation_books

_START_STRING = re.compile(r"Start\s+(\d{1,2})/(\d{1,2})/(\d{4})")
_NON_SHARE_TYPES = {"CASH", "DIV", "INT", "FEE"}
_SHARES_TOLERANCE = 0.5
_OVERLAP_FAIL = 0.05
_REFERENCE_MISSING = 0.01
_PAGE_POSITIONS_TOLERANCE = 5
_START_WINDOW_DAYS = 7


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


def _reference(
    index: Path | None, book: str
) -> tuple[set[str], tuple[str, float | None] | None] | None:
    """Trade-row keys and cash start of the same id in an earlier pull (a ``source-index.json`` run)."""

    if index is None:
        return None
    run = index.parent
    for entry in json.loads(index.read_text(encoding="utf-8"))["entries"]:
        if entry["relative_path"] != f"downloads/strategy_{book}_transactions.csv":
            continue
        path = run / "objects" / entry["sha256"][:2] / entry["sha256"]
        rows = _rows(path)
        return _trade_keys(rows), _cash_start(rows)
    return None


def check_book(
    book: str,
    raw: Path,
    *,
    listed: dict[str, tuple[str, int]] | None,
    page: dict[str, Any] | None,
    reference_index: Path | None,
    trade_keys: set[str],
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
    if cash is None or not perf:
        checks.append(_check("I3", "FAIL", "no CASH start row or no perf rows"))
    elif (perf_day != cash_day and any(r.get("Type") != "CASH" for r in tx)) or (
        cash_amount is not None
        and perf_equity is not None
        and abs(perf_equity - cash_amount) > 0.01
    ):
        checks.append(
            _check(
                "I3",
                "FAIL",
                f"perf starts {perf_day} with equity {perf_equity}; "
                f"transactions start {cash_day} with cash {cash_amount}",
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
        inception = _day(str(page.get("inceptionDt")))
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
    if reference_index is not None:
        if reference is None:
            checks.append(_check("I9", "WARN", "book absent from the reference pull"))
        else:
            reference_keys, reference_cash = reference
            missing_rows = len(reference_keys - trade_keys)
            share = missing_rows / len(reference_keys) if reference_keys else 0.0
            same_start = reference_cash is None or reference_cash == _cash_start(tx)
            checks.append(
                _check(
                    "I9",
                    "PASS" if share <= _REFERENCE_MISSING and same_start else "FAIL",
                    f"{missing_rows} of {len(reference_keys)} reference trade rows absent "
                    f"({share:.2%}); cash start "
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
        "series_start": {
            "cash_amount": cash_amount,
            "date": cash_day.isoformat() if cash_day else None,
        },
    }


def _decision(checks: Sequence[dict[str, str]]) -> str:
    statuses = {c["status"] for c in checks}
    return (
        "FAIL" if "FAIL" in statuses else ("REVIEW" if "WARN" in statuses else "PASS")
    )


def check_capture(
    capture: Path,
    *,
    books: Sequence[str] | None = None,
    raw_subdir: str = "raw",
    use_manifest: bool = True,
    reference_index: Path | None = None,
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
        )
        for book in selected
    ]
    by_book = {result["book"]: result for result in results}
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            smaller = min(len(keys[left]), len(keys[right]))
            shared = len(keys[left] & keys[right])
            if smaller and shared / smaller > _OVERLAP_FAIL:
                for book, other in ((left, right), (right, left)):
                    by_book[book]["checks"].append(
                        _check(
                            "I8",
                            "FAIL",
                            f"{shared} trade rows shared with book {other}",
                        )
                    )
                    by_book[book]["decision"] = "FAIL"
    for result in results:
        if not any(c["check"] == "I8" for c in result["checks"]):
            result["checks"].append(
                _check("I8", "PASS", "no trade rows shared with another book")
            )
    return {
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
    parser.add_argument(
        "--previous-generation-root",
        type=Path,
        help="data root; prints the predecessor fingerprints per book",
    )
    parser.add_argument("--previous-generation-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = check_capture(
        args.capture,
        books=args.book or None,
        raw_subdir=args.raw_subdir,
        use_manifest=not args.no_manifest,
        reference_index=args.reference_index,
    )
    if args.previous_generation_root is not None:
        previous = previous_generation_books(
            args.previous_generation_root, args.previous_generation_id
        )
        report["predecessor"] = previous
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / "book-identity-decisions.json"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
    decisions = {book: item["decision"] for book, item in report["books"].items()}
    print(json.dumps(decisions, sort_keys=True))
    return 0 if all(value == "PASS" for value in decisions.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
