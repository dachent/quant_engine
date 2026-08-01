from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from yf_marketdata.config import DatasetOutputConfig, OutputsConfig, WORKSPACE_ROOT
from yf_marketdata.writer import sanitize_file_name, write_dataset


class TestYfMarketDataWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(dir=WORKSPACE_ROOT))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_write_stacked_and_per_ticker_outputs(self) -> None:
        frame = pd.DataFrame(
            [
                {"Ticker": "AAPL", "Ticker.Name": "Apple", "Date": pd.Timestamp("2020-01-03"), "Close": 2.0},
                {"Ticker": "AAPL", "Ticker.Name": "Apple", "Date": pd.Timestamp("2020-01-02"), "Close": 1.0},
                {"Ticker": "MSFT", "Ticker.Name": "Microsoft", "Date": pd.Timestamp("2020-01-02"), "Close": 3.0},
            ]
        )
        config = DatasetOutputConfig(enabled=True, layout="both", drop_identity_columns_per_ticker=True)
        outputs = OutputsConfig(root_dir=self.temp_dir, format="parquet", datasets={})

        artifacts = write_dataset("history_adjusted", frame, config, outputs)

        self.assertTrue(any(artifact.layout == "stacked" for artifact in artifacts))
        self.assertTrue(any(artifact.layout == "per_ticker" for artifact in artifacts))
        self.assertTrue(all(artifact.format == "parquet" for artifact in artifacts))

        per_ticker_parquet = self.temp_dir / "history_adjusted__AAPL.parquet"
        stacked_parquet = self.temp_dir / "history_adjusted.parquet"
        self.assertTrue(per_ticker_parquet.exists())
        self.assertTrue(stacked_parquet.exists())
        self.assertFalse((self.temp_dir / "history_adjusted").exists())

        per_ticker_frame = pd.read_parquet(per_ticker_parquet)
        self.assertNotIn("Ticker", per_ticker_frame.columns)
        self.assertNotIn("Ticker.Name", per_ticker_frame.columns)
        self.assertEqual(list(per_ticker_frame["Date"]), [pd.Timestamp("2020-01-03"), pd.Timestamp("2020-01-02")])

    def test_write_stacked_output_uses_global_csv_format(self) -> None:
        frame = pd.DataFrame([{"Ticker": "AAPL", "Ticker.Name": "Apple", "Date": pd.Timestamp("2020-01-03"), "Close": 2.0}])
        config = DatasetOutputConfig(enabled=True, layout="stacked", drop_identity_columns_per_ticker=True)
        outputs = OutputsConfig(root_dir=self.temp_dir, format="csv", datasets={})

        artifacts = write_dataset("history_raw", frame, config, outputs)

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].path, self.temp_dir / "history_raw.csv")
        self.assertTrue(artifacts[0].path.exists())

    def test_sanitize_file_name_matches_vba_behavior(self) -> None:
        self.assertEqual(sanitize_file_name('BTC/USD:*?"<>|'), "BTC_USD_______")
