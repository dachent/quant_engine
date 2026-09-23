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
