"""Page actions inside the signed-in Schwab window for any non-trading read.

Allowed: navigate within ``https://client.schwab.com/``, click, scroll, select an option, download.
ONE guard: never act on a control whose accessible name, text, label or target address matches the
trading pattern (trade, transfer, withdraw, deposit, order, buy, sell, submit, confirm payment), and
never touch a submit or password control. There is no typing API, so credentials and 2FA are never
entered. Every action is logged value-free: the verb, the role and the first 8 hex characters of the
SHA-256 of the label, never the label (it can hold an account name or number).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ALLOWED_PREFIX = "https://client.schwab.com/"
MAX_SCROLL = 5000
# trading, transfer, withdraw, deposit, order, buy, sell, submit, confirm payment
TRADING = re.compile(
    r"(trad(e|es|ing)|transfer|withdraw|deposit|order|buy|sell|submit|confirm\s+payment)",
    re.IGNORECASE,
)
_BLOCKED_TYPES = frozenset({"submit", "password"})


_FACTS_SCRIPT = "e => ({type: e.type || null, href: e.href || null})"


class ActionRefused(ValueError):
    """The action targets a trading control or leaves the Schwab window."""


def label_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]


def refuse_if_trading(*texts: object) -> None:
    for text in texts:
        if isinstance(text, str) and TRADING.search(text):
            raise ActionRefused(
                "refused: the control matches the trading guard "
                f"(label {label_hash(text)}); only non-trading reads are allowed"
            )


class SchwabPage:
    """The guarded actions over a Playwright page (or any object with the same methods)."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self.log: list[dict[str, str]] = []

    def _record(self, verb: str, role: str, label: str) -> None:
        self.log.append({"action": verb, "role": role, "label_sha8": label_hash(label)})

    def _control(self, role: str, name: str) -> Any:
        refuse_if_trading(name)
        # exact: a substring match could resolve a control whose full name holds a blocked term
        control = self._page.get_by_role(role, name=name, exact=True).first
        # the EFFECTIVE type and the resolved href (a typeless <button> in a form submits)
        facts = control.evaluate(_FACTS_SCRIPT)
        kind = facts.get("type") if isinstance(facts, dict) else None
        href = facts.get("href") if isinstance(facts, dict) else None
        if isinstance(kind, str) and kind.casefold() in _BLOCKED_TYPES:
            raise ActionRefused(
                f"refused: a {kind} control (label {label_hash(name)}) is never touched"
            )
        if isinstance(href, str) and href and not href.startswith(ALLOWED_PREFIX):
            raise ActionRefused(
                f"refused: a link leaving {ALLOWED_PREFIX} (label {label_hash(name)})"
            )
        refuse_if_trading(
            control.inner_text(),
            href,
            control.get_attribute("aria-label"),
            control.get_attribute("value"),
            control.get_attribute("href"),
            control.get_attribute("title"),
        )
        return control

    def navigate(self, url: str) -> None:
        if not url.startswith(ALLOWED_PREFIX):
            raise ActionRefused(f"refused: only {ALLOWED_PREFIX} pages are opened")
        refuse_if_trading(url[len(ALLOWED_PREFIX) :])
        self._record("navigate", "url", url)
        self._page.goto(url, wait_until="domcontentloaded")

    def click(self, role: str, name: str) -> None:
        control = self._control(role, name)
        self._record("click", role, name)
        control.click()

    def scroll(self, delta_y: int) -> None:
        if not isinstance(delta_y, int) or abs(delta_y) > MAX_SCROLL:
            raise ActionRefused(f"refused: a scroll is at most {MAX_SCROLL} pixels")
        self._record("scroll", "page", str(delta_y))
        self._page.mouse.wheel(0, delta_y)

    def select(self, role: str, name: str, option: str) -> None:
        control = self._control(role, name)
        refuse_if_trading(option)
        self._record("select", role, f"{name}|{option}")
        control.select_option(label=option)

    def download(self, role: str, name: str, destination: Path) -> str:
        """Click a download control; the file is saved create-only. Returns the file's SHA-256."""

        control = self._control(role, name)
        if destination.exists():
            raise ActionRefused("refused: the download destination already exists")
        self._record("download", role, name)
        with self._page.expect_download() as info:
            control.click()
        info.value.save_as(str(destination))
        return hashlib.sha256(destination.read_bytes()).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-url", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    nav = sub.add_parser("navigate")
    nav.add_argument("url")
    click = sub.add_parser("click")
    click.add_argument("--role", required=True)
    click.add_argument("--name", required=True)
    scroll = sub.add_parser("scroll")
    scroll.add_argument("delta_y", type=int)
    select = sub.add_parser("select")
    select.add_argument("--role", required=True)
    select.add_argument("--name", required=True)
    select.add_argument("--option", required=True)
    download = sub.add_parser("download")
    download.add_argument("--role", required=True)
    download.add_argument("--name", required=True)
    download.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live only
    args = _parser().parse_args(argv)
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError:
        print(json.dumps({"error": "Playwright is required"}))
        return 2
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url)
        pages = [
            p
            for c in browser.contexts
            for p in c.pages
            if p.url.startswith(ALLOWED_PREFIX)
        ]
        if len(pages) != 1:
            print(
                json.dumps({"error": f"expected one Schwab page, found {len(pages)}"})
            )
            return 2
        page = SchwabPage(pages[0])
        result: dict[str, object] = {}
        try:
            if args.command == "navigate":
                page.navigate(args.url)
            elif args.command == "click":
                page.click(args.role, args.name)
            elif args.command == "scroll":
                page.scroll(args.delta_y)
            elif args.command == "select":
                page.select(args.role, args.name, args.option)
            else:
                result["sha256"] = page.download(args.role, args.name, args.out)
        except ActionRefused as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        result["log"] = page.log
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
