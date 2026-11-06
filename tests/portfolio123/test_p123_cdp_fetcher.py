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
) -> fetcher.Allowlist:
    payload = fetcher.build_allowlist_bytes(books, designers, drive, kinds)
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
            result = {"sessionId": "SESSION-1"}
        elif method == "Browser.setDownloadBehavior":
            self.download_path = message["params"].get("downloadPath", "")
        elif method == "Page.navigate":
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
                "params": {"guid": guid, "url": url, "suggestedFilename": name},
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


def _book_of(url: str) -> tuple[str, int]:
    for kind in fetcher.KINDS:
        if url.startswith(f"{BASE}{kind.path}?portid="):
            return kind.name, int(url.rsplit("=", 1)[1])
    raise AssertionError(url)


def good_navigate(url: str, browser: FakeBrowser) -> list[dict[str, Any]]:
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


def test_designer_models_are_refused_unless_skipped(tmp_path: Path) -> None:
    allowlist = _allowlist(designers=(4444444,))
    with pytest.raises(FetchRefused) as info:
        _run(tmp_path, allowlist=allowlist)
    assert info.value.code == "DESIGNER_MODEL_NEEDS_BODY_READ"
    assert not (tmp_path / "run").exists()
    manifest, browser, _ = _run(
        tmp_path, allowlist=allowlist, skip_designer_models=True
    )
    assert manifest["designer_models_not_fetched"] == ["4444444"]
    assert all("model" not in m["params"].get("url", "") for m in browser.sent)


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


def test_no_tab_and_ambiguous_tabs_refuse(tmp_path: Path) -> None:
    with pytest.raises(FetchRefused) as info:
        _run(tmp_path, tabs={"T": "https://example.com/"})
    assert info.value.code == "NO_SIGNED_IN_TAB"
    with pytest.raises(FetchRefused) as info:
        _run(
            tmp_path / "b",
            tabs={"A": f"{BASE}/app/a", "B": f"{BASE}/app/b"},
        )
    assert info.value.code == "TARGET_AMBIGUOUS"


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
                    "params": {"guid": "g2", "url": url, "suggestedFilename": name},
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
    assert calls == [([str(BOOKS[0])], 3), ([str(BOOKS[0]), str(BOOKS[1])], 6)]
    assert manifest["books"][str(BOOKS[0])]["identity"] == "PASS"
    assert manifest["books"][str(BOOKS[1])]["identity"] == "FAIL"
    assert manifest["books"][str(BOOKS[1])]["identity_checks"] == {"I8": "FAIL"}
    assert manifest["books_not_attempted"] == [str(BOOKS[2])]
    assert (root / "book-identity-decisions.json").is_file()


def test_review_continues_unless_pass_is_required(tmp_path: Path) -> None:
    def review(
        capture: Path, books: list[str], reference: Path | None
    ) -> dict[str, Any]:
        return {"books": {b: {"decision": "REVIEW", "checks": []} for b in books}}

    manifest, _, _ = _run(tmp_path, check=review)
    assert manifest["status"] == "COMPLETE"
    assert manifest["books"][str(BOOKS[0])]["identity"] == "REVIEW"
    manifest, _, _ = _run(tmp_path / "strict", check=review, require_identity_pass=True)
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"


def test_missing_decision_is_a_failure(tmp_path: Path) -> None:
    manifest, _, _ = _run(
        tmp_path, check=lambda capture, books, reference: {"books": {}}
    )
    assert _stop_code(manifest) == "STOP_IDENTITY_FAIL"


def test_identity_loader_is_guarded(tmp_path: Path) -> None:
    with pytest.raises(FetchRefused) as info:
        fetcher.load_identity_check(tmp_path)
    assert info.value.code == "IDENTITY_CHECK_UNAVAILABLE"
    module = tmp_path / "p123_book_identity_check.py"
    module.write_text("from quant_engine.portfolio123 import no_such_name\n")
    with pytest.raises(FetchRefused) as info:
        fetcher.load_identity_check(tmp_path)
    assert info.value.code == "IDENTITY_CHECK_UNAVAILABLE"
    module.write_text("x = 1\n")
    with pytest.raises(FetchRefused):
        fetcher.load_identity_check(tmp_path)
    module.write_text(
        "def check_capture(capture, **kwargs):\n"
        "    return {'books': {b: {'decision': 'PASS', 'checks': [], "
        "'kw': sorted(kwargs)} for b in kwargs['books']}}\n"
    )
    check = fetcher.load_identity_check(tmp_path)
    result = check(tmp_path, ["5"], None)
    assert result["books"]["5"]["decision"] == "PASS"
    assert result["books"]["5"]["kw"] == [
        "books",
        "raw_subdir",
        "reference_index",
        "use_manifest",
    ]


def test_cli_refuses_without_identity_check_before_any_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = fetcher.build_allowlist_bytes(BOOKS[:1])
    path = tmp_path / "allow.json"
    path.write_bytes(payload)

    def unavailable() -> None:
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
    with pytest.raises(FetchRefused) as info:
        _drive(tmp_path / "z", navigate, tabs={TAB: f"{BASE}/app/home"})
    assert info.value.code == "NO_SIGNED_IN_TAB"


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
