"""Drive the owner's signed-in Quantbase tab through a FIXED list of read-only pages.

Pairs with the passive recorder (``tools/quantbase_cdp_har_recorder.py``, a separate
process) so an unattended capture can load the pages the owner would otherwise open
by hand while the recorder records the traffic. This tool records nothing itself.

Hard limits, enforced before a message leaves the process:

* The only CDP messages it can send are ``Target.getTargets`` (discovery),
  ``Target.attachToTarget``, ``Page.enable`` and ``Page.navigate``. There is no
  ``Input.*``, ``Runtime.*``, ``Fetch.*``, ``Network.*`` or ``Page.reload``. A
  ``Page.navigate`` is sent only with exactly ``{"url": <allow-listed url>}``.
* The CDP URL must be a loopback ``http://host:port`` endpoint.
* Navigation targets come ONLY from a hash-bound allow-list file (data, not code).
  Every entry is validated before any connection is made: https, the exact host
  ``app.getquantbase.com``, no userinfo, port or fragment, no placeholder, no
  mutation-like word (the recorder's own constant) and no login-looking path.
* After every navigation the tab URL is read back (``Target.getTargets``). If it is
  no longer on ``app.getquantbase.com``, or its path looks like a login page, or any
  CDP call fails, the run STOPS at once: no further navigation, exit code 3 and the
  message ``STOP_REAUTH``.

Output is one JSON object per line (allow-list hash, one per step, a stop line when
stopped, a summary). It never contains a header, a cookie, a body or the tab URL.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from quantbase_cdp_har_recorder import (
    APP_ORIGIN,
    MUTATION_WORDS,
    CdpTransport,
    RecorderError,
    WebSocketTransport,
    parse_cdp_url,
)


SCHEMA_VERSION = 1
APP_HOST = "app.getquantbase.com"
DEFAULT_DWELL_SECONDS = 5.0
MAX_DWELL_SECONDS = 120.0
DEFAULT_LOAD_TIMEOUT_SECONDS = 30.0
MAX_PAGES = 50

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_REFUSED = 2
EXIT_STOP_REAUTH = 3

#: The only messages this process may send to the browser.
ALLOWED_CDP_METHODS = frozenset(
    {
        "Target.getTargets",
        "Target.attachToTarget",
        "Page.enable",
        "Page.navigate",
    }
)

_MUTATION_RE = re.compile("|".join(re.escape(word) for word in MUTATION_WORDS))
#: A path with one of these segments-in-text is a sign-in flow, never a read-only page.
_LOGIN_RE = re.compile(
    r"login|log-in|sign-?in|sign-?up|sign-?out|logout|log-out|oauth|(^|[/_-])sso($|[/_.-])"
    r"|authenticate|/auth(/|$)|session-expired|forgot|reset-password|verify-email"
)
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PRINTABLE_ASCII_RE = re.compile(r"[\x21-\x7e]+")


class NavigatorError(ValueError):
    """The run or request is refused; ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class StopReauth(Exception):
    """Navigation must stop now; ``reason`` is a stable code."""

    def __init__(self, reason: str, label: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.label = label


# ---------------------------------------------------------------------------
# URL and allow-list validation (pure)


def looks_like_login(path: str) -> bool:
    return bool(_LOGIN_RE.search(unquote(path).lower()))


def on_app_origin(url: str) -> bool:
    """True when ``url`` is https on exactly the app host (default port, no userinfo)."""

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == APP_HOST
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


def validate_url(url: object) -> str:
    """Return ``url`` if it may ever be navigated to, else raise a coded refusal."""

    if not isinstance(url, str) or not _PRINTABLE_ASCII_RE.fullmatch(url):
        raise NavigatorError("URL_NOT_PRINTABLE_ASCII")
    if "\\" in url:
        raise NavigatorError("URL_PATH_UNSAFE")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise NavigatorError("URL_UNPARSABLE") from None
    if parts.scheme != "https":
        raise NavigatorError("URL_NOT_HTTPS")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise NavigatorError("URL_HAS_USERINFO")
    if parts.hostname != APP_HOST:
        raise NavigatorError("URL_OFF_ORIGIN")
    if port is not None:
        raise NavigatorError("URL_HAS_PORT")
    if parts.netloc != APP_HOST:
        raise NavigatorError("URL_OFF_ORIGIN")
    if parts.fragment or "#" in url:
        raise NavigatorError("URL_HAS_FRAGMENT")
    decoded_once = unquote(url)
    if "{" in decoded_once or "}" in decoded_once:
        raise NavigatorError("URL_HAS_PLACEHOLDER")
    if ".." in unquote(parts.path).split("/"):
        raise NavigatorError("URL_PATH_UNSAFE")
    if not parts.path.startswith("/"):
        raise NavigatorError("URL_PATH_UNSAFE")
    decoded = unquote(url).lower()
    if _MUTATION_RE.search(decoded):
        raise NavigatorError("URL_MUTATION_LIKE")
    if looks_like_login(parts.path):
        raise NavigatorError("URL_LOGIN_LIKE")
    return url


def _dwell(value: object, where: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= MAX_DWELL_SECONDS
    ):
        raise NavigatorError("ALLOWLIST_DWELL_INVALID", where)
    return value + 0.0


@dataclass(frozen=True)
class Page:
    label: str
    url: str
    dwell_seconds: float


@dataclass(frozen=True)
class Allowlist:
    sha256: str
    pages: tuple[Page, ...]

    @property
    def urls(self) -> frozenset[str]:
        return frozenset(page.url for page in self.pages)


def parse_allowlist(raw: bytes, *, pin_sha256: str | None = None) -> Allowlist:
    """Hash, check the pin, then validate every entry. Nothing here touches a browser."""

    digest = hashlib.sha256(raw).hexdigest()
    if pin_sha256 is not None:
        if not _SHA256_RE.fullmatch(pin_sha256):
            raise NavigatorError("PIN_NOT_SHA256")
        if pin_sha256 != digest:
            raise NavigatorError("ALLOWLIST_SHA256_MISMATCH")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise NavigatorError("ALLOWLIST_NOT_JSON") from None
    if not isinstance(document, dict):
        raise NavigatorError("ALLOWLIST_SHAPE")
    unknown = set(document) - {"schema_version", "pages", "dwell_seconds", "note"}
    if unknown or document.get("schema_version") != SCHEMA_VERSION:
        raise NavigatorError("ALLOWLIST_SCHEMA_VERSION")
    default_dwell = DEFAULT_DWELL_SECONDS
    if "dwell_seconds" in document:
        default_dwell = _dwell(document["dwell_seconds"], "default")
    items = document.get("pages")
    if not isinstance(items, list) or not items or len(items) > MAX_PAGES:
        raise NavigatorError("ALLOWLIST_PAGES_INVALID")
    pages: list[Page] = []
    labels: set[str] = set()
    urls: set[str] = set()
    for index, item in enumerate(items):
        where = str(index)
        if not isinstance(item, dict) or set(item) - {"label", "url", "dwell_seconds"}:
            raise NavigatorError("ALLOWLIST_ENTRY_SHAPE", where)
        label = item.get("label")
        if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
            raise NavigatorError("ALLOWLIST_LABEL_INVALID", where)
        try:
            url = validate_url(item.get("url"))
        except NavigatorError as exc:
            raise NavigatorError(exc.code, where) from None
        if label in labels:
            raise NavigatorError("ALLOWLIST_LABEL_DUPLICATE", where)
        if url in urls:
            raise NavigatorError("ALLOWLIST_URL_DUPLICATE", where)
        labels.add(label)
        urls.add(url)
        dwell = (
            _dwell(item["dwell_seconds"], where)
            if "dwell_seconds" in item
            else default_dwell
        )
        pages.append(Page(label, url, dwell))
    return Allowlist(digest, tuple(pages))


# ---------------------------------------------------------------------------
# CDP guard


class GuardedTransport:
    """Wrap a transport so only the four allowed messages can ever be sent, and
    ``Page.navigate`` only ever carries exactly one allow-listed URL."""

    def __init__(self, inner: CdpTransport, allowed_urls: frozenset[str]) -> None:
        self._inner = inner
        self._urls = allowed_urls

    def send(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method not in ALLOWED_CDP_METHODS:
            raise NavigatorError("CDP_METHOD_REFUSED", str(method))
        if method == "Page.navigate":
            params = message.get("params")
            if (
                not isinstance(params, dict)
                or set(params) != {"url"}
                or params["url"] not in self._urls
            ):
                raise NavigatorError("CDP_NAVIGATE_URL_REFUSED")
        self._inner.send(message)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        return self._inner.receive(timeout)

    def close(self) -> None:
        self._inner.close()


# ---------------------------------------------------------------------------
# Navigator


class Navigator:
    def __init__(
        self,
        transport: CdpTransport,
        allowlist: Allowlist,
        *,
        emit: Callable[[Mapping[str, Any]], None],
        clock: Callable[[], float] = time.monotonic,
        load_timeout: float = DEFAULT_LOAD_TIMEOUT_SECONDS,
        call_timeout: float = 20.0,
    ) -> None:
        self._transport = GuardedTransport(transport, allowlist.urls)
        self._allowlist = allowlist
        self._emit = emit
        self._clock = clock
        self._load_timeout = load_timeout
        self._call_timeout = call_timeout
        self._next_id = 0
        self._events: deque[dict[str, Any]] = deque()
        self._target_id = ""
        self._sid = ""

    def _call(
        self, method: str, params: dict[str, Any], sid: str | None
    ) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        if sid is not None:
            message["sessionId"] = sid
        self._transport.send(message)
        deadline = self._clock() + self._call_timeout
        while True:
            reply = self._transport.receive(0.5)
            if reply is None:
                if self._clock() > deadline:
                    raise StopReauth("CDP_ERROR")
                continue
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise StopReauth("CDP_ERROR")
                result = reply.get("result", {})
                return result if isinstance(result, dict) else {}
            if "id" not in reply:
                self._events.append(reply)

    def _tab_url(self) -> str:
        result = self._call("Target.getTargets", {}, None)
        infos = result.get("targetInfos")
        if not isinstance(infos, list):
            raise StopReauth("CDP_ERROR")
        for info in infos:
            if isinstance(info, dict) and info.get("targetId") == self._target_id:
                url = info.get("url")
                return url if isinstance(url, str) else ""
        raise StopReauth("TAB_GONE")

    def _attach(self) -> None:
        result = self._call("Target.getTargets", {}, None)
        infos = result.get("targetInfos")
        if not isinstance(infos, list):
            raise StopReauth("CDP_ERROR")
        tabs = [
            i
            for i in infos
            if isinstance(i, dict)
            and i.get("type") == "page"
            and isinstance(i.get("url"), str)
            and i["url"].startswith(APP_ORIGIN + "/")
            and isinstance(i.get("targetId"), str)
        ]
        if not tabs:
            raise StopReauth("NO_TAB")
        if len(tabs) > 1:
            raise NavigatorError("MULTIPLE_QUANTBASE_TABS", str(len(tabs)))
        tab = tabs[0]
        if not on_app_origin(tab["url"]) or looks_like_login(urlsplit(tab["url"]).path):
            raise StopReauth("LOGIN_REDIRECT")
        self._target_id = str(tab["targetId"])
        attached = self._call(
            "Target.attachToTarget",
            {"targetId": self._target_id, "flatten": True},
            None,
        )
        sid = attached.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise StopReauth("CDP_ERROR")
        self._sid = sid
        self._call("Page.enable", {}, sid)

    def _drain(self, seconds: float) -> bool:
        """Read events for up to ``seconds``; True if this session fired its load event."""

        loaded = False
        deadline = self._clock() + seconds
        while True:
            if self._events:
                message = self._events.popleft()
            else:
                if self._clock() >= deadline:
                    return loaded
                incoming = self._transport.receive(0.5)
                if incoming is None:
                    continue
                message = incoming
            if (
                message.get("method") == "Page.loadEventFired"
                and message.get("sessionId") == self._sid
            ):
                loaded = True

    def _wait_for_load(self) -> bool:
        deadline = self._clock() + self._load_timeout
        while self._clock() < deadline:
            if self._events:
                message = self._events.popleft()
            else:
                incoming = self._transport.receive(0.5)
                if incoming is None:
                    continue
                message = incoming
            if (
                message.get("method") == "Page.loadEventFired"
                and message.get("sessionId") == self._sid
            ):
                return True
        return False

    def run(self) -> int:
        self._emit(
            {
                "event": "allowlist",
                "sha256": self._allowlist.sha256,
                "pages": len(self._allowlist.pages),
            }
        )
        counts = {"ok": 0, "timeout": 0}
        done = 0
        stopped = ""
        try:
            self._attach()
            for index, page in enumerate(self._allowlist.pages):
                try:
                    result = self._visit(page)
                except StopReauth as stop:
                    stop.label = page.label
                    raise
                counts[result["result"]] += 1
                done += 1
                self._emit(
                    {"event": "step", "index": index, "label": page.label, **result}
                )
        except StopReauth as stop:
            stopped = stop.reason
            self._emit(
                {
                    "event": "stop",
                    "code": "STOP_REAUTH",
                    "reason": stop.reason,
                    "at": stop.label or None,
                }
            )
            print("STOP_REAUTH", file=sys.stderr, flush=True)
        except RecorderError as exc:
            stopped = "CDP_ERROR"
            self._emit(
                {
                    "event": "stop",
                    "code": "STOP_REAUTH",
                    "reason": "CDP_ERROR",
                    "at": None,
                }
            )
            print(f"STOP_REAUTH {exc.code}", file=sys.stderr, flush=True)
        self._emit(
            {
                "event": "summary",
                "allowlist_sha256": self._allowlist.sha256,
                "planned": len(self._allowlist.pages),
                "attempted": done,
                "ok": counts["ok"],
                "timeout": counts["timeout"],
                "stopped": stopped or None,
            }
        )
        if stopped:
            return EXIT_STOP_REAUTH
        return EXIT_OK if counts["timeout"] == 0 else EXIT_INCOMPLETE

    def _visit(self, page: Page) -> dict[str, Any]:
        self._events.clear()
        reply = self._call("Page.navigate", {"url": page.url}, self._sid)
        if reply.get("errorText"):
            raise StopReauth("NAVIGATE_ERROR")
        loaded = self._wait_for_load()
        self._drain(page.dwell_seconds)
        final = urlsplit(self._tab_url())
        if not on_app_origin(final.geturl()):
            raise StopReauth("OFF_ORIGIN")
        if looks_like_login(final.path):
            raise StopReauth("LOGIN_REDIRECT")
        requested = urlsplit(page.url)
        same = (final.path, final.query) == (requested.path, requested.query)
        return {
            "result": "ok" if loaded else "timeout",
            "redirected_in_app": not same,
            "dwell_seconds": page.dwell_seconds,
        }


# ---------------------------------------------------------------------------
# CLI


def _emit_line(record: Mapping[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument(
        "--allowlist-sha256",
        help="pin: refuse unless the allow-list file has exactly this sha256",
    )
    parser.add_argument(
        "--load-timeout-seconds", type=float, default=DEFAULT_LOAD_TIMEOUT_SECONDS
    )
    return parser


def _connect(cdp_url: str) -> CdpTransport:  # pragma: no cover - needs a live browser
    return WebSocketTransport(cdp_url)


def main(
    argv: Sequence[str] | None = None,
    *,
    connect: Callable[[str], CdpTransport] = _connect,
    emit: Callable[[Mapping[str, Any]], None] = _emit_line,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    args = _parser().parse_args(argv)
    try:
        parse_cdp_url(args.cdp_url)
        try:
            raw = args.allowlist.read_bytes()
        except OSError:
            raise NavigatorError("ALLOWLIST_UNREADABLE") from None
        allowlist = parse_allowlist(raw, pin_sha256=args.allowlist_sha256)
        if not 0 < args.load_timeout_seconds <= 300:
            raise NavigatorError("LOAD_TIMEOUT_INVALID")
    except RecorderError:
        print("REFUSED CDP_URL_NOT_LOOPBACK_HTTP")
        return EXIT_REFUSED
    except NavigatorError as exc:
        print(f"REFUSED {exc.code}")
        return EXIT_REFUSED
    try:
        transport = connect(args.cdp_url)
    except RecorderError as exc:
        print(f"REFUSED {exc.code}")
        return EXIT_REFUSED
    try:
        return Navigator(
            transport,
            allowlist,
            emit=emit,
            clock=clock,
            load_timeout=args.load_timeout_seconds,
        ).run()
    except NavigatorError as exc:
        print(f"REFUSED {exc.code}")
        return EXIT_REFUSED
    finally:
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
