"""Fetch a FIXED allowlist of read-only Portfolio123 exports through the owner's tab.

The owner has already signed in to a dedicated Edge or Chrome profile with a loopback
debug port. This tool attaches to that one signed-in tab and, for every entry of a
hash-pinned allowlist, navigates the tab to the exact export URL. The browser saves the
download into a new directory; the bytes are kept UNMODIFIED, renamed only to the
book-id-based name the generation reader expects (``raw/strategy_<id>_<kind>.csv``),
and recorded in a value-free manifest (sha256, size, row count). Contents are never
printed. Hand the files to the staging CLI afterwards (``docs/providers/p123-capture.md``).

Transport allow-list, enforced before a message leaves the process: target discovery
and attach, ``Page.navigate`` to an exact allowlisted URL, and
``Browser.setDownloadBehavior`` to the one new download directory. Nothing else:
no ``Fetch.*``, no ``Input.*``, and ``Runtime.evaluate`` only in a routes run (below). One narrow widening exists for the
allowlisted designer-model JSON pages, which are shown rather than downloaded:
``Network.enable`` and ``Network.getResponseBody``, and a body is read only for the
request id of the one exact allowlisted ``GET /spr/strategy/model/<id>?dm=true``.

Stops (exit 3) rather than continuing: the tab leaves https://www.portfolio123.com or
lands on a login page, no download starts, a download is not exactly the expected file,
or the per-book identity check (``tools/p123_book_identity_check.py``) fails.

A second mode downloads one Google Drive file by direct-download URL under an exact
host allowlist, into a new directory, with a sha256 manifest.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from quant_engine.core import (
    InterpreterRefusal,
    check_entry_interpreter,
    require_current_interpreter,
)
from quant_engine.web_contracts import PORTFOLIO123_CONTRACT, WebContractError
from tools import p123_cdp_recorder as recorder


SCHEMA = "p123-cdp-fetcher/1"
ALLOWLIST_SCHEMA_VERSION = 1
ORIGIN = recorder.ORIGIN
P123_HOST = "www.portfolio123.com"
#: Reuse the recorder's reviewed constant: a URL containing one of these is refused.
MUTATION_WORDS: tuple[str, ...] = recorder.MUTATION_WORDS
_MUTATION_RE = re.compile("|".join(MUTATION_WORDS))

#: The messages this process may ever send to the browser.
ALLOWED_CDP_METHODS = frozenset(
    {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Page.navigate",
        "Browser.setDownloadBehavior",
        "Network.enable",
        "Network.getResponseBody",
    }
)
#: ``Runtime.evaluate`` is allowed only in a routes run, and only for an expression that
#: is byte-for-byte one of the pinned templates below (the probe, or one in-page GET of an
#: exact allowlisted URL). No other ``Runtime.*`` method exists in the allow-list.
INPAGE_CDP_METHODS = frozenset({"Runtime.evaluate"})
#: Allowed only in a run that fetches designer models, and ``getResponseBody`` only for
#: the request id of an exact allowlisted designer-model GET.
NETWORK_CDP_METHODS = frozenset({"Network.enable", "Network.getResponseBody"})
JSON_MEDIA = frozenset({"application/json", "text/json"})
#: Exit codes: 0 done, 2 refused before sending, 3 stopped mid-run, 4 review pending.
EXIT_REFUSED = 2
EXIT_STOP = 3
#: The capture is complete but at least one book is REVIEW: not a failure.
EXIT_REVIEW_PENDING = 4

DRIVE_URL = "https://drive.google.com/uc?export=download&id={id}"
#: Exact hosts a Drive download may start from or be redirected to. The per-file
#: content host is the one anchored pattern: ``doc-<label>-docs.googleusercontent.com``.
DRIVE_HOSTS = frozenset({"drive.google.com", "drive.usercontent.google.com"})
_DRIVE_CONTENT_HOST = re.compile(r"doc-[a-z0-9-]{1,63}-docs\.googleusercontent\.com")
_DRIVE_BLANK_TABS = frozenset({"about:blank", "chrome://newtab/", "edge://newtab/"})
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,100}")
_LOGIN_WORD = re.compile(r"login|signin|sign-in|logon|sso|auth|password", re.IGNORECASE)
_BOOK_ID = re.compile(r"[1-9][0-9]{0,11}")


@dataclass(frozen=True)
class Kind:
    """One reviewed read-only export: route, expected download name, saved name."""

    name: str
    path: str
    capability: str
    prefix: str
    saved: str
    stage_capability: str | None


#: Order is the fetch order within a book (the identity check needs transactions and
#: perf; holdings is optional and strengthens it).
KINDS: tuple[Kind, ...] = (
    Kind(
        "transactions_csv",
        "/p123/DownloadPortTrans",
        "strategy.transactions",
        "P123_SimTran_",
        "strategy_{id}_transactions.csv",
        "transactions.download",
    ),
    Kind(
        "perf_csv",
        "/p123/DownloadPortPerf",
        "strategy.performance",
        "P123_PortPerf_",
        "strategy_{id}_perf.csv",
        None,
    ),
    Kind(
        "holdings_csv",
        "/p123/DownloadPortHoldings",
        "strategy.holdings",
        "P123_PortHoldings_",
        "strategy_{id}_holdings.csv",
        None,
    ),
)
KIND_BY_NAME = {item.name: item for item in KINDS}
_DESIGNER_PATH = re.compile(r"/spr/strategy/model/[1-9][0-9]*")


class FetchRefused(ValueError):
    """Refused before anything was sent to the browser (exit 2)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class FetchStop(RuntimeError):
    """The run stops now (exit 3); files already saved are kept."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Allowlist (data, hash-bound)


@dataclass(frozen=True)
class Book:
    book_id: int
    kinds: tuple[str, ...]


@dataclass(frozen=True)
class Allowlist:
    books: tuple[Book, ...]
    designer_models: tuple[int, ...]
    drive_files: tuple[str, ...]
    page_record: bool
    sha256: str
    routes: tuple[tuple[str, int], ...] = ()


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _positive(value: object, what: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or _BOOK_ID.fullmatch(str(value)) is None
    ):
        raise FetchRefused("ALLOWLIST_INVALID", what)
    return value


def _parse_routes(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, list):
        raise FetchRefused("ALLOWLIST_INVALID", "routes")
    pairs: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "id"}:
            raise FetchRefused("ALLOWLIST_INVALID", "route entry")
        kind = item["kind"]
        if not isinstance(kind, str) or kind not in ROUTE_BY_NAME:
            raise FetchRefused("ALLOWLIST_INVALID", "route kind")
        pairs.append((kind, _positive(item["id"], "route id")))
    if len(set(pairs)) != len(pairs):
        raise FetchRefused("ALLOWLIST_INVALID", "duplicate route")
    for kind, ident in pairs:
        if kind == "designer_perf_full" and ("designer_model", ident) not in pairs:
            raise FetchRefused("ALLOWLIST_INVALID", "curve needs its model route")
    return tuple(pairs)


def parse_allowlist(payload: bytes, expected_sha256: str) -> Allowlist:
    digest = hashlib.sha256(payload).hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None:
        raise FetchRefused("ALLOWLIST_PIN_INVALID")
    if digest != expected_sha256:
        raise FetchRefused("ALLOWLIST_PIN_MISMATCH")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FetchRefused("ALLOWLIST_INVALID", "not JSON") from exc
    keys = {
        "schema_version",
        "origin",
        "books",
        "designer_models",
        "drive_files",
        "page_record",
    }
    if not isinstance(data, dict) or set(data) - {"routes", "page_gets"} != keys:
        raise FetchRefused("ALLOWLIST_INVALID", "keys")
    if "routes" in data and "page_gets" in data:
        raise FetchRefused("ALLOWLIST_INVALID", "routes and page_gets")
    if data["schema_version"] != ALLOWLIST_SCHEMA_VERSION:
        raise FetchRefused("ALLOWLIST_SCHEMA_UNSUPPORTED")
    if data["origin"] != ORIGIN:
        raise FetchRefused("ALLOWLIST_INVALID", "origin")
    books: list[Book] = []
    if not isinstance(data["books"], list):
        raise FetchRefused("ALLOWLIST_INVALID", "books")
    for item in data["books"]:
        if not isinstance(item, dict) or set(item) != {"id", "kinds"}:
            raise FetchRefused("ALLOWLIST_INVALID", "book entry")
        kinds = item["kinds"]
        if (
            not isinstance(kinds, list)
            or not kinds
            or len(set(map(str, kinds))) != len(kinds)
            or any(k not in KIND_BY_NAME for k in kinds)
        ):
            raise FetchRefused("ALLOWLIST_INVALID", "book kinds")
        books.append(
            Book(
                _positive(item["id"], "book id"),
                tuple(k.name for k in KINDS if k.name in kinds),
            )
        )
    designers = data["designer_models"]
    if not isinstance(designers, list):
        raise FetchRefused("ALLOWLIST_INVALID", "designer_models")
    drive = data["drive_files"]
    if not isinstance(drive, list) or any(
        not isinstance(item, str) or _DRIVE_ID.fullmatch(item) is None for item in drive
    ):
        raise FetchRefused("ALLOWLIST_INVALID", "drive_files")
    ids = [b.book_id for b in books]
    models = [_positive(item, "designer model id") for item in designers]
    if len(set(ids)) != len(ids) or len(set(models)) != len(models):
        raise FetchRefused("ALLOWLIST_INVALID", "duplicate id")
    if len(set(drive)) != len(drive):
        raise FetchRefused("ALLOWLIST_INVALID", "duplicate drive id")
    if not isinstance(data["page_record"], bool):
        raise FetchRefused("ALLOWLIST_INVALID", "page_record")
    return Allowlist(
        tuple(books),
        tuple(models),
        tuple(drive),
        data["page_record"],
        digest,
        # ``page_gets`` is the same list under its earlier name (PR 865's allowlists).
        _parse_routes(data.get("routes", data.get("page_gets", []))),
    )


def load_allowlist(path: Path, expected_sha256: str) -> Allowlist:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise FetchRefused("ALLOWLIST_UNREADABLE") from exc
    return parse_allowlist(payload, expected_sha256)


def build_allowlist_bytes(
    book_ids: Sequence[int],
    designer_ids: Sequence[int] = (),
    drive_ids: Sequence[str] = (),
    kinds: Sequence[str] | None = None,
    page_record: bool = False,
    routes: Sequence[tuple[str, int]] = (),
) -> bytes:
    chosen = list(kinds) if kinds else [k.name for k in KINDS]
    extra: dict[str, object] = (
        {"routes": [{"kind": k, "id": i} for k, i in routes]} if routes else {}
    )
    return canonical_bytes(
        {
            **extra,
            "schema_version": ALLOWLIST_SCHEMA_VERSION,
            "origin": ORIGIN,
            "books": [{"id": b, "kinds": chosen} for b in book_ids],
            "designer_models": list(designer_ids),
            "drive_files": list(drive_ids),
            "page_record": page_record,
        }
    )


def book_url(kind: Kind, book_id: int) -> str:
    return f"{ORIGIN}{kind.path}?portid={book_id}"


def designer_url(model_id: int) -> str:
    return f"{ORIGIN}/spr/strategy/model/{model_id}?dm=true"


def page_record_url(stamp_ms: int) -> str:
    """The account's investment list: the page record the identity check reads (I7)."""

    return f"{ORIGIN}/app/investment/resource?f=1&s={stamp_ms}"


def drive_url(file_id: str) -> str:
    return DRIVE_URL.format(id=file_id)


def allowed_p123_urls(allowlist: Allowlist) -> dict[str, tuple[str, int]]:
    """Exact URL -> (kind name, id) for every fetchable entry."""

    return {
        book_url(KIND_BY_NAME[kind], book.book_id): (kind, book.book_id)
        for book in allowlist.books
        for kind in book.kinds
    }


# ---------------------------------------------------------------------------
# URL screening (pure)


def _origin_ok(url: str, hosts: frozenset[str]) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in hosts
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


def screen_p123_url(url: str, allowed: Mapping[str, object]) -> None:
    """Raise FetchRefused unless ``url`` is exactly one allowlisted P123 export."""

    if not url.startswith("https://"):
        raise FetchRefused("URL_NOT_HTTPS")
    if not _origin_ok(url, frozenset({P123_HOST})):
        raise FetchRefused("URL_OFF_ORIGIN")
    parts = urlsplit(url)
    text = (unquote_plus(parts.path) + "?" + unquote_plus(parts.query)).lower()
    if _MUTATION_RE.search(text):
        raise FetchRefused("URL_MUTATION_WORD")
    if url not in allowed:
        raise FetchRefused("URL_NOT_ALLOWLISTED")
    try:
        endpoint = PORTFOLIO123_CONTRACT.route("GET", url)
    except WebContractError as exc:
        raise FetchRefused("URL_NOT_IN_REVIEWED_CONTRACT") from exc
    expected = KIND_BY_NAME[str(allowed[url][0])]  # type: ignore[index]
    if endpoint.capability != expected.capability:
        raise FetchRefused("URL_NOT_IN_REVIEWED_CONTRACT")


def screen_designer_url(url: str, allowed: Mapping[str, object]) -> None:
    """Raise FetchRefused unless ``url`` is exactly one allowlisted designer-model GET."""

    if not url.startswith("https://"):
        raise FetchRefused("URL_NOT_HTTPS")
    if not _origin_ok(url, frozenset({P123_HOST})):
        raise FetchRefused("URL_OFF_ORIGIN")
    parts = urlsplit(url)
    text = (unquote_plus(parts.path) + "?" + unquote_plus(parts.query)).lower()
    if _MUTATION_RE.search(text):
        raise FetchRefused("URL_MUTATION_WORD")
    if (
        url not in allowed
        or _DESIGNER_PATH.fullmatch(parts.path) is None
        or parts.query != "dm=true"
        or parts.fragment
    ):
        raise FetchRefused("URL_NOT_ALLOWLISTED")


def screen_page_record_url(url: str, expected: str) -> None:
    """Raise FetchRefused unless ``url`` is exactly the one page-record GET of this run.

    The only variable part of the route is ``s``, a 13-digit millisecond cache-buster
    (the preserved HARs show 78 calls with ``f=1`` and a different ``s`` each time). The
    run generates it once; ``expected`` is that exact URL.
    """

    if not url.startswith("https://"):
        raise FetchRefused("URL_NOT_HTTPS")
    if not _origin_ok(url, frozenset({P123_HOST})):
        raise FetchRefused("URL_OFF_ORIGIN")
    parts = urlsplit(url)
    text = (unquote_plus(parts.path) + "?" + unquote_plus(parts.query)).lower()
    if _MUTATION_RE.search(text):
        raise FetchRefused("URL_MUTATION_WORD")
    values = dict(parse_qsl(parts.query, keep_blank_values=True))
    if (
        url != expected
        or parts.path != "/app/investment/resource"
        or values.get("f") != "1"
        or re.fullmatch(r"[0-9]{13}", values.get("s", "")) is None
        or len(values) != 2
        or parts.fragment
    ):
        raise FetchRefused("URL_NOT_ALLOWLISTED")
    try:
        endpoint = PORTFOLIO123_CONTRACT.route("GET", url)
    except WebContractError as exc:
        raise FetchRefused("URL_NOT_IN_REVIEWED_CONTRACT") from exc
    if endpoint.capability != "investment.resource-list":
        raise FetchRefused("URL_NOT_IN_REVIEWED_CONTRACT")


def screen_drive_url(url: str, allowlist: Allowlist) -> str:
    """Return the file id when ``url`` is exactly an allowlisted Drive download."""

    if not url.startswith("https://"):
        raise FetchRefused("URL_NOT_HTTPS")
    if not _origin_ok(url, frozenset({"drive.google.com"})):
        raise FetchRefused("URL_OFF_ORIGIN")
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    keys = sorted(k for k, _ in pairs)
    if parts.path != "/uc" or keys != ["export", "id"] or parts.fragment:
        raise FetchRefused("URL_NOT_ALLOWLISTED")
    values = dict(pairs)
    file_id = values["id"]
    # The id is random text; scan everything else for mutation words.
    scrubbed = (parts.path + "?" + "&".join(k for k in keys) + values["export"]).lower()
    if _MUTATION_RE.search(scrubbed):
        raise FetchRefused("URL_MUTATION_WORD")
    if values["export"] != "download" or file_id not in allowlist.drive_files:
        raise FetchRefused("URL_NOT_ALLOWLISTED")
    return file_id


def drive_host_ok(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
        and (host in DRIVE_HOSTS or _DRIVE_CONTENT_HOST.fullmatch(host) is not None)
    )


def p123_tab_ok(url: str) -> bool:
    return recorder.origin_allowed(url) and not _LOGIN_WORD.search(urlsplit(url).path)


def drive_tab_ok(url: str) -> bool:
    return url in _DRIVE_BLANK_TABS or drive_host_ok(url)


# ---------------------------------------------------------------------------
# In-page routes: JSON and CSV that only answer a ``fetch()`` made FROM the signed-in page
#
# Direct navigation to these URLs returns HTTP 400. Each one is requested by an exact,
# pinned ``fetch`` expression run inside the tab, after the tab has been navigated to the
# host page that normally issues the request. Bytes are saved unmodified.

ACCOUNT_TABS: tuple[str, ...] = (
    "about",
    "investments",
    "perf",
    "positions",
    "trans",
    "broker",
    "reports",
    "notes",
    "liquidated",
)


@dataclass(frozen=True)
class Route:
    """One reviewed in-page read: host page, request, saved name, expected shape."""

    name: str
    host_page: str
    target: str
    saved: str
    shape: str  # json_object | json_list | json_any | csv | html | visit
    check: str = ""


ROUTES: tuple[Route, ...] = (
    Route(
        "designer_model",
        "/app/r2g/summary?id={id}",
        "/spr/strategy/model/{id}?dm=true",
        "r2g_{id}_dm_true.json",
        "json_object",
        "portUid",
    ),
    Route(
        "designer_perf_full",
        "/app/r2g/summary?id={id}",
        "/spr/strategy/model/{id}/perf?dm=true&init=true&years=25",
        "r2g_{id}_perf-full.json",
        "json_object",
        "perfData",
    ),
    Route(
        "designer_holdings",
        "/app/r2g/summary?id={id}",
        "/spr/strategy/model/{id}/holdings?dm=true",
        "r2g_{id}_holdings.json",
        "json_object",
        "holdings",
    ),
    Route(
        "investment_perfdata",
        "/app/investment/details?id={id}",
        "/app/investment/perf-data/{id}?init=1",
        "strategy_{id}_app_investment_perfdata_init.json",
        "json_object",
        "data",
    ),
    Route(
        "investment_perfdata_blank",
        "/app/investment/details?id={id}",
        "/app/investment/perf-data/{id}?init=",
        "strategy_{id}_app_investment_perfdata_init_blank.json",
        "json_any",
    ),
    Route(
        "investment_perfdata_true",
        "/app/investment/details?id={id}",
        "/app/investment/perf-data/{id}?init=true",
        "strategy_{id}_app_investment_perfdata_init_true.json",
        "json_object",
        "data",
    ),
    Route(
        "investment_trans_resource",
        "/app/investment/details?id={id}",
        "/app/investment/transResource/{id}",
        "strategy_{id}_app_investment_transresource.json",
        "json_any",
    ),
    Route(
        "investment_holdings",
        "/app/investment/details?id={id}",
        "/app/investment/holdings/{id}",
        "strategy_{id}_app_investment_holdings.json",
        "json_any",
    ),
    Route(
        "investment_performance",
        "/app/investment/details?id={id}",
        "/spr/strategy/portmodel/{id}/perf",
        "strategy_{id}_strategy_portmodel_perf.json",
        "json_any",
    ),
    Route(
        "investment_resource",
        "/app/investment/details?id={id}",
        "/app/investment/resource/{id}",
        "strategy_{id}_app_investment_resource.json",
        "json_object",
        "assetUid",
    ),
    Route(
        "investment_performance_page",
        "/app/investment/details?id={id}",
        "/app/investment/performance",
        "strategy_{id}_app_investment_performance.html",
        "html",
        "InvestmentPerformanceCtrl",
    ),
    Route(
        "book_port_summary",
        "/port_summary.jsp?portid={id}",
        "/port_summary.jsp?portid={id}",
        "book_{id}_port_summary.html",
        "html",
        "<title>Summary - ",
    ),
    Route(
        "investment_holdings_csv",
        "/app/investment/details?id={id}",
        "/app/investment/holdings.csv?id={id}&v=basic",
        "strategy_{id}_app_investment_holdings_basic.csv",
        "csv",
        "Ticker,Name,Prev Close,1D (%),1D,Value,Weight,Shares,Return%,Avg Price,Return,#Days",
    ),
    Route(
        "investment_trans_csv",
        "/app/investment/details?id={id}",
        "/app/investment/trans.csv?id={id}&type=&tickers=&fromDt=&toDt=",
        "strategy_{id}_app_investment_trans.csv",
        "csv",
        "Date,Type,Ticker,Name,Shares,Price,Commiss,Amount,Note",
    ),
    Route(
        "account_resource",
        "/app/account/details?id={id}",
        "/app/account/resource/{id}",
        "account_{id}_app_account_resource.json",
        "json_object",
    ),
    Route(
        "account_positions",
        "/app/account/details?id={id}",
        "/app/account/positions/{id}",
        "account_{id}_app_account_positions.json",
        "json_list",
    ),
    # Visit only: the details page and each tab, nothing saved (the SPA shell carries no
    # data; the data routes above are what the tabs request).
    Route("account_pages", "/app/account/details?id={id}", "", "", "visit"),
)
ROUTE_BY_NAME = {item.name: item for item in ROUTES}

PROBE_EXPRESSION = (
    "JSON.stringify([location.origin,location.pathname+location.search,"
    "document.readyState])"
)


def fetch_expression(url: str) -> str:
    """The one pinned in-page GET: same origin, cookies, no redirect, bytes as base64."""

    # The designer JSON endpoints answer HTTP 400 unless the request carries the page's own
    # angular_req marker and a JSON accept header (a bare fetch is refused by the server).
    headers = (
        ",headers:{accept:'application/json, text/plain, */*',angular_req:'1'}"
        if "/spr/strategy/model/" in url
        else ""
    )
    return (
        "(async()=>{const r=await fetch("
        + json.dumps(url)
        + ",{method:'GET',credentials:'include',redirect:'error'"
        + headers
        + "});"
        "const b=new Uint8Array(await r.arrayBuffer());let s='';"
        "for(let i=0;i<b.length;i+=8192){s+=String.fromCharCode.apply("
        "null,b.subarray(i,i+8192))}"
        "return JSON.stringify({s:r.status,t:r.headers.get('content-type')||'',"
        "u:r.url,b:btoa(s)})})()"
    )


def route_host_url(route: Route, ident: int) -> str:
    return f"{ORIGIN}{route.host_page.format(id=ident)}"


def route_target_url(route: Route, ident: int) -> str:
    return f"{ORIGIN}{route.target.format(id=ident)}"


def route_visit_urls(route: Route, ident: int) -> tuple[str, ...]:
    host = route_host_url(route, ident)
    return (host, *(f"{host}&t={tab}" for tab in ACCOUNT_TABS))


def route_urls(allowlist: Allowlist) -> tuple[frozenset[str], frozenset[str]]:
    """(navigable pages, fetchable URLs) for every allowlisted route entry."""

    pages: set[str] = set()
    fetches: set[str] = set()
    for name, ident in allowlist.routes:
        route = ROUTE_BY_NAME[name]
        pages.add(route_host_url(route, ident))
        if route.shape == "visit":
            pages.update(route_visit_urls(route, ident))
        else:
            fetches.add(route_target_url(route, ident))
    return frozenset(pages), frozenset(fetches)


def screen_route_url(url: str, allowed: frozenset[str]) -> None:
    """Raise FetchRefused unless ``url`` is exactly one allowlisted route URL."""

    if not url.startswith("https://"):
        raise FetchRefused("URL_NOT_HTTPS")
    if not _origin_ok(url, frozenset({P123_HOST})):
        raise FetchRefused("URL_OFF_ORIGIN")
    parts = urlsplit(url)
    text = (unquote_plus(parts.path) + "?" + unquote_plus(parts.query)).lower()
    if _MUTATION_RE.search(text):
        raise FetchRefused("URL_MUTATION_WORD")
    if url not in allowed or parts.fragment:
        raise FetchRefused("URL_NOT_ALLOWLISTED")


def _provider_day(value: object) -> date | None:
    """The model payload states dates as MM/DD/YYYY."""

    parts = str(value).split("/")
    try:
        month, day, year = (int(item) for item in parts)
        return date(year, month, day) if len(parts) == 3 else None
    except ValueError:
        return None


def _check_designer_curve(
    route: Route, ident: int, document: object, context: dict[int, date] | None
) -> None:
    """The perf curve must be the FULL curve: it starts on the model's inceptionDt.

    The model is read first and its inceptionDt is kept in ``context``; a curve that starts
    later (a truncated ``years`` window) is refused, never saved.
    """

    if route.name not in {"designer_model", "designer_perf_full"}:
        return
    if not isinstance(document, dict) or context is None:
        raise FetchStop("STOP_SHAPE_UNEXPECTED", "no model context")
    if route.name == "designer_model":
        inception = _provider_day(document.get("inceptionDt"))
        if inception is None:
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "model inceptionDt unreadable")
        context[ident] = inception
        return
    block = document.get("perfData")
    start = block.get("date") if isinstance(block, dict) else None
    try:
        first = date.fromisoformat(str(start))
    except ValueError as exc:
        raise FetchStop("STOP_SHAPE_UNEXPECTED", "curve start unreadable") from exc
    expected = context.get(ident)
    if expected is None:
        raise FetchStop("STOP_SHAPE_UNEXPECTED", "model not read before its curve")
    if first != expected:
        raise FetchStop(
            "STOP_SHAPE_UNEXPECTED", "curve start differs from model inceptionDt"
        )


def validate_route_body(
    route: Route,
    ident: int,
    content_type: str,
    data: bytes,
    context: dict[int, date] | None = None,
) -> int | None:
    """Fail closed unless ``data`` has exactly the reviewed shape; return CSV rows.

    Errors name the check, never a value from the body.
    """

    html = "text/html" in content_type
    if not data:
        raise FetchStop("STOP_SHAPE_UNEXPECTED", "empty body")
    if html and route.shape != "html":
        raise FetchStop("STOP_REAUTH", "an HTML page was served")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FetchStop("STOP_SHAPE_UNEXPECTED", "body is not UTF-8") from exc
    if route.shape in {"json_object", "json_list", "json_any"}:
        if "json" not in content_type:
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "response is not JSON")
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "body is not JSON") from exc
        want: tuple[type, ...] = {
            "json_object": (dict,),
            "json_list": (list,),
            "json_any": (dict, list),
        }[route.shape]
        if not isinstance(document, want):
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "body has an unexpected JSON type")
        if route.check and (
            not isinstance(document, dict) or route.check not in document
        ):
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "expected field missing")
        if (
            route.name == "designer_model"
            and isinstance(document, dict)
            and document.get("portUid") != ident
        ):
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "model id differs")
        _check_designer_curve(route, ident, document, context)
        return None
    if route.shape == "csv":
        if html or text.splitlines()[0] != route.check:
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "unexpected CSV header")
        return count_rows(data)
    if route.shape == "html":
        if not html or route.check not in text:
            raise FetchStop("STOP_SHAPE_UNEXPECTED", "unexpected page fragment")
        return None
    raise FetchStop("STOP_SHAPE_UNEXPECTED", "route has no body")


# ---------------------------------------------------------------------------
# Guarded transport


class CdpTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class GuardedTransport:
    """Send only the allowed methods, with their arguments pinned.

    ``readable`` holds the request ids whose body may be read: the session adds an id
    only after seeing the exact allowlisted designer-model GET under it.
    """

    def __init__(
        self,
        inner: CdpTransport,
        *,
        allowed_urls: frozenset[str],
        download_path: str,
        network_read: bool = False,
        inpage_expressions: frozenset[str] = frozenset(),
    ) -> None:
        self._inpage = inpage_expressions
        self._inner = inner
        self._urls = allowed_urls
        self._download_path = download_path
        self._network_read = network_read
        self.readable: set[str] = set()

    def send(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method not in ALLOWED_CDP_METHODS and not (
            self._inpage and method in INPAGE_CDP_METHODS
        ):
            raise FetchRefused("CDP_METHOD_REFUSED", str(method))
        if method in INPAGE_CDP_METHODS:
            if (
                set(params) != {"expression", "awaitPromise", "returnByValue"}
                or params["expression"] not in self._inpage
                or params["awaitPromise"] is not True
                or params["returnByValue"] is not True
            ):
                raise FetchRefused("CDP_EVALUATE_NOT_ALLOWLISTED")
        elif method in NETWORK_CDP_METHODS:
            if not self._network_read:
                raise FetchRefused("CDP_METHOD_REFUSED", str(method))
            if method == "Network.enable" and params:
                raise FetchRefused("CDP_NETWORK_ARGUMENTS_REFUSED")
            if method == "Network.getResponseBody" and (
                set(params) != {"requestId"}
                or not isinstance(params["requestId"], str)
                or params["requestId"] not in self.readable
            ):
                raise FetchRefused("CDP_BODY_READ_NOT_ALLOWLISTED")
        elif method == "Page.navigate":
            if set(params) != {"url"} or params["url"] not in self._urls:
                raise FetchRefused("CDP_NAVIGATE_NOT_ALLOWLISTED")
        elif method == "Browser.setDownloadBehavior":
            behavior = params.get("behavior")
            if behavior == "allowAndName":
                if (
                    set(params) != {"behavior", "downloadPath", "eventsEnabled"}
                    or params["downloadPath"] != self._download_path
                    or params["eventsEnabled"] is not True
                ):
                    raise FetchRefused("CDP_DOWNLOAD_PATH_REFUSED")
            elif behavior != "default" or set(params) != {"behavior"}:
                raise FetchRefused("CDP_DOWNLOAD_PATH_REFUSED")
        elif method == "Target.attachToTarget":
            if set(params) != {"targetId", "flatten"} or params["flatten"] is not True:
                raise FetchRefused("CDP_ATTACH_REFUSED")
        elif method == "Target.setDiscoverTargets" and params != {"discover": True}:
            raise FetchRefused("CDP_DISCOVER_REFUSED")
        self._inner.send(message)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        return self._inner.receive(timeout)

    def close(self) -> None:
        self._inner.close()


def connect(cdp_url: str) -> CdpTransport:  # pragma: no cover - needs a browser
    try:
        recorder.parse_cdp_url(cdp_url)
        return recorder.WebSocketTransport(cdp_url)
    except recorder.RecorderError as exc:
        raise FetchRefused(exc.code) from exc


# ---------------------------------------------------------------------------
# Session: attach, navigate, observe exactly one download


@dataclass
class Landed:
    kind: str
    ident: str
    relative_path: str
    sha256: str
    size_bytes: int
    rows: int | None


def count_rows(data: bytes) -> int | None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    rows = [row for row in csv.reader(io.StringIO(text)) if any(row)]
    return max(len(rows) - 1, 0)


class Session:
    def __init__(
        self,
        transport: CdpTransport,
        *,
        allowed_urls: frozenset[str],
        incoming: Path,
        tab_ok: Callable[[str], bool],
        tab_known: Callable[[str], bool],
        no_download_code: str,
        clock: Callable[[], float] = time.monotonic,
        call_timeout: float = 20.0,
        download_timeout: float = 180.0,
        attach_wait: float = 5.0,
        network_read: bool = False,
        inpage_expressions: frozenset[str] = frozenset(),
    ) -> None:
        self._transport = GuardedTransport(
            transport,
            allowed_urls=allowed_urls,
            download_path=str(incoming),
            network_read=network_read,
            inpage_expressions=inpage_expressions,
        )
        self._incoming = incoming
        self._tab_ok = tab_ok
        self._tab_known = tab_known
        self._no_download_code = no_download_code
        self._clock = clock
        self._call_timeout = call_timeout
        self._download_timeout = download_timeout
        self._attach_wait = attach_wait
        self._next_id = 0
        self._pending: deque[dict[str, Any]] = deque()
        self._targets: dict[str, str] = {}
        self._target_id = ""
        self._sid = ""

    # -- plumbing ---------------------------------------------------------
    def _note(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        if method in {"Target.targetCreated", "Target.targetInfoChanged"}:
            info = params.get("targetInfo")
            if isinstance(info, dict) and info.get("type") == "page":
                self._targets[str(info.get("targetId"))] = str(info.get("url", ""))
        elif method == "Target.targetDestroyed":
            self._targets.pop(str(params.get("targetId")), None)

    def _call(
        self, method: str, params: dict[str, Any], sid: str | None = None
    ) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        if sid:
            message["sessionId"] = sid
        self._transport.send(message)
        deadline = self._clock() + self._call_timeout
        while True:
            reply = self._transport.receive(0.5)
            if reply is None:
                if self._clock() > deadline:
                    raise FetchStop("CDP_CALL_TIMEOUT", method)
                continue
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise FetchStop("CDP_CALL_FAILED", method)
                result = reply.get("result", {})
                return result if isinstance(result, dict) else {}
            if "id" not in reply:
                self._note(reply)
                self._pending.append(reply)

    def _event(self, timeout: float) -> dict[str, Any] | None:
        if self._pending:
            return self._pending.popleft()
        message = self._transport.receive(timeout)
        if message is not None:
            self._note(message)
        return message

    # -- attach -----------------------------------------------------------
    def attach(self, target_id: str | None = None) -> None:
        self._call("Target.setDiscoverTargets", {"discover": True})
        deadline = self._clock() + self._attach_wait
        while self._clock() < deadline:
            message = self._event(0.5)
            if message is None and self._targets:
                break
        candidates = {
            tid: url
            for tid, url in self._targets.items()
            if (target_id is None or tid == target_id) and self._tab_known(url)
        }
        if not candidates:
            raise FetchStop("STOP_NO_SIGNED_IN_TAB", "no matching tab")
        good = {tid: url for tid, url in candidates.items() if self._tab_ok(url)}
        if not good:
            raise FetchStop("STOP_REAUTH", "the only candidate tab is on a login page")
        if len(good) > 1:
            raise FetchStop("STOP_TARGET_AMBIGUOUS", f"{len(good)} candidate tabs")
        (self._target_id,) = good
        result = self._call(
            "Target.attachToTarget", {"targetId": self._target_id, "flatten": True}
        )
        self._sid = str(result.get("sessionId", ""))
        if not self._sid:
            raise FetchStop("CDP_ATTACH_FAILED")
        self._call(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(self._incoming),
                "eventsEnabled": True,
            },
        )

    def release(self) -> None:
        try:
            self._call("Browser.setDownloadBehavior", {"behavior": "default"})
        except (FetchStop, FetchRefused, OSError):
            pass

    def _check_tab(self) -> None:
        url = self._targets.get(self._target_id, "")
        if url and not self._tab_ok(url):
            raise FetchStop("STOP_REAUTH", "the tab left the allowed origin or login")

    # -- one download -----------------------------------------------------
    def download(
        self,
        url: str,
        *,
        start_ok: Callable[[str, str], bool],
        saved: Path,
    ) -> tuple[str, int, int | None, str]:
        """Navigate to ``url`` and move the one downloaded file to ``saved``.

        Returns (sha256, size, rows, suggested-filename extension).
        """

        if any(self._incoming.iterdir()):
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "download directory not empty")
        navigated = self._call("Page.navigate", {"url": url}, self._sid)
        frame = navigated.get("frameId")
        if not isinstance(frame, str) or not frame:
            raise FetchStop(
                "STOP_DOWNLOAD_NOT_EXACT", "navigation returned no frame id"
            )
        deadline = self._clock() + self._download_timeout
        guid = ""
        suggested = ""
        total = received = -1
        done = False
        while not done:
            if self._clock() > deadline:
                if guid:
                    raise FetchStop(
                        "STOP_DOWNLOAD_NOT_EXACT", "download did not finish"
                    )
                raise FetchStop(
                    self._no_download_code,
                    "NO_DOWNLOAD_STARTED (signed out, session expired, or interstitial)",
                )
            message = self._event(0.5)
            self._check_tab()
            if message is None:
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            method = message.get("method")
            if method == "Browser.downloadWillBegin":
                if params.get("frameId") != frame:
                    # Another tab or frame of this browser started a download: it is
                    # not ours. Its file lands in the shared directory, which the
                    # exact-directory check below turns into a stop.
                    continue
                if guid:
                    raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "second download")
                guid = str(params.get("guid", ""))
                suggested = str(params.get("suggestedFilename", ""))
                if not guid or not start_ok(str(params.get("url", "")), suggested):
                    raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "unexpected download")
            elif method == "Browser.downloadProgress" and params.get("guid") == guid:
                state = params.get("state")
                if state == "canceled":
                    raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "download canceled")
                if state == "completed":
                    total = int(params.get("totalBytes", -1))
                    received = int(params.get("receivedBytes", -1))
                    done = True
        names = sorted(item.name for item in self._incoming.iterdir())
        if names != [guid]:
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "unexpected files in directory")
        source = self._incoming / guid
        data = source.read_bytes()
        if not data or len(data) != received or (total > 0 and total != received):
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "size mismatch or empty file")
        if saved.exists():
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "destination exists")
        saved.parent.mkdir(parents=True, exist_ok=True)
        source.rename(saved)
        extension = Path(suggested).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", extension) is None:
            extension = ""
        return hashlib.sha256(data).hexdigest(), len(data), count_rows(data), extension

    # -- one allowlisted JSON page (designer model) ------------------------
    def enable_network(self) -> None:
        self._call("Network.enable", {}, self._sid)

    def read_json(self, url: str, saved: Path, shape: type = dict) -> tuple[str, int]:
        """Navigate to the exact ``url`` and save its response body unmodified.

        The body is read for the one request id seen with exactly this URL and ``GET``;
        the guard refuses a read for any other id.
        """

        request_id = ""
        status = -1
        mime = ""
        finished = False
        self._call("Page.navigate", {"url": url}, self._sid)
        deadline = self._clock() + self._download_timeout
        while not finished:
            if self._clock() > deadline:
                raise FetchStop(
                    "STOP_REAUTH", "NO_RESPONSE (signed out or session expired)"
                )
            message = self._event(0.5)
            self._check_tab()
            if message is None:
                continue
            method = message.get("method")
            params = message.get("params")
            if (
                not isinstance(params, dict)
                or message.get("sessionId") != self._sid
                or not isinstance(method, str)
                or not method.startswith("Network.")
            ):
                continue
            ident = params.get("requestId")
            if method == "Network.requestWillBeSent":
                request = params.get("request")
                actual = request.get("url") if isinstance(request, dict) else None
                if ident == request_id and actual != url:
                    raise FetchStop("STOP_REAUTH", "the request was redirected")
                if (
                    isinstance(request, dict)
                    and actual == url
                    and request.get("method") == "GET"
                ):
                    if request_id and ident != request_id:
                        raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "second request")
                    request_id = str(ident)
                    self._transport.readable.add(request_id)
            elif ident == request_id and request_id:
                if method == "Network.responseReceived":
                    response = params.get("response")
                    if not isinstance(response, dict) or response.get("url") != url:
                        raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "response url")
                    status = int(response.get("status", -1))
                    mime = str(response.get("mimeType", "")).lower()
                elif method == "Network.loadingFailed":
                    raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "request failed")
                elif method == "Network.loadingFinished":
                    finished = True
        if status in {401, 403}:
            raise FetchStop("STOP_REAUTH", f"HTTP {status}")
        if status != 200:
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", f"HTTP {status}")
        if mime == "text/html":
            raise FetchStop("STOP_REAUTH", "an HTML page was served")
        if mime not in JSON_MEDIA:
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "response is not JSON")
        result = self._call(
            "Network.getResponseBody", {"requestId": request_id}, self._sid
        )
        self._transport.readable.discard(request_id)
        body = str(result.get("body", ""))
        data = (
            base64.b64decode(body)
            if result.get("base64Encoded")
            else body.encode("utf-8")
        )
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "body is not JSON") from exc
        if not data or not isinstance(document, shape):
            raise FetchStop(
                "STOP_DOWNLOAD_NOT_EXACT", f"body is not a JSON {shape.__name__}"
            )
        if saved.exists():
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "destination exists")
        saved.parent.mkdir(parents=True, exist_ok=True)
        with saved.open("xb") as handle:
            handle.write(data)
        return hashlib.sha256(data).hexdigest(), len(data)

    # -- in-page reads (routes mode) ----------------------------------------
    def _evaluate(self, expression: str) -> str:
        result = self._call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            self._sid,
        )
        value = result.get("result")
        if "exceptionDetails" in result or not isinstance(value, dict):
            raise FetchStop("STOP_INPAGE_FAILED", "the in-page script failed")
        text = value.get("value")
        if not isinstance(text, str):
            raise FetchStop("STOP_INPAGE_FAILED", "the in-page script returned no text")
        return text

    def visit(self, url: str, *, settle: float = 2.0) -> None:
        """Navigate the tab to the exact page and wait until it has loaded."""

        want = urlsplit(url)
        self._call("Page.navigate", {"url": url}, self._sid)
        deadline = self._clock() + self._download_timeout
        while True:
            if self._clock() > deadline:
                raise FetchStop("STOP_REAUTH", "PAGE_NOT_LOADED (signed out or stuck)")
            self._event(0.5)
            self._check_tab()
            try:
                origin, where, state = json.loads(self._evaluate(PROBE_EXPRESSION))
            except FetchStop as exc:
                if exc.code in {"CDP_CALL_FAILED", "STOP_INPAGE_FAILED"}:
                    continue  # the page is being replaced
                raise
            at = urlsplit(str(where))
            if origin != ORIGIN or _LOGIN_WORD.search(at.path):
                raise FetchStop("STOP_REAUTH", "the tab left the origin or is on login")
            if (
                at.path == want.path
                and sorted(parse_qsl(at.query, keep_blank_values=True))
                == sorted(parse_qsl(want.query, keep_blank_values=True))
                and state == "complete"
            ):
                break
        self._event(settle)

    def fetch_in_page(
        self,
        url: str,
        saved: Path,
        route: Route,
        ident: int,
        context: dict[int, date] | None = None,
    ) -> tuple[str, int, int | None]:
        """GET ``url`` from inside the page and save the body unmodified."""

        raw = self._evaluate(fetch_expression(url))
        try:
            reply = json.loads(raw)
            status = int(reply["s"])
            content_type = str(reply["t"]).lower()
            final = str(reply["u"])
            data = base64.b64decode(str(reply["b"]), validate=True)
        except (ValueError, KeyError, TypeError) as exc:
            raise FetchStop("STOP_INPAGE_FAILED", "malformed in-page result") from exc
        if final != url:
            raise FetchStop("STOP_REAUTH", "the request was redirected")
        if status in {401, 403}:
            raise FetchStop("STOP_REAUTH", f"HTTP {status}")
        if status != 200:
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", f"HTTP {status}")
        rows = validate_route_body(route, ident, content_type, data, context)
        if saved.exists():
            raise FetchStop("STOP_DOWNLOAD_NOT_EXACT", "destination exists")
        saved.parent.mkdir(parents=True, exist_ok=True)
        with saved.open("xb") as handle:
            handle.write(data)
        return hashlib.sha256(data).hexdigest(), len(data), rows


# ---------------------------------------------------------------------------
# Identity check wiring (tools/p123_book_identity_check.py, PR 827)

IdentityCheck = Callable[[Path, Sequence[str], Path | None], Mapping[str, Any]]


IDENTITY_CHECK_MODULE = "tools.p123_book_identity_check"


def load_identity_check(
    module_name: str = IDENTITY_CHECK_MODULE,
    *,
    prior_dirs: Sequence[Path] = (),
    cross_dirs: Sequence[Path] = (),
    as_of: date | None = None,
) -> IdentityCheck:
    """Import the per-book identity check by module path; refuse when it is absent.

    ``tools/p123_book_identity_check.py`` arrived with PR 827. Until that is on the
    branch, or when its package dependency is missing, the fetcher refuses to start a
    Portfolio123 run (there is no skip flag). ``prior_dirs`` (older or HAR download
    sets, warn) and ``cross_dirs`` (other capture directories of the same pull, fail)
    feed the check's permutation comparison; a reference index makes a book absent from
    the reference pull a failure. ``as_of`` is the financial cutoff D: the check rolls the
    holdings back to it (I5D) and the decisions file records it, which the generation plan
    requires to equal its own as-of date. Run the fetcher from the repository root as
    ``python -m tools.p123_cdp_fetcher`` so the ``tools`` package is importable.
    """

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name not in {module_name, module_name.split(".")[0]}:
            raise FetchRefused(
                "IDENTITY_CHECK_UNAVAILABLE", f"missing dependency {exc.name}"
            ) from exc
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", "module not present") from exc
    except Exception as exc:  # noqa: BLE001 - any import failure means unavailable
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", type(exc).__name__) from exc
    check = getattr(module, "check_capture", None)
    if not callable(check):
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", "no check_capture")

    def run(
        capture: Path, books: Sequence[str], reference_index: Path | None
    ) -> Mapping[str, Any]:
        # check_capture is not interpreter-guarded (only the tool's main is).
        try:
            require_current_interpreter("the P123 CDP fetcher book identity check")
        except InterpreterRefusal as exc:
            raise FetchRefused("INTERPRETER_REFUSED", str(exc)) from exc
        result = check(
            capture,
            books=list(books),
            raw_subdir="raw",
            use_manifest=True,
            reference_index=reference_index,
            require_reference=reference_index is not None,
            cross_checks=[(Path(item), "raw") for item in cross_dirs],
            prior_checks=[(Path(item), "") for item in prior_dirs],
            as_of=as_of,
        )
        return result  # type: ignore[no-any-return]

    return run


# ---------------------------------------------------------------------------
# Run


def _write_new(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _replace(path: Path, payload: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(payload)
    os.replace(temp, path)


def _prepare_root(root: Path) -> tuple[Path, Path, Path]:
    try:
        recorder.assert_output_root(root)
    except recorder.RecorderError as exc:
        raise FetchRefused(exc.code) from exc
    root.mkdir(parents=True)
    incoming = root / "incoming"
    capture = root / "capture"
    incoming.mkdir()
    (capture / "raw").mkdir(parents=True)
    return incoming, capture, root


def _identity_policy(decision: str) -> str:
    """STOP for anything but PASS or REVIEW; REVIEW never stops a download.

    A REVIEW is a set of warnings (a missing optional holdings export, no page record,
    a book that is not on the account page, ...). The operator accepts each one at
    generation time (``portfolio123_generation.py plan --accept-identity-review
    ID=REASON``), so the run finishes and is reported as REVIEW_PENDING (exit 4). An
    unknown or missing decision is treated as a FAIL.
    """

    if decision == "PASS":
        return "CONTINUE"
    if decision == "REVIEW":
        return "CONTINUE_REVIEW"
    return "STOP"


def _review_checks(item: Mapping[str, Any]) -> list[str]:
    """Names of the checks that are not PASS in one book's decision."""

    return sorted(
        str(c.get("check")) for c in item.get("checks", []) if c.get("status") != "PASS"
    )


def _capture_manifest(files: Sequence[Landed]) -> bytes:
    return canonical_bytes(
        {
            "artifacts": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "observed_length": item.size_bytes,
                }
                for item in files
            ],
            "schema": "p123-cdp-fetcher-capture/1",
        }
    )


def run_p123(
    transport: CdpTransport,
    allowlist: Allowlist,
    root: Path,
    *,
    identity_check: IdentityCheck,
    reference_index: Path | None = None,
    no_reference: bool = False,
    skip_designer_models: bool = False,
    target_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    download_timeout: float = 180.0,
) -> dict[str, Any]:
    """Fetch every allowlisted export; return the manifest (also written to disk)."""

    try:
        require_current_interpreter("the P123 CDP fetcher")
    except InterpreterRefusal as exc:
        raise FetchRefused("INTERPRETER_REFUSED", str(exc)) from exc
    if reference_index is None and not no_reference:
        raise FetchRefused(
            "REFERENCE_REQUIRED", "pass a reference index or no_reference"
        )
    if reference_index is not None and no_reference:
        raise FetchRefused("REFERENCE_CONFLICT")
    reference_sha: str | None = None
    if reference_index is not None:
        try:
            reference_sha = hashlib.sha256(reference_index.read_bytes()).hexdigest()
        except OSError as exc:
            raise FetchRefused("REFERENCE_UNREADABLE") from exc
    fetch_designers = bool(allowlist.designer_models) and not skip_designer_models
    fetch_page = allowlist.page_record
    network_read = fetch_designers or fetch_page
    if not allowlist.books and not fetch_designers and not fetch_page:
        raise FetchRefused("ALLOWLIST_EMPTY")
    urls = allowed_p123_urls(allowlist)
    for url in urls:  # screen the whole allowlist before anything is sent
        screen_p123_url(url, urls)
    designer_urls = {designer_url(model): model for model in allowlist.designer_models}
    for url in designer_urls:
        screen_designer_url(url, designer_urls)
    page_url = page_record_url(int(wall_clock() * 1000)) if fetch_page else ""
    if page_url:
        screen_page_record_url(page_url, page_url)
    navigable = set(urls)
    if fetch_designers:
        navigable |= set(designer_urls)
    if page_url:
        navigable.add(page_url)
    incoming, capture, base = _prepare_root(root)
    session = Session(
        transport,
        allowed_urls=frozenset(navigable),
        network_read=network_read,
        incoming=incoming,
        tab_ok=p123_tab_ok,
        tab_known=recorder.origin_allowed,
        no_download_code="STOP_REAUTH",
        clock=clock,
        download_timeout=download_timeout,
    )
    landed: list[Landed] = []
    books: dict[str, dict[str, Any]] = {}
    designers: dict[str, dict[str, Any]] = {}
    page_info: dict[str, Any] | None = None
    stop: FetchStop | None = None
    report: Mapping[str, Any] = {}

    def check_books() -> Mapping[str, Any]:
        landed_books = [
            str(b.book_id) for b in allowlist.books if str(b.book_id) in books
        ]
        try:
            return identity_check(capture, landed_books, reference_index)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FetchStop(
                "STOP_IDENTITY_FAIL", f"identity check error {type(exc).__name__}"
            ) from exc

    try:
        session.attach(target_id)
        if network_read:
            session.enable_network()
        if fetch_page:
            relative = "raw/p123_investment_resource.json"
            digest, size = session.read_json(page_url, capture / relative, list)
            landed.append(Landed("page_record_json", "-", relative, digest, size, None))
            page_info = {
                "relative_path": relative,
                "sha256": digest,
                "size_bytes": size,
            }
            _replace(capture / "capture-manifest.json", _capture_manifest(landed))
        for book in allowlist.books:
            entry = books.setdefault(
                str(book.book_id), {"files": [], "identity": "NOT_RUN"}
            )
            for name in book.kinds:
                kind = KIND_BY_NAME[name]
                url = book_url(kind, book.book_id)
                screen_p123_url(url, urls)
                relative = f"raw/{kind.saved.format(id=book.book_id)}"

                def start_ok(
                    actual: str, suggested: str, url: str = url, kind: Kind = kind
                ) -> bool:
                    return (
                        actual == url
                        and suggested.startswith(kind.prefix)
                        and suggested.endswith(".csv")
                        and "/" not in suggested
                        and "\\" not in suggested
                    )

                digest, size, rows, _ = session.download(
                    url, start_ok=start_ok, saved=capture / relative
                )
                item = Landed(name, str(book.book_id), relative, digest, size, rows)
                landed.append(item)
                entry["files"].append(
                    {
                        "kind": name,
                        "relative_path": relative,
                        "sha256": digest,
                        "size_bytes": size,
                        "rows": rows,
                        "stage_file_capability": kind.stage_capability,
                    }
                )
                _replace(capture / "capture-manifest.json", _capture_manifest(landed))
            report = check_books()
            result = report.get("books", {}).get(str(book.book_id), {})
            decision = str(result.get("decision", "FAIL"))
            entry["identity"] = decision
            entry["identity_checks"] = {
                str(c.get("check")): str(c.get("status"))
                for c in result.get("checks", [])
            }
            entry["identity_policy"] = _identity_policy(decision)
            if decision == "REVIEW":
                entry["review_checks"] = _review_checks(result)
            if entry["identity_policy"] == "STOP":
                raise FetchStop(
                    "STOP_IDENTITY_FAIL", f"book identity decision {decision}"
                )
        if fetch_designers:
            for model in allowlist.designer_models:
                url = designer_url(model)
                screen_designer_url(url, designer_urls)
                relative = f"raw/designer_model_{model}.json"
                digest, size = session.read_json(url, capture / relative)
                landed.append(
                    Landed(
                        "designer_model_json", str(model), relative, digest, size, None
                    )
                )
                designers[str(model)] = {
                    "relative_path": relative,
                    "sha256": digest,
                    "size_bytes": size,
                    "stage_file_capability": "designer-model.json",
                }
                _replace(capture / "capture-manifest.json", _capture_manifest(landed))
    except FetchStop as exc:
        stop = exc
    except FetchRefused as exc:
        stop = FetchStop("STOP_REFUSED", exc.code)
    finally:
        session.release()
    # The capture manifest is final now: the decisions file must bind exactly these
    # bytes, so the last check runs after the last manifest write, stopped or not.
    final_error: str | None = None
    if books:
        try:
            report = check_books()
        except (FetchStop, FetchRefused) as exc:
            final_error = exc.detail if isinstance(exc, FetchStop) else exc.code
    if report:
        _write_new(
            base / "book-identity-decisions.json",
            (json.dumps(report, indent=1, sort_keys=True, default=str) + "\n").encode(),
        )
    final_books = dict(report.get("books", {}))
    review: dict[str, list[str]] = {
        bid: _review_checks(item)
        for bid, item in final_books.items()
        if item.get("decision") == "REVIEW"
    }
    for bid, entry in books.items():
        if bid not in final_books and entry.get("identity") == "REVIEW":
            review[bid] = list(entry.get("review_checks", []))
    failed = sorted(
        bid for bid, item in final_books.items() if item.get("decision") == "FAIL"
    )
    if stop is None and failed:
        stop = FetchStop(
            "STOP_IDENTITY_FAIL", "final identity check failed: " + ", ".join(failed)
        )
    status = "STOPPED" if stop else ("REVIEW_PENDING" if review else "COMPLETE")
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "p123",
        "status": status,
        "review_pending": review,
        "stop": {"code": stop.code, "detail": stop.detail} if stop else None,
        "allowlist_sha256": allowlist.sha256,
        "capture_directory": "capture",
        "reference": {
            "mode": "NONE_DECLARED" if no_reference else "INDEX",
            "index_sha256": reference_sha,
        },
        "page_record": page_info,
        "books": books,
        "books_not_attempted": [
            str(b.book_id) for b in allowlist.books if str(b.book_id) not in books
        ],
        "final_identity": {
            book: str(item.get("decision"))
            for book, item in dict(report.get("books", {})).items()
        },
        "final_identity_error": final_error,
        "designer_models": designers,
        "designer_models_not_fetched": [
            str(m) for m in allowlist.designer_models if str(m) not in designers
        ],
        "safety": {
            "cdp_methods": sorted(
                ALLOWED_CDP_METHODS - (set() if network_read else NETWORK_CDP_METHODS)
            ),
            "body_reads": (
                "allowlisted page-record and designer-model requests only"
                if network_read
                else "none"
            ),
            "input_events": False,
        },
    }
    manifest["manifest_sha256"] = _write_new(
        base / "p123-fetch-manifest.json", canonical_bytes(manifest)
    )
    if stop:
        manifest["_stop"] = stop
    return manifest


def _assert_dry_run_root(root: Path) -> None:
    """A dry run lives under a scratch folder and never under a release or evidence root."""

    names = {part.lower() for part in root.resolve().parts}
    if "scratch" not in names:
        raise FetchRefused("DRY_RUN_ROOT_NOT_SCRATCH")
    if names & {"releases", "evidence"}:
        raise FetchRefused("DRY_RUN_ROOT_IN_RELEASE")


def run_routes(
    transport: CdpTransport,
    allowlist: Allowlist,
    root: Path,
    *,
    dry_run: bool = False,
    target_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    download_timeout: float = 180.0,
    settle: float = 2.0,
) -> dict[str, Any]:
    """Read every allowlisted in-page route; same code path for a dry run.

    A dry run differs only in where it writes: the root must be under a scratch folder,
    the files land in ``dry-run-capture`` (never ``capture``, the name the staging and
    generation steps read), and the manifest says it is not evidence. Nothing else
    changes, so a dry run exercises exactly what the real run will do.
    """

    try:
        require_current_interpreter("the P123 CDP fetcher")
    except InterpreterRefusal as exc:
        raise FetchRefused("INTERPRETER_REFUSED", str(exc)) from exc
    if not allowlist.routes:
        raise FetchRefused("ALLOWLIST_EMPTY", "no routes")
    pages, fetches = route_urls(allowlist)
    for url in pages | fetches:  # screen the whole allowlist before anything is sent
        screen_route_url(url, pages | fetches)
    if dry_run:
        _assert_dry_run_root(root)
    capture_name = "dry-run-capture" if dry_run else "capture"
    try:
        recorder.assert_output_root(root)
    except recorder.RecorderError as exc:
        raise FetchRefused(exc.code) from exc
    root.mkdir(parents=True)
    incoming = root / "incoming"
    capture = root / capture_name
    incoming.mkdir()
    (capture / "raw").mkdir(parents=True)
    session = Session(
        transport,
        allowed_urls=pages,
        incoming=incoming,
        tab_ok=p123_tab_ok,
        tab_known=recorder.origin_allowed,
        no_download_code="STOP_REAUTH",
        clock=clock,
        download_timeout=download_timeout,
        inpage_expressions=frozenset(
            {PROBE_EXPRESSION} | {fetch_expression(url) for url in fetches}
        ),
    )
    landed: list[Landed] = []
    records: list[dict[str, Any]] = []
    visited: list[str] = []
    stop: FetchStop | None = None
    groups: dict[str, list[tuple[Route, int]]] = {}
    inceptions: dict[int, date] = {}
    # the model is read before its curve (stable sort keeps the rest of the order)
    for name, ident in sorted(allowlist.routes, key=lambda e: e[0] != "designer_model"):
        route = ROUTE_BY_NAME[name]
        groups.setdefault(route_host_url(route, ident), []).append((route, ident))
    try:
        session.attach(target_id)
        for host, entries in groups.items():
            session.visit(host, settle=settle)
            visited.append(urlsplit(host).path)
            for route, ident in entries:
                if route.shape == "visit":
                    for url in route_visit_urls(route, ident)[1:]:
                        session.visit(url, settle=settle)
                        visited.append(
                            urlsplit(url).path + "?t=" + url.rsplit("=", 1)[1]
                        )
                    continue
                url = route_target_url(route, ident)
                screen_route_url(url, fetches)
                relative = f"raw/{route.saved.format(id=ident)}"
                digest, size, rows = session.fetch_in_page(
                    url, capture / relative, route, ident, inceptions
                )
                landed.append(
                    Landed(route.name, str(ident), relative, digest, size, rows)
                )
                records.append(
                    {
                        "kind": route.name,
                        "id": ident,
                        "relative_path": relative,
                        "sha256": digest,
                        "size_bytes": size,
                        "rows": rows,
                    }
                )
                _replace(capture / "capture-manifest.json", _capture_manifest(landed))
    except FetchStop as exc:
        stop = exc
    except FetchRefused as exc:
        stop = FetchStop("STOP_REFUSED", exc.code)
    finally:
        session.release()
    fetched = {(r["kind"], r["id"]) for r in records}
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "routes",
        "dry_run": dry_run,
        "evidence_eligible": not dry_run,
        "status": "STOPPED" if stop else "COMPLETE",
        "stop": {"code": stop.code, "detail": stop.detail} if stop else None,
        "allowlist_sha256": allowlist.sha256,
        "capture_directory": capture_name,
        "routes": records,
        "pages_visited": visited,
        "routes_not_fetched": [
            f"{name}:{ident}"
            for name, ident in allowlist.routes
            if ROUTE_BY_NAME[name].shape != "visit" and (name, ident) not in fetched
        ],
        "safety": {
            "cdp_methods": sorted(
                (ALLOWED_CDP_METHODS - NETWORK_CDP_METHODS) | INPAGE_CDP_METHODS
            ),
            "body_reads": "in-page GET of allowlisted routes only",
            "input_events": False,
        },
    }
    manifest["manifest_sha256"] = _write_new(
        root
        / ("p123-dry-run-manifest.json" if dry_run else "p123-fetch-manifest.json"),
        canonical_bytes(manifest),
    )
    if stop:
        manifest["_stop"] = stop
    return manifest


def run_drive(
    transport: CdpTransport,
    allowlist: Allowlist,
    file_id: str,
    root: Path,
    *,
    target_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    download_timeout: float = 600.0,
) -> dict[str, Any]:
    url = drive_url(file_id)
    screen_drive_url(url, allowlist)
    incoming, capture, base = _prepare_root(root)
    session = Session(
        transport,
        allowed_urls=frozenset({url}),
        incoming=incoming,
        tab_ok=drive_tab_ok,
        tab_known=drive_tab_ok,
        no_download_code="STOP_DOWNLOAD_NOT_EXACT",
        clock=clock,
        download_timeout=download_timeout,
    )
    stop: FetchStop | None = None
    record: dict[str, Any] | None = None
    try:
        session.attach(target_id)

        def start_ok(actual: str, suggested: str) -> bool:
            return (
                drive_host_ok(actual)
                and bool(suggested)
                and len(suggested) <= 255
                and not re.search(r"[\\/]|\.\.", suggested)
            )

        relative = f"files/drive_{file_id}"
        digest, size, rows, extension = session.download(
            url, start_ok=start_ok, saved=capture / (relative + ".part")
        )
        final = capture / (relative + extension)
        (capture / (relative + ".part")).rename(final)
        record = {
            "relative_path": f"capture/{relative}{extension}",
            "sha256": digest,
            "size_bytes": size,
        }
    except FetchStop as exc:
        stop = exc
    except FetchRefused as exc:
        stop = FetchStop("STOP_REFUSED", exc.code)
    finally:
        session.release()
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "drive",
        "status": "STOPPED" if stop else "COMPLETE",
        "stop": {"code": stop.code, "detail": stop.detail} if stop else None,
        "allowlist_sha256": allowlist.sha256,
        "file": record,
        "safety": {
            "cdp_methods": sorted(ALLOWED_CDP_METHODS - NETWORK_CDP_METHODS),
            "body_reads": "none",
            "input_events": False,
        },
    }
    manifest["manifest_sha256"] = _write_new(
        base / "p123-fetch-manifest.json", canonical_bytes(manifest)
    )
    if stop:
        manifest["_stop"] = stop
    return manifest


# ---------------------------------------------------------------------------
# CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make-allowlist", help="write a canonical allowlist JSON")
    make.add_argument("--out", type=Path, required=True)
    make.add_argument("--book", type=int, action="append", default=[])
    make.add_argument("--designer-model", type=int, action="append", default=[])
    make.add_argument("--drive-file-id", action="append", default=[])
    make.add_argument(
        "--page-record",
        action="store_true",
        help="also read the account investment list (the identity check's page record)",
    )
    make.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="KIND:ID",
        help="in-page route entry for the routes command, e.g. designer_model:123 "
        "(kinds: " + ", ".join(r.name for r in ROUTES) + ")",
    )
    make.add_argument(
        "--page-get",
        action="append",
        default=[],
        metavar="KIND:ID",
        help="alias of --route",
    )
    make.add_argument(
        "--kind",
        action="append",
        default=[],
        choices=[k.name for k in KINDS],
        help="export kinds per book (default: all three)",
    )
    routes_parser = sub.add_parser(
        "routes", help="read the allowlisted in-page routes (see --dry-run)"
    )
    routes_parser.add_argument("--cdp-url", required=True)
    routes_parser.add_argument("--allowlist", type=Path, required=True)
    routes_parser.add_argument("--allowlist-sha256", required=True)
    routes_parser.add_argument("--download-dir", type=Path, required=True)
    routes_parser.add_argument("--target-id")
    routes_parser.add_argument("--download-timeout", type=float)
    routes_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="same code path into a scratch root; never evidence, never a release root",
    )
    for name in ("run", "drive"):
        item = sub.add_parser(name)
        item.add_argument("--cdp-url", required=True)
        item.add_argument("--allowlist", type=Path, required=True)
        item.add_argument("--allowlist-sha256", required=True)
        item.add_argument("--download-dir", type=Path, required=True)
        item.add_argument("--target-id")
        item.add_argument("--download-timeout", type=float)
        if name == "run":
            item.add_argument(
                "--reference-index",
                type=Path,
                help="source-index.json of the 2026-07-31 run (required unless "
                "--no-reference)",
            )
            item.add_argument(
                "--no-reference",
                action="store_true",
                help="run without a reference pull (recorded in the manifest)",
            )
            item.add_argument(
                "--prior-dir",
                type=Path,
                action="append",
                default=[],
                help="older or HAR download set to compare for permuted rows (warn)",
            )
            item.add_argument(
                "--cross-capture-dir",
                type=Path,
                action="append",
                default=[],
                help="other capture directory of the same pull (shared rows fail)",
            )
            item.add_argument(
                "--as-of",
                type=date.fromisoformat,
                help="financial cutoff D (YYYY-MM-DD) the identity check rolls holdings "
                "back to; required, and equal to the generation plan's as-of date",
            )
            item.add_argument("--skip-designer-models", action="store_true")
            item.add_argument(
                "--dry-run",
                action="store_true",
                help="screen the whole allowlist and print the plan; no browser and "
                "nothing written (use `routes --dry-run` to rehearse a route read)",
            )
        else:
            item.add_argument("--file-id", required=True)
    return parser


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": manifest["status"],
        "mode": manifest["mode"],
        "manifest_sha256": manifest["manifest_sha256"],
    }
    if manifest["mode"] == "routes":
        out["dry_run"] = manifest["dry_run"]
        out["routes"] = len(manifest["routes"])
        out["routes_not_fetched"] = len(manifest["routes_not_fetched"])
    if manifest["mode"] == "p123":
        out["books"] = {
            book: {"files": len(item["files"]), "identity": item["identity"]}
            for book, item in manifest["books"].items()
        }
        out["books_not_attempted"] = len(manifest["books_not_attempted"])
        out["review_pending"] = manifest["review_pending"]
    return out


def dry_run_plan(allowlist: Allowlist) -> dict[str, Any]:
    """Screen every URL of the allowlist exactly as a run would; send nothing."""

    exports = allowed_p123_urls(allowlist)
    for url in exports:
        screen_p123_url(url, exports)
    designers = {designer_url(m): m for m in allowlist.designer_models}
    for url in designers:
        screen_designer_url(url, designers)
    pages, fetches = route_urls(allowlist)
    for url in pages | fetches:
        screen_route_url(url, pages | fetches)
    return {
        "dry_run": True,
        "allowlist_sha256": allowlist.sha256,
        "exports": len(exports),
        "designer_models": len(designers),
        "routes": [f"{k}:{i}" for k, i in allowlist.routes],
        "page_record": allowlist.page_record,
    }


def _route_arg(item: str) -> tuple[str, int]:
    kind, _, ident = item.partition(":")
    if kind not in ROUTE_BY_NAME or not ident.isdigit():
        raise FetchRefused("ALLOWLIST_INVALID", "route argument")
    return kind, _positive(int(ident), "route id")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "make-allowlist":
            routes = [_route_arg(item) for item in (*args.route, *args.page_get)]
            if (
                not args.book
                and not args.designer_model
                and not args.drive_file_id
                and not routes
            ):
                raise FetchRefused("ALLOWLIST_EMPTY")
            payload = build_allowlist_bytes(
                args.book,
                args.designer_model,
                args.drive_file_id,
                args.kind,
                args.page_record,
                routes,
            )
            parse_allowlist(payload, hashlib.sha256(payload).hexdigest())
            digest = _write_new(args.out, payload)
            print(json.dumps({"allowlist_sha256": digest}))
            return 0
        allowlist = load_allowlist(args.allowlist, args.allowlist_sha256)
        timeout = args.download_timeout
        extra: dict[str, Any] = {"download_timeout": timeout} if timeout else {}
        if args.command == "run" and args.dry_run:
            print(json.dumps(dry_run_plan(allowlist), sort_keys=True))
            return 0
        if args.command == "run" and allowlist.routes:
            if allowlist.books or allowlist.designer_models or allowlist.page_record:
                raise FetchRefused("ROUTES_MIXED_WITH_BOOKS", "use separate allowlists")
            args.command = "routes"  # route-only allowlist: no identity check, no as-of
            args.dry_run = False
        if args.command == "routes":
            refusal = check_entry_interpreter(
                "the P123 CDP fetcher (in-page routes)", "p123-cdp-fetcher"
            )
            if refusal is not None:
                return refusal
            transport = connect(args.cdp_url)
            try:
                manifest = run_routes(
                    transport,
                    allowlist,
                    args.download_dir,
                    dry_run=args.dry_run,
                    target_id=args.target_id,
                    **extra,
                )
            finally:
                transport.close()
        elif args.command == "run":
            refusal = check_entry_interpreter(
                "the P123 CDP fetcher (it runs the book identity check)",
                "p123-cdp-fetcher",
            )
            if refusal is not None:
                return refusal
            if args.reference_index is None and not args.no_reference:
                raise FetchRefused("REFERENCE_REQUIRED")
            if args.as_of is None:
                raise FetchRefused("AS_OF_REQUIRED", "pass --as-of D")
            check = load_identity_check(
                prior_dirs=args.prior_dir,
                cross_dirs=args.cross_capture_dir,
                as_of=args.as_of,
            )
            transport = connect(args.cdp_url)
            try:
                manifest = run_p123(
                    transport,
                    allowlist,
                    args.download_dir,
                    identity_check=check,
                    reference_index=args.reference_index,
                    no_reference=args.no_reference,
                    skip_designer_models=args.skip_designer_models,
                    target_id=args.target_id,
                    **extra,
                )
            finally:
                transport.close()
        else:
            transport = connect(args.cdp_url)
            try:
                manifest = run_drive(
                    transport,
                    allowlist,
                    args.file_id,
                    args.download_dir,
                    target_id=args.target_id,
                    **extra,
                )
            finally:
                transport.close()
    except FetchRefused as exc:
        print(f"REFUSED {exc.code}")
        return EXIT_REFUSED
    except FetchStop as exc:
        print(f"STOP {exc.code}")
        return EXIT_STOP
    stop = manifest.get("_stop")
    print(json.dumps(_summary(manifest), sort_keys=True, separators=(",", ":")))
    if stop is not None:
        print(f"STOP {stop.code}")
        return EXIT_STOP
    if manifest["status"] == "REVIEW_PENDING":
        # Nothing non-PASS passes silently: every REVIEW book and its checks are named.
        for book, names in sorted(manifest["review_pending"].items()):
            print(f"REVIEW_PENDING book={book} checks={','.join(names) or '-'}")
        return EXIT_REVIEW_PENDING
    return 0


if __name__ == "__main__":
    sys.exit(main())
