"""Read-only Schwab account-page capture: Transaction History and Balances text plus the page's own XHR.

For each account the tool selects the account with the page's own account selector, saves the page
text, and saves the raw JSON body of the balances XHR (``balances/brokerage``) or the history XHR
(``TransactionHistory/brokerage/transactions``) that the selection made the page send.  It sends no
request of its own, types nothing, and clicks only the account selector and one option whose text is
``...NNN``.  Every file is create-only and hashed in ``SHA256SUMS.txt``.  A page whose own
"HH:MM:SS AM ET, MM/DD/YYYY" stamp is not the declared New York date is refused.

A balances capture also compares, at capture time, the page's ``Cash & Cash Investments Total`` with
the XHR's own cash fields (``marginBalance`` plus the bank-sweep credit it does not carry,
``total``, a non-zero ``tradeDateBalance``); a mismatch (a mid-session balance that does not close)
is refused after its files are saved, unless ``--allow-cash-mismatch`` flags it in the record
instead.  A history capture records the rows it returned against the page's 200-row cap and the
bookmark, and, when ``--reach-back-to`` is given, refuses a capped history whose earliest row does
not precede that date.  Every account's record carries the page stamp, the local capture time and
whether the stamp falls in the regular session; a page that navigated or reloaded while an account
was being captured is refused.

The browser work (``main``) needs the optional ``schwab-live`` extra (Playwright) and a signed-in
window with a loopback debug port; everything else is plain Python and is tested offline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Protocol, Sequence

BALANCES_XHR = "/api/is.Balances/V1/Balances/balances/brokerage"
HISTORY_XHR = "/TransactionHistory/brokerage/transactions"
_KINDS = {"balances": BALANCES_XHR, "history": HISTORY_XHR}
_STAMP = re.compile(r"\d\d:\d\d:\d\d [AP]M ET, (\d\d)/(\d\d)/(\d{4})")
_SELECTED = re.compile(r"Account Selector:.*?ending in ((?:\d ){2}\d) Selected")
_ACCOUNT = re.compile(r"\d{3}")
_BLOCK = ("unable to authorize", "swaf")  # compared against the casefolded page text
_STAMP_TIME = re.compile(r"(\d\d):(\d\d):(\d\d) ([AP])M ET, (\d\d)/(\d\d)/(\d{4})")
_CASH_LABEL = "Cash & Cash Investments Total"
_PAGE_AMOUNT = re.compile(r"\(?-?\$?-?(\d{1,3}(,\d{3})+|\d+)(\.\d\d)?\)?")
HISTORY_ROW_CAP = 200  # the history page returns at most this many rows per search
NO_XHR = "NO_XHR_SEEN_IN_WINDOW"


class CaptureRefused(RuntimeError):
    """The page is not in a state that may be saved."""


def xhr_binding(kind: str, body: bytes, account: str, expected_et_date: str) -> str:
    """Prove the saved XHR body belongs to the selected account (or day) or refuse.

    A balances body names its account (``brokerageAccountList[0].accountId`` ends with the selected
    suffix); a history body carries only its date range, so it is bound to the declared date
    (``toDate``) and recorded as ``DATE_RANGE_ONLY``: it cannot identify the account.
    """

    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise CaptureRefused(f"{kind} XHR body is not JSON") from exc
    if not isinstance(doc, dict):
        raise CaptureRefused(f"{kind} XHR body is not a JSON object")
    if kind == "balances":
        rows = doc.get("brokerageAccountList")
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            raise CaptureRefused("balances XHR body does not hold exactly one account")
        number = rows[0].get("accountId")
        if not isinstance(number, str) or not number.endswith(account):
            raise CaptureRefused(
                f"balances XHR body is not account {account}: the page and the body disagree"
            )
        return "ACCOUNT_ID"
    to_date, from_date = doc.get("toDate"), doc.get("fromDate")
    match = (
        re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", to_date)
        if isinstance(to_date, str)
        else None
    )
    if match is None or not isinstance(from_date, str):
        raise CaptureRefused("history XHR body lacks its fromDate/toDate")
    if f"{match[3]}-{match[1]}-{match[2]}" != expected_et_date:
        raise CaptureRefused(
            f"history XHR toDate {to_date} is not the declared date {expected_et_date}"
        )
    return "DATE_RANGE_ONLY"


def page_cash_total(text: str) -> Decimal | None:
    """The amount printed for ``Cash & Cash Investments Total`` (same line, or alone on the next
    non-blank line); None when the label is absent, repeated or has no amount."""

    lines = text.replace("\r\n", "\n").split("\n")
    where = [n for n, line in enumerate(lines) if _CASH_LABEL in line]
    if len(where) != 1 or not lines[where[0]].strip().startswith(_CASH_LABEL):
        return None
    rest = lines[where[0]].strip()[len(_CASH_LABEL) :].strip()
    if not rest:
        rest = next((x.strip() for x in lines[where[0] + 1 :] if x.strip()), "")
    rest = rest.replace("\u2212", "-")
    if _PAGE_AMOUNT.fullmatch(rest) is None:
        return None
    value = Decimal(re.sub(r"[^\d.]", "", rest))
    return -value if "-" in rest or rest.startswith("(") else value


def _money(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return number if number.is_finite() else None


def cash_check(text: str, body: bytes) -> dict[str, str]:
    """Compare the page's cash total with the balances XHR's own figures (the evaluator's rule: a non-zero
    margin balance, with the sweep, equals the page; the live credit is a positive ``marginBalance`` plus a positive bank-sweep credit, and equals the
    page total and the response's own ``total`` to the cent; a non-zero ``tradeDateBalance`` must
    equal the page total).  ``status`` is ``AGREES`` or the reason the two do not close."""

    page = page_cash_total(text)
    entry = json.loads(body)["brokerageAccountList"][0]
    cash = entry.get("cashInvestments")
    cash = cash if isinstance(cash, dict) else {}
    margin = _money(cash["marginBalance"]) if "marginBalance" in cash else None
    sweep = _money(cash.get("schwabBankSweepFeature") or 0) or Decimal(0)
    total = _money(cash["total"]) if cash.get("total") is not None else None
    info = entry.get("marginsInfo")
    trade = (
        _money(info["tradeDateBalance"])
        if isinstance(info, dict) and info.get("tradeDateBalance") is not None
        else None
    )
    live = margin + sweep if margin is not None and margin > 0 and sweep > 0 else margin
    if page is None:
        status = "PAGE_TOTAL_ABSENT"
    elif margin is None:
        status = "XHR_MARGIN_BALANCE_ABSENT"
    elif margin != 0 and live != page:
        status = "MARGIN_BALANCE_DIFFERS_FROM_PAGE_TOTAL"
    elif margin == 0 and page != 0 and total is None:
        # a zero margin balance (a pending-trade credit) is only explained by the response's own total
        status = "XHR_TOTAL_ABSENT_FOR_ZERO_MARGIN_BALANCE"
    elif total is not None and total != page:
        status = "XHR_TOTAL_DIFFERS_FROM_PAGE_TOTAL"
    elif trade is not None and trade != 0 and trade != page:
        status = "TRADE_DATE_BALANCE_DIFFERS_FROM_PAGE_TOTAL"
    else:
        status = "AGREES"

    def show(value: Decimal | None) -> str:
        return "ABSENT" if value is None else format(value, "f")

    return {
        "cash_check": status,
        "page_cash_total": show(page),
        "xhr_margin_balance": show(margin),
        "xhr_bank_sweep": show(sweep),
        "xhr_total": show(total),
        "xhr_trade_date_balance": show(trade),
    }


def history_stats(body: bytes, cap: int = HISTORY_ROW_CAP) -> dict[str, str]:
    """Rows the history body holds against the page's row cap, its bookmark, and its earliest row
    date (ISO).  ``capped`` when more pages exist (a bookmark) or the row count reached the cap."""

    doc = json.loads(body)
    rows = doc.get("brokerageTransactions")
    rows = rows if isinstance(rows, list) else []
    dates = sorted(
        f"{m[3]}-{m[1]}-{m[2]}"
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("transactionDate"), str)
        and (m := re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", row["transactionDate"]))
    )
    capped = bool(doc.get("bookmark")) or len(rows) >= cap
    return {
        "history_rows": str(len(rows)),
        "history_row_cap": str(cap),
        "history_bookmark": "PRESENT" if doc.get("bookmark") else "ABSENT",
        "history_capped": "YES" if capped else "NO",
        "history_earliest_row": dates[0] if dates else "NONE",
    }


def session_of(text: str) -> dict[str, str]:
    """The page's own stamp and whether it falls in the regular New York session (a weekday,
    09:30 to 16:00 ET; market holidays are not known here)."""

    match = _STAMP_TIME.search(text)
    if match is None:
        return {"page_stamp": "ABSENT", "market_session": "UNKNOWN"}
    hour = int(match[1]) % 12 + (12 if match[4] == "P" else 0)
    minutes = hour * 60 + int(match[2])
    weekday = date(int(match[7]), int(match[5]), int(match[6])).weekday() < 5
    regular = weekday and 9 * 60 + 30 <= minutes < 16 * 60
    return {
        "page_stamp": match[0],
        "market_session": "REGULAR_SESSION" if regular else "OUTSIDE_REGULAR_SESSION",
    }


def et_date(text: str) -> str | None:
    """The page's own stamp as an ISO date (the stamp reads MM/DD/YYYY), or None."""

    match = _STAMP.search(text)
    return f"{match[3]}-{match[1]}-{match[2]}" if match else None


def selected_suffix(text: str) -> str | None:
    """The last three digits of the account the selector says is selected."""

    match = _SELECTED.search(re.sub(r"\s+", " ", text))
    return match[1].replace(" ", "") if match else None


@dataclass
class XhrLog:
    """The JSON bodies the page itself received, in arrival order (url without query)."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def add(self, method: str, url: str, status: int, body: bytes) -> None:
        if body[:1] not in (b"{", b"["):
            return
        self.entries.append(
            {
                "n": len(self.bodies),
                "method": method,
                "url": url.split("?")[0],
                "status": status,
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        self.bodies.append(body)

    def count(self, kind: str) -> int:
        return sum(1 for e in self.entries if _KINDS[kind] in e["url"])

    def latest(self, kind: str) -> bytes | None:
        for entry in reversed(self.entries):
            if _KINDS[kind] in entry["url"]:
                return self.bodies[int(entry["n"])]
        return None


class Page(Protocol):
    def text(self) -> str: ...

    def open_selector(self) -> None: ...

    def pick_option(self, suffix: str) -> None: ...

    def navigations(self) -> int:
        """How many times the page navigated or reloaded so far (optional: a page without it is
        not watched)."""
        ...


def _write_new(path: Path, data: bytes) -> str:
    with path.open("xb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


def capture_accounts(
    page: Page,
    xhr: XhrLog,
    kind: str,
    accounts: Sequence[str],
    out: Path,
    *,
    expected_et_date: str,
    pause: Callable[[float], None] = time.sleep,
    seconds: float = 5.0,
    reach_back_to: str | None = None,
    allow_cash_mismatch: bool = False,
    sink: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Select each account, check the page, save its text and the XHR the selection caused.

    ``sink`` receives every record as it is made, so a refusal mid-run keeps the earlier accounts'
    records.  A balances capture whose page cash total does not close to the XHR is refused after
    its files are saved (``allow_cash_mismatch`` records the status and goes on); a capped history
    that does not reach before ``reach_back_to`` (ISO date) is refused the same way."""

    if kind not in _KINDS:
        raise CaptureRefused(f"unknown kind {kind!r}")
    if reach_back_to is not None:
        try:
            reach_back_to = date.fromisoformat(reach_back_to).isoformat()
        except ValueError as exc:
            raise CaptureRefused(
                f"--reach-back-to {reach_back_to!r} is not an ISO date (YYYY-MM-DD)"
            ) from exc
    out.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    for account in accounts:
        if _ACCOUNT.fullmatch(account) is None:
            raise CaptureRefused(f"account {account!r} is not three digits")
        before = xhr.count(kind)
        navigations = getattr(page, "navigations", None)
        navigated_before = navigations() if callable(navigations) else None
        page.open_selector()
        pause(seconds)
        page.pick_option(account)
        pause(seconds)
        text = page.text()
        if any(word in text.casefold() for word in _BLOCK):
            raise CaptureRefused("the page shows an authorization block: stop")
        if selected_suffix(text) != account:
            raise CaptureRefused(
                f"selector does not show account {account} as selected"
            )
        if et_date(text) != expected_et_date:
            raise CaptureRefused(
                f"page stamp {et_date(text)} is not {expected_et_date}"
            )
        if (
            navigated_before is not None
            and callable(navigations)
            and navigations() != navigated_before
        ):
            raise CaptureRefused(
                f"the page navigated or reloaded while account {account} was captured: "
                "keep the page still during a capture and start again"
            )
        body = xhr.latest(kind) if xhr.count(kind) > before else None
        # refuse before anything of this account is written: a body of another account is never stored
        binding = (
            xhr_binding(kind, body, account, expected_et_date)
            if body is not None
            else NO_XHR
        )
        captured = int(time.time())
        header = f"CAPTURED_LOCAL: {captured}\n-----\n"
        record = {"account": account, "kind": kind, "stamp_date": expected_et_date}
        record["captured_local_epoch"] = str(captured)
        record.update(session_of(text))
        record["text_sha256"] = _write_new(
            out / f"schwab-{kind}-{account}.txt", (header + text).encode()
        )
        record["xhr_binding"] = binding
        record["xhr_sha256"] = (
            _write_new(out / f"schwab-{kind}-{account}.json", body)
            if body is not None
            else NO_XHR
        )
        problem: str | None = None
        if body is not None and kind == "balances":
            try:
                record.update(cash_check(text, body))
            except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                record.update(
                    cash_check="XHR_UNREADABLE",
                    page_cash_total="ABSENT",
                    xhr_margin_balance="ABSENT",
                    xhr_bank_sweep="ABSENT",
                    xhr_total="ABSENT",
                    xhr_trade_date_balance="ABSENT",
                )
            if record["cash_check"] != "AGREES" and not allow_cash_mismatch:
                problem = (
                    f"account {account}: the page's cash total does not close to the balances "
                    f"XHR ({record['cash_check']}: page {record['page_cash_total']}, "
                    f"marginBalance {record['xhr_margin_balance']} + sweep "
                    f"{record['xhr_bank_sweep']}, total {record['xhr_total']}, trade-date "
                    f"{record['xhr_trade_date_balance']}); the capture was taken while the "
                    f"balance was moving ({record['market_session']}): take it again into a new --out"
                )
        if body is not None and kind == "history":
            record.update(history_stats(body))
            if (
                reach_back_to is not None
                and record["history_capped"] == "YES"
                and (
                    record["history_earliest_row"] == "NONE"
                    or record["history_earliest_row"] >= reach_back_to
                )
            ):
                problem = (
                    f"account {account}: the history hit its {record['history_row_cap']}-row cap "
                    f"({record['history_rows']} rows, bookmark {record['history_bookmark']}) and its "
                    f"earliest row {record['history_earliest_row']} does not precede "
                    f"{reach_back_to}: narrow the search window and capture again into a new --out"
                )
        records.append(record)
        if sink is not None:
            sink.append(record)
        if problem is not None:
            raise CaptureRefused(problem)
    return records


def finalize(out: Path, xhr: XhrLog) -> str:
    """Write the XHR index and the hash list of whatever was saved (also after a refusal)."""

    index = out / "xhr-index.json"
    if not index.exists():
        _write_new(index, json.dumps(xhr.entries, indent=1).encode())
    return write_sums(out)


def write_sums(out: Path) -> str:
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
        for p in sorted(out.iterdir())
        if p.is_file() and p.name != "SHA256SUMS.txt"
    ]
    return _write_new(out / "SHA256SUMS.txt", ("\n".join(lines) + "\n").encode())


def _parser() -> argparse.ArgumentParser:  # pragma: no cover - live only
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--et-date",
        required=True,
        help="the New York date every page must carry (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--reach-back-to",
        help="ISO date: a history capped at 200 rows must have its earliest row before it "
        "(the start of the final statement period)",
    )
    parser.add_argument(
        "--allow-cash-mismatch",
        action="store_true",
        help="record a page-cash/XHR mismatch in the record instead of refusing it",
    )
    parser.add_argument(
        "--accounts",
        required=True,
        help="comma list of three-digit account suffixes, in capture order",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live only
    import importlib

    args = _parser().parse_args(argv)
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError:
        print("REFUSED: Playwright is required (uv sync --extra schwab-live)")
        return 2
    xhr = XhrLog()
    sink: list[dict[str, str]] = []
    accounts = [a for a in args.accounts.split(",") if a]
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url)
        pages = [
            p for c in browser.contexts for p in c.pages if "client.schwab.com" in p.url
        ]
        if len(pages) != 1:
            print("REFUSED: exactly one client.schwab.com tab must be open")
            return 2
        page = pages[0]

        def on_response(response: Any) -> None:
            if (
                response.request.resource_type in ("xhr", "fetch")
                and "schwab.com" in response.url
            ):
                try:
                    xhr.add(
                        response.request.method,
                        response.url,
                        response.status,
                        response.body(),
                    )
                except Exception:  # noqa: BLE001 - a body the browser dropped is simply not saved
                    return

        page.on("response", on_response)
        navigated = [0]
        page.on(
            "load",
            lambda frame: navigated.__setitem__(
                0, navigated[0] + (1 if frame == page else 0)
            ),
        )

        class Live:
            def text(self) -> str:
                return str(page.evaluate("document.body.innerText"))

            def open_selector(self) -> None:
                page.locator("#account-selector").click()

            def pick_option(self, suffix: str) -> None:
                page.get_by_text(f"\u2026{suffix}", exact=True).first.click()

            def navigations(self) -> int:
                return navigated[0]

        try:
            for kind, url in (
                ("balances", "https://client.schwab.com/app/accounts/balances/#/"),
                ("history", "https://client.schwab.com/app/accounts/history/#/"),
            ):
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(8)
                capture_accounts(
                    Live(),
                    xhr,
                    kind,
                    accounts,
                    args.out,
                    expected_et_date=args.et_date,
                    reach_back_to=args.reach_back_to,
                    allow_cash_mismatch=args.allow_cash_mismatch,
                    sink=sink,
                )
        finally:  # a refusal mid-run still leaves an index, the records and hashes of what was saved
            record_path = args.out / "capture-records.json"
            if sink and not record_path.exists():
                _write_new(record_path, json.dumps(sink, indent=1).encode())
            print(finalize(args.out, xhr))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
