"""Offline proof of the in-page routes of the Portfolio123 CDP fetcher.

SYNTHETIC: invented ids, a fake CDP transport, and bodies built from
``p123_route_shapes.json`` -- the key structure of the real, retained responses with every
string, number and list element replaced by a neutral placeholder (no real id or value).
"""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tools import p123_cdp_fetcher as fetcher
from tools.p123_cdp_fetcher import FetchRefused, GuardedTransport, run_routes

BASE = "https://www.portfolio123.com"
MODEL, INVEST, ACCOUNT = 1111111, 2222222, 3333333
TAB = "TAB-1"
INCEPTION = "01/01/2008"  # provider format, as the model payload states it
START = "2008-01-01"
SID_KEY = "session" + "Id"
SHAPES = json.loads(
    (Path(__file__).with_name("p123_route_shapes.json")).read_text(encoding="utf-8")
)
CSV_ROW = {
    "investment_holdings_csv": "AAA,Name,0,0,0,0,0,0,0,0,0,0\n",
    "investment_trans_csv": "2000-01-01,DIV,AAA,Name,,,,0,note\n",
}


def _bodies() -> dict[str, tuple[str, bytes]]:
    """route kind -> (content type, body) a healthy page would return."""

    model = dict(SHAPES["designer_model"], portUid=MODEL, inceptionDt=INCEPTION)
    perf = json.loads(json.dumps(SHAPES["designer_perf"]))
    perf["perfData"]["date"] = START
    out: dict[str, tuple[str, bytes]] = {
        "designer_model": ("application/json", json.dumps(model).encode()),
        "designer_perf_full": (
            "application/json",
            json.dumps(perf).encode(),
        ),
        "designer_holdings": (
            "application/json",
            json.dumps(SHAPES["designer_holdings"]).encode(),
        ),
        "investment_perfdata": (
            "application/json",
            json.dumps(SHAPES["investment_perfdata"]).encode(),
        ),
        "investment_resource": (
            "application/json",
            json.dumps(SHAPES["investment_resource"]).encode(),
        ),
        "investment_performance_page": (
            "text/html;charset=utf-8",
            b'<div ng-controller="InvestmentPerformanceCtrl">Loading...</div>\n',
        ),
        "book_port_summary": (
            "text/html;charset=utf-8",
            b"<html><head><title>Summary - Live Book</title></head></html>\n",
        ),
        "investment_performance": ("application/json", b'{"nav": []}'),
        "investment_holdings": ("application/json", b"[]"),
        "investment_trans_resource": ("application/json", b"{}"),
        "investment_perfdata_true": (
            "application/json",
            json.dumps(SHAPES["investment_perfdata"]).encode(),
        ),
        "investment_perfdata_blank": (
            "application/json",
            json.dumps(SHAPES["investment_perfdata"]).encode(),
        ),
        "account_resource": ("application/json", b'{"accountUid": 0}'),
        "account_positions": ("application/json", b"[]"),
    }
    for kind in CSV_ROW:
        header = SHAPES[kind + "_header"]
        out[kind] = ("text/csv", (header + "\n" + CSV_ROW[kind]).encode())
    return out


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RoutesBrowser:
    """A scripted CDP endpoint that answers Runtime.evaluate for pinned expressions."""

    def __init__(self, clock: FakeClock, tabs: dict[str, str]) -> None:
        self.clock = clock
        self.tabs = tabs
        self.page = ""
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()
        self.replies: dict[str, tuple[int, str, bytes]] = {}
        for route in fetcher.ROUTES:
            self._healthy(route)
        self.final_url: dict[str, str] = {}
        self.readiness = "complete"
        self.land_on: str | None = None

    def _healthy(self, route: fetcher.Route) -> None:
        if route.shape == "visit":
            return
        ctype, body = _bodies()[route.name]
        for ident in (MODEL, INVEST, ACCOUNT):
            self.replies[fetcher.route_target_url(route, ident)] = (200, ctype, body)

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        method, ident = message["method"], message["id"]
        result: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
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
            result = {SID_KEY: "ATTACHED-1"}
        elif method == "Page.navigate":
            self.page = self.land_on or message["params"]["url"]
            result = {"frameId": "FRAME-1"}
        elif method == "Runtime.evaluate":
            result = self._evaluate(message["params"]["expression"])
        self.queue.append({"id": ident, "result": result})
        self.queue.extend(events)

    def _evaluate(self, expression: str) -> dict[str, Any]:
        if expression == fetcher.PROBE_EXPRESSION:
            parts = urlsplit(self.page)
            where = parts.path + ("?" + parts.query if parts.query else "")
            value = json.dumps([BASE, where, self.readiness])
            return {"result": {"type": "string", "value": value}}
        start = expression.index("fetch(") + len("fetch(")
        url = json.loads(expression[start : expression.index(",{method:'GET'", start)])
        status, ctype, body = self.replies[url]
        value = json.dumps(
            {
                "s": status,
                "t": ctype,
                "u": self.final_url.get(url, url),
                "b": base64.b64encode(body).decode(),
            }
        )
        return {"result": {"type": "string", "value": value}}

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self.queue:
            return self.queue.popleft()
        self.clock.now += timeout
        return None

    def close(self) -> None:
        pass


def _allowlist(routes: list[tuple[str, int]]) -> fetcher.Allowlist:
    payload = fetcher.build_allowlist_bytes([], routes=routes)
    return fetcher.parse_allowlist(payload, hashlib.sha256(payload).hexdigest())


ALL_ROUTES = [
    ("designer_model", MODEL),
    ("designer_perf_full", MODEL),
    ("designer_holdings", MODEL),
    ("investment_perfdata", INVEST),
    ("investment_resource", INVEST),
    ("investment_performance_page", INVEST),
    ("book_port_summary", INVEST),
    ("investment_performance", INVEST),
    ("investment_holdings", INVEST),
    ("investment_perfdata_true", INVEST),
    ("investment_trans_resource", INVEST),
    ("investment_perfdata_blank", INVEST),
    ("investment_holdings_csv", INVEST),
    ("investment_trans_csv", INVEST),
    ("account_resource", ACCOUNT),
    ("account_positions", ACCOUNT),
    ("account_pages", ACCOUNT),
]


def _run(
    tmp_path: Path,
    routes: list[tuple[str, int]] = ALL_ROUTES,
    *,
    dry_run: bool = False,
    mutate: Any = None,
) -> tuple[dict[str, Any], RoutesBrowser, Path]:
    clock = FakeClock()
    browser = RoutesBrowser(clock, {TAB: f"{BASE}/app/home"})
    if mutate is not None:
        mutate(browser)
    root = tmp_path / "scratch" / "run"
    manifest = run_routes(
        browser,
        _allowlist(routes),
        root,
        dry_run=dry_run,
        clock=clock,
        download_timeout=10.0,
        settle=0.0,
    )
    return manifest, browser, root


def test_every_route_is_saved_unmodified_with_a_sha256_manifest(tmp_path: Path) -> None:
    manifest, _, root = _run(tmp_path)
    assert manifest["status"] == "COMPLETE"
    assert manifest["routes_not_fetched"] == []
    assert manifest["evidence_eligible"] is True
    assert {r["kind"] for r in manifest["routes"]} == {
        name for name, _ in ALL_ROUTES if name != "account_pages"
    }
    bodies = _bodies()
    for record in manifest["routes"]:
        saved = (root / "capture" / record["relative_path"]).read_bytes()
        assert saved == bodies[record["kind"]][1]
        assert record["sha256"] == hashlib.sha256(saved).hexdigest()
        assert record["size_bytes"] == len(saved)
    names = {r["relative_path"] for r in manifest["routes"]}
    assert f"raw/r2g_{MODEL}_dm_true.json" in names
    assert f"raw/r2g_{MODEL}_perf-full.json" in names
    assert f"raw/strategy_{INVEST}_app_investment_perfdata_init.json" in names
    assert f"raw/strategy_{INVEST}_app_investment_trans.csv" in names
    on_disk = json.loads((root / "capture" / "capture-manifest.json").read_text())
    assert len(on_disk["artifacts"]) == len(manifest["routes"])
    written = (root / "p123-fetch-manifest.json").read_bytes()
    assert hashlib.sha256(written).hexdigest() == manifest["manifest_sha256"]
    assert "InvestmentPerformanceCtrl" not in json.dumps(manifest, default=str)


def test_account_pages_are_visited_not_saved(tmp_path: Path) -> None:
    manifest, browser, _ = _run(tmp_path, [("account_pages", ACCOUNT)])
    assert manifest["status"] == "COMPLETE"
    assert manifest["routes"] == []
    assert len(manifest["pages_visited"]) == 1 + len(fetcher.ACCOUNT_TABS)
    navigated = [
        m["params"]["url"] for m in browser.sent if m["method"] == "Page.navigate"
    ]
    assert f"{BASE}/app/account/details?id={ACCOUNT}&t=liquidated" in navigated


def test_each_fetch_follows_a_visit_to_its_host_page(tmp_path: Path) -> None:
    _, browser, _ = _run(tmp_path)
    page = ""
    for message in browser.sent:
        if message["method"] == "Page.navigate":
            page = message["params"]["url"]
        elif (
            message["method"] == "Runtime.evaluate"
            and message["params"]["expression"] != fetcher.PROBE_EXPRESSION
        ):
            expression = message["params"]["expression"]
            assert "method:'GET'" in expression
            assert page.startswith((BASE + "/app/", BASE + "/port_summary.jsp"))


@pytest.mark.parametrize("dry_run", [False, True])
def test_dry_run_uses_the_same_path_and_is_never_evidence(
    tmp_path: Path, dry_run: bool
) -> None:
    manifest, browser, root = _run(tmp_path, dry_run=dry_run)
    assert manifest["status"] == "COMPLETE"
    assert manifest["dry_run"] is dry_run
    assert manifest["evidence_eligible"] is (not dry_run)
    assert (root / "dry-run-capture").is_dir() is dry_run
    assert (root / "capture").is_dir() is (not dry_run)
    name = "p123-dry-run-manifest.json" if dry_run else "p123-fetch-manifest.json"
    assert (root / name).is_file()
    assert (
        manifest["manifest_sha256"]
        == hashlib.sha256((root / name).read_bytes()).hexdigest()
    )
    assert {m["method"] for m in browser.sent} <= (
        fetcher.ALLOWED_CDP_METHODS | fetcher.INPAGE_CDP_METHODS
    )


def test_dry_run_and_real_run_send_the_same_messages(tmp_path: Path) -> None:
    _, real, _ = _run(tmp_path / "a")
    _, dry, _ = _run(tmp_path / "b", dry_run=True)

    def strip(browser: RoutesBrowser) -> list[tuple[str, str]]:
        return [
            (
                m["method"],
                str(m["params"].get("url") or m["params"].get("expression", "")),
            )
            for m in browser.sent
            if m["method"] in {"Page.navigate", "Runtime.evaluate"}
        ]

    assert strip(real) == strip(dry)


@pytest.mark.parametrize("part", ["releases", "evidence"])
def test_dry_run_refuses_a_release_or_evidence_root(tmp_path: Path, part: str) -> None:
    clock = FakeClock()
    browser = RoutesBrowser(clock, {TAB: f"{BASE}/app/home"})
    root = tmp_path / "scratch" / part / "run"
    with pytest.raises(FetchRefused) as info:
        run_routes(browser, _allowlist(ALL_ROUTES), root, dry_run=True, clock=clock)
    assert info.value.code == "DRY_RUN_ROOT_IN_RELEASE"
    assert browser.sent == []
    assert not root.exists()


def test_dry_run_refuses_a_root_that_is_not_scratch(tmp_path: Path) -> None:
    clock = FakeClock()
    browser = RoutesBrowser(clock, {TAB: f"{BASE}/app/home"})
    root = tmp_path / "elsewhere" / "run"
    # tmp_path itself may sit under a folder named scratch on some machines; force the miss.
    if "scratch" in {p.lower() for p in tmp_path.resolve().parts}:
        pytest.skip("tmp_path is under a scratch folder")
    with pytest.raises(FetchRefused) as info:
        run_routes(browser, _allowlist(ALL_ROUTES), root, dry_run=True, clock=clock)
    assert info.value.code == "DRY_RUN_ROOT_NOT_SCRATCH"
    assert browser.sent == []


@pytest.mark.parametrize(
    ("status", "ctype", "code"),
    [
        (400, "application/json", "STOP_DOWNLOAD_NOT_EXACT"),
        (500, "application/json", "STOP_DOWNLOAD_NOT_EXACT"),
        (401, "application/json", "STOP_REAUTH"),
        (403, "text/html", "STOP_REAUTH"),
        (200, "text/html;charset=utf-8", "STOP_REAUTH"),
        (200, "text/plain", "STOP_SHAPE_UNEXPECTED"),
    ],
)
def test_bad_status_or_content_type_fails_closed(
    tmp_path: Path, status: int, ctype: str, code: str
) -> None:
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["designer_model"], MODEL)

    def mutate(browser: RoutesBrowser) -> None:
        browser.replies[url] = (status, ctype, b"<html>login</html>")

    manifest, _, root = _run(tmp_path, [("designer_model", MODEL)], mutate=mutate)
    assert manifest["status"] == "STOPPED"
    assert manifest["stop"]["code"] == code
    assert manifest["routes"] == []
    assert not (root / "capture" / "raw" / f"r2g_{MODEL}_dm_true.json").exists()
    assert "login" not in json.dumps(manifest, default=str)


@pytest.mark.parametrize(
    ("route", "ident", "body", "ctype"),
    [
        ("designer_model", MODEL, b"[]", "application/json"),
        ("designer_model", MODEL, b'{"portUid": 9}', "application/json"),
        ("designer_model", MODEL, b"not json", "application/json"),
        ("designer_holdings", MODEL, b"", "application/json"),
        ("investment_perfdata", INVEST, b"{}", "application/json"),
        ("investment_holdings_csv", INVEST, b"Wrong,Header\n1,2\n", "text/csv"),
        ("investment_trans_csv", INVEST, b"Date,Type,Unexpected\n1,2,3\n", "text/csv"),
        ("investment_holdings_csv", INVEST, b"Ticker,Name\nA,B\n", "text/csv"),
        ("investment_trans_csv", INVEST, b"Date,Type,\n", "text/html"),
        ("investment_performance_page", INVEST, b"<p>nope</p>", "text/html"),
        (
            "investment_performance_page",
            INVEST,
            b"InvestmentPerformanceCtrl",
            "text/json",
        ),
        ("account_positions", ACCOUNT, b"{}", "application/json"),
    ],
)
def test_unexpected_shapes_fail_closed_and_value_free(
    tmp_path: Path, route: str, ident: int, body: bytes, ctype: str
) -> None:
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME[route], ident)

    def mutate(browser: RoutesBrowser) -> None:
        browser.replies[url] = (200, ctype, body)

    manifest, _, root = _run(tmp_path, [(route, ident)], mutate=mutate)
    assert manifest["status"] == "STOPPED"
    assert manifest["stop"]["code"] in {"STOP_SHAPE_UNEXPECTED", "STOP_REAUTH"}
    assert list((root / "capture" / "raw").iterdir()) == []
    assert body.decode(errors="ignore") not in json.dumps(manifest["stop"]) or not body


def test_a_redirected_request_is_a_reauth_stop(tmp_path: Path) -> None:
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["designer_model"], MODEL)

    def mutate(browser: RoutesBrowser) -> None:
        browser.final_url[url] = f"{BASE}/login"

    manifest, _, _ = _run(tmp_path, [("designer_model", MODEL)], mutate=mutate)
    assert manifest["stop"]["code"] == "STOP_REAUTH"


def test_a_login_page_after_navigation_is_a_reauth_stop(tmp_path: Path) -> None:
    def mutate(browser: RoutesBrowser) -> None:
        browser.land_on = f"{BASE}/app/login?next=1"

    manifest, _, _ = _run(tmp_path, [("designer_model", MODEL)], mutate=mutate)
    assert manifest["stop"]["code"] == "STOP_REAUTH"
    assert manifest["routes"] == []


def test_a_page_that_never_finishes_loading_stops(tmp_path: Path) -> None:
    def mutate(browser: RoutesBrowser) -> None:
        browser.readiness = "loading"

    manifest, _, _ = _run(tmp_path, [("designer_model", MODEL)], mutate=mutate)
    assert manifest["stop"]["code"] == "STOP_REAUTH"


def test_a_failed_route_keeps_earlier_files_and_names_the_rest(tmp_path: Path) -> None:
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["designer_perf_full"], MODEL)

    def mutate(browser: RoutesBrowser) -> None:
        browser.replies[url] = (400, "application/json", b"{}")

    manifest, _, root = _run(
        tmp_path,
        [
            ("designer_model", MODEL),
            ("designer_perf_full", MODEL),
            ("designer_holdings", MODEL),
        ],
        mutate=mutate,
    )
    assert [r["kind"] for r in manifest["routes"]] == ["designer_model"]
    assert manifest["routes_not_fetched"] == [
        f"designer_perf_full:{MODEL}",
        f"designer_holdings:{MODEL}",
    ]
    assert (root / "capture" / "raw" / f"r2g_{MODEL}_dm_true.json").is_file()


def test_output_root_must_be_new(tmp_path: Path) -> None:
    clock = FakeClock()
    browser = RoutesBrowser(clock, {TAB: f"{BASE}/app/home"})
    root = tmp_path / "scratch" / "run"
    root.mkdir(parents=True)
    with pytest.raises(FetchRefused):
        run_routes(browser, _allowlist(ALL_ROUTES), root, dry_run=True, clock=clock)
    assert browser.sent == []


# ---------------------------------------------------------------------------
# the guard


def _guard(expressions: frozenset[str]) -> tuple[GuardedTransport, list[Any]]:
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
        allowed_urls=frozenset(),
        download_path="D",
        inpage_expressions=expressions,
    )
    return guard, sent


def _evaluate(expression: str, **extra: Any) -> dict[str, Any]:
    params = {"expression": expression, "awaitPromise": True, "returnByValue": True}
    return {"id": 1, "method": "Runtime.evaluate", "params": params | extra}


def test_evaluate_is_refused_without_pinned_expressions() -> None:
    guard, sent = _guard(frozenset())
    with pytest.raises(FetchRefused) as info:
        guard.send(_evaluate(fetcher.PROBE_EXPRESSION))
    assert info.value.code == "CDP_METHOD_REFUSED"
    assert sent == []


def test_evaluate_only_sends_an_exact_pinned_expression() -> None:
    url = f"{BASE}/app/investment/resource/{INVEST}"
    pinned = fetcher.fetch_expression(url)
    guard, sent = _guard(frozenset({pinned, fetcher.PROBE_EXPRESSION}))
    guard.send(_evaluate(pinned))
    guard.send(_evaluate(fetcher.PROBE_EXPRESSION))
    assert len(sent) == 2
    for bad in (
        _evaluate(pinned + " "),
        _evaluate(fetcher.fetch_expression(url + "?x=1")),
        _evaluate(pinned, silent=True),
        _evaluate(pinned) | {"params": {"expression": pinned}},
        _evaluate(pinned, awaitPromise=False),
        _evaluate(pinned, returnByValue=False),
    ):
        with pytest.raises(FetchRefused) as info:
            guard.send(bad)
        assert info.value.code == "CDP_EVALUATE_NOT_ALLOWLISTED"
    assert len(sent) == 2
    for method in ("Fetch.enable", "Input.dispatchKeyEvent", "Runtime.callFunctionOn"):
        with pytest.raises(FetchRefused):
            guard.send({"id": 2, "method": method, "params": {}})


def test_the_pinned_expression_is_a_plain_same_origin_get() -> None:
    expression = fetcher.fetch_expression(f"{BASE}/app/account/resource/{ACCOUNT}")
    assert "method:'GET'" in expression
    assert "redirect:'error'" in expression
    assert "body" not in expression
    assert "POST" not in expression


# ---------------------------------------------------------------------------
# allowlist and URL screening


def test_routes_round_trip_and_old_allowlists_keep_their_bytes() -> None:
    plain = fetcher.build_allowlist_bytes([5])
    assert b"routes" not in plain
    parsed = fetcher.parse_allowlist(plain, hashlib.sha256(plain).hexdigest())
    assert parsed.routes == ()
    allow = _allowlist(ALL_ROUTES)
    assert allow.routes == tuple(ALL_ROUTES)


@pytest.mark.parametrize(
    "routes",
    [
        [{"kind": "nope", "id": 1}],
        [{"kind": "designer_model", "id": 0}],
        [{"kind": "designer_model", "id": True}],
        [{"kind": "designer_model", "id": 1}, {"kind": "designer_model", "id": 1}],
        [{"kind": "designer_model"}],
        "designer_model",
    ],
)
def test_bad_route_entries_are_refused(routes: Any) -> None:
    data = json.loads(fetcher.build_allowlist_bytes([5]))
    data["routes"] = routes
    payload = fetcher.canonical_bytes(data)
    with pytest.raises(FetchRefused) as info:
        fetcher.parse_allowlist(payload, hashlib.sha256(payload).hexdigest())
    assert info.value.code == "ALLOWLIST_INVALID"


def test_every_route_url_is_read_only_on_origin_and_unique() -> None:
    allow = _allowlist(ALL_ROUTES)
    pages, fetches = fetcher.route_urls(allow)
    for url in pages | fetches:
        fetcher.screen_route_url(url, pages | fetches)
        assert url.startswith(BASE + "/")
        assert not fetcher._MUTATION_RE.search(url.lower())
    assert len(fetches) == len(ALL_ROUTES) - 1


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.portfolio123.com/app/account/resource/{ACCOUNT}",
        f"https://evil.example/app/account/resource/{ACCOUNT}",
        f"{BASE}/app/account/resource/{ACCOUNT}?x=1",
        f"{BASE}/app/account/resource/{ACCOUNT}#frag",
        f"{BASE}/app/trade/order?id={ACCOUNT}",
    ],
)
def test_other_urls_are_refused_before_sending(url: str) -> None:
    allowed = frozenset({f"{BASE}/app/account/resource/{ACCOUNT}"})
    with pytest.raises(FetchRefused):
        fetcher.screen_route_url(url, allowed)


def test_cli_make_allowlist_accepts_routes(tmp_path: Path) -> None:
    out = tmp_path / "allow.json"
    code = fetcher.main(
        [
            "make-allowlist",
            "--out",
            str(out),
            "--route",
            f"designer_model:{MODEL}",
            "--route",
            f"account_pages:{ACCOUNT}",
        ]
    )
    assert code == 0
    data = json.loads(out.read_text())
    assert data["routes"] == [
        {"id": MODEL, "kind": "designer_model"},
        {"id": ACCOUNT, "kind": "account_pages"},
    ]
    assert (
        fetcher.main(["make-allowlist", "--out", str(tmp_path / "b"), "--route", "x:1"])
        == 2
    )


def test_perfdata_variants_are_both_exact_and_distinct() -> None:
    init1 = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["investment_perfdata"], 7)
    blank = fetcher.route_target_url(
        fetcher.ROUTE_BY_NAME["investment_perfdata_blank"], 7
    )
    assert init1 == f"{BASE}/app/investment/perf-data/7?init=1"
    assert blank == f"{BASE}/app/investment/perf-data/7?init="


def test_page_gets_is_the_same_allowlist_list_under_its_earlier_name() -> None:
    data = json.loads(fetcher.build_allowlist_bytes([], routes=[("designer_model", 5)]))
    data["page_gets"] = data.pop("routes")
    payload = fetcher.canonical_bytes(data)
    parsed = fetcher.parse_allowlist(payload, hashlib.sha256(payload).hexdigest())
    assert parsed.routes == (("designer_model", 5),)
    data["routes"] = data["page_gets"]
    both = fetcher.canonical_bytes(data)
    with pytest.raises(FetchRefused):
        fetcher.parse_allowlist(both, hashlib.sha256(both).hexdigest())


def test_run_dry_run_screens_and_prints_the_plan_without_a_browser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = fetcher.build_allowlist_bytes([], routes=ALL_ROUTES)
    allow = tmp_path / "a.json"
    allow.write_bytes(payload)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:1",
            "--allowlist",
            str(allow),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "never"),
            "--dry-run",
        ]
    )
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["dry_run"] is True
    assert len(plan["routes"]) == len(ALL_ROUTES)
    assert not (tmp_path / "never").exists()


def test_run_refuses_routes_mixed_with_books(tmp_path: Path) -> None:
    payload = fetcher.build_allowlist_bytes([5], routes=[("designer_model", 5)])
    allow = tmp_path / "a.json"
    allow.write_bytes(payload)
    code = fetcher.main(
        [
            "run",
            "--cdp-url",
            "http://127.0.0.1:1",
            "--allowlist",
            str(allow),
            "--allowlist-sha256",
            hashlib.sha256(payload).hexdigest(),
            "--download-dir",
            str(tmp_path / "never"),
            "--no-reference",
            "--as-of",
            "2000-01-03",
        ]
    )
    assert code == fetcher.EXIT_REFUSED


def test_transactions_csv_name_cannot_enter_the_transaction_capture_path() -> None:
    saved = fetcher.ROUTE_BY_NAME["investment_trans_csv"].saved.format(id=7)
    assert "transaction" not in saved.lower()


def _curve_run(tmp_path: Path, start: str | None) -> dict[str, Any]:
    perf = json.loads(json.dumps(SHAPES["designer_perf"]))
    perf["perfData"]["date"] = start
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["designer_perf_full"], MODEL)

    def mutate(browser: RoutesBrowser) -> None:
        browser.replies[url] = (200, "application/json", json.dumps(perf).encode())

    routes = [("designer_model", MODEL), ("designer_perf_full", MODEL)]
    manifest, _, root = _run(tmp_path, routes, mutate=mutate)
    perf_file = root / "capture" / "raw" / f"r2g_{MODEL}_perf-full.json"
    assert perf_file.exists() is (manifest["status"] == "COMPLETE")
    return manifest


def test_the_designer_curve_is_the_full_25_year_route() -> None:
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["designer_perf_full"], 7)
    assert url == f"{BASE}/spr/strategy/model/7/perf?dm=true&init=true&years=25"


def test_a_curve_starting_on_the_model_inception_is_saved(tmp_path: Path) -> None:
    assert _curve_run(tmp_path, START)["status"] == "COMPLETE"


@pytest.mark.parametrize("start", ["2011-01-03", "not a date", None])
def test_a_truncated_or_unreadable_curve_start_fails_closed(
    tmp_path: Path, start: str | None
) -> None:
    manifest = _curve_run(tmp_path, start)
    assert manifest["status"] == "STOPPED"
    assert manifest["stop"]["code"] == "STOP_SHAPE_UNEXPECTED"
    assert "2011" not in json.dumps(manifest, default=str)


def test_a_model_without_a_readable_inception_stops(tmp_path: Path) -> None:
    url = fetcher.route_target_url(fetcher.ROUTE_BY_NAME["designer_model"], MODEL)
    model = dict(SHAPES["designer_model"], portUid=MODEL, inceptionDt="x")

    def mutate(browser: RoutesBrowser) -> None:
        browser.replies[url] = (200, "application/json", json.dumps(model).encode())

    manifest, _, _ = _run(tmp_path, [("designer_model", MODEL)], mutate=mutate)
    assert manifest["stop"]["code"] == "STOP_SHAPE_UNEXPECTED"


def test_a_curve_route_needs_its_model_route_in_the_allowlist() -> None:
    payload = fetcher.build_allowlist_bytes([], routes=[("designer_perf_full", MODEL)])
    with pytest.raises(FetchRefused) as info:
        fetcher.parse_allowlist(payload, hashlib.sha256(payload).hexdigest())
    assert info.value.code == "ALLOWLIST_INVALID"


def test_the_model_is_read_before_its_curve_whatever_the_allowlist_order(
    tmp_path: Path,
) -> None:
    routes = [("designer_perf_full", MODEL), ("designer_model", MODEL)]
    manifest, _, _ = _run(tmp_path, routes)
    assert manifest["status"] == "COMPLETE"
    assert [r["kind"] for r in manifest["routes"]] == [
        "designer_model",
        "designer_perf_full",
    ]


def test_account_investment_routes_cover_the_five_account_reads() -> None:
    expected = {
        "investment_perfdata_true": f"{BASE}/app/investment/perf-data/9?init=true",
        "investment_performance_page": f"{BASE}/app/investment/performance",
        "investment_resource": f"{BASE}/app/investment/resource/9",
        "investment_holdings": f"{BASE}/app/investment/holdings/9",
        "investment_trans_resource": f"{BASE}/app/investment/transResource/9",
    }
    for kind, url in expected.items():
        route = fetcher.ROUTE_BY_NAME[kind]
        assert fetcher.route_target_url(route, 9) == url
        assert "{id}" in route.saved  # the staged name carries the investment id


def test_every_saved_name_carries_the_id() -> None:
    for route in fetcher.ROUTES:
        if route.shape != "visit":
            assert "{id}" in route.saved


def test_designer_fetch_carries_the_page_request_headers() -> None:
    designer = fetcher.fetch_expression(
        "https://www.portfolio123.com/spr/strategy/model/1?dm=true"
    )
    other = fetcher.fetch_expression(
        "https://www.portfolio123.com/app/investment/resource/1"
    )
    assert "angular_req:'1'" in designer
    assert "angular_req" not in other


def test_book_port_summary_route_is_a_named_html_route() -> None:
    route = next(r for r in fetcher.ROUTES if r.name == "book_port_summary")
    assert route.shape == "html"
    assert route.host_page == route.target
    assert "{id}" in route.saved
