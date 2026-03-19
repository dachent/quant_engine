from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

from yf_marketdata.config import WORKSPACE_ROOT, load_config
from yf_marketdata.exporter import run_export


class FakeProvider:
    def fetch_history(self, tickers: list[str]) -> pd.DataFrame:
        rows = []
        for ticker in tickers:
            rows.append(
                {
                    "Ticker": ticker,
                    "Date": pd.Timestamp("2020-01-02"),
                    "Open": 10.0,
                    "High": 12.0,
                    "Low": 9.0,
                    "Close": 11.0,
                    "Adj Close": 8.8,
                    "Volume": 100,
                    "Dividends": 0.0,
                    "Stock Splits": 0.0,
                }
            )
        return pd.DataFrame(rows)

    def fetch_search_results(self, tickers: list[str], max_results: int) -> dict[str, list[dict[str, object]]]:
        return {
            ticker: [
                {"symbol": ticker, "longname": f"{ticker} Name", "exchange": "TEST"},
                {"symbol": f"{ticker}.ALT", "shortname": f"{ticker} Alt"},
            ][:max_results]
            for ticker in tickers
        }

    def fetch_current_snapshot_inputs(self, tickers: list[str]) -> dict[str, object]:
        return {
            ticker: type(
                "CurrentSnapshotInput",
                (),
                {
                    "info": {
                        "regularMarketTime": 1_700_000_000,
                        "typeDisp": "Equity",
                        "quoteType": "EQUITY",
                        "currentPrice": 12.5,
                        "open": 10.0,
                        "dayHigh": 12.0,
                        "dayLow": 9.0,
                        "bid": 12.4,
                        "ask": 12.6,
                        "bidSize": 5,
                        "askSize": 7,
                        "volume": 100,
                        "averageVolume": 150,
                        "averageDailyVolume10Day": 120,
                        "averageDailyVolume3Month": 130,
                        "beta": 1.2,
                        "fiftyDayAverage": 11.5,
                        "twoHundredDayAverage": 10.5,
                        "fiftyTwoWeekLow": 8.0,
                        "fiftyTwoWeekHigh": 14.0,
                        "marketCap": 1_000_000,
                        "enterpriseValue": 1_100_000,
                        "epsTrailingTwelveMonths": 2.5,
                        "forwardEps": 3.0,
                        "trailingPE": 5.0,
                        "forwardPE": 4.0,
                        "nested": {"x": 1},
                    },
                    "analyst_targets": {"current": 12.5, "mean": 13.0, "median": 13.1, "low": 11.0, "high": 15.0},
                    "eps_trend": pd.DataFrame(
                        {
                            "current": [2.8, 3.3],
                            "30daysAgo": [2.7, 3.2],
                        },
                        index=["0y", "+1y"],
                    ),
                },
            )()
            for ticker in tickers
        }


class TestYfMarketDataIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(dir=WORKSPACE_ROOT))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_end_to_end_export(self) -> None:
        config_path = self.temp_dir / "config.yaml"
        payload = {
            "source": {"tickers": ["AAPL", "MSFT"]},
            "history": {"start_date": "2020-01-01", "end_date": "2020-01-03", "interval": "1d", "prepost": False},
            "lookup": {"resolve_names": True, "full_quote_results": True, "max_results": 2},
            "fetch": {
                "batch_size": 2,
                "threads": 1,
                "timeout_seconds": 30,
                "retry_attempts": 0,
                "retry_backoff_seconds": 0.0,
                "cache_dir": "cache",
            },
            "outputs": {
                "root_dir": "out",
                "format": "csv",
                "datasets": {
                    "history_yf": {"enabled": True, "layout": "stacked"},
                    "history_raw": {"enabled": True, "layout": "stacked"},
                    "history_adjusted": {"enabled": True, "layout": "both"},
                    "history_summary": {"enabled": True, "layout": "stacked"},
                    "ticker_search": {"enabled": True, "layout": "stacked"},
                    "current_snapshot": {"enabled": True, "layout": "stacked"},
                },
            },
        }
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)

        config = load_config(config_path)
        result = run_export(config, provider=FakeProvider())

        self.assertGreater(len(result.artifacts), 0)
        self.assertTrue(all(artifact.format == "csv" for artifact in result.artifacts))
        self.assertEqual(result.row_counts["history_raw"], 2)
        self.assertEqual(result.row_counts["history_adjusted"], 2)
        self.assertEqual(result.row_counts["history_summary"], 2)
        self.assertEqual(result.row_counts["ticker_search"], 4)
        self.assertEqual(result.row_counts["current_snapshot"], 2)

        raw_csv = self.temp_dir / "out" / "history_raw.csv"
        per_ticker_csv = self.temp_dir / "out" / "history_adjusted__AAPL.csv"
        current_snapshot_csv = self.temp_dir / "out" / "current_snapshot.csv"
        self.assertTrue(raw_csv.exists())
        self.assertTrue(per_ticker_csv.exists())
        self.assertTrue(current_snapshot_csv.exists())
        self.assertFalse((self.temp_dir / "out" / "history_raw").exists())

        per_ticker_frame = pd.read_csv(per_ticker_csv)
        current_snapshot_frame = pd.read_csv(current_snapshot_csv)
        self.assertNotIn("Ticker", per_ticker_frame.columns)
        self.assertEqual(float(per_ticker_frame.loc[0, "Close"]), 8.8)
        self.assertIn("TickerType", current_snapshot_frame.columns)
        self.assertEqual(current_snapshot_frame.loc[0, "TickerType"], "Equity")
        self.assertIn("Info.quoteType", current_snapshot_frame.columns)
        self.assertIn("Info.fiftyDayAverage", current_snapshot_frame.columns)
        self.assertIn("TargetPriceMean", current_snapshot_frame.columns)
        self.assertIn("Info.nested", current_snapshot_frame.columns)
