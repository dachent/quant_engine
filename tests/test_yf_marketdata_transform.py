from __future__ import annotations

import unittest

import pandas as pd

from yf_marketdata.transform import (
    build_adjusted_marketdata_frame,
    build_current_snapshot_frame,
    build_name_map,
    build_raw_marketdata_frame,
    build_summary_frame,
    build_ticker_lookup_frame,
    enrich_history_with_names,
)


class TestYfMarketDataTransform(unittest.TestCase):
    def test_workbook_transform_chain(self) -> None:
        history = pd.DataFrame(
            [
                {
                    "Ticker": "AAPL",
                    "Date": pd.Timestamp("2020-01-02"),
                    "Open": 100.0,
                    "High": 110.0,
                    "Low": 95.0,
                    "Close": 105.0,
                    "Adj Close": 84.0,
                    "Volume": 1000,
                    "Dividends": 0.0,
                    "Stock Splits": 0.0,
                }
            ]
        )
        enriched = enrich_history_with_names(history, {"AAPL": "Apple Inc."})
        raw = build_raw_marketdata_frame(enriched)
        adjusted = build_adjusted_marketdata_frame(raw)
        summary = build_summary_frame(raw)

        self.assertEqual(raw.loc[0, "Ticker.Name"], "Apple Inc.")
        self.assertAlmostEqual(adjusted.loc[0, "Open"], 80.0)
        self.assertAlmostEqual(adjusted.loc[0, "High"], 88.0)
        self.assertAlmostEqual(adjusted.loc[0, "Low"], 76.0)
        self.assertAlmostEqual(adjusted.loc[0, "Close"], 84.0)
        self.assertEqual(summary.loc[0, "Min of Date"], pd.Timestamp("2020-01-02"))
        self.assertEqual(summary.loc[0, "Max of Date"], pd.Timestamp("2020-01-02"))

    def test_search_result_helpers(self) -> None:
        search_results = {
            "AAPL": [{"longname": "Apple Inc.", "symbol": "AAPL", "nested": {"x": 1}}],
            "MSFT": [{"shortname": "Microsoft", "symbol": "MSFT"}],
        }
        names = build_name_map(search_results)
        lookup = build_ticker_lookup_frame(search_results)
        self.assertEqual(names["AAPL"], "Apple Inc.")
        self.assertEqual(names["MSFT"], "Microsoft")
        self.assertIn("RequestedTicker", lookup.columns)
        self.assertIn("nested", lookup.columns)

    def test_build_current_snapshot_frame(self) -> None:
        history = pd.DataFrame(
            [
                {
                    "Ticker": "AAPL",
                    "Ticker.Name": "Apple Inc.",
                    "Date": pd.Timestamp("2020-01-02"),
                    "Open": 100.0,
                    "High": 110.0,
                    "Low": 95.0,
                    "Close": 105.0,
                    "Adj Close": 84.0,
                    "Volume": 1000,
                    "Dividends": 0.0,
                    "Stock Splits": 0.0,
                }
            ]
        )
        current_snapshot_inputs = {
            "AAPL": type(
                "CurrentSnapshotInput",
                (),
                {
                    "info": {
                        "regularMarketTime": 1_700_000_000,
                        "typeDisp": "Equity",
                        "quoteType": "EQUITY",
                        "currentPrice": 123.45,
                        "open": 120.0,
                        "dayHigh": 125.0,
                        "dayLow": 119.0,
                        "bid": 123.4,
                        "ask": 123.5,
                        "bidSize": 10,
                        "askSize": 12,
                        "volume": 5000,
                        "averageVolume": 5500,
                        "averageDailyVolume10Day": 5300,
                        "averageDailyVolume3Month": 5400,
                        "marketCap": 2_000_000,
                        "enterpriseValue": 2_100_000,
                        "epsTrailingTwelveMonths": 4.5,
                        "forwardEps": 5.5,
                        "trailingPE": 27.4,
                        "forwardPE": 22.4,
                        "beta": 1.23,
                        "fiftyDayAverage": 118.0,
                        "fiftyDayAverageChange": 5.45,
                        "fiftyDayAverageChangePercent": 0.048,
                        "twoHundredDayAverage": 110.0,
                        "twoHundredDayAverageChange": 13.45,
                        "twoHundredDayAverageChangePercent": 0.122,
                        "beta3Year": 1.11,
                        "fiftyTwoWeekLow": 80.0,
                        "fiftyTwoWeekHigh": 140.0,
                        "nested": {"x": 1},
                    },
                    "analyst_targets": {"current": 123.45, "mean": 130.0, "median": 129.0, "low": 115.0, "high": 145.0},
                    "eps_trend": pd.DataFrame(
                        {"current": [4.8, 5.8], "7daysAgo": [4.7, 5.7]},
                        index=["0y", "+1y"],
                    ),
                },
            )()
        }

        frame = build_current_snapshot_frame(["AAPL"], {"AAPL": "Apple Inc."}, current_snapshot_inputs, history)

        self.assertEqual(frame.loc[0, "Ticker.Name"], "Apple Inc.")
        self.assertEqual(frame.loc[0, "TickerType"], "Equity")
        self.assertEqual(frame.loc[0, "LastPrice"], 123.45)
        self.assertEqual(frame.loc[0, "EpsEstimateCurrentYear"], 4.8)
        self.assertEqual(frame.loc[0, "EpsEstimate1Y"], 5.8)
        self.assertEqual(frame.loc[0, "DayRange"], "119.0 - 125.0")
        self.assertEqual(frame.loc[0, "FiftyTwoWeekRange"], "80.0 - 140.0")
        self.assertEqual(frame.loc[0, "Info.beta"], 1.23)
        self.assertEqual(frame.loc[0, "Info.fiftyDayAverage"], 118.0)
        self.assertEqual(frame.loc[0, "Info.twoHundredDayAverage"], 110.0)
        self.assertEqual(frame.loc[0, "Info.beta3Year"], 1.11)
        self.assertEqual(frame.loc[0, "Info.typeDisp"], "Equity")
        self.assertEqual(frame.loc[0, "Info.quoteType"], "EQUITY")
        self.assertEqual(frame.loc[0, "Info.nested"], '{"x": 1}')
        self.assertEqual(frame.loc[0, "EpsTrend.plus1y.current"], 5.8)

    def test_build_current_snapshot_frame_falls_back_to_quote_type(self) -> None:
        frame = build_current_snapshot_frame(
            ["BTC-USD"],
            {"BTC-USD": "Bitcoin USD"},
            {
                "BTC-USD": type(
                    "CurrentSnapshotInput",
                    (),
                    {
                        "info": {"quoteType": "CRYPTOCURRENCY", "currentPrice": 100000.0},
                        "analyst_targets": {},
                        "eps_trend": pd.DataFrame(),
                    },
                )()
            },
            pd.DataFrame(),
        )

        self.assertEqual(frame.loc[0, "TickerType"], "CRYPTOCURRENCY")
