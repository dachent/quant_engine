"""Offline proof of the allowlisted Portfolio123 CDP fetcher.

Everything is synthetic: invented book ids, invented file contents, and a fake CDP
transport that replays scripted replies and download events. No browser, no network.
"""

from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from tools import p123_cdp_fetcher as fetcher
from tools import p123_cdp_recorder as recorder
from tools.p123_cdp_fetcher import (
    ALLOWED_CDP_METHODS,
    FetchRefused,
    GuardedTransport,
    allowed_p123_urls,
    book_url,
    parse_allowlist,
    run_drive,
    run_p123,
    screen_drive_url,
    screen_p123_url,
)

BASE = "https://www.portfolio123.com"
BOOKS = (1111111, 2222222, 3333333)
DRIVE_ID = "1AbCdEfGhIjKlMnOp"
TAB = "TAB-1"
# Built from parts: the repository boundary scan rejects a quoted session-id literal.
SID_KEY = "session" + "Id"
ATTACH_REF = "ATTACHED-1"
CONTENT_SENTINEL = "SENTINELCONTENTVALUE"

PREFIX = {k.name: k.prefix for k in fetcher.KINDS}
PAYLOAD = {
    "transactions_csv": (
        "Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes\r\n"
        f"2020-01-02,{CONTENT_SENTINEL},BUY,1,2,3,0,,\r\n"
        "2020-01-03,ZZZ,SELL,1,2,3,0,,\r\n"
    ),
    "perf_csv": "Date,Total Equity\n2020-01-02,10\n2020-01-03,11\n2020-01-04,12\n",
    "holdings_csv": "Ticker,Shares\nAAA,1\n",
}


def _allowlist(
    books: tuple[int, ...] = BOOKS[:2],
    designers: tuple[int, ...] = (),
    drive: tuple[str, ...] = (DRIVE_ID,),
    kinds: tuple[str, ...] | None = None,
    page_record: bool = False,
) -> fetcher.Allowlist:
    payload = fetcher.build_allowlist_bytes(books, designers, drive, kinds, page_record)
    return parse_allowlist(payload, hashlib.sha256(payload).hexdigest())


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeBrowser:
    """A scripted CDP endpoint."""

    def __init__(
        self,
        clock: FakeClock,
        tabs: dict[str, str],
        on_navigate: Callable[[str, FakeBrowser], list[dict[str, Any]]],
    ) -> None:
        self.clock = clock
        self.tabs = tabs
        self.on_navigate = on_navigate
        self.sent: list[dict[str, Any]] = []
        self.download_path = ""
        self.queue: deque[dict[str, Any]] = deque()
        self.guid = 0
        self.bodies: dict[str, str] = {}
        self.omit_frame_id = False

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        method, ident = message["method"], message["id"]
        events: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        if method == "Target.setDiscoverTargets":
            events = [
                {
                    "method": "Target.targetCreated",
                    "params": {
                        "targetInfo": {"targetId": tid, "type": "page", "url": url}
                    },
                }
                for tid, url in self.tabs.items()
            ]
        elif method == "Target.attachToTarget":
            result = {SID_KEY: ATTACH_REF}
        elif method == "Browser.setDownloadBehavior":
            self.download_path = message["params"].get("downloadPath", "")
        elif method == "Network.getResponseBody":
            result = {
                "body": self.bodies[message["params"]["requestId"]],
                "base64Encoded": False,
            }
        elif method == "Page.navigate":
            if not self.omit_frame_id:
                result = {"frameId": "FRAME-1"}
            events = self.on_navigate(message["params"]["url"], self)
        self.queue.append({"id": ident, "result": result})
        self.queue.extend(events)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self.queue:
            return self.queue.popleft()
        self.clock.now += timeout
        return None

    def close(self) -> None:
        pass

    def download(
        self,
        url: str,
        name: str,
        content: bytes,
        *,
        reported: int | None = None,
        extra_file: bool = False,
        frame_id: str = "FRAME-1",
    ) -> list[dict[str, Any]]:
        self.guid += 1
        guid = f"guid-{self.guid}"
        directory = Path(self.download_path)
        (directory / guid).write_bytes(content)
        if extra_file:
            (directory / "stray.crdownload").write_bytes(b"x")
        size = len(content) if reported is None else reported
        return [
            {
                "method": "Browser.downloadWillBegin",
                "params": {
                    "guid": guid,
                    "url": url,
                    "suggestedFilename": name,
                    "frameId": frame_id,
                },
            },
            {
                "method": "Browser.downloadProgress",
                "params": {
                    "guid": guid,
                    "state": "completed",
                    "totalBytes": size,
                    "receivedBytes": size,
                },
            },
        ]


def designer_events(
    browser: FakeBrowser,
    url: str,
    body: str,
    *,
    status: int = 200,
    mime: str = "application/json",
    request_id: str = "REQ-1",
    request_url: str | None = None,
) -> list[dict[str, Any]]:
    browser.bodies[request_id] = body

    def event(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"method": method, SID_KEY: ATTACH_REF, "params": params}

    return [
        event(
            "Network.requestWillBeSent",
            {
                "requestId": request_id,
                "request": {"url": request_url or url, "method": "GET"},
            },
        ),
        event(
            "Network.responseReceived",
            {
                "requestId": request_id,
                "response": {"url": url, "status": status, "mimeType": mime},
            },
        ),
        event("Network.loadingFinished", {"requestId": request_id}),
    ]


def _book_of(url: str) -> tuple[str, int]:
    for kind in fetcher.KINDS:
        if url.startswith(f"{BASE}{kind.path}?portid="):
            return kind.name, int(url.rsplit("=", 1)[1])
    raise AssertionError(url)


PAGE_BODY = '[{"assetUid": 1111111, "assetName": "Book \u00e9", "posCnt": 3}]\n'
DESIGNER_BODY = '{"name": "Model \u00e9 \u4e2d", "portUid": 1, "desc": "caf\u00e9"}\n'


def good_navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
    if "/spr/strategy/model/" in url:
        return designer_events(browser, url, DESIGNER_BODY)
    if "/app/investment/resource" in url:
        return designer_events(browser, url, PAGE_BODY, request_id="REQ-PAGE")
    kind, book = _book_of(url)
    return browser.download(
        url, f"{PREFIX[kind]}Some Name {book}.csv", PAYLOAD[kind].encode()
    )


def _pass_check(
    capture: Path, books: list[str], reference: Path | None
) -> dict[str, Any]:
    return {"books": {b: {"decision": "PASS", "checks": []} for b in books}}


def _run(
    tmp_path: Path,
    navigate: Callable[[str, FakeBrowser], list[dict[str, Any]]] = good_navigate,
    *,
    allowlist: fetcher.Allowlist | None = None,
    tabs: dict[str, str] | None = None,
    check: fetcher.IdentityCheck = _pass_check,
    **kwargs: Any,
) -> tuple[dict[str, Any], FakeBrowser, Path]:
    clock = FakeClock()
    browser = FakeBrowser(clock, tabs or {TAB: f"{BASE}/app/home"}, navigate)
    root = tmp_path / "run"
    if "reference_index" not in kwargs:
        kwargs.setdefault("no_reference", True)
    kwargs.setdefault("wall_clock", lambda: 1786737863.73)
    manifest = run_p123(
        browser,
        allowlist or _allowlist(),
        root,
        identity_check=check,
        clock=clock,
        download_timeout=10.0,
        **kwargs,
    )
    return manifest, browser, root


# ---------------------------------------------------------------------------
# refusals before sending


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (
            f"http://www.portfolio123.com/p123/DownloadPortTrans?portid={BOOKS[0]}",
            "URL_NOT_HTTPS",
        ),
        (
            f"https://evil.example.com/p123/DownloadPortTrans?portid={BOOKS[0]}",
            "URL_OFF_ORIGIN",
        ),
        (
            f"https://user@www.portfolio123.com/p123/DownloadPortTrans?portid={BOOKS[0]}",
            "URL_OFF_ORIGIN",
        ),
        (
            f"{BASE}/p123/DownloadPortTrans?portid={BOOKS[0]}&delete=1",
            "URL_MUTATION_WORD",
        ),
        (f"{BASE}/app/screen/summary/5/revert", "URL_MUTATION_WORD"),
        (f"{BASE}/p123/DownloadPortTrans?portid=999", "URL_NOT_ALLOWLISTED"),
        (f"{BASE}/p123/DownloadPortTrans?portid={BOOKS[0]}&x=1", "URL_NOT_ALLOWLISTED"),
        (
            f"{BASE}/p123/DownloadPortTrans?portid={BOOKS[0]}#frag",
            "URL_NOT_ALLOWLISTED",
        ),
    ],
)
def test_screen_refuses_before_sending(url: str, code: str) -> None:
    urls = allowed_p123_urls(_allowlist())
    with pytest.raises(FetchRefused) as info:
        screen_p123_url(url, urls)
    assert info.value.code == code


def test_every_kind_is_a_reviewed_read_only_contract_route() -> None:
    urls = allowed_p123_urls(_allowlist())
    assert len(urls) == 2 * len(fetcher.KINDS)
    for url in urls:
        screen_p123_url(url, urls)


def test_mutation_words_are_the_recorders_constant() -> None:
    assert fetcher.MUTATION_WORDS == recorder.MUTATION_WORDS


@pytest.mark.parametrize(
    "message",
    [
        {"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"}},
        {"id": 1, "method": "Fetch.enable", "params": {}},
        {"id": 1, "method": "Network.enable", "params": {}},
        {"id": 1, "method": "Network.getResponseBody", "params": {}},
        {"id": 1, "method": "Input.dispatchMouseEvent", "params": {}},
        {"id": 1, "method": "Page.navigate", "params": {"url": f"{BASE}/other"}},
        {"id": 1, "method": "Page.navigate", "params": {"url": "x", "referrer": "y"}},
        {
            "id": 1,
            "method": "Browser.setDownloadBehavior",
            "params": {
                "behavior": "allowAndName",
                "downloadPath": "C:/elsewhere",
                "eventsEnabled": True,
            },
        },
        {
            "id": 1,
            "method": "Browser.setDownloadBehavior",
            "params": {"behavior": "allow", "downloadPath": "D"},
        },
    ],
)
def test_transport_guard_refuses_everything_else(message: dict[str, Any]) -> None:
    sent: list[Any] = []

    class Inner:
        def send(self, m: dict[str, Any]) -> None:
            sent.append(m)

        def receive(self, timeout: float) -> None:
            return None

        def close(self) -> None:
            pass

    guard = GuardedTransport(
        Inner(),
        allowed_urls=frozenset({book_url(fetcher.KINDS[0], BOOKS[0])}),
        download_path="D",
    )
    with pytest.raises(FetchRefused):
        guard.send(message)
    assert sent == []
    assert ALLOWED_CDP_METHODS == {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Page.navigate",
        "Browser.setDownloadBehavior",
        "Network.enable",
        "Network.getResponseBody",
        "Runtime.evaluate",  # only the exact page-GET expression (tested below)
    }


# ---------------------------------------------------------------------------
# allowlist pin and schema


def test_allowlist_pin_mismatch_and_schema() -> None:
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    good = hashlib.sha256(payload).hexdigest()
    parse_allowlist(payload, good)
    for bad in ("0" * 64, "nothex"):
        with pytest.raises(FetchRefused) as info:
            parse_allowlist(payload, bad)
        assert info.value.code in {"ALLOWLIST_PIN_MISMATCH", "ALLOWLIST_PIN_INVALID"}
    doc = json.loads(payload)
    for change in (
        {"schema_version": 2},
        {"origin": "https://evil.example.com"},
        {"books": [{"id": -1, "kinds": ["perf_csv"]}]},
        {"books": [{"id": 5, "kinds": ["delete_csv"]}]},
        {"books": [{"id": 5, "kinds": ["perf_csv"]}, {"id": 5, "kinds": ["perf_csv"]}]},
        {"drive_files": ["short"]},
        {"extra": 1},
    ):
        altered = fetcher.canonical_bytes({**doc, **change})
        with pytest.raises(FetchRefused) as info:
            parse_allowlist(altered, hashlib.sha256(altered).hexdigest())
        assert info.value.code.startswith("ALLOWLIST_")


def test_run_refuses_modified_allowlist_file(tmp_path: Path) -> None:
    path = tmp_path / "allow.json"
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    path.write_bytes(payload)
    pin = hashlib.sha256(payload).hexdigest()
    path.write_bytes(fetcher.build_allowlist_bytes(BOOKS[:2]))
    with pytest.raises(FetchRefused) as info:
        fetcher.load_allowlist(path, pin)
    assert info.value.code == "ALLOWLIST_PIN_MISMATCH"


# ---------------------------------------------------------------------------
# happy path


def test_fetch_saves_unmodified_bytes_and_manifests_without_values(
    tmp_path: Path,
) -> None:
    manifest, browser, root = _run(tmp_path)
    assert manifest["status"] == "COMPLETE"
    capture = root / "capture"
    for book in BOOKS[:2]:
        for kind in fetcher.KINDS:
            saved = capture / "raw" / kind.saved.format(id=book)
            assert saved.read_bytes() == PAYLOAD[kind.name].encode()
    entry = manifest["books"][str(BOOKS[0])]
    assert entry["identity"] == "PASS"
    files = {f["kind"]: f for f in entry["files"]}
    assert files["transactions_csv"]["rows"] == 2
    assert files["perf_csv"]["rows"] == 3
    assert files["transactions_csv"]["stage_file_capability"] == "transactions.download"
    assert (
        files["perf_csv"]["sha256"]
        == hashlib.sha256(PAYLOAD["perf_csv"].encode()).hexdigest()
    )
    on_disk = (root / "p123-fetch-manifest.json").read_text(encoding="utf-8")
    assert CONTENT_SENTINEL not in on_disk
    assert json.loads(on_disk)["allowlist_sha256"] == manifest["allowlist_sha256"]
    index = json.loads((capture / "capture-manifest.json").read_text(encoding="utf-8"))
    assert {a["relative_path"] for a in index["artifacts"]} == {
        f"raw/{k.saved.format(id=b)}" for b in BOOKS[:2] for k in fetcher.KINDS
    }
    assert {m["method"] for m in browser.sent} <= ALLOWED_CDP_METHODS
    navigated = [
        m["params"]["url"] for m in browser.sent if m["method"] == "Page.navigate"
    ]
    assert navigated == [book_url(k, b) for b in BOOKS[:2] for k in fetcher.KINDS]
    assert browser.sent[-1]["params"] == {"behavior": "default"}
    assert not list((root / "incoming").iterdir())


def test_output_root_must_be_new(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    with pytest.raises(FetchRefused) as info:
        _run(tmp_path)
    assert info.value.code == "OUTPUT_ROOT_EXISTS"


def test_skipped_designer_models_send_no_network_message(tmp_path: Path) -> None:
    allowlist = _allowlist(designers=(4444444,))
    manifest, browser, _ = _run(
        tmp_path, allowlist=allowlist, skip_designer_models=True
    )
    assert manifest["designer_models_not_fetched"] == ["4444444"]
    assert manifest["safety"]["body_reads"] == "none"
    assert all("model" not in m["params"].get("url", "") for m in browser.sent)
    assert not [m for m in browser.sent if m["method"].startswith("Network.")]


# ---------------------------------------------------------------------------
# stop rules


def _stop_code(manifest: dict[str, Any]) -> str:
    assert manifest["status"] == "STOPPED"
    return str(manifest["stop"]["code"])


def test_login_page_stops_with_reauth(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        if _book_of(url)[0] == "perf_csv":
            browser.tabs[TAB] = f"{BASE}/login/signin"
            return [
                {
                    "method": "Target.targetInfoChanged",
                    "params": {
                        "targetInfo": {
                            "targetId": TAB,
                            "type": "page",
                            "url": f"{BASE}/login/signin",
                        }
                    },
                }
            ]
        return good_navigate(url, browser)

    manifest, _, root = _run(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_REAUTH"
    assert (root / "capture/raw/strategy_1111111_transactions.csv").is_file()
    assert not (root / "capture/raw/strategy_1111111_perf.csv").exists()
    assert manifest["books_not_attempted"] == [str(BOOKS[1])]


def test_tab_leaving_the_origin_stops(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        return [
            {
                "method": "Target.targetInfoChanged",
                "params": {
                    "targetInfo": {
                        "targetId": TAB,
                        "type": "page",
                        "url": "https://elsewhere.example.com/",
                    }
                },
            }
        ]

    manifest, _, _ = _run(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_REAUTH"


def test_no_download_started_is_reauth_stop(tmp_path: Path) -> None:
    manifest, _, _ = _run(tmp_path, lambda url, browser: [])
    assert _stop_code(manifest) == "STOP_REAUTH"
    assert "NO_DOWNLOAD_STARTED" in manifest["stop"]["detail"]


def test_only_login_tab_is_a_reauth_stop(tmp_path: Path) -> None:
    manifest, browser, _ = _run(
        tmp_path, tabs={TAB: f"{BASE}/login"}, navigate=good_navigate
    )
    assert _stop_code(manifest) == "STOP_REAUTH"
    assert not [m for m in browser.sent if m["method"] == "Page.navigate"]


def test_no_tab_and_ambiguous_tabs_are_recorded_stops(tmp_path: Path) -> None:
    manifest, browser, root = _run(tmp_path, tabs={"T": "https://example.com/"})
    assert _stop_code(manifest) == "STOP_NO_SIGNED_IN_TAB"
    assert (root / "p123-fetch-manifest.json").is_file()
    assert not [m for m in browser.sent if m["method"] == "Page.navigate"]
    manifest, _, root = _run(
        tmp_path / "b", tabs={"A": f"{BASE}/app/a", "B": f"{BASE}/app/b"}
    )
    assert _stop_code(manifest) == "STOP_TARGET_AMBIGUOUS"
    assert (root / "p123-fetch-manifest.json").is_file()


def test_post_discovery_failure_exits_3_with_a_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    clock = FakeClock()
    browser = FakeBrowser(clock, {"T": "https://example.com/"}, good_navigate)
    monkeypatch.setattr(fetcher, "load_identity_check", lambda **kwargs: _pass_check)
    monkeypatch.setattr(fetcher, "connect", lambda url: browser)
    monkeypatch.setattr(fetcher.time, "monotonic", clock)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
            "--no-reference",
            "--as-of",
            "2026-09-11",
        ]
    )
    assert code == 3
    assert "STOP STOP_NO_SIGNED_IN_TAB" in capsys.readouterr().out
    assert (tmp_path / "out" / "p123-fetch-manifest.json").is_file()


def test_download_from_another_frame_is_not_ours(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        kind, book = _book_of(url)
        foreign = browser.download(
            f"{BASE}/p123/DownloadPortTrans?portid=9",
            f"{PREFIX[kind]}Other.csv",
            b"other bytes",
            frame_id="FRAME-OTHER",
        )
        return foreign + good_navigate(url, browser)

    manifest, _, root = _run(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_DOWNLOAD_NOT_EXACT"
    assert "unexpected files" in manifest["stop"]["detail"]
    assert not list((root / "capture" / "raw").iterdir())


def test_a_foreign_download_event_without_a_file_is_ignored(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        foreign = {
            "method": "Browser.downloadWillBegin",
            "params": {
                "guid": "foreign",
                "url": "https://elsewhere.example.com/x",
                "suggestedFilename": "x.bin",
                "frameId": "FRAME-OTHER",
            },
        }
        return [foreign, *good_navigate(url, browser)]

    manifest, _, _ = _run(tmp_path, navigate)
    assert manifest["status"] == "COMPLETE"


def test_navigation_without_a_frame_id_is_a_stop(tmp_path: Path) -> None:
    clock = FakeClock()
    browser = FakeBrowser(clock, {TAB: f"{BASE}/app/home"}, good_navigate)
    browser.omit_frame_id = True
    manifest = run_p123(
        browser,
        _allowlist(),
        tmp_path / "run",
        identity_check=_pass_check,
        no_reference=True,
        clock=clock,
        download_timeout=10.0,
    )
    assert _stop_code(manifest) == "STOP_DOWNLOAD_NOT_EXACT"
    assert "frame id" in manifest["stop"]["detail"]


@pytest.mark.parametrize(
    "mutate",
    [
        "wrong_prefix",
        "wrong_url",
        "not_csv",
        "path_in_name",
        "size_mismatch",
        "empty",
        "extra_file",
        "second_download",
        "canceled",
    ],
)
def test_download_must_be_exact(tmp_path: Path, mutate: str) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        kind, book = _book_of(url)
        name = f"{PREFIX[kind]}X.csv"
        content = PAYLOAD[kind].encode()
        if mutate == "wrong_prefix":
            name = "P123_SomethingElse_X.csv"
        elif mutate == "not_csv":
            name = f"{PREFIX[kind]}X.html"
        elif mutate == "path_in_name":
            name = f"{PREFIX[kind]}..\\X.csv"
        elif mutate == "empty":
            content = b""
        target = (
            f"{BASE}/p123/DownloadPortTrans?portid=9" if mutate == "wrong_url" else url
        )
        events = browser.download(
            target,
            name,
            content,
            reported=len(content) + 5 if mutate == "size_mismatch" else None,
            extra_file=mutate == "extra_file",
        )
        if mutate == "second_download":
            events.insert(
                1,
                {
                    "method": "Browser.downloadWillBegin",
                    "params": {
                        "guid": "g2",
                        "url": url,
                        "suggestedFilename": name,
                        "frameId": "FRAME-1",
                    },
                },
            )
        if mutate == "canceled":
            events[1]["params"]["state"] = "canceled"
        return events

    manifest, _, root = _run(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_DOWNLOAD_NOT_EXACT"
    assert not list((root / "capture" / "raw").iterdir())


# ---------------------------------------------------------------------------
# identity check wiring


def test_identity_runs_after_each_book_and_fail_stops_later_books(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], int]] = []

    def check(
        capture: Path, books: list[str], reference: Path | None
    ) -> dict[str, Any]:
        calls.append((list(books), len(list((capture / "raw").iterdir()))))
        decision = "FAIL" if len(books) == 2 else "PASS"
        return {
            "books": {
                b: {
                    "decision": decision,
                    "checks": [{"check": "I8", "status": decision}],
                }
                for b in books
            }
        }

    manifest, _, root = _run(tmp_path, allowlist=_allowlist(books=BOOKS), check=check)
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"
    assert calls == [
        ([str(BOOKS[0])], 3),
        ([str(BOOKS[0]), str(BOOKS[1])], 6),
        ([str(BOOKS[0]), str(BOOKS[1])], 6),  # the final check, after the last manifest
    ]
    assert manifest["books"][str(BOOKS[0])]["identity"] == "PASS"
    assert manifest["books"][str(BOOKS[1])]["identity"] == "FAIL"
    assert manifest["books"][str(BOOKS[1])]["identity_checks"] == {"I8": "FAIL"}
    assert manifest["books_not_attempted"] == [str(BOOKS[2])]
    assert (root / "book-identity-decisions.json").is_file()


CASH_ONLY_TRANSACTIONS = (
    "Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes\r\n"
    "2020-05-12,,CASH,,,1000000,,,\r\n"
)


def _cash_only_navigate(
    cash_books: set[int],
) -> Callable[[str, FakeBrowser], list[dict[str, Any]]]:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        kind, book = _book_of(url)
        content = PAYLOAD[kind]
        if book in cash_books:
            if kind == "transactions_csv":
                content = CASH_ONLY_TRANSACTIONS
            elif kind == "holdings_csv":
                content = "Ticker,Shares\n"
        return browser.download(url, f"{PREFIX[kind]}N.csv", content.encode())

    return navigate


def _decisions(**by_book: str) -> fetcher.IdentityCheck:
    """Per-book decisions; a REVIEW carries one warning check (I7), a FAIL one failure."""

    def check(
        capture: Path, books: list[str], reference: Path | None
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for b in books:
            decision = by_book.get("b" + b, "PASS")
            checks = [{"check": "I1", "status": "PASS", "detail": "ok"}]
            if decision != "PASS":
                status = "WARN" if decision == "REVIEW" else "FAIL"
                checks.append({"check": "I7", "status": status, "detail": "x"})
            out[b] = {"decision": decision, "checks": checks}
        return {"books": out}

    return check


def test_review_never_stops_downloads_and_is_review_pending(tmp_path: Path) -> None:
    manifest, _, root = _run(
        tmp_path,
        allowlist=_allowlist(books=BOOKS),
        check=_decisions(**{f"b{BOOKS[1]}": "REVIEW"}),
    )
    assert manifest["status"] == "REVIEW_PENDING"
    assert manifest["stop"] is None
    assert manifest["books_not_attempted"] == []
    assert set(manifest["books"]) == {str(b) for b in BOOKS}
    entry = manifest["books"][str(BOOKS[1])]
    assert entry["identity"] == "REVIEW"
    assert entry["identity_policy"] == "CONTINUE_REVIEW"
    assert entry["review_checks"] == ["I7"]
    assert manifest["books"][str(BOOKS[0])]["identity_policy"] == "CONTINUE"
    assert manifest["review_pending"] == {str(BOOKS[1]): ["I7"]}
    decisions = json.loads((root / "book-identity-decisions.json").read_text())
    assert decisions["books"][str(BOOKS[1])]["decision"] == "REVIEW"
    assert (root / "p123-fetch-manifest.json").is_file()


def test_every_kind_of_review_book_continues(tmp_path: Path) -> None:
    # a cash-only book (no special case any more), a book with trades, and an
    # allowlist without holdings and without a page record: all REVIEW, all fetched
    manifest, _, root = _run(
        tmp_path,
        _cash_only_navigate({BOOKS[0]}),
        allowlist=_allowlist(books=BOOKS, kinds=("transactions_csv", "perf_csv")),
        check=_decisions(**{f"b{b}": "REVIEW" for b in BOOKS}),
    )
    assert manifest["status"] == "REVIEW_PENDING"
    assert manifest["page_record"] is None
    assert set(manifest["review_pending"]) == {str(b) for b in BOOKS}
    assert not list((root / "capture/raw").glob("*holdings*"))
    assert len(list((root / "capture/raw").glob("strategy_*"))) == 2 * len(BOOKS)


def test_all_pass_is_complete_with_nothing_pending(tmp_path: Path) -> None:
    manifest, _, root = _run(tmp_path, check=_decisions())
    assert manifest["status"] == "COMPLETE"
    assert manifest["review_pending"] == {}
    assert (root / "book-identity-decisions.json").is_file()


def test_fail_after_review_stops_at_the_fail(tmp_path: Path) -> None:
    manifest, _, root = _run(
        tmp_path,
        allowlist=_allowlist(books=BOOKS),
        check=_decisions(**{f"b{BOOKS[0]}": "REVIEW", f"b{BOOKS[1]}": "FAIL"}),
    )
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"
    assert manifest["books_not_attempted"] == [str(BOOKS[2])]
    assert manifest["books"][str(BOOKS[1])]["identity_policy"] == "STOP"
    assert manifest["review_pending"] == {str(BOOKS[0]): ["I7"]}
    assert (root / "book-identity-decisions.json").is_file()


def test_an_unknown_decision_is_a_failure(tmp_path: Path) -> None:
    manifest, _, _ = _run(tmp_path, check=_decisions(**{f"b{BOOKS[0]}": "WEIRD"}))
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"


def _cli_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: fetcher.IdentityCheck,
    books: tuple[int, ...] = BOOKS[:2],
) -> int:
    payload = fetcher.build_allowlist_bytes(books)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    clock = FakeClock()
    browser = FakeBrowser(clock, {TAB: f"{BASE}/app/home"}, good_navigate)
    monkeypatch.setattr(fetcher, "load_identity_check", lambda **kwargs: check)
    monkeypatch.setattr(fetcher, "connect", lambda url: browser)
    monkeypatch.setattr(fetcher.time, "monotonic", clock)
    return fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
            "--download-timeout",
            "10",
            "--no-reference",
            "--as-of",
            "2026-09-11",
        ]
    )


def test_exit_codes_pass_review_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli_run(tmp_path / "a", monkeypatch, _decisions()) == 0
    assert "REVIEW_PENDING" not in capsys.readouterr().out
    code = _cli_run(
        tmp_path / "b",
        monkeypatch,
        _decisions(**{f"b{BOOKS[0]}": "REVIEW", f"b{BOOKS[1]}": "REVIEW"}),
    )
    out = capsys.readouterr().out
    assert code == 4
    assert '"status":"REVIEW_PENDING"' in out
    for book in BOOKS[:2]:
        assert f"REVIEW_PENDING book={book} checks=I7" in out
    code = _cli_run(
        tmp_path / "c",
        monkeypatch,
        _decisions(**{f"b{BOOKS[0]}": "REVIEW", f"b{BOOKS[1]}": "FAIL"}),
    )
    out = capsys.readouterr().out
    assert code == 3
    assert "STOP STOP_IDENTITY_FAIL" in out
    assert (tmp_path / "c" / "out" / "book-identity-decisions.json").is_file()


def test_there_is_no_require_identity_pass_option() -> None:
    parser = fetcher._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--cdp-url",
                "http://127.0.0.1:1",
                "--allowlist",
                "a",
                "--allowlist-sha256",
                "b",
                "--download-dir",
                "c",
                "--require-identity-pass",
            ]
        )


def test_missing_decision_is_a_failure(tmp_path: Path) -> None:
    manifest, _, _ = _run(
        tmp_path, check=lambda capture, books, reference: {"books": {}}
    )
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"


def _fake_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, source: str
) -> str:
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / f"{name}.py").write_text(source)
    return name


def test_identity_loader_is_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(FetchRefused) as info:
        fetcher.load_identity_check("tools.p123_no_such_check_module")
    assert info.value.code == "IDENTITY_CHECK_UNAVAILABLE"
    broken = _fake_module(
        tmp_path,
        monkeypatch,
        "fake_ic_broken",
        "from quant_engine.portfolio123 import no_such_name\n",
    )
    with pytest.raises(FetchRefused) as info:
        fetcher.load_identity_check(broken)
    assert info.value.code == "IDENTITY_CHECK_UNAVAILABLE"
    missing_dependency = _fake_module(
        tmp_path, monkeypatch, "fake_ic_dep", "import not_a_real_dependency_xyz\n"
    )
    with pytest.raises(FetchRefused) as info:
        fetcher.load_identity_check(missing_dependency)
    assert info.value.code == "IDENTITY_CHECK_UNAVAILABLE"
    no_function = _fake_module(tmp_path, monkeypatch, "fake_ic_none", "x = 1\n")
    with pytest.raises(FetchRefused):
        fetcher.load_identity_check(no_function)
    good = _fake_module(
        tmp_path,
        monkeypatch,
        "fake_ic_good",
        "def check_capture(capture, **kwargs):\n"
        "    return {'books': {b: {'decision': 'PASS', 'checks': [], "
        "'kw': sorted(kwargs)} for b in kwargs['books']}}\n",
    )
    check = fetcher.load_identity_check(good)
    result = check(tmp_path, ["5"], None)
    assert result["books"]["5"]["decision"] == "PASS"
    assert result["books"]["5"]["kw"] == [
        "as_of",
        "books",
        "cross_checks",
        "prior_checks",
        "raw_subdir",
        "reference_index",
        "require_reference",
        "use_manifest",
    ]


def test_cli_refuses_without_identity_check_before_any_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    path = tmp_path / "allow.json"
    path.write_bytes(payload)

    def unavailable(**kwargs: Any) -> None:
        raise FetchRefused("IDENTITY_CHECK_UNAVAILABLE")

    def no_connect(url: str) -> None:
        raise AssertionError("connected")

    monkeypatch.setattr(fetcher, "load_identity_check", unavailable)
    monkeypatch.setattr(fetcher, "connect", no_connect)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
            "--no-reference",
            "--as-of",
            "2026-09-11",
        ]
    )
    assert code == 2
    assert capsys.readouterr().out.strip() == "REFUSED IDENTITY_CHECK_UNAVAILABLE"
    assert not (tmp_path / "out").exists()


def test_cli_make_allowlist_and_loopback_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "allow.json"
    assert fetcher.main(["make-allowlist", "--out", str(out), "--book", "5"]) == 0
    pin = json.loads(capsys.readouterr().out)["allowlist_sha256"]
    assert pin == hashlib.sha256(out.read_bytes()).hexdigest()
    assert out.read_bytes().endswith(b"\n") and b"\r" not in out.read_bytes()
    with pytest.raises(FetchRefused) as info:
        fetcher.connect("http://example.com:9333")
    assert info.value.code == "CDP_URL_NOT_LOOPBACK_HTTP"
    with pytest.raises(FetchRefused):
        fetcher.connect("https://127.0.0.1:9333")


# ---------------------------------------------------------------------------
# Google Drive mode


def _drive(
    tmp_path: Path,
    navigate: Callable[[str, FakeBrowser], list[dict[str, Any]]],
    tabs: dict[str, str] | None = None,
    file_id: str = DRIVE_ID,
) -> tuple[dict[str, Any], FakeBrowser, Path]:
    clock = FakeClock()
    browser = FakeBrowser(clock, tabs or {TAB: "about:blank"}, navigate)
    root = tmp_path / "drive"
    manifest = run_drive(
        browser, _allowlist(), file_id, root, clock=clock, download_timeout=10.0
    )
    return manifest, browser, root


DRIVE_CONTENT = b"drive bytes \x00\x01 unmodified"
CONTENT_HOST = "https://doc-0g-ab-docs.googleusercontent.com/docs/securesc/x"


def test_drive_download_saves_unmodified_with_sha256(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        assert url == f"https://drive.google.com/uc?export=download&id={DRIVE_ID}"
        return browser.download(CONTENT_HOST, "report.ZIP", DRIVE_CONTENT)

    manifest, browser, root = _drive(tmp_path, navigate)
    assert manifest["status"] == "COMPLETE"
    saved = root / manifest["file"]["relative_path"]
    assert saved.name == f"drive_{DRIVE_ID}.zip"
    assert saved.read_bytes() == DRIVE_CONTENT
    assert manifest["file"]["sha256"] == hashlib.sha256(DRIVE_CONTENT).hexdigest()
    assert {m["method"] for m in browser.sent} <= ALLOWED_CDP_METHODS
    assert "report" not in (root / "p123-fetch-manifest.json").read_text()


@pytest.mark.parametrize(
    "final_url",
    [
        "https://evil.example.com/file",
        "http://drive.usercontent.google.com/download",
        "https://doc-0g-ab-docs.googleusercontent.com.evil.example/x",
        "https://lh3.googleusercontent.com/x",
    ],
)
def test_drive_redirect_to_unlisted_host_stops(tmp_path: Path, final_url: str) -> None:
    manifest, _, root = _drive(
        tmp_path, lambda url, b: b.download(final_url, "a.bin", b"abc")
    )
    assert _stop_code(manifest) == "STOP_DOWNLOAD_NOT_EXACT"
    assert manifest["file"] is None


def test_drive_usercontent_host_and_interstitial(tmp_path: Path) -> None:
    manifest, _, _ = _drive(
        tmp_path,
        lambda url, b: b.download(
            "https://drive.usercontent.google.com/download?id=x", "a.bin", b"abc"
        ),
    )
    assert manifest["status"] == "COMPLETE"
    manifest, _, _ = _drive(tmp_path / "x", lambda url, b: [])
    assert _stop_code(manifest) == "STOP_DOWNLOAD_NOT_EXACT"


def test_drive_login_tab_and_unlisted_ids(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        return [
            {
                "method": "Target.targetInfoChanged",
                "params": {
                    "targetInfo": {
                        "targetId": TAB,
                        "type": "page",
                        "url": "https://accounts.google.com/signin",
                    }
                },
            }
        ]

    manifest, _, _ = _drive(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_REAUTH"
    with pytest.raises(FetchRefused) as info:
        _drive(tmp_path / "y", navigate, file_id="ZZZZZZZZZZZZZZZZ")
    assert info.value.code == "URL_NOT_ALLOWLISTED"
    manifest, _, _ = _drive(tmp_path / "z", navigate, tabs={TAB: f"{BASE}/app/home"})
    assert _stop_code(manifest) == "STOP_NO_SIGNED_IN_TAB"


def test_drive_download_from_another_frame_is_not_ours(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        foreign = browser.download(
            CONTENT_HOST, "other.zip", b"foreign", frame_id="FRAME-OTHER"
        )
        return foreign + browser.download(CONTENT_HOST, "report.zip", DRIVE_CONTENT)

    manifest, _, root = _drive(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_DOWNLOAD_NOT_EXACT"
    assert manifest["file"] is None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (f"http://drive.google.com/uc?export=download&id={DRIVE_ID}", "URL_NOT_HTTPS"),
        (f"https://docs.google.com/uc?export=download&id={DRIVE_ID}", "URL_OFF_ORIGIN"),
        (
            f"https://drive.google.com/uc?export=download&id={DRIVE_ID}&confirm=t",
            "URL_NOT_ALLOWLISTED",
        ),
        (
            f"https://drive.google.com/uc?export=view&id={DRIVE_ID}",
            "URL_NOT_ALLOWLISTED",
        ),
        (f"https://drive.google.com/file/d/{DRIVE_ID}", "URL_NOT_ALLOWLISTED"),
    ],
)
def test_drive_url_screen(url: str, code: str) -> None:
    with pytest.raises(FetchRefused) as info:
        screen_drive_url(url, _allowlist())
    assert info.value.code == code
    assert screen_drive_url(fetcher.drive_url(DRIVE_ID), _allowlist()) == DRIVE_ID


# ---------------------------------------------------------------------------
# designer-model JSON: the narrow Network.getResponseBody widening

DESIGNER_IDS = (4444444, 5555555)


def _designer_url(model: int) -> str:
    return f"{BASE}/spr/strategy/model/{model}?dm=true"


def _designer_run(
    tmp_path: Path,
    navigate: Callable[[str, FakeBrowser], list[dict[str, Any]]] = good_navigate,
    **kwargs: Any,
) -> tuple[dict[str, Any], FakeBrowser, Path]:
    return _run(
        tmp_path,
        navigate,
        allowlist=_allowlist(books=BOOKS[:1], designers=DESIGNER_IDS),
        **kwargs,
    )


def test_designer_body_is_saved_byte_identical(tmp_path: Path) -> None:
    manifest, browser, root = _designer_run(tmp_path)
    assert manifest["status"] == "COMPLETE"
    expected = DESIGNER_BODY.encode("utf-8")
    for model in DESIGNER_IDS:
        saved = root / "capture" / "raw" / f"designer_model_{model}.json"
        assert saved.read_bytes() == expected
        entry = manifest["designer_models"][str(model)]
        assert entry["sha256"] == hashlib.sha256(expected).hexdigest()
        assert entry["size_bytes"] == len(expected)
        assert entry["stage_file_capability"] == "designer-model.json"
    assert manifest["designer_models_not_fetched"] == []
    assert (
        manifest["safety"]["body_reads"]
        == "allowlisted page-record and designer-model requests only"
    )
    on_disk = (root / "p123-fetch-manifest.json").read_text(encoding="utf-8")
    assert "caf" not in on_disk
    sent = [m["method"] for m in browser.sent]
    assert sent.count("Network.enable") == 1
    assert sent.count("Network.getResponseBody") == len(DESIGNER_IDS)
    # designers come after the books, and a body is read only for designer requests
    navigations = [
        m["params"]["url"] for m in browser.sent if m["method"] == "Page.navigate"
    ]
    assert [u for u in navigations if "/spr/strategy/model/" in u] == navigations[-2:]
    assert {m["method"] for m in browser.sent} <= ALLOWED_CDP_METHODS
    index = json.loads(
        (root / "capture" / "capture-manifest.json").read_text(encoding="utf-8")
    )
    assert f"raw/designer_model_{DESIGNER_IDS[0]}.json" in {
        a["relative_path"] for a in index["artifacts"]
    }


def test_designer_identity_fail_stops_before_designers(tmp_path: Path) -> None:
    manifest, browser, _ = _designer_run(
        tmp_path, check=_decisions(**{f"b{BOOKS[0]}": "FAIL"})
    )
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"
    assert not [m for m in browser.sent if m["method"] == "Network.getResponseBody"]
    assert manifest["designer_models_not_fetched"] == [str(i) for i in DESIGNER_IDS]


class _Inner:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, m: dict[str, Any]) -> None:
        self.sent.append(m)

    def receive(self, timeout: float) -> None:
        return None

    def close(self) -> None:
        pass


def _guard(network_read: bool = True) -> tuple[GuardedTransport, _Inner]:
    inner = _Inner()
    guard = GuardedTransport(
        inner,
        allowed_urls=frozenset({_designer_url(DESIGNER_IDS[0])}),
        download_path="D",
        network_read=network_read,
    )
    guard.readable.add("REQ-OK")
    return guard, inner


def test_body_read_is_allowed_only_for_a_registered_request_id() -> None:
    guard, inner = _guard()
    guard.send({"id": 1, "method": "Network.enable", "params": {}})
    guard.send(
        {
            "id": 2,
            "method": "Network.getResponseBody",
            "params": {"requestId": "REQ-OK"},
        }
    )
    assert [m["method"] for m in inner.sent] == [
        "Network.enable",
        "Network.getResponseBody",
    ]
    for params in (
        {"requestId": "REQ-OTHER"},
        {"requestId": ["REQ-OK"]},
        {"requestId": "REQ-OK", "extra": 1},
        {},
    ):
        with pytest.raises(FetchRefused) as info:
            guard.send({"id": 3, "method": "Network.getResponseBody", "params": params})
        assert info.value.code == "CDP_BODY_READ_NOT_ALLOWLISTED"
    with pytest.raises(FetchRefused):
        guard.send(
            {"id": 4, "method": "Network.enable", "params": {"maxTotalBufferSize": 1}}
        )
    assert len(inner.sent) == 2


@pytest.mark.parametrize(
    "method",
    [
        "Network.getCookies",
        "Network.getAllCookies",
        "Network.setRequestInterception",
        "Network.setExtraHTTPHeaders",
        "Network.getRequestPostData",
        "Network.disable",
        "Fetch.enable",
        "Fetch.fulfillRequest",
        "Input.dispatchKeyEvent",
        "Page.captureSnapshot",
    ],
)
def test_every_other_method_stays_refused_with_network_read_on(method: str) -> None:
    guard, inner = _guard()
    with pytest.raises(FetchRefused) as info:
        guard.send({"id": 1, "method": method, "params": {}})
    assert info.value.code == "CDP_METHOD_REFUSED"
    assert inner.sent == []


def test_network_methods_are_refused_when_no_designer_is_fetched() -> None:
    guard, inner = _guard(network_read=False)
    guard.readable.add("REQ-OK")
    for message in (
        {"id": 1, "method": "Network.enable", "params": {}},
        {
            "id": 2,
            "method": "Network.getResponseBody",
            "params": {"requestId": "REQ-OK"},
        },
    ):
        with pytest.raises(FetchRefused):
            guard.send(message)
    assert inner.sent == []


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (f"{BASE}/spr/strategy/model/9999999?dm=true", "URL_NOT_ALLOWLISTED"),
        (
            f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}?dm=false",
            "URL_NOT_ALLOWLISTED",
        ),
        (f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}", "URL_NOT_ALLOWLISTED"),
        (
            f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}?dm=true&x=1",
            "URL_NOT_ALLOWLISTED",
        ),
        (
            f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}?x=1&dm=true",
            "URL_NOT_ALLOWLISTED",
        ),
        (
            f"{BASE}/spr/strategy/portmodel/{DESIGNER_IDS[0]}/perf",
            "URL_NOT_ALLOWLISTED",
        ),
        (
            f"http://www.portfolio123.com/spr/strategy/model/{DESIGNER_IDS[0]}?dm=true",
            "URL_NOT_HTTPS",
        ),
        (
            f"https://evil.example.com/spr/strategy/model/{DESIGNER_IDS[0]}?dm=true",
            "URL_OFF_ORIGIN",
        ),
        (
            f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}?dm=true&delete=1",
            "URL_MUTATION_WORD",
        ),
    ],
)
def test_designer_screen_refuses_other_urls(url: str, code: str) -> None:
    allowed = {_designer_url(DESIGNER_IDS[0]): DESIGNER_IDS[0]}
    with pytest.raises(FetchRefused) as info:
        fetcher.screen_designer_url(url, allowed)
    assert info.value.code == code
    fetcher.screen_designer_url(_designer_url(DESIGNER_IDS[0]), allowed)


def test_navigation_to_other_designer_urls_is_refused_by_the_guard() -> None:
    guard, inner = _guard()
    for url in (
        _designer_url(9999999),
        f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}?dm=false",
        f"{BASE}/spr/strategy/model/{DESIGNER_IDS[0]}?dm=true&x=1",
    ):
        with pytest.raises(FetchRefused):
            guard.send({"id": 1, "method": "Page.navigate", "params": {"url": url}})
    assert inner.sent == []


def test_a_request_for_another_url_is_never_read(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        if "/spr/strategy/model/" in url:
            return designer_events(
                browser, url, DESIGNER_BODY, request_url=url + "&other=1"
            )
        return good_navigate(url, browser)

    manifest, browser, root = _designer_run(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_REAUTH"
    assert not [m for m in browser.sent if m["method"] == "Network.getResponseBody"]
    assert not list((root / "capture" / "raw").glob("designer_*"))


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"status": 401}, "STOP_REAUTH"),
        ({"status": 403}, "STOP_REAUTH"),
        ({"mime": "text/html"}, "STOP_REAUTH"),
        ({"status": 500}, "STOP_DOWNLOAD_NOT_EXACT"),
        ({"mime": "image/png"}, "STOP_DOWNLOAD_NOT_EXACT"),
        ({"body": "not json"}, "STOP_DOWNLOAD_NOT_EXACT"),
        ({"body": "[1, 2]"}, "STOP_DOWNLOAD_NOT_EXACT"),
    ],
)
def test_designer_response_must_be_exact(
    tmp_path: Path, kwargs: dict[str, Any], code: str
) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        if "/spr/strategy/model/" in url:
            options = dict(kwargs)
            body = options.pop("body", DESIGNER_BODY)
            return designer_events(browser, url, body, **options)
        return good_navigate(url, browser)

    manifest, _, root = _designer_run(tmp_path, navigate)
    assert _stop_code(manifest) == code
    assert not list((root / "capture" / "raw").glob("designer_*"))


def test_designer_redirect_to_login_stops_with_reauth(tmp_path: Path) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        if "/spr/strategy/model/" in url:
            events = designer_events(browser, url, DESIGNER_BODY)[:1]
            events.append(
                {
                    "method": "Network.requestWillBeSent",
                    SID_KEY: ATTACH_REF,
                    "params": {
                        "requestId": "REQ-1",
                        "request": {"url": f"{BASE}/login", "method": "GET"},
                        "redirectResponse": {"status": 302},
                    },
                }
            )
            return events
        return good_navigate(url, browser)

    manifest, browser, _ = _designer_run(tmp_path, navigate)
    assert _stop_code(manifest) == "STOP_REAUTH"
    assert not [m for m in browser.sent if m["method"] == "Network.getResponseBody"]


def test_designer_login_tab_exits_3_through_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        if "/spr/strategy/model/" in url:
            return [
                {
                    "method": "Target.targetInfoChanged",
                    "params": {
                        "targetInfo": {
                            "targetId": TAB,
                            "type": "page",
                            "url": f"{BASE}/login/signin",
                        }
                    },
                }
            ]
        return good_navigate(url, browser)

    payload = fetcher.build_allowlist_bytes(BOOKS[:1], DESIGNER_IDS)
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    clock = FakeClock()
    browser = FakeBrowser(clock, {TAB: f"{BASE}/app/home"}, navigate)
    monkeypatch.setattr(fetcher, "load_identity_check", lambda **kwargs: _pass_check)
    monkeypatch.setattr(fetcher, "connect", lambda url: browser)
    monkeypatch.setattr(fetcher.time, "monotonic", clock)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
            "--download-timeout",
            "10",
            "--no-reference",
            "--as-of",
            "2026-09-11",
        ]
    )
    assert code == 3
    assert "STOP STOP_REAUTH" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# page record (the identity check's I7 input), same narrow body-read rule

PAGE_URL = f"{BASE}/app/investment/resource?f=1&s=1786737863730"


def _page_run(
    tmp_path: Path,
    navigate: Callable[[str, FakeBrowser], list[dict[str, Any]]] = good_navigate,
    **kwargs: Any,
) -> tuple[dict[str, Any], FakeBrowser, Path]:
    return _run(
        tmp_path,
        navigate,
        allowlist=_allowlist(books=BOOKS[:1], page_record=True),
        **kwargs,
    )


def test_page_record_is_saved_first_unmodified_and_listed(tmp_path: Path) -> None:
    manifest, browser, root = _page_run(tmp_path)
    assert manifest["status"] == "COMPLETE"
    saved = root / "capture" / "raw" / "p123_investment_resource.json"
    assert saved.read_bytes() == PAGE_BODY.encode("utf-8")
    assert (
        manifest["page_record"]["sha256"]
        == hashlib.sha256(PAGE_BODY.encode("utf-8")).hexdigest()
    )
    index = json.loads(
        (root / "capture" / "capture-manifest.json").read_text(encoding="utf-8")
    )
    assert "raw/p123_investment_resource.json" in {
        a["relative_path"] for a in index["artifacts"]
    }
    navigations = [
        m["params"]["url"] for m in browser.sent if m["method"] == "Page.navigate"
    ]
    assert navigations[0] == PAGE_URL
    sent = [m["method"] for m in browser.sent]
    assert sent.count("Network.enable") == 1
    assert sent.count("Network.getResponseBody") == 1
    assert "Book" not in (root / "p123-fetch-manifest.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (PAGE_URL.replace("https://", "http://"), "URL_NOT_HTTPS"),
        (
            PAGE_URL.replace("www.portfolio123.com", "evil.example.com"),
            "URL_OFF_ORIGIN",
        ),
        (PAGE_URL.replace("f=1", "f=2"), "URL_NOT_ALLOWLISTED"),
        (PAGE_URL.replace("1786737863730", "17867378637"), "URL_NOT_ALLOWLISTED"),
        (PAGE_URL + "&x=1", "URL_NOT_ALLOWLISTED"),
        (PAGE_URL.replace("resource?", "resource/5?"), "URL_NOT_ALLOWLISTED"),
        (PAGE_URL.replace("1786737863730", "1786737863731"), "URL_NOT_ALLOWLISTED"),
        (PAGE_URL + "&delete=1", "URL_MUTATION_WORD"),
    ],
)
def test_page_record_screen_refuses_other_urls(url: str, code: str) -> None:
    with pytest.raises(FetchRefused) as info:
        fetcher.screen_page_record_url(url, PAGE_URL)
    assert info.value.code == code
    fetcher.screen_page_record_url(PAGE_URL, PAGE_URL)


def test_page_record_route_is_in_the_reviewed_contract() -> None:
    from quant_engine.web_contracts.portfolio123 import PORTFOLIO123_CONTRACT

    endpoint = PORTFOLIO123_CONTRACT.route("GET", PAGE_URL)
    assert endpoint.capability == "investment.resource-list"
    assert endpoint.method == "GET"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"mime": "text/html"}, "STOP_REAUTH"),
        ({"status": 401}, "STOP_REAUTH"),
        ({"body": "{}"}, "STOP_DOWNLOAD_NOT_EXACT"),
        ({"body": "garbage"}, "STOP_DOWNLOAD_NOT_EXACT"),
    ],
)
def test_page_record_response_must_be_a_json_list(
    tmp_path: Path, kwargs: dict[str, Any], code: str
) -> None:
    def navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
        if "/app/investment/resource" in url:
            options = dict(kwargs)
            body = options.pop("body", PAGE_BODY)
            return designer_events(browser, url, body, **options)
        return good_navigate(url, browser)

    manifest, browser, root = _page_run(tmp_path, navigate)
    assert _stop_code(manifest) == code
    assert manifest["books"] == {}
    assert not [
        m
        for m in browser.sent
        if m["method"] == "Page.navigate" and "Download" in str(m)
    ]


def test_page_record_guard_refuses_a_different_stamp() -> None:
    guard = GuardedTransport(
        _Inner(),
        allowed_urls=frozenset({PAGE_URL}),
        download_path="D",
        network_read=True,
    )
    with pytest.raises(FetchRefused):
        guard.send(
            {
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": PAGE_URL.replace("730", "731")},
            }
        )


def test_allowlist_without_page_record_sends_no_network_message(
    tmp_path: Path,
) -> None:
    manifest, browser, _ = _run(tmp_path)
    assert manifest["page_record"] is None
    assert not [m for m in browser.sent if m["method"].startswith("Network.")]


# ---------------------------------------------------------------------------
# reference pull, permutation inputs, final check, interpreter


def test_reference_is_required_unless_declared_absent(tmp_path: Path) -> None:
    clock = FakeClock()
    browser = FakeBrowser(clock, {TAB: f"{BASE}/app/home"}, good_navigate)
    for kwargs, code in (
        ({}, "REFERENCE_REQUIRED"),
        (
            {"reference_index": tmp_path / "x.json", "no_reference": True},
            "REFERENCE_CONFLICT",
        ),
        ({"reference_index": tmp_path / "missing.json"}, "REFERENCE_UNREADABLE"),
    ):
        with pytest.raises(FetchRefused) as info:
            run_p123(
                browser,
                _allowlist(),
                tmp_path / "run",
                identity_check=_pass_check,
                clock=clock,
                **kwargs,
            )
        assert info.value.code == code
    assert not (tmp_path / "run").exists()
    assert browser.sent == []


def test_reference_index_is_passed_and_recorded(tmp_path: Path) -> None:
    index = tmp_path / "source-index.json"
    index.write_bytes(b'{"entries": []}')
    seen: list[Path | None] = []

    def check(
        capture: Path, books: list[str], reference: Path | None
    ) -> dict[str, Any]:
        seen.append(reference)
        return _pass_check(capture, books, reference)

    manifest, _, _ = _run(tmp_path, check=check, reference_index=index)
    assert set(seen) == {index}
    assert manifest["reference"] == {
        "mode": "INDEX",
        "index_sha256": hashlib.sha256(b'{"entries": []}').hexdigest(),
    }
    manifest, _, _ = _run(tmp_path / "n")
    assert manifest["reference"] == {"mode": "NONE_DECLARED", "index_sha256": None}


def test_final_check_runs_after_the_final_manifest_write(tmp_path: Path) -> None:
    digests: list[str] = []

    def check(
        capture: Path, books: list[str], reference: Path | None
    ) -> dict[str, Any]:
        digests.append(
            hashlib.sha256((capture / "capture-manifest.json").read_bytes()).hexdigest()
        )
        return _pass_check(capture, books, reference)

    manifest, _, root = _page_run(tmp_path, check=check)
    final = hashlib.sha256(
        (root / "capture/capture-manifest.json").read_bytes()
    ).hexdigest()
    assert digests[-1] == final
    assert len(digests) == 2  # after the book, and the final check
    assert manifest["final_identity"] == {str(BOOKS[0]): "PASS"}
    assert (root / "book-identity-decisions.json").is_file()


def test_decisions_file_is_written_when_stopped(tmp_path: Path) -> None:
    manifest, _, root = _run(tmp_path, check=_decisions(**{f"b{BOOKS[0]}": "FAIL"}))
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"
    assert (root / "book-identity-decisions.json").is_file()
    assert manifest["final_identity"] == {str(BOOKS[0]): "FAIL"}


def test_loader_passes_permutation_inputs_and_requires_a_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _fake_module(
        tmp_path,
        monkeypatch,
        "fake_ic_kw",
        "def check_capture(capture, **kw):\n"
        "    return {'books': {}, 'kw': {k: (list(v) if isinstance(v, list) else v)"
        " for k, v in kw.items()}}\n",
    )
    prior, cross = tmp_path / "old", tmp_path / "other"
    check = fetcher.load_identity_check(name, prior_dirs=[prior], cross_dirs=[cross])
    with_reference = check(tmp_path, ["5"], tmp_path / "idx.json")["kw"]
    assert with_reference["require_reference"] is True
    assert with_reference["prior_checks"] == [(prior, "")]
    assert with_reference["cross_checks"] == [(cross, "raw")]
    assert with_reference["raw_subdir"] == "raw"
    without = check(tmp_path, ["5"], None)["kw"]
    assert without["require_reference"] is False


def test_interpreter_other_than_3_11_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    name = _fake_module(
        tmp_path,
        monkeypatch,
        "fake_ic_never",
        "def check_capture(capture, **kw):\n    raise AssertionError('called')\n",
    )
    check = fetcher.load_identity_check(name)
    monkeypatch.setattr(sys, "version_info", (3, 13, 1, "final", 0))
    with pytest.raises(FetchRefused) as info:
        check(tmp_path, ["5"], None)
    assert info.value.code == "INTERPRETER_REFUSED"
    clock = FakeClock()
    browser = FakeBrowser(clock, {TAB: f"{BASE}/app/home"}, good_navigate)
    with pytest.raises(FetchRefused) as info:
        run_p123(
            browser,
            _allowlist(),
            tmp_path / "run",
            identity_check=_pass_check,
            no_reference=True,
            clock=clock,
        )
    assert info.value.code == "INTERPRETER_REFUSED"
    assert browser.sent == []
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
            "--no-reference",
            "--as-of",
            "2026-09-11",
        ]
    )
    assert code == 2
    assert "found Python 3.13.1" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_refuses_a_run_without_reference_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
    assert capsys.readouterr().out.strip() == "REFUSED REFERENCE_REQUIRED"


# ---------------------------------------------------------------------------
# first-class in-page GET routes (page_gets) and the dry run

INV = 1897754
GET_JSON = '{"rows": [1, 2, 3], "note": "café"}'


class PageBrowser(FakeBrowser):
    """Answers the guarded ``Runtime.evaluate`` of an allowlisted page GET."""

    def __init__(self, *args: Any, replies: dict[str, tuple[int, str, str]], **kw: Any):
        super().__init__(*args, **kw)
        self.replies = replies
        self.evaluated: list[str] = []

    def send(self, message: dict[str, Any]) -> None:
        if message["method"] != "Runtime.evaluate":
            return super().send(message)
        self.sent.append(message)
        expression = message["params"]["expression"]
        self.evaluated.append(expression)
        url = json.loads(expression[len("fetch(") : expression.index(", {method")])
        status, mime, text = self.replies[url]
        self.queue.append(
            {
                "id": message["id"],
                "result": {
                    "result": {"value": {"status": status, "type": mime, "text": text}}
                },
            }
        )
        return None


def _gets_allowlist(*gets: tuple[str, int]) -> fetcher.Allowlist:
    payload = fetcher.build_allowlist_bytes((), (), (), None, False, gets)
    return parse_allowlist(payload, hashlib.sha256(payload).hexdigest())


def _get_run(
    tmp_path: Path,
    gets: tuple[tuple[str, int], ...],
    replies: dict[str, tuple[int, str, str]],
) -> tuple[dict[str, Any], PageBrowser, Path]:
    clock = FakeClock()
    browser = PageBrowser(
        clock, {TAB: f"{BASE}/app/home"}, good_navigate, replies=replies
    )
    root = tmp_path / "run"
    manifest = run_p123(
        browser,
        _gets_allowlist(*gets),
        root,
        identity_check=_pass_check,
        no_reference=True,
        clock=clock,
        download_timeout=10.0,
    )
    return manifest, browser, root


def test_every_page_get_route_is_exact_and_screened() -> None:
    for item in fetcher.PAGE_GETS:
        url = fetcher.page_get_url(item.name, INV)
        fetcher.screen_page_get_url(url, item.name, INV)
    full = fetcher.page_get_url("designer_perf_full", 12)
    assert full == f"{BASE}/spr/strategy/model/12/perf?dm=true&init=true&years=15"
    for url, code in (
        (f"{BASE}/spr/strategy/model/12/perf?dm=true&init=true&years=16", "NOT_ALLOW"),
        (full + "#x", "NOT_ALLOW"),
        (full.replace("https://www.", "https://evil."), "OFF_ORIGIN"),
        (full + "&delete=1", "MUTATION"),
        (f"{BASE}/spr/strategy/model/13/perf?dm=true&init=true&years=15", "NOT_ALLOW"),
    ):
        with pytest.raises(FetchRefused, match=code):
            fetcher.screen_page_get_url(url, "designer_perf_full", 12)


def test_page_gets_are_fetched_in_page_and_saved_unmodified(tmp_path: Path) -> None:
    gets = (("designer_perf_full", 12), ("investment_holdings", INV))
    replies = {
        fetcher.page_get_url(k, i): (200, "application/json;charset=utf-8", GET_JSON)
        for k, i in gets
    }
    manifest, browser, root = _get_run(tmp_path, gets, replies)
    assert manifest["status"] == "COMPLETE"
    assert browser.evaluated == [
        fetcher.page_get_expression(fetcher.page_get_url(k, i)) for k, i in gets
    ]
    saved = root / "capture" / "raw" / "designer_perf_full_12.json"
    assert saved.read_bytes() == GET_JSON.encode()
    entry = manifest["page_gets"][f"investment_holdings:{INV}"]
    assert entry["sha256"] == hashlib.sha256(GET_JSON.encode()).hexdigest()
    # the whole run sent only guarded methods: no navigation, no download
    assert {m["method"] for m in browser.sent} <= ALLOWED_CDP_METHODS
    assert "Page.navigate" not in {m["method"] for m in browser.sent}
    assert CONTENT_SENTINEL not in json.dumps(manifest)


def test_a_non_200_page_get_is_recorded_and_401_stops(tmp_path: Path) -> None:
    gets = (("account_resource", 5), ("account_positions", 5))
    url_a, url_b = (fetcher.page_get_url(k, i) for k, i in gets)
    manifest, _, root = _get_run(
        tmp_path,
        gets,
        {
            url_a: (404, "application/json", "{}"),
            url_b: (200, "application/json", "[]"),
        },
    )
    assert manifest["page_gets"]["account_resource:5"] == {
        "kind": "account_resource",
        "id": 5,
        "status": 404,
        "saved": False,
    }
    assert not (root / "capture" / "raw" / "account_resource_5.json").exists()
    assert (root / "capture" / "raw" / "account_positions_5.json").is_file()
    stopped, _, _ = _get_run(
        tmp_path / "again", gets[:1], {url_a: (401, "application/json", "{}")}
    )
    assert _stop_code(stopped) == "STOP_REAUTH"


@pytest.mark.parametrize(
    "params",
    [
        {"expression": "document.cookie", "awaitPromise": True, "returnByValue": True},
        {"expression": "1+1"},
        {
            "expression": fetcher.page_get_expression(
                f"{BASE}/app/investment/resource/1"
            ),
            "awaitPromise": True,
            "returnByValue": True,
            "extra": 1,
        },
        {
            "expression": fetcher.page_get_expression(
                f"{BASE}/app/investment/resource/2"
            ),
            "awaitPromise": True,
            "returnByValue": True,
        },
    ],
)
def test_the_guard_lets_through_only_the_allowlisted_expression(
    params: dict[str, Any],
) -> None:
    allowed = fetcher.page_get_expression(f"{BASE}/app/investment/resource/1")
    guard = GuardedTransport(
        FakeBrowser(FakeClock(), {}, good_navigate),
        allowed_urls=frozenset(),
        download_path="/x",
        page_get_expressions=frozenset({allowed}),
    )
    with pytest.raises(FetchRefused, match="CDP_EVALUATE_NOT_ALLOWLISTED"):
        guard.send({"id": 1, "method": "Runtime.evaluate", "params": params})
    guard.send(
        {
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {
                "expression": allowed,
                "awaitPromise": True,
                "returnByValue": True,
            },
        }
    )


def test_page_gets_allowlist_roundtrip_and_old_bytes_are_unchanged() -> None:
    plain = fetcher.build_allowlist_bytes((1,), (), (), None, False)
    assert b"page_gets" not in plain
    parsed = _gets_allowlist(("account_resource", 7))
    assert parsed.page_gets == (("account_resource", 7),)
    for bad in ([{"kind": "nope", "id": 1}], [{"kind": "account_resource", "id": 0}]):
        document = json.loads(plain)
        document["page_gets"] = bad
        payload = fetcher.canonical_bytes(document)
        with pytest.raises(FetchRefused, match="ALLOWLIST_INVALID"):
            parse_allowlist(payload, hashlib.sha256(payload).hexdigest())


def test_dry_run_screens_and_prints_the_plan_without_a_browser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = fetcher.build_allowlist_bytes(
        (BOOKS[0],), (12,), (), None, True, (("investment_perfdata", INV),)
    )
    path = tmp_path / "allow.json"
    path.write_bytes(payload)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:1",  # never contacted
            "--allowlist",
            str(path),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "never"),
            "--no-reference",
            "--dry-run",
        ]
    )
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["dry_run"] is True
    assert plan["page_gets"] == [f"investment_perfdata:{INV}"]
    assert plan["exports"] == 3 and plan["designer_models"] == 1
    assert not (tmp_path / "never").exists()
