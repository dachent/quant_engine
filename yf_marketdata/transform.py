from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any

import pandas as pd


YF_HISTORY_OUTPUT_COLUMNS = [
    "Ticker",
    "Ticker.Name",
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
RAW_MARKETDATA_COLUMNS = ["Ticker", "Ticker.Name", "Date", "Open", "High", "Low", "Close", "Volume", "AdjClose"]
ADJUSTED_MARKETDATA_COLUMNS = ["Ticker", "Ticker.Name", "Date", "Open", "High", "Low", "Close", "Volume"]
SUMMARY_COLUMNS = ["Ticker", "Ticker.Name", "Min of Date", "Max of Date"]
CURRENT_SNAPSHOT_COLUMNS = [
    "Ticker",
    "Ticker.Name",
    "TickerType",
    "QuoteTimestampUtc",
    "QuoteDate",
    "LastPrice",
    "Open",
    "High",
    "Low",
    "Bid",
    "Ask",
    "BidSize",
    "AskSize",
    "Volume",
    "AverageVolume",
    "AverageVolume10Day",
    "AverageVolume3Month",
    "DayRange",
    "FiftyTwoWeekRange",
    "MarketCap",
    "EnterpriseValue",
    "EpsTrailingTwelveMonths",
    "ForwardEps",
    "EpsEstimateCurrentYear",
    "EpsEstimate1Y",
    "TargetPriceCurrent",
    "TargetPriceMean",
    "TargetPriceMedian",
    "TargetPriceLow",
    "TargetPriceHigh",
    "TrailingPe",
    "ForwardPe",
]


def enrich_history_with_names(history: pd.DataFrame, name_map: dict[str, str | None]) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame(columns=YF_HISTORY_OUTPUT_COLUMNS)
    enriched = history.copy()
    enriched.insert(1, "Ticker.Name", enriched["Ticker"].map(name_map))
    enriched = _sort_frame(enriched)
    return enriched[YF_HISTORY_OUTPUT_COLUMNS]


def build_raw_marketdata_frame(yf_history: pd.DataFrame) -> pd.DataFrame:
    if yf_history.empty:
        return pd.DataFrame(columns=RAW_MARKETDATA_COLUMNS)
    raw = yf_history.rename(columns={"Adj Close": "AdjClose"}).copy()
    raw = _sort_frame(raw)
    return raw[RAW_MARKETDATA_COLUMNS]


def build_adjusted_marketdata_frame(raw_marketdata: pd.DataFrame) -> pd.DataFrame:
    if raw_marketdata.empty:
        return pd.DataFrame(columns=ADJUSTED_MARKETDATA_COLUMNS)
    adjusted = raw_marketdata.copy()
    ratio = pd.Series(pd.NA, index=adjusted.index, dtype="Float64")
    valid = adjusted["Close"].notna() & (adjusted["Close"] != 0) & adjusted["AdjClose"].notna()
    ratio.loc[valid] = adjusted.loc[valid, "AdjClose"] / adjusted.loc[valid, "Close"]
    adjusted["Open"] = adjusted["Open"] * ratio
    adjusted["High"] = adjusted["High"] * ratio
    adjusted["Low"] = adjusted["Low"] * ratio
    adjusted["Close"] = adjusted["AdjClose"]
    adjusted = adjusted.drop(columns=["AdjClose"])
    adjusted = _sort_frame(adjusted)
    return adjusted[ADJUSTED_MARKETDATA_COLUMNS]


def build_summary_frame(raw_marketdata: pd.DataFrame) -> pd.DataFrame:
    if raw_marketdata.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)
    grouped = (
        raw_marketdata.groupby(["Ticker", "Ticker.Name"], dropna=False)["Date"]
        .agg(["min", "max"])
        .reset_index()
        .rename(columns={"min": "Min of Date", "max": "Max of Date"})
    )
    grouped = grouped.sort_values(["Ticker"], kind="stable").reset_index(drop=True)
    return grouped[SUMMARY_COLUMNS]


def build_ticker_lookup_frame(search_results: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for requested_ticker in sorted(search_results):
        for index, record in enumerate(search_results[requested_ticker], start=1):
            row = {"RequestedTicker": requested_ticker, "ResultIndex": index}
            for key, value in sorted(record.items()):
                row[key] = _normalize_value(value)
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["RequestedTicker", "ResultIndex"])
    frame = pd.DataFrame(rows)
    sort_columns = [column for column in ["RequestedTicker", "ResultIndex", "symbol"] if column in frame.columns]
    return frame.sort_values(sort_columns, kind="stable").reset_index(drop=True)


def build_current_snapshot_frame(
    tickers: list[str],
    name_map: dict[str, str | None],
    current_snapshot_inputs: dict[str, Any],
    history_yf: pd.DataFrame,
) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame(columns=CURRENT_SNAPSHOT_COLUMNS)

    latest_history = _latest_history_by_ticker(history_yf)
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        snapshot_input = current_snapshot_inputs.get(ticker)
        info = _coerce_mapping(getattr(snapshot_input, "info", {}))
        analyst_targets = _coerce_mapping(getattr(snapshot_input, "analyst_targets", {}))
        eps_trend = _coerce_frame(getattr(snapshot_input, "eps_trend", pd.DataFrame()))
        history_row = latest_history.get(ticker, {})

        quote_timestamp = _derive_quote_timestamp(info, history_row)
        last_price = _first_non_null(info.get("currentPrice"), info.get("regularMarketPrice"), history_row.get("Close"))
        open_price = _first_non_null(info.get("open"), info.get("regularMarketOpen"), history_row.get("Open"))
        high_price = _first_non_null(info.get("dayHigh"), info.get("regularMarketDayHigh"), history_row.get("High"))
        low_price = _first_non_null(info.get("dayLow"), info.get("regularMarketDayLow"), history_row.get("Low"))
        volume = _first_non_null(info.get("volume"), info.get("regularMarketVolume"), history_row.get("Volume"))
        average_volume_10_day = _first_non_null(info.get("averageDailyVolume10Day"), info.get("averageVolume10days"))
        day_range = _first_non_null(info.get("regularMarketDayRange"), _format_range(low_price, high_price))
        fifty_two_week_range = _first_non_null(
            info.get("fiftyTwoWeekRange"),
            _format_range(info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")),
        )
        eps_estimate_current_year = _first_non_null(_get_eps_trend_value(eps_trend, "0y", "current"), info.get("epsCurrentYear"))
        eps_estimate_1y = _first_non_null(_get_eps_trend_value(eps_trend, "+1y", "current"), info.get("forwardEps"), info.get("epsForward"))

        row = {
            "Ticker": ticker,
            "Ticker.Name": _first_non_null(name_map.get(ticker), info.get("longName"), info.get("shortName"), info.get("displayName")),
            "TickerType": _first_non_null(info.get("typeDisp"), info.get("quoteType")),
            "QuoteTimestampUtc": quote_timestamp,
            "QuoteDate": quote_timestamp.normalize() if not pd.isna(quote_timestamp) else pd.NaT,
            "LastPrice": last_price,
            "Open": open_price,
            "High": high_price,
            "Low": low_price,
            "Bid": info.get("bid"),
            "Ask": info.get("ask"),
            "BidSize": info.get("bidSize"),
            "AskSize": info.get("askSize"),
            "Volume": volume,
            "AverageVolume": info.get("averageVolume"),
            "AverageVolume10Day": average_volume_10_day,
            "AverageVolume3Month": info.get("averageDailyVolume3Month"),
            "DayRange": day_range,
            "FiftyTwoWeekRange": fifty_two_week_range,
            "MarketCap": _first_non_null(info.get("marketCap"), info.get("nonDilutedMarketCap")),
            "EnterpriseValue": info.get("enterpriseValue"),
            "EpsTrailingTwelveMonths": _first_non_null(info.get("epsTrailingTwelveMonths"), info.get("trailingEps")),
            "ForwardEps": _first_non_null(info.get("forwardEps"), info.get("epsForward")),
            "EpsEstimateCurrentYear": eps_estimate_current_year,
            "EpsEstimate1Y": eps_estimate_1y,
            "TargetPriceCurrent": _first_non_null(analyst_targets.get("current"), last_price),
            "TargetPriceMean": _first_non_null(analyst_targets.get("mean"), info.get("targetMeanPrice")),
            "TargetPriceMedian": _first_non_null(analyst_targets.get("median"), info.get("targetMedianPrice")),
            "TargetPriceLow": _first_non_null(analyst_targets.get("low"), info.get("targetLowPrice")),
            "TargetPriceHigh": _first_non_null(analyst_targets.get("high"), info.get("targetHighPrice")),
            "TrailingPe": info.get("trailingPE"),
            "ForwardPe": info.get("forwardPE"),
        }
        row.update(_flatten_prefixed_mapping("Info", info))
        row.update(_flatten_prefixed_mapping("AnalystTargets", analyst_targets))
        row.update(_flatten_eps_trend(eps_trend))
        rows.append(row)

    frame = pd.DataFrame(rows)
    frame = _sort_frame(frame)
    extra_columns = sorted(column for column in frame.columns if column not in CURRENT_SNAPSHOT_COLUMNS)
    return frame[CURRENT_SNAPSHOT_COLUMNS + extra_columns]


def build_name_map(search_results: dict[str, list[dict[str, Any]]]) -> dict[str, str | None]:
    names: dict[str, str | None] = {}
    for ticker, records in search_results.items():
        first = records[0] if records else {}
        if not isinstance(first, dict):
            names[ticker] = None
            continue
        long_name = first.get("longname")
        short_name = first.get("shortname")
        if isinstance(long_name, str) and long_name.strip():
            names[ticker] = long_name.strip()
        elif isinstance(short_name, str) and short_name.strip():
            names[ticker] = short_name.strip()
        else:
            names[ticker] = None
    return names


def sort_per_ticker_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "Date" not in frame.columns:
        return frame.copy()
    return frame.sort_values(["Date"], ascending=[False], kind="stable").reset_index(drop=True)


def _sort_frame(frame: pd.DataFrame) -> pd.DataFrame:
    sort_columns: list[str] = []
    ascending: list[bool] = []
    if "Ticker" in frame.columns:
        sort_columns.append("Ticker")
        ascending.append(True)
    if "Date" in frame.columns:
        sort_columns.append("Date")
        ascending.append(False)
    if sort_columns:
        frame = frame.sort_values(sort_columns, ascending=ascending, kind="stable").reset_index(drop=True)
    return frame


def _normalize_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _latest_history_by_ticker(history_yf: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if history_yf.empty or "Ticker" not in history_yf.columns:
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for ticker, ticker_frame in history_yf.groupby("Ticker", sort=False, dropna=False):
        latest_row = sort_per_ticker_frame(ticker_frame).iloc[0].to_dict()
        latest[ticker] = latest_row
    return latest


def _derive_quote_timestamp(info: dict[str, Any], history_row: dict[str, Any]) -> pd.Timestamp:
    value = info.get("regularMarketTime")
    if value is not None:
        timestamp = _coerce_timestamp(value)
        if timestamp is not pd.NaT:
            return timestamp
    history_date = history_row.get("Date")
    if history_date is None or pd.isna(history_date):
        return pd.NaT
    timestamp = pd.to_datetime(history_date, errors="coerce", utc=True)
    if timestamp is pd.NaT:
        return pd.NaT
    return timestamp.tz_convert("UTC").tz_localize(None) if timestamp.tzinfo is not None else timestamp


def _coerce_timestamp(value: Any) -> pd.Timestamp:
    if value is None:
        return pd.NaT
    if isinstance(value, (int, float)) and not pd.isna(value):
        unit = "ms" if abs(float(value)) >= 1_000_000_000_000 else "s"
        timestamp = pd.to_datetime(value, unit=unit, errors="coerce", utc=True)
    else:
        timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if timestamp is pd.NaT:
        return pd.NaT
    return timestamp.tz_convert("UTC").tz_localize(None)


def _format_range(low: Any, high: Any) -> str | None:
    if low is None or high is None or pd.isna(low) or pd.isna(high):
        return None
    return f"{low} - {high}"


def _first_non_null(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except TypeError:
            pass
        return value
    return None


def _flatten_prefixed_mapping(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in sorted(values.items()):
        flattened[f"{prefix}.{key}"] = _normalize_value(value)
    return flattened


def _flatten_eps_trend(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    flattened: dict[str, Any] = {}
    normalized = frame.copy()
    normalized.index = [_normalize_eps_period(value) for value in normalized.index]
    for period, row in normalized.iterrows():
        for column, value in row.items():
            flattened[f"EpsTrend.{period}.{column}"] = _normalize_value(value)
    return flattened


def _normalize_eps_period(value: Any) -> str:
    mapping = {
        "0q": "currentQuarter",
        "+1q": "plus1q",
        "0y": "currentYear",
        "+1y": "plus1y",
    }
    token = str(value)
    if token in mapping:
        return mapping[token]
    token = token.replace("+", "plus").replace("-", "minus")
    return token.replace(" ", "_")


def _get_eps_trend_value(frame: pd.DataFrame, period: str, column: str) -> Any:
    if frame.empty or period not in frame.index or column not in frame.columns:
        return None
    value = frame.loc[period, column]
    return _first_non_null(value)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _coerce_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame()
