"""The per-cycle base-current bundle is generated from pinned inputs, reproducibly."""

from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Any
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import base_bundle_builder as bb  # noqa: E402

N = 130
TICKERS = ("BIL", "SPY", "IWM", "HYG")
DRIFT = {"BIL": 0.00015, "SPY": 0.0004, "IWM": -0.0003, "HYG": 0.0002}
LEDGER = {
    "provider-ledger-generation.json": b'{"generation": "synthetic"}\n',
    "provider-ledger.json": b'{"catalog": "synthetic"}\n',
    "provider-ledger-source-proof.json": b'{"proof": "synthetic"}\n',
    "strategy-evidence.json": b'{"evidence": "synthetic"}\n',
}
SERIES = "Series A"


def _days() -> list[date]:
    out: list[date] = []
    day = date(2026, 1, 1)
    while len(out) < N:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _write(path: Path, data: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _capture_rows(*, dividend_day: int | None = N - 8) -> list[dict[str, Any]]:
    rows = []
    for ticker in TICKERS:
        price = 50.0 + TICKERS.index(ticker)
        for n, day in enumerate(_days()):
            pays = ticker in ("BIL", "HYG") and dividend_day is not None
            due = dividend_day is not None and n % 21 == dividend_day % 21
            dividend = 0.02 if pays and due else 0.0
            wiggle = 0.0 if ticker == "BIL" else 0.001 * ((n % 5) - 2)
            if n:
                price = round(price * (1 + DRIFT[ticker] + wiggle), 4)
            rows.append(
                {
                    "Ticker": ticker,
                    "Date": f"{day.isoformat()}T00:00:00",
                    "Close": price,
                    "Adj Close": price,
                    "Dividends": dividend,
                }
            )
    return rows


def _capture_dir(root: Path, rows: list[dict[str, Any]]) -> Path:
    capture = root / "capture"
    files: dict[str, bytes] = {}
    for name in bb.CAPTURE_ARTIFACT.values():
        payload = {"dataset": name, "rows": rows}
        files[f"artifacts/{name}"] = _write(
            capture / "artifacts" / name, _dump(payload)
        )
        files[f"artifacts/{name}.manifest.json"] = _write(
            capture / "artifacts" / f"{name}.manifest.json", _dump({"of": name})
        )
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(["Ticker", "Date", "Close", "Adj Close", "Dividends"])
    for row in rows:
        writer.writerow(
            [row["Ticker"], row["Date"], row["Close"], repr(row["Adj Close"]), 0]
        )
    files["raw/base-proxies/history.csv"] = _write(
        capture / "raw/base-proxies/history.csv", text.getvalue().encode()
    )
    _write(
        capture / "MANIFEST.json",
        _dump(
            {
                "adapter_sha256": "a" * 64,
                "config_effective_sha256": "c" * 64,
                "cutoff": _days()[-1].isoformat(),
                "fetch_completed_utc": "2026-07-08T00:00:02Z",
                "fetch_started_utc": "2026-07-08T00:00:01Z",
                "files": {
                    k: {"sha256": _sha(v), "size_bytes": len(v)}
                    for k, v in files.items()
                },
                "not_fetched": [],
                "run_id": "SYN-1",
                "run_label": "syn-base-proxies",
            }
        ),
    )
    _write(
        capture / "governance-evidence.json",
        _dump({"review": "review-link", "run_id": "SYN-1"}),
    )
    return capture


def _french_zip(root: Path, upto: int) -> Path:
    lines = ["synthetic", "", ",Mkt-RF,SMB,HML,RMW,CMA,RF"]
    lines += [f"{d.strftime('%Y%m%d')},0,0,0,0,0,0.015" for d in _days()[1:upto]]
    path = root / "french.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("F-F_daily.csv", "\r\n".join(lines) + "\r\n")
    return path


def _governance(targets: bytes) -> dict[str, Any]:
    return {
        "as_of_date": _days()[-1].isoformat(),
        "series_composition": {SERIES: {"live_observations": 10}},
        "target_source": {
            "expected_sha256": _sha(targets),
            "expected_size_bytes": len(targets),
            "interval_policy": {
                "series_policies": {
                    SERIES: {
                        "finalized_through": "2026-07-01",
                        "source_vintage": "v" * 64,
                    }
                }
            },
        },
    }


def _spec_template() -> dict[str, Any]:
    names = [
        *LEDGER,
        "source/base-proxies-adjusted.json",
        "source/base-proxies-adjusted.json.manifest.json",
        "source/acquisition.json",
        "source/factor-derivation-proof.json",
        "source/interval-authority.json",
    ]
    return {
        "as_of_date": "2000-01-01",
        "factor_source": {
            "expected_sha256": "",
            "expected_size_bytes": 0,
            "interval_policy": {"available_through_anchor": "", "source_vintage": ""},
            "relative_path": "factors.csv",
        },
        "models": [{"model_id": "m"}],
        "provenance_sources": [
            {
                "artifact_id": f"x/{Path(n).name}",
                "expected_sha256": "",
                "expected_size_bytes": 0,
                "relative_path": n,
            }
            for n in names
        ],
        "series_composition": {},
        "target_source": {},
    }


def _authority_template() -> dict[str, Any]:
    return {
        "authority_specs": [
            {"relative_path": "specs/b.json", "sha256": "0"},
            {"relative_path": "specs/a.json", "sha256": "0"},
        ],
        "per_series_authority": {SERIES: {"status": "carried text"}},
        "selected_target_sha256": "0",
        "source_plan": {"path": "old", "sha256": "0", "size_bytes": 0},
        "target_interval_policy": {
            "series_policies": {
                SERIES: {"finalized_through": "x", "source_vintage": "y"}
            }
        },
    }


def _fixture(root: Path, construction: str, rows: list[dict[str, Any]]) -> Path:
    _capture_dir(root, rows)
    targets = b"series_id,date,return\nSeries A,2026-07-01,0.01\n"
    # live_only from a D plan pass, live_plus_backtest carried from a predecessor
    plan = root / "plan"
    _write(plan / "inputs/history/selected-targets.csv", targets)
    _write(
        plan / "inputs/governance/base-decomposition-spec.json",
        _dump(_governance(targets)),
    )
    _write(plan / "specs/a.json", b'{"a": 1}\n')
    _write(plan / "specs/b.json", b'{"b": 2}\n')
    _write(plan / "plan-manifest.json", b'{"plan": 1}\n')
    _write(root / "authority-template.json", _dump(_authority_template()))
    _write(root / "spec-template.json", _dump(_spec_template()))
    pred = root / "pred"
    _write(pred / "live_plus_backtest/base-source/targets.csv", targets)
    authority = _authority_template() | {"selected_target_sha256": _sha(targets)}
    _write(
        pred / "live_plus_backtest/base-source/source/interval-authority.json",
        _dump(authority),
    )
    _write(
        pred / "spec/live_plus_backtest/base-decomposition-spec.json",
        _dump(_governance(targets)),
    )
    ledger = root / "ledger"
    _write(
        ledger / "provider-ledger-generation.json",
        LEDGER["provider-ledger-generation.json"],
    )
    _write(ledger / "provider-ledger.json", LEDGER["provider-ledger.json"])
    config = {
        "construction": construction,
        "anchor": _days()[-1].isoformat(),
        "capture_dir": "capture",
        "native_raw": "capture/raw/base-proxies/history.csv",
        "french_zip": str(_french_zip(root, N - 25).relative_to(root)),
        "capture_dir_text": "CAPTURE",
        "factor_path_text": "FACTORS",
        "ledger_generation": "ledger/provider-ledger-generation.json",
        "ledger_catalog": "ledger/provider-ledger.json",
        "predecessor_bundle": "pred",
        "modes": {
            "live_only": {
                "plan_dir": "plan",
                "plan_manifest_text": "PLAN",
                "authority_template": "authority-template.json",
                "spec_template": "spec-template.json",
            },
            "live_plus_backtest": {"spec_template": "spec-template.json"},
        },
    }
    path = root / "config.json"
    _write(path, _dump(config))
    return path


@pytest.fixture(autouse=True)
def _synthetic_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    # the real exporter needs a verified ledger generation; covered by the real test
    monkeypatch.setattr(bb, "provider_ledger_files", lambda _g, _c: dict(LEDGER))


@pytest.mark.parametrize("construction", bb.CONSTRUCTIONS)
def test_a_mini_bundle_is_deterministic_and_self_consistent(
    tmp_path: Path, construction: str
) -> None:
    config = _fixture(tmp_path, construction, _capture_rows())
    first = bb.build_bundle(bb.load_config(config), tmp_path / "one")
    second = bb.build_bundle(bb.load_config(config), tmp_path / "two")
    one = (tmp_path / "one/BUNDLE.json").read_bytes()
    assert one == (tmp_path / "two/BUNDLE.json").read_bytes()
    assert first == second
    assert b"\r" not in one
    # every file is listed with its hash, and the file on disk has that hash
    for rel, entry in first["files"].items():
        data = (tmp_path / "one" / rel).read_bytes()
        assert entry == {"sha256": _sha(data), "size_bytes": len(data)}
        assert data == (tmp_path / "two" / rel).read_bytes()
    assert bb.compare_reference(tmp_path / "two", tmp_path / "one") == []
    for mode in bb.MODES:
        spec = json.loads(
            (tmp_path / f"one/spec/{mode}/base-decomposition-spec.json").read_bytes()
        )
        base = tmp_path / "one" / mode / "base-source"
        for item in spec["provenance_sources"]:
            data = (base / item["relative_path"]).read_bytes()
            assert item["expected_sha256"] == _sha(data)
        factors = (tmp_path / "one/factors.csv").read_bytes()
        assert spec["factor_source"]["expected_sha256"] == _sha(factors)
        assert spec["factor_source"]["interval_policy"]["available_through_anchor"] == (
            _days()[-1].isoformat()
        )
    authority = json.loads(
        (
            tmp_path / "one/live_only/base-source/source/interval-authority.json"
        ).read_bytes()
    )
    assert [e["relative_path"] for e in authority["authority_specs"]] == [
        "specs/b.json",
        "specs/a.json",
    ]
    assert authority["authority_specs"][1]["sha256"] == _sha(b'{"a": 1}\n')
    assert authority["source_plan"]["path"] == "PLAN"
    assert authority["target_interval_policy"]["series_policies"][SERIES] == {
        "finalized_through": "2026-07-01",
        "source_vintage": "v" * 64,
    }
    proof = json.loads(
        (
            tmp_path / "one/live_only/base-source/source/factor-derivation-proof.json"
        ).read_bytes()
    )
    assert proof["status"] == "PASS_SOURCE_AND_LITERAL_RATIONAL_ARITHMETIC"
    assert proof["capture"]["run_id"] == "SYN-1"
    if construction == bb.TOTAL_RETURN_FRENCH_RF:
        assert "FRENCH_RF_THROUGH_" in proof["construction"]
        assert proof["receipt"]["overlap_days"] == 60
        assert (
            tmp_path / "one/live_only/base-source/source/french-5-factors-daily.zip"
        ).exists()
    else:
        assert proof["construction"] == bb.ADJUSTED_CONSTRUCTION
    with pytest.raises(bb.BaseBundleError):
        bb.build_bundle(bb.load_config(config), tmp_path / "one")


def test_adjusted_ratio_matches_an_independent_hand_computation() -> None:
    rows = _capture_rows()
    built = bb.adjusted_ratio_rows(bb.adjusted_prices(rows))
    closes: dict[str, dict[str, float]] = {}
    for row in rows:
        closes.setdefault(row["Ticker"], {})[row["Date"][:10]] = row["Close"]
    days = [d.isoformat() for d in _days()]
    assert [r.date for r in built] == days[1:]
    for factor in built:
        before = days[days.index(factor.date) - 1]
        rf = closes["BIL"][factor.date] / closes["BIL"][before] - 1
        assert float(factor.rf) == pytest.approx(rf, abs=1e-6)
        for ticker, value in zip(bb.PROXY_TICKERS, factor.xs):
            expected = closes[ticker][factor.date] / closes[ticker][before] - 1 - rf
            assert round(float(value), 6) == round(expected, 6)


def test_adjusted_ratio_requires_all_four_prices_without_fill() -> None:
    rows = [
        r
        for r in _capture_rows()
        if not (r["Ticker"] == "IWM" and r["Date"][:10] == _days()[5].isoformat())
    ]
    built = bb.adjusted_ratio_rows(bb.adjusted_prices(rows))
    dates = [r.date for r in built]
    assert _days()[5].isoformat() not in dates
    after = next(r for r in built if r.date == _days()[6].isoformat())
    assert after.previous == _days()[4].isoformat()


def test_a_capture_without_the_latest_monthly_distribution_is_refused(
    tmp_path: Path,
) -> None:
    config = _fixture(
        tmp_path, bb.TOTAL_RETURN_FRENCH_RF, _capture_rows(dividend_day=None)
    )
    with pytest.raises(bb.BaseBundleError, match="distribution"):
        bb.build_bundle(bb.load_config(config), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_capture_file_that_differs_from_its_stage_manifest_is_refused(
    tmp_path: Path,
) -> None:
    config = _fixture(tmp_path, bb.ADJUSTED_RATIO, _capture_rows())
    raw = tmp_path / "capture/raw/base-proxies/history.csv"
    raw.write_bytes(raw.read_bytes() + b"x\n")
    with pytest.raises(bb.BaseBundleError, match="stage manifest"):
        bb.build_bundle(bb.load_config(config), tmp_path / "out")


@pytest.mark.skipif(
    not (
        os.environ.get("QE_BASE_BUNDLE_CONFIG")
        and os.environ.get("QE_BASE_BUNDLE_REFERENCE")
    ),
    reason="set QE_BASE_BUNDLE_CONFIG and QE_BASE_BUNDLE_REFERENCE to the retained inputs",
)
def test_the_retained_reference_bundle_reproduces_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()  # the real ledger exporter
    out = tmp_path / "bundle"
    bb.build_bundle(bb.load_config(Path(os.environ["QE_BASE_BUNDLE_CONFIG"])), out)
    assert bb.compare_reference(out, Path(os.environ["QE_BASE_BUNDLE_REFERENCE"])) == []
