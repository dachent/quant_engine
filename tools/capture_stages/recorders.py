"""Read-only recorders (#913 S1) for John Hancock, Composer and Schwab statements/history.

They drive a signed-in window through a Playwright-like ``page`` (``url``, ``goto``, ``inner_text``, ``evaluate``,
``click_text``) and NEVER type: they navigate to declared pages, read text, list links and fetch documents with plain GETs.
A login/2FA wall stops the recorder (``StageBlocked("BLOCKED_LOGIN")``) and the owner signs in. Everything is written to a new
directory and only renamed into place when the WHOLE plan succeeded (no partial capture to hash). Hosts, label sets and link
shapes are fixed here, so a link or label that a page offers can never widen what is fetched or clicked.

The page-text and document handling is exactly what was done by hand on 2026-11-11/12 (eight John Hancock quarterly
statements, ten Composer monthly statements, Schwab Transaction History and Statements pages for eight accounts).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import base64
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit
import urllib.request

try:
    from statement_browser import SEGMENT_RE, RESERVED, StageBlocked, is_login_wall
except ImportError:  # pragma: no cover - imported as tools.capture_stages.recorders
    from tools.capture_stages.statement_browser import (
        SEGMENT_RE,
        RESERVED,
        StageBlocked,
        is_login_wall,
    )

JH_ORIGIN = "https://wwwcd.bcomplete.com"
COMPOSER_ORIGIN = "https://app.composer.trade"
SCHWAB_ORIGIN = "https://client.schwab.com"
PLAN_QUERY_RE = re.compile(
    r"planId=[0-9a-f-]{36}&plancode=[A-Z0-9]{3,12}&planGroup=[A-Z]{2,4}"
)
COMPOSER_LABELS = frozenset({"Statements", "Documents"})
CLICK_LABELS = COMPOSER_LABELS | {"Search"}
PRESIGNED_RE = re.compile(
    r"https://[a-z0-9.-]*\.?s3\.amazonaws\.com/[^?#\s]*statements/\d{4}-\d{2}\.pdf\?[^\s]+"
)
MAX_PDF_BYTES = 8_000_000

# Fixed read-only probes run inside the page (no argument is ever interpolated into script text).
LINKS_JS = "() => [...document.querySelectorAll('a')].map(a => [(a.innerText || '').trim().slice(0, 60), a.href]).filter(x => x[1])"
FETCH_JS = """async (url) => {
  const r = await fetch(url, {method: 'GET', credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = ''; for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return [r.status, btoa(s)];
}"""


def _name_ok(name: str) -> bool:
    return (
        bool(SEGMENT_RE.fullmatch(name))
        and not name.endswith(".")
        and name.split(".")[0].upper() not in RESERVED
    )


def _origin(url: str) -> str:
    return urlsplit(url)._replace(path="", query="", fragment="").geturl()


def _check_page(page: Any, text: str, expect: str, allowed: str) -> None:
    wall = is_login_wall(page.url, text)
    if _origin(page.url) != allowed and not wall:
        raise ValueError("REDIRECT_ORIGIN")
    if wall or expect not in text:
        raise StageBlocked("BLOCKED_LOGIN")


class _Staging:
    """Whole-recording-or-nothing directory: files go to ``.<name>.staging`` and are renamed on success."""

    def __init__(self, out_dir: Path) -> None:
        self.out = Path(out_dir)
        if self.out.exists():
            raise FileExistsError(str(self.out))
        self.tmp = self.out.with_name(f".{self.out.name}.staging")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True)

    def write_text(self, name: str, text: str) -> None:
        if not _name_ok(name):
            raise ValueError("PLAN_NAME")
        with (self.tmp / name).open("x", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def write_bytes(self, name: str, data: bytes) -> None:
        if not _name_ok(name):
            raise ValueError("PLAN_NAME")
        with (self.tmp / name).open("xb") as fh:
            fh.write(data)

    def commit(self) -> None:
        self.tmp.rename(self.out)

    def abort(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def pdf_to_text(pdf: Path, *, run: Callable[..., Any] = subprocess.run) -> str:
    """``pdftotext -layout`` of one downloaded statement (the parsers read this layout)."""
    r = run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise ValueError("PDF_TEXT")
    return str(r.stdout)


def _page_fetch_pdf(page: Any, url: str) -> bytes:
    status, b64 = page.evaluate(
        FETCH_JS, url
    )  # a GET inside the signed-in page, same origin only (checked by the caller)
    data = base64.b64decode(b64)
    if status != 200 or not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF_FETCH")
    return data


def _http_get_pdf(url: str) -> bytes:  # pragma: no cover - live only
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - https and host shape are checked by the caller
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        req, timeout=60
    ) as resp:  # noqa: S310
        data: bytes = resp.read(MAX_PDF_BYTES + 1)
    if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF_FETCH")
    return data


def john_hancock_plan(plan_query: str) -> list[dict[str, str]]:
    """Page plan for the participant site. ``plan_query`` (plan id, plan code, group) is supplied at run time and never kept in
    the repository."""
    if not PLAN_QUERY_RE.fullmatch(plan_query):
        raise ValueError("PLAN_QUERY")
    base = JH_ORIGIN
    return [
        {"name": "account_overview", "url": f"{base}/Home/Overview/AccountOverview.aspx?{plan_query}", "expect": "Account Overview"},
        {"name": "statements_archive", "url": f"{base}/Home/StatementsAndReporting/StatementCentral.aspx#/statement-central/statements-archive?{plan_query}", "expect": "Statements"},
    ]  # fmt: skip


def record_john_hancock(
    page: Any,
    plan_query: str,
    out_dir: Path,
    *,
    to_text: Callable[[Path], str] = pdf_to_text,
) -> list[str]:
    """Overview and statements-archive text, then every quarterly statement PDF listed on the archive (GET inside the signed-in
    page, same origin, ``%PDF`` and size checked) with its ``pdftotext`` text, as ``stmt-<n>.pdf`` / ``stmt-<n>.txt``."""
    st = _Staging(out_dir)
    names: list[str] = []
    try:
        for item in john_hancock_plan(plan_query):
            page.goto(item["url"])
            text = page.inner_text("body")
            _check_page(page, text, item["expect"], JH_ORIGIN)
            st.write_text(f"{item['name']}.txt", text)
            names.append(item["name"])
        links = [
            (label, href)
            for label, href in page.evaluate(LINKS_JS)
            if _origin(href) == JH_ORIGIN
            and re.search(r"\.pdf(\?|$)", href, re.I)
            and "statement" in href.lower()
        ]
        if not links:
            raise StageBlocked(
                "BLOCKED_LOGIN"
            )  # an archive with no statements is a page we are not really on
        for i, (_label, href) in enumerate(
            sorted(set(links), key=lambda x: x[1]), start=1
        ):
            pdf = _page_fetch_pdf(page, href)
            st.write_bytes(f"stmt-{i:02d}.pdf", pdf)
            tmp_pdf = st.tmp / f"stmt-{i:02d}.pdf"
            st.write_text(f"stmt-{i:02d}.txt", to_text(tmp_pdf))
            names.append(f"stmt-{i:02d}")
        st.commit()
    except BaseException:
        st.abort()
        raise
    return names


def record_composer(
    page: Any,
    out_dir: Path,
    *,
    http_get: Callable[[str], bytes] = _http_get_pdf,
    to_text: Callable[[Path], str] = pdf_to_text,
) -> list[str]:
    """Portfolio page, then Settings > Documents > Statements (real click on a whitelisted label), then each presigned monthly
    statement link (https, an ``s3.amazonaws.com`` ``statements/YYYY-MM.pdf`` URL only) with its text."""
    st = _Staging(out_dir)
    names: list[str] = []
    try:
        for name, path, expect in (
            ("portfolio", "/portfolio", "Portfolio"),
            ("documents", "/settings/documents", "Documents"),
        ):
            page.goto(COMPOSER_ORIGIN + path)
            text = page.inner_text("body")
            _check_page(page, text, expect, COMPOSER_ORIGIN)
            st.write_text(f"{name}.txt", text)
            names.append(name)
        label = "Statements"
        if (
            label not in COMPOSER_LABELS
        ):  # pragma: no cover - guards a future edit of the constant above
            raise ValueError("LABEL")
        page.click_text(label)
        links = sorted(
            {
                href
                for _t, href in page.evaluate(LINKS_JS)
                if PRESIGNED_RE.fullmatch(href)
            }
        )
        if not links:
            raise StageBlocked("BLOCKED_LOGIN")
        for href in links:
            month = re.search(r"statements/(\d{4}-\d{2})\.pdf", href)
            assert month is not None
            st.write_bytes(f"stmt-{month.group(1)}.pdf", http_get(href))
            st.write_text(
                f"stmt-{month.group(1)}.txt",
                to_text(st.tmp / f"stmt-{month.group(1)}.pdf"),
            )
            names.append(f"stmt-{month.group(1)}")
        st.commit()
    except BaseException:
        st.abort()
        raise
    return names


SELECT_ACCOUNT_JS = """(suffix) => {
  const list = [...document.querySelectorAll('#account-selector-list a')];
  const a = list.find(x => x.innerText.replace(/\\s/g, '').includes(suffix));
  if (!a) return false; a.click(); return true;
}"""


SET_RANGE_JS = """(label) => {
  const sel = document.getElementById('date-range-select-id');
  if (!sel) return false;
  const o = [...sel.options].find(x => x.text.trim() === label);
  if (!o) return false;
  sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); return true;
}"""
STATEMENT_RANGE = "Last 5 years"
SEARCH_LABEL = "Search"
STATEMENT_DATE_RE = re.compile(r"(\d\d)/(\d\d)/(\d{4})	Statements")
MAX_STATEMENTS = 60


def statement_dates(text: str) -> list[str]:
    """ISO dates of the ``Statements`` rows on a Schwab Statements page (newest first, as listed)."""
    return [
        f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        for m in STATEMENT_DATE_RE.finditer(text)
    ]


def record_schwab_statements(
    page: Any,
    accounts: Sequence[str],
    out_dir: Path,
    *,
    to_text: Callable[[Path], str] = pdf_to_text,
) -> list[str]:
    """Per account: Transaction History page text, then the Statements page (account selected by a fixed script, range set to
    ``STATEMENT_RANGE`` by a fixed script, a real click on ``Search``), then every listed statement downloaded through the
    page's download handler (``page.download_pdf(index, dest)``) with its ``pdftotext`` text. Files are
    ``history-<s>.txt``, ``statements-<s>.txt`` and ``statement-<s>-<YYYY-MM-DD>.pdf/.txt``. The statement count on the page must
    equal the number downloaded, and the whole run is renamed into place only when every account succeeded."""
    st = _Staging(out_dir)
    names: list[str] = []
    try:
        for suffix in accounts:
            if not re.fullmatch(r"[0-9]{3,4}", suffix):
                raise ValueError("ACCOUNT_SUFFIX")
            for kind, path in (
                ("history", "/app/accounts/history/#/"),
                ("statements", "/app/accounts/statements/#/"),
            ):
                page.goto(SCHWAB_ORIGIN + path)
                if not page.evaluate(SELECT_ACCOUNT_JS, suffix):
                    raise StageBlocked("BLOCKED_LOGIN")
                if kind == "statements":
                    if not page.evaluate(SET_RANGE_JS, STATEMENT_RANGE):
                        raise StageBlocked("BLOCKED_LOGIN")
                    page.click_text(SEARCH_LABEL)
                text = page.inner_text("body")
                _check_page(page, text, suffix, SCHWAB_ORIGIN)
                st.write_text(f"{kind}-{suffix}.txt", text)
                names.append(f"{kind}-{suffix}")
                if kind == "statements":
                    dates = statement_dates(text)
                    if (
                        not dates
                        or len(dates) > MAX_STATEMENTS
                        or len(set(dates)) != len(dates)
                    ):
                        raise StageBlocked("BLOCKED_LOGIN")
                    for i, d in enumerate(dates):
                        pdf = st.tmp / f"statement-{suffix}-{d}.pdf"
                        page.download_pdf(i, pdf)
                        if not pdf.is_file() or not pdf.read_bytes().startswith(
                            b"%PDF"
                        ):
                            raise ValueError("PDF_FETCH")
                        st.write_text(f"statement-{suffix}-{d}.txt", to_text(pdf))
                        names.append(f"statement-{suffix}-{d}")
        st.commit()
    except BaseException:
        st.abort()
        raise
    return names


def record_schwab_pages(
    page: Any,
    accounts: Sequence[str],
    out_dir: Path,
    *,
    pages: Mapping[str, str] | None = None,
) -> list[str]:
    """For each account suffix: select it in the account selector (a fixed click script), then save the Transaction History
    and Statements pages as text. Downloading the statement PDFs themselves needs the browser's download handler and is done
    by the live runner; this records the pages and their statement list."""
    wanted = dict(
        pages
        or {
            "history": "/app/accounts/history/#/",
            "statements": "/app/accounts/statements/#/",
        }
    )
    st = _Staging(out_dir)
    names: list[str] = []
    try:
        for suffix in accounts:
            if not re.fullmatch(r"[0-9]{3,4}", suffix):
                raise ValueError("ACCOUNT_SUFFIX")
            for kind, path in wanted.items():
                page.goto(SCHWAB_ORIGIN + path)
                if not page.evaluate(SELECT_ACCOUNT_JS, suffix):
                    raise StageBlocked(
                        "BLOCKED_LOGIN"
                    )  # no selector on the page: not signed in, or not the page expected
                text = page.inner_text("body")
                _check_page(page, text, suffix, SCHWAB_ORIGIN)
                st.write_text(f"{kind}-{suffix}.txt", text)
                names.append(f"{kind}-{suffix}")
        st.commit()
    except BaseException:
        st.abort()
        raise
    return names


class PlaywrightPage:  # pragma: no cover - live only
    """The page protocol over a Playwright page attached to the owner's window by CDP. ``click_text`` is a real click on a
    label from ``COMPOSER_LABELS`` only; there is no fill, type or press method."""

    def __init__(self, page: Any) -> None:
        self._p = page

    @property
    def url(self) -> str:
        return str(self._p.url)

    def goto(self, url: str) -> None:
        self._p.goto(url, wait_until="domcontentloaded")
        self._p.wait_for_timeout(6000)

    def inner_text(self, selector: str) -> str:
        return str(self._p.inner_text(selector))

    def evaluate(self, script: str, arg: Any = None) -> Any:
        return (
            self._p.evaluate(script, arg)
            if arg is not None
            else self._p.evaluate(script)
        )

    def click_text(self, label: str) -> None:
        if label not in CLICK_LABELS:
            raise ValueError("LABEL")
        self._p.get_by_text(label, exact=True).first.click()
        self._p.wait_for_timeout(6000)

    def download_pdf(self, index: int, dest: Path) -> None:
        """Click the ``index``-th 'Click to Download PDF' button and save the download through the browser's handler."""
        buttons = self._p.locator(
            "button[aria-label^='Click to Download PDF'], button[title^='Click to Download PDF']"
        )
        with self._p.expect_download(timeout=60000) as info:
            buttons.nth(index).click()
        info.value.save_as(str(dest))


def attach_page(
    port: int, host_suffixes: Sequence[str]
) -> PlaywrightPage:  # pragma: no cover - live only
    """Attach to the signed-in window on ``port`` and return its tab whose host is one of ``host_suffixes``."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    for ctx in browser.contexts:
        for pg in ctx.pages:
            host = (urlsplit(pg.url).hostname or "").lower()
            if any(host == h or host.endswith("." + h) for h in host_suffixes):
                return PlaywrightPage(pg)
    raise StageBlocked("BLOCKED_NO_WINDOW")
