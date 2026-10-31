from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import tt_order_planner as planner  # noqa: E402
import tt_source_manifest_builder as builder  # noqa: E402

SLUGS = builder.GOVERNED_SLUGS
CHART_LAST = "09/01/2026"
WORKBOOK_SHA = hashlib.sha256(b"synthetic-workbook").hexdigest()


def _put(root: Path, name: str, value: object) -> bytes:
    data = (
        value.encode()
        if isinstance(value, str)
        else json.dumps(value, separators=(",", ":")).encode()
    )
    (root / name).write_bytes(data)
    return data


def _row(name: str, data: bytes, modified: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "object": name,
        "url": f"https://example.invalid/o/{name}?alt=media",
        "http_status": 200,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "etag": '"synthetic"',
    }
    if modified:
        row["last_modified"] = modified
    return row


def _holdings(rows: list[tuple[str, str]]) -> list[list[str]]:
    return [["Date", "Allocation"], *[[d, a] for d, a in rows]]


def _capture(
    tmp_path: Path,
    *,
    truncated_slug: str | None = None,
    late_symbol_slug: str | None = None,
    drift_slug: str | None = None,
    latest_short_slug: str | None = None,
) -> tuple[Path, Path]:
    jroot = tmp_path / "run"
    troot = tmp_path / "run-txt"
    jroot.mkdir()
    troot.mkdir()
    rows = []
    comp = {
        "slug": list(SLUGS),
        "portfolios": [f"Synthetic {s}" for s in SLUGS],
        "nav-end": [1100.0 + i for i in range(6)],
        "date-end": [CHART_LAST] * 6,
        "updated": ["2026-09-02T01:00Z"] * 6,
    }
    rows.append(
        _row(
            "portfolio-comp.json",
            _put(jroot, "portfolio-comp.json", comp),
            "Wed, 02 Sep 2026 01:00:00 GMT",
        )
    )
    trows = []
    for i, slug in enumerate(SLUGS):
        history = [
            ("8/31/2026 4:00:00 PM", "SPY=60.00%, BIL=40.00%"),
            ("9/1/2026 4:00:00 PM", "SPY=50.00%, BIL=50.00%"),
        ]
        if slug == late_symbol_slug:
            history[-1] = ("9/1/2026 4:00:00 PM", "SPY=50.00%, XYZ=50.00%")
        if slug == drift_slug:
            history[0] = ("8/31/2026 4:00:00 PM", "SPY=58.00%, BIL=40.00%")
        if slug == latest_short_slug:
            history[-1] = ("9/1/2026 4:00:00 PM", "SPY=50.00%")
        if slug == truncated_slug:
            history = [
                ("8/30/2026 4:00:00 PM", "SPY=60.00%, BIL=40.00%"),
                ("8/31/2026 4:00:00 PM", "SPY=60.00%"),
                ("9/1/2026 4:00:00 PM", "SPY=50.00%, BIL=50.00%"),
            ]
        files: dict[str, object] = {
            "performance-chart.json": {
                "labels": ["Date", "S", "B"],
                "data": [
                    ["08/31/2026", 1000.0, 1000.0],
                    [CHART_LAST, 1100.0 + i, 1001.0],
                ],
            },
            "performance-table.json": [["Metric", "", "S"], ["End", CHART_LAST, "$1"]],
            "holdings-history.json": _holdings(history),
            "portfolio-holdings.json": [
                ["Symbol", "Name", "Allocation", "Price"],
                ["SPY", "SPY", "50.00%", "$100.00"],
                ["BIL", "BIL", "50.00%", "$91.00"],
            ],
        }
        for obj, value in files.items():
            name = f"{slug}__{obj}"
            rows.append(
                _row(name, _put(jroot, name, value), "Wed, 02 Sep 2026 01:00:00 GMT")
            )
        for obj, text in (
            ("last-update.txt", "2026-09-03T02:57Z"),
            ("last-rebalance.txt", "September 1, 2026"),
        ):
            name = f"{slug}__{obj}"
            data = _put(troot, name, text)
            row = _row(f"{slug}/{obj}", data)
            row["text"] = text
            del row["url"], row["etag"]
            trows.append(row)
    vane = [["Key", "Value"], ["MarketVane", "1"], ["StockMom", "1.5"],
            ["EcoMom", "2.5"], ["StockPref", "3.5"], ["VixMargin", "4.5"],
            ["DdMargin", "5.5"]]  # fmt: skip
    rows.append(
        _row(
            "tt-market-vane__status.json",
            _put(jroot, "tt-market-vane__status.json", vane),
            "Wed, 02 Sep 2026 01:00:00 GMT",
        )
    )
    (jroot / "capture-manifest.json").write_text(
        json.dumps({"captured_at_utc": "2026-09-03T02:39:48+00:00", "objects": rows})
    )
    (troot / "capture-manifest.json").write_text(
        json.dumps({"captured_at_utc": "2026-09-03T02:41:00+00:00", "objects": trows})
    )
    return jroot, troot


def _tape(tmp_path: Path, last: str = "2026-09-01") -> tuple[Path, Path]:
    tape = tmp_path / "tape.json"
    data = json.dumps(
        {
            "provider": "yahoo",
            "dataset": "history_adjusted",
            "rows": [
                {"Ticker": t, "Date": d, "Close": 1.0, "Adj Close": 1.0}
                for t in ("BIL", "SPY")
                for d in ("2026-08-28T00:00:00", f"{last}T00:00:00")
            ],
        }
    ).encode()
    tape.write_bytes(data)
    man = tmp_path / "tape.json.manifest.json"
    man.write_text(
        json.dumps(
            {
                "artifact": {
                    "name": "tape.json",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                },
                "coverage": {"missing_tickers": []},
                "request": {
                    "dataset": "history_adjusted",
                    "provider": "yahoo",
                    "transport": "yfinance",
                    "tickers": ["BIL", "SPY"],
                },
                "schema_version": 1,
                "source_run_id": "synthetic-run",
            }
        )
    )
    return tape, man


def _args(tmp_path: Path, jroot: Path, troot: Path, **extra: str) -> list[str]:
    tape, man = _tape(tmp_path, extra.pop("tape_last", "2026-09-01"))
    argv = [
        "build",
        "--capture-dir", str(jroot),
        "--txt-capture-dir", str(troot),
        "--out-dir", str(tmp_path / "out"),
        "--min-capture-date", "2026-09-01",
        "--portfolio-version", "synthetic-v1",
        "--live-inception", "2026-08-31",
        "--price-tape", str(tape),
        "--price-tape-manifest", str(man),
        "--owner-workbook-sha256", WORKBOOK_SHA,
    ]  # fmt: skip
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return argv


def _build(tmp_path: Path, **extra: str) -> int:
    jroot, troot = _capture(tmp_path)
    return builder.main(_args(tmp_path, jroot, troot, **extra))


def _manifest(tmp_path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (tmp_path / "out" / builder.MANIFEST_NAME).read_bytes()
    )
    return loaded


def test_build_verify_and_existing_readers_accept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _build(tmp_path) == 0
    out = tmp_path / "out"
    assert builder.main(["verify", "--out-dir", str(out)]) == 0
    assert builder.main(["read-compat", "--out-dir", str(out)]) == 0
    assert '"accepted": true' in capsys.readouterr().out
    prov = _manifest(tmp_path)[builder.PROVENANCE_KEY]
    assert prov["captures"][0]["captured_at_utc"] == "2026-09-03T02:39:48Z"
    assert prov["objects"][builder.GLOBAL_OBJECT]["last_modified_utc"].startswith(
        "2026-09-02T01:00:00"
    )
    assert prov["holdings_row_validation"]["flags"] == []
    assert not (tmp_path / "out.partial").exists()


def test_output_is_create_only(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    argv = _args(tmp_path, jroot, troot)
    assert builder.main(argv) == 0
    assert builder.main(argv) == 2


def test_refuses_changed_bytes_status_and_missing(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    victim = jroot / f"{SLUGS[0]}__performance-table.json"
    victim.write_bytes(victim.read_bytes().replace(b"Metric", b"Metrix"))
    assert builder.main(_args(tmp_path, jroot, troot)) == 2
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "out.partial").exists()


def test_refuses_non_200_and_missing_object(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    path = jroot / "capture-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["objects"][1]["http_status"] = 304
    path.write_text(json.dumps(manifest))
    assert builder.main(_args(tmp_path, jroot, troot)) == 2
    manifest["objects"][1]["http_status"] = 200
    victim = manifest["objects"].pop(1)
    (jroot / victim["object"]).unlink()
    path.write_text(json.dumps(manifest))
    assert builder.main(_args(tmp_path, jroot, troot)) == 2


def test_refuses_extra_slug_unless_listed(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    data = _put(jroot, "tt-other__performance-chart.json", [])
    path = jroot / "capture-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["objects"].append(_row("tt-other__performance-chart.json", data))
    path.write_text(json.dumps(manifest))
    assert builder.main(_args(tmp_path, jroot, troot)) == 2


def test_refuses_stale_capture_and_token_url(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    argv = _args(tmp_path, jroot, troot)
    argv[argv.index("--min-capture-date") + 1] = "2026-10-01"
    assert builder.main(argv) == 2
    path = jroot / "capture-manifest.json"
    path.write_text(path.read_text().replace("alt=media", "alt=media&token=x"))
    argv[argv.index("--min-capture-date") + 1] = "2026-09-01"
    assert builder.main(argv) == 2


def test_market_vane_missing_needs_explicit_file(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    vane = jroot / "tt-market-vane__status.json"
    vane_bytes = vane.read_bytes()
    path = jroot / "capture-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["objects"] = [
        r for r in manifest["objects"] if r["object"] != "tt-market-vane__status.json"
    ]
    path.write_text(json.dumps(manifest))
    vane.unlink()
    assert builder.main(_args(tmp_path, jroot, troot)) == 2
    explicit = tmp_path / "vane.json"
    explicit.write_bytes(vane_bytes)
    extra = {
        "market_vane_status": str(explicit),
        "market_vane_captured_at": "2026-09-03T00:00:00Z",
    }
    assert builder.main(_args(tmp_path, jroot, troot, **extra)) == 0
    prov = _manifest(tmp_path)[builder.PROVENANCE_KEY]
    assert prov["market_vane"]["source"] == "EXPLICIT_FILE"


def test_price_tape_gap_needs_label(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    assert builder.main(_args(tmp_path, jroot, troot, tape_last="2026-08-31")) == 2
    assert (
        builder.main(
            _args(
                tmp_path,
                jroot,
                troot,
                tape_last="2026-08-31",
                price_tape_gap_label="tape-ends-0831",
            )
        )
        == 0
    )
    prov = _manifest(tmp_path)[builder.PROVENANCE_KEY]
    assert prov["price_tape_gap"] == {
        "absent_symbols_through_chart_end": [],
        "chart_last_date": "2026-09-01",
        "label": "tape-ends-0831",
        "tape_observed_end": "2026-08-31",
    }
    assert builder.main(["verify", "--out-dir", str(tmp_path / "out")]) == 0


def test_gap_label_covers_a_symbol_the_tape_never_carries(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path, late_symbol_slug=SLUGS[0])
    assert builder.main(_args(tmp_path, jroot, troot)) == 2
    argv = _args(tmp_path, jroot, troot, price_tape_gap_label="xyz-not-on-tape")
    assert builder.main(argv) == 0
    manifest = _manifest(tmp_path)
    assert manifest["price_tape"]["target"]["observed_end"] == "2026-08-31"
    gap = manifest[builder.PROVENANCE_KEY]["price_tape_gap"]
    assert gap["absent_symbols_through_chart_end"] == ["XYZ"]
    assert builder.main(["verify", "--out-dir", str(tmp_path / "out")]) == 0


def test_market_vane_provenance_keeps_capture_and_modified_times(
    tmp_path: Path,
) -> None:
    assert _build(tmp_path) == 0
    vane = _manifest(tmp_path)[builder.PROVENANCE_KEY]["market_vane"]
    assert vane["captured_at_utc"] == "2026-09-03T02:39:48Z"
    assert vane["source_last_modified_utc"] == "2026-09-02T01:00:00Z"


def test_verify_detects_tamper(tmp_path: Path) -> None:
    assert _build(tmp_path) == 0
    out = tmp_path / "out"
    victim = out / builder.VIEW_DIR / f"{SLUGS[0]}__last-update.txt"
    victim.write_bytes(b"2026-09-03T02:58Z")
    assert builder.main(["verify", "--out-dir", str(out)]) == 2


def test_verify_detects_edited_provenance(tmp_path: Path) -> None:
    assert _build(tmp_path) == 0
    out = tmp_path / "out"
    manifest = _manifest(tmp_path)
    manifest[builder.PROVENANCE_KEY]["min_capture_date"] = "2020-01-01"
    (out / builder.MANIFEST_NAME).write_bytes(builder._canonical(manifest))
    assert builder.main(["verify", "--out-dir", str(out)]) == 2


def test_dropped_position_row_is_truncated_not_normalised(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path, truncated_slug=SLUGS[-1])
    assert builder.main(_args(tmp_path, jroot, troot)) == 0
    manifest = _manifest(tmp_path)
    validation = manifest[builder.PROVENANCE_KEY]["holdings_row_validation"]
    assert validation["flags"] == [
        {
            "class": "SYMBOL_DROP",
            "fallback": {"kind": "LAST_COMPLETE_ROW", "observed_date": "2026-08-30"},
            "fallback_corroborated_by_live_portfolio_holdings": False,
            "flag": "TRUNCATED_ROW",
            "observed_date": "2026-08-31",
            "slug": SLUGS[-1],
        }
    ]
    assert validation["counts_by_class"] == {SLUGS[-1]: {"SYMBOL_DROP": 1}}
    snapshots = manifest["selected_components"][-1]["dated_holdings"]["snapshots"]
    assert {"SPY": 0.6} in [row["weights"] for row in snapshots]
    assert builder.main(["verify", "--out-dir", str(tmp_path / "out")]) == 0


def test_rounding_or_cash_row_is_not_truncation(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path, drift_slug=SLUGS[1])
    assert builder.main(_args(tmp_path, jroot, troot)) == 0
    validation = _manifest(tmp_path)[builder.PROVENANCE_KEY]["holdings_row_validation"]
    assert [f["flag"] for f in validation["flags"]] == ["ALLOCATION_SUM_OFF"]
    assert validation["counts_by_class"] == {SLUGS[1]: {"ROUNDING_OR_CASH": 1}}


def test_latest_short_row_is_explained_by_live_holdings(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path, latest_short_slug=SLUGS[2])
    assert builder.main(_args(tmp_path, jroot, troot)) == 0
    (flag,) = _manifest(tmp_path)[builder.PROVENANCE_KEY]["holdings_row_validation"][
        "flags"
    ]
    assert flag["class"] == "LIVE_SYMBOL_MISSING"
    assert flag["flag"] == "TRUNCATED_ROW"
    assert flag["fallback"] == {"kind": "LIVE_PORTFOLIO_HOLDINGS"}


def test_over_allocation_class_and_tolerances_are_data() -> None:
    comp = {
        "slug": "s",
        "current_holdings": [{"symbol": "A", "weight": 1.0}],
        "dated_holdings": {
            "snapshots": [
                {"observed_date": "2026-01-01", "weights": {"A": 1.0}},
                {"observed_date": "2026-01-02", "weights": {"A": 1.2}},
            ]
        },
    }
    narrow = builder._holdings_validation([comp], 0.005, 0.02)
    assert narrow["counts_by_class"] == {"s": {"OVER_ALLOCATION": 1}}
    assert narrow["allocation_sum_tolerance"] == 0.005
    assert builder._holdings_validation([comp], 0.3, 0.02)["flags"] == []


def test_tolerance_is_recorded_data(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path, truncated_slug=SLUGS[0])
    argv = _args(tmp_path, jroot, troot, allocation_sum_tolerance="0.49")
    assert builder.main(argv) == 0
    validation = _manifest(tmp_path)[builder.PROVENANCE_KEY]["holdings_row_validation"]
    assert validation["allocation_sum_tolerance"] == 0.49
    assert validation["flags"] == []


def test_caller_supplied_labels_record_the_owner_workbook(tmp_path: Path) -> None:
    assert _build(tmp_path) == 0
    caller = _manifest(tmp_path)[builder.PROVENANCE_KEY]["caller_supplied"]
    assert caller["portfolio_version"] == "synthetic-v1"
    assert caller["live_inception"] == "2026-08-31"
    assert caller["component_weights"]["owner_workbook_sha256"] == WORKBOOK_SHA
    assert caller["component_weights"]["basis"] == "EQUAL_1_OVER_N"


def test_vane_capture_dir_supplies_the_status(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    vane = jroot / "tt-market-vane__status.json"
    data = vane.read_bytes()
    path = jroot / "capture-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["objects"] = [
        r for r in manifest["objects"] if r["object"] != "tt-market-vane__status.json"
    ]
    path.write_text(json.dumps(manifest))
    vane.unlink()
    vroot = tmp_path / "run-vane"
    vroot.mkdir()
    (vroot / "tt-market-vane__status.json").write_bytes(data)
    row = _row("tt-market-vane/status.json", data, "Wed, 02 Sep 2026 01:00:00 GMT")
    row["captured_at_utc"] = "2026-09-03T03:00:00+00:00"
    (vroot / "capture-manifest.json").write_text(json.dumps({"objects": [row]}))
    argv = _args(tmp_path, jroot, troot, vane_capture_dir=str(vroot))
    assert builder.main(argv) == 0
    prov = _manifest(tmp_path)[builder.PROVENANCE_KEY]
    assert prov["market_vane"]["source"] == "CAPTURE_INVENTORY"
    assert prov["market_vane"]["captured_at_utc"] == "2026-09-03T03:00:00Z"


def test_legacy_shape_requires_flag(tmp_path: Path) -> None:
    jroot, troot = _capture(tmp_path)
    for root in (jroot, troot):
        path = root / "capture-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["captured_at"] = manifest.pop("captured_at_utc")
        for row in manifest["objects"]:
            for key in ("sha256", "http_status", "url", "etag", "last_modified"):
                row.pop(key, None)
        path.write_text(json.dumps(manifest))
    argv = _args(tmp_path, jroot, troot)
    assert builder.main(argv) == 2
    assert builder.main([*argv, "--accept-legacy-capture-shape"]) == 0
    prov = _manifest(tmp_path)[builder.PROVENANCE_KEY]
    assert prov["capture_shape"] == builder.SHAPE_LEGACY
    assert builder.main(["verify", "--out-dir", str(tmp_path / "out")]) == 0


# ---- order planner (Refs #609) -------------------------------------------


def _o(symbol: str, amount: str) -> planner.Order:
    return planner.Order(symbol, Decimal(amount))


def test_sells_precede_buys_and_fund_them() -> None:
    batch = planner.order_batch([_o("SPY", "500")], [_o("IBIT", "450")], Decimal(0))
    assert [side for side, _ in batch.sequence] == ["SELL", "BUY"]
    assert batch.rejected_buys == ()
    assert batch.cash_after == Decimal(50)


def test_unaffordable_buy_is_rejected_whole_never_partial() -> None:
    batch = planner.order_batch([], [_o("IBIT", "150"), _o("BIL", "60")], Decimal(100))
    assert [o.symbol for _, o in batch.sequence] == ["BIL"]
    (rejection,) = batch.rejected_buys
    assert rejection.order == _o("IBIT", "150")
    assert rejection.reason == planner.INSUFFICIENT_CASH
    assert rejection.available_cash == Decimal(100)
    assert batch.cash_after == Decimal(40)


def test_buying_power_rejection_when_proceeds_are_not_spendable() -> None:
    sells, buys = [_o("SPY", "500")], [_o("IBIT", "450")]
    batch = planner.order_batch(sells, buys, Decimal(10), count_sell_proceeds=False)
    assert [side for side, _ in batch.sequence] == ["SELL"]
    assert batch.rejected_buys[0].order.symbol == "IBIT"


def test_planner_refuses_bad_input() -> None:
    with pytest.raises(ValueError):
        planner.order_batch([], [_o("A", "0")], Decimal(1))
    with pytest.raises(ValueError):
        planner.order_batch([_o("A", "1")], [_o("A", "1")], Decimal(1))
    with pytest.raises(ValueError):
        planner.order_batch([], [], Decimal(-1))
