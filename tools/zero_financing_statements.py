"""Provider-observed zero-financing days from pinned provider monthly statements.

A pure reader of statement text (extracted by ``pdftotext -layout``): per account-month it
decides whether the statement proves that the account borrowed nothing on any day of its
period, and emits one ``OBSERVED_ZERO_FINANCING_DAY`` row per calendar day of a qualifying
period, pinned to the statement PDF (sha256 and page).  A period that does not qualify
yields no zero rows and is listed as unobserved with its reason; an absent field refuses.

Rules (control ruling 2026-11-14), per statement:

* retirement-type statement (IRA / Roth / 401K ...) with no margin loan block: no margin
  feature, every day of the period is a zero day;
* otherwise the statement needs the Margin Loan block and ALL of: opening and closing
  margin loan balance exactly $0.00, no margin-interest expense posting in the period
  (interest accrues daily, so a debit day would post), no margin-loan borrow/repay
  transaction line;
* the account number printed on the statement must match the account the mapping names.

The mapping from the file label (h8 = sha256("acct-<provider>-<last3>")[:8]) to the ledger
account id, and the last three digits, live in a local-only, hash-pinned input file; the
digits never enter the artifact.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Final, Mapping, Sequence, cast

ARTIFACT_ID: Final = "zero-financing-observations"
SCHEMA_VERSION: Final = 1
BASIS: Final = "OBSERVED_ZERO_FINANCING_DAY"
CLASSIFICATION: Final = "ZERO_FINANCING_OBSERVED"
# a day inside a printed Margin Interest range: borrowing observed, amount = the range's
# average margin daily balance (an average over the range, never a day balance)
FINANCING_CLASSIFICATION: Final = "OBSERVED_FINANCING_DAY"
RANGE_AVERAGE: Final = "RANGE_AVERAGE"
# the exact margin loan balance printed for a statement's last day
STATEMENT_CLOSING_BALANCE: Final = "STATEMENT_CLOSING_BALANCE"
# a day's end-of-day cash balance derived from the statement's own transactions, tied
# to its printed beginning and ending cash
DAY_BALANCE_DERIVED: Final = "DAY_BALANCE_DERIVED"

_MONTHS: Final = {name: i for i, name in enumerate(calendar.month_name) if name}
_PERIOD = re.compile(
    r"(?P<m1>[A-Z][a-z]+) (?P<d1>\d{1,2})(?:, (?P<y1>\d{4}))?\s*-\s*"
    r"(?:(?P<m2>[A-Z][a-z]+) )?(?P<d2>\d{1,2}), (?P<y2>\d{4})"
)
_ACCOUNT_MASK = re.compile(r"\*{4}-\*\d{3}|\d{4}-\d{4}")
_HEADER_LINES = 80
# The account type is read from the statement's own title line, against exact allowlisted
# titles (a substring such as "RETIREMENT" in holder text is never a type).
_TITLES = (
    (re.compile(r"Schwab One.? Account of"), "margin"),
    (re.compile(r"Roth Contributory IRA of"), "retirement"),
    (re.compile(r"Rollover IRA of"), "retirement"),
    (re.compile(r"Contributory IRA of"), "retirement"),
    (
        re.compile(r"Schwab Personal Choice Retirement Account.? \(PCRA\) of"),
        "retirement",
    ),
)
# the layout is the one of the provider whose name opens its titles
LAYOUT_PROVIDER_ID: Final = _TITLES[0][0].pattern.split()[0].lower()
# identifiers printed on statements that are not the account's own number
_NON_ACCOUNT_IDS = frozenset({"2101-0500", "0616-1160"})
_LOAN_MOVE = re.compile(
    r"^[ \t]*\d{2}/\d{2}[ \t].*?\b(margin[ \t]+loan[ \t]+(advance|payment|repayment)|"
    r"loan[ \t]+(advance|repayment)|margin[ \t]+borrow)",
    re.M | re.I,
)
# A margin-interest posting names the days it charged: "INTEREST 03/30THRU 04/28".  Those
# days were financing days whichever statement month they fall in (a debit on the 30th
# posts in the NEXT statement), so they are never zero days.
_INTEREST_PERIOD = re.compile(
    r"INTEREST[ \t]+(\d{2})/(\d{2})[ \t]*THRU[ \t]+(\d{2})/(\d{2})"
)
_SHA = re.compile(r"[0-9a-f]{64}")


class ZeroFinancingError(ValueError):
    """A statement or input cannot be read as complete evidence."""


class CaptureClaimsDiffer(ZeroFinancingError):
    """A capture's own claims disagree with the raw bytes it pins: that capture is not
    evidence (its account stays unobserved), the other accounts are unaffected."""


def statement_label(provider_id: str, last3: str) -> str:
    return hashlib.sha256(f"acct-{provider_id}-{last3}".encode()).hexdigest()[:8]


_MONEY_AMOUNT = re.compile(r"\(?-?\$[\d,]+\.\d\d(?!\d)\)?")
_MARGIN_INTEREST_WORDS = re.compile(r"margin\s+interest", re.I)
_DATED_LINE = re.compile(r"^[ \t]*\d{1,2}/\d{1,2}(/\d{2,4})?[ \t]")
_AMOUNT_AT_END = re.compile(r"\(?-?[\d,]+\.\d\d\)?[ \t]*$")
_TRANSACTIONS_SUMMARY = "Transactions - Summary"


def _parse_money(amount_text: str) -> Decimal:
    """One printed amount as a signed number: "$1,250.00", "($1,250.00)", "-$5.00"."""

    text = amount_text.strip().replace("\u2212", "-")
    if not re.fullmatch(r"\(?-?\$?-?(\d{1,3}(,\d{3})+|\d+)(\.\d\d)?\)?-?", text):
        raise ZeroFinancingError("margin loan balance is not a number")
    negative = text.startswith("(") or "-" in text
    digits = re.sub(r"[^\d.]", "", text)
    value = Decimal(digits)
    return -value if negative else value


def _loan_block(lines: list[str]) -> tuple[Decimal, Decimal] | None:
    """(opening, closing) margin loan balance of the Margin Loan block, if printed.

    The first balance row under the column headings must hold at least two printed
    amounts, each in a recognised form (``$x``, ``($x)``, ``-$x``); any other number in
    the row, or a missing row, refuses."""

    headers = [
        n
        for n, x in enumerate(lines)
        if "Opening Margin" in x and "Closing Margin" in x
    ]
    if len(headers) > 1:
        return None  # the strict layout check refuses the statement (LOAN_BLOCK_NOT_UNIQUE)
    for number, line in enumerate(lines):
        if "Opening Margin" in line and "Closing Margin" in line:
            for follow in lines[number + 1 : number + 7]:
                amounts = _MONEY_AMOUNT.findall(follow)
                if len(amounts) >= 2:
                    if len(amounts) < 4:
                        # opening, closing, funds available, buying power: a row with
                        # fewer cannot be anchored to its labels (a blank closing balance
                        # would pair the opening balance with "Funds Available")
                        raise ZeroFinancingError(
                            "margin loan block row is not four amounts"
                        )
                    if follow.count("$") != len(amounts):
                        raise ZeroFinancingError(
                            "margin loan block has an unparsed amount"
                        )
                    return _block_amounts(line, follow)
            raise ZeroFinancingError("margin loan block has no balances")
    return None


_INT_LABEL = re.compile(
    r"(?i)margin\s*int(?:erest)?\b|interest\s+on\s+debit|debit\s+interest"
)
_INT_EXEMPT = re.compile(
    r"(?i)includes margin interest|no margin interest was charged|margin interest rate charged on daily margin|"
    r"certain margin loan interest|interest paid on margin|margin loan rates?|"
    r"average margin|daily balance|margin interest (details|\(continued\))"
)
_THRU = re.compile(r"INTEREST[ \t]+(\d{2})/(\d{2})[ \t]*THRU[ \t]+(\d{2})/(\d{2})")


def _margin_interest_postings(lines: list[str]) -> tuple[int, bool]:
    """(margin-interest postings, ambiguous).

    Every line that names margin interest (any case, any variant such as "Margin Int" or
    "Interest on Debit Balance") must be accounted for: a table heading, a known label or
    disclosure covering the matched words themselves, or a posting.  A dated line with an
    amount is always a posting.  Postings and printed charged periods ("INTEREST a THRU
    b") pair one to one by position, each within a page's reach of its pair, with no period
    printed twice; anything else is ambiguous and the caller refuses the statement."""

    labels: list[int] = []
    for number, line in enumerate(lines):
        spans = list(_INT_LABEL.finditer(line))
        if not spans:
            continue
        heading = re.match(r"[ \t]*Margin Interest([ \t]{2,}\S.*)?$", line) and not (
            re.search(r"\d", line)
        )
        exempt = [m.span() for m in _INT_EXEMPT.finditer(line)]
        dated = bool(_DATED_LINE.match(line)) and bool(re.search(r"\d\.\d\d", line))
        for match in spans:
            covered = any(a <= match.start() < b for a, b in exempt)
            if heading or (covered and not dated):
                continue
            labels.append(number)
            break
    thru = [(n, p) for n, line in enumerate(lines) for p in _THRU.findall(line)]
    periods = [p for _n, p in thru]
    ambiguous = len(labels) != len(thru) or len(set(periods)) != len(periods)
    if not ambiguous:
        ambiguous = any(
            abs(label - n) > _PAIR_REACH
            for label, (n, _p) in zip(sorted(labels), sorted(thru), strict=True)
        )
    return len(periods), ambiguous


_PAIR_REACH = 25
# below this loan balance a day's interest rounds to nothing and no range is printed
_INTEREST_DUST = Decimal(15)


def _incomplete(lines: list[str]) -> str | None:
    """Why a statement cannot be read as complete, or None.

    Every statement prints a "Transactions - Summary".  When transaction details are
    printed they must end in exactly one "Total Transactions" line (a dropped page breaks
    that); when none are printed, the summary may state only $0.00 amounts."""

    text = "\n".join(lines)
    if _TRANSACTIONS_SUMMARY not in text:
        return "TRANSACTIONS_SUMMARY_ABSENT"
    details = sum(1 for line in lines if "Transaction Details" in line)
    totals = sum(1 for line in lines if "Total Transactions" in line)
    if details:
        return None if totals == 1 else "TRANSACTION_DETAILS_WITHOUT_ONE_TOTAL"
    for number, line in enumerate(lines):
        if _TRANSACTIONS_SUMMARY in line:
            for follow in lines[number + 1 : number + 9]:
                amounts = _MONEY_AMOUNT.findall(follow)
                if len(amounts) >= 6:
                    # beginning cash, the activity columns, ending cash: activity is zero
                    if any(_parse_money(t) != 0 for t in amounts[1:-1]):
                        return "ACTIVITY_SUMMARY_WITHOUT_TRANSACTION_DETAILS"
                    return None
            return "TRANSACTIONS_SUMMARY_UNREADABLE"
    return None


def _interest_periods(text: str, period_end: date) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for match in _INTEREST_PERIOD.finditer(text):
        m1, d1, m2, d2 = (int(g) for g in match.groups())
        year2 = period_end.year if m2 <= period_end.month else period_end.year - 1
        year1 = year2 if m1 <= m2 else year2 - 1
        try:
            out.append((date(year1, m1, d1), date(year2, m2, d2)))
        except ValueError:
            raise ZeroFinancingError("margin interest period is not a date") from None
    return out


_TABLE_RANGE = re.compile(r"(?<![\d/])(\d{2})/(\d{2})(?:-(\d{2})/(\d{2}))?(?![\d/])")
_ENTRY = (
    r"(\d{2})/(\d{2})(?:-(\d{2})/(\d{2}))?[ \t]+\(?\$?([\d,]+\.\d\d)\)?[ \t]+"
    r"(\d+\.\d+)[ \t]+([\d,]+\.\d\d)"
)
_RANGE_DETAIL = re.compile(_ENTRY)
_ROW_ONLY = re.compile(r"(?:" + _ENTRY + r"[ \t]*)+")


def _table_rows(lines: list[str], window: int = 13) -> list[str]:
    """The data rows of every "Margin Interest" table: lines under the heading that hold
    only dates, ranges, amounts and rates (the pending-activity lines below carry words)."""

    out: list[str] = []
    for number, line in enumerate(lines):
        if not re.match(r"[ \t]*Margin Interest([ \t]{2,}\S.*)?$", line) or (
            _MONEY_AMOUNT.search(line)
        ):
            continue
        for follow in lines[number + 1 : number + 1 + window]:
            text = follow.strip()
            if re.fullmatch(r"[\d/\-\s,.$()%]+", text) and re.match(
                r"\d{2}/\d{2}", text
            ):
                out.append(follow)
    return out


def _range_dates(match: "re.Match[str]", period_end: date) -> tuple[date, date]:
    m1, d1 = int(match.group(1)), int(match.group(2))
    m2 = int(match.group(3)) if match.group(3) else m1
    d2 = int(match.group(4)) if match.group(4) else d1
    year2 = period_end.year if m2 <= period_end.month else period_end.year - 1
    year1 = year2 if m1 <= m2 else year2 - 1
    try:
        return date(year1, m1, d1), date(year2, m2, d2)
    except ValueError:
        raise ZeroFinancingError("margin interest range is not a date") from None


def _interest_table_ranges(
    lines: list[str], period_end: date, window: int = 13
) -> list[tuple[date, date]]:
    """The day ranges (a single day counts as a range of one day) of the statement's
    "Margin Interest" table: each is a stretch of days with a margin debit."""

    return [
        _range_dates(match, period_end)
        for row in _table_rows(lines, window)
        for match in _TABLE_RANGE.finditer(row)
    ]


def _interest_range_details(
    lines: list[str], period_end: date, window: int = 13
) -> list[tuple[date, date, Decimal, Decimal, Decimal]] | None:
    """Every range of the Margin Interest table with its average margin daily balance,
    rate and interest paid, or None when any row holds something else."""

    out: list[tuple[date, date, Decimal, Decimal, Decimal]] = []
    for row in _table_rows(lines, window):
        if _ROW_ONLY.fullmatch(row.strip()) is None:
            return None
        for match in _RANGE_DETAIL.finditer(row):
            first, last = _range_dates(match, period_end)
            out.append(
                (
                    first,
                    last,
                    Decimal(match.group(5).replace(",", "")),
                    Decimal(match.group(6)),
                    Decimal(match.group(7).replace(",", "")),
                )
            )
    return out


def _interest_paid_period(lines: list[str]) -> Decimal | None:
    """The "Interest Paid on Margin Loan - This Period" amount of the loan block."""

    for number, line in enumerate(lines):
        if "Interest Paid on Margin" not in line:
            continue
        if not any("This Period" in x for x in lines[number : number + 4]):
            continue
        for row in lines[number + 1 : number + 6]:
            found = re.findall(r"\(?\$[\d,]+\.\d\d\)?", row)
            if len(found) >= 2:
                return abs(_parse_money(found[-2]))
        return None
    return None


_FIXED_HOLIDAYS = frozenset({(1, 1), (7, 4), (12, 25)})


def _business_day(day: date, step: int) -> date:
    """The nearest business day at or beyond ``day`` in the direction of ``step``
    (weekends and the fixed-date holidays are skipped)."""

    while day.weekday() >= 5 or (day.month, day.day) in _FIXED_HOLIDAYS:
        day += timedelta(days=step)
    return day


def reconcile_interest(facts: Mapping[str, Any]) -> tuple[bool, str]:
    """(reconciled, reason): the statement's charged periods, printed ranges and the
    interest paid agree, so the days of a charged period outside every printed range carry
    no interest.  Anything that does not agree refuses the refinement (the month stays
    unobserved with the reason)."""

    postings = facts["margin_interest_postings"]
    if postings < 1:
        return False, "NO_INTEREST_POSTING"
    periods = sorted(set(facts["thru_periods"]))
    if not periods:
        return False, "NO_CHARGED_PERIOD"
    for (_a1, b1), (a2, _b2) in zip(periods, periods[1:], strict=False):
        if a2 <= b1:
            return False, "CHARGED_PERIODS_OVERLAP"
    details = facts["range_details"]
    if details is None or facts["interest_table_ranges"] != len(details):
        return False, "RANGE_DETAIL_MISSING"
    paid = facts["interest_paid_period"]
    if paid is None:
        return False, "INTEREST_PAID_NOT_PRINTED"
    if paid <= 0 or not details:
        return False, "POSTING_WITHOUT_INTEREST_RANGES"
    ranges = sorted(details)
    for (_a1, b1, *_x), (a2, _b2, *_y) in zip(ranges, ranges[1:], strict=False):
        if a2 <= b1:
            return False, "RANGES_OVERLAP"
    for first, last, average, rate, interest in ranges:
        if not any(a <= first and last <= b for a, b in periods):
            return False, "RANGE_OUTSIDE_CHARGED_PERIOD"
        expected = average * rate / Decimal(100) * ((last - first).days + 1) / 360
        if abs(expected - interest) > max(Decimal("0.03"), interest * Decimal("0.03")):
            return False, "RANGE_INTEREST_NOT_PLAUSIBLE"
    if sum((r[4] for r in ranges), Decimal(0)) != paid:
        return False, "INTEREST_DOES_NOT_RECONCILE"
    block = facts["loan_block"]
    if block is not None:
        # A loan outstanding on a statement's first (last) day, inside a charged period, must
        # be inside a printed range: interest accrues every day it is outstanding.  The
        # balance carried in may show on the day before the period or on its first business
        # day (a holiday accrues nothing), and the closing balance on the last business day.
        start, end = facts["period_start"], facts["period_end"]

        def printed(day: date) -> bool:
            return any(first <= day <= last for first, last, *_rest in ranges)

        def charged(day: date) -> bool:
            return any(a <= day <= b for a, b in periods)

        opening_days = (start - timedelta(days=1), _business_day(start, 1))
        closing_days = (end, _business_day(end, -1))
        if abs(block[0]) >= _INTEREST_DUST and any(map(charged, opening_days)):
            if not any(printed(d) for d in opening_days):
                return False, "LOAN_BALANCE_OUTSIDE_PRINTED_RANGES"
        if abs(block[1]) >= _INTEREST_DUST and any(map(charged, closing_days)):
            if not any(printed(d) for d in closing_days):
                return False, "LOAN_BALANCE_OUTSIDE_PRINTED_RANGES"
    return True, "INTEREST_RECONCILED"


def _labelled(lines: list[str], label: str, window: int = 3) -> list[str]:
    """The lines that carry or directly follow every line containing ``label``."""

    out: list[str] = []
    for number, line in enumerate(lines):
        if label in line:
            out.extend(lines[number : number + window])
    return out


def _page_of(text: str, needle: str) -> int | None:
    for number, page in enumerate(text.split("\f"), start=1):
        if needle in page:
            return number
    return None


def _account_type(lines: list[str]) -> str | None:
    """ "margin" or "retirement" from the statement's title line: the first non-blank line,
    cut at the "Account Number" label that shares it, against the exact allowlisted titles;
    None when the title is not one of them."""

    for line in lines[:_HEADER_LINES]:
        if line.strip():
            title = " ".join(line.split("Account Number")[0].split())
            for pattern, kind in _TITLES:
                if pattern.fullmatch(title):
                    return kind
            return None
    return None


def _layout_issue(
    text: str, lines: list[str], account_type: str | None, own: set[str]
) -> str | None:
    """Why the page does not parse against the one strict Schwab layout, or None.

    A statement counts toward a zero day only when every expected element is present
    exactly as expected; an unrecognised layout refuses the month (the days stay
    unobserved), never a guess."""

    if account_type is None:
        return "TITLE_NOT_RECOGNISED"
    full = {n for n in own if re.fullmatch(r"\d{4}-\d{4}", n)}
    if len(full) > 1:
        return "ACCOUNT_NUMBER_FORMS_DIFFER"
    number = next(iter(full), "")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    for index, page in enumerate(pages, start=1):
        for found, total in re.findall(r"(?m)^[ \t]*(\d+) of (\d+)[ \t]*$", page):
            if int(found) != index or int(total) != len(pages):
                return "PAGE_MARKER_INCONSISTENT"
    for token in re.findall(r"\b\d{4}-\d{4}\b", text):
        if token != number and token not in _NON_ACCOUNT_IDS:
            return "FOREIGN_ACCOUNT_NUMBER"
    edge = [x for x in lines if "Opening Margin" in x or "Closing Margin" in x]
    both = [x for x in edge if "Opening Margin" in x and "Closing Margin" in x]
    info = text.count("Margin Loan Information")
    if account_type == "retirement":
        if info or edge:
            return "MARGIN_BLOCK_ON_RETIREMENT_STATEMENT"
        return None
    if info != 1 or len(both) != 1 or len(edge) != 1:
        return "LOAN_BLOCK_NOT_UNIQUE"
    if not re.search(r"(?m)^[ \t]*1 of \d+[ \t]*$", pages[0] if pages else ""):
        return "PAGE_ONE_MARKER_ABSENT"
    if text.count("Transactions - Summary") != 1:
        return "TRANSACTIONS_SUMMARY_NOT_UNIQUE"
    return None


def _block_amounts(header: str, row: str) -> tuple[Decimal, Decimal]:
    """Opening and closing amounts of the row, anchored to the header's own labels (the
    columns of other blocks may share the line): each label takes the amount that starts
    nearest its column, within the label's width.  The two labels must take two different
    amounts, opening left of closing; any ambiguity refuses."""

    found = list(_MONEY_AMOUNT.finditer(row))
    picked: list[int] = []
    for label in ("Opening Margin", "Closing Margin"):
        position = header.index(label)
        candidates = [
            (abs(m.start() - position), index)
            for index, m in enumerate(found)
            if abs(m.start() - position) <= 14
        ]
        if not candidates:
            raise ZeroFinancingError("margin loan block amount is not under its label")
        best = min(candidates)
        if sum(1 for c in candidates if c[0] == best[0]) > 1:
            raise ZeroFinancingError("margin loan block amount column is ambiguous")
        picked.append(best[1])
    if picked[0] >= picked[1]:
        raise ZeroFinancingError("margin loan block amounts share a column")
    return (
        _parse_money(found[picked[0]].group(0)),
        _parse_money(found[picked[1]].group(0)),
    )


def parse_statement(text: str, file_period_end: date | None = None) -> dict[str, Any]:
    """Facts of one statement.  The period comes from the statement's own labelled
    "Statement Period" and the account from its labelled "Account Number" (every
    occurrence must agree); ``file_period_end`` only records whether the file label
    matches (a mismatch is reported, never trusted)."""

    text = text.replace("\r\n", "\n")
    lines = text.splitlines()
    periods = set()
    for chunk in _labelled(lines, "Statement Period"):
        for match in _PERIOD.finditer(chunk):
            m1 = _MONTHS.get(match["m1"])
            m2 = _MONTHS.get(match["m2"] or match["m1"])
            if m1 is None or m2 is None:
                continue
            try:
                y2 = int(match["y2"])
                end = date(y2, m2, int(match["d2"]))
                y1 = int(match["y1"]) if match["y1"] else (y2 if m1 <= m2 else y2 - 1)
                start = date(y1, m1, int(match["d1"]))
            except ValueError:
                continue
            if start <= end:
                if (end - start).days + 1 > 31:
                    raise ZeroFinancingError("statement period is longer than a month")
                periods.add((start, end))
    if len(periods) != 1:
        raise ZeroFinancingError(
            "statement period is absent"
            if not periods
            else "statement period is ambiguous"
        )
    ((start, end),) = periods
    numbers = {
        re.sub(r"\D", "", n)[-3:]
        for chunk in _labelled(lines, "Account Number")
        for n in _ACCOUNT_MASK.findall(chunk)
    }
    if len(numbers) != 1:
        raise ZeroFinancingError("statement account number is absent or ambiguous")
    postings, ambiguous = _margin_interest_postings(lines)
    account_type = _account_type(lines)
    own_numbers = {
        n
        for chunk in _labelled(lines, "Account Number")
        for n in _ACCOUNT_MASK.findall(chunk)
    }
    return {
        "last3": next(iter(numbers)),
        "period_start": start,
        "period_end": end,
        "file_label_matches": file_period_end is None or file_period_end == end,
        # the account type is read from the statement header only, never from disclosures
        "retirement": account_type == "retirement",
        "account_type": account_type,
        "layout_issue": _layout_issue(text, lines, account_type, own_numbers),
        "full_number": next(
            (n for n in own_numbers if re.fullmatch(r"\d{4}-\d{4}", n)), None
        ),
        "loan_block": _loan_block(lines),
        "incomplete": _incomplete(lines),
        "margin_interest_postings": postings,
        "margin_interest_ambiguous": ambiguous,
        "loan_movements": len(_LOAN_MOVE.findall(text)),
        "interest_periods": _interest_periods(text, end)
        + _interest_table_ranges(lines, end),
        "interest_table_ranges": len(_interest_table_ranges(lines, end)),
        "thru_periods": _interest_periods(text, end),
        "range_details": _interest_range_details(lines, end),
        "interest_paid_period": _interest_paid_period(lines),
        "table_page": _page_of(text, "Average Margin"),
        "details_page": _page_of(text, "Transaction Details"),
        "block_page": _page_of(text, "Margin Loan Information"),
        "header_page": _page_of(text, "Statement Period"),
    }


def verdict(facts: Mapping[str, Any]) -> tuple[str, str]:
    """('ZERO' | 'FINANCING' | 'UNVERIFIED', reason) for one statement period.

    A margin account's ZERO is provisional: its days are zero only once the NEXT statement
    is pinned and shows no interest for them (``build`` decides)."""

    if facts["layout_issue"]:
        return "UNVERIFIED", f"LAYOUT_{facts['layout_issue']}"
    no_margin_feature = facts["retirement"] and facts["loan_block"] is None
    if facts["incomplete"] and not no_margin_feature:
        # a retirement account has no margin feature: its transaction listing is not
        # evidence of anything here
        return "UNVERIFIED", str(facts["incomplete"])
    if facts["margin_interest_ambiguous"]:
        return "UNVERIFIED", "AMBIGUOUS_MARGIN_INTEREST_LINE"
    block = facts["loan_block"]
    if block is None:
        if facts["retirement"]:
            return "ZERO", "RETIREMENT_ACCOUNT_NO_MARGIN_FEATURE"
        return "UNVERIFIED", "NO_MARGIN_LOAN_BLOCK_AND_NOT_A_RETIREMENT_STATEMENT"
    if facts["retirement"]:
        return "UNVERIFIED", "RETIREMENT_STATEMENT_WITH_A_MARGIN_LOAN_BLOCK"
    opening, closing = block
    if opening != 0 or closing != 0:
        return "FINANCING", "MARGIN_LOAN_BALANCE_NONZERO"
    if facts["margin_interest_postings"]:
        return "FINANCING", "MARGIN_INTEREST_POSTED_IN_PERIOD"
    if facts["interest_table_ranges"]:
        return "FINANCING", "MARGIN_DEBIT_DAYS_IN_PERIOD"
    if facts["loan_movements"]:
        return "FINANCING", "MARGIN_LOAN_MOVEMENT_IN_PERIOD"
    return "ZERO", "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT"


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def extractor_identity() -> str:
    """The text extractor's own version line: a different extractor can read the same bytes
    differently, so it is part of the generation identity."""

    try:
        result = subprocess.run(["pdftotext", "-v"], capture_output=True, check=False)
    except OSError:
        return "pdftotext-unavailable"
    text = (result.stderr + result.stdout).decode("utf-8", errors="replace")
    return (text.strip().splitlines() or ["pdftotext-unknown"])[0].strip()


def _pdftotext(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ZeroFinancingError(
            f"statement text cannot be extracted: {path.name}"
        ) from exc
    return result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")


def load_mapping(raw: bytes, provider_id: str) -> dict[str, tuple[str, str]]:
    """label -> (ledger account id, last3) from the local-only mapping input."""

    top = json.loads(raw)
    if (
        not isinstance(top, dict)
        or set(top) != {"schema_version", "accounts"}
        or top["schema_version"] != 1
        or not isinstance(top["accounts"], list)
        or not top["accounts"]
    ):
        raise ZeroFinancingError("mapping shape differs")
    out: dict[str, tuple[str, str]] = {}
    seen_last3: set[str] = set()
    seen_accounts: set[str] = set()
    for item in top["accounts"]:
        if not isinstance(item, dict) or set(item) != {"ledger_account_id", "last3"}:
            raise ZeroFinancingError("mapping row keys differ")
        account, last3 = item["ledger_account_id"], item["last3"]
        if (
            not isinstance(account, str)
            or not re.fullmatch(r"account-[0-9a-f]{16}", account)
            or not isinstance(last3, str)
            or not re.fullmatch(r"\d{3}", last3)
        ):
            raise ZeroFinancingError("mapping row is invalid")
        label = statement_label(provider_id, last3)
        if label in out or last3 in seen_last3 or account in seen_accounts:
            raise ZeroFinancingError("mapping names an account or last-3 twice")
        seen_last3.add(last3)
        seen_accounts.add(account)
        out[label] = (account, last3)
    return out


def load_sums(raw: bytes) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S.*)", line.strip())
        if match is None or match[2] in sums:
            raise ZeroFinancingError("SHA256SUMS line is invalid or repeated")
        sums[match[2]] = match[1]
    return sums


_OUTGOING = re.compile(
    r"(?i)(buy|bought|purchas|withdraw|transfer|journal|wire|fee|margin int|"
    r"interest on debit|loan|advance|debit|sell short|expense|check|\batm\b)"
)
# the only entries that can never create a debit: a credit with a readable positive
# amount whose action is one of these stems (anything else voids the bracket)
_CREDIT_ACTION = re.compile(
    r"(?i)^\s*(cash dividend|qual[\w.]* dividend|reinvest dividend|bank interest|"
    r"credit interest|moneylink deposit|deposit|cash in lieu|long term cap gain|"
    r"short term cap gain|special dividend|pr yr cash div|pr yr div reinvest)\s*$"
)
_TX_AMOUNT = re.compile(r"\(?-?\$?\d[\d,]*(\.\d\d)?\)?-?")


def _tx_amount(text: object) -> Decimal | None:
    """A transaction amount as a signed number, or None when it is not readable."""

    raw = str(text).strip() if text is not None else ""
    if not _TX_AMOUNT.fullmatch(raw):
        return None
    negative = raw.startswith(("(", "-")) or raw.endswith("-")
    value = Decimal(re.sub(r"[^\d.]", "", raw))
    return -value if negative else value


def _transactions_cannot_debit(items: list[dict[str, Any]]) -> tuple[bool, str]:
    """(ok, reason).  Every amount must parse; a same-day reinvested dividend (credit X
    and its share purchase -X on the same symbol) nets to zero; every remaining entry
    must be a positive credit of an allowlisted action that names no outgoing word."""

    entries: list[tuple[str, str, str, Decimal, str]] = []
    for item in items:
        amount = _tx_amount(item["amount"])
        if amount is None:
            return False, "CAPTURE_TRANSACTION_AMOUNT_UNREADABLE"
        entries.append(
            (
                str(item["date"]),
                str(item["action"]),
                str(item.get("symbol", "")),
                amount,
                f"{item['action']} {item['description']}",
            )
        )
    for _day, _action, _symbol, _amount, text in entries:
        if _OUTGOING.search(text):
            return False, "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"
    remaining = list(entries)
    for entry in entries:
        if entry[1].strip().lower() == "reinvest dividend" and entry in remaining:
            for other in remaining:
                if (
                    other[1].strip().lower() == "reinvest shares"
                    and other[0] == entry[0]
                    and other[2] == entry[2]
                    and entry[2] != ""
                    and other[3] == -entry[3]
                ):
                    remaining.remove(entry)
                    remaining.remove(other)
                    break
    for _day, action, _symbol, amount, text in remaining:
        if (
            amount <= 0
            or not _CREDIT_ACTION.fullmatch(action)
            or _OUTGOING.search(text)
        ):
            return False, "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"
    return True, ""


_CAPTURE_KEYS = frozenset(
    {
        "schema_version",
        "provider_id",
        "account_last3",
        "captured_at_utc",
        "capture_date",
        "window_start",
        "window_end",
        "balances",
        "transactions_reported_count",
        "transactions",
        "transactions_complete",
        "raw_files",
    }
)


# Extra keys (how the page was read, row labels ...) are descriptive and ignored: only
# the required keys below can influence the verdict.


_PAGE_DEBIT_LINE = re.compile(r"(?i)margin loan|debit balance|margin debit")


_ROW_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _row_date(text: object) -> date:
    """A history row's own date: the first date printed ("MM/DD/YYYY"); a trailing
    "as of MM/DD/YYYY" is ignored (the row date decides the window)."""

    match = _ROW_DATE.match(str(text).strip())
    if match is None:
        raise ValueError("history row date is unreadable")
    return date(int(match[3]), int(match[1]), int(match[2]))


_PAGE_STAMP = re.compile(r"\d{2}:\d{2}:\d{2} [AP]M ET, (\d{2})/(\d{2})/(\d{4})")
_PAGE_ACCOUNT = re.compile(r"Account Selector:[^\n]*ending in (\d) (\d) (\d) Selected")
_PAGE_AMOUNT = re.compile(r"\(?-?\$?-?\d[\d,]*\.\d\d\)?")
_RETIREMENT_TX_WORDS = re.compile(
    r"(?i)margin\s*int|interest on debit|debit interest|margin loan|loan advance|debit"
)


def _page_facts(text: str, last3: str) -> date:
    """The portal timezone date of a raw page text ("Updated ... ET, mm/dd/yyyy") and the
    account it says it shows ("Account ending in d d d"): both must be present and the
    account must be the mapped one, or the page is not evidence of this account."""

    text = text.replace("\r\n", "\n")
    stamps = {m.groups() for m in _PAGE_STAMP.finditer(text)}
    if len(stamps) != 1:
        raise ZeroFinancingError("capture page has no single dated stamp")
    accounts = {"".join(m.groups()) for m in _PAGE_ACCOUNT.finditer(text)}
    if accounts != {last3}:
        raise CaptureClaimsDiffer("capture page names another account")
    month, day, year = (int(g) for g in next(iter(stamps)))
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ZeroFinancingError("capture page date is invalid") from exc


def _page_amount(text: str, label: str) -> Decimal | None:
    """The amount printed for ``label``: on the label's own line after it, or alone on
    the next non-blank line; any other content in between means no amount.  The label must
    appear exactly once and open its line: a repeated or prefixed label refuses."""

    lines = text.replace("\r\n", "\n").split("\n")
    where = [n for n, line in enumerate(lines) if label in line]
    if not where:
        return None
    if len(where) != 1 or not lines[where[0]].strip().startswith(label):
        raise CaptureClaimsDiffer("capture page repeats or prefixes a label")
    number = where[0]
    rest = lines[number].strip()[len(label) :].strip()
    candidates = [rest] if rest else []
    if not rest:
        for follow in lines[number + 1 :]:
            if follow.strip():
                candidates = [follow.strip()]
                break
    if candidates and _PAGE_AMOUNT.fullmatch(candidates[0].replace("\u2212", "-")):
        try:
            return _parse_money(candidates[0])
        except ZeroFinancingError:
            return None
    return None


def _recompute_capture(
    top: Mapping[str, Any],
    files: list[Any],
    raw_dir: Path | None,
    last3: str,
    margin_account: bool,
    last_start: date,
    own_number: str | None = None,
) -> tuple[Decimal, date, list[dict[str, str]]]:
    """Re-derive the capture from its pinned raw files and refuse any difference with the
    JSON's own claims.  Returns (borrowing amount: 0 when none, the capture date the raw
    pages show, the raw transactions of [final period start, capture date])."""

    assert raw_dir is not None

    def pick(kind: str, suffix: str) -> list[Any]:
        return [f for f in files if f["kind"] == kind and f["name"].endswith(suffix)]

    histories, history_pages = pick("history", ".json"), pick("history", ".txt")
    balances_json, balances_text = pick("balances", ".json"), pick("balances", ".txt")
    if len(histories) != 1 or len(history_pages) != 1:
        raise ZeroFinancingError("capture needs one raw history response and page")
    history_page = (raw_dir / history_pages[0]["name"]).read_text(
        encoding="utf-8", errors="replace"
    )
    capture_date = _page_facts(history_page, last3)
    window_start = date.fromisoformat(top["window_start"])
    window_end = date.fromisoformat(top["window_end"])
    try:
        history = json.loads((raw_dir / histories[0]["name"]).read_bytes())
        raw_rows = history["brokerageTransactions"]
        requested_from = _row_date(history["fromDate"])
        requested_to = _row_date(history["toDate"])
        if "bookmark" not in history or not isinstance(raw_rows, list):
            raise KeyError("bookmark")
        raw_all = [
            {
                "date": _row_date(row["transactionDate"]).isoformat(),
                "action": str(row["action"]),
                "description": str(row.get("description", "")),
                "amount": str(row["amount"]),
                "symbol": str(row.get("symbol") or ""),
            }
            for row in raw_rows
        ]
        claimed = sorted(
            (
                str(t["date"]),
                str(t["action"]),
                str(t["amount"]),
                str(t.get("description", "")),
                str(t.get("symbol") or ""),
            )
            for t in top["transactions"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ZeroFinancingError("capture raw history is unreadable") from exc
    if requested_from > min(last_start, window_start) or requested_to < capture_date:
        raise ZeroFinancingError(
            "capture raw history range does not cover the final period and capture"
        )
    if history["bookmark"]:
        # more pages exist: the rows held must reach back past the requested start
        if not raw_all or min(r["date"] for r in raw_all) >= last_start.isoformat():
            raise ZeroFinancingError(
                "capture raw history does not reach its window start"
            )
    derived_claim = sorted(
        (r["date"], r["action"], r["amount"], r["description"], r["symbol"])
        for r in raw_all
        if window_start.isoformat() <= r["date"] <= window_end.isoformat()
    )
    if derived_claim != claimed:
        raise CaptureClaimsDiffer("capture transactions differ from the raw history")
    scan = [
        r
        for r in raw_all
        if last_start.isoformat() <= r["date"] <= capture_date.isoformat()
    ]
    if len(balances_json) != 1:
        raise ZeroFinancingError("capture needs exactly one raw balances response")
    try:
        balances = json.loads((raw_dir / balances_json[0]["name"]).read_bytes())
        entries = [
            e
            for e in balances["brokerageAccountList"]
            if str(e.get("accountId", ""))[-3:] == last3
        ]
        raw_margin = (
            Decimal(str(entries[0]["cashInvestments"]["marginBalance"]))
            if len(entries) == 1
            else None
        )
        raw_loan: list[Decimal] | None = None
        if len(entries) == 1 and isinstance(entries[0].get("marginsInfo"), dict):
            info = entries[0]["marginsInfo"]
            raw_loan = [
                Decimal(str(info[key]))
                for key in ("balanceSubjectInterest", "tradeDateBalance")
            ]
    except (KeyError, TypeError, ArithmeticError, ValueError) as exc:
        raise ZeroFinancingError("capture raw balances are unreadable") from exc
    if raw_margin is None:
        raise ZeroFinancingError("capture raw balances do not name the account once")
    if own_number is not None:
        raw_id = re.sub(r"\D", "", str(entries[0].get("accountId", "")))
        if len(raw_id) >= 8 and raw_id != re.sub(r"\D", "", own_number):
            raise CaptureClaimsDiffer("capture raw account number differs")
    structured = (entries[0].get("info") or {}).get("isMargin")
    if isinstance(structured, bool) and structured != margin_account:
        raise CaptureClaimsDiffer("capture raw account type disagrees")
    if margin_account and raw_loan is None:
        raise CaptureClaimsDiffer("capture raw loan fields are absent")
    if raw_loan is not None and any(v != 0 for v in raw_loan):
        if margin_account:
            raise CaptureClaimsDiffer("capture raw loan fields are not zero")
        return max(raw_loan), capture_date, scan  # a retirement account showing a loan
    claimed_balances = top["balances"]
    if not margin_account and isinstance(claimed_balances, dict):
        claimed_loan = claimed_balances.get("margin_loan_balance")
        if isinstance(claimed_loan, str) and claimed_loan.strip():
            if not _zero_amount(claimed_loan):
                # a retirement account claimed to hold a loan: borrowing, never extended
                return Decimal(1), capture_date, scan
    page = None
    if len(balances_text) == 1:
        page = (raw_dir / balances_text[0]["name"]).read_text(
            encoding="utf-8", errors="replace"
        )
        if _page_facts(page, last3) != capture_date:
            raise CaptureClaimsDiffer("capture pages are dated differently")
    elif len(balances_text) > 1 or margin_account:
        raise ZeroFinancingError("capture needs exactly one raw balances page")
    subject = _page_amount(page, "Balance Subject to Interest") if page else None
    cash_total = _page_amount(page, "Cash & Cash Investments Total") if page else None
    if page is not None and (
        _PAGE_DEBIT_LINE.search(page) or (cash_total is not None and cash_total < 0)
    ):
        raise CaptureClaimsDiffer("capture page shows a debit or negative cash")
    if subject is not None and subject != 0 and not margin_account:
        return subject, capture_date, scan  # a retirement page showing a loan (P1-7)
    # the API marginBalance field is the account's cash credit when it is non-zero: it
    # is only that when the page's own cash total says so, to the cent
    if raw_margin != 0 and (cash_total is None or raw_margin != cash_total):
        if margin_account:
            raise CaptureClaimsDiffer(
                "capture raw marginBalance is not the page's cash total"
            )
        return raw_margin, capture_date, scan  # unexplained borrowing
    if margin_account:
        if subject is None or subject < 0:
            raise CaptureClaimsDiffer(
                "capture subject-to-interest is not on its raw page"
            )
        if not isinstance(claimed_balances, dict):
            raise ZeroFinancingError("capture balances are absent")
        for key in ("margin_loan_balance", "balance_subject_to_interest"):
            claimed_text = claimed_balances.get(key)
            if not isinstance(claimed_text, str) or not claimed_text.strip():
                raise ZeroFinancingError(f"capture {key} is absent")
            try:
                value = _parse_money("$" + claimed_text.strip().lstrip("$"))
            except ZeroFinancingError as exc:
                raise ZeroFinancingError(f"capture {key} is unreadable") from exc
            if value != subject:
                raise CaptureClaimsDiffer(f"capture {key} differs from its page")
    return Decimal(0), capture_date, scan


def _zero_amount(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return _parse_money(value if "$" in value else "$" + value.strip()) == 0
    except ZeroFinancingError:
        return False


def evaluate_capture(
    raw: bytes,
    *,
    provider_id: str,
    last3: str,
    last_end: date,
    cutoff: date,
    raw_dir: Path | None,
    margin_account: bool = True,
    last_start: date | None = None,
    own_number: str | None = None,
) -> tuple[bool, str]:
    """(bracket holds, reason) for a dated account capture after the last statement.

    Structure, account, window and raw-file hashes are refused (an exception); a capture
    that is sound but shows borrowing evidence (nonzero balance, a transaction that can
    create a debit, incomplete list) only fails the bracket, leaving those days
    unobserved.  The account, the capture date and every transaction are read from the
    pinned raw files, never taken from the JSON's own claims."""

    try:
        top = json.loads(raw)
    except ValueError as exc:
        raise ZeroFinancingError("capture is not JSON") from exc
    if (
        not isinstance(top, dict)
        or not _CAPTURE_KEYS <= set(top)
        or top["schema_version"] != 1
    ):
        raise ZeroFinancingError("capture shape differs")
    if top["provider_id"] != provider_id:
        raise ZeroFinancingError("capture is for another provider")
    if top["account_last3"] != last3:
        raise ZeroFinancingError("capture account differs from the mapped account")
    try:
        capture_date = date.fromisoformat(top["capture_date"])
        window_start = date.fromisoformat(top["window_start"])
        window_end = date.fromisoformat(top["window_end"])
    except (TypeError, ValueError) as exc:
        raise ZeroFinancingError("capture dates are invalid") from exc
    if (
        capture_date <= cutoff
        or capture_date <= last_end
        or window_end != capture_date
        or window_start > window_end
        or window_start > last_end + timedelta(days=1)
    ):
        raise ZeroFinancingError(
            "capture does not cover the days after the last statement"
        )
    files = top["raw_files"]
    if not isinstance(files, list) or not files:
        raise ZeroFinancingError("capture names no raw files")
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "sha256", "size_bytes", "kind"}
            or raw_dir is None
        ):
            raise ZeroFinancingError("capture raw file entry is invalid")
        if (
            not isinstance(item["name"], str)
            or item["name"] in ("", ".", "..")
            or "/" in item["name"]
            or "\\" in item["name"]
        ):
            raise ZeroFinancingError("capture raw file name carries a path")
        path = raw_dir / str(item["name"])
        content = (
            path.read_bytes() if path.is_file() and not path.is_symlink() else None
        )
        if (
            content is None
            or hashlib.sha256(content).hexdigest() != item["sha256"]
            or len(content) != item["size_bytes"]
        ):
            raise ZeroFinancingError("capture raw file identity differs")
        if item["kind"] not in ("history", "balances"):
            raise ZeroFinancingError("capture raw file kind is unknown")
    try:
        borrowed, raw_date, scan = _recompute_capture(
            top,
            files,
            raw_dir,
            last3,
            margin_account,
            last_start if last_start is not None else last_end,
            own_number,
        )
    except CaptureClaimsDiffer:
        return False, "CAPTURE_CLAIMS_DIFFER_FROM_RAW"
    if raw_date != capture_date:
        return False, "CAPTURE_CLAIMS_DIFFER_FROM_RAW"
    if not margin_account:
        # a no-margin account type: the capture shows the account at its date; borrowing
        # evidence on such an account is an anomaly, never extended over
        if borrowed != 0:
            return False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING"
        if any(
            _RETIREMENT_TX_WORDS.search(f"{t['action']} {t['description']}")
            for t in scan
        ):
            return False, "RETIREMENT_ACCOUNT_SHOWS_INTEREST_OR_DEBIT"
        return True, "ACCOUNT_EXISTS_AT_CAPTURE"
    balances = top["balances"]
    if not isinstance(balances, dict):
        raise ZeroFinancingError("capture balances are absent")
    # absent fields refuse; a nonzero balance only fails the bracket
    for key in ("margin_loan_balance", "balance_subject_to_interest"):
        if not isinstance(balances.get(key), str) or not balances[key].strip():
            raise ZeroFinancingError(f"capture {key} is absent")
    if not (
        _zero_amount(balances["margin_loan_balance"])
        and _zero_amount(balances["balance_subject_to_interest"])
    ):
        return False, "CAPTURE_MARGIN_BALANCE_NOT_ZERO"
    transactions = top["transactions"]
    if not isinstance(transactions, list) or top["transactions_complete"] is not True:
        return False, "CAPTURE_TRANSACTIONS_NOT_COMPLETE"
    if top["transactions_reported_count"] != len(transactions):
        return False, "CAPTURE_TRANSACTION_COUNT_DIFFERS"
    for item in transactions:
        if not isinstance(item, dict) or not {
            "date",
            "action",
            "description",
            "amount",
        } <= set(item):
            raise ZeroFinancingError("capture transaction row is invalid")
        try:
            when = date.fromisoformat(item["date"])
        except (TypeError, ValueError) as exc:
            raise ZeroFinancingError("capture transaction date is invalid") from exc
        if not window_start <= when <= window_end:
            raise ZeroFinancingError("capture transaction is outside its window")
    effect_ok, effect_reason = _transactions_cannot_debit(scan)
    if not effect_ok:
        return False, effect_reason
    return True, "BRACKET_PROVEN_BY_CAPTURE"


_SUMMARY_ROW_AMOUNT = re.compile(r"\(?-?\$[\d,]+\.\d\d(?!\d)\)?")
_LADDER_CATEGORY = re.compile(
    r"^(?:(\d{2})/(\d{2})[ \t]+)?(Purchase|Sale|Dividend|Interest|Withdrawal|Deposit|"
    r"Expense|Other)\b(?![(/])"
)
_LADDER_COLUMN = {
    "Deposit": 1,
    "Withdrawal": 2,
    "Purchase": 3,
    "Sale": 4,
    "Dividend": 5,
    "Interest": 5,
    "Expense": 6,
}
_REALIZED = re.compile(r"\(?[\d,]+\.\d\d\)?,?\((?:ST|LT)\)")
_ROW_AMOUNT = re.compile(r"(?<![\d$.,/\-])\(?-?[\d,]+\.\d\d\)?(?![\d.,%])")


def _pdftotext_raw(path: Path) -> str:
    """The statement text in content-stream order (one transaction per run of lines, the
    amounts beside their own rows), where the physical layout interleaves columns."""

    try:
        result = subprocess.run(
            ["pdftotext", "-raw", str(path), "-"], check=True, capture_output=True
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ZeroFinancingError(
            f"statement text cannot be extracted: {path.name}"
        ) from exc
    return result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")


def parse_cash_ladder(
    raw_text: str, start: date, end: date
) -> tuple[dict[date, Decimal] | None, Decimal | None, str]:
    """(end-of-day cash balance for every day of the statement period, beginning cash, reason).

    The balance of a day is the printed beginning cash plus every transaction amount dated
    on or before it.  It is returned only when the statement ties out: the eight printed
    cash summary amounts add up, each summary column equals the sum of its category of
    transaction rows to the cent, every row has a date inside the period and a readable
    amount, and the ladder ends on the printed ending cash.  Anything else is None with the
    reason (the days stay unobserved), never a guess."""

    lines = raw_text.replace("\r\n", "\n").split("\n")
    try:
        head = next(
            n
            for n, x in enumerate(lines)
            if x.strip().startswith("Transactions - Summary")
        )
    except StopIteration:
        return None, None, "CASH_SUMMARY_ABSENT"
    summary: list[Decimal] | None = None
    for line in lines[head + 1 : head + 16]:
        found = _SUMMARY_ROW_AMOUNT.findall(line)
        if len(found) == 8:
            summary = [_parse_money(x) for x in found]
            break
    if summary is None:
        return None, None, "CASH_SUMMARY_ROW_NOT_FOUND"
    begin, end_cash = summary[0], summary[7]
    if begin + sum(summary[1:7], Decimal(0)) != end_cash:
        return None, None, "CASH_SUMMARY_DOES_NOT_ADD_UP"
    first = next(
        (n for n, x in enumerate(lines) if x.strip().startswith("Transaction Details")),
        None,
    )
    last = next(
        (n for n, x in enumerate(lines) if x.strip().startswith("Total Transactions")),
        None,
    )
    records: list[list[str]] = []
    dates: list[date | None] = []
    categories: list[str] = []
    if first is not None:
        if last is None or last < first:
            return None, None, "TRANSACTION_LIST_NOT_CLOSED"
        current: date | None = None
        for line in lines[first + 1 : last]:
            match = _LADDER_CATEGORY.match(line.strip())
            if match is None and re.match(r"\d{2}/\d{2}[ \t]+\S", line.strip()):
                return None, None, "UNKNOWN_TRANSACTION_CATEGORY"
            if match:
                if match.group(1):
                    try:
                        current = date(
                            end.year, int(match.group(1)), int(match.group(2))
                        )
                    except ValueError:
                        return None, None, "TRANSACTION_DATE_INVALID"
                    if not start <= current <= end:
                        return None, None, "TRANSACTION_DATE_OUTSIDE_PERIOD"
                records.append([line])
                dates.append(current)
                categories.append(match.group(3))
            elif records:
                records[-1].append(line)
    column_totals = [Decimal(0)] * 7
    by_day: dict[date, Decimal] = {}
    for record, when, category in zip(records, dates, categories, strict=True):
        if category == "Other":
            continue  # activity that does not move cash (transfers of securities, ...)
        if when is None:
            return None, None, "TRANSACTION_WITHOUT_DATE"
        text = _REALIZED.sub(" ", " ".join(record))
        text = re.sub(r"\$[\d,]+\.\d\d", " ", text)
        amounts = _ROW_AMOUNT.findall(text)
        if not amounts:
            return None, None, "TRANSACTION_AMOUNT_ABSENT"
        try:
            amount = _parse_money(amounts[-1])
        except ZeroFinancingError:
            return None, None, "TRANSACTION_AMOUNT_UNREADABLE"
        column_totals[_LADDER_COLUMN[category]] += amount
        by_day[when] = by_day.get(when, Decimal(0)) + amount
    if column_totals[1:] != list(summary[1:7]):
        return None, None, "CASH_CATEGORY_TOTALS_DIFFER"
    ladder: dict[date, Decimal] = {}
    cash = begin
    day = start
    while day <= end:
        cash += by_day.get(day, Decimal(0))
        ladder[day] = cash
        day += timedelta(days=1)
    if ladder[end] != end_cash:
        return None, None, "CASH_LADDER_DOES_NOT_CLOSE"
    return ladder, begin, "CASH_LADDER_CLOSES"


def _refine_table_from_raw(facts: dict[str, Any], raw_text: str) -> None:
    """The Margin Interest table read from the content-stream text, where a wrapped cell of
    the physical layout is one line: used only when it contains every range the layout read
    (the layout may drop trailing cells, never invent them)."""

    lines = raw_text.replace("\r\n", "\n").splitlines()
    end = facts["period_end"]
    plain = _interest_table_ranges(lines, end, 40)
    layout_plain = facts["interest_periods"][len(facts["thru_periods"]) :]
    if not set(layout_plain) <= set(plain) or len(plain) < len(layout_plain):
        return
    facts["interest_periods"] = list(facts["thru_periods"]) + plain
    facts["interest_table_ranges"] = len(plain)
    facts["range_details"] = _interest_range_details(lines, end, 40)


def build(
    pdf_dir: Path,
    sums: Mapping[str, str],
    mapping: Mapping[str, tuple[str, str]],
    *,
    provider_id: str,
    extract: Callable[[Path], str] | None = None,
    artifact_prefix: str | None = None,
    captures: Mapping[str, bytes] | None = None,
    capture_dir: Path | None = None,
    cutoff: date | None = None,
    history_start: Mapping[str, date] | None = None,
    capture_names: Mapping[str, str] | None = None,
    extract_raw: Callable[[Path], str] | None = None,
) -> dict[str, Any]:
    if provider_id != LAYOUT_PROVIDER_ID:
        raise ZeroFinancingError("the statement layout reader is bound to one provider")
    raw_extract = extract_raw or (_pdftotext_raw if extract is None else None)
    extract = extract or _pdftotext  # resolved at call time
    names = sorted(sums)
    if not names:
        raise ZeroFinancingError("no statements are pinned")
    prefix = artifact_prefix or f"zero-financing/{provider_id}"
    duplicate_periods: list[str] = []
    interest_days: dict[str, set[date]] = {}
    unobserved: list[dict[str, Any]] = []
    # (account, start, end) -> record of the first statement that carries the period
    periods: dict[tuple[str, date, date], dict[str, Any]] = {}
    seen_accounts: set[str] = set()
    full_numbers: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for name in names:
        match = re.fullmatch(
            re.escape(provider_id) + r"-stmt-([0-9a-f]{8})-(\d{4}-\d{2}-\d{2})\.pdf",
            name,
        )
        if match is None:
            raise ZeroFinancingError(f"unexpected statement file name: {name}")
        label, day = match.groups()
        path = pdf_dir / name
        if not path.is_file() or path.is_symlink():
            raise ZeroFinancingError(f"pinned statement is missing: {name}")
        content = path.read_bytes()
        sizes[name] = len(content)
        digest = hashlib.sha256(content).hexdigest()
        if digest != sums[name]:
            raise ZeroFinancingError(f"statement identity differs: {name}")
        if label not in mapping:
            raise ZeroFinancingError(f"statement label is not mapped: {label}")
        account, last3 = mapping[label]
        text = extract(path)
        facts = parse_statement(text, date.fromisoformat(day))
        if facts["last3"] != last3:
            raise ZeroFinancingError(
                f"statement account differs from the mapped account: {label}"
            )
        if not facts["file_label_matches"]:
            raise ZeroFinancingError(
                f"file name differs from the printed period: {name}"
            )
        seen_accounts.add(account)
        if facts["full_number"] is not None:
            known = full_numbers.setdefault(account, facts["full_number"])
            if known != facts["full_number"]:
                raise ZeroFinancingError(
                    f"statements of one account print different account numbers: {label}"
                )
        key = (account, facts["period_start"], facts["period_end"])
        if facts["margin_interest_postings"] > len(facts["interest_periods"]):
            raise ZeroFinancingError(
                f"a margin-interest posting names no charged period: {name}"
            )
        raw_text: str | None = None
        if raw_extract is not None:
            try:
                raw_text = raw_extract(path)
            except ZeroFinancingError:
                raw_text = None
        if raw_text is not None:
            _refine_table_from_raw(facts, raw_text)
        state, reason = verdict(facts)
        fingerprint = (
            facts["last3"],
            facts["retirement"],
            facts["loan_block"],
            facts["margin_interest_postings"],
            facts["loan_movements"],
            tuple(facts["interest_periods"]),
            state,
        )
        if key in periods:
            # the same statement period pinned twice must read identically, or refuse
            if periods[key]["fingerprint"] != fingerprint:
                raise ZeroFinancingError(
                    f"two different statements cover one period: {label}"
                )
            duplicate_periods.append(name)
            continue
        # the page the evidence sits on: the loan block, else the header (never invented)
        page = facts["block_page"] if facts["loan_block"] else facts["header_page"]
        if state == "ZERO" and page is None:
            raise ZeroFinancingError(f"evidence page is absent: {name}")
        retirement_type = state == "ZERO" and facts["loan_block"] is None
        periods[key] = {
            "fingerprint": fingerprint,
            "state": state,
            "reason": reason,
            "name": name,
            "digest": digest,
            "page": page,
            "type_only": retirement_type,
            "loan_closing": facts["loan_block"][1] if facts["loan_block"] else None,
            "loan_opening": facts["loan_block"][0] if facts["loan_block"] else None,
            "no_interest": not facts["margin_interest_postings"]
            and not facts["interest_table_ranges"],
            "number": facts["full_number"],
            "details_page": facts["details_page"],
            "coverage": None,
            "ladder": None,
            "ladder_reason": None,
            "refine_reason": None,
            "table_page": facts["table_page"] or facts["header_page"],
        }
        if state == "FINANCING":
            ok, why = reconcile_interest(facts)
            periods[key]["refine_reason"] = why
            if ok:
                periods[key]["coverage"] = (
                    list(facts["thru_periods"]),
                    list(facts["range_details"]),
                )
            if raw_text is not None and facts["loan_block"] is not None:
                periods[key]["ladder_reason"] = "CASH_LADDER_NOT_TRIED"
                try:
                    ladder, _begin, why_ladder = parse_cash_ladder(
                        raw_text, facts["period_start"], facts["period_end"]
                    )
                except ZeroFinancingError:
                    ladder, why_ladder = None, "RAW_TEXT_UNAVAILABLE"
                if ladder is not None and facts["range_details"]:
                    # a printed debit range must be a debit stretch of the ladder too
                    checked = ladder
                    for first_day, last_day, *_rest in facts["range_details"]:
                        for printed in _days(first_day, last_day):
                            if (
                                facts["period_start"] <= printed <= facts["period_end"]
                                and checked[printed] >= 0
                            ):
                                ladder, why_ladder = (
                                    None,
                                    "LADDER_CONTRADICTS_PRINTED_RANGE",
                                )
                if ladder is not None and (
                    facts["margin_interest_postings"] or facts["interest_table_ranges"]
                ):
                    # interest was charged: the ladder must show a debit day in the span
                    charged_span = {
                        d for a, b in facts["interest_periods"] for d in _days(a, b)
                    }
                    if not any(
                        balance < 0
                        for day, balance in ladder.items()
                        if day in charged_span
                    ):
                        ladder, why_ladder = (
                            None,
                            "LADDER_SHOWS_NO_DEBIT_FOR_CHARGED_INTEREST",
                        )
                periods[key]["ladder"] = ladder
                periods[key]["ladder_reason"] = why_ladder
        for first, last in facts["interest_periods"]:
            interest_days.setdefault(account, set()).update(_days(first, last))
    rows: list[dict[str, Any]] = []
    by_account: dict[str, list[tuple[date, date]]] = {}
    for account, start, end in periods:
        by_account.setdefault(account, []).append((start, end))
    for account, spans in by_account.items():
        spans.sort()
        for (s1, e1), (s2, e2) in zip(spans, spans[1:], strict=False):
            if s2 <= e1:
                raise ZeroFinancingError("two statement periods of one account overlap")
            earlier, later = periods[(account, s1, e1)], periods[(account, s2, e2)]
            if (
                s2 == e1 + timedelta(days=1)
                and earlier["loan_closing"] is not None
                and later["loan_opening"] is not None
                and abs(earlier["loan_closing"]) != abs(later["loan_opening"])
            ):
                # the margin loan balance printed at one statement's end is the next
                # one's start: a chain that breaks leaves both months unproven
                for broken in (earlier, later):
                    broken.update(
                        state="UNVERIFIED",
                        reason="LOAN_BALANCE_CHAIN_BREAKS",
                        coverage=None,
                        ladder=None,
                    )
        for index, (start, end) in enumerate(spans):
            record = periods[(account, start, end)]
            state, reason = record["state"], record["reason"]
            if state == "ZERO" and not record["type_only"]:
                # a margin account's days are zero only once the NEXT statement is pinned
                # (a debit's interest posts after the period closes)
                following = spans[index + 1] if index + 1 < len(spans) else None
                if following is None or following[0] != end + timedelta(days=1):
                    state, reason = "UNVERIFIED", "NEXT_STATEMENT_NOT_PINNED"
                elif periods[(account, *following)]["state"] not in (
                    "ZERO",
                    "FINANCING",
                ):
                    # the proving statement must itself be fully readable
                    state, reason = "UNVERIFIED", "NEXT_STATEMENT_NOT_VERIFIED"
            if state != "ZERO":
                unobserved.append(
                    {
                        "account_id": account,
                        "period_start": start.isoformat(),
                        "period_end": end.isoformat(),
                        "state": state,
                        "reason": reason,
                    }
                )
                continue
            for current in _days(start, end):
                if current in interest_days.get(account, ()):
                    continue  # charged by a later posting: a financing day
                rows.append(
                    {
                        "account_id": account,
                        "as_of_date": current.isoformat(),
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "0",
                        },
                        "classification": CLASSIFICATION,
                        "measurement_basis": BASIS,
                        "provider_id": provider_id,
                        "source_artifact_id": f"{prefix}/{record['name']}",
                        "source_artifact_sha256": record["digest"],
                        "source_field": (
                            "Statement account type"
                            if record["type_only"]
                            else "Margin Loan Information"
                        ),
                        "source_page": record["page"],
                        "verdict_reason": reason,
                    }
                )
    # Days after an account's latest statement, up to the cutoff, from its dated capture:
    # a retirement type still stands (the account exists at the capture date); a margin
    # account's final period and the days after it count only when the bracket holds.
    capture_notes: list[dict[str, Any]] = []
    if captures and cutoff is not None:
        last3_of = {account: last3 for account, last3 in mapping.values()}
        for account, spans in by_account.items():
            if account not in captures:
                continue
            start, end = spans[-1]
            record = periods[(account, start, end)]
            raw = captures[account]
            holds, why = evaluate_capture(
                raw,
                provider_id=provider_id,
                last3=last3_of[account],
                last_end=end,
                cutoff=cutoff,
                raw_dir=capture_dir,
                margin_account=not record["type_only"],
                last_start=start,
                own_number=record["number"],
            )
            if record["type_only"] and holds:
                holds = record["state"] == "ZERO"
                why = "RETIREMENT_TYPE_THROUGH_CUTOFF" if holds else "TYPE_NOT_PROVEN"
            elif not record["type_only"] and (
                record["state"] != "ZERO" or record["loan_closing"] != 0
            ):
                holds, why = False, "LAST_STATEMENT_NOT_ZERO"
            note: dict[str, Any] = {
                "account_id": account,
                "bracket": holds,
                "reason": why,
            }
            capture_notes.append(note)
            if not holds:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            first_new = end + timedelta(days=1)
            if not record["type_only"]:
                # the final statement period is proven by the bracket, not by a next
                # statement: re-admit its days (minus the days an interest range charged)
                first_new = start
                rows[:] = [
                    r
                    for r in rows
                    if not (
                        r["account_id"] == account
                        and date.fromisoformat(r["as_of_date"]) >= start
                    )
                ]
                unobserved[:] = [
                    u
                    for u in unobserved
                    if not (
                        u["account_id"] == account
                        and u["period_start"] == start.isoformat()
                    )
                ]
            for current in _days(first_new, cutoff):
                if current in interest_days.get(account, ()):
                    continue
                rows.append(
                    {
                        "account_id": account,
                        "as_of_date": current.isoformat(),
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "0",
                        },
                        "classification": CLASSIFICATION,
                        "measurement_basis": BASIS,
                        "provider_id": provider_id,
                        "source_artifact_id": (
                            f"{prefix}/captures/{(capture_names or {}).get(account, account)}"
                        ),
                        "source_artifact_sha256": digest,
                        "source_field": "Account capture: margin loan balance and activity",
                        "source_page": 1,
                        "verdict_reason": why,
                    }
                )
    # Charged periods of reconciled statements: every day of a charged period is observed,
    # a financing day inside a printed range (amount = the range average) and a zero day
    # outside every range (the statement's interest paid is fully accounted for by them).
    charged: dict[tuple[str, date], tuple[dict[str, Any], Any]] = {}
    for (account, _start, _end), record in periods.items():
        if record["coverage"] is None:
            continue
        thru, details = record["coverage"]
        for first, last in thru:
            for current in _days(first, last):
                if cutoff is not None and current > cutoff:
                    continue
                if (account, current) in charged:
                    raise ZeroFinancingError(
                        "two statements charge the same account day"
                    )
                hit = next((d for d in details if d[0] <= current <= d[1]), None)
                charged[(account, current)] = (record, hit)
    tail_rows: list[tuple[str, date, dict[str, Any]]] = []
    for (account, start, end), record in periods.items():
        if record["coverage"] is None:
            continue
        thru, _details = record["coverage"]
        tail_start = max(b for _a, b in thru) + timedelta(days=1)
        if tail_start > end:
            continue
        following = next(
            (sp for sp in by_account[account] if sp[0] == end + timedelta(days=1)), None
        )
        if following is None:
            continue
        proof = periods[(account, *following)]
        interest_free = proof["state"] == "ZERO" or (
            # a month whose loan balance appears later still proves the days before it: it
            # opens at zero (the chain agrees with this closing) and charges no interest
            proof["state"] == "FINANCING"
            and proof["no_interest"]
            and proof["loan_opening"] == 0
        )
        if not interest_free or proof["type_only"]:
            continue
        for current in _days(tail_start, end):
            if (account, current) not in charged and (
                cutoff is None or current <= cutoff
            ):
                tail_rows.append((account, current, proof))
    for account, current, proof in tail_rows:
        rows.append(
            {
                "account_id": account,
                "as_of_date": current.isoformat(),
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
                "classification": CLASSIFICATION,
                "measurement_basis": BASIS,
                "provider_id": provider_id,
                "source_artifact_id": f"{prefix}/{proof['name']}",
                "source_artifact_sha256": proof["digest"],
                "source_field": "Margin Loan Information: next statement, no interest",
                "source_page": proof["page"],
                "verdict_reason": "TAIL_PROVEN_BY_NEXT_STATEMENT_NO_INTEREST",
            }
        )
    for (account, current), (record, hit) in sorted(charged.items()):
        if record["table_page"] is None:
            raise ZeroFinancingError("interest table page is absent")
        row: dict[str, Any] = {
            "account_id": account,
            "as_of_date": current.isoformat(),
            "amount": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "0" if hit is None else str(hit[2]),
            },
            "classification": CLASSIFICATION
            if hit is None
            else FINANCING_CLASSIFICATION,
            "measurement_basis": BASIS,
            "provider_id": provider_id,
            "source_artifact_id": f"{prefix}/{record['name']}",
            "source_artifact_sha256": record["digest"],
            "source_field": (
                "Margin Interest table: no range printed, interest reconciled"
                if hit is None
                else "Margin Interest table: range average margin daily balance"
            ),
            "source_page": record["table_page"],
            "verdict_reason": (
                "INTEREST_RECONCILED_NO_RANGE"
                if hit is None
                else f"INTEREST_RANGE_{hit[0]:%m/%d}-{hit[1]:%m/%d}_RECONCILED"
            ),
        }
        if hit is not None:
            row["amount_measurement"] = RANGE_AVERAGE
        rows.append(row)
    # Months that did not reconcile on interest but whose cash ladder ties to the printed
    # beginning and ending cash: a day with a debit cash balance is a financing day with
    # that exact end-of-day balance, any other day a zero day.
    # A closing ladder never overrides interest evidence (reconciled ranges, proven zero
    # days): it fills only the days nothing else covers.
    have = {(r["account_id"], r["as_of_date"]) for r in rows}
    for (account, start, end), record in sorted(periods.items()):
        ladder = record["ladder"]
        if ladder is None:
            continue
        for current, balance in sorted(ladder.items()):
            if (cutoff is not None and current > cutoff) or (
                (account, current.isoformat()) in have
            ):
                continue
            debit = balance < 0
            if not debit and current in interest_days.get(account, ()):
                # trade-date cash >= 0 does not prove a zero loan on a day interest was
                # charged for (by any statement of the account): only a reconciled table
                # proves such a day zero, and that path has already run
                continue
            have.add((account, current.isoformat()))
            ladder_row: dict[str, Any] = {
                "account_id": account,
                "as_of_date": current.isoformat(),
                "amount": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": str(-balance) if debit else "0",
                },
                "classification": FINANCING_CLASSIFICATION if debit else CLASSIFICATION,
                "measurement_basis": BASIS,
                "provider_id": provider_id,
                "source_artifact_id": f"{prefix}/{record['name']}",
                "source_artifact_sha256": record["digest"],
                "source_field": "Transaction Details: end-of-day cash tied to printed cash",
                "source_page": record["details_page"] or record["page"],
                "verdict_reason": "CASH_LADDER_CLOSES",
            }
            if debit:
                ladder_row["amount_measurement"] = DAY_BALANCE_DERIVED
            rows.append(ladder_row)
    # A statement that prints a non-zero closing margin loan balance carries an exact
    # borrowing fact for its last day, whatever the rest of the month proves.
    have = {(r["account_id"], r["as_of_date"]) for r in rows}
    for (account, _start, end), record in periods.items():
        closing = record["loan_closing"]
        if record["state"] != "FINANCING" or not closing:
            continue
        if cutoff is not None and end > cutoff:
            continue
        clash = [
            r
            for r in rows
            if r["account_id"] == account and r["as_of_date"] == end.isoformat()
        ]
        rows[:] = [r for r in rows if r not in clash]  # the printed balance wins
        rows.append(
            {
                "account_id": account,
                "as_of_date": end.isoformat(),
                "amount": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": str(abs(closing)),
                },
                "classification": FINANCING_CLASSIFICATION,
                "measurement_basis": BASIS,
                "provider_id": provider_id,
                "source_artifact_id": f"{prefix}/{record['name']}",
                "source_artifact_sha256": record["digest"],
                "source_field": "Margin Loan Information: closing margin loan balance",
                "source_page": record["page"],
                "verdict_reason": "STATEMENT_CLOSING_LOAN_BALANCE",
                "amount_measurement": STATEMENT_CLOSING_BALANCE,
            }
        )
    reconciliation = sorted(
        (
            {
                "account_id": account,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "reconciled": record["coverage"] is not None,
                "reason": record["refine_reason"],
                "cash_ladder": record["ladder_reason"],
            }
            for (account, start, end), record in periods.items()
            if record["refine_reason"] is not None
        ),
        key=lambda r: (r["account_id"], r["period_start"]),
    )
    removed = sorted(
        (
            {"account_id": a, "as_of_date": d.isoformat()}
            for a, days in interest_days.items()
            for d in days
        ),
        key=lambda r: (r["account_id"], r["as_of_date"]),
    )
    keys = [(r["account_id"], r["as_of_date"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ZeroFinancingError("two statements cover the same account day")
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": ARTIFACT_ID,
        "provider_id": provider_id,
        "rows": sorted(rows, key=lambda r: (r["account_id"], r["as_of_date"])),
        "unobserved_periods": sorted(
            unobserved, key=lambda r: (r["account_id"], r["period_start"])
        ),
        "statements": len(names),
        "statement_manifest": [
            {"name": name, "sha256": sums[name], "size_bytes": sizes[name]}
            for name in names
        ],
        "accounts": sorted(seen_accounts),
        "duplicate_period_files": sorted(duplicate_periods),
        "interest_charged_days": removed,
        "interest_reconciliation": reconciliation,
        "coverage_gaps": _gaps(periods),
        "capture_decisions": sorted(capture_notes, key=lambda n: n["account_id"]),
    }


def _gaps(
    periods: Mapping[tuple[str, date, date], Any],
) -> list[dict[str, str]]:
    """Days between an account's statement periods: never observed by any statement."""

    by_account: dict[str, list[tuple[date, date]]] = {}
    for account, start, end in periods:
        by_account.setdefault(account, []).append((start, end))
    out: list[dict[str, str]] = []
    for account, spans in sorted(by_account.items()):
        spans.sort()
        for (_s1, e1), (s2, _e2) in zip(spans, spans[1:], strict=False):
            if s2 > e1 + timedelta(days=1):
                out.append(
                    {
                        "account_id": account,
                        "gap_start": (e1 + timedelta(days=1)).isoformat(),
                        "gap_end": (s2 - timedelta(days=1)).isoformat(),
                    }
                )
    return out


def canonical(artifact: Mapping[str, Any]) -> bytes:
    return (json.dumps(artifact, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _amount_fits(classification: str, amount: object) -> bool:
    """A zero day carries exactly zero; a financing day a positive range average."""

    if not isinstance(amount, dict) or set(amount) != {"currency", "status", "value"}:
        return False
    if amount["currency"] != "USD" or amount["status"] != "AVAILABLE":
        return False
    if classification == CLASSIFICATION:
        return bool(amount["value"] == "0")
    try:
        return Decimal(str(amount["value"])) > 0
    except ArithmeticError:
        return False


def validate_rows(artifact: object, accounts: set[str]) -> list[Mapping[str, Any]]:
    """The rows of an artifact the ledger may use, or a refusal."""

    if (
        not isinstance(artifact, dict)
        or artifact.get("artifact_id") != ARTIFACT_ID
        or artifact.get("schema_version") != SCHEMA_VERSION
        or not isinstance(artifact.get("rows"), list)
        or not isinstance(artifact.get("provider_id"), str)
        or not artifact["provider_id"].strip()
    ):
        raise ZeroFinancingError("zero-financing artifact shape differs")
    keys = {
        "account_id",
        "as_of_date",
        "amount",
        "classification",
        "measurement_basis",
        "provider_id",
        "source_artifact_id",
        "source_artifact_sha256",
        "source_field",
        "source_page",
        "verdict_reason",
    }
    seen: set[tuple[str, str]] = set()
    for row in artifact["rows"]:
        if not isinstance(row, dict) or set(row) not in (
            keys,
            keys | {"amount_measurement"},
        ):
            raise ZeroFinancingError("zero-financing row keys differ")
        financing = row.get("classification") == FINANCING_CLASSIFICATION
        if financing != ("amount_measurement" in row) or (
            financing
            and row["amount_measurement"]
            not in (RANGE_AVERAGE, STATEMENT_CLOSING_BALANCE, DAY_BALANCE_DERIVED)
        ):
            raise ZeroFinancingError("financing-day row must declare RANGE_AVERAGE")
        try:
            date.fromisoformat(row["as_of_date"])
        except (TypeError, ValueError) as exc:
            raise ZeroFinancingError("zero-financing row date is invalid") from exc
        amount = row["amount"]
        if (
            row["account_id"] not in accounts
            or row["measurement_basis"] != BASIS
            or row["classification"] not in (CLASSIFICATION, FINANCING_CLASSIFICATION)
            or row["provider_id"] != artifact.get("provider_id")
            or not _amount_fits(row["classification"], amount)
            or not isinstance(row["source_artifact_id"], str)
            or not row["source_artifact_id"].strip()
            or not isinstance(row["source_field"], str)
            or not row["source_field"].strip()
            or not isinstance(row["source_artifact_sha256"], str)
            or _SHA.fullmatch(row["source_artifact_sha256"]) is None
            or not isinstance(row["source_page"], int)
            or isinstance(row["source_page"], bool)
            or row["source_page"] < 1
        ):
            raise ZeroFinancingError("zero-financing row is invalid")
        key = (row["account_id"], row["as_of_date"])
        if key in seen:
            raise ZeroFinancingError("zero-financing rows repeat an account day")
        seen.add(key)
    return cast(list[Mapping[str, Any]], artifact["rows"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--sums", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--mapping-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    mapping_raw = args.mapping.read_bytes()
    if hashlib.sha256(mapping_raw).hexdigest() != args.mapping_sha256:
        raise ZeroFinancingError("mapping identity differs")
    artifact = build(
        args.pdf_dir,
        load_sums(args.sums.read_bytes()),
        load_mapping(mapping_raw, args.provider_id),
        provider_id=args.provider_id,
    )
    args.out.write_bytes(canonical(artifact))
    print(
        json.dumps(
            {
                "rows": len(artifact["rows"]),
                "unobserved_periods": len(artifact["unobserved_periods"]),
                "accounts": len(artifact["accounts"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroFinancingError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
