"""Offline tests for the governed Yahoo fetch driver (synthetic transports only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import yahoo_fetch_driver as driver


NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
CUTOFF = "2026-10-09"  # a Friday
START = "2026-09-01"


class _Cache:
    location: str | None = None

    def set_cache_location(self, value: str) -> None:
        self.location = value


class FakeYF:
    """Synthetic stand-in for the ``yfinance`` module (group_by='ticker' frames)."""

    def __init__(
        self,
        *,
        empty: set[str] | None = None,
        drop_after: dict[str, date] | None = None,
        start_after: dict[str, date] | None = None,
        every_day: set[str] | None = None,
        skip_days: dict[str, int] | None = None,
        null_close: set[str] | None = None,
    ) -> None:
        self.cache = _Cache()
        self.calls: list[list[str]] = []
        self.empty = empty or set()
        self.drop_after = drop_after or {}
        self.start_after = start_after or {}
        self.every_day = every_day or set()
        self.skip_days = skip_days or {}
        self.null_close = null_close or set()

    def download(self, **kwargs: Any) -> pd.DataFrame:
        tickers = kwargs["tickers"]
        tickers = [tickers] if isinstance(tickers, str) else list(tickers)
        self.calls.append(tickers)
        start = date.fromisoformat(kwargs["start"])
        end = date.fromisoformat(kwargs["end"])
        parts = {}
        for ticker in tickers:
            if ticker in self.empty:
                continue
            days = []
            cursor = start
            while cursor < end:
                if (
                    (ticker in self.every_day or cursor.weekday() < 5)
                    and (cursor <= self.drop_after.get(ticker, date.max))
                    and cursor >= self.start_after.get(ticker, date.min)
                ):
                    days.append(cursor)
                cursor += timedelta(days=1)
            for _ in range(self.skip_days.get(ticker, 0)):
                if len(days) > 3:
                    days.pop(len(days) // 2)
            if not days:
                continue
            if kwargs["interval"] != "1d":
                stamps = [
                    pd.Timestamp(d) + pd.Timedelta(hours=9 + h)
                    for d in days
                    for h in range(7)
                ]
            else:
                stamps = [pd.Timestamp(d) for d in days]
            idx = pd.DatetimeIndex(stamps, name="Date")
            n = len(stamps)
            close = [100.0 + i for i in range(n)]
            frame = pd.DataFrame(
                {
                    "Open": [c - 1 for c in close],
                    "High": [c + 1 for c in close],
                    "Low": [c - 2 for c in close],
                    "Close": close,
                    "Adj Close": [c * 0.98 for c in close],
                    "Volume": [1000 + i for i in range(n)],
                    "Dividends": [0.5 if i == 2 else 0.0 for i in range(n)],
                    "Stock Splits": [2.0 if i == 4 else 0.0 for i in range(n)],
                },
                index=idx,
            )
            if ticker in self.null_close:
                frame.iloc[0, frame.columns.get_loc("Close")] = float("nan")
            parts[ticker] = frame
        if not parts:
            return pd.DataFrame()
        return pd.concat(parts, axis=1, sort=True)


class FakeChart:
    def __init__(
        self,
        *,
        extra_day: bool = False,
        nulls: int = 0,
        empty: set[str] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.searches: list[str] = []
        self.extra_day = extra_day
        self.nulls = nulls
        self.empty = empty or set()
        self.fail = fail or set()

    def chart(self, symbol: str, period1: int, period2: int) -> bytes:
        self.calls.append(symbol)
        if symbol in self.fail:
            raise driver.DriverError("HTTP 404")
        zone = ZoneInfo("Europe/London")
        stamps, closes = [], []
        cursor = datetime.fromtimestamp(period1, timezone.utc).astimezone(zone).date()
        last = datetime.fromtimestamp(period2, timezone.utc).date() + (
            timedelta(days=3) if self.extra_day else timedelta(0)
        )
        index = 0
        while cursor <= last:
            if cursor.weekday() < 5:
                local = datetime(cursor.year, cursor.month, cursor.day, tzinfo=zone)
                stamp = int(local.timestamp())
                if period1 <= stamp < period2 or self.extra_day:
                    stamps.append(stamp)
                    closes.append(None if index < self.nulls else 1.1 + index / 1000)
                    index += 1
            cursor += timedelta(days=1)
        if symbol in self.empty:
            stamps, closes = [], []
        body = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "symbol": symbol,
                            "instrumentType": "CURRENCY",
                            "dataGranularity": "1d",
                            "exchangeTimezoneName": "Europe/London",
                            "exchangeName": "CCY",
                            "longName": f"{symbol} synthetic",
                        },
                        "timestamp": stamps,
                        "indicators": {"quote": [{"close": closes}]},
                    }
                ]
            }
        }
        return json.dumps(body).encode()

    def search(self, query: str) -> bytes:
        self.searches.append(query)
        return json.dumps({"quotes": [{"symbol": "ZZ.ST", "longname": query}]}).encode()


def _group(group_id: str = "daily", **overrides: Any) -> dict[str, Any]:
    group = {
        "group_id": group_id,
        "kind": "yfinance",
        "symbols": ["AAA", "BBB"],
        "interval": "1d",
        "start": START,
        "through": "cutoff",
        "min_rows": 20,
        "max_missing_weekdays": 3,
    }
    group.update(overrides)
    return group


def _config(groups: list[dict[str, Any]] | None = None, **top: Any) -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "run_label": "unit-test",
        "cutoff": CUTOFF,
        "groups": groups if groups is not None else [_group()],
        "limits": {"retry_attempts": 0, "retry_backoff_seconds": 0},
    }
    document.update(top)
    return json.dumps(document).encode()


def _fetch(
    tmp_path: Path, raw: bytes | None = None, run_id: str = "run-1", **kw: Any
) -> Path:
    config = driver.parse_config(raw if raw is not None else _config())
    kw.setdefault("yf", FakeYF())
    kw.setdefault("chart", FakeChart())
    return driver.run_fetch(
        config, stage_root=tmp_path / "stage", run_id=run_id, now=NOW, **kw
    )


# ---------------------------------------------------------------- config


def test_adapter_constants_match_the_maintained_adapter() -> None:
    adapter = driver.load_adapter()
    assert driver.VALID_INTERVALS == frozenset(adapter.VALID_INTERVALS)
    assert driver._HISTORY_COLUMNS == tuple(adapter.HISTORY_COLUMNS)
    assert set(driver._DATASETS) <= set(adapter.HISTORY_DATASETS)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d["groups"][0].update(through="2026-10-10"),
        lambda d: d["groups"][0].update(symbols=["AAA", "AAA"]),
        lambda d: d["groups"][0].update(interval="2d"),
        lambda d: d["groups"][0].update(artifact_profile="capture-v0"),
        lambda d: d["groups"][0].update(
            exceptions=[{"symbol": "ZZZ", "rule": "last_bar", "label": "LATE_LABEL"}]
        ),
        lambda d: d["groups"][0].update(
            exceptions=[{"symbol": "AAA", "rule": "last_bar", "label": "late"}]
        ),
        lambda d: d["groups"][0].update(
            exceptions=[{"symbol": "AAA", "rule": "nope", "label": "LATE_LABEL"}]
        ),
        lambda d: d["groups"][0].update(start="2026-10-10"),
        lambda d: d["groups"][0].update(kind="chart", interval="1h"),
        lambda d: d.update(cutoff="2026-13-01"),
        lambda d: d.update(groups=[]),
        lambda d: d["groups"][0].pop("min_rows"),
    ],
)
def test_config_refusals(mutate: Any) -> None:
    document = json.loads(_config())
    mutate(document)
    with pytest.raises(driver.DriverError):
        driver.parse_config(json.dumps(document).encode())


def test_cutoff_override_changes_the_effective_config_hash() -> None:
    a = driver.parse_config(_config())
    b = driver.parse_config(_config(), cutoff_override="2026-10-08")
    assert a.sha256 != b.sha256
    assert b.cutoff.isoformat() == "2026-10-08"


# -------------------------------------------------------------- happy path


def test_fetch_stages_hash_bound_run_and_verifies(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    manifest = json.loads((run / "MANIFEST.json").read_text())
    names = {p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()}
    assert names == set(manifest["files"]) | {"MANIFEST.json"}
    assert "raw/daily/history.csv" in names
    assert "artifacts/yahoo-daily-history_adjusted.json.manifest.json" in names
    # the manifest is written last: nothing it binds is newer than it
    newest = max((run / n).stat().st_mtime_ns for n in manifest["files"])
    assert (run / "MANIFEST.json").stat().st_mtime_ns >= newest
    report = driver.verify_run(run, now=NOW)
    assert report["status"] == "VERIFIED"
    coverage = json.loads((run / "coverage.json").read_text())
    entry = coverage["groups"][0]["symbols"][0]
    assert entry["status"] == "PASS"
    assert entry["last_bar"] == CUTOFF
    assert entry["first_bar"] == START
    assert entry["adjustment"]["split_event_count"] == 1
    assert entry["adjustment"]["dividend_event_count"] == 1
    assert entry["adjustment"]["adj_to_close_ratio_first"] == 0.98


def test_fetch_is_deterministic_and_create_only(tmp_path: Path) -> None:
    a = _fetch(tmp_path, run_id="run-a")
    b = _fetch(tmp_path, run_id="run-b")
    for name in (
        "artifacts/yahoo-daily-history_adjusted.json",
        "raw/daily/history.csv",
    ):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    with pytest.raises(driver.DriverError, match="already"):
        _fetch(tmp_path, run_id="run-a")
    assert b"\r\n" not in (a / "raw/daily/history.csv").read_bytes()
    assert b"\r\n" not in (a / "artifacts/yahoo-daily-history.json").read_bytes()


def test_manifest_matches_the_adapters_own_run_manifest(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    adapter = driver.load_adapter()
    artifact = run / "artifacts/yahoo-daily-history_adjusted.json"
    mine = json.loads(
        (run / "artifacts/yahoo-daily-history_adjusted.json.manifest.json").read_text()
    )
    scratch = tmp_path / "adapter" / artifact.name
    scratch.parent.mkdir()
    scratch.write_bytes(artifact.read_bytes())
    theirs_path = adapter.write_run_manifest(scratch, mine["request"], mine["coverage"])
    assert json.loads(theirs_path.read_text()) == mine


def test_existing_readers_accept_the_staged_artifacts(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    attribution = importlib.import_module("portfolio_implementation_return_attribution")
    rows = attribution._market_rows(
        run / "artifacts/yahoo-daily-history_adjusted.json",
        accepted_as_of=date(2026, 10, 9),
    )
    assert set(rows) == {"AAA", "BBB"}
    style = importlib.import_module("portfolio_public_style_sources")
    artifact = run / "artifacts/yahoo-daily-history_adjusted.json"
    manifest = artifact.with_name(artifact.name + ".manifest.json")
    from decimal import Decimal

    source = json.loads(artifact.read_text(), parse_float=Decimal, parse_int=Decimal)
    man = json.loads(manifest.read_text(), parse_float=Decimal, parse_int=Decimal)
    style._verify_source(source, artifact.read_bytes(), man, manifest, ["AAA", "BBB"])


def test_capture_profile_is_what_the_fx_reader_requires(tmp_path: Path) -> None:
    groups = [
        _group(
            "stable",
            symbols=["USDC-USD", "USDT-USD"],
            calendar="every_day",
            datasets=["history"],
            artifact_profile="capture-v0",
            max_missing_weekdays=0,
        )
    ]
    run = driver._commit_run  # keep linters quiet about unused attr access
    del run
    run_dir = _fetch(
        tmp_path,
        _config(groups),
        yf=FakeYF(every_day={"USDC-USD", "USDT-USD"}),
    )
    fx = importlib.import_module("portfolio_fx_rates")
    path = run_dir / "artifacts/yahoo-stable-history.json"
    capture, _ = fx._capture(path)
    rates = fx._rates(capture, date(2026, 9, 1), date(2026, 10, 9))
    assert len(rates) == 2 * 39
    assert driver.verify_run(run_dir, now=NOW)["status"] == "VERIFIED"


def test_chart_group_stages_raw_response_the_fx_tool_reads(tmp_path: Path) -> None:
    groups = [
        _group("fx", kind="chart", symbols=["EURUSD=X"], min_rows=20, max_null_closes=2)
    ]
    run = _fetch(tmp_path, _config(groups), chart=FakeChart(nulls=1))
    raw = run / "raw/fx/EURUSD_X.chart.json"
    fx = importlib.import_module("portfolio_fx_rates")
    observed, meta = fx._fiat_observations(raw.read_bytes(), "EURUSD=X", "EUR")
    assert meta["exchange_timezone"] == "Europe/London"
    assert observed[0][1] is None
    coverage = json.loads((run / "coverage.json").read_text())
    assert coverage["groups"][0]["symbols"][0]["null_closes"] == 1
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


# ------------------------------------------------------------ fail closed


def _rejected(tmp_path: Path, raw: bytes, match: str, **kw: Any) -> dict[str, Any]:
    with pytest.raises(driver.DriverError, match="rejected"):
        _fetch(tmp_path, raw, run_id="bad", **kw)
    assert not (tmp_path / "stage" / "bad").exists()
    report = json.loads((tmp_path / "stage" / "bad.REJECTED.json").read_text())
    assert any(match in message for message in report["failures"]), report["failures"]
    return report


def test_empty_provider_response_is_rejected(tmp_path: Path) -> None:
    _rejected(tmp_path, _config(), "missing required ticker", yf=FakeYF(empty={"BBB"}))


def test_short_history_is_rejected(tmp_path: Path) -> None:
    raw = _config([_group(min_rows=60)])
    _rejected(tmp_path, raw, "short")


def test_late_last_bar_is_rejected_then_labelled(tmp_path: Path) -> None:
    yf = FakeYF(drop_after={"AAA": date(2026, 10, 7)})
    _rejected(tmp_path, _config(), "late: last bar 2026-10-07", yf=yf)
    labelled = _config(
        [
            _group(
                exceptions=[
                    {"symbol": "AAA", "rule": "last_bar", "label": "DELISTED_20261007"}
                ]
            )
        ]
    )
    run = _fetch(tmp_path, labelled, run_id="run-ok", yf=yf)
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["status"] == "LABELLED"
    assert entry["last_bar_status"] == "LATE_LABELLED"
    assert entry["exceptions_applied"][0]["label"] == "DELISTED_20261007"
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


def test_weekend_cutoff_expects_the_prior_friday(tmp_path: Path) -> None:
    raw = json.loads(_config())
    raw["cutoff"] = "2026-10-10"  # Saturday
    run = _fetch(tmp_path, json.dumps(raw).encode(), run_id="sat")
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["expected_last_session"] == "2026-10-09"
    assert entry["last_bar"] == "2026-10-09"


def test_late_first_bar_is_rejected(tmp_path: Path) -> None:
    _rejected(
        tmp_path,
        _config(),
        "first bar",
        yf=FakeYF(start_after={"BBB": date(2026, 9, 20)}),
    )


def test_missing_sessions_over_the_limit_are_rejected(tmp_path: Path) -> None:
    _rejected(tmp_path, _config(), "missing sessions", yf=FakeYF(skip_days={"AAA": 6}))


def test_open_session_is_rejected(tmp_path: Path) -> None:
    config = driver.parse_config(_config())
    with pytest.raises(driver.DriverError, match="rejected"):
        driver.run_fetch(
            config,
            stage_root=tmp_path / "s",
            run_id="today",
            yf=FakeYF(),
            chart=FakeChart(),
            now=datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc),
        )


def test_chart_bar_after_cutoff_and_empty_chart_are_rejected(tmp_path: Path) -> None:
    groups = [_group("fx", kind="chart", symbols=["EURUSD=X"])]
    run = _fetch(
        tmp_path, _config(groups), run_id="trim", chart=FakeChart(extra_day=True)
    )
    raw = run / "raw/fx/EURUSD_X.chart.json"
    derived = run / "derived/fx/EURUSD_X.chart.json"
    manifest = json.loads((run / "MANIFEST.json").read_text())
    assert {"raw/fx/EURUSD_X.chart.json", "derived/fx/EURUSD_X.chart.json"} <= set(
        manifest["files"]
    )
    assert raw.read_bytes() != derived.read_bytes()
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["raw_bars_after_through_dropped"] > 0
    assert entry["status"] == "PASS"
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"
    derived.write_bytes(raw.read_bytes())
    _rewrite_manifest(run, "derived/fx/EURUSD_X.chart.json")
    with pytest.raises(driver.DriverError, match="cutoff-clean"):
        driver.verify_run(run)
    groups2 = [_group("fx", kind="chart", symbols=["EURUSD=X"], max_null_closes=0)]
    with pytest.raises(driver.DriverError):
        _fetch(
            tmp_path,
            _config(groups2),
            run_id="empty",
            chart=FakeChart(empty={"EURUSD=X"}),
        )
    with pytest.raises(driver.DriverError):
        _fetch(tmp_path, _config(groups2), run_id="nulls", chart=FakeChart(nulls=2))
    with pytest.raises(driver.DriverError):
        _fetch(
            tmp_path,
            _config(groups2),
            run_id="http",
            chart=FakeChart(fail={"EURUSD=X"}),
        )


# -------------------------------------------------------------- resolution


def _resolution_config(status: str = "UNRESOLVED") -> bytes:
    confirmed = {
        "probe_run_id": "probe-1",
        "raw_sha256": "a" * 64,
        "confirmed_by": "coordinator",
        "note": "name and currency match",
    }
    entry: dict[str, Any] = {
        "id": "ASB",
        "status": status,
        "resolved_ticker": "ASB.WA" if status == "CONFIRMED" else None,
        "candidates": [{"ticker": "ASB.WA", "note": "ASBISc, Warsaw"}],
    }
    if status == "CONFIRMED":
        entry["evidence"] = confirmed
    return _config([_group(symbols=["AAA", "ASB"])], symbol_resolution=[entry])


def test_unresolved_id_is_never_fetched_as_a_plain_ticker(tmp_path: Path) -> None:
    yf = FakeYF()
    with pytest.raises(driver.DriverError, match="UNRESOLVED"):
        _fetch(tmp_path, _resolution_config(), yf=yf)
    assert yf.calls == []
    run = _fetch(
        tmp_path, _resolution_config(), run_id="skip", yf=yf, allow_unresolved=True
    )
    assert [t for call in yf.calls for t in call] == ["AAA"]
    manifest = json.loads((run / "MANIFEST.json").read_text())
    assert manifest["not_fetched"][0]["label"] == "NOT_FETCHED_UNRESOLVED"
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


def test_confirmed_id_is_fetched_through_its_resolved_ticker(tmp_path: Path) -> None:
    yf = FakeYF()
    run = _fetch(tmp_path, _resolution_config("CONFIRMED"), yf=yf)
    assert sorted(t for call in yf.calls for t in call) == ["AAA", "ASB.WA"]
    coverage = json.loads((run / "coverage.json").read_text())
    ids = {(e["id"], e["ticker"]) for e in coverage["groups"][0]["symbols"]}
    assert ("ASB", "ASB.WA") in ids


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(status="UNRESOLVED"),
        lambda e: e.pop("evidence"),
        lambda e: e["evidence"].update(raw_sha256="xyz"),
        lambda e: e["evidence"].pop("confirmed_by"),
    ],
)
def test_resolution_entry_shape_is_enforced(mutate: Any) -> None:
    document = json.loads(_resolution_config("CONFIRMED"))
    entry = document["symbol_resolution"][0]
    mutate(entry)
    with pytest.raises(driver.DriverError):
        driver.parse_config(json.dumps(document).encode())


def test_probe_tries_candidates_and_admits_nothing(tmp_path: Path) -> None:
    document = json.loads(_resolution_config())
    document["symbol_resolution"][0]["search_query"] = "ASBISc Enterprises"
    document["symbol_resolution"].append(
        {
            "id": "FDEV",
            "status": "UNRESOLVED",
            "resolved_ticker": None,
            "candidates": [],
        }
    )
    document["groups"][0]["symbols"].append("FDEV")
    config = driver.parse_config(json.dumps(document).encode())
    chart = FakeChart()
    run = driver.run_probe(
        config, stage_root=tmp_path / "s", run_id="probe-1", chart=chart, now=NOW
    )
    report = json.loads((run / "probe-report.json").read_text())
    assert report["label"] == "CANDIDATE_NOT_ADMITTED"
    asb, fdev = report["items"]
    assert asb["candidates"][0]["result"] == "FETCHED"
    assert asb["candidates"][0]["meta"]["long_name"] == "ASB.WA synthetic"
    assert asb["search"]["query"] == "ASBISc Enterprises"
    assert "NO_CANDIDATES" in fdev["note"]
    assert chart.calls == ["ASB.WA"]
    assert driver.verify_run(run)["status"] == "VERIFIED_NOT_ADMITTED"
    assert config.resolution_for("ASB").status == "UNRESOLVED"  # type: ignore[union-attr]


def test_probe_records_a_failed_candidate(tmp_path: Path) -> None:
    config = driver.parse_config(_resolution_config())
    run = driver.run_probe(
        config,
        stage_root=tmp_path / "s",
        run_id="probe-2",
        chart=FakeChart(fail={"ASB.WA"}),
        now=NOW,
    )
    item = json.loads((run / "probe-report.json").read_text())["items"][0]
    assert item["candidates"][0]["result"] == "FETCH_ERROR"


# -------------------------------------------------------------- governance


def _evidence(config: driver.Config, **overrides: Any) -> bytes:
    document = {
        "schema_version": 1,
        "run_id": "live-1",
        "config_sha256": config.sha256,
        "cutoff": CUTOFF,
        "review": "https://github.com/dachent/quant_engine/issues/1#issuecomment-123",
        "valid_from_utc": "2026-10-12T10:00:00Z",
        "valid_until_utc": "2026-10-12T20:00:00Z",
        "max_requests": 3,
    }
    document.update(overrides)
    return json.dumps(document).encode()


def test_governance_accepts_once_and_binds_everything(tmp_path: Path) -> None:
    config = driver.parse_config(_config())
    evidence = _evidence(config)
    budget = driver.authorize_live(
        evidence, config=config, run_id="live-1", stage_root=tmp_path, now=NOW
    )
    assert budget == 3
    with pytest.raises(driver.GovernanceError, match="already used"):
        driver.authorize_live(
            evidence, config=config, run_id="live-1", stage_root=tmp_path, now=NOW
        )


@pytest.mark.parametrize(
    "override",
    [
        {"run_id": "other"},
        {"config_sha256": "0" * 64},
        {"cutoff": "2026-10-08"},
        {"review": "https://example.com/x"},
        {"valid_until_utc": "2026-10-12T14:00:00Z"},
        {"valid_from_utc": "2026-10-11T00:00:00Z"},
        {"max_requests": 0},
        {"max_requests": True},
    ],
)
def test_governance_refusals(tmp_path: Path, override: dict[str, Any]) -> None:
    config = driver.parse_config(_config())
    with pytest.raises(driver.GovernanceError):
        driver.authorize_live(
            _evidence(config, **override),
            config=config,
            run_id="live-1",
            stage_root=tmp_path,
            now=NOW,
        )


def test_request_budget_is_enforced_and_not_swallowed(tmp_path: Path) -> None:
    config = driver.parse_config(_config())
    with pytest.raises(driver.GovernanceError, match="budget"):
        driver.run_fetch(
            config,
            stage_root=tmp_path / "s",
            run_id="bud-1",
            yf=FakeYF(),
            chart=FakeChart(),
            now=NOW,
            max_requests=0,
        )


# ------------------------------------------------------------------ verify


def _rewrite_manifest(run: Path, name: str) -> None:
    manifest = json.loads((run / "MANIFEST.json").read_text())
    manifest["files"][name] = driver._file_record((run / name).read_bytes())
    (run / "MANIFEST.json").write_bytes(driver.pretty(manifest))


def test_verify_rejects_tampering(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    raw = run / "raw/daily/history.csv"
    original = raw.read_bytes()
    raw.write_bytes(original.replace(b"100.0", b"100.5", 1))
    with pytest.raises(driver.DriverError, match="differs from its recorded hash"):
        driver.verify_run(run)
    raw.write_bytes(original)
    (run / "stray.txt").write_bytes(b"x")
    with pytest.raises(driver.DriverError, match="unbound file stray.txt"):
        driver.verify_run(run)
    (run / "stray.txt").unlink()
    (run / "coverage.json").unlink()
    with pytest.raises(driver.DriverError, match="missing file coverage.json"):
        driver.verify_run(run)


def test_verify_rederives_coverage_and_rows_from_raw_bytes(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    name = "artifacts/yahoo-daily-history.json"
    document = json.loads((run / name).read_text())
    document["rows"][0]["Close"] = document["rows"][0]["Close"] + 1.0
    (run / name).write_bytes((json.dumps(document, indent=2) + "\n").encode())
    _rewrite_manifest(run, name)
    with pytest.raises(driver.DriverError) as excinfo:
        driver.verify_run(run)
    message = str(excinfo.value)
    assert "differs from its manifest" in message
    assert "differs from the raw bytes" in message


def test_verify_rejects_a_raw_file_edited_with_a_forged_manifest(
    tmp_path: Path,
) -> None:
    run = _fetch(tmp_path)
    raw = run / "raw/daily/history.csv"
    lines = raw.read_bytes().decode().splitlines(keepends=True)
    raw.write_bytes("".join(lines[:-3]).encode())  # drop the last three bars
    _rewrite_manifest(run, "raw/daily/history.csv")
    with pytest.raises(driver.DriverError) as excinfo:
        driver.verify_run(run)
    assert "late: last bar" in str(excinfo.value) or "raw staging" in str(excinfo.value)


def test_verify_rejects_edited_config(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    document = json.loads((run / "config.json").read_text())
    document["groups"][0]["min_rows"] = 1
    (run / "config.json").write_bytes(driver.pretty(document))
    _rewrite_manifest(run, "config.json")
    with pytest.raises(driver.DriverError, match="not the bound effective config"):
        driver.verify_run(run)


# --------------------------------------------------------- retained / CLI


def test_verify_retained_applies_the_same_rules(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    artifact = run / "artifacts/yahoo-daily-history_adjusted.json"
    manifest = artifact.with_name(artifact.name + ".manifest.json")
    kwargs: dict[str, Any] = dict(
        manifest=manifest, calendar="weekday", min_rows=20, max_missing_weekdays=3
    )
    good = driver.verify_retained(artifact, cutoff=date(2026, 10, 9), **kwargs)
    assert good["problems"] == [] and len(good["symbols"]) == 2
    late = driver.verify_retained(artifact, cutoff=date(2026, 10, 14), **kwargs)
    assert any("late" in p for p in late["problems"])
    early = driver.verify_retained(artifact, cutoff=date(2026, 10, 7), **kwargs)
    assert any("after the declared through" in p for p in early["problems"])
    with pytest.raises(driver.DriverError, match="expected sha256"):
        driver.verify_retained(
            artifact, cutoff=date(2026, 10, 9), sha256_expected="0" * 64, **kwargs
        )


def test_cli_verify_and_config_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _fetch(tmp_path)
    assert driver.main(["verify", str(run)]) == 0
    config_path = tmp_path / "c.json"
    config_path.write_bytes(_resolution_config())
    assert driver.main(["config-check", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert '"status": "UNRESOLVED"' in out
    artifact = run / "artifacts/yahoo-daily-history_adjusted.json"
    report = tmp_path / "rep.json"
    args = [
        "verify-retained",
        "--artifact",
        str(artifact),
        "--manifest",
        str(artifact) + ".manifest.json",
        "--cutoff",
        CUTOFF,
        "--min-rows",
        "20",
        "--max-missing-weekdays",
        "3",
        "--report",
        str(report),
    ]
    assert driver.main(args) == 0
    assert report.exists()
    assert driver.main(args) == 1  # report path is create-only


def test_cli_refuses_a_broken_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _fetch(tmp_path)
    (run / "MANIFEST.json").unlink()
    assert driver.main(["verify", str(run)]) == 1
    assert "Error" in capsys.readouterr().err


def test_intraday_bars_are_counted_by_timestamp_not_by_day(tmp_path: Path) -> None:
    groups = [_group("hourly", interval="60m", min_rows=7 * 20, max_missing_weekdays=3)]
    run = _fetch(tmp_path, _config(groups), run_id="hourly")
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["rows"] == 7 * 29  # 29 weekdays, 7 bars each
    assert entry["duplicate_dates"] == 0
    assert entry["status"] == "PASS"
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"
    short = _config([_group("hourly", interval="60m", min_rows=7 * 30)])
    _rejected(tmp_path, short, "short", yf=FakeYF())


def test_duplicate_intraday_timestamp_is_still_a_duplicate() -> None:
    group = driver.parse_config(_config()).groups[0]
    entry = driver.evaluate_series(
        group,
        "AAA",
        "AAA",
        ["2026-09-01T09:00:00", "2026-09-01T09:00:00"],
        null_closes=0,
    )
    assert any("duplicate" in m for m in entry["failures"])


def test_null_raw_close_counts_against_the_null_rule(tmp_path: Path) -> None:
    _rejected(tmp_path, _config(), "null closes", yf=FakeYF(null_close={"AAA"}))
    labelled = _config([_group(max_null_closes=1)])
    run = _fetch(tmp_path, labelled, run_id="nulls", yf=FakeYF(null_close={"AAA"}))
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["null_closes"] == 1


def test_every_group_is_resolved_before_any_transport_call(tmp_path: Path) -> None:
    pending = _group("pending", symbols=["ASB"])
    document = json.loads(_resolution_config())
    document["groups"] = [_group(), pending]
    config = driver.parse_config(json.dumps(document).encode())
    yf = FakeYF()
    with pytest.raises(driver.DriverError, match="UNRESOLVED"):
        driver.preflight(config)
    with pytest.raises(driver.DriverError, match="UNRESOLVED"):
        driver.run_fetch(
            config,
            stage_root=tmp_path / "s",
            run_id="pre-1",
            yf=yf,
            chart=FakeChart(),
            now=NOW,
        )
    assert yf.calls == []
    assert not (tmp_path / "s" / "pre-1").exists()
    assert driver.preflight(config, allow_unresolved=True)[0]["id"] == "ASB"


# ------------------------------------------------- derived TT symbol set


def _tt_manifest(tmp_path: Path, symbols_at_start: list[str], later: list[str]) -> Path:
    document = {
        "portfolio": {"live_inception": "2026-08-31"},
        "selected_components": [
            {
                "dated_holdings": {
                    "snapshots": [
                        {
                            "observed_date": "2026-08-28",
                            "weights": {s: 0.1 for s in symbols_at_start},
                        },
                        {
                            "observed_date": "2026-09-03",
                            "weights": {s: 0.1 for s in later},
                        },
                        {"observed_date": "2026-12-01", "weights": {"LATE": 1.0}},
                    ]
                }
            }
        ],
    }
    path = tmp_path / "tt.json"
    path.write_text(json.dumps(document))
    return path


def _derived_config() -> bytes:
    group = _group("tt-daily", symbols_from="tt-source-manifest")
    del group["symbols"], group["start"]
    group.update(
        symbols_must_include=["BNY", "DUK"], start_offset_days_before_inception=7
    )
    return _config([group])


def test_tt_symbols_are_derived_not_listed(tmp_path: Path) -> None:
    manifest = _tt_manifest(tmp_path, ["AAA", "BRK.B"], ["BNY", "DUK"])
    config = driver.parse_config(_derived_config(), tt_source_manifest=manifest)
    group = config.groups[0]
    assert group.symbols == ("AAA", "BNY", "BRK-B", "DUK")  # LATE is after the cutoff
    assert group.start == date(2026, 8, 24)
    assert config.document["derivations"]["tt-source-manifest:2026-10-09"][
        "inception"
    ] == ("2026-08-31")
    with pytest.raises(driver.DriverError, match="needs --tt-source-manifest"):
        driver.parse_config(_derived_config())
    stale = _tt_manifest(tmp_path, ["AAA"], ["DUK"])
    with pytest.raises(driver.DriverError, match=r"lacks required \['BNY'\]"):
        driver.parse_config(_derived_config(), tt_source_manifest=stale)


def test_derived_group_refuses_a_hand_list_and_stages_resolved(tmp_path: Path) -> None:
    manifest = _tt_manifest(tmp_path, ["AAA"], ["BNY", "DUK"])
    document = json.loads(_derived_config())
    document["groups"][0]["symbols"] = ["AAA"]
    with pytest.raises(driver.DriverError, match="must not list"):
        driver.parse_config(json.dumps(document).encode(), tt_source_manifest=manifest)
    config = driver.parse_config(_derived_config(), tt_source_manifest=manifest)
    run = driver.run_fetch(
        config,
        stage_root=tmp_path / "s",
        run_id="tt-run",
        yf=FakeYF(),
        chart=FakeChart(),
        now=NOW,
    )
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"
    staged = json.loads((run / "config.json").read_text())
    assert staged["groups"][0]["symbols"] == ["AAA", "BNY", "DUK"]
    with pytest.raises(driver.DriverError, match="derivations are written"):
        driver.parse_config((run / "config.json").read_bytes())


# ------------------------------------------- reference-based admission

HOLD = date(2026, 9, 7)


def _points(values: dict[str, float]) -> list[dict[str, Any]]:
    return [{"date": d, "value": v} for d, v in values.items()]


def _reference(**over: Any) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "kind": "equity",
        "name": "ASBISc Enterprises Plc",
        "name_tokens": ["asbis"],
        "country": "POL",
        "currency": "PLN",
        "accepted_exchanges": ["WSE"],
        "tolerance": 0.01,
        "min_points": 3,
        "points": _points(
            {
                "2026-09-01": 100.0,
                "2026-09-02": 102.0,
                "2026-09-03": 104.0,
                "2026-09-04": 99.0,
            }
        ),
        "sources": [{"artifact": "ibkr-catalog", "sha256": "a" * 64, "note": "marks"}],
    }
    ref.update(over)
    return ref


def _equity_chart(
    ticker: str,
    closes: dict[str, float],
    *,
    currency: str = "PLN",
    exchange: str = "WSE",
    long_name: str = "ASBISc Enterprises Plc",
) -> bytes:
    zone = ZoneInfo("Europe/Warsaw")
    days = sorted(closes)
    stamps = [
        int(datetime(*map(int, d.split("-")), 9, 0, tzinfo=zone).timestamp())
        for d in days
    ]
    body = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": currency,
                        "symbol": ticker,
                        "exchangeName": exchange,
                        "dataGranularity": "1d",
                        "exchangeTimezoneName": "Europe/Warsaw",
                        "longName": long_name,
                    },
                    "timestamp": stamps,
                    "indicators": {"quote": [{"close": [closes[d] for d in days]}]},
                }
            ]
        }
    }
    return json.dumps(body).encode()


GOOD = {
    "2026-09-01": 100.2,
    "2026-09-02": 101.8,
    "2026-09-03": 104.1,
    "2026-09-04": 99.3,
}


def _probe_config(
    reference: dict[str, Any] | None = None, extra: list[str] | None = None
) -> bytes:
    entry = {
        "id": "ASB",
        "status": "UNRESOLVED",
        "resolved_ticker": None,
        "candidates": [
            {"ticker": t, "note": "candidate"} for t in ["ASB.WA", *(extra or [])]
        ],
        "reference": reference if reference is not None else _reference(),
    }
    return _config([_group(symbols=["AAA", "ASB"])], symbol_resolution=[entry])


def _recorded(tmp_path: Path, files: dict[str, bytes]) -> Path:
    directory = tmp_path / "rec"
    directory.mkdir(exist_ok=True)
    for ticker, payload in files.items():
        (directory / f"{driver._safe_name(ticker)}.chart.json").write_bytes(payload)
    return directory


def _probe(
    tmp_path: Path, raw: bytes, files: dict[str, bytes], run_id: str = "probe-a"
) -> Path:
    config = driver.parse_config(raw)
    return driver.run_probe(
        config,
        stage_root=tmp_path / "s",
        run_id=run_id,
        chart=driver.RecordedChart(_recorded(tmp_path, files)),
        now=NOW,
    )


def test_matching_candidate_is_admitted_and_applied(tmp_path: Path) -> None:
    run = _probe(tmp_path, _probe_config(), {"ASB.WA": _equity_chart("ASB.WA", GOOD)})
    item = json.loads((run / "probe-report.json").read_text())["items"][0]
    assert item["resolution"]["status"] == "ADMITTED"
    assert item["candidates"][0]["judgement"]["verdict"] == "ADMITTED"
    assert driver.verify_run(run)["status"] == "VERIFIED_NOT_ADMITTED"
    out = driver.apply_admissions(_probe_config(), run)
    config = driver.parse_config(out)
    entry = config.resolution_for("ASB")
    assert entry is not None and entry.status == "CONFIRMED"
    assert entry.resolved_ticker == "ASB.WA"
    assert entry.evidence is not None and entry.evidence["probe_run_id"] == "probe-a"
    assert driver.resolve_group(config.groups[0], config, allow_unresolved=False)[0][
        1
    ] == (
        "ASB",
        "ASB.WA",
    )
    with pytest.raises(driver.DriverError, match="different config"):
        driver.apply_admissions(_probe_config(_reference(tolerance=0.02)), run)


@pytest.mark.parametrize(
    ("chart_kwargs", "closes", "failed"),
    [
        ({"currency": "EUR"}, GOOD, "currency"),
        ({"exchange": "NMS"}, GOOD, "exchange"),
        ({"long_name": "Other Corp"}, GOOD, "name"),
        ({}, {k: v * 1.05 for k, v in GOOD.items()}, "price_points"),
        ({}, {"2026-09-01": 100.0}, "price_points"),
    ],
)
def test_any_disagreement_keeps_the_id_unresolved(
    tmp_path: Path, chart_kwargs: dict[str, Any], closes: dict[str, float], failed: str
) -> None:
    run = _probe(
        tmp_path,
        _probe_config(),
        {"ASB.WA": _equity_chart("ASB.WA", closes, **chart_kwargs)},
    )
    item = json.loads((run / "probe-report.json").read_text())["items"][0]
    assert item["resolution"]["status"] == "UNRESOLVED"
    assert failed in item["candidates"][0]["judgement"]["failed"]
    assert json.loads((run / "admissions.json").read_text())["admitted"] == []
    out = driver.apply_admissions(_probe_config(), run)
    assert driver.parse_config(out).resolution_for("ASB").status == "UNRESOLVED"  # type: ignore[union-attr]


def test_pence_quotes_are_rescaled_and_read_as_gbp(tmp_path: Path) -> None:
    ref = _reference(
        currency="GBP", accepted_exchanges=["LSE"], name_tokens=["frontier"]
    )
    closes = {k: v * 100 for k, v in GOOD.items()}
    chart = _equity_chart(
        "FDEV.L",
        closes,
        currency="GBp",
        exchange="LSE",
        long_name="Frontier Developments",
    )
    raw = _probe_config(ref).replace(b"ASB.WA", b"FDEV.L")
    run = _probe(tmp_path, raw, {"FDEV.L": chart})
    item = json.loads((run / "probe-report.json").read_text())["items"][0]
    assert item["resolution"] == {
        "status": "ADMITTED",
        "ticker": "FDEV.L",
        "raw_file": "raw/ASB/FDEV.L.chart.json",
        "raw_sha256": driver.sha256_hex(chart),
    }


def test_missing_series_two_matches_and_no_reference(tmp_path: Path) -> None:
    run = _probe(tmp_path, _probe_config(), {}, run_id="probe-none")
    item = json.loads((run / "probe-report.json").read_text())["items"][0]
    assert item["candidates"][0]["judgement"]["verdict"] == "NO_SERIES"
    assert item["resolution"]["status"] == "UNRESOLVED"
    both = {
        "ASB.WA": _equity_chart("ASB.WA", GOOD),
        "ASB.XX": _equity_chart("ASB.XX", GOOD),
    }
    run2 = _probe(tmp_path, _probe_config(extra=["ASB.XX"]), both, run_id="probe-two")
    assert (
        json.loads((run2 / "probe-report.json").read_text())["items"][0]["resolution"][
            "status"
        ]
        == "AMBIGUOUS"
    )
    document = json.loads(_probe_config())
    del document["symbol_resolution"][0]["reference"]
    run3 = _probe(
        tmp_path,
        json.dumps(document).encode(),
        {"ASB.WA": _equity_chart("ASB.WA", GOOD)},
        run_id="probe-noref",
    )
    cand = json.loads((run3 / "probe-report.json").read_text())["items"][0][
        "candidates"
    ][0]
    assert cand["judgement"]["verdict"] == "NO_REFERENCE"


def test_probe_verify_rejudges_from_raw_bytes(tmp_path: Path) -> None:
    run = _probe(tmp_path, _probe_config(), {"ASB.WA": _equity_chart("ASB.WA", GOOD)})
    raw = run / "raw/ASB/ASB.WA.chart.json"
    bad = _equity_chart("ASB.WA", {k: v * 1.5 for k, v in GOOD.items()})
    raw.write_bytes(bad)
    manifest = json.loads((run / "MANIFEST.json").read_text())
    manifest["files"]["raw/ASB/ASB.WA.chart.json"] = driver._file_record(bad)
    (run / "MANIFEST.json").write_bytes(driver.pretty(manifest))
    with pytest.raises(driver.DriverError, match="hash differs|judgement differs"):
        driver.verify_run(run)


def test_reference_shape_is_enforced() -> None:
    for mutate in (
        lambda r: r.update(tolerance=0.5),
        lambda r: r.update(min_points=9),
        lambda r: r.update(kind="bond"),
        lambda r: r["points"].append(dict(r["points"][0])),
        lambda r: r["sources"][0].update(sha256="zz"),
        lambda r: r.update(accepted_exchanges=[]),
    ):
        ref = _reference()
        mutate(ref)
        with pytest.raises(driver.DriverError):
            driver.parse_config(_probe_config(ref))


def test_cli_offline_probe_and_apply(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_bytes(_probe_config())
    rec = _recorded(tmp_path, {"ASB.WA": _equity_chart("ASB.WA", GOOD)})
    stage = tmp_path / "stage"
    assert (
        driver.main(
            [
                "probe",
                "--config",
                str(cfg),
                "--stage-root",
                str(stage),
                "--run-id",
                "cli-probe",
                "--recorded-dir",
                str(rec),
            ]
        )
        == 0
    )
    out = tmp_path / "confirmed.json"
    assert (
        driver.main(
            [
                "apply-admissions",
                "--config",
                str(cfg),
                "--probe-run",
                str(stage / "cli-probe"),
                "--out",
                str(out),
                "--cutoff",
                CUTOFF,
            ]
        )
        == 0
    )
    assert (
        driver.parse_config(out.read_bytes()).resolution_for("ASB").status
        == "CONFIRMED"
    )  # type: ignore[union-attr]
    assert (
        driver.main(
            [
                "apply-admissions",
                "--config",
                str(cfg),
                "--probe-run",
                str(stage / "cli-probe"),
                "--out",
                str(out),
                "--cutoff",
                CUTOFF,
            ]
        )
        == 1
    )


def test_retained_chart_derive_out_is_cutoff_clean(tmp_path: Path) -> None:
    raw = FakeChart(extra_day=True).chart(
        "EURUSD=X",
        int(datetime(2026, 8, 31, 12, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()),
    )
    artifact = tmp_path / "chart.json"
    artifact.write_bytes(raw)
    common: dict[str, Any] = dict(
        manifest=None,
        cutoff=date(2026, 10, 9),
        calendar="weekday",
        min_rows=10,
        max_missing_weekdays=3,
        kind="chart",
        symbol="EURUSD=X",
        start=date(2026, 9, 1),
    )
    flagged = driver.verify_retained(artifact, **common)
    assert any("after the declared through" in p for p in flagged["problems"])
    out = tmp_path / "derived.json"
    clean = driver.verify_retained(artifact, derive_out=out, **common)
    assert clean["problems"] == []
    assert clean["derived_sha256"] == driver.sha256_hex(out.read_bytes())
    assert artifact.read_bytes() == raw
    with pytest.raises(FileExistsError):
        driver.verify_retained(artifact, derive_out=out, **common)


# ----------------------------------------------- official closes (Schwab ingest)


def _positions(tmp_path: Path, symbols: list[str]) -> Path:
    path = tmp_path / "positions.json"
    path.write_text(json.dumps({"positions": [{"symbol": s} for s in symbols]}))
    return path


def _held_config() -> bytes:
    group = _group("held-closes", symbols_from="schwab-positions", datasets=["history"])
    del group["symbols"], group["start"]
    group.update(
        start_offset_days_before_through=10,
        min_rows=5,
        official_closes={},
    )
    return _config([group])


def test_official_close_files_carry_the_raw_close_not_adj_close(tmp_path: Path) -> None:
    positions = _positions(tmp_path, ["SPY", "BRK/B", "SPY"])
    config = driver.parse_config(_held_config(), schwab_positions=positions)
    group = config.groups[0]
    assert group.symbols == ("BRK-B", "SPY")
    assert group.official_names == {"BRK-B": "BRK.B", "SPY": "SPY"}
    run = driver.run_fetch(
        config,
        stage_root=tmp_path / "s",
        run_id="held-run",
        yf=FakeYF(),
        chart=FakeChart(),
        now=NOW,
    )
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"
    files = sorted(p.name for p in (run / "official-closes").iterdir())
    assert files == [
        "BRK.B_yahoo_daily.csv",
        "SPY_yahoo_daily.csv",
        "official-closes-manifest.json",
    ]
    spy = (run / "official-closes/SPY_yahoo_daily.csv").read_text().splitlines()
    assert spy[0] == "date,open,high,low,close,adjclose,volume,dividend"
    last = spy[-1].split(",")
    assert last[0] == CUTOFF
    close, adj = float(last[4]), float(last[5])
    assert adj == pytest.approx(close * 0.98)  # the fake gives Adj Close = 0.98 x Close
    assert close != adj
    raw = (run / "raw/held-closes/history.csv").read_text().splitlines()
    header = raw[0].split(",")
    spy_last = [r.split(",") for r in raw[1:] if r.startswith("SPY,")][-1]
    assert header[5] == "Close" and header[6] == "Adj Close"
    assert (
        spy_last[5] == last[4]
    )  # official `close` is the raw `Close` column, text for text
    coverage = json.loads((run / "coverage.json").read_text())["official_closes"]
    assert coverage["names"]["SPY"]["cutoff_close"] == last[4]


def test_the_schwab_ingest_reader_takes_the_driver_files(tmp_path: Path) -> None:
    from decimal import Decimal

    schwab_generation = importlib.import_module("schwab_generation")
    positions = _positions(tmp_path, ["SPY", "SPXL"])
    config = driver.parse_config(_held_config(), schwab_positions=positions)
    run = driver.run_fetch(
        config,
        stage_root=tmp_path / "s",
        run_id="ingest-run",
        yf=FakeYF(),
        chart=FakeChart(),
        now=NOW,
    )
    closes = schwab_generation._official_closes(
        run / "official-closes", date(2026, 10, 9)
    )
    assert set(closes) == {"SPXL", "SPY"}
    raw = {
        r.split(",")[0]: r.split(",")[5]
        for r in (run / "raw/held-closes/history.csv").read_text().splitlines()[1:]
        if r.split(",")[1].startswith(CUTOFF)
    }
    assert closes["SPY"] == Decimal(raw["SPY"])
    assert closes["SPY"] != Decimal(raw["SPY"]) * Decimal("0.98")


def test_official_close_file_tamper_and_missing_cutoff_bar(tmp_path: Path) -> None:
    positions = _positions(tmp_path, ["SPY"])
    config = driver.parse_config(_held_config(), schwab_positions=positions)
    run = driver.run_fetch(
        config,
        stage_root=tmp_path / "s",
        run_id="tamper-run",
        yf=FakeYF(),
        chart=FakeChart(),
        now=NOW,
    )
    target = run / "official-closes/SPY_yahoo_daily.csv"
    original = target.read_bytes()
    lines = original.decode().splitlines()
    cols = lines[-1].split(",")
    cols[4] = cols[5]  # swap the raw close for the adjusted close
    target.write_bytes(("\n".join([*lines[:-1], ",".join(cols)]) + "\n").encode())
    _rewrite_manifest(run, "official-closes/SPY_yahoo_daily.csv")
    with pytest.raises(driver.DriverError, match="raw-Close file"):
        driver.verify_run(run)
    labelled = json.loads(_held_config())
    labelled["groups"][0]["exceptions"] = []
    with pytest.raises(driver.DriverError, match="rejected"):
        driver.run_fetch(
            config,
            stage_root=tmp_path / "s2",
            run_id="late-run",
            yf=FakeYF(drop_after={"SPY": date(2026, 10, 7)}),
            chart=FakeChart(),
            now=NOW,
        )


def test_official_closes_need_a_daily_history_group_and_schwab_positions(
    tmp_path: Path,
) -> None:
    with pytest.raises(driver.DriverError, match="needs --schwab-positions"):
        driver.parse_config(_held_config())
    bad = json.loads(_config([_group(interval="60m", official_closes={})]))
    with pytest.raises(driver.DriverError, match="official closes need"):
        driver.parse_config(json.dumps(bad).encode())
    with pytest.raises(driver.DriverError, match="no positions"):
        driver.parse_config(_held_config(), schwab_positions=_positions(tmp_path, []))


# --------------------------------------- fetch timestamps and official manifest


def _held_run(tmp_path: Path, run_id: str = "time-run", **kw: Any) -> Path:
    config = driver.parse_config(
        _held_config(), schwab_positions=_positions(tmp_path, ["SPY", "BRK/B"])
    )
    return driver.run_fetch(
        config,
        stage_root=tmp_path / "s",
        run_id=run_id,
        yf=FakeYF(),
        chart=FakeChart(),
        **kw,
    )


def test_manifest_records_fetch_times_from_the_governance_clock(tmp_path: Path) -> None:
    ticks = iter([datetime(2026, 10, 12, 15, 7, tzinfo=timezone.utc)])
    run = _held_run(tmp_path, now=NOW, clock=lambda: next(ticks))
    manifest = json.loads((run / "MANIFEST.json").read_text())
    assert manifest["fetch_started_utc"] == "2026-10-12T15:00:00Z"
    assert manifest["fetch_completed_utc"] == "2026-10-12T15:07:00Z"
    assert manifest["cutoff"] == CUTOFF
    sidecar = json.loads(
        (run / "official-closes/official-closes-manifest.json").read_text()
    )
    assert sidecar["fetch_started_utc"] == manifest["fetch_started_utc"]
    assert sidecar["fetch_completed_utc"] == manifest["fetch_completed_utc"]
    assert sidecar["cutoff"] == CUTOFF
    assert sidecar["name_rule"].startswith("Schwab symbol with '/' written as '.'")
    by_name = {f["symbol"]: f for f in sidecar["files"]}
    assert set(by_name) == {"BRK.B", "SPY"}
    spy = by_name["SPY"]
    body = (run / "official-closes" / spy["file"]).read_bytes()
    assert spy["sha256"] == driver.sha256_hex(body) and spy["size_bytes"] == len(body)
    assert spy["cutoff_bar"] == CUTOFF and spy["last_bar"] == CUTOFF
    assert spy["cutoff_close"] == body.decode().splitlines()[-1].split(",")[4]
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


def test_clock_that_runs_backwards_is_refused(tmp_path: Path) -> None:
    with pytest.raises(driver.DriverError, match="completed before it started"):
        _held_run(
            tmp_path,
            now=NOW,
            clock=lambda: datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc),
        )


def _patch_header(run: Path, **changes: Any) -> None:
    manifest = json.loads((run / "MANIFEST.json").read_text())
    for key, value in changes.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    (run / "MANIFEST.json").write_bytes(driver.pretty(manifest))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"fetch_started_utc": None}, "fetch_started_utc is missing"),
        ({"fetch_completed_utc": None}, "fetch_completed_utc is missing"),
        ({"fetch_completed_utc": "2026-10-12T14:00:00Z"}, "precedes"),
        ({"fetch_started_utc": "2026-10-12T15:00:00+02:00"}, "not a Z timestamp"),
        (
            {
                "fetch_started_utc": "2026-10-09T20:00:00Z",
                "fetch_completed_utc": "2026-10-09T21:00:00Z",
            },
            "before the cutoff day had closed",
        ),
    ],
)
def test_verify_rechecks_fetch_time_presence_and_order(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    run = _held_run(tmp_path, now=NOW)
    _patch_header(run, **changes)
    with pytest.raises(driver.DriverError, match=message):
        driver.verify_run(run)


def test_official_manifest_is_bound_to_files_and_times(tmp_path: Path) -> None:
    run = _held_run(tmp_path, now=NOW)
    sidecar = run / "official-closes/official-closes-manifest.json"
    document = json.loads(sidecar.read_text())
    document["files"][0]["sha256"] = "0" * 64
    sidecar.write_bytes(driver.pretty(document))
    _rewrite_manifest(run, "official-closes/official-closes-manifest.json")
    with pytest.raises(
        driver.DriverError, match="official-closes-manifest.json differs"
    ):
        driver.verify_run(run)
    run2 = _held_run(tmp_path, run_id="time-run-2", now=NOW)
    _patch_header(
        run2,
        fetch_started_utc="2026-10-12T15:00:01Z",
        fetch_completed_utc="2026-10-12T15:00:02Z",
    )
    with pytest.raises(
        driver.DriverError, match="official-closes-manifest.json differs"
    ):
        driver.verify_run(run2)


# ------------------------------------------------ R3 raw SPXL history (gate b)

SHIPPED_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "docs/providers/yahoo-fetch/r3-batch-a.config.json"
)


def _shipped_spxl_group() -> dict[str, Any]:
    groups = json.loads(SHIPPED_CONFIG.read_bytes())["groups"]
    (group,) = [item for item in groups if item["group_id"] == "spxl-raw-history"]
    return group


def test_the_shipped_config_names_a_raw_daily_spxl_history_group() -> None:
    group = _shipped_spxl_group()
    assert group["symbols"] == ["SPXL"] and group["interval"] == "1d"
    assert group["through"] == "cutoff"
    # raw only: the builders refuse a history_adjusted SPXL file
    assert group["datasets"] == ["history"]
    parsed = driver.parse_config(_config([group])).groups[0]
    assert parsed.datasets == ("history",)
    # the proof's SPXL file starts at the fund's inception (2008-11-05)
    assert parsed.start <= date(2008, 11, 5)


def test_the_raw_spxl_file_is_accepted_by_every_builder_reader(
    tmp_path: Path,
) -> None:
    import portfolio_implementation_reconciliation as reconciliation
    import portfolio_implementation_return_attribution as attribution
    import portfolio_strategy_history as history

    run = _fetch(tmp_path, _config([_shipped_spxl_group()]), run_id="spxl")
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"
    manifest = json.loads((run / "MANIFEST.json").read_text())
    spxl = [name for name in manifest["files"] if "spxl-raw-history" in name]
    assert spxl and not any("history_adjusted" in name for name in spxl)
    (artifact_name,) = [name for name in spxl if name.endswith("history.json")]
    artifact = run / artifact_name
    payload = json.loads(artifact.read_text())
    assert (payload["provider"], payload["transport"], payload["dataset"]) == (
        "yahoo",
        "yfinance",
        "history",
    )
    cutoff = date.fromisoformat(CUTOFF)
    prices = reconciliation._market_prices(artifact, accepted_as_of=cutoff)
    assert min(prices) <= date(2008, 11, 5) and max(prices) == cutoff
    assert len(prices) > 4400
    rows = attribution._market_rows(artifact, accepted_as_of=cutoff)
    assert max(rows["SPXL"]) == cutoff
    extension, _ = history._yahoo_price_extension(artifact, "spxl-raw-history")
    assert max(extension["SPXL"]) == cutoff


def test_the_whole_shipped_config_parses_with_the_new_group(tmp_path: Path) -> None:
    """What `config-check` runs on the Saturday: sixteen groups, nothing counts fifteen."""

    manifest = _tt_manifest(tmp_path, ["BNY", "DUK", "IBIT", "PEP", "SHY"], ["AAPL"])
    positions = _positions(tmp_path, ["SPY", "AAPL"])
    raw = SHIPPED_CONFIG.read_bytes()
    config = driver.parse_config(
        raw, tt_source_manifest=manifest, schwab_positions=positions
    )
    ids = [group.group_id for group in config.groups]
    assert len(ids) == len(set(ids)) == len(json.loads(raw)["groups"]) == 16
    assert "spxl-raw-history" in ids and "qsi-daily" in ids


def test_a_command_that_reads_live_data_or_writes_the_fetch_config_needs_a_cutoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_bytes(_config())
    gov = tmp_path / "g.json"
    gov.write_text("{}")
    base = ["--config", str(cfg), "--stage-root", str(tmp_path / "s"), "--run-id", "r"]
    for argv in (
        ["fetch", *base, "--governance", str(gov)],
        ["probe", *base],
        [
            "apply-admissions",
            "--config",
            str(cfg),
            "--probe-run",
            str(tmp_path),
            "--out",
            str(tmp_path / "o"),
        ],
    ):
        assert driver.main(argv) == 1
        assert "needs --cutoff" in capsys.readouterr().err
    # an offline probe, config-check and verify do not need one, and config-check says where it came from
    assert driver.main(["config-check", "--config", str(cfg)]) == 0
    assert json.loads(capsys.readouterr().out)["cutoff_source"] == "config_default"
    assert driver.main(["config-check", "--config", str(cfg), "--cutoff", CUTOFF]) == 0
    assert json.loads(capsys.readouterr().out)["cutoff_source"] == "cli"


def test_a_fetch_before_the_cutoff_day_has_closed_is_refused_before_any_evidence_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    cfg = tmp_path / "c.json"
    cfg.write_bytes(_config())
    gov = tmp_path / "g.json"
    gov.write_text("{}")

    def spent(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("the evidence was consumed")

    monkeypatch.setattr(driver, "authorize_live", spent)
    code = driver.main(
        [
            "fetch",
            "--config",
            str(cfg),
            "--stage-root",
            str(tmp_path / "s"),
            "--run-id",
            "r",
            "--governance",
            str(gov),
            "--cutoff",
            today,
        ]
    )
    assert code == 1
    assert "not a closed UTC day" in capsys.readouterr().err


def test_the_closed_day_rule_is_the_utc_date_of_the_start_timestamp() -> None:
    config = driver.parse_config(_config())  # through = the cutoff, a Friday
    through = config.cutoff
    # 23:59:59 UTC on the cutoff day is still open (Friday 18:59 ET after the November clock change)
    open_ = datetime(
        through.year, through.month, through.day, 23, 59, 59, tzinfo=timezone.utc
    )
    closed = open_ + timedelta(seconds=1)
    with pytest.raises(driver.DriverError, match="not a closed UTC day"):
        driver.require_closed_days(config, open_)
    driver.require_closed_days(config, closed)
    # the clock is compared in UTC whatever zone it is expressed in
    eastern = closed.astimezone(ZoneInfo("America/New_York"))
    driver.require_closed_days(config, eastern)


def test_a_config_the_driver_resolved_can_be_read_back_by_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply-admissions writes the resolved config; fetch and config-check must accept it."""

    manifest = _tt_manifest(tmp_path, ["AAA", "BRK.B"], ["BNY", "DUK"])
    source = driver.parse_config(_derived_config(), tt_source_manifest=manifest)
    resolved = tmp_path / "r3-config.json"
    resolved.write_bytes(source.effective_bytes)
    assert (
        driver.main(["config-check", "--config", str(resolved), "--cutoff", CUTOFF])
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["config_sha256"] == source.sha256
    assert printed["cutoff"] == CUTOFF
    # without the cutoff the CLI still reads it, and a different cutoff is refused loudly
    assert driver.main(["config-check", "--config", str(resolved)]) == 0
    capsys.readouterr()
    assert (
        driver.main(
            ["config-check", "--config", str(resolved), "--cutoff", "2026-09-17"]
        )
        == 1
    )
    assert "derived for cutoff" in capsys.readouterr().err
    # the unresolved source config is not accepted as resolved
    with pytest.raises(
        driver.DriverError, match="derivations are written by the driver"
    ):
        driver.parse_config(source.effective_bytes, tt_source_manifest=manifest)


# ------------------------------------------------ held closes: D and the previous trading day


def _row(close: str) -> list[str]:
    return ["1", "1", "1", close, close, "0", "0"]


def test_official_coverage_needs_every_symbol_on_the_cutoff_and_the_previous_trading_day() -> (
    None
):
    cutoff = date(2026, 10, 9)
    full = {"2026-10-07": _row("7"), "2026-10-08": _row("8"), "2026-10-09": _row("9")}
    # a market holiday on the 8th: no symbol has a bar, the previous trading day is the 7th
    holiday = {"2026-10-07": _row("7"), "2026-10-09": _row("9")}
    coverage, failures = driver.official_coverage(
        {"AAA": holiday, "BBB": holiday}, cutoff
    )
    assert failures == []
    assert coverage["names"]["AAA"]["previous_bar"] == "2026-10-07"
    assert coverage["names"]["AAA"]["previous_close"] == "7"
    # one symbol lacks the 8th that another has: it is named, with the day
    _, failures = driver.official_coverage({"AAA": full, "BBB": holiday}, cutoff)
    assert failures == [
        "official close for BBB has no bar on the previous trading day 2026-10-08 before 2026-10-09"
    ]
    # a symbol with only the cutoff bar fails on the previous day, and one without the cutoff bar fails too
    only_cutoff = {"2026-10-09": _row("9")}
    _, failures = driver.official_coverage({"AAA": full, "CCC": only_cutoff}, cutoff)
    assert any(
        "CCC has no bar on the previous trading day" in item for item in failures
    )
    _, failures = driver.official_coverage({"AAA": {"2026-10-08": _row("8")}}, cutoff)
    assert any("has no bar on 2026-10-09" in item for item in failures)


def test_a_held_symbol_without_the_previous_trading_day_rejects_the_run(
    tmp_path: Path,
) -> None:
    positions = _positions(tmp_path, ["SPY", "NEW"])
    config = driver.parse_config(_held_config(), schwab_positions=positions)
    with pytest.raises(driver.DriverError, match="rejected"):
        driver.run_fetch(
            config,
            stage_root=tmp_path / "s",
            run_id="no-previous",
            yf=FakeYF(start_after={"NEW": date.fromisoformat(CUTOFF)}),
            chart=FakeChart(),
            now=NOW,
        )
    rejected = (tmp_path / "s" / "no-previous.REJECTED.json").read_text()
    assert "NEW has no bar on the previous trading day" in rejected


def test_the_official_manifest_carries_the_previous_trading_day_close(
    tmp_path: Path,
) -> None:
    positions = _positions(tmp_path, ["SPY"])
    config = driver.parse_config(_held_config(), schwab_positions=positions)
    run = driver.run_fetch(
        config,
        stage_root=tmp_path / "s",
        run_id="held-previous",
        yf=FakeYF(),
        chart=FakeChart(),
        now=NOW,
    )
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"
    sidecar = json.loads(
        (run / "official-closes/official-closes-manifest.json").read_text()
    )
    (entry,) = sidecar["files"]
    rows = [
        r.split(",")
        for r in (run / "official-closes/SPY_yahoo_daily.csv")
        .read_text()
        .splitlines()[1:]
    ]
    assert entry["cutoff_bar"] == rows[-1][0] == CUTOFF
    assert entry["previous_bar"] == rows[-2][0]
    assert entry["previous_close"] == rows[-2][4]
    # the file the derive step reads is date..close per symbol, listed with its sha256
    assert entry["sha256"] == driver.sha256_hex(
        (run / "official-closes/SPY_yahoo_daily.csv").read_bytes()
    )


def _held_config_excluding(*excluded: str) -> bytes:
    group = _group("held-closes", symbols_from="schwab-positions", datasets=["history"])
    del group["symbols"], group["start"]
    group.update(
        start_offset_days_before_through=10,
        min_rows=5,
        official_closes={},
        exclude_symbols=list(excluded),
    )
    return _config([group])


def test_a_cash_equivalent_holding_can_be_left_out_of_the_held_closes(
    tmp_path: Path,
) -> None:
    positions = _positions(tmp_path, ["SPY", "SWVXX", "BRK/B"])
    config = driver.parse_config(
        _held_config_excluding("SWVXX"), schwab_positions=positions
    )
    group = config.groups[0]
    assert group.symbols == ("BRK-B", "SPY")
    assert group.official_names == {"BRK-B": "BRK.B", "SPY": "SPY"}
    (derivation,) = json.loads(config.effective_bytes)["derivations"].values()
    assert derivation["excluded"] == ["SWVXX"] and "SWVXX" not in derivation["symbols"]
    # an excluded symbol that is not held is ignored (selling it never blocks a fetch)
    other = driver.parse_config(
        _held_config_excluding("SWVXX"), schwab_positions=_positions(tmp_path, ["SPY"])
    )
    assert other.groups[0].symbols == ("SPY",)
    # the exclusion is part of the effective config, so it is part of the evidence hash
    assert (
        config.sha256
        != driver.parse_config(_held_config(), schwab_positions=positions).sha256
    )
    with pytest.raises(driver.DriverError, match="every held symbol is excluded"):
        driver.parse_config(
            _held_config_excluding("SPY"),
            schwab_positions=_positions(tmp_path, ["SPY"]),
        )


def test_exclude_symbols_is_for_the_schwab_derived_group_only(tmp_path: Path) -> None:
    manifest = _tt_manifest(tmp_path, ["AAA"], ["BNY"])
    group = _group(
        "tt-daily", symbols_from="tt-source-manifest", exclude_symbols=["AAA"]
    )
    del group["symbols"], group["start"]
    group.update(symbols_must_include=["BNY"], start_offset_days_before_inception=7)
    with pytest.raises(driver.DriverError, match="schwab-positions only"):
        driver.parse_config(_config([group]), tt_source_manifest=manifest)


def test_the_shipped_held_closes_group_excludes_the_sweep_fund() -> None:
    groups = json.loads(SHIPPED_CONFIG.read_bytes())["groups"]
    (held,) = [g for g in groups if g["group_id"] == "held-closes"]
    assert held["exclude_symbols"] == ["SWVXX"] and held["through"] == "cutoff"


def test_held_check_names_the_held_symbols_no_retained_pull_served(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    positions = _positions(tmp_path, ["SPY", "NEWCO", "BRK/B"])
    served = tmp_path / "served"
    served.mkdir()
    (served / "SPY_yahoo_daily.csv").write_text("date,close\n")
    (served / "BRK.B_yahoo_daily.csv").write_text("date,close\n")
    (served / "OLD_yahoo_daily.csv").write_text(
        "date,close\n"
    )  # served once, no longer held
    argv = ["held-check", "--positions", str(positions), "--served-dir", str(served)]
    assert driver.main(argv) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["never_served"] == ["NEWCO"]
    assert {row["symbol"]: row["served_before"] for row in report["held"]} == {
        "BRK.B": True,
        "NEWCO": False,
        "SPY": True,
    }
    assert "NEWCO" in captured.err and "before any evidence is minted" in captured.err
    # an explicit allowance reports without refusing; nothing unseen exits clean
    assert driver.main([*argv, "--allow-unseen"]) == 0
    capsys.readouterr()
    (served / "NEWCO_yahoo_daily.csv").write_text("date,close\n")
    assert driver.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["never_served"] == []


def test_held_check_lists_what_the_ingest_aliases_or_values_as_cash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    positions = _positions(tmp_path, ["SPY", "BRK/B", "MMKT"])
    served = tmp_path / "served"
    served.mkdir()
    for name in ("SPY", "BRK.B", "MMKT"):
        (served / f"{name}_yahoo_daily.csv").write_text("date,close\n")
    config = tmp_path / "ingest.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "price_ratio_tolerance": "0.1",
                "symbol_aliases": {"BRK/B": "BRK-B", "OLD": "NEW"},
                "cash_equivalent_symbols": ["MMKT", "GONE"],
                "price_ratio_exceptions": {},
            }
        )
    )
    argv = ["held-check", "--positions", str(positions), "--served-dir", str(served)]
    assert driver.main([*argv, "--ingest-config", str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    # only the symbols actually held are named, with the spelling the ingest would look for
    assert report["aliased_by_ingest"] == {"BRK.B": "BRK-B"}
    assert report["cash_equivalent_by_ingest"] == ["MMKT"]
    # without the config the report has neither key; a broken config is refused
    assert driver.main(argv) == 0
    assert "aliased_by_ingest" not in json.loads(capsys.readouterr().out)
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert driver.main([*argv, "--ingest-config", str(bad)]) == 1
    assert "ingest config" in capsys.readouterr().err


def test_make_evidence_names_the_effective_config_and_is_accepted_by_the_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_bytes(_config())
    out = tmp_path / "evidence.json"
    review = "https://github.com/dachent/quant_engine/issues/1#issuecomment-1"
    argv = [
        "make-evidence",
        "--config",
        str(cfg),
        "--cutoff",
        CUTOFF,
        "--run-id",
        "r3-yahoo",
        "--review",
        review,
        "--valid-from-utc",
        "2026-10-10T00:00:00Z",
        "--valid-until-utc",
        "2026-10-10T23:00:00Z",
        "--max-requests",
        "250",
        "--out",
        str(out),
    ]
    assert driver.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    config = driver.parse_config(cfg.read_bytes(), cutoff_override=CUTOFF)
    assert printed["config_sha256"] == config.sha256 and printed["cutoff"] == CUTOFF
    budget = driver.authorize_live(
        out.read_bytes(),
        config=config,
        run_id="r3-yahoo",
        stage_root=tmp_path / "stage",
        now=datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc),
    )
    assert budget == 250
    # create-only, a window over 24 h or without an offset is refused, and so is another review
    assert driver.main(argv) == 1
    capsys.readouterr()
    long_window = [*argv[:-2], "--out", str(tmp_path / "long.json")]
    long_window[long_window.index("--valid-until-utc") + 1] = "2026-10-11T00:00:01Z"
    assert driver.main(long_window) == 1
    assert "at most 24 hours" in capsys.readouterr().err
    naive = [*argv[:-2], "--out", str(tmp_path / "naive.json")]
    naive[naive.index("--valid-from-utc") + 1] = "2026-10-10T00:00:00"
    assert driver.main(naive) == 1
    capsys.readouterr()
    bad_review = [*argv[:-2], "--out", str(tmp_path / "review.json")]
    bad_review[bad_review.index("--review") + 1] = "https://example.com/x"
    assert driver.main(bad_review) == 1
    assert (
        not (tmp_path / "long.json").exists()
        and not (tmp_path / "review.json").exists()
    )
    # evidence made for another cutoff does not authorize this config
    other = driver.parse_config(cfg.read_bytes(), cutoff_override="2026-10-08")
    with pytest.raises(driver.GovernanceError):
        driver.authorize_live(
            out.read_bytes(),
            config=other,
            run_id="r3-yahoo",
            stage_root=tmp_path / "stage2",
            now=datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc),
        )


def _closes_file(directory: Path, symbol: str, rows: dict[str, float]) -> None:
    directory.mkdir(exist_ok=True)
    lines = ["date,open,high,low,close,adjclose,volume,dividend"]
    lines += [f"{day},0,0,0,{close},{close},1,0" for day, close in rows.items()]
    (directory / f"{symbol}_yahoo_daily.csv").write_text("\n".join(lines) + "\n")


def test_rescale_check_catches_a_split_after_the_retained_pull(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = tmp_path / "previous"
    new = tmp_path / "new"
    days = {"2026-09-16": 100.0, "2026-09-17": 101.0}
    _closes_file(previous, "SAME", days)
    _closes_file(previous, "SPLIT", days)
    _closes_file(previous, "GONE", {"2026-08-03": 5.0})
    _closes_file(
        new, "SAME", {**days, "2026-11-06": 120.0}
    )  # dividends and moves after the retained pull change nothing
    _closes_file(
        new, "SPLIT", {"2026-09-16": 50.0, "2026-09-17": 50.5, "2026-11-06": 55.0}
    )
    _closes_file(new, "GONE", {"2026-11-06": 6.0})
    _closes_file(new, "NEWCO", {"2026-11-06": 9.0})
    argv = ["rescale-check", "--closes-dir", str(new), "--previous-dir", str(previous)]
    assert driver.main(argv) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["rescaled"] == ["SPLIT"]
    assert report["no_overlap"] == ["GONE"]
    assert report["not_in_previous"] == ["NEWCO"]
    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert by_symbol["SAME"]["status"] == "ok"
    assert by_symbol["SPLIT"]["ratio_new_over_previous"] == 0.5
    assert "SPLIT" in captured.err and "GONE" in captured.err
    # the clean case exits 0; a symbol no retained pull holds is warned about, and refused
    # only when the operator requires every symbol to have a previous pull
    (new / "SPLIT_yahoo_daily.csv").unlink()
    (new / "GONE_yahoo_daily.csv").unlink()
    assert driver.main(argv) == 0
    assert "NEWCO" in capsys.readouterr().err
    assert driver.main([*argv, "--require-previous"]) == 1
    capsys.readouterr()


def test_rescale_check_refuses_malformed_input(tmp_path: Path) -> None:
    new = tmp_path / "new"
    new.mkdir()
    previous = tmp_path / "previous"
    previous.mkdir()
    with pytest.raises(driver.DriverError, match="holds no official-closes file"):
        driver.rescale_check(new, [previous])
    (new / "BAD_yahoo_daily.csv").write_text("nope\n")
    with pytest.raises(driver.DriverError, match="not an official-closes file"):
        driver.rescale_check(new, [previous])
    with pytest.raises(driver.DriverError, match="tolerance"):
        driver.rescale_check(new, [previous], 0.0)
    with pytest.raises(driver.DriverError, match="not a directory"):
        driver.rescale_check(new, [tmp_path / "missing"])


def test_rescale_check_uses_the_last_day_of_a_retained_adjusted_history(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "adjusted.json"
    rows = [
        # dividend-adjusted earlier days differ from the raw close; the last day equals it
        {"Ticker": "SPXL", "Date": "2026-09-16T00:00:00", "Close": 99.0},
        {"Ticker": "SPXL", "Date": "2026-09-17T00:00:00", "Close": 101.0},
        {"Ticker": "OTHER", "Date": "2026-09-17T00:00:00", "Close": 10.0},
    ]
    artifact.write_text(json.dumps({"rows": rows}))
    new = tmp_path / "new"
    _closes_file(new, "SPXL", {"2026-09-16": 100.0, "2026-09-17": 101.0})
    _closes_file(new, "OTHER", {"2026-09-17": 5.0})
    previous = tmp_path / "previous"
    previous.mkdir()
    report = driver.rescale_check(new, [previous], adjusted_json=[artifact])
    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert by_symbol["SPXL"]["status"] == "ok"
    assert by_symbol["SPXL"]["basis"] == "adjusted_last_day"
    assert by_symbol["SPXL"]["days_compared"] == 1
    assert report["rescaled"] == ["OTHER"]
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(driver.DriverError, match="not a history artifact"):
        driver.rescale_check(new, [previous], adjusted_json=[bad])


def test_rescale_check_compares_every_retained_vintage_not_the_last_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    oldest, middle, latest = (
        tmp_path / "oldest",
        tmp_path / "middle",
        tmp_path / "latest",
    )
    new = tmp_path / "new"
    days = {"2026-09-16": 100.0, "2026-09-17": 101.0}
    half = {day: close / 2 for day, close in days.items()}
    # a split after the oldest pull: the later pulls already hold split-adjusted closes, so
    # the new pull (also split-adjusted) matches them and only the oldest pull shows it
    _closes_file(oldest, "SPLIT", days)
    _closes_file(middle, "SPLIT", half)
    _closes_file(latest, "SPLIT", half)
    _closes_file(new, "SPLIT", half)
    _closes_file(oldest, "CLEAN", days)
    _closes_file(middle, "CLEAN", days)
    _closes_file(latest, "CLEAN", days)
    _closes_file(new, "CLEAN", days)
    argv = ["rescale-check", "--closes-dir", str(new)]
    for directory in (oldest, middle, latest):
        argv += ["--previous-dir", str(directory)]
    assert driver.main(argv) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["rescaled"] == ["SPLIT"]
    assert report["retained_vintages_disagree"] == ["SPLIT"]
    assert len(report["vintages_compared"]) == 6
    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    split = by_symbol["SPLIT"]
    assert split["status"] == "RESCALED"
    assert split["rescaled_sources"] == [str(oldest / "SPLIT_yahoo_daily.csv")]
    assert [item["status"] for item in split["vintages"]] == ["RESCALED", "ok", "ok"]
    assert by_symbol["CLEAN"]["status"] == "ok"
    assert "RETAINED_VINTAGES_DISAGREE" in captured.err
    # argument order does not matter
    argv_reversed = ["rescale-check", "--closes-dir", str(new)]
    for directory in (latest, middle, oldest):
        argv_reversed += ["--previous-dir", str(directory)]
    assert driver.main(argv_reversed) == 1
    capsys.readouterr()


def test_rescale_check_flags_disagreeing_retained_vintages_even_if_new_matches_one(
    tmp_path: Path,
) -> None:
    older, newer, new = tmp_path / "older", tmp_path / "newer", tmp_path / "new"
    _closes_file(older, "AB", {"2026-09-17": 10.0})
    _closes_file(newer, "AB", {"2026-09-17": 10.0, "2026-10-01": 11.0})
    _closes_file(new, "AB", {"2026-09-17": 10.0, "2026-10-01": 11.0})
    adjusted = tmp_path / "adjusted.json"
    adjusted.write_text(
        json.dumps(
            {"rows": [{"Ticker": "AB", "Date": "2026-09-17T00:00:00", "Close": 5.0}]}
        )
    )
    report = driver.rescale_check(new, [older, newer], adjusted_json=[adjusted])
    row = report["symbols"][0]
    assert report["retained_vintages_disagree"] == ["AB"]
    assert report["rescaled"] == ["AB"]
    assert row["status"] == "RESCALED"
    assert len(row["retained_vintages_disagree"]) == 2
    # without the disagreeing artifact the same pulls are clean
    clean = driver.rescale_check(new, [older, newer])
    assert clean["rescaled"] == [] and clean["retained_vintages_disagree"] == []
    # two retained pulls that disagree with each other, new matching neither, both reported
    _closes_file(new, "AB", {"2026-09-17": 20.0})
    both = driver.rescale_check(new, [older, newer])
    assert both["rescaled"] == ["AB"]
    assert both["symbols"][0]["rescaled_sources"] == [
        str(older / "AB_yahoo_daily.csv"),
        str(newer / "AB_yahoo_daily.csv"),
    ]


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-3"])
def test_rescale_check_refuses_non_finite_and_non_positive_closes(
    tmp_path: Path, bad: str
) -> None:
    new, previous = tmp_path / "new", tmp_path / "previous"
    _closes_file(new, "AB", {"2026-09-17": 10.0})
    _closes_file(previous, "AB", {"2026-09-17": 10.0})
    new_file = new / "AB_yahoo_daily.csv"
    retained_file = previous / "AB_yahoo_daily.csv"
    good_new, good_retained = new_file.read_bytes(), retained_file.read_bytes()
    new_file.write_bytes(good_new.replace(b",10.0,10.0,", f",{bad},{bad},".encode()))
    with pytest.raises(
        driver.DriverError, match=r"AB_yahoo_daily\.csv 2026-09-17: close"
    ):
        driver.rescale_check(new, [previous])
    new_file.write_bytes(good_new)
    retained_file.write_bytes(
        good_retained.replace(b",10.0,10.0,", f",{bad},{bad},".encode())
    )
    with pytest.raises(
        driver.DriverError, match=r"AB_yahoo_daily\.csv 2026-09-17: close"
    ):
        driver.rescale_check(new, [previous])
    retained_file.write_bytes(good_retained)
    artifact = tmp_path / "adjusted.json"
    literal = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}.get(bad, bad)
    artifact.write_bytes(
        b'{"rows": [{"Ticker": "AB", "Date": "2026-09-17T00:00:00", "Close": '
        + literal.encode()
        + b"}]}"
    )
    with pytest.raises(driver.DriverError, match=r"adjusted\.json 2026-09-17: close"):
        driver.rescale_check(new, [previous], adjusted_json=[artifact])


# ------------------------------------------------ native driver behaviour (R3 live sitting)


class HolidayYF(FakeYF):
    """FakeYF that, like Yahoo, has no bar on the given market closures."""

    def __init__(self, closed: set[date], **kw: Any) -> None:
        super().__init__(**kw)
        self.closed = closed

    def download(self, **kwargs: Any) -> pd.DataFrame:
        frame = super().download(**kwargs)
        if frame.empty:
            return frame
        return frame[[stamp.date() not in self.closed for stamp in frame.index]]


# CUTOFF 2026-10-09; START 2026-09-01; Labor Day is Monday 2026-09-07.
LABOR_DAY = date(2026, 9, 7)


def _nyse_group(**overrides: Any) -> dict[str, Any]:
    return _group("vol", calendar="nyse", max_missing_weekdays=0, **overrides)


def test_nyse_calendar_accepts_exactly_the_market_closures(tmp_path: Path) -> None:
    run = _fetch(
        tmp_path,
        _config([_nyse_group()]),
        yf=HolidayYF({LABOR_DAY}),
    )
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["status"] == "PASS" and entry["missing_weekday_count"] == 0
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


def test_the_weekday_calendar_needs_a_fixed_allowance_for_the_same_data(
    tmp_path: Path,
) -> None:
    config = _config([_group("vol", max_missing_weekdays=0)])
    with pytest.raises(driver.DriverError, match="fetch rejected"):
        _fetch(tmp_path, config, yf=HolidayYF({LABOR_DAY}))


def test_a_missing_day_that_is_not_a_closure_is_rejected_under_nyse(
    tmp_path: Path,
) -> None:
    with pytest.raises(driver.DriverError, match="fetch rejected"):
        _fetch(
            tmp_path,
            _config([_nyse_group()]),
            yf=HolidayYF({LABOR_DAY, date(2026, 9, 16)}),
        )
    reject = json.loads((tmp_path / "stage" / "run-1.REJECTED.json").read_text())
    assert any("missing sessions exceed the allowed 0" in m for m in reject["failures"])


def test_an_ad_hoc_closure_is_declared_per_group(tmp_path: Path) -> None:
    closed = {LABOR_DAY, date(2026, 9, 16)}
    run = _fetch(
        tmp_path,
        _config([_nyse_group(closed_days=["2026-09-16"])]),
        yf=HolidayYF(closed),
    )
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"calendar": "weekday", "closed_days": ["2026-09-16"]},  # only with nyse
        {"closed_days": ["2026-09-19"]},  # a Saturday
        {"closed_days": ["2026-09-16", "2026-09-16"]},  # duplicate
        {"closed_days": ["2026-08-31"]},  # outside the window
    ],
)
def test_closed_days_are_validated(overrides: dict[str, Any]) -> None:
    group = _nyse_group()
    group.update(overrides)
    with pytest.raises(driver.DriverError, match="closed_days"):
        driver.parse_config(_config([group]))


def _fx_chart(
    stamps_local: list[tuple[datetime, float]], symbol: str = "EURUSD=X"
) -> bytes:
    zone = ZoneInfo("Europe/London")
    body = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": symbol,
                        "instrumentType": "CURRENCY",
                        "dataGranularity": "1d",
                        "exchangeTimezoneName": "Europe/London",
                    },
                    "timestamp": [
                        int(d.replace(tzinfo=zone).timestamp()) for d, _ in stamps_local
                    ],
                    "indicators": {"quote": [{"close": [c for _, c in stamps_local]}]},
                }
            ]
        }
    }
    return json.dumps(body).encode()


def test_parse_chart_ignores_only_a_live_bar_after_the_cutoff() -> None:
    cutoff = date(2026, 10, 9)
    midnight = [
        (datetime(2026, 10, 8), 1.1),
        (datetime(2026, 10, 9), 1.2),
        (datetime(2026, 10, 12), 1.3),  # a midnight bar after the cutoff is kept here
    ]
    live = (datetime(2026, 10, 12, 15, 30), 1.4)
    raw = _fx_chart([*midnight, live])
    bars, info = driver.parse_chart(raw, "EURUSD=X", ignore_live_after=cutoff)
    assert [d for d, _ in bars] == [
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 12),
    ]
    assert info["live_bars_ignored"] == 1
    # without the cutoff, or for a live bar on/before it, the bar still refuses
    with pytest.raises(driver.DriverError, match="not at local midnight"):
        driver.parse_chart(raw, "EURUSD=X")
    early = _fx_chart([midnight[0], (datetime(2026, 10, 9, 15, 30), 1.4)])
    with pytest.raises(driver.DriverError, match="not at local midnight"):
        driver.parse_chart(early, "EURUSD=X", ignore_live_after=cutoff)

    # Only the single TERMINAL appended bar is ignored: two off-midnight bars after the
    # cutoff, or an off-midnight bar followed by the real last bar, fail closed.
    two = _fx_chart([*midnight, live, (datetime(2026, 10, 12, 16, 30), 1.5)])
    with pytest.raises(driver.DriverError, match="not at local midnight"):
        driver.parse_chart(two, "EURUSD=X", ignore_live_after=cutoff)
    mid_then_bar = _fx_chart([midnight[0], live, (datetime(2026, 10, 13), 1.5)])
    with pytest.raises(driver.DriverError, match="not at local midnight"):
        driver.parse_chart(mid_then_bar, "EURUSD=X", ignore_live_after=cutoff)


class LiveBarChart(FakeChart):
    """FakeChart that, like Yahoo while FX is open, appends a bar stamped with the clock."""

    def __init__(self, live_day: date, **kw: Any) -> None:
        super().__init__(**kw)
        self.live_day = live_day

    def chart(self, symbol: str, period1: int, period2: int) -> bytes:
        raw = super().chart(symbol, period1, period2)
        document = json.loads(raw)
        body = document["chart"]["result"][0]
        zone = ZoneInfo("Europe/London")
        live = datetime(
            self.live_day.year,
            self.live_day.month,
            self.live_day.day,
            15,
            30,
            tzinfo=zone,
        )
        body["timestamp"].append(int(live.timestamp()))
        body["indicators"]["quote"][0]["close"].append(1.5)
        return json.dumps(document).encode()


def _chart_config() -> bytes:
    return _config(
        [_group("fx", kind="chart", symbols=["EURUSD=X"], max_missing_weekdays=3)]
    )


def test_a_live_fx_bar_after_the_cutoff_is_staged_raw_and_dropped_from_the_derived_file(
    tmp_path: Path,
) -> None:
    chart = LiveBarChart(date(2026, 10, 12))
    run = _fetch(tmp_path, _chart_config(), chart=chart)
    raw = json.loads((run / "raw/fx/EURUSD_X.chart.json").read_text())
    derived = json.loads((run / "derived/fx/EURUSD_X.chart.json").read_text())
    raw_stamps = raw["chart"]["result"][0]["timestamp"]
    derived_stamps = derived["chart"]["result"][0]["timestamp"]
    assert (
        len(raw_stamps) == len(derived_stamps) + 1
    )  # the live bar, raw bytes unmodified
    assert raw["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1] == 1.5
    entry = json.loads((run / "coverage.json").read_text())["groups"][0]["symbols"][0]
    assert entry["status"] == "PASS"
    assert entry["raw_bars_after_through_dropped"] == 1
    assert driver.verify_run(run, now=NOW)["status"] == "VERIFIED"


def test_a_live_fx_bar_on_or_before_the_cutoff_still_rejects(tmp_path: Path) -> None:
    with pytest.raises(driver.DriverError, match="fetch rejected"):
        _fetch(tmp_path, _chart_config(), chart=LiveBarChart(date(2026, 10, 9)))


_SINGLETON: dict[str, Any] = {}


class SingletonYF(FakeYF):
    """FakeYF whose cache behaves like yfinance's: one sqlite file, opened at the first
    location it is given and kept for the whole process. A later group fails with
    ``unable to open database file`` when that directory no longer exists."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.locations: list[str] = []
        outer = self

        class Cache:
            def set_cache_location(self, value: str) -> None:
                outer.locations.append(value)
                if "db" not in _SINGLETON:
                    path = Path(value) / "tz.db"
                    _SINGLETON["db"] = sqlite3.connect(str(path))
                    _SINGLETON["path"] = path

        self.cache = Cache()  # type: ignore[assignment]

    def download(self, **kwargs: Any) -> pd.DataFrame:
        if not Path(_SINGLETON["path"]).exists():
            raise ValueError("unable to open database file")
        _SINGLETON["db"].execute("select 1")
        return super().download(**kwargs)


def test_every_group_of_a_run_opens_the_same_persistent_cache(tmp_path: Path) -> None:
    groups = [
        _group("one", symbols=["AAA"]),
        _group("two", symbols=["BBB"]),
        _group("three", symbols=["CCC"]),
    ]
    yf = SingletonYF()
    try:
        outcome = driver.dry_run(
            driver.parse_config(_config(groups)),
            yf=yf,
            chart=FakeChart(),
            now=NOW,
            stage_root=tmp_path / "scratch",
        )
        assert outcome["status"] == "VERIFIED", outcome
        assert len(yf.locations) >= 3
        assert len(set(yf.locations)) == 1  # one directory, not one per group
        assert Path(yf.locations[0]).is_dir()  # still there for the next group or run
        # a second run in the same process reuses it and still reads fresh data
        again = driver.dry_run(
            driver.parse_config(_config(groups)),
            yf=yf,
            chart=FakeChart(),
            now=NOW,
            stage_root=tmp_path / "scratch",
            run_id="DRY-second",
        )
        assert again["status"] == "VERIFIED", again
        assert len(set(yf.locations)) == 1
    finally:
        if "db" in _SINGLETON:
            _SINGLETON.pop("db").close()
            _SINGLETON.clear()


def test_a_group_never_reads_another_groups_cached_result(tmp_path: Path) -> None:
    yf = FakeYF()
    _fetch(tmp_path, _config([_group("one")]), yf=yf, run_id="run-a")
    _fetch(tmp_path, _config([_group("one")]), yf=yf, run_id="run-b")
    assert len(yf.calls) == 2  # the second run fetched again, no result-cache hit


def test_dry_run_rehearses_without_governance_and_verifies(tmp_path: Path) -> None:
    config = driver.parse_config(
        _config([_group("one"), _group("two", symbols=["CCC"])])
    )
    scratch = tmp_path / "scratch"
    outcome = driver.dry_run(
        config, yf=FakeYF(), chart=FakeChart(), now=NOW, stage_root=scratch
    )
    assert outcome["status"] == "VERIFIED"
    assert outcome["mode"] == "dry-run" and outcome["evidence_consumed"] is False
    assert outcome["run_id"].startswith("DRY-")
    assert not (scratch / ".governance-consumed").exists()
    assert {entry.name for entry in scratch.iterdir()} == {outcome["run_id"]}


def test_dry_run_reports_a_rejection_with_its_failures(tmp_path: Path) -> None:
    config = driver.parse_config(_config([_group("one")]))
    outcome = driver.dry_run(
        config,
        yf=FakeYF(empty={"AAA"}),
        chart=FakeChart(),
        now=NOW,
        stage_root=tmp_path / "scratch",
    )
    assert outcome["status"] == "REJECTED" and outcome["failures"]


def test_a_dry_run_never_uses_a_release_root_or_a_governed_id(tmp_path: Path) -> None:
    config = driver.parse_config(_config())
    for name in ("R3-YAHOO", ".governance-consumed", "MANIFEST.json"):
        governed = tmp_path / f"root-{name.strip('.')}"
        governed.mkdir()
        (governed / name).write_bytes(b"{}")
        with pytest.raises(driver.DriverError, match="scratch directory"):
            driver.dry_run(
                config, yf=FakeYF(), chart=FakeChart(), now=NOW, stage_root=governed
            )
    with pytest.raises(driver.DriverError, match="must start with DRY-"):
        driver.dry_run(
            config,
            yf=FakeYF(),
            chart=FakeChart(),
            now=NOW,
            stage_root=tmp_path / "ok",
            run_id="R3-YAHOO",
        )
    # and a governed command refuses a rehearsal id: no evidence can name or spend it
    with pytest.raises(driver.DriverError, match="DRY-"):
        driver.make_evidence(
            config,
            run_id="DRY-x1",
            review="https://github.com/dachent/quant_engine/issues/1#issuecomment-2",
            valid_from_utc=NOW,
            valid_until_utc=NOW + timedelta(hours=1),
            max_requests=10,
        )
    with pytest.raises(driver.GovernanceError, match="rehearsal"):
        driver.authorize_live(
            b"{}", config=config, run_id="DRY-x1", stage_root=tmp_path / "g", now=NOW
        )


def test_dry_run_command_needs_an_explicit_cutoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_bytes(_config())
    assert driver.main(["dry-run", "--config", str(config)]) == 1
    assert "dry-run needs --cutoff" in capsys.readouterr().err
    args = driver._parser().parse_args(
        ["dry-run", "--config", str(config), "--cutoff", CUTOFF]
    )
    assert (
        args.command == "dry-run"
        and args.stage_root is None
        and args.max_requests == 2000
    )


def test_the_shipped_volatility_groups_use_the_nyse_calendar() -> None:
    groups = {
        item["group_id"]: item
        for item in json.loads(SHIPPED_CONFIG.read_bytes())["groups"]
    }
    for name in ("vol-shv-uvxy-sh", "vol-qqq-gld-tqqq"):
        parsed = driver._parse_group(groups[name], date(2026, 11, 6), 0)
        assert parsed.calendar == "nyse" and parsed.max_missing_weekdays == 0
    # the 2025-01-09 national day of mourning is not in the regular calendar: declared
    shv = driver._parse_group(groups["vol-shv-uvxy-sh"], date(2026, 11, 6), 0)
    assert shv.closed_days == (date(2025, 1, 9),)
    sessions = driver.expected_dates(
        shv.start, date(2026, 11, 6), shv.calendar, shv.closed_days
    )
    assert date(2025, 1, 9) not in sessions and date(2026, 7, 3) not in sessions
    assert date(2026, 11, 6) in sessions
