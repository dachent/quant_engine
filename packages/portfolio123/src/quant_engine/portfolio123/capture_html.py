"""Fail-closed parser for the Portfolio123 strategy transactions HTML list.

The page ``GET /transact_list.jsp?portid=<id>&offset=<n>`` is server-rendered HTML:
one ``TableData`` table with a pager row, a ``rowTwo`` header row, and up to 50 data
rows of 11 cells.  This module reads exactly that structure and nothing else, with
the standard-library ``html.parser`` only.  Anything unexpected refuses the page:
a missing or second table, a stray or implied close inside the table, a changed
header, a wrong cell count or cell structure, a transaction type outside the
reviewed vocabulary, a page-size selection other than 50, links bound to another
strategy, a declared range that disagrees with the request offset or the row count
(a short page that is not the last, rows beyond the declared end), and an empty page.

Identifiers (the strategy id in links, the per-row sequence number, link targets)
are checked and then dropped: they never reach a normalized record.  Messages carry
a stable code only, never page content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit


ROWS_PER_PAGE = 50
MAX_HTML_BYTES = 16 * 1024 * 1024
TABLE_CLASS = "TableData"
HEADER = (
    "",
    "Date",
    "Type",
    "Ticker",
    "Shares",
    "Price",
    "Amount",
    "Tot Fees",
    "Rank",
    "Note",
    "TRADE",
)
TYPES = ("BUY", "SELL", "DIV", "SPLIT")
_TICKER = re.compile(r"[A-Za-z0-9.^\-]{1,15}")
_MARKET = re.compile(r":([A-Za-z]{1,8})")
_NUMBER = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_SEQUENCE = re.compile(r"\d{1,12}")
_US_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})")
_POSITION = re.compile(r"(\d{1,9}) - (\d{1,9}) of")
_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_STRUCTURAL = frozenset({"table", "thead", "tbody", "tr", "th", "td"})
_PAGER_TITLES = frozenset({"First Page", "Previous Page", "Next Page", "Last Page"})
MAX_NOTE = 200


class HtmlRefusal(ValueError):
    """The page is not exactly the reviewed structure; ``code`` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HtmlTable:
    """Catalog description of a reviewed HTML table route."""

    header: tuple[str, ...]
    rows_per_page: int
    page_param: str
    id_param: str
    types: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "header": list(self.header),
            "rows_per_page": self.rows_per_page,
            "page_param": self.page_param,
            "id_param": self.id_param,
            "types": list(self.types),
        }


TRANSACTIONS_TABLE = HtmlTable(HEADER, ROWS_PER_PAGE, "offset", "portid", TYPES)


@dataclass(frozen=True)
class TransactionsPage:
    """One parsed page: normalized rows plus checks-only facts."""

    first: int
    last: int
    total: int
    rows: tuple[dict[str, Any], ...]
    sequences: tuple[str, ...]


# --------------------------------------------------------------------------- tree


class _El:
    __slots__ = ("tag", "attrs", "children", "closed", "stray", "dup_attr")

    def __init__(self, tag: str, attrs: dict[str, str], dup_attr: bool) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_El | str] = []
        self.closed = False
        self.stray = 0
        self.dup_attr = dup_attr

    def elements(self) -> list[_El]:
        return [c for c in self.children if isinstance(c, _El)]

    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def walk(self) -> list[_El]:
        found: list[_El] = []
        for child in self.elements():
            found.append(child)
            found.extend(child.walk())
        return found

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return _norm("".join(parts))

    def own_text(self) -> str:
        return _norm("".join(c for c in self.children if isinstance(c, str)))


def _norm(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", {}, False)
        self.stack: list[_El] = [self.root]

    def _new(self, tag: str, attrs: list[tuple[str, str | None]]) -> _El:
        values: dict[str, str] = {}
        duplicate = False
        for key, value in attrs:
            if key in values:
                duplicate = True
            values[key] = value or ""
        element = _El(tag, values, duplicate)
        self.stack[-1].children.append(element)
        return element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = self._new(tag, attrs)
        if tag in _VOID:
            element.closed = True
        else:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._new(tag, attrs).closed = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                return
        self.stack[-1].stray += 1

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _tree(body: bytes) -> _El:
    if len(body) > MAX_HTML_BYTES:
        raise HtmlRefusal("HTML_TOO_LARGE")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HtmlRefusal("HTML_NOT_UTF8") from None
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------- cells


def _only_whitespace_text(element: _El) -> None:
    if any(isinstance(c, str) and c.strip() for c in element.children):
        raise HtmlRefusal("TABLE_STRUCTURE")


def _number(text: str) -> float | None:
    if not text:
        return None
    if _NUMBER.fullmatch(text) is None:
        raise HtmlRefusal("NUMBER_UNREVIEWED")
    try:
        exact = Decimal(text.replace(",", ""))
        value = float(exact)
    except (InvalidOperation, ValueError, OverflowError):
        raise HtmlRefusal("NUMBER_UNREVIEWED") from None
    # Normalized numbers are JSON floats: refuse any value a float cannot hold exactly
    # (more than about 15 significant digits) instead of silently changing the source.
    if not math.isfinite(value) or Decimal(repr(value)) != exact:
        raise HtmlRefusal("NUMBER_UNREVIEWED")
    return value


def _numeric_cell(cell: _El) -> float | None:
    children = cell.elements()
    if len(children) > 1 or any(c.tag != "span" or c.elements() for c in children):
        raise HtmlRefusal("CELL_STRUCTURE")
    if children and cell.own_text():
        raise HtmlRefusal("CELL_STRUCTURE")
    return _number(cell.text())


def _us_date(text: str) -> date:
    match = _US_DATE.fullmatch(text)
    if match is None:
        raise HtmlRefusal("DATE_UNREVIEWED")
    month, day, year = int(match[1]), int(match[2]), int(match[3])
    if len(match[3]) == 2:
        year += 2000 if year < 70 else 1900
    try:
        return date(year, month, day)
    except ValueError:
        raise HtmlRefusal("DATE_UNREVIEWED") from None


def _link_query(href: str, expected_path: str) -> dict[str, str]:
    parts = urlsplit(href)
    if parts.fragment or parts.scheme or parts.netloc:
        raise HtmlRefusal("LINK_UNREVIEWED")
    if parts.path.lstrip("/") != expected_path.lstrip("/"):
        raise HtmlRefusal("LINK_UNREVIEWED")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    result = dict(pairs)
    if len(result) != len(pairs):
        raise HtmlRefusal("LINK_UNREVIEWED")
    return result


def _date_cell(cell: _El, strategy: str) -> date:
    children = cell.elements()
    if len(children) != 1 or children[0].tag != "a" or children[0].elements():
        raise HtmlRefusal("CELL_STRUCTURE")
    link = children[0]
    if cell.own_text():
        raise HtmlRefusal("CELL_STRUCTURE")
    query = _link_query(link.attrs.get("href", ""), "holdings_history.jsp")
    if set(query) != {"portid", "date"} or query["portid"] != strategy:
        raise HtmlRefusal("LINK_STRATEGY_MISMATCH")
    shown = _us_date(link.text())
    if _us_date(query["date"]) != shown:
        raise HtmlRefusal("DATE_LINK_MISMATCH")
    return shown


def _type_cell(cell: _El) -> str:
    children = cell.elements()
    if len(children) != 1 or children[0].tag != "span" or children[0].elements():
        raise HtmlRefusal("CELL_STRUCTURE")
    if "txt-b" not in children[0].classes() or cell.own_text():
        raise HtmlRefusal("CELL_STRUCTURE")
    value = children[0].text()
    if value not in TYPES:
        raise HtmlRefusal("TYPE_UNREVIEWED")
    return value


def _ticker_cell(cell: _El) -> tuple[str, str]:
    children = cell.elements()
    if len(children) != 1 or children[0].tag != "div" or cell.own_text():
        raise HtmlRefusal("CELL_STRUCTURE")
    box = children[0]
    if "ticker-box" not in box.classes():
        raise HtmlRefusal("CELL_STRUCTURE")
    tickers = [
        c
        for c in box.elements()
        if c.tag == "span" and "ticker-box-ticker" in c.classes()
    ]
    if len(tickers) != 1:
        raise HtmlRefusal("CELL_STRUCTURE")
    smalls = [c for c in tickers[0].elements() if c.tag == "small"]
    if len(smalls) != 1 or len(tickers[0].elements()) != 1:
        raise HtmlRefusal("CELL_STRUCTURE")
    ticker = tickers[0].own_text()
    market = _MARKET.fullmatch(smalls[0].text())
    if _TICKER.fullmatch(ticker) is None or market is None:
        raise HtmlRefusal("TICKER_UNREVIEWED")
    return ticker, market[1]


def _plain_cell(cell: _El) -> str:
    if cell.elements():
        raise HtmlRefusal("CELL_STRUCTURE")
    return cell.text()


def _data_row(row: _El, strategy: str) -> tuple[dict[str, Any], str]:
    _only_whitespace_text(row)
    cells = row.elements()
    if len(cells) != len(HEADER) or any(c.tag != "td" for c in cells):
        raise HtmlRefusal("ROW_CELL_COUNT")
    sequence = _plain_cell(cells[0])
    if _SEQUENCE.fullmatch(sequence) is None:
        raise HtmlRefusal("ROW_SEQUENCE_UNREVIEWED")
    when = _date_cell(cells[1], strategy)
    kind = _type_cell(cells[2])
    ticker, market = _ticker_cell(cells[3])
    note = _plain_cell(cells[9])
    if len(note) > MAX_NOTE:
        raise HtmlRefusal("NOTE_TOO_LONG")
    if cells[10].elements() or cells[10].text():
        raise HtmlRefusal("TRADE_CELL_UNREVIEWED")
    record: dict[str, Any] = {
        "date": when.isoformat(),
        "type": kind,
        "ticker": ticker,
        "market": market,
        "shares": _numeric_cell(cells[4]),
        "price": _numeric_cell(cells[5]),
        "amount": _numeric_cell(cells[6]),
        "tot_fees": _numeric_cell(cells[7]),
        "rank": _numeric_cell(cells[8]),
        "note": note,
    }
    return record, sequence


# --------------------------------------------------------------------------- page


def _find_table(root: _El) -> _El:
    tables = [e for e in root.walk() if e.tag == "table" and TABLE_CLASS in e.classes()]
    if not tables:
        raise HtmlRefusal("TABLE_NOT_FOUND")
    if len(tables) != 1:
        raise HtmlRefusal("TABLE_AMBIGUOUS")
    table = tables[0]
    for element in [table, *table.walk()]:
        if element.dup_attr:
            raise HtmlRefusal("TABLE_STRUCTURE")
        if element.tag in _STRUCTURAL and not element.closed:
            raise HtmlRefusal("TABLE_MALFORMED")
        if element.stray and element.tag != "#root":
            raise HtmlRefusal("TABLE_MALFORMED")
    return table


def _pager(head: _El, strategy: str, offset: int) -> tuple[int, int, int]:
    _only_whitespace_text(head)
    cells = head.elements()
    if len(cells) != 1 or cells[0].tag != "th":
        raise HtmlRefusal("PAGER_UNREVIEWED")
    cell = cells[0]
    if cell.attrs.get("colspan") != str(len(HEADER) + 1) or "tbl-header" not in (
        cell.classes()
    ):
        raise HtmlRefusal("PAGER_UNREVIEWED")
    inner = cell.walk()
    positions = [e for e in inner if e.tag == "span" and "curPos" in e.classes()]
    totals = [
        e for e in inner if e.tag == "span" and e.attrs.get("id") == "resulttotalcnt"
    ]
    if len(positions) != 1 or len(totals) != 1 or totals[0] not in positions[0].walk():
        raise HtmlRefusal("PAGER_UNREVIEWED")
    match = _POSITION.fullmatch(positions[0].own_text())
    count = totals[0].text()
    if match is None or not count.isascii() or not count.isdigit():
        raise HtmlRefusal("PAGER_UNREVIEWED")
    first, last, total = int(match[1]), int(match[2]), int(count)
    for link in (e for e in inner if e.tag == "a"):
        if link.attrs.get("title") == "Download":
            download = _link_query(link.attrs.get("href", ""), "p123/DownloadPortTrans")
            if download != {"portid": strategy}:
                raise HtmlRefusal("LINK_STRATEGY_MISMATCH")
            continue
        if link.attrs.get("title") not in _PAGER_TITLES or link.elements():
            raise HtmlRefusal("PAGER_UNREVIEWED")
        query = _link_query(link.attrs.get("href", ""), "transact_list.jsp")
        if query.get("portid") != strategy or not set(query) <= {"portid", "offset"}:
            raise HtmlRefusal("LINK_STRATEGY_MISMATCH")
        value = query.get("offset", "0")
        if not value.isascii() or not value.isdigit():
            raise HtmlRefusal("PAGER_UNREVIEWED")
        if (
            link.attrs["title"] == "Last Page"
            and int(value) != ((max(total, 1) - 1) // ROWS_PER_PAGE) * ROWS_PER_PAGE
        ):
            raise HtmlRefusal("PAGER_INCONSISTENT")
    selected = [
        o
        for e in inner
        if e.tag == "select" and e.attrs.get("id") == "rowspp"
        for o in e.elements()
        if o.tag == "option" and "selected" in o.attrs
    ]
    if len(selected) > 1 or (
        selected and selected[0].attrs.get("value") != str(ROWS_PER_PAGE)
    ):
        raise HtmlRefusal("ROWS_PER_PAGE_UNREVIEWED")
    if first != offset + 1:
        raise HtmlRefusal("OFFSET_MISMATCH")
    if total < 1 or last != min(offset + ROWS_PER_PAGE, total) or last < first:
        raise HtmlRefusal("PAGER_INCONSISTENT")
    return first, last, total


def parse_transactions_page(
    body: bytes, *, strategy: str, offset: int
) -> TransactionsPage:
    """Parse one page; ``strategy`` and ``offset`` come from the request URL."""

    if not strategy.isascii() or not strategy.isdigit() or offset < 0:
        raise HtmlRefusal("REQUEST_UNREVIEWED")
    table = _find_table(_tree(body))
    _only_whitespace_text(table)
    sections = table.elements()
    if [s.tag for s in sections] != ["thead", "tbody"]:
        raise HtmlRefusal("TABLE_STRUCTURE")
    for section in sections:
        _only_whitespace_text(section)
        if any(r.tag != "tr" for r in section.elements()):
            raise HtmlRefusal("TABLE_STRUCTURE")
    head_rows, data = sections[0].elements(), sections[1].elements()
    if len(head_rows) != 2:
        raise HtmlRefusal("TABLE_STRUCTURE")
    if not data:
        raise HtmlRefusal("EMPTY_PAGE")
    first, last, total = _pager(head_rows[0], strategy, offset)
    header = head_rows[1]
    _only_whitespace_text(header)
    labels = tuple(c.text() for c in header.elements())
    if (
        "rowTwo" not in header.classes()
        or any(c.tag != "th" for c in header.elements())
        or labels != HEADER
    ):
        raise HtmlRefusal("HEADER_UNREVIEWED")
    declared = last - first + 1
    if len(data) > declared:
        raise HtmlRefusal("ROWS_AFTER_DECLARED_END")
    if len(data) < declared:
        raise HtmlRefusal("SHORT_PAGE_NOT_LAST")
    records: list[dict[str, Any]] = []
    sequences: list[str] = []
    for row in data:
        record, sequence = _data_row(row, strategy)
        records.append(record)
        sequences.append(sequence)
    if len(set(sequences)) != len(sequences):
        raise HtmlRefusal("DUPLICATE_ROWS")
    return TransactionsPage(first, last, total, tuple(records), tuple(sequences))
