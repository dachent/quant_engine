from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import logging
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .config import ExportConfig


LOGGER = logging.getLogger("yf_marketdata")
YF_HISTORY_COLUMNS = [
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
]


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class CurrentSnapshotInput:
    info: dict[str, Any]
    analyst_targets: dict[str, Any]
    eps_trend: pd.DataFrame


class YahooFinanceProvider:
    def __init__(self, config: ExportConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def fetch_history(self, tickers: list[str]) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for batch_index, batch in enumerate(_batched(tickers, self.config.fetch.batch_size), start=1):
            LOGGER.info("Fetching history batch %s with %s tickers", batch_index, len(batch))
            batch_frame = self._fetch_history_batch(batch)
            if not batch_frame.empty:
                frames.append(batch_frame)
        if not frames:
            return pd.DataFrame(columns=YF_HISTORY_COLUMNS)
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["Ticker", "Date"], keep="last")
        return combined[YF_HISTORY_COLUMNS]

    def fetch_search_results(self, tickers: list[str], max_results: int) -> dict[str, list[dict[str, Any]]]:
        results: dict[str, list[dict[str, Any]]] = {}
        for ticker in tickers:
            results[ticker] = self._fetch_search_result(ticker, max_results=max_results)
        return results

    def fetch_current_snapshot_inputs(self, tickers: list[str]) -> dict[str, CurrentSnapshotInput]:
        if not tickers:
            return {}

        results: dict[str, CurrentSnapshotInput] = {}
        max_workers = max(1, self.config.fetch.threads)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._fetch_current_snapshot_ticker, ticker): ticker for ticker in tickers}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    results[ticker] = future.result()
                except Exception as exc:
                    LOGGER.warning("Current snapshot fetch failed for %s: %s", ticker, exc)
                    results[ticker] = CurrentSnapshotInput(info={}, analyst_targets={}, eps_trend=pd.DataFrame())
        return results

    def _fetch_history_batch(self, tickers: list[str]) -> pd.DataFrame:
        cache_path = self._history_cache_path(tickers)
        if cache_path is not None and cache_path.exists():
            LOGGER.info("Loading cached history from %s", cache_path)
            return pd.read_parquet(cache_path)

        last_error: Exception | None = None
        for attempt in range(self.config.fetch.retry_attempts + 1):
            try:
                frame = self._download_history_frame(tickers)
                missing = [ticker for ticker in tickers if ticker not in set(frame["Ticker"])]
                if missing:
                    fallback_frames = [frame]
                    for ticker in missing:
                        LOGGER.warning("Ticker %s missing from batch result; retrying individually", ticker)
                        fallback_frames.append(self._fetch_history_individual(ticker))
                    frame = pd.concat(fallback_frames, ignore_index=True)
                    frame = frame.drop_duplicates(subset=["Ticker", "Date"], keep="last")
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    frame.to_parquet(cache_path, index=False)
                return frame[YF_HISTORY_COLUMNS]
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.fetch.retry_attempts:
                    break
                sleep_seconds = self.config.fetch.retry_backoff_seconds * (attempt + 1)
                LOGGER.warning(
                    "History batch fetch failed on attempt %s/%s: %s",
                    attempt + 1,
                    self.config.fetch.retry_attempts + 1,
                    exc,
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        if len(tickers) > 1:
            LOGGER.warning("Batch history fetch failed; falling back to individual downloads for %s tickers", len(tickers))
            frames = [self._fetch_history_individual(ticker) for ticker in tickers]
            frames = [frame for frame in frames if not frame.empty]
            if not frames:
                raise ProviderError(f"History fetch failed for batch {tickers}: {last_error}")
            combined = pd.concat(frames, ignore_index=True)
            combined = combined.drop_duplicates(subset=["Ticker", "Date"], keep="last")
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                combined.to_parquet(cache_path, index=False)
            return combined[YF_HISTORY_COLUMNS]

        raise ProviderError(f"History fetch failed for {tickers[0]}: {last_error}")

    def _fetch_history_individual(self, ticker: str) -> pd.DataFrame:
        cache_path = self._history_cache_path([ticker])
        if cache_path is not None and cache_path.exists():
            return pd.read_parquet(cache_path)

        last_error: Exception | None = None
        for attempt in range(self.config.fetch.retry_attempts + 1):
            try:
                frame = self._download_history_frame([ticker])
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    frame.to_parquet(cache_path, index=False)
                return frame[YF_HISTORY_COLUMNS]
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.fetch.retry_attempts:
                    break
                sleep_seconds = self.config.fetch.retry_backoff_seconds * (attempt + 1)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        raise ProviderError(f"History fetch failed for {ticker}: {last_error}")

    def _download_history_frame(self, tickers: list[str]) -> pd.DataFrame:
        yf = _import_yfinance()
        kwargs = {
            "tickers": tickers if len(tickers) > 1 else tickers[0],
            "start": self.config.history.start_date.isoformat(),
            "end": self.config.history.end_date.isoformat(),
            "interval": self.config.history.interval,
            "prepost": self.config.history.prepost,
            "actions": True,
            "auto_adjust": False,
            "group_by": "ticker",
            "progress": False,
            "threads": self.config.fetch.threads,
            "timeout": self.config.fetch.timeout_seconds,
            "repair": False,
        }
        try:
            raw = yf.download(session=self.session, **kwargs)
        except TypeError:
            raw = yf.download(**kwargs)
        except Exception as exc:
            if "curl_cffi session" in str(exc):
                raw = yf.download(**kwargs)
            else:
                raise
        return _normalize_download_result(raw, tickers)

    def _fetch_search_result(self, ticker: str, *, max_results: int) -> list[dict[str, Any]]:
        cache_path = self._search_cache_path(ticker, max_results)
        if cache_path is not None and cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]

        yf = _import_yfinance()
        last_error: Exception | None = None
        for attempt in range(self.config.fetch.retry_attempts + 1):
            try:
                search = _build_search(yf, self.session, ticker, max_results, self.config.fetch.timeout_seconds)
                quotes = [item for item in search.quotes if isinstance(item, dict)]
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with cache_path.open("w", encoding="utf-8") as handle:
                        json.dump(quotes, handle, ensure_ascii=False, indent=2)
                return quotes
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.fetch.retry_attempts:
                    break
                sleep_seconds = self.config.fetch.retry_backoff_seconds * (attempt + 1)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        raise ProviderError(f"Search fetch failed for {ticker}: {last_error}")

    def _fetch_current_snapshot_ticker(self, ticker: str) -> CurrentSnapshotInput:
        yf = _import_yfinance()
        ticker_handle = _build_ticker(yf, ticker, self.session)
        fallback_ticker_handle = None

        def _run(fetcher):
            nonlocal fallback_ticker_handle
            try:
                return fetcher(ticker_handle)
            except Exception as exc:
                if "curl_cffi session" not in str(exc):
                    raise
                if fallback_ticker_handle is None:
                    fallback_ticker_handle = yf.Ticker(ticker)
                return fetcher(fallback_ticker_handle)

        info = self._fetch_current_endpoint(
            ticker,
            "info",
            lambda: _coerce_mapping(_run(lambda handle: getattr(handle, "info", {}))),
            default={},
        )
        analyst_targets = self._fetch_current_endpoint(
            ticker,
            "analyst targets",
            lambda: _coerce_mapping(_run(lambda handle: handle.get_analyst_price_targets())),
            default={},
        )
        eps_trend = self._fetch_current_endpoint(
            ticker,
            "EPS trend",
            lambda: _coerce_frame(_run(lambda handle: handle.get_eps_trend())),
            default=pd.DataFrame(),
        )
        return CurrentSnapshotInput(info=info, analyst_targets=analyst_targets, eps_trend=eps_trend)

    def _fetch_current_endpoint(self, ticker: str, label: str, fetcher, default):
        last_error: Exception | None = None
        for attempt in range(self.config.fetch.retry_attempts + 1):
            try:
                return fetcher()
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.fetch.retry_attempts:
                    break
                sleep_seconds = self.config.fetch.retry_backoff_seconds * (attempt + 1)
                LOGGER.warning(
                    "Current %s fetch failed for %s on attempt %s/%s: %s",
                    label,
                    ticker,
                    attempt + 1,
                    self.config.fetch.retry_attempts + 1,
                    exc,
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        LOGGER.warning("Current %s unavailable for %s: %s", label, ticker, last_error)
        return default

    def _history_cache_path(self, tickers: list[str]) -> Path | None:
        if self.config.fetch.cache_dir is None:
            return None
        payload = {
            "tickers": tickers,
            "start": self.config.history.start_date.isoformat(),
            "end": self.config.history.end_date.isoformat(),
            "interval": self.config.history.interval,
            "prepost": self.config.history.prepost,
        }
        digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]
        return self.config.fetch.cache_dir / f"history_{digest}.parquet"

    def _search_cache_path(self, ticker: str, max_results: int) -> Path | None:
        if self.config.fetch.cache_dir is None:
            return None
        payload = {"ticker": ticker, "max_results": max_results}
        digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]
        return self.config.fetch.cache_dir / f"search_{digest}.json"


def _normalize_download_result(raw: pd.DataFrame, requested_tickers: list[str]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=YF_HISTORY_COLUMNS)

    frames: list[pd.DataFrame] = []
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        if any(ticker in level0 for ticker in requested_tickers):
            for ticker in requested_tickers:
                if ticker in level0:
                    frames.append(_normalize_single_ticker_frame(raw[ticker].copy(), ticker))
        else:
            level1 = set(raw.columns.get_level_values(1))
            for ticker in requested_tickers:
                if ticker in level1:
                    frames.append(_normalize_single_ticker_frame(raw.xs(ticker, axis=1, level=1).copy(), ticker))
    else:
        frames.append(_normalize_single_ticker_frame(raw.copy(), requested_tickers[0]))

    if not frames:
        return pd.DataFrame(columns=YF_HISTORY_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    return combined[YF_HISTORY_COLUMNS]


def _normalize_single_ticker_frame(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    data = frame.reset_index()
    first_column = str(data.columns[0])
    data = data.rename(columns={first_column: "Date"})
    for column in YF_HISTORY_COLUMNS[2:]:
        if column not in data.columns:
            data[column] = pd.NA
    data.insert(0, "Ticker", ticker)
    data = data.dropna(
        axis=0,
        how="all",
        subset=["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"],
    )
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    if hasattr(data["Date"].dt, "tz") and data["Date"].dt.tz is not None:
        data["Date"] = data["Date"].dt.tz_localize(None)
    return data[YF_HISTORY_COLUMNS]


def _import_yfinance():
    try:
        import yfinance as yf
    except ModuleNotFoundError as exc:
        vendor_dir = Path(__file__).resolve().parent.parent / ".vendor"
        if vendor_dir.exists():
            sys.path.insert(0, str(vendor_dir))
            try:
                import yfinance as yf
            except ModuleNotFoundError:
                raise ProviderError("yfinance is not installed. Install it before running the exporter.") from exc
        else:
            raise ProviderError("yfinance is not installed. Install it before running the exporter.") from exc
    return yf


def _build_ticker(yf, ticker: str, session: requests.Session):
    try:
        return yf.Ticker(ticker, session=session)
    except Exception as exc:
        if "curl_cffi session" not in str(exc) and not isinstance(exc, TypeError):
            raise
        return yf.Ticker(ticker)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _coerce_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame()


def _batched(items: list[str], batch_size: int) -> list[list[str]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def _build_search(yf, session: requests.Session, ticker: str, max_results: int, timeout_seconds: int):
    kwargs = {
        "query": ticker,
        "max_results": max_results,
        "news_count": 0,
        "lists_count": 0,
        "include_research": False,
        "include_nav_links": False,
        "timeout": timeout_seconds,
    }
    try:
        return yf.Search(session=session, **kwargs)
    except Exception as exc:
        if "curl_cffi session" not in str(exc):
            raise
        return yf.Search(**kwargs)
