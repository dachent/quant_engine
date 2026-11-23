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


def test_held_qsi_and_qb_kinds_are_driver_valid_and_cover_their_tickers() -> None:
    held = y.build_config("held", CUTOFF, "R4")
    held_tickers = [s for g in held["groups"] for s in g["symbols"]]
    # Yahoo no longer serves BK: the symbol is BNY; one common start (IBIT is the youngest)
    assert sorted(held_tickers) == sorted(
        [
            "BNY",
            "BRK-B",
            "CURE",
            "DUK",
            "IBIT",
            "PEP",
            "PG",
            "SHY",
            "SQQQ",
            "TMF",
            "TYD",
            "UPRO",
        ]
    )
    assert "BK" not in held_tickers
    assert [g["start"] for g in held["groups"]] == ["2024-01-11"]
    qsi = y.build_config("qsi", CUTOFF, "R4")
    qsi_tickers = [s for g in qsi["groups"] for s in g["symbols"]]
    assert len(qsi_tickers) == 15 and len(set(qsi_tickers)) == 15
    assert all(len(g["symbols"]) == 1 for g in qsi["groups"])
    qb = y.build_config("qb", CUTOFF, "R4")
    assert [g["symbols"] for g in qb["groups"]] == [["SH"], ["GLD"], ["TQQQ"]]
    for kind, config in (("held", held), ("qsi", qsi), ("qb", qb)):
        assert config == y.build_config(kind, CUTOFF, "R4")
        assert y.config_sha256(config) != y.config_sha256(
            y.build_config(kind, date(2026, 11, 13), "R4")
        )
        assert all(g["through"] == "cutoff" for g in config["groups"])
        assert y.run_id("R4", kind, None) == f"R4-{kind.upper()}"


FX_FIRST = date(2025, 1, 1)


FX_COLUMNS = [
    "Ticker",
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
    "Dividends",
    "Stock Splits",
]


def _write_fx(
    folder: Path,
    *,
    drop: dict[str, str] | None = None,
    stop: dict[str, str] | None = None,
    close: dict[str, float] | None = None,
) -> Path:
    """A capture-v0 history file (the exact keys the driver writes for the fx profile)."""

    folder.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for symbol in ("USDC-USD", "USDT-USD"):
        day = FX_FIRST
        while day <= CUTOFF:
            iso = day.isoformat()
            skip = (drop or {}).get(symbol) == iso or iso > (stop or {}).get(
                symbol, "9999"
            )
            if not skip:
                price = (close or {}).get(symbol, 1.0)
                rows.append(
                    {
                        "Ticker": symbol,
                        "Date": f"{iso}T00:00:00",
                        "Open": price,
                        "High": price,
                        "Low": price,
                        "Close": price,
                        "Adj Close": price,
                        "Volume": 1.0,
                        "Dividends": 0.0,
                        "Stock Splits": 0.0,
                    }
                )
            day += timedelta(days=1)
    document = {
        "provider": "yahoo",
        "transport": "yfinance",
        "dataset": "history",
        "tickers": ["USDC-USD", "USDT-USD"],
        "columns": FX_COLUMNS,
        "rows": rows,
        "start": FX_FIRST.isoformat(),
        "end": (CUTOFF + timedelta(days=1)).isoformat(),
        "interval": "1d",
        "prepost": False,
    }
    target = folder / "yahoo-fx-usd-stables-history.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return (
        target  # no history_adjusted file: the capture-v0 profile carries history only
    )


def test_fx_kind_is_driver_valid_every_day_from_the_fx_observed_start() -> None:
    config = y.build_config("fx", CUTOFF, "R5")
    assert [g["symbols"] for g in config["groups"]] == [["USDC-USD", "USDT-USD"]]
    assert [g["start"] for g in config["groups"]] == ["2025-01-01"]
    assert [g["calendar"] for g in config["groups"]] == ["every_day"]
    # the profile the FX stager reads: history only, capture-v0 keys (not the default tape)
    assert [g["artifact_profile"] for g in config["groups"]] == ["capture-v0"]
    assert [g["datasets"] for g in config["groups"]] == [["history"]]
    parsed = driver.parse_config(y.config_bytes(config), cutoff_override="2026-11-06")
    assert [g.artifact_profile for g in parsed.groups] == ["capture-v0"]
    assert [list(g.datasets) for g in parsed.groups] == [["history"]]
    # other kinds keep the default tape profile
    assert all(
        "artifact_profile" not in g
        for g in y.build_config("base", CUTOFF, "R5")["groups"]
    )
    assert config == y.build_config("fx", CUTOFF, "R5")
    assert y.config_sha256(config) != y.config_sha256(
        y.build_config("fx", date(2026, 11, 13), "R5")
    )
    assert y.run_id("R5", "fx", None) == "R5-FX"
    assert "R5-FX config_sha256" in y.comment_body(CUTOFF, "R5", ["fx"], None)


def test_fx_acceptance_passes_a_complete_series(tmp_path: Path) -> None:
    _write_fx(tmp_path)
    result = y.acceptance(tmp_path, "fx", CUTOFF)
    assert result["passed"], result["findings"]
    assert result["tickers"]["USDC-USD"]["first"] == "2025-01-01"
    assert result["tickers"]["USDT-USD"]["last"] == "2026-11-06"


def test_fx_acceptance_refuses_a_missing_day(tmp_path: Path) -> None:
    _write_fx(tmp_path, drop={"USDT-USD": "2026-03-14"})
    findings = y.acceptance(tmp_path, "fx", CUTOFF)["findings"]
    assert any(
        f.startswith("USDT-USD: 1 missing UTC days, first 2026-03-14") for f in findings
    )
    assert not any(f.startswith("USDC-USD") for f in findings)


def test_fx_acceptance_refuses_a_stale_series(tmp_path: Path) -> None:
    _write_fx(tmp_path, stop={"USDC-USD": "2026-11-05"})
    findings = y.acceptance(tmp_path, "fx", CUTOFF)["findings"]
    assert any(f.startswith("USDC-USD: last bar") for f in findings)


def test_fx_acceptance_refuses_a_bad_close(tmp_path: Path) -> None:
    _write_fx(tmp_path, close={"USDC-USD": 0.5})
    findings = y.acceptance(tmp_path, "fx", CUTOFF)["findings"]
    assert any(f.startswith("USDC-USD: close outside") for f in findings)


def test_fx_capture_feeds_the_fx_stager(tmp_path: Path) -> None:
    import portfolio_fx_rates as fxr

    history = _write_fx(tmp_path / "artifacts")
    assert y.acceptance(history.parent, "fx", CUTOFF)["passed"]
    value, _raw = fxr._capture(history)  # noqa: SLF001 - the exact-keys check the stager runs
    assert set(value) == {
        "provider",
        "transport",
        "dataset",
        "tickers",
        "columns",
        "rows",
        "start",
        "end",
        "interval",
        "prepost",
    }
    staged = fxr.stage_candidate(
        history,
        tmp_path / "staged",
        observed_start="2025-01-01",
        observed_end=CUTOFF.isoformat(),
    )
    assert staged["published"] is False
    assert staged["rate_count"] == 2 * ((CUTOFF - FX_FIRST).days + 1)
