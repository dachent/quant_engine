from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

import pytest

from alpaca_capture_support import (
    ACCOUNT_ID,
    NUMBER_KEY,
    ScriptedFetcher,
    account_body,
    capture_config,
    fill,
    governed,
    nontrade,
    ok,
    page,
    positions_body,
)
from quant_engine.alpaca.capture import (
    CaptureError,
    CaptureIncomplete,
    CaptureRefused,
    GovernedFetcher,
    RawResponse,
    run_capture,
    verify_run,
)
from quant_engine.alpaca.capture.run import read_manifest

PAGE_ONE = [fill(1), fill(2, side="sell", price="11")]
PAGE_TWO = [nontrade(3, "DIV", "0.42", symbol="SYNA"), nontrade(4, "CSD", "-50")]
PAGE_THREE = [nontrade(5, "FEE", "-0.10")]


def _staging(tmp_path: Path) -> Path:
    root = tmp_path / "staging"
    root.mkdir(exist_ok=True)
    return root


def _happy_script() -> list[RawResponse]:
    return [
        ok(account_body()),
        ok(positions_body()),
        ok(page(PAGE_ONE)),
        ok(page(PAGE_TWO)),
        ok(page(PAGE_THREE)),
    ]


def _run(
    tmp_path: Path, script: list[RawResponse], run_id: str = "run-1", **config: Any
) -> tuple[Any, ScriptedFetcher]:
    capture = capture_config(**config)
    fetcher, inner, governance = governed(tmp_path, capture, script)
    result = run_capture(
        config=capture,
        fetcher=fetcher,
        governance=governance,
        staging_root=_staging(tmp_path),
        run_id=run_id,
    )
    return result, inner


def test_paginated_capture_stages_normalizes_and_verifies(tmp_path: Path) -> None:
    result, inner = _run(tmp_path, _happy_script())
    assert result.request_count == 5 and result.activity_count == 5
    paths = [path for path, _ in inner.calls]
    assert paths == [
        "/v2/account",
        "/v2/positions",
        "/v2/account/activities",
        "/v2/account/activities",
        "/v2/account/activities",
    ]
    cursors = [query.get("page" + "_token") for _, query in inner.calls[2:]]
    assert cursors == [None, PAGE_ONE[-1]["id"], PAGE_TWO[-1]["id"]]
    assert inner.calls[2][1] == {
        "direction": "asc",
        "page_size": "2",
        "after": "2024-01-01",
        "until": "2024-12-31",
    }
    report = verify_run(result.run_directory)
    assert report.ok, report.findings
    assert report.activity_count == 5
    summary = json.loads((result.run_directory / "normalized/summary.json").read_text())
    assert summary["activity_count"] == 5
    assert summary["by_category"]["fee"]["cash_amount"] == "-0.10"
    manifest = read_manifest(result.run_directory)
    assert manifest["terminal"]["complete"] is True
    assert manifest["classification"] == "restricted-private"
    # No account identifier or credential-shaped key reaches the staged metadata.
    for name in ("manifest.json", "request_manifest.jsonl"):
        text = (result.run_directory / name).read_text(encoding="utf-8")
        assert ACCOUNT_ID not in text and "synth-a" not in text


def test_exactly_full_last_page_needs_a_terminating_empty_page(tmp_path: Path) -> None:
    script = [
        ok(account_body()),
        ok(positions_body()),
        ok(page(PAGE_ONE)),
        ok(page([])),
    ]
    result, inner = _run(tmp_path, script)
    assert len(inner.calls) == 4 and result.activity_count == 2
    assert verify_run(result.run_directory).ok


def test_positions_are_optional(tmp_path: Path) -> None:
    script = [ok(account_body()), ok(page(PAGE_THREE))]
    result, inner = _run(tmp_path, script, include_positions=False)
    assert [p for p, _ in inner.calls] == ["/v2/account", "/v2/account/activities"]
    assert not (result.run_directory / "normalized/positions.json").exists()
    assert verify_run(result.run_directory).ok


def test_rate_limit_stops_without_retry_and_keeps_evidence(tmp_path: Path) -> None:
    limited = RawResponse(
        status=429, body=b'{"message":"synthetic limit"}', headers={"retry-after": "7"}
    )
    capture = capture_config()
    fetcher, inner, governance = governed(
        tmp_path,
        capture,
        [ok(account_body()), ok(positions_body()), ok(page(PAGE_ONE)), limited],
    )
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="limited",
        )
    assert stopped.value.reason == "rate_limited"
    assert len(inner.calls) == 4  # exactly one attempt per request, no retry
    run_directory = Path(stopped.value.run_directory)
    records = [
        json.loads(line)
        for line in (run_directory / "request_manifest.jsonl").read_text().splitlines()
    ]
    assert records[-1]["status"] == 429 and records[-1]["retry_after_seconds"] == 7
    assert (run_directory / "raw/0004-activities.bin").read_bytes() == limited.body
    manifest = read_manifest(run_directory)
    assert manifest["terminal"] == {
        "complete": False,
        "reason": "rate_limited",
        "detail": "",
    }
    assert not (run_directory / "normalized").exists()
    assert "run_incomplete" in verify_run(run_directory).findings


@pytest.mark.parametrize("status", [401, 403, 500, 503, 302])
def test_other_statuses_stop_the_run(tmp_path: Path, status: int) -> None:
    script = [RawResponse(status=status, body=b"synthetic", headers={})]
    capture = capture_config()
    fetcher, _, governance = governed(tmp_path, capture, script)
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="bad-status",
        )
    assert stopped.value.reason == f"http_status_{status}"


def test_wrong_account_stops_before_any_further_read(tmp_path: Path) -> None:
    script = [ok(account_body(**{"id": "SYNTH-OTHER", NUMBER_KEY: "SYNTH-OTHER-2"}))]
    capture = capture_config()
    fetcher, inner, governance = governed(tmp_path, capture, script)
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="mismatch",
        )
    assert stopped.value.reason == "account_binding_mismatch"
    assert len(inner.calls) == 1


def test_unknown_activity_type_refuses_but_keeps_raw_bytes(tmp_path: Path) -> None:
    bad = [fill(1), nontrade(2, "NEWTYPE", "1")]
    script = [ok(account_body()), ok(positions_body()), ok(page(bad)), ok(page([]))]
    capture = capture_config()
    fetcher, _, governance = governed(tmp_path, capture, script)
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="unknown",
        )
    assert stopped.value.reason == "normalization_refused"
    run_directory = Path(stopped.value.run_directory)
    assert (run_directory / "raw/0003-activities.bin").read_bytes() == page(bad)
    assert not (run_directory / "normalized").exists()
    assert "unknown activity type" in read_manifest(run_directory)["terminal"]["detail"]


def test_non_array_activities_response_is_unparseable(tmp_path: Path) -> None:
    script = [ok(account_body()), ok(positions_body()), ok(b'{"message": "x"}')]
    capture = capture_config()
    fetcher, _, governance = governed(tmp_path, capture, script)
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="shape",
        )
    assert stopped.value.reason == "unparseable_response"


def test_pagination_guards(tmp_path: Path) -> None:
    capture = capture_config(max_pages=2)
    fetcher, _, governance = governed(
        tmp_path,
        capture,
        [
            ok(account_body()),
            ok(positions_body()),
            ok(page(PAGE_ONE)),
            ok(page(PAGE_TWO)),
        ],
    )
    with pytest.raises(CaptureIncomplete) as limit:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="limit",
        )
    assert limit.value.reason == "max_pages_reached"

    stuck = [fill(3), PAGE_ONE[-1]]
    fetcher, _, governance = governed(
        tmp_path,
        capture_config(),
        [ok(account_body()), ok(positions_body()), ok(page(PAGE_ONE)), ok(page(stuck))],
        name="evidence-2.json",
    )
    with pytest.raises(CaptureIncomplete) as loop:
        run_capture(
            config=capture_config(),
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="loop",
        )
    assert loop.value.reason == "cursor_not_advancing"


def test_budget_exhaustion_stops_before_the_next_request(tmp_path: Path) -> None:
    capture = capture_config()
    fetcher, inner, governance = governed(
        tmp_path,
        capture,
        _happy_script(),
        metering={"max_requests": 2, "retry_count": 0},
    )
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="budget",
        )
    assert stopped.value.reason == "governance_refused"
    assert len(inner.calls) == 2


def test_transport_failure_is_recorded(tmp_path: Path) -> None:
    class Broken:
        def fetch(self, path: str, query: Mapping[str, str]) -> RawResponse:
            raise CaptureError("synthetic transport failure")

    capture = capture_config()
    _, _, governance = governed(tmp_path, capture, [])
    with pytest.raises(CaptureIncomplete) as stopped:
        run_capture(
            config=capture,
            fetcher=GovernedFetcher(Broken(), governance),
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="broken",
        )
    assert stopped.value.reason == "transport_failure"


def test_governed_fetcher_never_forwards_a_mutation(tmp_path: Path) -> None:
    capture = capture_config()
    fetcher, inner, governance = governed(tmp_path, capture, [ok(b"[]")])
    for method, path in (
        ("POST", "/v2/orders"),
        ("DELETE", "/v2/positions"),
        ("GET", "/v2/orders"),
        ("PUT", "/v2/account"),
    ):
        with pytest.raises(CaptureRefused):
            fetcher.get(method, path, {})
    assert inner.calls == [] and governance.used_requests == 0


def test_staging_is_create_only(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, _happy_script(), run_id="once")
    capture = capture_config()
    fetcher, inner, governance = governed(
        tmp_path, capture, _happy_script(), name="evidence-2.json"
    )
    before = (result.run_directory / "manifest.json").read_bytes()
    with pytest.raises(CaptureError, match="create-only"):
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="once",
        )
    assert inner.calls == []
    assert (result.run_directory / "manifest.json").read_bytes() == before
    with pytest.raises(CaptureRefused):
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=Path("relative-root"),
            run_id="rel",
        )
    with pytest.raises(CaptureRefused):
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=_staging(tmp_path),
            run_id="../escape",
        )


def _tamper_cases(root: Path) -> dict[str, Any]:
    return {
        "raw": lambda: (root / "raw/0003-activities.bin").write_bytes(b"[]"),
        "normalized": lambda: (root / "normalized/activities.jsonl").write_bytes(b""),
        "stray": lambda: (root / "raw/stray.bin").write_bytes(b"x"),
        "deleted": lambda: (root / "raw/0001-account.bin").unlink(),
        "requests": lambda: (root / "request_manifest.jsonl").write_bytes(b""),
    }


@pytest.mark.parametrize("case", ["raw", "normalized", "stray", "deleted", "requests"])
def test_verifier_detects_tampering(tmp_path: Path, case: str) -> None:
    result, _ = _run(tmp_path, _happy_script())
    assert verify_run(result.run_directory).ok
    _tamper_cases(result.run_directory)[case]()
    assert not verify_run(result.run_directory).ok


def test_verifier_detects_manifest_edit_and_missing_manifest(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, _happy_script())
    manifest = result.run_directory / "manifest.json"
    manifest.write_bytes(
        manifest.read_bytes().replace(b'"complete":true', b'"complete":false')
    )
    assert verify_run(result.run_directory).findings == ("manifest_digest_mismatch",)
    assert verify_run(tmp_path / "nothing").findings == ("manifest_missing",)


def test_verifier_rederives_normalized_output_independently(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, _happy_script())
    target = result.run_directory / "normalized/activities.jsonl"
    lines = target.read_bytes().splitlines(keepends=True)
    forged = b"".join(lines[1:])  # drop one row but keep every digest consistent
    manifest_path = result.run_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    import hashlib

    manifest["normalized_files"]["normalized/activities.jsonl"] = hashlib.sha256(
        forged
    ).hexdigest()
    target.write_bytes(forged)
    new_manifest = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_path.write_bytes(new_manifest)
    (result.run_directory / "MANIFEST.sha256").write_text(
        hashlib.sha256(new_manifest).hexdigest() + "\n"
    )
    assert "normalized_output_mismatch" in verify_run(result.run_directory).findings
