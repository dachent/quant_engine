"""Offline proof of the passive Portfolio123 CDP recorder.

Everything here is synthetic: invented values in the field structure and route
templates already known from preserved evidence (designer-model route,
transactions list, menu links, the copy-to-screen mutation link). No browser, no
network: a fake CDP transport replays scripted events.
"""

from __future__ import annotations

import base64
from collections import deque
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.portfolio123 import catalog_rows
from tools import p123_cdp_recorder as rec
from tools.p123_cdp_recorder import (
    ALLOWED_CDP_METHODS,
    GuardedTransport,
    Listener,
    Recorder,
    RecorderError,
    build_proposal,
    classify,
    decode_message,
    encode_frame,
    merge_shapes,
    path_template,
    propose_catalog,
    shape_of,
    verify_run,
)

BASE = "https://www.portfolio123.com"
SECRET_ID = "7777771"  # an invented identifier that must never be persisted
SECRET_ONE = "cookie-sentinel-value-never-persisted"
SECRET_TWO = "bearer-sentinel-value-never-persisted"
SECRET_TEXT = "SENTINELSYMBOL"
DESIGNER_URL = f"{BASE}/spr/strategy/model/1764562?dm=true"
TRANSACTIONS = f"{BASE}/transact_list.jsp?portid={SECRET_ID}&offset="
REVERT_URL = f"{BASE}/app/screen/summary/5/revert?PortSimId=11&PortSimRuleType=1"


def _synthetic(described: Any) -> Any:
    """A value for a described catalog spec (invented, type-correct)."""

    if isinstance(described, str):
        base = described.rstrip("?")
        return {
            "int": 1,
            "num": 1.5,
            "bool": True,
            "null": None,
            "date": "2000-01-31",
            "us_date": "01/31/2000",
        }.get(base, "x")
    if "seq" in described:
        return [_synthetic(described["seq"])]
    if "choice" in described:
        return described["choice"][0]
    return {
        key: _synthetic(value)
        for key, value in {**described["required"], **described["optional"]}.items()
    }


def designer_body() -> bytes:
    row = next(r for r in catalog_rows() if r["capability"] == "designer-model.json")
    return json.dumps(_synthetic(row["shape"])).encode()


def transactions_html(rows: int = 50) -> bytes:
    head = "".join(
        f"<th>{name}</th>"
        for name in (
            "Date",
            "Type",
            "Ticker",
            "Shares",
            "Price",
            "Amount",
            "Tot Fees",
            "Rank",
            "Note",
            "TRADE",
        )
    )
    body = "".join(
        f"<tr><td>01/31/2000</td><td>{SECRET_TEXT}</td><td>{SECRET_TEXT}</td></tr>"
        for _ in range(rows)
    )
    return f"<html><table><tr>{head}</tr>{body}</table></html>".encode()


def feed(
    recorder: Recorder,
    method: str,
    url: str,
    body: bytes,
    *,
    status: int = 200,
    ctype: str = "application/json",
    calls: list[str] | None = None,
) -> None:
    def read() -> bytes:
        if calls is not None:
            calls.append(url)
        return body

    recorder.handle(
        method=method,
        url=url,
        status=status,
        content_type=ctype,
        read_body=read,
        resource_type="XHR",
    )


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    return tmp_path / "run"


def all_run_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


# --- pure classification ---------------------------------------------------


def test_path_templates_replace_identifiers_and_drop_path_parameters() -> None:
    assert path_template("/spr/strategy/model/1764562") == "/spr/strategy/model/{id}"
    assert (
        path_template("/app/investment/perf-data/3") == "/app/investment/perf-data/{id}"
    )
    assert (
        path_template("/transact_list.jsp;" + "js" + "essionid" + "=ABC123")
        == "/transact_list.jsp"
    )
    assert path_template("/a/123e4567-e89b-12d3-a456-426614174000/b") == "/a/{id}/b"
    assert path_template("/x/model1764562/y") == "/x/model{id}/y"
    assert path_template("/sv/opener/AIFACTOR/3") == "/sv/opener/AIFACTOR/{id}"
    assert path_template("/app/opener/SCR") == "/app/opener/SCR"


def test_known_routes_classify_as_expected() -> None:
    designer = classify("GET", DESIGNER_URL)
    assert (designer.kind, designer.template) == ("RECORD", "/spr/strategy/model/{id}")
    assert designer.query_keys == ("dm",)
    assert designer.query_flags == (("dm", "true"),)
    listing = classify("GET", TRANSACTIONS + "100")
    assert listing.kind == "RECORD"
    assert listing.query_keys == ("offset", "portid")
    assert listing.pagination == (("offset", 100),)
    # the identifier value is never part of a decision
    assert SECRET_ID not in repr(listing)
    assert classify("GET", f"{BASE}/app/investment/perf-data/3?init=").kind == "RECORD"
    assert classify("GET", f"{BASE}/app/opener/SCR").kind == "RECORD"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.portfolio123.com/x",
        "https://portfolio123.com/x",
        "https://api.portfolio123.com/x",
        "https://www.portfolio123.com:8443/x",
        "https://www.portfolio123.com@evil.example/x",
        "https://evil.example/www.portfolio123.com",
        "https://www.portfolio123.com.evil.example/x",
        "data:text/html,x",
        "not a url",
    ],
)
def test_other_origins_are_dropped_and_counted_not_listed(url: str) -> None:
    assert classify("GET", url).kind == "OFFORIGIN"


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
)
def test_every_non_get_method_is_refused(method: str) -> None:
    decision = classify(method, f"{BASE}/port_simul6.jsp")
    assert decision.kind == "NON_GET"
    assert decision.template == "/port_simul6.jsp"


@pytest.mark.parametrize(
    "url",
    [
        REVERT_URL,
        f"{BASE}/a/delete/3",
        f"{BASE}/a/b?action=Remove",
        f"{BASE}/a/save",
        f"{BASE}/a/submit",
        f"{BASE}/a/UPDATE",
        f"{BASE}/a/create?x=1",
        f"{BASE}/a/rebalance",
        f"{BASE}/a/trade",
        f"{BASE}/a/order",
        f"{BASE}/a/subscribe",
        f"{BASE}/a/cancel",
        f"{BASE}/logout",
        f"{BASE}/a/b?password=1",
        f"{BASE}/a/b?token=1",
        f"{BASE}/a/b?x=%72evert",
    ],
)
def test_mutation_looking_gets_are_refused(url: str) -> None:
    decision = classify("GET", url)
    assert decision.kind == "MUTATION_LIKE"
    assert "?" not in decision.template


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/sv/account-settings/billing",
        f"{BASE}/app/account/positions/3",
        f"{BASE}/profile/view",
        f"{BASE}/a/invoice/3",
        f"{BASE}/a/payment",
        f"{BASE}/spr/user",
    ],
)
def test_billing_and_identity_pages_need_an_explicit_flag(url: str) -> None:
    assert classify("GET", url).kind == "IDENTITY"
    words = classify("GET", url).words
    assert classify("GET", url, frozenset(words)).kind == "RECORD"
    assert classify("GET", url, frozenset({"user"} - set(words))).kind in {
        "IDENTITY",
        "RECORD",
    }


# --- shapes carry names and types, never values ----------------------------


def test_shape_of_has_names_and_types_only() -> None:
    value = {
        "name": "Alice Secretname",
        "n": 3,
        "x": 1.5,
        "ok": True,
        "none": None,
        "d": "2000-01-31",
        "u": "01/31/2000",
        "rows": [{"a": 1}, {"a": 2, "b": "s"}],
    }
    shape = shape_of(value)
    assert shape["name"] == "str" and shape["d"] == "date" and shape["u"] == "us_date"
    assert shape["rows"]["$array"] == {"a": "int", "b": "str", "$optional": ["b"]}
    assert shape["rows"]["$len_min"] == shape["rows"]["$len_max"] == 2
    assert "Secretname" not in json.dumps(shape)


def test_objects_keyed_by_data_collapse() -> None:
    keyed = {f"{n:04d}": {"v": n} for n in range(5)}
    shape = shape_of(keyed)
    assert shape == {"$keyed": {"v": "int"}, "$keys": 5}
    many = {f"k{n}": 1 for n in range(100)}
    assert "$keyed" in shape_of(many)


def test_merge_shapes_unions_types_and_optional_keys() -> None:
    merged = merge_shapes(shape_of({"a": 1, "b": None}), shape_of({"a": 2.5, "c": "x"}))
    assert merged == {"a": "num", "b": "null", "c": "str", "$optional": ["b", "c"]}
    assert merge_shapes("str", "null") == "null|str"
    assert merge_shapes(None, "int") == "int"


# --- recorder --------------------------------------------------------------


def test_discover_records_metadata_only_and_refuses_the_rest(run_root: Path) -> None:
    recorder = Recorder(run_root, mode="discover")
    calls: list[str] = []
    feed(recorder, "GET", DESIGNER_URL, designer_body(), calls=calls)
    feed(
        recorder,
        "GET",
        TRANSACTIONS + "0",
        transactions_html(),
        ctype="text/html; charset=utf-8",
        calls=calls,
    )
    feed(recorder, "POST", f"{BASE}/port_simul6.jsp", b"<html></html>", calls=calls)
    feed(recorder, "GET", REVERT_URL, b"{}", calls=calls)
    feed(recorder, "GET", f"{BASE}/sv/account-settings/billing", b"{}", calls=calls)
    feed(recorder, "GET", "https://elsewhere.example/x", b"{}", calls=calls)
    feed(recorder, "GET", f"{BASE}/static/app.js", b"x", ctype="text/javascript")
    feed(recorder, "GET", f"{BASE}/missing", b"{}", status=404, calls=calls)
    manifest = recorder.finish()

    # refused requests never had their body read
    assert calls == [DESIGNER_URL, TRANSACTIONS + "0"]
    assert manifest["status"] == "DISCOVERY"
    assert manifest["MUTATION_LIKE_NOT_RECORDED"] == [
        {
            "count": 1,
            "method": "GET",
            "path_template": "/app/screen/summary/{id}/revert",
            "reason": "PATTERN",
        },
        {
            "count": 1,
            "method": "POST",
            "path_template": "/port_simul6.jsp",
            "reason": "NON_GET",
        },
    ]
    assert manifest["IDENTITY_BILLING_NOT_RECORDED"] == [
        {
            "count": 1,
            "path_template": "/sv/account-settings/billing",
            "words": ["account", "billing"],
        }
    ]
    assert manifest["counts"]["offorigin"] == 1
    assert manifest["safety"] == rec.SAFETY
    blob = all_run_bytes(run_root)
    for secret in (SECRET_ID, SECRET_TEXT, "elsewhere", "SecretName"):
        assert secret.encode() not in blob
    assert not (run_root / "raw").exists()
    entries = json.loads((run_root / "discovery.json").read_text("utf-8"))
    designer = next(
        e for e in entries if e["path_template"] == "/spr/strategy/model/{id}"
    )
    assert designer["query_keys"] == ["dm"] and designer["query_flags"] == [
        ["dm", "true"]
    ]
    assert designer["body_shape"]["name"] == "str"
    assert designer["body_shape"]["endDt"] == "us_date"
    html = next(e for e in entries if e["path_template"] == "/transact_list.jsp")[
        "html"
    ]
    assert html["table_profiles"][0]["data_rows"] == 50
    assert html["table_profiles"][0]["header_cells"][:3] == ["Date", "Type", "Ticker"]
    assert manifest["pagination"] == [
        {
            "path_template": "/transact_list.jsp",
            "key": "offset",
            "distinct": 1,
            "min": 0,
            "max": 0,
            "step": None,
            "contiguous": True,
        }
    ]
    assert verify_run(run_root)["mode"] == "discover"


def test_capture_stores_raw_bytes_hashes_and_writes_manifest_last(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    original = rec._write_new

    def spy(path: Path, payload: bytes) -> dict[str, object]:
        order.append(path.name)
        return original(path, payload)

    monkeypatch.setattr(rec, "_write_new", spy)
    recorder = Recorder(run_root, mode="capture")
    body = designer_body()
    page = transactions_html()
    feed(recorder, "GET", DESIGNER_URL, body)
    feed(recorder, "GET", TRANSACTIONS + "0", page, ctype="text/html")
    feed(
        recorder,
        "GET",
        f"{BASE}/a/b.csv",
        b"Date,Symbol\n01/31/2000,X\n",
        ctype="text/csv",
    )
    feed(recorder, "POST", f"{BASE}/port_simul6.jsp", b"<html></html>")
    manifest = recorder.finish()

    assert order[-1] == "manifest.json"
    assert order[-2] == "entries.json"
    assert manifest["status"] == "CAPTURE_COMPLETE"
    raw = sorted((run_root / "raw").iterdir())
    assert [p.read_bytes() for p in raw] == [body, page, b"Date,Symbol\n01/31/2000,X\n"]
    entries = json.loads((run_root / "entries.json").read_text("utf-8"))
    assert [e["body_sha256"] for e in entries] == [
        hashlib.sha256(x).hexdigest()
        for x in (body, page, b"Date,Symbol\n01/31/2000,X\n")
    ]
    assert entries[2]["csv"]["columns"] == ["Date", "Symbol"]
    result = verify_run(run_root)
    assert result["raw_bodies"] == 3 and result["mode"] == "capture"
    # a refused request leaves no trace beyond its template
    assert b"port_simul6" in (run_root / "manifest.json").read_bytes()
    assert not any(b"port_simul6" in p.read_bytes() for p in raw)


def test_oversize_and_unreadable_bodies_are_flagged_not_recorded(
    run_root: Path,
) -> None:
    recorder = Recorder(run_root, mode="capture", max_body_bytes=10)
    feed(recorder, "GET", f"{BASE}/a/big", b"x" * 11, ctype="text/html")

    def boom() -> bytes:
        raise RuntimeError("evicted")

    recorder.handle(
        method="GET",
        url=f"{BASE}/a/gone",
        status=200,
        content_type="application/json",
        read_body=boom,
    )
    manifest = recorder.finish()
    assert manifest["status"] == "CAPTURE_WITH_UNRECORDED_BODIES"
    assert manifest["OVERSIZE_NOT_RECORDED"] == [
        {"count": 1, "path_template": "/a/big"}
    ]
    assert manifest["counts"]["unreadable_bodies"] == 1
    assert manifest["counts"]["raw_bodies"] == 0


def test_identity_flag_records_only_the_named_family(run_root: Path) -> None:
    recorder = Recorder(run_root, mode="discover", include_identity=["account"])
    feed(recorder, "GET", f"{BASE}/app/account/positions/3", b"{}")
    feed(recorder, "GET", f"{BASE}/a/billing", b"{}")
    manifest = recorder.finish()
    assert manifest["include_identity"] == ["account"]
    assert [
        e["path_template"]
        for e in json.loads((run_root / "discovery.json").read_text("utf-8"))
    ] == ["/app/account/positions/{id}"]
    assert manifest["IDENTITY_BILLING_NOT_RECORDED"][0]["words"] == ["billing"]
    assert verify_run(run_root)["entries"] == 1
    with pytest.raises(RecorderError):
        Recorder(run_root.parent / "other", mode="discover", include_identity=["nope"])


# --- output root rules -----------------------------------------------------


def test_output_root_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_NOT_ABSOLUTE"):
        Recorder(Path("relative-run"), mode="discover")
    existing = tmp_path / "exists"
    existing.mkdir()
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_EXISTS"):
        Recorder(existing, mode="discover")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_REPOSITORY"):
        Recorder(repo / "evidence" / "run", mode="discover")
    sync = tmp_path / "OneDrive - Example"
    sync.mkdir()
    monkeypatch.setenv("OneDriveCommercial", str(sync))
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_SYNCED_SCRATCH"):
        Recorder(sync / "scratch" / "run", mode="discover")
    # the governed evidence root inside a synced library is allowed
    assert Recorder(
        sync / "evidence" / "p123-block0" / "run", mode="discover"
    ).root.exists()


def test_files_are_create_only(run_root: Path) -> None:
    recorder = Recorder(run_root, mode="capture")
    feed(recorder, "GET", f"{BASE}/a/b", b"{}")
    recorder.finish()
    with pytest.raises(RecorderError, match="ARTIFACT_EXISTS"):
        recorder.finish()


# --- verify refuses forgeries ----------------------------------------------


def _rebind(root: Path) -> None:
    """Rewrite the manifest hashes so only the semantic checks can object."""

    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    for item in manifest["artifacts"]:
        payload = (root / item["path"]).read_bytes()
        item["sha256"] = hashlib.sha256(payload).hexdigest()
        item["size_bytes"] = len(payload)
    (root / "manifest.json").write_bytes(rec._canonical(manifest))


@pytest.fixture
def finished(run_root: Path) -> Path:
    recorder = Recorder(run_root, mode="capture")
    feed(recorder, "GET", DESIGNER_URL, designer_body())
    feed(recorder, "GET", TRANSACTIONS + "0", transactions_html(), ctype="text/html")
    recorder.finish()
    return run_root


def _code(root: Path) -> str:
    with pytest.raises(RecorderError) as caught:
        verify_run(root)
    return caught.value.code


def test_verify_accepts_a_clean_run(finished: Path) -> None:
    assert verify_run(finished)["entries"] == 2


def test_verify_detects_a_changed_raw_body(finished: Path) -> None:
    target = next((finished / "raw").iterdir())
    target.write_bytes(target.read_bytes() + b" ")
    assert _code(finished) == "ARTIFACT_HASH_DIFFERS"


def test_verify_detects_raw_body_rebound_in_manifest_only(finished: Path) -> None:
    target = next((finished / "raw").iterdir())
    target.write_bytes(target.read_bytes() + b" ")
    _rebind(finished)
    assert _code(finished) == "RAW_HASH_DIFFERS"


def test_verify_detects_a_stray_file(finished: Path) -> None:
    (finished / "raw" / "99999.body").write_bytes(b"x")
    assert _code(finished) == "RUN_DIRECTORY_DIFFERS_FROM_MANIFEST"


def test_verify_rederives_metadata_from_raw_bytes(finished: Path) -> None:
    entries = json.loads((finished / "entries.json").read_text("utf-8"))
    entries[0]["body_shape"]["name"] = "int"
    (finished / "entries.json").write_bytes(rec._canonical(entries))
    _rebind(finished)
    assert _code(finished) == "DERIVED_METADATA_DIFFERS"


def test_verify_refuses_a_recorded_mutation_route(finished: Path) -> None:
    entries = json.loads((finished / "entries.json").read_text("utf-8"))
    entries[0]["path_template"] = "/app/screen/summary/{id}/revert"
    (finished / "entries.json").write_bytes(rec._canonical(entries))
    _rebind(finished)
    assert _code(finished) == "REFUSED_ROUTE_RECORDED"


def test_verify_refuses_a_recorded_non_get(finished: Path) -> None:
    entries = json.loads((finished / "entries.json").read_text("utf-8"))
    entries[0]["method"] = "POST"
    (finished / "entries.json").write_bytes(rec._canonical(entries))
    _rebind(finished)
    assert _code(finished) == "NON_GET_RECORDED"


def test_verify_refuses_a_weakened_safety_block(finished: Path) -> None:
    manifest = json.loads((finished / "manifest.json").read_text("utf-8"))
    manifest["safety"]["passive"] = False
    (finished / "manifest.json").write_bytes(rec._canonical(manifest))
    assert _code(finished) == "MANIFEST_SAFETY_BLOCK_DIFFERS"


def test_verify_refuses_raw_files_in_a_discover_run(run_root: Path) -> None:
    recorder = Recorder(run_root, mode="discover")
    feed(recorder, "GET", f"{BASE}/a/b", b"{}")
    recorder.finish()
    entries = json.loads((run_root / "discovery.json").read_text("utf-8"))
    entries[0]["raw_path"] = "raw/00000.body"
    (run_root / "discovery.json").write_bytes(rec._canonical(entries))
    _rebind(run_root)
    assert _code(run_root) == "DISCOVER_RUN_HOLDS_RAW"


# --- CDP: allow-list, framing, passive listener ----------------------------


class FakeTransport:
    """Scripted browser. Replies to the allowed calls; raises Trip on header reads."""

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
    """Any read of its contents fails the test (headers, cookies)."""

    def _boom(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("headers or cookies were read")

    __getitem__ = get = items = keys = values = __iter__ = __len__ = _boom  # type: ignore[assignment]


def net(sess: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    message = {"method": method, "params": params}
    message["sessionId"] = sess
    return message


def request_events(
    sess: str, rid: str, method: str, url: str, status: int, mime: str
) -> list[dict[str, Any]]:
    request = {"method": method, "url": url, "headers": Trip(one=SECRET_ONE)}
    response = {
        "status": status,
        "mimeType": mime,
        "headers": Trip(two=SECRET_TWO),
        "url": url,
    }
    return [
        net(sess, "Network.requestWillBeSent", {"requestId": rid, "request": request}),
        net(sess, "Network.requestWillBeSentExtraInfo", Trip(headers=SECRET_ONE)),  # type: ignore[arg-type]
        net(
            sess,
            "Network.responseReceived",
            {"requestId": rid, "response": response, "type": "XHR"},
        ),
        net(sess, "Network.responseReceivedExtraInfo", Trip(headers=SECRET_ONE)),  # type: ignore[arg-type]
        net(sess, "Network.loadingFinished", {"requestId": rid}),
    ]


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
        "Network.emulateNetworkConditions",
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
            {
                "id": 1,
                "method": method,
                "params": {"requestId": "r", "targetId": "t"},
            }
        )
    assert len(inner.sent) == len(ALLOWED_CDP_METHODS)
    assert ALLOWED_CDP_METHODS == {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Network.enable",
        "Network.getResponseBody",
    }


def test_tool_source_never_names_an_acting_cdp_method() -> None:
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
        "requestHeaders",
        "postData",
    ):
        assert name not in source


def test_frame_roundtrip_and_fragmentation() -> None:
    key = b"\x01\x02\x03\x04"
    for size in (0, 5, 125, 126, 70000):
        payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
        opcode, decoded = decode_message(io.BytesIO(encode_frame(1, payload, key)).read)
        assert (opcode, decoded) == (1, payload)
    first = b"\x01\x03abc"  # text, not final, unmasked server frame
    second = b"\x80\x02de"  # continuation, final
    opcode, decoded = decode_message(io.BytesIO(first + second).read)
    assert (opcode, decoded) == (1, b"abcde")
    opcode, decoded = decode_message(io.BytesIO(b"\x89\x02hi").read)
    assert (opcode, decoded) == (9, b"hi")


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


def test_listener_is_passive_and_records_only_allowed_responses(run_root: Path) -> None:
    designer = designer_body()
    page = transactions_html()
    transport = FakeTransport(
        {"r1": designer, "r2": page, "r3": b"<html>revert</html>", "r6": b"{}"}
    )
    transport.targets = [
        {"targetId": "T1", "type": "page", "url": f"{BASE}/app/opener/SCR"},
        {"targetId": "T2", "type": "page", "url": "https://mail.example/inbox"},
        {"targetId": "T3", "type": "service_worker", "url": f"{BASE}/sw.js"},
    ]
    events: list[dict[str, Any]] = []
    events += request_events("S-T1", "r1", "GET", DESIGNER_URL, 200, "application/json")
    events += request_events("S-T1", "r2", "GET", TRANSACTIONS + "0", 200, "text/html")
    events += request_events(
        "S-T1", "r3", "POST", f"{BASE}/port_simul6.jsp", 200, "text/html"
    )
    events += request_events("S-T1", "r4", "GET", REVERT_URL, 200, "text/html")
    events += request_events(
        "S-T1", "r5", "GET", "https://cdn.example/x.js", 200, "text/javascript"
    )
    events += request_events(
        "S-T1", "r6", "GET", f"{BASE}/a/binary", 200, "application/json"
    )
    # events from a sess we never attached to are ignored
    events += request_events("S-T2", "r7", "GET", f"{BASE}/x", 200, "application/json")
    events.append(
        net(
            "S-T1",
            "Network.requestWillBeSent",
            {
                "requestId": "r8",
                "request": {"method": "GET", "url": f"{BASE}/redir", "headers": Trip()},
            },
        )
    )
    events.append(net("S-T1", "Network.loadingFailed", {"requestId": "r8"}))
    recorder = Recorder(run_root, mode="capture")
    original_send = transport.send

    def after_attach(message: dict[str, Any]) -> None:
        original_send(message)
        if message["method"] == "Network.enable":
            transport.inbound.extend(events)
            events.clear()

    transport.send = after_attach  # type: ignore[method-assign]
    Listener(transport, recorder, max_seconds=0).run()
    manifest = recorder.finish()

    sent_methods = [m["method"] for m in transport.sent]
    assert set(sent_methods) <= ALLOWED_CDP_METHODS
    # only the signed-in P123 page was attached, once
    attaches = [m for m in transport.sent if m["method"] == "Target.attachToTarget"]
    assert [m["params"]["targetId"] for m in attaches] == ["T1"]
    # bodies were requested only for recorded responses
    wanted = [
        m["params"]["requestId"]
        for m in transport.sent
        if m["method"] == "Network.getResponseBody"
    ]
    assert wanted == ["r1", "r2", "r6"]
    assert manifest["browser_closed"] is True
    assert manifest["counts"]["offorigin"] == 1
    assert manifest["counts"]["aborted"] == 1
    assert manifest["counts"]["unreadable_bodies"] == 0
    assert [m["path_template"] for m in manifest["MUTATION_LIKE_NOT_RECORDED"]] == [
        "/app/screen/summary/{id}/revert",
        "/port_simul6.jsp",
    ]
    raw = sorted((run_root / "raw").iterdir())
    assert [p.read_bytes() for p in raw] == [designer, page, b"{}"]
    blob = all_run_bytes(run_root)
    for secret in (
        SECRET_ONE,
        SECRET_TWO,
        SECRET_ID,
        "cdn.example",
        "mail.example",
    ):
        assert secret.encode() not in blob
    assert verify_run(run_root)["raw_bodies"] == 3


def test_listener_attaches_when_a_tab_navigates_to_portfolio123(run_root: Path) -> None:
    transport = FakeTransport({})
    transport.targets = [{"targetId": "T9", "type": "page", "url": "about:blank"}]
    recorder = Recorder(run_root, mode="discover")
    original_send = transport.send

    def hook(message: dict[str, Any]) -> None:
        original_send(message)
        if message["method"] == "Target.setDiscoverTargets":
            transport.inbound.append(
                {
                    "method": "Target.targetInfoChanged",
                    "params": {
                        "targetInfo": {
                            "targetId": "T9",
                            "type": "page",
                            "url": f"{BASE}/app/opener/SCR",
                        }
                    },
                }
            )

    transport.send = hook  # type: ignore[method-assign]
    Listener(transport, recorder).run()
    assert [m["method"] for m in transport.sent] == [
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Network.enable",
    ]


def test_cdp_call_failure_does_not_attach_and_does_not_crash(run_root: Path) -> None:
    class Failing(FakeTransport):
        def send(self, message: dict[str, Any]) -> None:
            super().send(message)
            if message["method"] == "Target.attachToTarget":
                self.inbound[0] = {"id": message["id"], "error": {"message": "gone"}}

    transport = Failing({})
    transport.targets = [{"targetId": "T1", "type": "page", "url": f"{BASE}/x"}]
    recorder = Recorder(run_root, mode="discover")
    Listener(transport, recorder).run()
    assert not any(m["method"] == "Network.enable" for m in transport.sent)


# --- propose-catalog -------------------------------------------------------


def _discover_for_proposal(root: Path, *, extra_field: bool = False) -> Path:
    recorder = Recorder(root, mode="discover")
    body = json.loads(designer_body())
    if extra_field:
        body["zzNewField"] = 1
    feed(recorder, "GET", DESIGNER_URL, json.dumps(body).encode())
    for offset in (0, 50, 100):
        feed(
            recorder,
            "GET",
            TRANSACTIONS + str(offset),
            transactions_html(50 if offset < 100 else 7),
            ctype="text/html",
        )
    feed(
        recorder,
        "GET",
        f"{BASE}/spr/aifactor/predictor/3001/predictions",
        json.dumps(
            {"id": 3001, "name": SECRET_TEXT, "rows": [{"t": SECRET_TEXT, "s": 0.5}]}
        ).encode(),
    )
    feed(
        recorder,
        "GET",
        f"{BASE}/spr/aifactor/model/42",
        json.dumps(
            {"id": 42, "target": "ret", "features": [{"name": "a", "w": 1.0}]}
        ).encode(),
    )
    feed(
        recorder,
        "GET",
        f"{BASE}/spr/screen/definition/9",
        json.dumps(
            {"id": 9, "rules": ["x"], "universe": "u", "ranking": None}
        ).encode(),
    )
    feed(recorder, "GET", f"{BASE}/spr/user", b"{}")
    feed(recorder, "GET", f"{BASE}/app/opener/SIM", b"<html></html>", ctype="text/html")
    feed(recorder, "GET", REVERT_URL, b"{}")
    recorder.finish()
    return root


def test_propose_catalog_builds_draft_rows_from_known_and_synthetic_routes(
    tmp_path: Path,
) -> None:
    discover = _discover_for_proposal(tmp_path / "discover-1")
    out = tmp_path / "catalog-draft"
    written = propose_catalog(discover, out)

    assert written["applied"] is False
    assert sorted(p.name for p in out.iterdir()) == [
        "catalog-diff.draft.json",
        "catalog-diff.draft.md",
        "proposal-manifest.json",
        "test_capture_new_rows.py.draft",
    ]
    proposal = json.loads((out / "catalog-diff.draft.json").read_text("utf-8"))
    assert proposal["applied"] is False and "never applied" in proposal["banner"]
    by_cap = {row["capability"]: row for row in proposal["capabilities"]}

    designer = by_cap["designer-model.json"]
    assert designer["change"]["change"] == "CONFIRMS_EXISTING"
    assert designer["candidates"][0]["route_template"] == "/spr/strategy/model/{id}"

    transactions = by_cap["transactions.account"]["candidates"][0]
    assert transactions["route_template"] == "/transact_list.jsp"
    assert transactions["query_keys"] == ["offset", "portid"]
    assert transactions["response_media_type"] == "html"
    assert transactions["paginated"] and transactions["page_param"] == "offset"
    assert transactions["html_profile"]["most_common_data_rows_per_page"] == 50
    assert transactions["html_profile"]["header_cells"][-1] == "TRADE"
    # the reviewed catalog row already admits this HTML route
    assert not transactions["requires_code_change"]
    assert by_cap["transactions.account"]["change"]["change"] == "SAME_ROUTE_UNVERIFIED"

    predictor = by_cap["aifactor.predictor"]["candidates"][0]
    assert predictor["route_template"] == "/spr/aifactor/predictor/{id}/predictions"
    assert predictor["path"] == "/spr/aifactor/predictor/{predictor_id}/predictions"
    assert (
        predictor["path_pattern"] == r"/spr/aifactor/predictor/[1-9][0-9]*/predictions"
    )
    assert "Obj(" in predictor["shape_spec_source"]
    assert by_cap["aifactor.model"]["candidates"][0]["match"] == "KEYWORD"
    # the model route is also an AI Factor family route for the predictor: owner picks
    assert by_cap["aifactor.predictor"]["needs_owner_pick"] is True
    screen = by_cap["screen.definition"]["candidates"][0]
    assert '"ranking": "null"' in screen["shape_spec_source"]
    # only the HTML opener page was seen: a candidate the owner must reject or pin
    simulation = by_cap["simulation.result"]["candidates"]
    assert [c["route_template"] for c in simulation] == ["/app/opener/SIM"]
    assert simulation[0]["response_media_type"] == "html"
    assert "/spr/user" not in {
        c["route_template"]
        for row in proposal["capabilities"]
        for c in row["candidates"]
    }

    blob = all_run_bytes(out)
    for secret in (SECRET_TEXT, SECRET_ID, "3001"):
        assert secret.encode() not in blob
    skeleton = (out / "test_capture_new_rows.py.draft").read_text("utf-8")
    compile(skeleton, "skeleton", "exec")
    assert "pytest.mark.skip" in skeleton
    assert '"offset"' in skeleton and "Ticker" in skeleton
    manifest = json.loads((out / "proposal-manifest.json").read_text("utf-8"))
    for item in manifest["artifacts"]:
        assert (
            hashlib.sha256((out / item["path"]).read_bytes()).hexdigest()
            == item["sha256"]
        )


def test_propose_catalog_reports_drift_from_a_confirmed_row(tmp_path: Path) -> None:
    discover = _discover_for_proposal(tmp_path / "discover-1", extra_field=True)
    proposal = build_proposal(
        json.loads((discover / "discovery.json").read_text("utf-8"))
    )
    designer = next(
        r for r in proposal["capabilities"] if r["capability"] == "designer-model.json"
    )
    assert designer["change"]["change"] == "DIFFERS_FROM_EXISTING"
    assert designer["change"]["fields_added"] == ["zzNewField"]


def test_propose_catalog_pins_and_raw_run_drift(tmp_path: Path) -> None:
    discover = _discover_for_proposal(tmp_path / "discover-1")
    capture = tmp_path / "capture-1"
    recorder = Recorder(capture, mode="capture")
    feed(
        recorder,
        "GET",
        f"{BASE}/spr/aifactor/model/42",
        json.dumps({"id": 42, "target": "ret", "features": [], "extra": 1}).encode(),
    )
    recorder.finish()
    out = tmp_path / "draft"
    propose_catalog(
        discover,
        out,
        raw_run=capture,
        pins={"aifactor.predictor": "/spr/aifactor/model/{id}"},
    )
    proposal = json.loads((out / "catalog-diff.draft.json").read_text("utf-8"))
    by_cap = {row["capability"]: row for row in proposal["capabilities"]}
    pinned = by_cap["aifactor.predictor"]["candidates"]
    assert [c["route_template"] for c in pinned] == ["/spr/aifactor/model/{id}"]
    assert pinned[0]["match"] == "PINNED"
    model = by_cap["aifactor.model"]["candidates"][0]
    assert model["shape_differs_between_runs"] is True
    assert "extra" in model["top_level_shape"]


def test_propose_catalog_refuses_unverified_input_and_bad_output(
    tmp_path: Path,
) -> None:
    discover = _discover_for_proposal(tmp_path / "discover-1")
    (discover / "discovery.json").write_bytes(b"[]\n")
    with pytest.raises(RecorderError, match="ARTIFACT_HASH_DIFFERS"):
        propose_catalog(discover, tmp_path / "out")
    clean = _discover_for_proposal(tmp_path / "discover-2")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_IN_REPOSITORY"):
        propose_catalog(clean, repo / "draft")
    out = tmp_path / "draft"
    propose_catalog(clean, out)
    with pytest.raises(RecorderError, match="OUTPUT_ROOT_EXISTS"):
        propose_catalog(clean, out)


def test_cli_verify_and_refusal_codes(
    finished: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rec.main(["verify", "--run-root", str(finished)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["raw_bodies"] == 2
    (finished / "raw" / "00000.body").write_bytes(b"changed")
    assert rec.main(["verify", "--run-root", str(finished)]) == 2
    assert capsys.readouterr().out.strip() == "REFUSED ARTIFACT_HASH_DIFFERS"
    assert (
        rec.main(["propose-catalog", "--discover", str(finished), "--out", "relative"])
        == 2
    )
    assert capsys.readouterr().out.startswith("REFUSED ")


def test_summary_is_aggregate_only(run_root: Path) -> None:
    recorder = Recorder(run_root, mode="discover")
    feed(recorder, "GET", TRANSACTIONS + "0", transactions_html(), ctype="text/html")
    printed = json.dumps(rec._summary(recorder.finish()))
    assert "/transact_list.jsp" in printed
    for secret in (SECRET_TEXT, SECRET_ID):
        assert secret not in printed


def test_generated_spec_source_is_valid_and_fails_closed_on_mixed_types() -> None:
    from quant_engine.portfolio123.capture_shapes import Obj, Seq, validate_shape

    body = json.loads(designer_body())
    spec = eval(  # noqa: S307 - evaluating the tool's own generated spec text
        rec._spec_source(shape_of(body)), {"Obj": Obj, "Seq": Seq}
    )
    validate_shape(body, spec)
    mixed = merge_shapes(shape_of({"a": 1}), shape_of({"a": "x"}))
    assert (
        rec._spec_source(mixed)
        == 'Obj(\n    {\n        "a": "<MIXED:int|str>",\n    }\n)'
    )
    assert '"<KEYED' in rec._spec_source(shape_of({str(n): 1 for n in range(3)}))


def test_small_maps_keyed_by_tickers_or_names_are_collapsed() -> None:
    shape = shape_of(
        {
            "positions": {"AAPL": {"q": 1}, "MSFT": {"q": 2}},
            "named": {"Apple Inc": 1, "Strategy One": 2},
            "single": {"BRK.B": 1},
            "record": {"daysAvg": 1.0, "retAvg": 2.0, "trades": 3},
        }
    )
    assert shape["positions"] == {"$keyed": {"q": "int"}, "$keys": 2}
    assert shape["named"] == {"$keyed": "int", "$keys": 2}
    assert shape["single"] == {"$keyed": "int", "$keys": 1}
    assert set(shape["record"]) == {"daysAvg", "retAvg", "trades"}
    blob = json.dumps(shape)
    for leaked in ("AAPL", "MSFT", "Apple", "Strategy", "BRK"):
        assert leaked not in blob


def test_embedded_id_patterns_match_the_observed_route() -> None:
    import re

    path, pattern = rec._template_pattern(path_template("/x/model1764562/y"), "model")
    assert path == "/x/model{model_id}/y"
    assert re.fullmatch(pattern, "/x/model1764562/y")
    assert not re.fullmatch(pattern, "/x/model{id}/y")


def test_any_2xx_status_makes_a_route_a_candidate(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "d", mode="discover")
    feed(
        recorder,
        "GET",
        f"{BASE}/spr/aifactor/model/42",
        json.dumps({"id": 1}).encode(),
        status=206,
    )
    recorder.finish()
    entries = json.loads((tmp_path / "d" / "discovery.json").read_text("utf-8"))
    proposal = build_proposal(entries)
    model = next(
        r for r in proposal["capabilities"] if r["capability"] == "aifactor.model"
    )
    assert [c["route_template"] for c in model["candidates"]] == [
        "/spr/aifactor/model/{id}"
    ]
    assert model["candidates"][0]["status_counts"] == {"206": 1}


class _IdleTransport(FakeTransport):
    """Replies to calls, then idles (timeouts) before the browser closes."""

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
    run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(run_root, mode="discover")
    Listener(_IdleTransport(20), recorder, warn_after=30.0, clock=_ticking()).run()
    manifest = recorder.finish()
    assert [w["code"] for w in manifest["warnings"]] == ["NO_ENTRIES_RECORDED"]
    assert "after 30 s" in manifest["warnings"][0]["text"]
    assert "start it FIRST" in manifest["warnings"][0]["text"]
    err = capsys.readouterr().err
    assert err.count("WARNING NO_ENTRIES_RECORDED") == 1
    assert verify_run(run_root)["status"] == "DISCOVERY"


def test_no_warning_when_something_was_recorded(
    run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(run_root, mode="discover")
    feed(recorder, "GET", f"{BASE}/a/b", b"{}")
    Listener(_IdleTransport(20), recorder, warn_after=30.0, clock=_ticking()).run()
    assert recorder.finish()["warnings"] == []
    assert capsys.readouterr().err == ""


def test_empty_run_always_records_a_warning_even_if_disabled(
    run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(run_root, mode="capture")
    Listener(_IdleTransport(3), recorder, warn_after=0, clock=_ticking()).run()
    manifest = recorder.finish()
    assert [w["code"] for w in manifest["warnings"]] == ["NO_ENTRIES_RECORDED"]
    assert "WARNING NO_ENTRIES_RECORDED" in capsys.readouterr().err


def test_warn_after_option_defaults_to_30() -> None:
    args = rec._parser().parse_args(
        ["discover", "--cdp-url", "http://127.0.0.1:9333", "--output-root", "C:/x"]
    )
    assert args.warn_after_seconds == 30.0
