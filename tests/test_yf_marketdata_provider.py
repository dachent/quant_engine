from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from yf_marketdata.config import (
    DatasetOutputConfig,
    ExportConfig,
    FetchConfig,
    HistoryConfig,
    LookupConfig,
    OutputsConfig,
    SourceConfig,
    WORKSPACE_ROOT,
)
from yf_marketdata.provider import YahooFinanceProvider


class TestYfMarketDataProvider(unittest.TestCase):
    def test_fetch_current_snapshot_inputs(self) -> None:
        config = _build_config()
        provider = YahooFinanceProvider(config)

        with patch("yf_marketdata.provider._import_yfinance", return_value=_FakeYf()):
            result = provider.fetch_current_snapshot_inputs(["AAPL"])

        self.assertIn("AAPL", result)
        self.assertEqual(result["AAPL"].info["currentPrice"], 123.45)
        self.assertEqual(result["AAPL"].analyst_targets["mean"], 130.0)
        self.assertEqual(result["AAPL"].eps_trend.loc["+1y", "current"], 5.8)

    def test_fetch_current_snapshot_inputs_retries_and_tolerates_partial_failures(self) -> None:
        config = _build_config(retry_attempts=1)
        provider = YahooFinanceProvider(config)

        with patch("yf_marketdata.provider._import_yfinance", return_value=_RetryingFakeYf()):
            result = provider.fetch_current_snapshot_inputs(["AAPL"])

        self.assertEqual(result["AAPL"].info["currentPrice"], 123.45)
        self.assertEqual(result["AAPL"].analyst_targets, {})
        self.assertEqual(result["AAPL"].eps_trend.loc["0y", "current"], 4.8)


def _build_config(retry_attempts: int = 0) -> ExportConfig:
    return ExportConfig(
        config_path=WORKSPACE_ROOT / "test.yaml",
        workspace_root=WORKSPACE_ROOT,
        source=SourceConfig(tickers=["AAPL"]),
        history=HistoryConfig(start_date=date(2020, 1, 1), end_date=date(2020, 1, 2), interval="1d", prepost=False),
        lookup=LookupConfig(resolve_names=True, full_quote_results=False, max_results=1),
        fetch=FetchConfig(
            batch_size=1,
            threads=1,
            timeout_seconds=30,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=0.0,
            cache_dir=WORKSPACE_ROOT / "outputs" / "test_cache",
        ),
        outputs=OutputsConfig(
            root_dir=WORKSPACE_ROOT / "outputs" / "test_out",
            format="csv",
            datasets={"current_snapshot": DatasetOutputConfig(enabled=True, layout="stacked", drop_identity_columns_per_ticker=True)},
        ),
    )


class _FakeTicker:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    @property
    def info(self) -> dict[str, object]:
        return {"currentPrice": 123.45}

    def get_analyst_price_targets(self) -> dict[str, object]:
        return {"mean": 130.0}

    def get_eps_trend(self) -> pd.DataFrame:
        return pd.DataFrame({"current": [4.8, 5.8]}, index=["0y", "+1y"])


class _FakeYf:
    def Ticker(self, ticker: str, session=None):  # noqa: N802
        return _FakeTicker(ticker)


class _RetryingFakeTicker(_FakeTicker):
    def __init__(self, ticker: str) -> None:
        super().__init__(ticker)
        self.info_attempts = 0

    @property
    def info(self) -> dict[str, object]:
        self.info_attempts += 1
        if self.info_attempts == 1:
            raise RuntimeError("transient info failure")
        return {"currentPrice": 123.45}

    def get_analyst_price_targets(self) -> dict[str, object]:
        raise RuntimeError("analyst targets unavailable")


class _RetryingFakeYf:
    def __init__(self) -> None:
        self._tickers: dict[tuple[str, bool], _RetryingFakeTicker] = {}

    def Ticker(self, ticker: str, session=None):  # noqa: N802
        key = (ticker, session is not None)
        if key not in self._tickers:
            self._tickers[key] = _RetryingFakeTicker(ticker)
        return self._tickers[key]
