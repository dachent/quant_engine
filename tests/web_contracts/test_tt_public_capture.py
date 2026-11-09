from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from pathlib import Path

import pytest

from tools import tt_public_capture as tt

TOKENS = {
    "public": "11111111-aaaa-4bbb-8ccc-000000000001",
    "allochist": "22222222-aaaa-4bbb-8ccc-000000000002",
    "holdings": "33333333-aaaa-4bbb-8ccc-000000000003",
}
LAST_MODIFIED = "Fri, 06 Nov 2026 06:49:34 GMT"
FETCH_TIME = "2026-11-06T21:52:00+00:00"


def chart(last: str) -> bytes:
    return (
        '{"labels":["Date","A"],"data":[["11/03/2026",1.5],["' + last + '",2.25]]}\r\n'
    ).encode()


class FakeTransport:
    def __init__(self, last_chart: str, stamp: str, overrides: dict | None = None):
        self.last_chart = last_chart
        self.stamp = stamp
        self.overrides = overrides or {}
        self.urls: list[str] = []

    def body_for(self, url: str) -> bytes:
        if "performance-chart" in url:
            return chart(self.last_chart)
        if "last-update" in url:
            return self.stamp.encode()
        return b'\xef\xbb\xbf{"raw": "\\u00e9"}\r\n' + url.split("?")[0].encode()

    def get(self, url: str, timeout: float) -> tt.HttpResponse:
        self.urls.append(url)
        for needle, behaviour in self.overrides.items():
            if needle in url:
                if isinstance(behaviour, Exception):
                    raise behaviour
                return behaviour
        return tt.HttpResponse(
            200, self.body_for(url), {"etag": '"abc"', "Last-Modified": LAST_MODIFIED}
        )


def run(tmp_path: Path, transport: FakeTransport, cutoff: str = "2026-11-05"):
    out = tmp_path / "cap"
    stream = io.StringIO()
    code, manifest = tt.run_capture(
        out,
        date.fromisoformat(cutoff),
        TOKENS,
        transport,
        clock=lambda: tt._now().replace(year=2026, month=11, day=6),
        stream=stream,
    )
    return out, code, manifest, stream.getvalue()


def all_files_text(out: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file())


def test_fresh_capture_table_and_byte_identity(tmp_path):
    transport = FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    out, code, manifest, printed = run(tmp_path, transport)
    assert code == 0 and manifest["status"] == "COMPLETE"
    assert len(manifest["objects"]) == 1 + 6 * 4 + 6 * 2 + 1
    assert len(transport.urls) == 38
    for obj in manifest["objects"]:
        body = (out / obj["file"]).read_bytes()
        assert obj["http_status"] == 200 and obj["bytes"] == len(body)
        assert obj["sha256"] == hashlib.sha256(body).hexdigest()
        assert obj["etag"] == '"abc"' and obj["last_modified"] == LAST_MODIFIED
        assert "token" not in obj["url"]
        assert body == transport.body_for(obj["url"])
    assert (out / "json" / "tt-hot-sauce__holdings-history.json").exists()
    assert (out / "txt" / "tt-hokey-pokey__last-rebalance.txt").exists()
    assert (out / "vane" / "tt-market-vane__status.json").exists()
    assert "tt-hot-sauce__portfolio-holdings.json\t200\t" in printed
    assert manifest["sha256_list"] == sorted(
        manifest["sha256_list"], key=lambda e: e["file"]
    )
    assert set(manifest["captured_at_utc"]) == {"json", "txt", "vane"}
    assert manifest["authority"] == "#283 comment 5515808323; #699 comment 6536613285"
    on_disk = json.loads((out / "capture-manifest.json").read_bytes())
    assert on_disk == manifest
    assert b"\r" not in (out / "capture-manifest.json").read_bytes()


def test_token_routing(tmp_path):
    transport = FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    run(tmp_path, transport)
    for url in transport.urls:
        if "holdings-history" in url:
            assert TOKENS["allochist"] in url
        elif "portfolio-holdings" in url:
            assert TOKENS["holdings"] in url
        else:
            assert TOKENS["public"] in url
        assert url.startswith(tt.BASE) and "alt=media&token=" in url


def test_stale_for_d_fresh_for_previous_day(tmp_path):
    stale_t = FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    out, code, manifest, printed = run(tmp_path, stale_t, cutoff="2026-11-06")
    assert code == tt.EXIT_STALE and manifest["status"] == "STALE"
    assert (out / "capture-manifest.json").exists()
    reasons = manifest["freshness"]["reasons"]
    assert len(reasons) == 12
    assert (
        "tt-hokey-pokey__performance-chart.json: found 2026-11-05; "
        "required last chart date == 2026-11-06" in reasons
    )
    assert (
        "tt-hokey-pokey/last-update.txt: found 2026-11-06T06:46Z; "
        "required stamp >= 2026-11-07T06:00Z" in reasons
    )
    assert "STALE tt-hot-sauce/last-update.txt" in printed
    fresh_t = FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    sub = tmp_path / "second"
    sub.mkdir()
    _, code2, manifest2, _ = run(sub, fresh_t, cutoff="2026-11-05")
    assert code2 == 0 and manifest2["freshness"]["result"] == "FRESH"


def test_freshness_boundary_and_unparseable(tmp_path):
    t = FakeTransport("2026-11-05", "2026-11-06T05:59Z")
    _, code, manifest, _ = run(tmp_path, t)
    assert code == tt.EXIT_STALE
    assert all("found 2026-11-05" not in r for r in manifest["freshness"]["reasons"])
    t2 = FakeTransport("11/05/2026", "2026-11-06T06:00Z")
    sub = tmp_path / "b"
    sub.mkdir()
    assert run(sub, t2)[1] == 0
    t3 = FakeTransport("11/05/2026", "garbage")
    sub3 = tmp_path / "c"
    sub3.mkdir()
    _, code3, m3, _ = run(sub3, t3)
    assert code3 == tt.EXIT_STALE
    assert "unparseable stamp: garbage" in m3["freshness"]["reasons"][0]


def test_create_only(tmp_path):
    out = tmp_path / "cap"
    out.mkdir()
    (out / "keep.txt").write_bytes(b"x")
    with pytest.raises(tt.CaptureRefusal):
        tt.run_capture(out, date(2026, 11, 5), TOKENS, FakeTransport("11/05/2026", "s"))
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_errors_recorded_and_exit_nonzero_and_redacted(tmp_path):
    leaky = ConnectionError(
        f"failed https://x/o?alt=media&token={TOKENS['public']} {TOKENS['holdings']}"
    )
    transport = FakeTransport(
        "11/05/2026",
        "2026-11-06T06:46Z",
        {
            "tt-mach-3-v3%2Fperformance-table": leaky,
            "tt-hot-sauce%2Fholdings-history": tt.HttpResponse(403, b"denied"),
        },
    )
    out, code, manifest, printed = run(tmp_path, transport)
    assert code == tt.EXIT_FAILED and manifest["status"] == "FAILED_OBJECTS"
    by = {o["object"]: o for o in manifest["objects"]}
    err = by["tt-mach-3-v3__performance-table.json"]
    assert err["error"].startswith("ConnectionError") and "http_status" not in err
    assert not (out / err["file"]).exists()
    bad = by["tt-hot-sauce__holdings-history.json"]
    assert bad["http_status"] == 403 and bad["error"] == "HTTP 403"
    assert not (out / bad["file"]).exists()
    assert "FAILED tt-mach-3-v3__performance-table.json" in printed
    blob = all_files_text(out) + printed.encode()
    for token in TOKENS.values():
        assert token.encode() not in blob


def test_token_never_in_outputs_or_stdio(tmp_path, capsys):
    tokens_file = tmp_path / "tok" / "t.json"
    tokens_file.parent.mkdir()
    tokens_file.write_bytes(json.dumps(TOKENS).encode())
    out = tmp_path / "cap"
    rc = tt.main(
        [
            "capture",
            "--out",
            str(out),
            "--cutoff",
            "2026-11-05",
            "--tokens-file",
            str(tokens_file),
        ],
        transport=FakeTransport("11/05/2026", "2026-11-06T06:46Z"),
    )
    assert rc == 0
    captured = capsys.readouterr()
    blob = all_files_text(out) + captured.out.encode() + captured.err.encode()
    for token in TOKENS.values():
        assert token.encode() not in blob
    assert b"token=" not in (out / "capture-manifest.json").read_bytes()


def test_tokens_from_environment(monkeypatch):
    for key, name in tt.TOKEN_ENV.items():
        monkeypatch.setenv(name, TOKENS[key])
    assert tt.load_tokens(None) == TOKENS


def test_token_refusals_do_not_leak(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    inside = repo / "t.json"
    inside.write_bytes(json.dumps(TOKENS).encode())
    with pytest.raises(tt.CaptureRefusal):
        tt.load_tokens(inside)
    bad = tmp_path / "bad.json"
    bad.write_bytes(json.dumps({**TOKENS, "public": "not-a-token-secret"}).encode())
    with pytest.raises(tt.CaptureRefusal) as exc:
        tt.load_tokens(bad)
    assert "not-a-token-secret" not in str(exc.value)
    out = tmp_path / "cap"
    rc = tt.main(
        [
            "capture",
            "--out",
            str(out),
            "--cutoff",
            "2026-11-05",
            "--tokens-file",
            str(inside),
        ]
    )
    assert rc == tt.EXIT_REFUSED and not out.exists()
    err = capsys.readouterr().err
    assert TOKENS["public"] not in err
    rc = tt.main(["capture", "--out", str(tmp_path), "--cutoff", "2026-11-05"])
    assert rc == tt.EXIT_REFUSED


def test_redact_helper():
    text = f"boom ?token={TOKENS['public']}&x=1 and {TOKENS['holdings']}"
    cleaned = tt.redact(text, list(TOKENS.values()))
    assert TOKENS["public"] not in cleaned and TOKENS["holdings"] not in cleaned
    assert "token=[REDACTED]&x=1" in cleaned


def test_unreadable_chart_is_stale(tmp_path):
    t = FakeTransport(
        "11/05/2026",
        "2026-11-06T06:46Z",
        {
            "tt-hot-sauce%2Fperformance-chart": tt.HttpResponse(200, b"not json"),
        },
    )
    _, code, manifest, _ = run(tmp_path, t)
    assert code == tt.EXIT_STALE
    assert manifest["freshness"]["reasons"] == [
        "tt-hot-sauce__performance-chart.json: found unreadable chart; "
        "required last chart date == 2026-11-05"
    ]


def _tokens_file(tmp_path: Path) -> Path:
    holder = tmp_path / "keys"
    holder.mkdir()
    path = holder / "tokens.json"
    path.write_bytes(json.dumps(TOKENS).encode())
    return path


def test_out_inside_git_repo_refused_before_anything_is_created(tmp_path, capsys):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    out = repo / "captures" / "run"
    transport = FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    code = tt.main(
        [
            "capture",
            "--out",
            str(out),
            "--cutoff",
            "2026-11-05",
            "--tokens-file",
            str(_tokens_file(tmp_path)),
        ],
        transport,
    )
    assert code == tt.EXIT_REFUSED
    assert "inside a git repository" in capsys.readouterr().err
    assert not (repo / "captures").exists() and transport.urls == []
    with pytest.raises(tt.CaptureRefusal):
        tt.run_capture(out, date(2026, 11, 5), TOKENS, transport)
    assert not (repo / "captures").exists()


def test_group_manifests_are_consumable_by_the_governed_builder(tmp_path):
    from tools import tt_source_manifest_builder as builder

    out, code, _, _ = run(tmp_path, FakeTransport("11/05/2026", "2026-11-06T06:46Z"))
    assert code == 0
    loaded = {
        role: builder._load_capture(out / role, role, legacy=False)
        for role in ("json", "txt", "vane")
    }
    assert len(loaded["json"].records) == 1 + 6 * 4
    assert len(loaded["txt"].records) == 6 * 2
    assert list(loaded["vane"].records) == ["tt-market-vane__status.json"]
    for role, capture in loaded.items():
        assert capture.captured_at_utc.endswith("Z")
        group = json.loads((out / role / "capture-manifest.json").read_bytes())
        assert isinstance(group["captured_at_utc"], str)
        for record in capture.records.values():
            assert record.http_status == 200
            assert record.data == (out / role / record.filename).read_bytes()
    assert "tt-hokey-pokey__last-update.txt" in loaded["txt"].records


def test_subset_groups_are_partial_not_complete(tmp_path):
    out = tmp_path / "cap"
    stream = io.StringIO()
    transport = FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    code, manifest = tt.run_capture(
        out, date(2026, 11, 5), TOKENS, transport, groups=["vane"], stream=stream
    )
    assert code == tt.EXIT_PARTIAL == 4
    assert manifest["status"] == "PARTIAL_GROUPS"
    assert manifest["omitted_groups"] == ["json", "txt"]
    assert "PARTIAL omitted groups: json,txt" in stream.getvalue()
    assert (
        len(transport.urls) == 1 and (out / "vane" / "capture-manifest.json").exists()
    )
    again = tmp_path / "cap2"
    code2, manifest2 = tt.run_capture(
        again,
        date(2026, 11, 5),
        TOKENS,
        FakeTransport("11/05/2026", "2026-11-06T06:46Z"),
        groups=["vane"],
        allow_partial=True,
        stream=io.StringIO(),
    )
    assert code2 == 0 and manifest2["status"] == "PARTIAL_GROUPS"


def test_full_capture_lists_no_omitted_groups(tmp_path):
    _, code, manifest, _ = run(
        tmp_path, FakeTransport("11/05/2026", "2026-11-06T06:46Z")
    )
    assert code == 0 and manifest["omitted_groups"] == []
