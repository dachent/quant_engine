"""Read-only Schwab account-page capture: Transaction History and Balances text plus the page's own XHR.

For each account the tool selects the account with the page's own account selector, saves the page
text, and saves the raw JSON body of the balances XHR (``balances/brokerage``) or the history XHR
(``TransactionHistory/brokerage/transactions``) that the selection made the page send.  It sends no
request of its own, types nothing, and clicks only the account selector and one option whose text is
``...NNN``.  Every file is create-only and hashed in ``SHA256SUMS.txt``.  A page whose own
"HH:MM:SS AM ET, MM/DD/YYYY" stamp is not the declared New York date is refused.

The browser work (``main``) needs the optional ``schwab-live`` extra (Playwright) and a signed-in
window with a loopback debug port; everything else is plain Python and is tested offline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
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
) -> list[dict[str, str]]:
    """Select each account, check the page, save its text and the XHR the selection caused."""

    if kind not in _KINDS:
        raise CaptureRefused(f"unknown kind {kind!r}")
    out.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    for account in accounts:
        if _ACCOUNT.fullmatch(account) is None:
            raise CaptureRefused(f"account {account!r} is not three digits")
        before = xhr.count(kind)
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
        body = xhr.latest(kind) if xhr.count(kind) > before else None
        # refuse before anything of this account is written: a body of another account is never stored
        binding = (
            xhr_binding(kind, body, account, expected_et_date)
            if body is not None
            else NO_XHR
        )
        header = f"CAPTURED_LOCAL: {int(time.time())}\n-----\n"
        record = {"account": account, "kind": kind, "stamp_date": expected_et_date}
        record["text_sha256"] = _write_new(
            out / f"schwab-{kind}-{account}.txt", (header + text).encode()
        )
        record["xhr_binding"] = binding
        record["xhr_sha256"] = (
            _write_new(out / f"schwab-{kind}-{account}.json", body)
            if body is not None
            else NO_XHR
        )
        records.append(record)
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

        class Live:
            def text(self) -> str:
                return str(page.evaluate("document.body.innerText"))

            def open_selector(self) -> None:
                page.locator("#account-selector").click()

            def pick_option(self, suffix: str) -> None:
                page.get_by_text(f"\u2026{suffix}", exact=True).first.click()

        try:
            for kind, url in (
                ("balances", "https://client.schwab.com/app/accounts/balances/#/"),
                ("history", "https://client.schwab.com/app/accounts/history/#/"),
            ):
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(8)
                capture_accounts(
                    Live(), xhr, kind, accounts, args.out, expected_et_date=args.et_date
                )
        finally:  # a refusal mid-run still leaves an index and hashes for what was saved
            print(finalize(args.out, xhr))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
