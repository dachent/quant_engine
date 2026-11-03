from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_fx_rates.py"
SPEC = importlib.util.spec_from_file_location("portfolio_fx_rates", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load portfolio FX rates tool")
fx_rates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fx_rates
SPEC.loader.exec_module(fx_rates)


def _capture(path: Path, *, omit_last_usdt: bool = False) -> None:
    rows = [
        {
            "Ticker": ticker,
            "Date": f"2026-09-0{day}T00:00:00",
            "Open": 1.0,
            "High": 1.0,
            "Low": 1.0,
            "Close": close,
            "Adj Close": close,
            "Volume": 1,
            "Dividends": 0.0,
            "Stock Splits": 0.0,
        }
        for ticker, day, close in (
            ("USDC-USD", 1, 0.9998),
            ("USDC-USD", 2, 0.9999),
            ("USDT-USD", 1, 0.9997),
            ("USDT-USD", 2, 0.9996),
        )
        if not (omit_last_usdt and ticker == "USDT-USD" and day == 2)
    ]
    path.write_text(
        json.dumps(
            {
                "provider": "yahoo",
                "transport": "yfinance",
                "dataset": "history",
                "tickers": ["USDC-USD", "USDT-USD"],
                "columns": [
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
                ],
                "rows": rows,
                "start": "2026-09-01",
                "end": "2026-09-03",
                "interval": "1d",
                "prepost": False,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def test_stage_candidate_preserves_rates_and_never_publishes(tmp_path: Path) -> None:
    raw = tmp_path / "capture.json"
    output = tmp_path / "portfolio-fx-rates"
    _capture(raw)

    first = fx_rates.stage_candidate(
        raw,
        output,
        observed_start="2026-09-01",
        observed_end="2026-09-02",
    )
    second = fx_rates.stage_candidate(
        raw,
        output,
        observed_start="2026-09-01",
        observed_end="2026-09-02",
    )

    assert first == second
    assert first["published"] is False
    generation_root = output / "generations" / str(first["generation_id"])
    assert not (output / "CURRENT.json").exists()
    assert (generation_root / "raw" / "yfinance-history.json").read_bytes() == (
        raw.read_bytes()
    )
    assert (generation_root / "rates.csv").read_text(encoding="utf-8").splitlines() == [
        "date,currency,base_currency,rate,provider,ticker,field,timing",
        "2026-09-01,USDC,USD,0.9998,yahoo,USDC-USD,Close,UTC_DAILY_CLOSE",
        "2026-09-02,USDC,USD,0.9999,yahoo,USDC-USD,Close,UTC_DAILY_CLOSE",
        "2026-09-01,USDT,USD,0.9997,yahoo,USDT-USD,Close,UTC_DAILY_CLOSE",
        "2026-09-02,USDT,USD,0.9996,yahoo,USDT-USD,Close,UTC_DAILY_CLOSE",
    ]
    catalog = json.loads((generation_root / "catalog.json").read_text("utf-8"))
    assert catalog["generation_status"] == "CANDIDATE_PENDING_OWNER_SELECTION"
    assert catalog["coverage"]["rate_count"] == 4


def test_stage_candidate_rejects_missing_required_currency_date(tmp_path: Path) -> None:
    raw = tmp_path / "capture.json"
    _capture(raw, omit_last_usdt=True)

    with pytest.raises(
        fx_rates.PortfolioFXRatesError,
        match="required ticker/date coverage differs",
    ):
        fx_rates.stage_candidate(
            raw,
            tmp_path / "portfolio-fx-rates",
            observed_start="2026-09-01",
            observed_end="2026-09-02",
        )


def _chart(path: Path, *, symbol: str = "EURUSD=X") -> list[tuple[str, object]]:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    # Session dates; bars sit at local midnight, so summer bars are 23:00 UTC of
    # the previous calendar day.
    sessions = [
        ("2026-01-05", 1.1700),
        ("2026-01-06", 1.1710),
        ("2026-06-30", 1.1600),
        ("2026-07-01", None),
        ("2026-07-02", 1.1620),
    ]
    stamps = [
        int(datetime.fromisoformat(day).replace(tzinfo=london).timestamp())
        for day, _ in sessions
    ]
    path.write_text(
        json.dumps(
            {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "currency": "USD",
                                "symbol": symbol,
                                "exchangeTimezoneName": "Europe/London",
                                "dataGranularity": "1d",
                                "instrumentType": "CURRENCY",
                            },
                            "timestamp": stamps,
                            "indicators": {
                                "quote": [{"close": [close for _, close in sessions]}]
                            },
                        }
                    ],
                    "error": None,
                }
            }
        ),
        encoding="utf-8",
    )
    return sessions


def _policy(path: Path, **overrides: object) -> Path:
    policy: dict[str, object] = {
        "policy_id": "fx-fiat-lookup-v1",
        "session_date_convention": "LOCAL_DATE_IN_PROVIDER_DECLARED_EXCHANGE_TIMEZONE",
        "null_close": "OMIT_ROW_NEVER_FILL",
        "lookup_rule": "LAST_OBSERVED_CLOSE_ON_OR_BEFORE_DATE_WITHIN_MAX_AGE",
        "max_lookup_age_days": 3,
    }
    policy.update(overrides)
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_fiat_candidate_uses_local_session_dates_and_never_fills_null_closes(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "chart.json"
    sessions = _chart(raw)
    policy = _policy(tmp_path / "policy.json")
    csv_path = tmp_path / "chart.csv"
    # A CSV rendered from UTC dates shifts every summer bar back one day.
    csv_path.write_text(
        "date,close\n"
        "2026-01-05,1.17\n2026-01-06,1.171\n2026-06-29,1.16\n2026-06-30,\n2026-07-01,1.162\n",
        encoding="utf-8",
    )
    result = fx_rates.stage_fiat_candidate(
        raw,
        tmp_path / "out",
        policy,
        symbol="EURUSD=X",
        currency="EUR",
        observed_start="2026-01-01",
        observed_end="2026-07-02",
        chart_csv=csv_path,
    )
    assert result["published"] is False
    assert result["null_close_dates"] == ["2026-07-01"]
    assert result["rate_count"] == 4
    assert result["chart_csv_check"]["csv_date_differs_from_session_date"] == 3
    assert result["chart_csv_check"]["csv_close_differs_from_response"] == 0
    assert not (tmp_path / "out" / "CURRENT.json").exists()
    again = fx_rates.stage_fiat_candidate(
        raw,
        tmp_path / "out",
        policy,
        symbol="EURUSD=X",
        currency="EUR",
        observed_start="2026-01-01",
        observed_end="2026-07-02",
        chart_csv=csv_path,
    )
    assert again["generation_id"] == result["generation_id"]

    root = tmp_path / "out" / "generations" / str(result["generation_id"])
    rows = (root / "rates.csv").read_text(encoding="utf-8").splitlines()
    assert [line.split(",")[0] for line in rows[1:]] == [
        day for day, close in sessions if close is not None
    ]
    table = fx_rates.FiatRateTable(root)
    from datetime import date

    assert table.rate_on(date(2026, 6, 30))[1] == date(2026, 6, 30)
    # the omitted 2026-07-01 carries the last observed close within the policy age
    rate, observed = table.rate_on(date(2026, 7, 1))
    assert observed == date(2026, 6, 30) and str(rate) == "1.16"
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="older than"):
        table.rate_on(date(2026, 7, 20))
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="no fiat rate"):
        table.rate_on(date(2025, 12, 31))


def test_fiat_candidate_rejects_wrong_symbol_policy_and_tampered_artifacts(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "chart.json"
    _chart(raw)
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="symbol"):
        fx_rates.stage_fiat_candidate(
            raw,
            tmp_path / "out",
            _policy(tmp_path / "p.json"),
            symbol="GBPUSD=X",
            currency="GBP",
            observed_start="2026-01-01",
            observed_end="2026-07-02",
        )
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="rule is unsupported"):
        fx_rates.stage_fiat_candidate(
            raw,
            tmp_path / "out",
            _policy(tmp_path / "bad.json", null_close="ZERO_FILL"),
            symbol="EURUSD=X",
            currency="EUR",
            observed_start="2026-01-01",
            observed_end="2026-07-02",
        )
    result = fx_rates.stage_fiat_candidate(
        raw,
        tmp_path / "out",
        _policy(tmp_path / "ok.json"),
        symbol="EURUSD=X",
        currency="EUR",
        observed_start="2026-01-01",
        observed_end="2026-07-02",
    )
    root = tmp_path / "out" / "generations" / str(result["generation_id"])
    (root / "rates.csv").write_bytes((root / "rates.csv").read_bytes() + b"x")
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="differs"):
        fx_rates.FiatRateTable(root)


def test_fiat_candidate_refuses_a_currency_that_disagrees_with_the_symbol(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "chart.json"
    _chart(raw)
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="its currency must be"):
        fx_rates.stage_fiat_candidate(
            raw,
            tmp_path / "out",
            _policy(tmp_path / "p.json"),
            symbol="EURUSD=X",
            currency="GBP",
            observed_start="2026-01-01",
            observed_end="2026-07-02",
        )
    document = json.loads(raw.read_text(encoding="utf-8"))
    document["chart"]["result"][0]["meta"]["currency"] = "EUR"
    raw.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="not a currency quoted"):
        fx_rates.stage_fiat_candidate(
            raw,
            tmp_path / "out",
            _policy(tmp_path / "q.json"),
            symbol="EURUSD=X",
            currency="EUR",
            observed_start="2026-01-01",
            observed_end="2026-07-02",
        )


def test_fiat_candidate_refuses_a_boundary_past_the_response(tmp_path: Path) -> None:
    raw = tmp_path / "chart.json"
    _chart(raw)
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="runs past the last"):
        fx_rates.stage_fiat_candidate(
            raw,
            tmp_path / "out",
            _policy(tmp_path / "p.json"),
            symbol="EURUSD=X",
            currency="EUR",
            observed_start="2026-01-01",
            observed_end="2026-12-31",
        )


def test_fiat_table_rejects_a_manifest_edited_to_match_altered_bytes(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "chart.json"
    _chart(raw)
    result = fx_rates.stage_fiat_candidate(
        raw,
        tmp_path / "out",
        _policy(tmp_path / "p.json"),
        symbol="EURUSD=X",
        currency="EUR",
        observed_start="2026-01-01",
        observed_end="2026-07-02",
    )
    root = tmp_path / "out" / "generations" / str(result["generation_id"])
    altered = (root / "rates.csv").read_bytes().replace(b"1.17", b"9.99")
    (root / "rates.csv").write_bytes(altered)
    manifest = json.loads((root / "generation.json").read_bytes())
    for artifact in manifest["artifacts"]:
        if artifact["artifact_id"] == "rates.csv":
            import hashlib

            artifact["sha256"] = hashlib.sha256(altered).hexdigest()
    (root / "generation.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    # the stale generation id still matches the directory name, but is no longer the
    # identity of the edited manifest
    with pytest.raises(fx_rates.PortfolioFXRatesError, match="identity|invalid"):
        fx_rates.FiatRateTable(root)
