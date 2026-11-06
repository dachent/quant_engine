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
no ``Runtime.*``, no ``Fetch.*``, no ``Network.*``, no ``Input.*``. Because reading a
JSON page body would need one of those, designer-model entries are validated but not
fetched (see ``docs/providers/p123-capture.md``).

Stops (exit 3) rather than continuing: the tab leaves https://www.portfolio123.com or
lands on a login page, no download starts, a download is not exactly the expected file,
or the per-book identity check (``tools/p123_book_identity_check.py``) fails.

A second mode downloads one Google Drive file by direct-download URL under an exact
host allowlist, into a new directory, with a sha256 manifest.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote_plus, urlsplit

_TOOLS_DIRECTORY = Path(__file__).resolve().parent
if str(_TOOLS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIRECTORY))

from quant_engine.web_contracts import WebContractError  # noqa: E402
from quant_engine.web_contracts.portfolio123 import PORTFOLIO123_CONTRACT  # noqa: E402
import p123_cdp_recorder as recorder  # noqa: E402


SCHEMA = "p123-cdp-fetcher/1"
ALLOWLIST_SCHEMA_VERSION = 1
ORIGIN = recorder.ORIGIN
P123_HOST = "www.portfolio123.com"
#: Reuse the recorder's reviewed constant: a URL containing one of these is refused.
MUTATION_WORDS: tuple[str, ...] = recorder.MUTATION_WORDS
_MUTATION_RE = re.compile("|".join(MUTATION_WORDS))

#: The only messages this process may send to the browser.
ALLOWED_CDP_METHODS = frozenset(
    {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Page.navigate",
        "Browser.setDownloadBehavior",
    }
)
#: Exit codes: 0 done, 2 refused before sending, 3 stopped mid-run.
EXIT_REFUSED = 2
EXIT_STOP = 3

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
    sha256: str


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
    keys = {"schema_version", "origin", "books", "designer_models", "drive_files"}
    if not isinstance(data, dict) or set(data) != keys:
        raise FetchRefused("ALLOWLIST_INVALID", "keys")
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
    return Allowlist(tuple(books), tuple(models), tuple(drive), digest)


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
) -> bytes:
    chosen = list(kinds) if kinds else [k.name for k in KINDS]
    return canonical_bytes(
        {
            "schema_version": ALLOWLIST_SCHEMA_VERSION,
            "origin": ORIGIN,
            "books": [{"id": b, "kinds": chosen} for b in book_ids],
            "designer_models": list(designer_ids),
            "drive_files": list(drive_ids),
        }
    )


def book_url(kind: Kind, book_id: int) -> str:
    return f"{ORIGIN}{kind.path}?portid={book_id}"


def designer_url(model_id: int) -> str:
    return f"{ORIGIN}/spr/strategy/model/{model_id}?dm=true"


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
# Guarded transport


class CdpTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class GuardedTransport:
    """Send only the four allowed methods, with their arguments pinned."""

    def __init__(
        self,
        inner: CdpTransport,
        *,
        allowed_urls: frozenset[str],
        download_path: str,
    ) -> None:
        self._inner = inner
        self._urls = allowed_urls
        self._download_path = download_path

    def send(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method not in ALLOWED_CDP_METHODS:
            raise FetchRefused("CDP_METHOD_REFUSED", str(method))
        if method == "Page.navigate":
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
    ) -> None:
        self._transport = GuardedTransport(
            transport, allowed_urls=allowed_urls, download_path=str(incoming)
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
            raise FetchRefused("NO_SIGNED_IN_TAB")
        good = {tid: url for tid, url in candidates.items() if self._tab_ok(url)}
        if not good:
            raise FetchStop("STOP_REAUTH", "the only candidate tab is on a login page")
        if len(good) > 1:
            raise FetchRefused("TARGET_AMBIGUOUS", f"{len(good)} candidate tabs")
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
        self._call("Page.navigate", {"url": url}, self._sid)
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


# ---------------------------------------------------------------------------
# Identity check wiring (tools/p123_book_identity_check.py, PR 827)

IdentityCheck = Callable[[Path, Sequence[str], Path | None], Mapping[str, Any]]


def load_identity_check(directory: Path | None = None) -> IdentityCheck:
    """Load the per-book identity check by file path; refuse when it is absent.

    ``tools/p123_book_identity_check.py`` arrived with PR 827. Until that is on the
    branch, or when its package dependency is missing, the fetcher refuses to start a
    Portfolio123 run (there is no skip flag).
    """

    path = (
        directory or Path(__file__).resolve().parent
    ) / "p123_book_identity_check.py"
    if not path.is_file():
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", "module file not present")
    spec = importlib.util.spec_from_file_location("p123_book_identity_check", path)
    if spec is None or spec.loader is None:
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", "not loadable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any import failure means unavailable
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", type(exc).__name__) from exc
    check = getattr(module, "check_capture", None)
    if not callable(check):
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE", "no check_capture")

    def run(
        capture: Path, books: Sequence[str], reference_index: Path | None
    ) -> Mapping[str, Any]:
        result = check(
            capture,
            books=list(books),
            raw_subdir="raw",
            use_manifest=True,
            reference_index=reference_index,
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
    skip_designer_models: bool = False,
    require_identity_pass: bool = False,
    target_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    download_timeout: float = 180.0,
) -> dict[str, Any]:
    """Fetch every allowlisted export; return the manifest (also written to disk)."""

    if allowlist.designer_models and not skip_designer_models:
        raise FetchRefused(
            "DESIGNER_MODEL_NEEDS_BODY_READ",
            "designer-model JSON is a page, not a download; reading it needs "
            "Network.getResponseBody. Pass --skip-designer-models to fetch the "
            "books only",
        )
    if not allowlist.books:
        raise FetchRefused("ALLOWLIST_EMPTY")
    urls = allowed_p123_urls(allowlist)
    for url in urls:  # screen the whole allowlist before anything is sent
        screen_p123_url(url, urls)
    incoming, capture, base = _prepare_root(root)
    session = Session(
        transport,
        allowed_urls=frozenset(urls),
        incoming=incoming,
        tab_ok=p123_tab_ok,
        tab_known=recorder.origin_allowed,
        no_download_code="STOP_REAUTH",
        clock=clock,
        download_timeout=download_timeout,
    )
    landed: list[Landed] = []
    books: dict[str, dict[str, Any]] = {}
    stop: FetchStop | None = None
    report: Mapping[str, Any] = {}
    try:
        session.attach(target_id)
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
            report = identity_check(
                capture,
                [str(b.book_id) for b in allowlist.books if str(b.book_id) in books],
                reference_index,
            )
            result = report.get("books", {}).get(str(book.book_id), {})
            decision = str(result.get("decision", "FAIL"))
            entry["identity"] = decision
            entry["identity_checks"] = {
                str(c.get("check")): str(c.get("status"))
                for c in result.get("checks", [])
            }
            if decision == "FAIL" or (require_identity_pass and decision != "PASS"):
                raise FetchStop(
                    "STOP_IDENTITY_FAIL", f"book identity decision {decision}"
                )
    except FetchStop as exc:
        stop = exc
    finally:
        session.release()
    if report:
        _write_new(
            base / "book-identity-decisions.json",
            (json.dumps(report, indent=1, sort_keys=True, default=str) + "\n").encode(),
        )
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "p123",
        "status": "STOPPED" if stop else "COMPLETE",
        "stop": {"code": stop.code, "detail": stop.detail} if stop else None,
        "allowlist_sha256": allowlist.sha256,
        "capture_directory": "capture",
        "books": books,
        "books_not_attempted": [
            str(b.book_id) for b in allowlist.books if str(b.book_id) not in books
        ],
        "designer_models_not_fetched": [str(m) for m in allowlist.designer_models],
        "safety": {
            "cdp_methods": sorted(ALLOWED_CDP_METHODS),
            "reads_page_body": False,
            "input_events": False,
        },
    }
    manifest["manifest_sha256"] = _write_new(
        base / "p123-fetch-manifest.json", canonical_bytes(manifest)
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
            "cdp_methods": sorted(ALLOWED_CDP_METHODS),
            "reads_page_body": False,
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
        "--kind",
        action="append",
        default=[],
        choices=[k.name for k in KINDS],
        help="export kinds per book (default: all three)",
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
            item.add_argument("--reference-index", type=Path)
            item.add_argument("--skip-designer-models", action="store_true")
            item.add_argument("--require-identity-pass", action="store_true")
        else:
            item.add_argument("--file-id", required=True)
    return parser


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": manifest["status"],
        "mode": manifest["mode"],
        "manifest_sha256": manifest["manifest_sha256"],
    }
    if manifest["mode"] == "p123":
        out["books"] = {
            book: {"files": len(item["files"]), "identity": item["identity"]}
            for book, item in manifest["books"].items()
        }
        out["books_not_attempted"] = len(manifest["books_not_attempted"])
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "make-allowlist":
            if not args.book and not args.designer_model and not args.drive_file_id:
                raise FetchRefused("ALLOWLIST_EMPTY")
            payload = build_allowlist_bytes(
                args.book, args.designer_model, args.drive_file_id, args.kind
            )
            parse_allowlist(payload, hashlib.sha256(payload).hexdigest())
            digest = _write_new(args.out, payload)
            print(json.dumps({"allowlist_sha256": digest}))
            return 0
        allowlist = load_allowlist(args.allowlist, args.allowlist_sha256)
        timeout = args.download_timeout
        if args.command == "run":
            check = load_identity_check()
            transport = connect(args.cdp_url)
            try:
                extra: dict[str, Any] = {"download_timeout": timeout} if timeout else {}
                manifest = run_p123(
                    transport,
                    allowlist,
                    args.download_dir,
                    identity_check=check,
                    reference_index=args.reference_index,
                    skip_designer_models=args.skip_designer_models,
                    require_identity_pass=args.require_identity_pass,
                    target_id=args.target_id,
                    **extra,
                )
            finally:
                transport.close()
        else:
            transport = connect(args.cdp_url)
            try:
                extra = {"download_timeout": timeout} if timeout else {}
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
