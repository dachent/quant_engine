from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from yf_marketdata.config import WORKSPACE_ROOT, ConfigurationError, load_config


class TestYfMarketDataConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(dir=WORKSPACE_ROOT))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_config_with_today_token(self) -> None:
        payload = self._base_payload()
        payload["source"]["tickers"] = ["AAPL", "MSFT", "AAPL"]
        payload["history"]["end_date"] = "today"
        payload["lookup"]["full_quote_results"] = True
        payload["lookup"]["max_results"] = 5
        for dataset_name in payload["outputs"]["datasets"]:
            payload["outputs"]["datasets"][dataset_name]["enabled"] = True

        config_path = self._write_config(payload)
        config = load_config(config_path)

        self.assertEqual(config.source.tickers, ["AAPL", "MSFT"])
        self.assertLessEqual(config.history.start_date, config.history.end_date)
        self.assertEqual(config.history.interval, "1d")
        self.assertEqual(config.history.end_date, date.today())
        self.assertEqual(config.outputs.format, "parquet")
        self.assertEqual(config.outputs.root_dir, self.temp_dir / "out")

    def test_rejects_write_path_outside_workspace(self) -> None:
        payload = self._base_payload()
        payload["outputs"]["root_dir"] = str(Path(tempfile.gettempdir()) / "outside")
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def test_ticker_search_requires_full_quote_results(self) -> None:
        payload = self._base_payload()
        payload["outputs"]["datasets"]["ticker_search"]["enabled"] = True
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def test_rejects_removed_validation_section(self) -> None:
        payload = self._base_payload()
        payload["validation"] = {"fail_on_mismatch": False}
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def test_rejects_removed_dataset_formats_key(self) -> None:
        payload = self._base_payload()
        payload["outputs"]["datasets"]["history_raw"]["formats"] = ["csv"]
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def test_rejects_removed_dataset_path_key(self) -> None:
        payload = self._base_payload()
        payload["outputs"]["datasets"]["history_raw"]["path"] = "out/history_raw"
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def test_rejects_unsupported_output_format(self) -> None:
        payload = self._base_payload()
        payload["outputs"]["format"] = "xlsx"
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def test_rejects_legacy_dataset_names(self) -> None:
        payload = self._base_payload()
        payload["outputs"]["datasets"]["raw_marketdata"] = {"enabled": True, "layout": "stacked"}
        config_path = self._write_config(payload)
        with self.assertRaises(ConfigurationError):
            load_config(config_path)

    def _write_config(self, payload: dict) -> Path:
        path = self.temp_dir / "config.yaml"
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)
        return path

    def _base_payload(self) -> dict:
        return {
            "source": {"tickers": ["AAPL"]},
            "history": {"start_date": "2020-01-01", "end_date": "2020-01-02", "interval": "1d", "prepost": False},
            "lookup": {"resolve_names": True, "full_quote_results": False, "max_results": 1},
            "fetch": {
                "batch_size": 1,
                "threads": 1,
                "timeout_seconds": 30,
                "retry_attempts": 0,
                "retry_backoff_seconds": 0.0,
                "cache_dir": "cache",
            },
            "outputs": {
                "root_dir": "out",
                "format": "parquet",
                "datasets": {
                    "history_yf": {"enabled": True, "layout": "stacked"},
                    "history_raw": {"enabled": True, "layout": "stacked"},
                    "history_adjusted": {"enabled": True, "layout": "both"},
                    "history_summary": {"enabled": True, "layout": "stacked"},
                    "ticker_search": {"enabled": False, "layout": "stacked"},
                    "current_snapshot": {"enabled": False, "layout": "stacked"},
                },
            },
        }
