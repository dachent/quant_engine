from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.grizzlybulls_capture import (
    GrizzlyCaptureError,
    Recorder,
    shape_of,
    verify_run,
)
from tools.grizzlybulls_schwab_crosscheck import (
    CrosscheckError,
    Fill,
    crosscheck,
    load_fills,
)
from quant_engine.web_contracts.grizzlybulls import parse_trade_history_csv

from test_grizzlybulls_contract import (
    ROWS,
    history_csv,
    page,
    sheet_rows,
)

SLUG = "x-model"
BASE = "https://grizzlybulls.com"
AUTH = ["authorization", "accept"]
SENTINEL_SESSION_VALUE = "synthetic-session-value-never-persisted"


def _recorder(tmp_path: Path, mode: str = "capture") -> Recorder:
    return Recorder(tmp_path / "run", mode=mode)


def _feed_full(rec: Recorder, history: bytes | None = None) -> None:
    def get(path: str, body: bytes, *, ctype: str, headers: list[str]) -> None:
        rec.handle(
            method="GET",
            url=BASE + path,
            status=200,
            content_type=ctype,
            request_header_names=headers,
            read_body=lambda: body,
        )

    get(
        "/api/models",
        b'[{"slug":"x-model","privilege":"gold"}]',
        ctype="application/json",
        headers=[],
    )
    get(
        f"/_next/data/B/models/{SLUG}.json?slug={SLUG}",
        json.dumps(page(sheet_rows())).encode(),
        ctype="application/json",
        headers=[],
    )
    for mode in ("blended", "live"):
        get(
            f"/api/modelTradeHistory?slug={SLUG}&mode={mode}",
            history or history_csv(ROWS),
            ctype="text/csv; charset=utf-8",
            headers=AUTH,
        )


def test_capture_persists_raw_bytes_untouched_and_manifest_last(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    raw = history_csv(ROWS)
    _feed_full(rec, raw)
    manifest = rec.finish(expected_slugs=[SLUG])
    assert manifest["status"] == "COMPLETE"
    root = tmp_path / "run"
    raws = sorted((root / "raw").glob("*history*"))
    assert [p.read_bytes() for p in raws] == [raw, raw]
    assert hashlib.sha256(raw).hexdigest() in (root / "manifest.json").read_text()
    assert verify_run(root) == {"status": "COMPLETE", "artifact_count": 5}


def test_identity_routes_are_never_read_and_headers_never_persisted(
    tmp_path: Path,
) -> None:
    rec = _recorder(tmp_path)

    def boom() -> bytes:
        raise AssertionError("body of a denied route was read")

    for path in ("/api/user", "/api/login", "/api/logout", "/api/create-portal-link"):
        rec.handle(
            method="POST",
            url=BASE + path,
            status=200,
            content_type="application/json",
            request_header_names=["authorization", "cookie"],
            read_body=boom,
        )
    rec.handle(
        method="GET",
        url="https://other.invalid/x",
        status=200,
        content_type="text/csv",
        request_header_names=[],
        read_body=boom,
    )
    manifest = rec.finish()
    assert manifest["skipped_offorigin_count"] == 1
    assert not (tmp_path / "run" / "raw").exists()
    assert (
        SENTINEL_SESSION_VALUE not in (tmp_path / "run" / "manifest.json").read_text()
    )


def test_failures_fail_closed_but_optional_historical_does_not(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    _feed_full(rec)
    rec.handle(
        method="GET",
        url=f"{BASE}/api/modelsHistorical?slug={SLUG}",
        status=500,
        content_type="application/json",
        request_header_names=AUTH,
        read_body=lambda: b'{"message":"boom"}',
    )
    assert rec.finish(expected_slugs=[SLUG])["status"] == "COMPLETE"

    other = Recorder(tmp_path / "other", mode="capture")
    other.handle(
        method="GET",
        url=f"{BASE}/api/modelTradeHistory?slug={SLUG}&mode=live",
        status=403,
        content_type="application/json",
        request_header_names=AUTH,
        read_body=lambda: b'{"message":"no"}',
    )
    assert other.finish(expected_slugs=[SLUG])["status"] == "FAILED_REQUIRED_CAPABILITY"


def test_unauthenticated_gated_request_and_drifted_body_are_failures(
    tmp_path: Path,
) -> None:
    rec = _recorder(tmp_path)
    rec.handle(
        method="GET",
        url=f"{BASE}/api/modelTradeHistory?slug={SLUG}&mode=live",
        status=200,
        content_type="text/csv",
        request_header_names=[],
        read_body=lambda: history_csv(ROWS),
    )
    rec.handle(
        method="GET",
        url=f"{BASE}/api/modelTradeHistory?slug={SLUG}&mode=blended",
        status=200,
        content_type="text/csv",
        request_header_names=AUTH,
        read_body=lambda: history_csv(ROWS).replace(b"Sell", b"Hold"),
    )
    manifest = rec.finish(expected_slugs=[SLUG])
    assert manifest["status"] == "FAILED_REQUIRED_CAPABILITY"
    assert len(manifest["failures"]) == 2  # type: ignore[arg-type]


def test_missing_capability_is_incomplete(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.handle(
        method="GET",
        url=BASE + "/api/models",
        status=200,
        content_type="application/json",
        request_header_names=[],
        read_body=lambda: b'[{"slug":"x-model","privilege":"gold"}]',
    )
    assert (
        rec.finish(expected_slugs=[SLUG])["status"] == "INCOMPLETE_MISSING_CAPABILITY"
    )


def test_verify_rejects_tamper_and_strays(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    _feed_full(rec)
    rec.finish(expected_slugs=[SLUG])
    root = tmp_path / "run"
    (root / "stray.txt").write_text("x")
    with pytest.raises(GrizzlyCaptureError):
        verify_run(root)
    (root / "stray.txt").unlink()
    target = next((root / "raw").glob("*live*"))
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(GrizzlyCaptureError):
        verify_run(root)


def test_discovery_records_shape_never_values(tmp_path: Path) -> None:
    rec = Recorder(tmp_path / "disc", mode="discover")
    rec.handle(
        method="GET",
        url=f"{BASE}/api/somethingNew?token=abc",
        status=200,
        content_type="application/json",
        request_header_names=["Authorization", "Cookie"],
        read_body=lambda: json.dumps(
            {"rows": [{"symbol": "SECRETISH-VALUE"}]}
        ).encode(),
    )
    manifest = rec.finish()
    text = (tmp_path / "disc" / "manifest.json").read_text()
    assert "SECRETISH-VALUE" not in text and "abc" not in text
    entry = manifest["entries"][0]  # type: ignore[index]
    assert entry["query_keys"] == ["token"]
    assert entry["request_header_names"] == ["authorization", "cookie"]
    assert shape_of({"a": [1, 2], "b": "x"}) == {"a": ["int", "len=2"], "b": "str"}


def test_output_root_must_be_new_and_outside_git(tmp_path: Path) -> None:
    (tmp_path / "exists").mkdir()
    with pytest.raises(GrizzlyCaptureError):
        Recorder(tmp_path / "exists", mode="capture")
    (tmp_path / "work" / ".git").mkdir(parents=True)
    with pytest.raises(GrizzlyCaptureError):
        Recorder(tmp_path / "work" / "run", mode="capture")
    with pytest.raises(GrizzlyCaptureError):
        Recorder(tmp_path / "ok", mode="other")


def _catalog(rows: list[tuple[str, str, str]], held: str) -> dict[str, object]:
    return {
        "transactions": [
            {
                "account_id": "account-1",
                "symbol": "SPXL",
                "kind": kind,
                "event_date": day,
                "quantity": qty,
            }
            for day, kind, qty in rows
        ]
        + [
            {
                "account_id": "account-1",
                "symbol": "OTHER",
                "kind": "Buy",
                "event_date": "2030-01-01",
                "quantity": "1",
            }
        ],
        "positions": [{"account_id": "account-1", "symbol": "SPXL", "quantity": held}],
    }


def test_crosscheck_measures_lag_and_state_disagreement() -> None:
    series = parse_trade_history_csv(history_csv(ROWS))
    # fills: sell 1 day after the Sell signal, buy 2 days after the Rebuy, then nothing
    catalog = _catalog(
        [("2030-01-03", "Sell", "10"), ("2030-01-05", "Buy", "10")],
        held="10",
    )
    fills, held = load_fills(catalog, "account-1", "SPXL")
    report = crosscheck(series, fills, held)
    assert report["matched_fill_count"] == 2
    assert report["lag_days"] == {"min": 1, "max": 2, "median": 2}
    assert report["unmatched_fills"] == []
    # signals on 01-09 (Sell) and 01-10 (Rebuy) have no fill
    assert report["signals_in_window_without_fill"] == [
        "2030-01-09T14:00:00",
        "2030-01-10T07:00:00",
    ]
    # 01-03 and 01-04 (fill lags the Rebuy), 01-09 (vendor flat, account long)
    assert report["weekday_state_disagreement_days"] == 3
    assert "not fills" in str(report["disclaimer"])


def test_crosscheck_flags_unmatched_fill_and_refuses_bad_catalogs() -> None:
    series = parse_trade_history_csv(history_csv(ROWS))
    fills = [
        Fill(
            __import__("datetime").date(2030, 3, 1),
            "Buy",
            __import__("decimal").Decimal(1),
        )
    ]
    assert crosscheck(series, fills, fills[0].quantity)["unmatched_fills"] == [
        "2030-03-01"
    ]
    with pytest.raises(CrosscheckError):
        load_fills(_catalog([], "1"), "account-1", "SPXL")
    with pytest.raises(CrosscheckError):
        load_fills([], "a", "b")
