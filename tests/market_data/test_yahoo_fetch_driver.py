"""Offline tests for the governed Yahoo fetch driver (synthetic transports only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
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
    assert files == ["BRK.B_yahoo_daily.csv", "SPY_yahoo_daily.csv"]
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
