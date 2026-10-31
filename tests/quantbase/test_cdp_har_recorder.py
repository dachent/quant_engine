"""Offline proof of the passive Quantbase CDP HAR recorder.

Everything here is synthetic: invented values in the response key sets the HAR
intake and converter tests already use, replayed through a fake CDP transport. No
browser, no network, no credential. The HAR the recorder writes is then fed to the
real intake driver (``stage``/``parse``/``verify``) and the HAR-to-CSV converter.
"""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

import pytest

import capture_quantbase_har as driver
import quantbase_cdp_har_recorder as rec
import quantbase_har_to_csv as converter
from quantbase_cdp_har_recorder import (
    ALLOWED_CDP_METHODS,
    GuardedTransport,
    Listener,
    Recorder,
    RecorderError,
    classify,
    decode_message,
    encode_frame,
    verify_run,
)
from quant_engine.web_contracts import QUANTBASE_CONTRACT

API = "https://qbapi3.com"
APP = "https://app.getquantbase.com"
JWT = "SENTINEL-JWT-VALUE-0123456789abcdef"
COOKIE = "cookie-sentinel-value-never-persisted"
BODY_MARK = "BODYSENTINELTEXT"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _helpers() -> Any:
    spec = importlib.util.spec_from_file_location(
        "qb_converter_test_helpers",
        REPOSITORY_ROOT / "tests/quantbase/test_har_to_csv.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helpers = _helpers()


def lenient(root: Path) -> None:
    """The real policy minus the temp-dir bans (pytest's tmp_path lives in temp)."""

    rec.assert_output_root(root, forbidden_parts=frozenset(), forbidden_roots=())


def make(root: Path, mode: str = "capture", **kwargs: Any) -> Recorder:
    return Recorder(root, mode=mode, root_policy=lenient, **kwargs)


def feed(
    recorder: Recorder,
    url: str,
    body: object | bytes,
    *,
    method: str = "GET",
    status: int = 200,
    ctype: str = "application/json",
    jwt: str | None = JWT,
    calls: list[str] | None = None,
) -> None:
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read() -> bytes:
        if calls is not None:
            calls.append(url)
        return payload

    headers = {"Accept": "application/json", "Cookie": COOKIE}
    if jwt is not None:
        headers["jwt"] = jwt
    recorder.handle(
        method=method,
        url=url,
        status=status,
        content_type=ctype,
        read_body=read,
        request_headers=headers,
        response_headers={"Content-Type": ctype, "Set-Cookie": COOKIE},
        resource_type="XHR",
        wall_time=1_790_000_000.5,
        duration_ms=12.3456,
    )


STATS = f"{API}/api/funds/stats/?end_day=2026-10-07&fund=Volatis%20Strategy&start_day=2026-09-27"
STATS_BODY = {"results": [{"time": "2026-10-02T00:00:00Z", "value": BODY_MARK}]}
DASHBOARD = f"{API}/api/users/123/dashboard_data/"


# --- pure classification ---------------------------------------------------


def _all_reviewed_urls() -> list[tuple[str, str]]:
    return [
        ("users.profile", f"{API}/api/users/?platform=web&u=someone"),
        ("portfolio.dashboard", DASHBOARD),
        ("accounts.linked", f"{API}/api/users/123/accounts/"),
        ("accounts.activity", f"{API}/api/users/123/accounts/activity/"),
        ("funds.statistics", STATS),
        (
            "funds.rebalance-history",
            f"{API}/api/funds/rebalance_history/?title=Volatis%20Strategy",
        ),
        ("partners.status", f"{API}/api/users/123/partners/statis/status/"),
        ("funds.catalog", f"{APP}/_next/data/B1/funds.json"),
        ("funds.index", f"{APP}/_next/data/B1/index.json"),
        ("funds.invest", f"{APP}/_next/data/B1/invest.json"),
        ("funds.projections", f"{APP}/_next/data/B1/projections.json"),
    ]


def test_every_reviewed_route_classifies_as_record_with_its_capability() -> None:
    urls = _all_reviewed_urls()
    assert {c for c, _ in urls} == {e.capability for e in QUANTBASE_CONTRACT.endpoints}
    for capability, url in urls:
        decision = classify("GET", url)
        assert decision.kind == "RECORD", url
        assert decision.capability == capability
        assert decision.authenticated is url.startswith(API)


def test_allowed_origins_are_exactly_the_contract_origins() -> None:
    assert set(rec.ALLOWED_ORIGINS) == {e.origin for e in QUANTBASE_CONTRACT.endpoints}


def test_templates_hide_identifiers_and_build_id() -> None:
    assert classify("GET", DASHBOARD).template == "/api/users/{user_id}/dashboard_data/"
    assert (
        classify("GET", f"{APP}/_next/data/AbC123xyz/funds.json").template
        == "/_next/data/{build_id}/funds.json"
    )
    assert rec.path_template("/api/funds/107/stats/") == "/api/funds/{id}/stats/"
    assert "107" not in classify("GET", f"{API}/api/funds/107/").template


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
def test_every_non_get_method_is_refused(method: str) -> None:
    decision = classify(method, f"{API}/api/users/123/track-event/")
    assert decision.kind == "NON_GET"
    assert classify(method, DASHBOARD).kind == "NON_GET"


@pytest.mark.parametrize(
    "url",
    [
        f"{API}/api/users/123/track-event/",
        f"{API}/api/users/123/withdraw/",
        f"{API}/api/users/123/transfer/",
        f"{API}/api/users/123/deposit/",
        f"{API}/api/users/123/accounts/link/",
        f"{API}/api/users/123/settings/password/",
        f"{API}/api/funds/invest/order/",
        f"{APP}/_next/data/B1/signout.json",
        f"{API}/api/users/123/sign-out/",
        f"{API}/api/users/123/logout/",
    ],
)
def test_mutation_looking_gets_are_refused(url: str) -> None:
    assert classify("GET", url).kind == "MUTATION_LIKE"


@pytest.mark.parametrize(
    "url",
    [
        f"{API}/api/funds/",
        f"{API}/api/users/123/accounts/activity/?page=2",
        f"{APP}/_next/data/B1/funds/107.json",
        f"{API}/api/partners/users/5/",
    ],
)
def test_gets_outside_the_reviewed_contract_are_listed_not_recorded(url: str) -> None:
    assert classify("GET", url).kind == "UNREVIEWED"


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.test/x.js",
        "http://qbapi3.com/api/funds/stats/",
        "https://qbapi3.com.evil.test/api/users/1/accounts/",
        "https://evil.test@qbapi3.com/api/users/1/accounts/",
        "https://qbapi3.com:8443/api/users/1/accounts/",
        "https://sub.qbapi3.com/api/users/1/accounts/",
        "https://app.getquantbase.com.evil.test/_next/data/B/funds.json",
        "not a url",
    ],
)
def test_other_origins_are_dropped(url: str) -> None:
    assert classify("GET", url).kind == "OFFORIGIN"


# --- recorder --------------------------------------------------------------


def test_only_recordable_responses_are_stored_and_body_is_read_only_for_them(
    tmp_path: Path,
) -> None:
    recorder = make(tmp_path / "run")
    calls: list[str] = []
    feed(recorder, STATS, STATS_BODY, calls=calls)
    feed(
        recorder,
        f"{API}/api/users/123/track-event/",
        {"x": 1},
        method="POST",
        calls=calls,
    )
    feed(recorder, f"{API}/api/funds/", {"x": 1}, calls=calls)
    feed(recorder, "https://cdn.example.test/a.js", {"x": 1}, calls=calls)
    feed(recorder, DASHBOARD, {"x": 1}, status=401, calls=calls)
    feed(recorder, DASHBOARD, b"<html>", ctype="text/html", calls=calls)
    feed(recorder, f"{API}/api/users/123/accounts/", {"x": 1}, jwt=None, calls=calls)
    feed(recorder, f"{API}/api/users/123/accounts/", {"x": 1}, jwt="short", calls=calls)
    feed(recorder, f"{API}/api/users/123/accounts/", b"{not json", calls=calls)
    manifest = recorder.finish()
    assert calls == [STATS, f"{API}/api/users/123/accounts/"]
    assert manifest["counts"]["entries"] == 1
    assert manifest["counts"]["offorigin"] == 1
    assert manifest["counts"]["missing_jwt"] == 2
    assert [m["path_template"] for m in manifest["MUTATION_LIKE_NOT_RECORDED"]] == [
        "/api/users/{user_id}/track-event/"
    ]
    assert manifest["UNREVIEWED_ROUTE_NOT_RECORDED"] == [
        {"path_template": "/api/funds/", "query_keys": [], "count": 1}
    ]
    reasons: dict[tuple[str, int], int] = {}
    for item in manifest["OTHER_NOT_RECORDED"]:
        key = (item["reason"], item["status"])
        reasons[key] = reasons.get(key, 0) + item["count"]
    assert reasons == {
        ("NON_2XX", 401): 1,
        ("NOT_JSON", 200): 2,
        ("MISSING_JWT", 200): 2,
    }
    assert [w["code"] for w in manifest["warnings"]] == ["API_RESPONSES_WITHOUT_JWT"]
    assert verify_run(recorder.root)["entries"] == 1


def test_har_is_standards_shaped_and_holds_jwt_but_no_cookie_class_header(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    recorder = make(root)
    feed(recorder, STATS, STATS_BODY)
    recorder.finish()
    har_path = root / "har" / rec.DEFAULT_HAR_NAME
    document = json.loads(har_path.read_bytes())
    log = document["log"]
    assert log["version"] == "1.2"
    assert log["creator"]["name"] == "quantbase_cdp_har_recorder"
    (entry,) = log["entries"]
    assert set(entry) == {
        "startedDateTime",
        "time",
        "request",
        "response",
        "cache",
        "timings",
    }
    assert entry["startedDateTime"].startswith("2026-09-")
    request, response = entry["request"], entry["response"]
    assert request["method"] == "GET" and request["url"] == STATS
    assert {"name": "jwt", "value": JWT} in request["headers"]
    assert {"name": "Accept", "value": "application/json"} in request["headers"]
    assert request["queryString"] == [
        {"name": "end_day", "value": "2026-10-07"},
        {"name": "fund", "value": "Volatis Strategy"},
        {"name": "start_day", "value": "2026-09-27"},
    ]
    assert response["status"] == 200
    assert response["content"]["mimeType"] == "application/json"
    assert json.loads(response["content"]["text"]) == STATS_BODY
    assert response["content"]["size"] == len(response["content"]["text"].encode())
    names = {h["name"].casefold() for h in request["headers"] + response["headers"]}
    assert not names & {"cookie", "set-cookie", "authorization", "proxy-authorization"}
    assert COOKIE.encode() not in har_path.read_bytes()
    assert b"\r\n" not in har_path.read_bytes()


def test_jwt_value_is_only_in_the_har_file(tmp_path: Path) -> None:
    root = tmp_path / "run"
    recorder = make(root)
    feed(recorder, STATS, STATS_BODY)
    recorder.finish()
    holders = [
        p.relative_to(root).as_posix()
        for p in sorted(root.rglob("*"))
        if p.is_file() and JWT.encode() in p.read_bytes()
    ]
    assert holders == [f"har/{rec.DEFAULT_HAR_NAME}"]


def test_manifest_is_hash_bound_and_written_last(tmp_path: Path) -> None:
    root = tmp_path / "run"
    recorder = make(root)
    feed(recorder, STATS, STATS_BODY)
    feed(recorder, DASHBOARD, {"data": []})
    manifest = recorder.finish()
    har_bytes = (root / "har" / rec.DEFAULT_HAR_NAME).read_bytes()
    (artifact,) = manifest["artifacts"]
    assert artifact["sha256"] == hashlib.sha256(har_bytes).hexdigest()
    assert manifest["status"] == "CAPTURE_COMPLETE"
    assert manifest["capabilities"] == {
        "funds.statistics": {"responses": 1, "distinct_requests": 1},
        "portfolio.dashboard": {"responses": 1, "distinct_requests": 1},
    }
    assert {p.name for p in (root / "har").iterdir()} == {rec.DEFAULT_HAR_NAME}
    stored = json.loads((root / "manifest.json").read_bytes())
    assert (
        stored["entry_digests"][0]["body_sha256"]
        == hashlib.sha256(json.dumps(STATS_BODY).encode()).hexdigest()
    )
    assert (root / "manifest.json").stat().st_mtime_ns >= (
        root / "har"
    ).stat().st_mtime_ns - 10**9
    assert verify_run(root)["entries"] == 2


def test_repeated_identical_requests_count_as_one_distinct_request(
    tmp_path: Path,
) -> None:
    recorder = make(tmp_path / "run")
    for _ in range(3):
        feed(recorder, DASHBOARD, {"data": []})
    manifest = recorder.finish()
    assert manifest["capabilities"]["portfolio.dashboard"] == {
        "responses": 3,
        "distinct_requests": 1,
    }


def test_oversize_and_unreadable_bodies_are_flagged_not_recorded(
    tmp_path: Path,
) -> None:
    recorder = make(tmp_path / "run", max_body_bytes=16)
    feed(recorder, DASHBOARD, {"padding": "x" * 100})

    def boom() -> bytes:
        raise RecorderError("NO_BODY")

    recorder.handle(
        method="GET",
        url=STATS,
        status=200,
        content_type="application/json",
        read_body=boom,
        request_headers={"jwt": JWT},
    )
    manifest = recorder.finish()
    assert manifest["status"] == "CAPTURE_WITH_UNRECORDED_BODIES"
    assert manifest["counts"]["oversize_bodies"] == 1
    assert manifest["counts"]["unreadable_bodies"] == 1
    assert manifest["counts"]["entries"] == 0
    assert "NO_ENTRIES_RECORDED" in {w["code"] for w in manifest["warnings"]}


def test_discover_is_value_free_and_keeps_no_header(tmp_path: Path) -> None:
    root = tmp_path / "run"
    recorder = make(root, "discover")
    feed(recorder, STATS, STATS_BODY)
    feed(recorder, f"{API}/api/funds/", {"x": 1})
    manifest = recorder.finish()
    blob = b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())
    for secret in (JWT, COOKIE, BODY_MARK, "Volatis", "2026-09-27"):
        assert secret.encode() not in blob
    (entry,) = json.loads((root / "discovery.json").read_bytes())
    assert entry["capability"] == "funds.statistics"
    assert entry["query_keys"] == ["end_day", "fund", "start_day"]
    assert entry["jwt_header_present"] is True
    assert entry["body_shape"]["results"]["$array"] == {
        "time": "datetime",
        "value": "str",
    }
    assert "har" not in {p.name for p in root.iterdir()}
    assert (
        manifest["UNREVIEWED_ROUTE_NOT_RECORDED"][0]["path_template"] == "/api/funds/"
    )
    assert verify_run(root)["mode"] == "discover"


def test_files_are_create_only_and_names_are_validated(tmp_path: Path) -> None:
    root = tmp_path / "run"
    recorder = make(root)
    feed(recorder, STATS, STATS_BODY)
    recorder.finish()
    with pytest.raises(RecorderError, match="ARTIFACT_EXISTS"):
        recorder.finish()
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_EXISTS"):
        make(root)
    for bad in ("x.json", "../x.har", "a b.har", ".hidden.har", "x" * 200 + ".har"):
        with pytest.raises(RecorderError, match="HAR_NAME_INVALID"):
            make(tmp_path / "other", har_name=bad)
    with pytest.raises(RecorderError, match="MODE_INVALID"):
        make(tmp_path / "other", "replay")


def test_output_root_policy(tmp_path: Path) -> None:
    strict = rec.assert_output_root
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_NOT_ABSOLUTE"):
        strict(Path("relative-run"))
    existing = tmp_path / "exists"
    existing.mkdir()
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_EXISTS"):
        strict(existing, forbidden_roots=())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_REPOSITORY"):
        strict(repo / "evidence" / "run", forbidden_roots=())
    for part in ("scratch", "tmp", "Temp"):
        with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_SCRATCH_OR_TEMP"):
            strict(tmp_path / part / "run", forbidden_roots=())
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_FORBIDDEN_LOCATION"):
        strict(tmp_path / "run", forbidden_roots=(tmp_path,))
    # the defaults forbid the repository checkout and the system temp directory
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_(REPOSITORY|FORBIDDEN)"):
        strict(REPOSITORY_ROOT / "evidence" / "run")
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_FORBIDDEN_LOCATION"):
        strict(Path(tempfile.gettempdir()) / "evidence" / "run")
    # a governed root away from repo, scratch and temp is accepted
    governed = tmp_path / "V Capital" / "evidence" / "quantbase" / "run"
    strict(governed, forbidden_parts=frozenset(), forbidden_roots=())


# --- verify refusals -------------------------------------------------------


def _finished(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    recorder = make(root)
    feed(recorder, STATS, STATS_BODY)
    feed(recorder, DASHBOARD, {"data": [1]})
    recorder.finish()
    return root


def _rebind(root: Path, document: dict[str, Any]) -> None:
    """Rewrite the HAR and re-bind its hash so only the structure check can catch it."""

    path = root / "har" / rec.DEFAULT_HAR_NAME
    payload = json.dumps(document, separators=(",", ":")).encode()
    path.write_bytes(payload)
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["artifacts"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    (root / "manifest.json").write_bytes(rec._canonical(manifest))


def _code(root: Path) -> str:
    with pytest.raises(RecorderError) as exc:
        verify_run(root)
    return exc.value.code


def _har(root: Path) -> dict[str, Any]:
    loaded = json.loads((root / "har" / rec.DEFAULT_HAR_NAME).read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def test_verify_accepts_a_clean_run_and_prints_only_aggregates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _finished(tmp_path)
    assert rec.main(["verify", "--run-root", str(root)]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["entries"] == 2
    for secret in (JWT, COOKIE, BODY_MARK, "Volatis"):
        assert secret not in out.out + out.err


def test_verify_detects_tampering_and_strays(tmp_path: Path) -> None:
    root = _finished(tmp_path)
    har = root / "har" / rec.DEFAULT_HAR_NAME
    original = har.read_bytes()
    har.write_bytes(original + b" ")
    assert _code(root) == "ARTIFACT_HASH_DIFFERS"
    har.write_bytes(original)
    (root / "extra.txt").write_bytes(b"x")
    assert _code(root) == "RUN_DIRECTORY_DIFFERS_FROM_MANIFEST"
    (root / "extra.txt").unlink()
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["safety"]["reads_cookies"] = True
    (root / "manifest.json").write_bytes(rec._canonical(manifest))
    assert _code(root) == "MANIFEST_SAFETY_BLOCK_DIFFERS"


def test_verify_refuses_a_recorded_post_offorigin_unreviewed_or_mutation_entry(
    tmp_path: Path,
) -> None:
    cases = {
        "NON_GET_RECORDED": lambda e: e["request"].update(method="POST"),
        "OFFORIGIN_RECORDED": lambda e: e["request"].update(url="https://x.test/api/a"),
        "MUTATION_LIKE_RECORDED": lambda e: e["request"].update(
            url=f"{API}/api/users/123/track-event/"
        ),
        "UNREVIEWED_ROUTE_RECORDED": lambda e: e["request"].update(
            url=f"{API}/api/funds/"
        ),
        "NON_2XX_RECORDED": lambda e: e["response"].update(status=404),
        "COOKIE_CLASS_HEADER_RECORDED": lambda e: e["request"]["headers"].append(
            {"name": "Cookie", "value": "x"}
        ),
        "JWT_HEADER_MISSING": lambda e: e["request"].update(
            headers=[h for h in e["request"]["headers"] if h["name"] != "jwt"]
        ),
        "BODY_NOT_JSON": lambda e: e["response"]["content"].update(text="{nope"),
        "BODY_TEXT_MISSING": lambda e: e["response"]["content"].update(text=""),
    }
    for expected, mutate in cases.items():
        root = _finished(tmp_path / expected)
        document = _har(root)
        mutate(document["log"]["entries"][0])
        _rebind(root, document)
        assert _code(root) == expected, expected


def test_verify_refuses_count_hash_and_structure_drift(tmp_path: Path) -> None:
    root = _finished(tmp_path / "a")
    document = _har(root)
    document["log"]["entries"].pop()
    _rebind(root, document)
    assert _code(root) == "ENTRY_COUNT_DIFFERS"
    root = _finished(tmp_path / "b")
    document = _har(root)
    document["log"]["entries"][0]["response"]["content"]["text"] = json.dumps({"a": 1})
    _rebind(root, document)
    assert _code(root) == "ENTRY_HASH_DIFFERS"
    root = _finished(tmp_path / "c")
    document = _har(root)
    document["log"]["version"] = "1.1"
    _rebind(root, document)
    assert _code(root) == "HAR_STRUCTURE"
    root = _finished(tmp_path / "d")
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["origins"].append("https://evil.test")
    (root / "manifest.json").write_bytes(rec._canonical(manifest))
    assert _code(root) == "MANIFEST_ORIGINS_DIFFER"


# --- CDP: allow-list, framing, passive listener ----------------------------


class FakeTransport:
    """Scripted browser. Replies to the allowed calls; raises on header reads."""

    def __init__(self, bodies: dict[str, bytes] | None = None) -> None:
        self.inbound: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []
        self.bodies = bodies or {}
        self.targets: list[dict[str, Any]] = []
        self.closed = False

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        mid, method = message["id"], message["method"]
        reply: dict[str, Any] = {"id": mid, "result": {}}
        if method == "Target.setDiscoverTargets":
            self.inbound.extend(
                {"method": "Target.targetCreated", "params": {"targetInfo": t}}
                for t in self.targets
            )
        elif method == "Target.attachToTarget":
            reply["result"] = {"sessionId": "S-" + message["params"]["targetId"]}
        elif method == "Network.getResponseBody":
            raw = self.bodies.get(message["params"]["requestId"])
            if raw is None:
                reply = {"id": mid, "error": {"code": -32000, "message": "evicted"}}
            else:
                reply["result"] = {
                    "body": base64.b64encode(raw).decode(),
                    "base64Encoded": True,
                }
        self.inbound.appendleft(reply)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self.inbound:
            return self.inbound.popleft()
        raise RecorderError("CDP_CLOSED")

    def close(self) -> None:
        self.closed = True


class Trip(dict):  # type: ignore[type-arg]
    """Any read of its contents fails the test (headers of unrecorded traffic)."""

    def _boom(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("headers of an unrecorded request were read")

    __getitem__ = get = items = keys = values = __iter__ = __len__ = _boom  # type: ignore[assignment]


def net(sess: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"method": method, "params": params, "sessionId": sess}


def request_events(
    sess: str,
    rid: str,
    method: str,
    url: str,
    status: int,
    mime: str,
    *,
    recorded: bool,
    ts: float = 100.0,
) -> list[dict[str, Any]]:
    req_headers: Any = (
        {"Accept": "application/json", "jwt": JWT} if recorded else Trip()
    )
    resp_headers: Any = {"content-type": mime} if recorded else Trip()
    return [
        net(
            sess,
            "Network.requestWillBeSent",
            {
                "requestId": rid,
                "request": {"method": method, "url": url, "headers": req_headers},
                "wallTime": 1_790_000_000.0,
                "timestamp": ts,
            },
        ),
        net(sess, "Network.requestWillBeSentExtraInfo", Trip(headers=COOKIE)),  # type: ignore[arg-type]
        net(
            sess,
            "Network.responseReceived",
            {
                "requestId": rid,
                "response": {
                    "status": status,
                    "mimeType": mime,
                    "headers": resp_headers,
                    "url": url,
                },
                "type": "XHR",
            },
        ),
        net(sess, "Network.responseReceivedExtraInfo", Trip(headers=COOKIE)),  # type: ignore[arg-type]
        net(
            sess, "Network.loadingFinished", {"requestId": rid, "timestamp": ts + 0.25}
        ),
    ]


def run_listener(
    recorder: Recorder, transport: FakeTransport, events: list[dict[str, Any]]
) -> None:
    original = transport.send

    def after_attach(message: dict[str, Any]) -> None:
        original(message)
        if message["method"] == "Network.enable":
            transport.inbound.extend(events)
            events.clear()

    transport.send = after_attach  # type: ignore[method-assign]
    Listener(transport, recorder, max_seconds=0).run()


def test_guard_refuses_every_message_that_could_act_on_the_browser() -> None:
    inner = FakeTransport()
    guard = GuardedTransport(inner)
    for method in (
        "Page.navigate",
        "Page.reload",
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Fetch.enable",
        "Fetch.continueRequest",
        "Network.getCookies",
        "Network.getAllCookies",
        "Network.setCookie",
        "Network.setExtraHTTPHeaders",
        "Network.setCacheDisabled",
        "Network.loadNetworkResource",
        "Page.addScriptToEvaluateOnNewDocument",
        "Storage.getCookies",
        "DOM.setOuterHTML",
        "Target.createTarget",
        "Target.closeTarget",
        "Browser.close",
    ):
        with pytest.raises(RecorderError, match="CDP_METHOD_REFUSED"):
            guard.send({"id": 1, "method": method, "params": {}})
    assert inner.sent == []
    for method in sorted(ALLOWED_CDP_METHODS):
        guard.send(
            {"id": 1, "method": method, "params": {"requestId": "r", "targetId": "t"}}
        )
    assert len(inner.sent) == len(ALLOWED_CDP_METHODS)
    assert ALLOWED_CDP_METHODS == {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Network.enable",
        "Network.getResponseBody",
    }


def test_tool_source_never_names_an_acting_cdp_method_or_extra_info() -> None:
    source = Path(rec.__file__).read_text("utf-8")
    for name in (
        "Page.navigate",
        "Runtime.evaluate",
        "Input.dispatch",
        "Fetch.enable",
        "Network.getCookies",
        "Network.getAllCookies",
        "Network.setCookie",
        "Storage.getCookies",
        'ExtraInfo"',
        "postData",
        "subprocess",
        "os.system",
    ):
        assert name not in source


def test_frame_roundtrip_and_fragmentation() -> None:
    key = b"\x01\x02\x03\x04"
    for size in (0, 5, 125, 126, 70000):
        payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
        assert decode_message(io.BytesIO(encode_frame(1, payload, key)).read) == (
            1,
            payload,
        )
    assert decode_message(io.BytesIO(b"\x01\x03abc\x80\x02de").read) == (1, b"abcde")
    assert decode_message(io.BytesIO(b"\x89\x02hi").read) == (9, b"hi")


def test_cdp_url_must_be_loopback_http() -> None:
    assert rec.parse_cdp_url("http://127.0.0.1:9333") == ("127.0.0.1", 9333)
    for bad in (
        "http://example.com:9333",
        "https://127.0.0.1:9333",
        "http://127.0.0.1",
        "ws://127.0.0.1:9333",
        "http://192.168.1.5:9333",
    ):
        with pytest.raises(RecorderError, match="CDP_URL_NOT_LOOPBACK_HTTP"):
            rec.parse_cdp_url(bad)


def test_listener_is_passive_and_reads_headers_only_of_recorded_requests(
    tmp_path: Path,
) -> None:
    stats = json.dumps(STATS_BODY).encode()
    transport = FakeTransport({"r1": stats, "r2": b"{}"})
    transport.targets = [
        {"targetId": "T1", "type": "page", "url": f"{APP}/dashboard"},
        {"targetId": "T2", "type": "page", "url": "https://mail.example/inbox"},
        {"targetId": "T3", "type": "service_worker", "url": f"{APP}/sw.js"},
    ]
    events: list[dict[str, Any]] = []
    for args in (
        ("r1", "GET", STATS, 200, "application/json", True),
        ("r2", "GET", DASHBOARD, 200, "application/json", True),
        (
            "r3",
            "POST",
            f"{API}/api/users/123/track-event/",
            200,
            "application/json",
            False,
        ),
        ("r4", "OPTIONS", DASHBOARD, 204, "text/plain", False),
        ("r5", "GET", f"{API}/api/funds/", 200, "application/json", False),
        ("r6", "GET", "https://cdn.example/x.js", 200, "text/javascript", False),
        ("r7", "GET", f"{APP}/_next/static/a.js", 200, "text/javascript", False),
    ):
        events += request_events(
            "S-T1", args[0], args[1], args[2], args[3], args[4], recorded=args[5]
        )
    events += request_events(
        "S-T2", "r8", "GET", STATS, 200, "application/json", recorded=False
    )
    events.append(
        net(
            "S-T1",
            "Network.requestWillBeSent",
            {
                "requestId": "r9",
                "request": {"method": "GET", "url": f"{APP}/x", "headers": Trip()},
            },
        )
    )
    events.append(net("S-T1", "Network.loadingFailed", {"requestId": "r9"}))
    root = tmp_path / "run"
    recorder = make(root)
    run_listener(recorder, transport, events)
    manifest = recorder.finish()
    assert {m["method"] for m in transport.sent} <= ALLOWED_CDP_METHODS
    attaches = [m for m in transport.sent if m["method"] == "Target.attachToTarget"]
    assert [m["params"]["targetId"] for m in attaches] == ["T1"]
    wanted = [
        m["params"]["requestId"]
        for m in transport.sent
        if m["method"] == "Network.getResponseBody"
    ]
    assert wanted == ["r1", "r2"]
    assert manifest["browser_closed"] is True
    assert manifest["counts"]["entries"] == 2
    assert manifest["counts"]["offorigin"] == 1
    assert manifest["counts"]["aborted"] == 1
    assert sorted(m["method"] for m in manifest["MUTATION_LIKE_NOT_RECORDED"]) == [
        "OPTIONS",
        "POST",
    ]
    document = json.loads((root / "har" / rec.DEFAULT_HAR_NAME).read_bytes())
    first = document["log"]["entries"][0]
    assert {"name": "jwt", "value": JWT} in first["request"]["headers"]
    assert first["time"] == 250.0
    assert first["startedDateTime"].startswith("2026-09-")
    blob = b"".join(
        p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != rec.DEFAULT_HAR_NAME
    )
    assert JWT.encode() not in blob and COOKIE.encode() not in blob
    assert b"mail.example" not in blob and b"cdn.example" not in blob
    assert verify_run(root)["entries"] == 2


def test_listener_attaches_when_a_tab_navigates_to_quantbase(tmp_path: Path) -> None:
    transport = FakeTransport({})
    transport.targets = [{"targetId": "T9", "type": "page", "url": "about:blank"}]
    original = transport.send

    def hook(message: dict[str, Any]) -> None:
        original(message)
        if message["method"] == "Target.setDiscoverTargets":
            transport.inbound.append(
                {
                    "method": "Target.targetInfoChanged",
                    "params": {
                        "targetInfo": {
                            "targetId": "T9",
                            "type": "page",
                            "url": f"{APP}/dashboard",
                        }
                    },
                }
            )

    transport.send = hook  # type: ignore[method-assign]
    Listener(transport, make(tmp_path / "run", "discover")).run()
    assert [m["method"] for m in transport.sent] == [
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Network.enable",
    ]


def test_cdp_call_failure_does_not_attach_and_does_not_crash(tmp_path: Path) -> None:
    class Failing(FakeTransport):
        def send(self, message: dict[str, Any]) -> None:
            super().send(message)
            if message["method"] == "Target.attachToTarget":
                self.inbound[0] = {"id": message["id"], "error": {"message": "gone"}}

    transport = Failing({})
    transport.targets = [{"targetId": "T1", "type": "page", "url": f"{APP}/x"}]
    Listener(transport, make(tmp_path / "run", "discover")).run()
    assert not any(m["method"] == "Network.enable" for m in transport.sent)


def test_redirected_request_is_not_recorded(tmp_path: Path) -> None:
    transport = FakeTransport({})
    transport.targets = [{"targetId": "T1", "type": "page", "url": f"{APP}/x"}]
    first = request_events(
        "S-T1", "r1", "GET", DASHBOARD, 200, "application/json", recorded=True
    )[:1]
    again = net(
        "S-T1",
        "Network.requestWillBeSent",
        {
            "requestId": "r1",
            "request": {"method": "GET", "url": STATS, "headers": {"jwt": JWT}},
            "redirectResponse": {"status": 302},
        },
    )
    recorder = make(tmp_path / "run")
    run_listener(recorder, transport, [*first, again])
    manifest = recorder.finish()
    assert manifest["counts"]["entries"] == 0
    assert manifest["OTHER_NOT_RECORDED"] == [
        {
            "reason": "NON_2XX",
            "path_template": "/api/users/{user_id}/dashboard_data/",
            "status": 302,
            "count": 1,
        }
    ]


# --- zero-entries warning --------------------------------------------------


class _IdleTransport(FakeTransport):
    def __init__(self, idle_polls: int) -> None:
        super().__init__({})
        self.idle = idle_polls

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self.inbound:
            return self.inbound.popleft()
        if self.idle > 0:
            self.idle -= 1
            return None
        raise RecorderError("CDP_CLOSED")


def _ticking() -> Any:
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 10.0
        return state["t"]

    return clock


def test_zero_entries_warns_once_after_n_seconds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = make(tmp_path / "run", "discover")
    Listener(_IdleTransport(20), recorder, warn_after=30.0, clock=_ticking()).run()
    manifest = recorder.finish()
    assert [w["code"] for w in manifest["warnings"]] == ["NO_ENTRIES_RECORDED"]
    assert "after 30 s" in manifest["warnings"][0]["text"]
    assert "start it FIRST" in manifest["warnings"][0]["text"]
    assert capsys.readouterr().err.count("WARNING NO_ENTRIES_RECORDED") == 1
    assert verify_run(tmp_path / "run")["status"] == "DISCOVERY"


def test_no_warning_when_something_was_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = make(tmp_path / "run", "discover")
    feed(recorder, DASHBOARD, {"data": []})
    Listener(_IdleTransport(20), recorder, warn_after=30.0, clock=_ticking()).run()
    assert recorder.finish()["warnings"] == []
    assert capsys.readouterr().err == ""


def test_empty_run_always_records_a_warning_even_if_disabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = make(tmp_path / "run")
    Listener(_IdleTransport(3), recorder, warn_after=0, clock=_ticking()).run()
    manifest = recorder.finish()
    assert [w["code"] for w in manifest["warnings"]] == ["NO_ENTRIES_RECORDED"]
    assert "WARNING NO_ENTRIES_RECORDED" in capsys.readouterr().err


def test_cli_defaults_and_summary_carry_no_header_or_body(tmp_path: Path) -> None:
    args = rec._parser().parse_args(
        ["capture", "--cdp-url", "http://127.0.0.1:9333", "--output-root", "C:/x"]
    )
    assert args.warn_after_seconds == 30.0 and args.har_name == rec.DEFAULT_HAR_NAME
    recorder = make(tmp_path / "run")
    feed(recorder, STATS, STATS_BODY)
    feed(recorder, f"{API}/api/funds/?x=1", {"x": 1})
    text = json.dumps(rec._summary(recorder.finish()))
    for secret in (JWT, COOKIE, BODY_MARK, "Volatis", "someone"):
        assert secret not in text
    assert '"/api/funds/"' in text and '"x"' in text


# --- proof against the real driver and converter ---------------------------


def _replay_session(
    root: Path,
) -> tuple[Recorder, dict[str, Any], list[dict[str, Any]]]:
    """Replay the same synthetic session the driver tests use, through fake CDP."""

    bodies = helpers._bodies()
    entries = helpers._entries(bodies, set())
    transport = FakeTransport()
    transport.targets = [{"targetId": "T1", "type": "page", "url": f"{APP}/dashboard"}]
    events: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        request, response = entry["request"], entry["response"]
        method, url = request["method"], request["url"]
        rid = f"q{index}"
        text = response["content"]["text"]
        transport.bodies[rid] = text.encode()
        reviewed = classify(method, url).kind == "RECORD"
        mime = (
            "application/json"
            if url.startswith((API, f"{APP}/_next/data"))
            else "text/javascript"
        )
        events += request_events(
            "S-T1", rid, method, url, response["status"], mime, recorded=reviewed
        )
    recorder = make(root)
    run_listener(recorder, transport, events)
    return recorder, bodies, entries


def _driver_manifest(bodies: dict[str, Any]) -> dict[str, Any]:
    manifest: dict[str, Any] = helpers._manifest(bodies, {}, set())
    manifest["capture_files"] = [{"name": rec.DEFAULT_HAR_NAME}]
    manifest["ignored_origins"] = []
    return manifest


def test_recorder_har_is_accepted_by_the_real_driver_and_converter(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recorded"
    recorder, bodies, entries = _replay_session(root)
    manifest = recorder.finish()
    # noise (OPTIONS, track-event POST, static asset, off-origin) never reached the HAR
    assert manifest["counts"]["entries"] == 8
    assert manifest["counts"]["offorigin"] == 1
    assert verify_run(root)["entries"] == 8
    capture = root / "har"
    assert {p.name for p in capture.iterdir()} == {rec.DEFAULT_HAR_NAME}

    manifest_path = tmp_path / "intake-manifest.json"
    manifest_path.write_bytes(json.dumps(_driver_manifest(bodies)).encode())
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = driver.stage_intake(manifest_path, capture, staging)
    intake_dir = Path(str(staged["intake_dir"]))
    parsed = driver.parse_intake(intake_dir)
    assert "RAW_HAR_HOLDS_SESSION_MATERIAL" in parsed["flags"]
    verified = driver.verify_intake(intake_dir)
    assert verified["state"] == "VERIFIED_WITH_FLAGS"
    report = json.loads((intake_dir / "parse-report.json").read_bytes())
    assert report["disposed_entries"] == 0
    assert report["entries_total"] == 8
    assert report["extracted_responses"] == 8
    for item in manifest["capabilities"]:
        assert (
            report["capabilities"][item]["responses"]
            == manifest["capabilities"][item]["responses"]
        )
    for path in intake_dir.rglob("*"):
        if path.is_file() and path.parent.name != "raw":
            assert JWT.encode() not in path.read_bytes()

    out = tmp_path / "csv"
    out.mkdir()
    converted = converter.convert(intake_dir, out, "public")
    assert converted["state"] == "CONVERTED"
    assert converter.verify_conversion(intake_dir, out, "public")["state"] == "VERIFIED"


def test_the_capture_directory_is_a_valid_stage_input_with_exact_file_names(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recorded"
    recorder, bodies, _ = _replay_session(root)
    recorder.finish()
    manifest = _driver_manifest(bodies)
    manifest["capture_files"] = [{"name": "other-name.har"}]
    path = tmp_path / "m.json"
    path.write_bytes(json.dumps(manifest).encode())
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(
        driver.QuantbaseHarIntakeError, match="differs from the manifest"
    ):
        driver.stage_intake(path, root / "har", staging)


def test_a_recorded_har_with_an_unreviewed_route_would_fail_the_driver_so_none_is_recorded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recorded"
    recorder, bodies, _ = _replay_session(root)
    feed(recorder, f"{APP}/_next/data/B1/funds/107.json", {"pageProps": {}})
    feed(recorder, f"{API}/api/users/123/accounts/activity/?page=2", {"data": []})
    manifest = recorder.finish()
    listed = {
        m["path_template"]: m["query_keys"]
        for m in manifest["UNREVIEWED_ROUTE_NOT_RECORDED"]
        if m["path_template"] != "/_next/static/chunks/app.js"
    }
    assert listed == {
        "/_next/data/{build_id}/funds/{id}.json": [],
        "/api/users/{user_id}/accounts/activity/": ["page"],
    }
    manifest_path = tmp_path / "m.json"
    manifest_path.write_bytes(json.dumps(_driver_manifest(bodies)).encode())
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = driver.stage_intake(manifest_path, root / "har", staging)
    assert driver.parse_intake(Path(str(staged["intake_dir"])))["state"]
