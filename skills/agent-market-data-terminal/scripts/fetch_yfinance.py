#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


USER_AGENT = "Codex agent-market-data-terminal/1.0"
VALID_INTERVALS = {
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1h",
    "1d",
    "5d",
    "1wk",
    "1mo",
    "3mo",
}
HISTORY_COLUMNS = [
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
SNAPSHOT_CANONICAL_COLUMNS = [
    "Ticker",
    "TickerType",
    "QuoteTimestamp",
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
    "AverageDailyVolume10Day",
    "AverageDailyVolume3Month",
    "Beta",
    "FiftyDayAverage",
    "TwoHundredDayAverage",
    "FiftyTwoWeekLow",
    "FiftyTwoWeekHigh",
    "MarketCap",
    "EnterpriseValue",
    "EpsTrailingTwelveMonths",
    "ForwardEps",
    "TrailingPE",
    "ForwardPE",
    "TargetPriceCurrent",
    "TargetPriceMean",
    "TargetPriceMedian",
    "TargetPriceLow",
    "TargetPriceHigh",
]


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("Expected true or false.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Yahoo Finance data directly through yfinance.")
    parser.add_argument("--dataset", required=True, choices=["history", "current_snapshot"])
    parser.add_argument("--ticker", dest="tickers", action="append", required=True, help="Ticker to fetch. Repeat for multiple tickers.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    parser.add_argument("--start", help="History start date in YYYY-MM-DD or a supported token such as today or yesterday.")
    parser.add_argument("--end", default="today", help="History end date in YYYY-MM-DD or a supported token such as today or yesterday.")
    parser.add_argument("--interval", default="1d", help="History interval, for example 1d or 1wk.")
    parser.add_argument("--prepost", type=parse_bool, default=False, help="Include pre/post-market data for history datasets.")
    parser.add_argument("--batch-size", type=int, default=10, help="History batch size.")
    parser.add_argument("--threads", type=int, default=4, help="History download and snapshot fetch concurrency.")
    parser.add_argument("--timeout-seconds", type=int, default=60, help="Network timeout for yfinance calls.")
    parser.add_argument("--retry-attempts", type=int, default=2, help="Retry attempts for transient Yahoo failures.")
    parser.add_argument("--retry-backoff-seconds", type=float, default=2.0, help="Base retry backoff in seconds.")
    parser.add_argument("--cache-dir", help="Optional workspace-local cache directory.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for ticker in args.tickers:
        cleaned = str(ticker).strip()
        if not cleaned:
            continue
        if cleaned not in seen:
            tickers.append(cleaned)
            seen.add(cleaned)
    if not tickers:
        raise ValueError("Provide at least one non-empty --ticker value.")

    if args.batch_size < 1:
        raise ValueError("--batch-size must be >= 1.")
    if args.threads < 1:
        raise ValueError("--threads must be >= 1.")
    if args.timeout_seconds < 1:
        raise ValueError("--timeout-seconds must be >= 1.")
    if args.retry_attempts < 0:
        raise ValueError("--retry-attempts must be >= 0.")
    if args.retry_backoff_seconds < 0:
        raise ValueError("--retry-backoff-seconds must be >= 0.")

    if args.dataset == "history":
        if not args.start:
            raise ValueError("--start is required when --dataset history is used.")
        if args.interval not in VALID_INTERVALS:
            raise ValueError(f"--interval must be one of {sorted(VALID_INTERVALS)}.")
        start_date = parse_date_token(args.start, "--start")
        end_date = parse_date_token(args.end, "--end")
        if start_date > end_date:
            raise ValueError("--start must be on or before --end.")

    return tickers


def import_optional(module_name: str, package_hint: str):
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise RuntimeError(f"{package_hint} is required for this helper.") from exc


def import_pandas():
    return import_optional("pandas", "The optional 'pandas' package")


def import_yfinance():
    return import_optional("yfinance", "The optional 'yfinance' package")


def parse_date_token(value: str, label: str) -> date:
    token = value.strip().lower()
    today = date.today()
    if token == "today":
        return today
    if token == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD, today, or yesterday.") from exc


def resolve_cache_dir(raw_value: str | None, workspace_root: Path) -> Path | None:
    if not raw_value:
        return None
    path = Path(raw_value).expanduser()
    path = (workspace_root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"--cache-dir must resolve inside {workspace_root}.") from exc
    return path


def build_cache_key(dataset: str, payload: dict[str, object]) -> str:
    encoded = json.dumps({"dataset": dataset, **payload}, sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()[:20]


def history_cache_path(cache_dir: Path | None, tickers: list[str], args: argparse.Namespace) -> Path | None:
    if cache_dir is None:
        return None
    key = build_cache_key(
        "history",
        {
            "tickers": tickers,
            "start": args.start,
            "end": args.end,
            "interval": args.interval,
            "prepost": args.prepost,
        },
    )
    return cache_dir / f"history_{key}.csv"


def snapshot_cache_path(cache_dir: Path | None, tickers: list[str]) -> Path | None:
    if cache_dir is None:
        return None
    key = build_cache_key("current_snapshot", {"tickers": tickers})
    return cache_dir / f"current_snapshot_{key}.json"


def batched(items: list[str], batch_size: int) -> list[list[str]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def fetch_with_retries(fetcher, attempts: int, backoff_seconds: float):
    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            return fetcher()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            sleep_seconds = backoff_seconds * (attempt + 1)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    raise RuntimeError(str(last_error) if last_error is not None else "Unknown Yahoo fetch error.")


def normalize_download_result(raw, requested_tickers: list[str]):
    pandas = import_pandas()
    if raw is None or raw.empty:
        return pandas.DataFrame(columns=HISTORY_COLUMNS)

    frames = []
    if isinstance(raw.columns, pandas.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        if any(ticker in level0 for ticker in requested_tickers):
            for ticker in requested_tickers:
                if ticker in level0:
                    frames.append(normalize_single_ticker_frame(raw[ticker].copy(), ticker))
        else:
            level1 = set(raw.columns.get_level_values(1))
            for ticker in requested_tickers:
                if ticker in level1:
                    frames.append(normalize_single_ticker_frame(raw.xs(ticker, axis=1, level=1).copy(), ticker))
    else:
        frames.append(normalize_single_ticker_frame(raw.copy(), requested_tickers[0]))

    if not frames:
        return pandas.DataFrame(columns=HISTORY_COLUMNS)

    combined = pandas.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["Ticker", "Date"], keep="last")
    combined = combined.sort_values(["Ticker", "Date"], kind="stable").reset_index(drop=True)
    return combined[HISTORY_COLUMNS]


def normalize_single_ticker_frame(frame, ticker: str):
    pandas = import_pandas()
    data = frame.reset_index()
    first_column = str(data.columns[0])
    data = data.rename(columns={first_column: "Date"})
    for column in HISTORY_COLUMNS[2:]:
        if column not in data.columns:
            data[column] = pandas.NA
    data.insert(0, "Ticker", ticker)
    data = data.dropna(
        axis=0,
        how="all",
        subset=["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"],
    )
    data["Date"] = pandas.to_datetime(data["Date"], errors="coerce")
    if hasattr(data["Date"].dt, "tz") and data["Date"].dt.tz is not None:
        data["Date"] = data["Date"].dt.tz_localize(None)
    return data[HISTORY_COLUMNS]


def download_history_frame(yf, tickers: list[str], args: argparse.Namespace):
    return yf.download(
        tickers=tickers if len(tickers) > 1 else tickers[0],
        start=parse_date_token(args.start, "--start").isoformat(),
        end=parse_date_token(args.end, "--end").isoformat(),
        interval=args.interval,
        prepost=args.prepost,
        actions=True,
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=args.threads,
        timeout=args.timeout_seconds,
    )


def fetch_history_frame(tickers: list[str], args: argparse.Namespace, cache_dir: Path | None):
    pandas = import_pandas()
    yf = import_yfinance()
    cache_path = history_cache_path(cache_dir, tickers, args)
    if cache_path is not None and cache_path.exists():
        frame = pandas.read_csv(cache_path, parse_dates=["Date"])
        for column in HISTORY_COLUMNS:
            if column not in frame.columns:
                frame[column] = pandas.NA
        return frame[HISTORY_COLUMNS]

    frames = []
    for batch in batched(tickers, args.batch_size):
        def fetch_batch():
            return normalize_download_result(download_history_frame(yf, batch, args), batch)

        try:
            batch_frame = fetch_with_retries(fetch_batch, args.retry_attempts, args.retry_backoff_seconds)
        except RuntimeError as exc:
            warn(f"Batch history fetch failed for {batch}: {exc}")
            batch_frame = pandas.DataFrame(columns=HISTORY_COLUMNS)

        batch_tickers = set(batch_frame["Ticker"]) if not batch_frame.empty else set()
        missing_tickers = [ticker for ticker in batch if ticker not in batch_tickers]
        for ticker in missing_tickers:
            try:
                single_frame = fetch_with_retries(
                    lambda ticker=ticker: normalize_download_result(download_history_frame(yf, [ticker], args), [ticker]),
                    args.retry_attempts,
                    args.retry_backoff_seconds,
                )
                if not single_frame.empty:
                    batch_frame = pandas.concat([batch_frame, single_frame], ignore_index=True)
            except RuntimeError as exc:
                warn(f"History fetch failed for {ticker}: {exc}")

        if not batch_frame.empty:
            frames.append(batch_frame)

    if frames:
        combined = pandas.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["Ticker", "Date"], keep="last")
        combined = combined.sort_values(["Ticker", "Date"], kind="stable").reset_index(drop=True)
    else:
        combined = pandas.DataFrame(columns=HISTORY_COLUMNS)

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_csv(cache_path, index=False)
    return combined[HISTORY_COLUMNS]


def extract_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not math.isnan(float(value)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    return str(value)


def first_present(*values: object) -> object | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        if value == "":
            continue
        return value
    return None


def serialize_nested(value: object) -> object:
    if value is None:
        return None
    try:
        pandas = import_pandas()
        if value is pandas.NA or pandas.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple, set)):
        return json.dumps(list(value), ensure_ascii=False)
    if hasattr(value, "item") and callable(getattr(value, "item")):
        try:
            return serialize_nested(value.item())
        except Exception:
            pass
    return str(value)


def coerce_mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def coerce_frame(value: object):
    pandas = import_pandas()
    return value.copy() if isinstance(value, pandas.DataFrame) else pandas.DataFrame()


def fetch_snapshot_endpoint(label: str, fetcher, args: argparse.Namespace, default):
    try:
        return fetch_with_retries(fetcher, args.retry_attempts, args.retry_backoff_seconds)
    except RuntimeError as exc:
        warn(f"Yahoo snapshot {label} fetch failed: {exc}")
        return default


def build_snapshot_row(ticker: str, info: dict[str, object], analyst_targets: dict[str, object], eps_trend, fast_info: dict[str, object]) -> dict[str, object]:
    row: dict[str, object] = {
        "Ticker": ticker,
        "TickerType": first_present(info.get("typeDisp"), info.get("quoteType")),
        "QuoteTimestamp": extract_timestamp(info.get("regularMarketTime")),
        "LastPrice": first_present(info.get("currentPrice"), info.get("regularMarketPrice"), fast_info.get("lastPrice")),
        "Open": first_present(info.get("open"), fast_info.get("open")),
        "High": first_present(info.get("dayHigh"), fast_info.get("dayHigh")),
        "Low": first_present(info.get("dayLow"), fast_info.get("dayLow")),
        "Bid": info.get("bid"),
        "Ask": info.get("ask"),
        "BidSize": info.get("bidSize"),
        "AskSize": info.get("askSize"),
        "Volume": first_present(info.get("volume"), info.get("regularMarketVolume"), fast_info.get("lastVolume"), fast_info.get("volume")),
        "AverageVolume": first_present(info.get("averageVolume"), fast_info.get("tenDayAverageVolume")),
        "AverageDailyVolume10Day": first_present(info.get("averageDailyVolume10Day"), fast_info.get("tenDayAverageVolume")),
        "AverageDailyVolume3Month": first_present(info.get("averageDailyVolume3Month"), info.get("averageVolume"), fast_info.get("threeMonthAverageVolume")),
        "Beta": info.get("beta"),
        "FiftyDayAverage": first_present(info.get("fiftyDayAverage"), fast_info.get("fiftyDayAverage")),
        "TwoHundredDayAverage": first_present(info.get("twoHundredDayAverage"), fast_info.get("twoHundredDayAverage")),
        "FiftyTwoWeekLow": first_present(info.get("fiftyTwoWeekLow"), fast_info.get("yearLow")),
        "FiftyTwoWeekHigh": first_present(info.get("fiftyTwoWeekHigh"), fast_info.get("yearHigh")),
        "MarketCap": first_present(info.get("marketCap"), fast_info.get("marketCap")),
        "EnterpriseValue": info.get("enterpriseValue"),
        "EpsTrailingTwelveMonths": info.get("epsTrailingTwelveMonths"),
        "ForwardEps": info.get("forwardEps"),
        "TrailingPE": info.get("trailingPE"),
        "ForwardPE": info.get("forwardPE"),
        "TargetPriceCurrent": analyst_targets.get("current"),
        "TargetPriceMean": analyst_targets.get("mean"),
        "TargetPriceMedian": analyst_targets.get("median"),
        "TargetPriceLow": analyst_targets.get("low"),
        "TargetPriceHigh": analyst_targets.get("high"),
    }

    for key in sorted(info):
        row[f"Info.{key}"] = serialize_nested(info[key])
    for key in sorted(analyst_targets):
        row[f"AnalystTargets.{key}"] = serialize_nested(analyst_targets[key])
    if not eps_trend.empty:
        for index_value, series in eps_trend.iterrows():
            for column_name, value in series.items():
                row[f"EpsTrend.{index_value}.{column_name}"] = serialize_nested(value)
    return row


def fetch_single_snapshot_row(yf, ticker: str, args: argparse.Namespace) -> dict[str, object]:
    handle = yf.Ticker(ticker)
    info = coerce_mapping(fetch_snapshot_endpoint("info", lambda: getattr(handle, "info", {}), args, {}))
    analyst_targets = coerce_mapping(
        fetch_snapshot_endpoint("analyst targets", lambda: handle.get_analyst_price_targets(), args, {})
    )
    eps_trend = coerce_frame(fetch_snapshot_endpoint("EPS trend", lambda: handle.get_eps_trend(), args, None))
    fast_info = coerce_mapping(fetch_snapshot_endpoint("fast info", lambda: dict(getattr(handle, "fast_info", {}) or {}), args, {}))
    return build_snapshot_row(ticker, info, analyst_targets, eps_trend, fast_info)


def build_snapshot_frame(rows: list[dict[str, object]]):
    pandas = import_pandas()
    frame = pandas.DataFrame(rows)
    extra_columns = sorted(column for column in frame.columns if column not in SNAPSHOT_CANONICAL_COLUMNS)
    ordered_columns = [column for column in SNAPSHOT_CANONICAL_COLUMNS if column in frame.columns] + extra_columns
    return frame.reindex(columns=ordered_columns)


def fetch_current_snapshot_frame(tickers: list[str], args: argparse.Namespace, cache_dir: Path | None):
    pandas = import_pandas()
    yf = import_yfinance()
    cache_path = snapshot_cache_path(cache_dir, tickers)

    if cache_path is not None and cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            return build_snapshot_frame([row for row in rows if isinstance(row, dict)])

    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {executor.submit(fetch_single_snapshot_row, yf, ticker, args): ticker for ticker in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                warn(f"Current snapshot fetch failed for {ticker}: {exc}")
                rows.append({"Ticker": ticker, "FetchError": str(exc)})

    rows.sort(key=lambda item: str(item.get("Ticker", "")))
    frame = build_snapshot_frame(rows)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(frame_to_rows(frame), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return frame


def frame_to_rows(frame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in frame.to_dict(orient="records"):
        rows.append({key: serialize_nested(value) for key, value in record.items()})
    return rows


def write_raw_frame(frame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    workspace_root = Path.cwd().resolve()

    try:
        tickers = validate_args(args)
        cache_dir = resolve_cache_dir(args.cache_dir, workspace_root)

        if args.dataset == "history":
            frame = fetch_history_frame(tickers, args, cache_dir)
            dataset_metadata = {
                "start": args.start,
                "end": args.end,
                "interval": args.interval,
                "prepost": args.prepost,
            }
        else:
            frame = fetch_current_snapshot_frame(tickers, args, cache_dir)
            dataset_metadata = {}

        if args.format == "raw":
            write_raw_frame(frame, output_path)
            print(f"Saved raw yfinance artifact to {output_path}")
            print(f"dataset={args.dataset} rows={len(frame)}")
            return 0

        artifact = {
            "provider": "yahoo",
            "transport": "yfinance",
            "dataset": args.dataset,
            "tickers": tickers,
            "columns": list(frame.columns),
            "rows": frame_to_rows(frame),
        }
        if cache_dir is not None:
            artifact["cache_dir"] = str(cache_dir)
        artifact.update(dataset_metadata)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed yfinance artifact to {output_path}")
        print(f"dataset={args.dataset} rows={len(frame)}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
