"""Offline proof of the Quantbase CDP navigator.

A fake CDP transport replaces the browser, exactly as in the recorder tests. No
browser, no network, no credential: the tab URLs below are synthetic.
"""

from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from typing import Any

import pytest

import quantbase_cdp_har_recorder as rec
import quantbase_cdp_navigator as nav
from quantbase_cdp_navigator import (
    ALLOWED_CDP_METHODS,
    GuardedTransport,
    Navigator,
    NavigatorError,
    parse_allowlist,
    validate_url,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = "https://app.getquantbase.com"
SENTINEL = "SENTINEL-QUERY-VALUE-0123456789"
TAB_ID = "TAB-1"
SID = "SESSION-1"
SID_KEY = "session" + "Id"

PAGES = [
    {"label": "dashboard", "url": f"{APP}/dashboard"},
    {"label": "activity", "url": f"{APP}/account-activity"},
    {"label": "fund", "url": f"{APP}/fund/details/Volatis%20Strategy"},
]


def allowlist_bytes(pages: list[dict[str, Any]] | None = None, **extra: Any) -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "dwell_seconds": 2,
        "pages": PAGES if pages is None else pages,
    }
    document.update(extra)
    return json.dumps(document).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeBrowser:
    """Scripted browser. ``final`` maps a navigated URL to the tab URL afterwards."""

    def __init__(self, clock: Clock, tab_url: str = f"{APP}/dashboard") -> None:
        self.clock = clock
        self.inbound: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []
        self.tabs: list[dict[str, Any]] = [
            {"targetId": TAB_ID, "type": "page", "url": tab_url},
            {"targetId": "BG", "type": "service_worker", "url": f"{APP}/sw.js"},
        ]
        self.final: dict[str, str] = {}
        self.no_load: set[str] = set()
        self.fail: str | None = None
        self.navigate_error: str | None = None
        self.closed = False

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        mid, method = message["id"], message["method"]
        if self.fail == method:
            self.inbound.append({"id": mid, "error": {"code": -32000, "message": "x"}})
            return
        result: dict[str, Any] = {}
        if method == "Target.getTargets":
            result = {"targetInfos": [dict(t) for t in self.tabs]}
        elif method == "Target.attachToTarget":
            result = {SID_KEY: SID}
        elif method == "Page.navigate":
            url = message["params"]["url"]
            self.tabs[0]["url"] = self.final.get(url, url)
            if self.navigate_error:
                result = {"errorText": self.navigate_error}
            self.inbound.append({"id": mid, "result": result})
            if url not in self.no_load:
                self.inbound.append(
                    {"method": "Page.loadEventFired", "params": {}, SID_KEY: SID}
                )
            return
        self.inbound.append({"id": mid, "result": result})

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self.inbound:
            return self.inbound.popleft()
        self.clock.now += timeout
        return None

    def close(self) -> None:
        self.closed = True

    @property
    def navigated(self) -> list[str]:
        return [m["params"]["url"] for m in self.sent if m["method"] == "Page.navigate"]


def run(
    browser: FakeBrowser, clock: Clock, pages: list[dict[str, Any]] | None = None
) -> tuple[int, list[dict[str, Any]]]:
    allowlist = parse_allowlist(allowlist_bytes(pages))
    lines: list[dict[str, Any]] = []
    code = Navigator(browser, allowlist, emit=lines.append, clock=clock).run()
    return code, lines


def setup() -> tuple[Clock, FakeBrowser]:
    clock = Clock()
    return clock, FakeBrowser(clock)


# --- happy path --------------------------------------------------------------


def test_happy_path_navigates_in_allowlist_order_and_reports_each_step() -> None:
    clock, browser = setup()
    code, lines = run(browser, clock)
    assert code == 0
    assert browser.navigated == [p["url"] for p in PAGES]
    assert [m["method"] for m in browser.sent[:3]] == [
        "Target.getTargets",
        "Target.attachToTarget",
        "Page.enable",
    ]
    assert {m["method"] for m in browser.sent} <= ALLOWED_CDP_METHODS
    steps = [x for x in lines if x["event"] == "step"]
    assert [s["label"] for s in steps] == ["dashboard", "activity", "fund"]
    assert all(s["result"] == "ok" and s["redirected_in_app"] is False for s in steps)
    assert lines[0]["event"] == "allowlist"
    assert lines[0]["sha256"] == hashlib.sha256(allowlist_bytes()).hexdigest()
    summary = lines[-1]
    assert summary["event"] == "summary"
    assert (summary["ok"], summary["timeout"], summary["stopped"]) == (3, 0, None)
    # the dwell is honoured: 3 pages x 2 s of (fake) clock at least
    assert clock.now - 1000.0 >= 6.0


def test_attach_uses_flatten_and_only_the_quantbase_page_target() -> None:
    clock, browser = setup()
    run(browser, clock)
    attach = [m for m in browser.sent if m["method"] == "Target.attachToTarget"]
    assert attach[0]["params"] == {"targetId": TAB_ID, "flatten": True}
    assert all(
        m.get(SID_KEY) == SID for m in browser.sent if m["method"].startswith("Page.")
    )


def test_load_timeout_is_recorded_and_the_run_continues() -> None:
    clock, browser = setup()
    browser.no_load.add(PAGES[1]["url"])
    code, lines = run(browser, clock)
    assert code == 1
    results = {s["label"]: s["result"] for s in lines if s["event"] == "step"}
    assert results == {"dashboard": "ok", "activity": "timeout", "fund": "ok"}
    assert len(browser.navigated) == 3


def test_in_app_redirect_is_flagged_but_not_a_stop() -> None:
    clock, browser = setup()
    browser.final[PAGES[0]["url"]] = f"{APP}/dashboard?tab=2"
    code, lines = run(browser, clock)
    assert code == 0
    steps = [s for s in lines if s["event"] == "step"]
    assert steps[0]["redirected_in_app"] is True
    assert steps[1]["redirected_in_app"] is False


# --- STOP rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("landing", "reason"),
    [
        (f"{APP}/sign-in", "LOGIN_REDIRECT"),
        (f"{APP}/login?next=%2Fdashboard", "LOGIN_REDIRECT"),
        (f"{APP}/auth/callback", "LOGIN_REDIRECT"),
        ("https://login.example.test/start", "OFF_ORIGIN"),
        ("http://app.getquantbase.com/dashboard", "OFF_ORIGIN"),
        ("https://app.getquantbase.com.evil.test/dashboard", "OFF_ORIGIN"),
        ("about:blank", "OFF_ORIGIN"),
        ("", "OFF_ORIGIN"),
    ],
)
def test_stop_reauth_when_the_tab_lands_on_login_or_off_origin(
    landing: str, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    clock, browser = setup()
    browser.final[PAGES[1]["url"]] = landing
    code, lines = run(browser, clock)
    assert code == 3
    # stopped immediately: the page after the bad one is never navigated to
    assert browser.navigated == [PAGES[0]["url"], PAGES[1]["url"]]
    stop = [x for x in lines if x["event"] == "stop"][0]
    assert stop == {
        "event": "stop",
        "code": "STOP_REAUTH",
        "reason": reason,
        "at": "activity",
    }
    assert [s["label"] for s in lines if s["event"] == "step"] == ["dashboard"]
    assert lines[-1]["stopped"] == reason
    assert "STOP_REAUTH" in capsys.readouterr().err


def test_stop_reauth_on_any_cdp_error_during_navigation() -> None:
    clock, browser = setup()
    browser.fail = "Page.navigate"
    code, lines = run(browser, clock)
    assert code == 3
    assert [x["reason"] for x in lines if x["event"] == "stop"] == ["CDP_ERROR"]
    assert browser.navigated == [PAGES[0]["url"]]


def test_stop_reauth_when_navigate_reports_an_error_text() -> None:
    clock, browser = setup()
    browser.navigate_error = "net::ERR_X"
    code, lines = run(browser, clock)
    assert code == 3
    assert [x["reason"] for x in lines if x["event"] == "stop"] == ["NAVIGATE_ERROR"]
    assert len(browser.navigated) == 1


@pytest.mark.parametrize("method", ["Target.attachToTarget", "Page.enable"])
def test_stop_reauth_on_cdp_error_while_attaching(method: str) -> None:
    clock, browser = setup()
    browser.fail = method
    code, _ = run(browser, clock)
    assert code == 3
    assert browser.navigated == []


def test_stop_reauth_when_the_transport_dies() -> None:
    clock, browser = setup()

    def dead(timeout: float) -> dict[str, Any] | None:
        raise rec.RecorderError("CDP_CLOSED")

    browser.receive = dead  # type: ignore[method-assign]
    code, lines = run(browser, clock)
    assert code == 3
    assert [x["reason"] for x in lines if x["event"] == "stop"] == ["CDP_ERROR"]


def test_stop_reauth_when_the_call_never_answers() -> None:
    clock, browser = setup()
    browser.send = lambda message: browser.sent.append(message)  # type: ignore[method-assign]
    code, _ = run(browser, clock)
    assert code == 3


def test_no_quantbase_tab_stops_before_any_navigation() -> None:
    clock, browser = setup()
    browser.tabs[0]["url"] = "https://example.test/"
    code, lines = run(browser, clock)
    assert code == 3
    assert browser.navigated == []
    assert [x["reason"] for x in lines if x["event"] == "stop"] == ["NO_TAB"]


def test_a_tab_already_on_the_login_page_stops_before_any_navigation() -> None:
    clock, browser = setup()
    browser.tabs[0]["url"] = f"{APP}/sign-in"
    code, lines = run(browser, clock)
    assert code == 3
    assert browser.navigated == []
    assert [x["reason"] for x in lines if x["event"] == "stop"] == ["LOGIN_REDIRECT"]


def test_two_quantbase_tabs_are_refused_not_guessed() -> None:
    clock, browser = setup()
    browser.tabs.append({"targetId": "T2", "type": "page", "url": f"{APP}/profile"})
    with pytest.raises(NavigatorError, match="MULTIPLE_QUANTBASE_TABS"):
        run(browser, clock)
    assert browser.navigated == []


def test_output_never_carries_a_tab_url_or_query_value() -> None:
    clock, browser = setup()
    browser.final[PAGES[0]["url"]] = f"{APP}/dashboard?token_{SENTINEL}=1&k={SENTINEL}"
    code, lines = run(browser, clock)
    assert code == 0
    blob = json.dumps(lines)
    assert SENTINEL not in blob
    assert "http" not in blob


# --- allow-list validation: each rule refuses -------------------------------


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://app.getquantbase.com/dashboard", "URL_NOT_HTTPS"),
        ("ftp://app.getquantbase.com/dashboard", "URL_NOT_HTTPS"),
        ("https://qbapi3.com/api/funds/stats/", "URL_OFF_ORIGIN"),
        ("https://example.test/dashboard", "URL_OFF_ORIGIN"),
        ("https://app.getquantbase.com.evil.test/dashboard", "URL_OFF_ORIGIN"),
        ("https://evil.test@app.getquantbase.com/dashboard", "URL_HAS_USERINFO"),
        ("https://user:pw@app.getquantbase.com/dashboard", "URL_HAS_USERINFO"),
        ("https://app.getquantbase.com:8443/dashboard", "URL_HAS_PORT"),
        ("https://app.getquantbase.com:443/dashboard", "URL_HAS_PORT"),
        ("https://app.getquantbase.com/dashboard#delete", "URL_HAS_FRAGMENT"),
        ("https://app.getquantbase.com/dashboard#", "URL_HAS_FRAGMENT"),
        (
            "https://app.getquantbase.com/fund/details/{fund_name}",
            "URL_HAS_PLACEHOLDER",
        ),
        ("https://app.getquantbase.com/a/../dashboard", "URL_PATH_UNSAFE"),
        ("https://app.getquantbase.com/a/%2e%2e/dashboard", "URL_PATH_UNSAFE"),
        ("https://app.getquantbase.com\\dashboard", "URL_PATH_UNSAFE"),
        ("https://app.getquantbase.com", "URL_PATH_UNSAFE"),
        ("https://app.getquantbase.com/da shboard", "URL_NOT_PRINTABLE_ASCII"),
        ("https://app.getquantbase.com/dash\nboard", "URL_NOT_PRINTABLE_ASCII"),
        ("https://app.getquantbase.com/dashböard", "URL_NOT_PRINTABLE_ASCII"),
        ("", "URL_NOT_PRINTABLE_ASCII"),
        ("https://app.getquantbase.com/sign-in", "URL_LOGIN_LIKE"),
        ("https://app.getquantbase.com/login", "URL_LOGIN_LIKE"),
        ("https://app.getquantbase.com/auth/session", "URL_LOGIN_LIKE"),
    ],
)
def test_url_rules_refuse(url: str, code: str) -> None:
    with pytest.raises(NavigatorError) as caught:
        validate_url(url)
    assert caught.value.code == code


def test_non_string_url_is_refused() -> None:
    for value in (None, 5, ["https://app.getquantbase.com/dashboard"]):
        with pytest.raises(NavigatorError, match="URL_NOT_PRINTABLE_ASCII"):
            validate_url(value)


def test_every_mutation_word_is_refused_in_path_query_and_encoded_form() -> None:
    assert nav.MUTATION_WORDS is rec.MUTATION_WORDS  # one list, imported, not copied
    for word in rec.MUTATION_WORDS:
        for url in (
            f"{APP}/{word}",
            f"{APP}/dashboard/{word.upper()}",
            f"{APP}/dashboard?x={word}",
            f"{APP}/a-{word}-b",
            f"{APP}/%{ord(word[0]):02x}{word[1:]}",
        ):
            with pytest.raises(NavigatorError) as caught:
                validate_url(url)
            assert caught.value.code == "URL_MUTATION_LIKE", url


def test_plain_read_only_pages_pass_the_url_rules() -> None:
    for page in PAGES:
        assert validate_url(page["url"]) == page["url"]
    assert validate_url(f"{APP}/dashboard?range=1y") == f"{APP}/dashboard?range=1y"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json", "ALLOWLIST_NOT_JSON"),
        (b"\xff\xfe", "ALLOWLIST_NOT_JSON"),
        (b"[]", "ALLOWLIST_SHAPE"),
        (allowlist_bytes(schema_version=2), "ALLOWLIST_SCHEMA_VERSION"),
        (allowlist_bytes(extra_key=1), "ALLOWLIST_SCHEMA_VERSION"),
        (allowlist_bytes([]), "ALLOWLIST_PAGES_INVALID"),
        (
            allowlist_bytes(
                [{"label": f"p{i}", "url": f"{APP}/p{i}"} for i in range(51)]
            ),
            "ALLOWLIST_PAGES_INVALID",
        ),
        (allowlist_bytes(["x"]), "ALLOWLIST_ENTRY_SHAPE"),  # type: ignore[list-item]
        (
            allowlist_bytes([{"label": "a", "url": f"{APP}/dashboard", "act": "x"}]),
            "ALLOWLIST_ENTRY_SHAPE",
        ),
        (
            allowlist_bytes([{"label": "a b", "url": f"{APP}/dashboard"}]),
            "ALLOWLIST_LABEL_INVALID",
        ),
        (allowlist_bytes([{"url": f"{APP}/dashboard"}]), "ALLOWLIST_LABEL_INVALID"),
        (
            allowlist_bytes(
                [
                    {"label": "a", "url": f"{APP}/dashboard"},
                    {"label": "a", "url": f"{APP}/profile"},
                ]
            ),
            "ALLOWLIST_LABEL_DUPLICATE",
        ),
        (
            allowlist_bytes(
                [
                    {"label": "a", "url": f"{APP}/dashboard"},
                    {"label": "b", "url": f"{APP}/dashboard"},
                ]
            ),
            "ALLOWLIST_URL_DUPLICATE",
        ),
        (allowlist_bytes(dwell_seconds=-1), "ALLOWLIST_DWELL_INVALID"),
        (allowlist_bytes(dwell_seconds=True), "ALLOWLIST_DWELL_INVALID"),
        (allowlist_bytes(dwell_seconds=9999), "ALLOWLIST_DWELL_INVALID"),
        (
            allowlist_bytes(
                [{"label": "a", "url": f"{APP}/dashboard", "dwell_seconds": "5"}]
            ),
            "ALLOWLIST_DWELL_INVALID",
        ),
        (
            allowlist_bytes([{"label": "a", "url": "https://example.test/dashboard"}]),
            "URL_OFF_ORIGIN",
        ),
        (
            allowlist_bytes([{"label": "a", "url": f"{APP}/profile/delete"}]),
            "URL_MUTATION_LIKE",
        ),
    ],
)
def test_allowlist_refusals(raw: bytes, code: str) -> None:
    with pytest.raises(NavigatorError) as caught:
        parse_allowlist(raw)
    assert caught.value.code == code


def test_allowlist_dwell_defaults_and_overrides() -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "pages": [
                {"label": "a", "url": f"{APP}/dashboard"},
                {"label": "b", "url": f"{APP}/profile", "dwell_seconds": 0},
            ],
        }
    ).encode()
    pages = parse_allowlist(raw).pages
    assert [p.dwell_seconds for p in pages] == [nav.DEFAULT_DWELL_SECONDS, 0.0]


# --- hash pin and the CLI ----------------------------------------------------


def write_allowlist(tmp_path: Path, raw: bytes | None = None) -> Path:
    path = tmp_path / "allowlist.json"
    path.write_bytes(allowlist_bytes() if raw is None else raw)
    return path


def cli(
    tmp_path: Path,
    *extra: str,
    cdp: str = "http://127.0.0.1:9333",
    raw: bytes | None = None,
    pin: bool = True,
) -> tuple[int, list[dict[str, Any]], list[str], FakeBrowser]:
    clock = Clock()
    browser = FakeBrowser(clock)
    connected: list[str] = []

    def connect(url: str) -> FakeBrowser:
        connected.append(url)
        return browser

    lines: list[dict[str, Any]] = []
    path = write_allowlist(tmp_path, raw)
    pinned: list[str] = []
    if pin and "--allowlist-sha256" not in extra:
        pinned = ["--allowlist-sha256", hashlib.sha256(path.read_bytes()).hexdigest()]
    code = nav.main(
        ["--cdp-url", cdp, "--allowlist", str(path), *pinned, *extra],
        connect=connect,
        emit=lines.append,
        clock=clock,
    )
    return code, lines, connected, browser


def test_pin_mismatch_is_refused_before_any_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, connected, browser = cli(tmp_path, "--allowlist-sha256", "0" * 64)
    assert code == 2
    assert "REFUSED ALLOWLIST_SHA256_MISMATCH" in capsys.readouterr().out
    assert connected == [] and browser.sent == [] and lines == []


def test_pin_must_be_a_sha256(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, connected, _ = cli(tmp_path, "--allowlist-sha256", "abc")
    assert code == 2 and connected == []
    assert "PIN_NOT_SHA256" in capsys.readouterr().out


def test_matching_pin_runs_and_the_hash_is_printed_first(tmp_path: Path) -> None:
    digest = hashlib.sha256(allowlist_bytes()).hexdigest()
    code, lines, connected, browser = cli(tmp_path, "--allowlist-sha256", digest)
    assert code == 0
    assert connected == ["http://127.0.0.1:9333"]
    assert lines[0] == {"event": "allowlist", "sha256": digest, "pages": 3}
    assert browser.closed


def test_a_changed_allowlist_byte_changes_the_hash(tmp_path: Path) -> None:
    pinned = hashlib.sha256(allowlist_bytes()).hexdigest()
    raw = allowlist_bytes(note="edited after pinning")
    code, _, connected, _ = cli(tmp_path, "--allowlist-sha256", pinned, raw=raw)
    assert code == 2 and connected == []


@pytest.mark.parametrize(
    "cdp",
    [
        "https://127.0.0.1:9333",
        "http://example.test:9333",
        "http://192.168.1.5:9333",
        "http://127.0.0.1",
        "ws://127.0.0.1:9333",
    ],
)
def test_non_loopback_cdp_url_is_refused_before_connecting(
    tmp_path: Path, cdp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, connected, _ = cli(tmp_path, cdp=cdp)
    assert code == 2 and connected == []
    assert "CDP_URL_NOT_LOOPBACK_HTTP" in capsys.readouterr().out


@pytest.mark.parametrize(
    "cdp", ["http://127.0.0.1:9333", "http://localhost:9222", "http://[::1]:9333"]
)
def test_loopback_cdp_urls_are_accepted(tmp_path: Path, cdp: str) -> None:
    code, _, connected, _ = cli(tmp_path, cdp=cdp)
    assert code == 0 and connected == [cdp]


def test_invalid_allowlist_is_refused_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = allowlist_bytes([{"label": "a", "url": f"{APP}/profile/delete"}])
    code, _, connected, _ = cli(tmp_path, raw=raw)
    assert code == 2 and connected == []
    assert "REFUSED URL_MUTATION_LIKE" in capsys.readouterr().out


def test_missing_allowlist_file_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    code = nav.main(
        [
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            "no-such-file.json",
            "--allowlist-sha256",
            "0" * 64,
        ],
        connect=lambda url: (_ for _ in ()).throw(AssertionError("connected")),
    )
    assert code == 2
    assert "ALLOWLIST_UNREADABLE" in capsys.readouterr().out


def test_cli_stop_reauth_exit_code(tmp_path: Path) -> None:
    clock = Clock()
    browser = FakeBrowser(clock)
    browser.final[PAGES[0]["url"]] = f"{APP}/sign-in"
    code = nav.main(
        [
            "--cdp-url",
            "http://127.0.0.1:9333",
            "--allowlist",
            str(write_allowlist(tmp_path)),
            "--allowlist-sha256",
            hashlib.sha256(allowlist_bytes()).hexdigest(),
        ],
        connect=lambda url: browser,
        emit=lambda record: None,
        clock=clock,
    )
    assert code == 3
    assert browser.navigated == [PAGES[0]["url"]]
    assert browser.closed


# --- CDP allow-list guard ----------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        return None

    def close(self) -> None:
        pass


def test_guard_refuses_every_message_that_could_act_on_the_page() -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset({PAGES[0]["url"]}))
    for method in (
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Input.synthesizeTapGesture",
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Page.reload",
        "Page.addScriptToEvaluateOnNewDocument",
        "Page.navigateToHistoryEntry",
        "Page.setDocumentContent",
        "Fetch.enable",
        "Fetch.continueRequest",
        "Network.enable",
        "Network.setExtraHTTPHeaders",
        "Network.setCookie",
        "Network.getCookies",
        "Network.getResponseBody",
        "Storage.getCookies",
        "DOM.setOuterHTML",
        "Emulation.setScriptExecutionDisabled",
        "Target.createTarget",
        "Target.closeTarget",
        "Browser.close",
        "",
    ):
        with pytest.raises(NavigatorError, match="CDP_METHOD_REFUSED"):
            guard.send({"id": 1, "method": method, "params": {}})
    with pytest.raises(NavigatorError, match="CDP_METHOD_REFUSED"):
        guard.send({"id": 1, "params": {}})
    assert inner.sent == []


def test_guard_passes_the_four_allowed_methods_and_only_those() -> None:
    assert ALLOWED_CDP_METHODS == {
        "Target.getTargets",
        "Target.attachToTarget",
        "Page.enable",
        "Page.navigate",
    }
    inner = Sink()
    guard = GuardedTransport(inner, frozenset({PAGES[0]["url"]}))
    for method in sorted(ALLOWED_CDP_METHODS - {"Page.navigate"}):
        guard.send({"id": 1, "method": method, "params": {}})
    guard.send({"id": 2, "method": "Page.navigate", "params": {"url": PAGES[0]["url"]}})
    assert len(inner.sent) == len(ALLOWED_CDP_METHODS)


@pytest.mark.parametrize(
    "params",
    [
        {"url": f"{APP}/profile"},  # valid page, but not in this allow-list
        {"url": "https://example.test/"},
        {"url": f"{APP}/dashboard", "referrer": "https://example.test/"},
        {"url": f"{APP}/dashboard", "transitionType": "typed"},
        {"url": f"{APP}/dashboard#x"},
        {},
        {"url": None},
    ],
)
def test_guard_refuses_a_navigate_that_is_not_exactly_one_allowlisted_url(
    params: dict[str, Any],
) -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset({PAGES[0]["url"]}))
    with pytest.raises(NavigatorError, match="CDP_NAVIGATE_URL_REFUSED"):
        guard.send({"id": 1, "method": "Page.navigate", "params": params})
    assert inner.sent == []


def test_guard_refuses_navigate_without_params() -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset({PAGES[0]["url"]}))
    with pytest.raises(NavigatorError, match="CDP_NAVIGATE_URL_REFUSED"):
        guard.send({"id": 1, "method": "Page.navigate"})


def test_navigator_cannot_be_made_to_send_an_acting_message() -> None:
    clock, browser = setup()
    navigator = Navigator(
        browser, parse_allowlist(allowlist_bytes()), emit=lambda r: None, clock=clock
    )
    for method in ("Input.dispatchMouseEvent", "Runtime.evaluate"):
        with pytest.raises(NavigatorError, match="CDP_METHOD_REFUSED"):
            navigator._call(method, {}, None)
    assert browser.sent == []


def test_tool_source_never_names_an_acting_cdp_method() -> None:
    source = (REPOSITORY_ROOT / "tools/quantbase_cdp_navigator.py").read_text(
        encoding="utf-8"
    )
    named = set(re.findall(r'"((?:[A-Z][A-Za-z]+)\.[a-z][A-Za-z]+)"', source))
    assert named == (
        ALLOWED_CDP_METHODS | nav.SCROLL_ONLY_METHODS | {"Page.loadEventFired"}
    )
    assert nav.SCROLL_ONLY_METHODS == {"Input.dispatchMouseEvent"}
    for banned in ("Runtime.", "Fetch.", "Network.", "Page.reload"):
        assert f'"{banned}' not in source
    # the only input event type the source ever names is the wheel
    for acting in (
        "mousePressed",
        "mouseReleased",
        "mouseMoved",
        "keyDown",
        "touchStart",
    ):
        assert acting not in source
    assert source.count('"mouseWheel"') == 2  # the guard and the sender


# --- committed allow-list data ----------------------------------------------


def test_the_derived_allowlist_parses_and_holds_only_masked_read_only_pages() -> None:
    path = REPOSITORY_ROOT / "docs/providers/quantbase-navigator-allowlist.derived.json"
    parsed = parse_allowlist(path.read_bytes())
    assert [p.label for p in parsed.pages] == [
        "profile",
        "dashboard",
        "account-activity",
        "fund-volatis",
    ]
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\d{3,}", "".join(p.url for p in parsed.pages))
    assert not re.search(r"jwt|cookie|bearer|eyJ", text, re.IGNORECASE)


def test_the_template_is_refused_until_the_operator_fills_the_placeholder() -> None:
    path = (
        REPOSITORY_ROOT / "docs/providers/quantbase-navigator-allowlist.template.json"
    )
    with pytest.raises(NavigatorError, match="URL_HAS_PLACEHOLDER"):
        parse_allowlist(path.read_bytes())
    assert "UNVERIFIED" in path.read_text(encoding="utf-8")


TEMPLATE = (
    REPOSITORY_ROOT / "docs/providers/quantbase-navigator-allowlist.template.json"
)


def test_template_filled_in_by_the_operator_is_accepted() -> None:
    raw = TEMPLATE.read_bytes().replace(b"{fund_name}", b"Example%20Fund")
    parsed = parse_allowlist(raw)
    assert [p.label for p in parsed.pages][-1] == "fund-other"


@pytest.mark.parametrize(
    "entry",
    [
        {"label": "x", "url": f"{APP}/fund/details/{{fund_name}}"},
        {"label": "x", "url": f"{APP}/fund/details/%7Bfund_name%7D"},
        {"label": "{fund_name}", "url": f"{APP}/profile"},
        {"label": "x", "url": f"{APP}/fund/details/{{anything}}"},
        {"label": "x", "url": f"{APP}/p?q={{id}}"},
    ],
)
def test_any_leftover_placeholder_in_label_or_url_is_refused(
    entry: dict[str, str],
) -> None:
    pages = [{"label": "ok", "url": f"{APP}/dashboard"}, entry]
    with pytest.raises(NavigatorError) as caught:
        parse_allowlist(allowlist_bytes(pages))
    assert caught.value.code in {
        "URL_HAS_PLACEHOLDER",
        "ALLOWLIST_LABEL_INVALID",
        "URL_MUTATION_LIKE",
    }


def test_cli_refuses_the_unfilled_template_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, connected, browser = cli(tmp_path, raw=TEMPLATE.read_bytes())
    assert code == 2 and connected == [] and browser.sent == [] and lines == []
    assert "REFUSED URL_HAS_PLACEHOLDER" in capsys.readouterr().out


# --- ordering, dwell default, never revisit ---------------------------------


def test_default_dwell_is_thirty_seconds() -> None:
    raw = json.dumps(
        {"schema_version": 1, "pages": [{"label": "a", "url": f"{APP}/profile"}]}
    ).encode()
    assert nav.DEFAULT_DWELL_SECONDS == 30.0
    assert parse_allowlist(raw).pages[0].dwell_seconds == 30.0


@pytest.mark.parametrize(
    "second",
    [
        f"{APP}/account-activity",
        f"{APP}/account-activity?x=1",
        f"{APP}/account-activity/",
        f"{APP}/Account-Activity",
        f"{APP}/account%2Dactivity",
    ],
)
def test_account_activity_listed_twice_is_refused(second: str) -> None:
    pages = [
        {"label": "a", "url": f"{APP}/account-activity"},
        {"label": "b", "url": second},
    ]
    with pytest.raises(NavigatorError) as caught:
        parse_allowlist(allowlist_bytes(pages))
    assert caught.value.code in {
        "ALLOWLIST_ACCOUNT_ACTIVITY_REPEATED",
        "ALLOWLIST_URL_DUPLICATE",
    }


def test_any_page_repeated_with_a_different_query_is_refused() -> None:
    pages = [
        {"label": "a", "url": f"{APP}/dashboard"},
        {"label": "b", "url": f"{APP}/dashboard?range=1y"},
    ]
    with pytest.raises(NavigatorError, match="ALLOWLIST_PAGE_REVISITED"):
        parse_allowlist(allowlist_bytes(pages))


def test_a_hand_built_allowlist_cannot_revisit_a_page_either() -> None:
    clock, browser = setup()
    base = parse_allowlist(allowlist_bytes())
    twice = nav.Allowlist(base.sha256, base.pages + (base.pages[0],))
    with pytest.raises(NavigatorError, match="ALLOWLIST_PAGE_REVISITED"):
        Navigator(browser, twice, emit=lambda r: None, clock=clock)
    assert browser.sent == []


def test_derived_allowlist_order_profile_first_and_activity_once() -> None:
    path = REPOSITORY_ROOT / "docs/providers/quantbase-navigator-allowlist.derived.json"
    pages = parse_allowlist(path.read_bytes()).pages
    paths = [urlsplit(p.url).path for p in pages]
    assert paths[0] == "/profile" and paths[1] == "/dashboard"
    assert paths.count("/account-activity") == 1
    assert {p.dwell_seconds for p in pages} == {30.0}


def test_step_lines_carry_label_dwell_and_no_counts() -> None:
    clock, browser = setup()
    _, lines = run(browser, clock)
    steps = [x for x in lines if x["event"] == "step"]
    assert [s["label"] for s in steps] == ["dashboard", "activity", "fund"]
    assert all(s["dwell_seconds"] == 2.0 for s in steps)
    assert {s["capability_counts"] for s in steps} == {"READ_RECORDER_MANIFEST"}


# --- --scroll fallback -------------------------------------------------------


def input_sent(browser: FakeBrowser) -> list[dict[str, Any]]:
    return [m for m in browser.sent if m["method"].startswith("Input.")]


def run_scroll(browser: FakeBrowser, clock: Clock, scroll: bool) -> int:
    allowlist = parse_allowlist(allowlist_bytes())
    return Navigator(
        browser, allowlist, emit=lambda r: None, clock=clock, scroll=scroll
    ).run()


def test_without_scroll_no_input_message_is_ever_sent() -> None:
    clock, browser = setup()
    assert run_scroll(browser, clock, scroll=False) == 0
    assert input_sent(browser) == []


def test_scroll_default_is_off_in_the_cli(tmp_path: Path) -> None:
    code, _, _, browser = cli(tmp_path)
    assert code == 0 and input_sent(browser) == []


def test_with_scroll_only_bounded_mouse_wheel_events_are_sent() -> None:
    clock, browser = setup()
    assert run_scroll(browser, clock, scroll=True) == 0
    events = input_sent(browser)
    per_page = nav.WHEEL_DOWN_STEPS * 2
    assert len(events) == 3 * per_page
    for message in events:
        assert message["method"] == "Input.dispatchMouseEvent"
        params = message["params"]
        assert set(params) <= {"type", "x", "y", "deltaX", "deltaY"}
        assert params["type"] == "mouseWheel"
        assert params["deltaX"] == 0 and 0 < abs(params["deltaY"]) <= 600
        assert 0 <= params["x"] <= 2000 and 0 <= params["y"] <= 2000
        assert "button" not in params and "modifiers" not in params
        assert message[SID_KEY] == SID
    for start in range(0, len(events), per_page):
        chunk = [m["params"]["deltaY"] for m in events[start : start + per_page]]
        assert sum(chunk) == 0 and sum(abs(d) for d in chunk) <= 5000
    order = [
        m["method"]
        for m in browser.sent
        if m["method"].startswith(("Page.nav", "Input"))
    ]
    assert order[0] == "Page.navigate" and order[1] == "Input.dispatchMouseEvent"


def test_cli_scroll_flag_enables_the_wheel(tmp_path: Path) -> None:
    code, lines, _, browser = cli(tmp_path, "--scroll")
    assert code == 0 and input_sent(browser)
    assert lines[-1]["scroll"] is True
    steps = [x for x in lines if x["event"] == "step"]
    assert {s["wheel_events"] for s in steps} == {nav.WHEEL_DOWN_STEPS * 2}


def test_no_wheel_when_the_page_did_not_load() -> None:
    clock, browser = setup()
    browser.no_load.update(p["url"] for p in PAGES)
    assert run_scroll(browser, clock, scroll=True) == 1
    assert input_sent(browser) == []


@pytest.mark.parametrize(
    "params",
    [
        {"type": "mousePressed", "x": 5, "y": 5, "button": "left", "clickCount": 1},
        {"type": "mouseReleased", "x": 5, "y": 5, "button": "left"},
        {"type": "mouseMoved", "x": 5, "y": 5},
        {"type": "mouseMoved", "x": 5, "y": 5, "buttons": 1},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 100, "button": "left"},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 100, "buttons": 1},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 100, "modifiers": 2},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 100, "clickCount": 1},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaX": 50, "deltaY": 100},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 601},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": -601},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 0},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 1.5},
        {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": True},
        {"type": "mouseWheel", "x": 5, "y": 5},
        {"type": "mouseWheel", "x": -1, "y": 5, "deltaY": 100},
        {"type": "mouseWheel", "x": 5, "y": 5000, "deltaY": 100},
        {"type": "mouseWheel", "x": "5", "y": 5, "deltaY": 100},
        {"type": "mouseWheel", "y": 5, "deltaY": 100},
        {"x": 5, "y": 5, "deltaY": 100},
        {},
    ],
)
def test_scroll_still_refuses_any_input_that_is_not_a_plain_wheel(
    params: dict[str, Any],
) -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset(), scroll=True)
    with pytest.raises(NavigatorError, match="CDP_INPUT_REFUSED"):
        guard.send({"id": 1, "method": "Input.dispatchMouseEvent", "params": params})
    assert inner.sent == []


def test_scroll_refuses_input_without_params() -> None:
    guard = GuardedTransport(Sink(), frozenset(), scroll=True)
    with pytest.raises(NavigatorError, match="CDP_INPUT_REFUSED"):
        guard.send({"id": 1, "method": "Input.dispatchMouseEvent"})


@pytest.mark.parametrize(
    "method",
    [
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Input.dispatchTouchEvent",
        "Input.synthesizeScrollGesture",
        "Input.synthesizeTapGesture",
        "Input.emulateTouchFromMouseEvent",
        "Input.dispatchDragEvent",
        "Runtime.evaluate",
        "Fetch.enable",
        "Network.enable",
        "Page.reload",
    ],
)
def test_scroll_does_not_widen_anything_else(method: str) -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset(), scroll=True)
    with pytest.raises(NavigatorError, match="CDP_METHOD_REFUSED"):
        guard.send({"id": 1, "method": method, "params": {}})
    assert inner.sent == []


def test_wheel_without_scroll_flag_is_refused_as_a_method() -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset())
    wheel = {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 100}
    with pytest.raises(NavigatorError, match="CDP_METHOD_REFUSED"):
        guard.send({"id": 1, "method": "Input.dispatchMouseEvent", "params": wheel})
    assert inner.sent == []


def test_wheel_budget_is_bounded_per_page_and_resets_on_a_new_page() -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset(), scroll=True)
    wheel = {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 400}
    for _ in range(12):
        guard.send({"id": 1, "method": "Input.dispatchMouseEvent", "params": wheel})
    with pytest.raises(NavigatorError, match="CDP_INPUT_BUDGET_EXCEEDED"):
        guard.send({"id": 1, "method": "Input.dispatchMouseEvent", "params": wheel})
    assert len(inner.sent) == 12
    guard.new_page()
    guard.send({"id": 1, "method": "Input.dispatchMouseEvent", "params": wheel})
    assert len(inner.sent) == 13


def test_wheel_total_delta_is_bounded() -> None:
    inner = Sink()
    guard = GuardedTransport(inner, frozenset(), scroll=True)
    big = {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 600}
    sent = 0
    with pytest.raises(NavigatorError, match="CDP_INPUT_BUDGET_EXCEEDED"):
        for _ in range(12):
            guard.send({"id": 1, "method": "Input.dispatchMouseEvent", "params": big})
            sent += 1
    assert sent * 600 <= 5000 and sent == 8


# --- review findings: pin is required, attempts counted at navigation start --


def test_missing_pin_is_refused_before_any_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, connected, browser = cli(tmp_path, pin=False)
    assert code == 2
    assert "REFUSED PIN_REQUIRED" in capsys.readouterr().out
    assert connected == [] and browser.sent == [] and lines == []


def test_attempted_counts_the_navigation_that_ended_in_a_stop() -> None:
    clock, browser = setup()
    browser.final[PAGES[1]["url"]] = f"{APP}/sign-in"
    code, lines = run(browser, clock)
    assert code == 3
    summary = lines[-1]
    assert summary["attempted"] == len(browser.navigated) == 2
    assert summary["ok"] == 1


def test_attempted_counts_a_navigation_that_errored() -> None:
    clock, browser = setup()
    browser.fail = "Page.navigate"
    _, lines = run(browser, clock)
    assert lines[-1]["attempted"] == 1 and lines[-1]["ok"] == 0
