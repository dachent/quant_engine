"""Offline tests for the one-command Yahoo base/style re-capture (synthetic artifacts, driver stubbed)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

import yahoo_fetch_driver as driver
import yahoo_refresh_capture as y

CUTOFF = date(2026, 11, 6)  # a Friday
TINY = (
    ("t-etf", "2026-08-03", ("BIL", "SPY"), "weekday"),
    ("t-btc", "2026-08-03", ("BTC-USD",), "every_day"),
)
PAID = {"BIL": ["2026-09-01", "2026-10-01", "2026-11-02"]}


def _days(start: str, calendar: str) -> list[str]:
    d, out = date.fromisoformat(start), []
    while d <= CUTOFF:
        if calendar == "every_day" or d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _write(
    folder: Path,
    *,
    paid: dict[str, list[str]] | None = None,
    drop_adjusted_day: bool = False,
    truncate: set[str] | None = None,
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for group_id, first, symbols, calendar in TINY:
        rows, adjusted = [], []
        for symbol in symbols:
            for day in _days(first, calendar):
                if symbol in (truncate or set()) and day > "2026-10-30":
                    continue
                row = {
                    "Ticker": symbol,
                    "Date": f"{day}T00:00:00",
                    "Close": 100.0,
                    "Dividends": 0.3 if day in (paid or PAID).get(symbol, []) else 0.0,
                }
                rows.append(row)
                adjusted.append(dict(row))
        if drop_adjusted_day and group_id == "t-etf":
            adjusted.pop()
        for name, data in (("history", rows), ("history_adjusted", adjusted)):
            (folder / f"yahoo-{group_id}-{name}.json").write_text(
                json.dumps({"rows": data}), encoding="utf-8"
            )


@pytest.fixture()
def tiny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(y.KINDS, "tiny", TINY)
    monkeypatch.setattr(y, "MONTHLY_PAYERS", ("BIL",))


def test_config_is_deterministic_and_driver_valid() -> None:
    first = y.build_config("style", CUTOFF, "R4")
    assert first == y.build_config("style", CUTOFF, "R4")
    assert y.config_sha256(first) == y.config_sha256(
        y.build_config("style", CUTOFF, "R4")
    )
    assert y.config_sha256(first) != y.config_sha256(
        y.build_config("style", date(2026, 11, 13), "R4")
    )
    tickers = [s for g in first["groups"] for s in g["symbols"]]
    assert len(tickers) == 22 and len(set(tickers)) == 22
    base = y.build_config("base", CUTOFF, "R4")
    assert [g["symbols"] for g in base["groups"]] == [["SPY", "IWM", "HYG", "BIL"]]
    assert all(g["through"] == "cutoff" for g in first["groups"] + base["groups"])


def test_run_id_convention() -> None:
    assert y.run_id("R3", "base", None) == "R3-BASE"
    assert y.run_id("R3", "style", 2) == "R3-STYLE-2"
    assert y.run_id("R3", "style", 1) == "R3-STYLE"


def test_comment_is_value_free_and_binds_config_shas() -> None:
    body = y.comment_body(CUTOFF, "R4", ["base", "style"], None)
    for kind in ("base", "style"):
        assert y.config_sha256(y.build_config(kind, CUTOFF, "R4")) in body
    assert "R4-BASE" in body and "R4-STYLE" in body and "not an owner approval" in body


def test_distribution_record_rules() -> None:
    first = date(2024, 1, 2)
    sound = [
        f"{year}-{month:02d}-01"
        for year, month in [(2024, m) for m in range(1, 13)]
        + [(2025, m) for m in range(1, 13)]
        + [(2026, m) for m in range(1, 12)]
    ]
    assert y.distribution_findings(sound, CUTOFF, first) == []
    stale = y.distribution_findings(
        sound[:-2], CUTOFF, first
    )  # October and November missing
    assert any("older than" in f for f in stale) and any("2026-10" in f for f in stale)
    gap = y.distribution_findings(
        [d for d in sound if d != "2026-05-01"], CUTOFF, first
    )
    assert any("2026-05" in f for f in gap)
    wide = y.distribution_findings(
        ["2026-07-01", "2026-09-01", "2026-10-01", "2026-11-02"],
        CUTOFF,
        date(2026, 6, 1),
    )
    assert any("exceeds" in f for f in wide)
    assert y.distribution_findings([], CUTOFF) == ["no distribution recorded"]


def test_acceptance_passes_sound_artifacts(tiny: None, tmp_path: Path) -> None:
    _write(tmp_path)
    result = y.acceptance(tmp_path, "tiny", CUTOFF)
    assert result["passed"], result["findings"]
    assert result["tickers"]["BIL"]["last_distribution"] == "2026-11-02"
    assert result["tickers"]["BTC-USD"]["last"] == "2026-11-06"


def test_acceptance_refuses_missing_november_distribution(
    tiny: None, tmp_path: Path
) -> None:
    _write(tmp_path, paid={"BIL": ["2026-09-01"]})
    findings = y.acceptance(tmp_path, "tiny", CUTOFF)["findings"]
    assert any(f.startswith("BIL:") for f in findings)


def test_acceptance_refuses_stale_last_bar_and_crypto_lag(
    tiny: None, tmp_path: Path
) -> None:
    _write(tmp_path, truncate={"BTC-USD", "SPY"})
    findings = y.acceptance(tmp_path, "tiny", CUTOFF)["findings"]
    assert any(f.startswith("BTC-USD: last bar") for f in findings)
    assert any(f.startswith("SPY: last bar") for f in findings)


def test_acceptance_refuses_raw_adjusted_mismatch(tiny: None, tmp_path: Path) -> None:
    _write(tmp_path, drop_adjusted_day=True)
    assert any(
        "different dates" in f
        for f in y.acceptance(tmp_path, "tiny", CUTOFF)["findings"]
    )


def _stub_driver(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], *, fail: str | None = None
) -> None:
    def fake_main(argv: list[str]) -> int:
        calls.append(list(argv))
        if argv[0] == "fetch":
            stage = (
                Path(argv[argv.index("--stage-root") + 1])
                / argv[argv.index("--run-id") + 1]
            )
            _write(stage / "artifacts")
        return 1 if argv[0] == fail else 0

    monkeypatch.setattr(driver, "main", fake_main)


def test_run_kind_orchestrates_the_governed_sequence(
    tiny: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    _stub_driver(monkeypatch, calls)
    monkeypatch.setattr(
        y,
        "build_config",
        lambda kind, cutoff, label: {**_cfg(), "cutoff": cutoff.isoformat()},
    )
    monkeypatch.setattr(y, "config_sha256", lambda document: "a" * 64)
    now = datetime(2026, 11, 17, 13, 30, 5, tzinfo=timezone.utc)
    result = y.run_kind(
        "tiny",
        CUTOFF,
        "R4",
        2,
        "https://example.test/issues/283#issuecomment-1",
        tmp_path,
        now=now,
    )
    assert [c[0] for c in calls] == ["make-evidence", "fetch", "verify"]
    evidence = calls[0]
    assert evidence[evidence.index("--run-id") + 1] == "R4-TINY-2"
    assert evidence[evidence.index("--valid-from-utc") + 1] == "2026-11-17T13:28:05Z"
    assert evidence[evidence.index("--valid-until-utc") + 1] == "2026-11-18T09:30:05Z"
    assert evidence[evidence.index("--max-requests") + 1] == str(y.MAX_REQUESTS)
    assert calls[1][calls[1].index("--cutoff") + 1] == "2026-11-06"
    run_folder = tmp_path / "R4-TINY-2" / "stage" / "R4-TINY-2"
    assert result["passed"] and (run_folder / "ACCEPTANCE.json").is_file()
    sums = (run_folder / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "artifacts/yahoo-t-etf-history.json" in sums and "ACCEPTANCE.json" in sums


def test_run_kind_stops_when_the_driver_refuses(
    tiny: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    _stub_driver(monkeypatch, calls, fail="fetch")
    monkeypatch.setattr(y, "build_config", lambda kind, cutoff, label: _cfg())
    with pytest.raises(y.RefreshCaptureError):
        y.run_kind("tiny", CUTOFF, "R4", None, "https://example.test/c", tmp_path)
    assert [c[0] for c in calls] == ["make-evidence", "fetch"]  # verify never reached


def test_run_kind_is_create_only(
    tiny: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "R4-TINY").mkdir()
    monkeypatch.setattr(y, "build_config", lambda kind, cutoff, label: _cfg())
    with pytest.raises(FileExistsError):
        y.run_kind("tiny", CUTOFF, "R4", None, "https://example.test/c", tmp_path)


def _cfg() -> dict[str, Any]:
    return {
        "cutoff": "2026-11-06",
        "groups": [],
        "run_label": "r4-tiny",
        "schema_version": 1,
    }


def test_sector_kind_covers_the_sector_bundle_tickers() -> None:
    config = y.build_config("sector", CUTOFF, "R3")
    tickers = [s for g in config["groups"] for s in g["symbols"]]
    assert sorted(tickers) == sorted(
        [
            "XLC",
            "XLY",
            "XLP",
            "XLE",
            "XLF",
            "XLV",
            "XLI",
            "XLB",
            "XLRE",
            "XLK",
            "XLU",
            "BIL",
        ]
    )
    assert y.run_id("R3", "sector", 2) == "R3-SECTOR-2"
    assert y.config_sha256(config) == y.config_sha256(
        y.build_config("sector", CUTOFF, "R3")
    )
