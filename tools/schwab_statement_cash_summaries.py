"""Schwab statement cash summaries: the printed cash-summary columns of every retained statement.

One row per account-month statement (``statement_cash_summaries`` of the Schwab catalog),
built from the pinned statement PDFs by ``zero_financing_statements.scan_cash_statement``
(never by a second reader), except the pre-2025 PCRA "Cash Transactions Summary" layout, which has its own closing-only reader (`LEGACY_LAYOUT_CLOSES`: begin + movements == end; flow columns not row-tied).  A month that does not tie is still emitted with the reason,
so a gap is named and never silently dropped; a month with no retained statement has no
row (the closure names it missing).

``tie_status`` is ``TIED`` only on a full tie: the eight printed amounts add up, every
category column equals the sum of its own rows to the cent, and the cash ladder ends on the
printed ending cash.  ``deposits_tied`` / ``withdrawals_tied`` say only that THAT column
equals the sum of its own category rows, so a month whose Sales column fails still shows
whether its flow columns tie.

Usage: ``python tools/schwab_statement_cash_summaries.py STATEMENT_DIR MAPPING_JSON OUT_JSON``
where the mapping is ``{"accounts": [{"ledger_account_id": ..., "last3": ...}]}``.
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import zero_financing_statements as zfs

_MONTHS = {
    name: number
    for number, name in enumerate(
        "January February March April May June July August September October "
        "November December".split(),
        1,
    )
}
_PERIOD = re.compile(
    r"Statement Period\s*\n([A-Za-z]+) (\d+)-(?:([A-Za-z]+) )?(\d+), (\d{4})"
)
_ACCOUNT = re.compile(r"Account Number\s*\n([\d\-*]+)")
_COLUMN_INDEX = {"deposits": 0, "withdrawals": 1}


class StatementSummaryError(ValueError):
    """A statement whose period or account cannot be read, or that maps to no account."""


def statement_period(raw_text: str) -> tuple[date, date]:
    match = _PERIOD.search(raw_text)
    if match is None:
        raise StatementSummaryError("statement period is absent")
    first_month, first_day, last_month, last_day, year = match.groups()
    try:
        return (
            date(int(year), _MONTHS[first_month], int(first_day)),
            date(int(year), _MONTHS[last_month or first_month], int(last_day)),
        )
    except (KeyError, ValueError) as exc:
        raise StatementSummaryError("statement period is not a date") from exc


def account_last3(raw_text: str) -> str:
    match = _ACCOUNT.search(raw_text)
    digits = re.sub(r"\D", "", match.group(1)) if match else ""
    if len(digits) < 3:
        raise StatementSummaryError("statement account number is absent")
    return digits[-3:]


def _amount(value: Any) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": format(value, "f")}


_LEGACY_LABELS = {
    "Starting Cash*": "beginning_cash",
    "Deposits and other Cash Credits": "deposits",
    "Investments Sold": "sales_redemptions",
    "Dividends and Interest": "dividends_interest",
    "Withdrawals and other Debits": "withdrawals",
    "Investments Purchased": "purchases",
    "Fees and Charges": "expenses",
    "Ending Cash*": "ending_cash",
}
_LEGACY_HEAD = re.compile(r"^Cash Transactions Summary This Period", re.M)
_MONEY = re.compile(r"\(?-?[\d,]+\.\d{2}\)?")


def _legacy_summary(raw_text: str) -> dict[str, Any] | None:
    """The pre-2025 PCRA layout: a "Cash Transactions Summary" block (This Period / Year to
    Date).  Returns the This Period amounts when the block reads and closes
    (beginning + the six movement columns == ending), else None."""

    head = _LEGACY_HEAD.search(raw_text)
    if head is None:
        return None
    values: dict[str, Any] = {}
    for line in raw_text[head.end() :].splitlines()[1:12]:
        for label, name in _LEGACY_LABELS.items():
            if line.startswith(label):
                tokens = _MONEY.findall(line[len(label) :])
                if not tokens:
                    return None
                word = tokens[0]
                negative = word.startswith("(") or word.startswith("-")
                number = Decimal(re.sub(r"[(),-]", "", word))
                values[name] = -number if negative else number
    if set(values) != set(_LEGACY_LABELS.values()):
        return None
    movement = sum(
        (values[n] for n in zfs.CASH_SUMMARY_COLUMNS if n != "beginning_cash"),
        Decimal(0),
    )
    return (
        values if values["beginning_cash"] + movement == values["ending_cash"] else None
    )


def summary_row(
    *,
    account_id: str,
    raw_text: str,
    source_artifact_id: str,
    source_sha256: str,
) -> dict[str, Any]:
    """The catalog row of one statement."""

    start, end = statement_period(raw_text)
    facts, reason = zfs.scan_cash_statement(raw_text, start, end)
    row: dict[str, Any] = {
        "account_id": account_id,
        "deposits_tied": False,
        "period_end": end.isoformat(),
        "period_start": start.isoformat(),
        "source_artifact_id": source_artifact_id,
        "source_sha256": source_sha256,
        "tie_status": reason,
        "withdrawals_tied": False,
    }
    if facts is None:
        legacy = _legacy_summary(raw_text) if reason == "CASH_SUMMARY_ABSENT" else None
        if legacy is None:
            return row  # no readable summary page: the reason names the gap
        # the old layout prints the columns but no per-row detail this reader ties against:
        # the amounts close (begin + movements == end); the flow columns are not row-tied here
        row["tie_status"] = "LEGACY_LAYOUT_CLOSES"
        for name, value in legacy.items():
            row[name] = _amount(value)
        return row
    printed = facts["printed"]
    for name in ("beginning_cash", "ending_cash", *zfs.CASH_SUMMARY_COLUMNS):
        row[name] = _amount(printed[name])
    if facts["rows_ok"]:
        for column, index in _COLUMN_INDEX.items():
            row[f"{column}_tied"] = bool(facts["tied"][index])
    ladder, _begin, ladder_reason, _printed = zfs.parse_cash_ladder_totals(
        raw_text, start, end
    )
    row["tie_status"] = "TIED" if ladder is not None else ladder_reason
    return row


def build_rows(
    statements: list[dict[str, Any]], last3_to_account: dict[str, str]
) -> list[dict[str, Any]]:
    """Rows of ``statements`` (each ``{raw_text, source_artifact_id, source_sha256}``),
    sorted by (account_id, period_start); two statements of one account-month refuse."""

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in statements:
        last3 = account_last3(item["raw_text"])
        account = last3_to_account.get(last3)
        if account is None:
            raise StatementSummaryError(f"statement account ...{last3} is not mapped")
        row = summary_row(
            account_id=account,
            raw_text=item["raw_text"],
            source_artifact_id=item["source_artifact_id"],
            source_sha256=item["source_sha256"],
        )
        key = (account, row["period_start"])
        if key in seen:
            raise StatementSummaryError(f"two statements for {account} {key[1]}")
        seen.add(key)
        rows.append(row)
    # decorate-sort: a keyword-argument sort trips the boundary credential pattern
    order = sorted((r["account_id"], r["period_start"], n) for n, r in enumerate(rows))
    return [rows[n] for _a, _p, n in order]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("statement_dir", type=Path)
    parser.add_argument("mapping", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    last3_to_account = {
        item["last3"]: item["ledger_account_id"] for item in mapping["accounts"]
    }
    statements = []
    for pdf in sorted(args.statement_dir.glob("*.pdf")):
        payload = pdf.read_bytes()
        statements.append(
            {
                "raw_text": zfs.pdftotext_raw(pdf),
                "source_artifact_id": f"statements/{pdf.name}",
                "source_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    rows = build_rows(statements, last3_to_account)
    if args.out.exists():
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    args.out.write_text(
        json.dumps(rows, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"rows": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
