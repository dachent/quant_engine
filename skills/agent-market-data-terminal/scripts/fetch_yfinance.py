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


USER_AGENT = "Codex agent-market-data-terminal/1.0"
HISTORY_DATASETS = {
    "history",
    "history_yf",
    "history_raw",
    "history_adjusted",
    "history_summary",
}
DATASET_CHOICES = sorted(HISTORY_DATASETS | {"ticker_search", "current_snapshot"})
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
    parser = argparse.ArgumentParser(
        description="Fetch Yahoo Finance data directly through yfinance."
    )
    parser.add_argument("--dataset", required=True, choices=DATASET_CHOICES)
    parser.add_argument(
        "--ticker",
        dest="tickers",
        action="append",
        required=True,
        help="Ticker to fetch. Repeat for multiple tickers.",
    )
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    parser.add_argument(
        "--start",
        help="History start date in YYYY-MM-DD or a supported token such as today or yesterday.",
    )
    parser.add_argument(
        "--end",
        default="today",
        help="History end date in YYYY-MM-DD or a supported token such as today or yesterday.",
    )
    parser.add_argument(
        "--interval", default="1d", help="History interval, for example 1d or 1wk."
    )
    parser.add_argument(
        "--prepost",
        type=parse_bool,
        default=False,
        help="Include pre/post-market data for history datasets.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10, help="History batch size."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="History download and snapshot fetch concurrency.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="Network timeout for yfinance calls.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=2,
        help="Retry attempts for transient Yahoo failures.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Base retry backoff in seconds.",
    )
    parser.add_argument(
        "--resolve-names",
        type=parse_bool,
        default=False,
        help="Resolve Yahoo long/short names for workbook-lineage history datasets.",
    )
    parser.add_argument(
        "--search-count",
        type=int,
        default=5,
        help="Maximum quote-search results per ticker.",
    )
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
    if args.search_count < 1:
        raise ValueError("--search-count must be >= 1.")

    if args.dataset in HISTORY_DATASETS:
        if not args.start:
            raise ValueError("--start is required for history datasets.")
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
    normalized_value = value.strip().lower()
    today = date.today()
    if normalized_value == "today":
        return today
    if normalized_value == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD, today, or yesterday.") from exc


def resolve_cache_dir(raw_value: str | None, workspace_root: Path) -> Path | None:
    if not raw_value:
        return None
    path = Path(raw_value).expanduser()
    path = (
        (workspace_root / path).resolve() if not path.is_absolute() else path.resolve()
    )
    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"--cache-dir must resolve inside {workspace_root}.") from exc
    return path


def resolve_output_path(raw_value: str, workspace_root: Path) -> Path:
    path = Path(raw_value).expanduser()
    path = (
        (workspace_root / path).resolve() if not path.is_absolute() else path.resolve()
    )
    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"--output must resolve inside {workspace_root}.") from exc
    return path


def build_cache_key(dataset: str, payload: dict[str, object]) -> str:
    encoded = json.dumps({"dataset": dataset, **payload}, sort_keys=True).encode(
        "utf-8"
    )
    return sha256(encoded).hexdigest()[:20]


def history_cache_path(
    cache_dir: Path | None, tickers: list[str], args: argparse.Namespace
) -> Path | None:
    if cache_dir is None:
        return None
    key = build_cache_key(
        "history",
        {
            "tickers": tickers,
            "start": parse_date_token(args.start, "--start").isoformat(),
            "end": parse_date_token(args.end, "--end").isoformat(),
            "interval": args.interval,
            "prepost": args.prepost,
        },
    )
    return cache_dir / f"history_{key}.csv"


def history_selected_cache_path(
    cache_dir: Path | None, tickers: list[str], args: argparse.Namespace
) -> Path | None:
    raw_path = history_cache_path(cache_dir, tickers, args)
    if raw_path is None:
        return None
    return raw_path.with_name(f"{raw_path.stem}_selected.csv")


def snapshot_cache_path(cache_dir: Path | None, tickers: list[str]) -> Path | None:
    if cache_dir is None:
        return None
    key = build_cache_key("current_snapshot", {"tickers": tickers})
    return cache_dir / f"current_snapshot_{key}.json"


def search_cache_path(
    cache_dir: Path | None, ticker: str, search_count: int
) -> Path | None:
    if cache_dir is None:
        return None
    key = build_cache_key(
        "ticker_search", {"ticker": ticker, "search_count": search_count}
    )
    return cache_dir / f"ticker_search_{key}.json"


def batched(items: list[str], batch_size: int) -> list[list[str]]:
    return [
        items[index : index + batch_size] for index in range(0, len(items), batch_size)
    ]


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
    raise RuntimeError(
        str(last_error) if last_error is not None else "Unknown Yahoo fetch error."
    )


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
                    frames.append(
                        normalize_single_ticker_frame(raw[ticker].copy(), ticker)
                    )
        else:
            level1 = set(raw.columns.get_level_values(1))
            for ticker in requested_tickers:
                if ticker in level1:
                    frames.append(
                        normalize_single_ticker_frame(
                            raw.xs(ticker, axis=1, level=1).copy(), ticker
                        )
                    )
    else:
        frames.append(normalize_single_ticker_frame(raw.copy(), requested_tickers[0]))

    if not frames:
        return pandas.DataFrame(columns=HISTORY_COLUMNS)

    combined = pandas.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["Ticker", "Date"], keep="last")
    combined = combined.sort_values(["Ticker", "Date"], kind="stable").reset_index(
        drop=True
    )
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
        subset=[
            "Open",
            "High",
            "Low",
            "Close",
            "Adj Close",
            "Volume",
            "Dividends",
            "Stock Splits",
        ],
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


def configure_yfinance_cache(yf, cache_dir: Path | None) -> None:
    if cache_dir is None:
        raise ValueError("Yahoo history requires --cache-dir for task-local caching.")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = getattr(yf, "cache", None)
    setter = getattr(cache, "set_cache_location", None)
    if not callable(setter):
        raise RuntimeError("yfinance cache API is unavailable.")
    setter(str(cache_dir))


def filter_history_window_with_exclusions(frame, args: argparse.Namespace):
    pandas = import_pandas()
    if frame.empty:
        return frame, []
    start_date = pandas.Timestamp(parse_date_token(args.start, "--start"))
    end_date = pandas.Timestamp(parse_date_token(args.end, "--end"))
    filtered = frame.copy()
    parsed_dates = pandas.to_datetime(filtered["Date"], errors="coerce")
    if parsed_dates.dt.tz is not None:
        parsed_dates = parsed_dates.dt.tz_localize(None)
    exclusions = []
    for index, parsed_date in parsed_dates.items():
        if pandas.isna(parsed_date):
            reason = "invalid_date"
        elif parsed_date < start_date:
            reason = "out_of_window_before_start"
        elif parsed_date >= end_date:
            reason = "out_of_window_end_exclusive"
        else:
            continue
        exclusions.append(
            {
                "ticker": str(filtered.loc[index, "Ticker"]),
                "date": serialize_nested(filtered.loc[index, "Date"]),
                "reason": reason,
            }
        )
    filtered["Date"] = parsed_dates
    return (
        filtered.loc[
            (filtered["Date"] >= start_date) & (filtered["Date"] < end_date)
        ].reset_index(drop=True),
        exclusions,
    )


def filter_history_window(frame, args: argparse.Namespace):
    filtered, _ = filter_history_window_with_exclusions(frame, args)
    return filtered


def validate_source_dates(frame, tickers: list[str]) -> None:
    pandas = import_pandas()
    parsed_dates = pandas.to_datetime(frame["Date"], errors="coerce")
    invalid_tickers = sorted(
        str(ticker)
        for ticker in tickers
        if bool(parsed_dates.loc[frame["Ticker"] == ticker].isna().any())
    )
    if invalid_tickers:
        raise ValueError(f"invalid required date: {', '.join(invalid_tickers)}")


def invalid_history_tickers(
    frame, tickers: list[str], price_column: str = "Close"
) -> tuple[list[str], list[str]]:
    pandas = import_pandas()
    missing: list[str] = []
    invalid: list[str] = []
    for ticker in tickers:
        subset = frame.loc[frame["Ticker"] == ticker]
        if subset.empty:
            missing.append(ticker)
            continue
        prices = pandas.to_numeric(subset[price_column], errors="coerce")
        valid = prices.notna() & prices.map(math.isfinite) & (prices > 0)
        if not bool(valid.all()):
            invalid.append(ticker)
    return missing, invalid


def validate_history_frame(frame, tickers: list[str], price_column: str = "Close"):
    missing, invalid = invalid_history_tickers(frame, tickers, price_column)
    if missing:
        raise ValueError(f"missing required ticker data: {', '.join(missing)}")
    if invalid:
        raise ValueError(f"invalid in-window price: {', '.join(invalid)}")
    return frame


def history_source_evidence(frame, exclusions: list[dict[str, object]]):
    rows = frame_to_rows(frame[HISTORY_COLUMNS])
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "row_count": len(rows),
        "sha256": sha256(encoded).hexdigest(),
        "rows": rows,
    }, exclusions


def write_history_failure_receipt(
    cache_dir: Path,
    cache_path: Path | None,
    raw_frames: list,
    exclusions: list[dict[str, object]],
    reason: str,
) -> None:
    pandas = import_pandas()
    raw_frame = (
        pandas.concat(raw_frames, ignore_index=True)[HISTORY_COLUMNS]
        if raw_frames
        else pandas.DataFrame(columns=HISTORY_COLUMNS)
    )
    evidence, exclusions = history_source_evidence(raw_frame, exclusions)
    receipt = {
        "schema_version": 1,
        "status": "REJECTED",
        "reason": reason,
        "source_raw_artifact": evidence,
        "exclusions": exclusions,
    }
    receipt_bytes = json.dumps(
        receipt, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    receipt_digest = sha256(receipt_bytes).hexdigest()[:20]
    cache_prefix = (
        cache_path.stem.removeprefix("history_")
        if cache_path is not None
        else "uncached"
    )
    receipt_name = f"history_failure_{cache_prefix}_{receipt_digest}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = cache_dir / receipt_name
    if not receipt_path.exists():
        receipt_path.write_bytes(receipt_bytes + b"\n")


def fetch_history_frame(
    tickers: list[str],
    args: argparse.Namespace,
    cache_dir: Path | None,
    yf=None,
):
    pandas = import_pandas()
    yf = yf or import_yfinance()
    configure_yfinance_cache(yf, cache_dir)
    cache_path = history_cache_path(cache_dir, tickers, args)
    selected_cache_path = history_selected_cache_path(cache_dir, tickers, args)
    price_column = "Adj Close" if args.dataset == "history_adjusted" else "Close"
    if cache_path is not None and cache_path.exists():
        try:
            selected_path = (
                selected_cache_path
                if selected_cache_path is not None and selected_cache_path.exists()
                else cache_path
            )
            frame = pandas.read_csv(selected_path, parse_dates=["Date"])
            for column in HISTORY_COLUMNS:
                if column not in frame.columns:
                    frame[column] = pandas.NA
            frame = frame[HISTORY_COLUMNS]
            validate_source_dates(frame, tickers)
            raw_frame = pandas.read_csv(cache_path, parse_dates=["Date"])
            for column in HISTORY_COLUMNS:
                if column not in raw_frame.columns:
                    raw_frame[column] = pandas.NA
            raw_frame = raw_frame[HISTORY_COLUMNS]
            validate_source_dates(raw_frame, tickers)
            _, exclusions = filter_history_window_with_exclusions(raw_frame, args)
            frame, _ = filter_history_window_with_exclusions(frame, args)
            validate_history_frame(frame, tickers, price_column)
            frame.attrs["source_raw_artifact"], frame.attrs["exclusions"] = (
                history_source_evidence(raw_frame, exclusions)
            )
            return frame
        except (OSError, ValueError, pandas.errors.ParserError) as exc:
            warn(f"Ignoring unusable cached Yahoo history: {exc}")

    frames = []
    raw_frames = []
    exclusions = []

    def reject(exc: ValueError):
        write_history_failure_receipt(
            cache_dir, cache_path, raw_frames, exclusions, str(exc)
        )
        raise exc

    for batch in batched(tickers, args.batch_size):

        def fetch_batch():
            return normalize_download_result(
                download_history_frame(yf, batch, args), batch
            )

        try:
            batch_frame = fetch_with_retries(
                fetch_batch, args.retry_attempts, args.retry_backoff_seconds
            )
        except RuntimeError as exc:
            warn(f"Batch history fetch failed for {batch}: {exc}")
            batch_frame = pandas.DataFrame(columns=HISTORY_COLUMNS)

        raw_frames.append(batch_frame.copy())
        try:
            validate_source_dates(batch_frame, batch)
        except ValueError as exc:
            reject(exc)
        batch_frame, batch_exclusions = filter_history_window_with_exclusions(
            batch_frame, args
        )
        exclusions.extend(batch_exclusions)
        missing_tickers, invalid_tickers = invalid_history_tickers(
            batch_frame, batch, price_column
        )
        unusable_tickers = missing_tickers + invalid_tickers
        for ticker in unusable_tickers:
            try:
                single_frame = fetch_with_retries(
                    lambda ticker=ticker: normalize_download_result(
                        download_history_frame(yf, [ticker], args), [ticker]
                    ),
                    args.retry_attempts,
                    args.retry_backoff_seconds,
                )
                raw_frames.append(single_frame.copy())
                try:
                    validate_source_dates(single_frame, [ticker])
                except ValueError as exc:
                    reject(exc)
                single_frame, single_exclusions = filter_history_window_with_exclusions(
                    single_frame, args
                )
                exclusions.extend(single_exclusions)
                if not single_frame.empty:
                    batch_frame = batch_frame.loc[batch_frame["Ticker"] != ticker]
                    batch_frame = pandas.concat(
                        [batch_frame, single_frame], ignore_index=True
                    )
            except RuntimeError as exc:
                warn(f"History fetch failed for {ticker}: {exc}")

        if not batch_frame.empty:
            frames.append(batch_frame)

    if frames:
        combined = pandas.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset=["Ticker", "Date"], keep="last")
        combined = combined.sort_values(["Ticker", "Date"], kind="stable").reset_index(
            drop=True
        )
    else:
        combined = pandas.DataFrame(columns=HISTORY_COLUMNS)

    combined = combined[HISTORY_COLUMNS]
    try:
        validate_history_frame(combined, tickers, price_column)
    except ValueError as exc:
        reject(exc)
    raw_frame = (
        pandas.concat(raw_frames, ignore_index=True)[HISTORY_COLUMNS]
        if raw_frames
        else pandas.DataFrame(columns=HISTORY_COLUMNS)
    )
    combined.attrs["source_raw_artifact"], combined.attrs["exclusions"] = (
        history_source_evidence(raw_frame, exclusions)
    )
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        raw_frame.to_csv(cache_path, index=False)
        combined.to_csv(selected_cache_path, index=False)
    return combined[HISTORY_COLUMNS]


def fetch_search_results(
    tickers: list[str], args: argparse.Namespace, cache_dir: Path | None
) -> dict[str, list[dict[str, object]]]:
    yf = import_yfinance()
    results: dict[str, list[dict[str, object]]] = {}
    for ticker in tickers:
        cache_path = search_cache_path(cache_dir, ticker, args.search_count)
        if cache_path is not None and cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            results[ticker] = [item for item in cached if isinstance(item, dict)]
            continue

        def fetch_search():
            try:
                search = yf.Search(
                    ticker,
                    max_results=args.search_count,
                    news_count=0,
                    lists_count=0,
                    include_cb=False,
                    include_nav_links=False,
                    include_research=False,
                    timeout=args.timeout_seconds,
                )
            except TypeError:
                search = yf.Search(ticker, max_results=args.search_count)
            return [
                item for item in getattr(search, "quotes", []) if isinstance(item, dict)
            ]

        try:
            quotes = fetch_with_retries(
                fetch_search, args.retry_attempts, args.retry_backoff_seconds
            )
        except RuntimeError as exc:
            warn(f"Ticker search failed for {ticker}: {exc}")
            quotes = []
        results[ticker] = quotes
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(quotes, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return results


def build_name_map(
    search_results: dict[str, list[dict[str, object]]],
) -> dict[str, str | None]:
    names: dict[str, str | None] = {}
    for ticker, records in search_results.items():
        first = records[0] if records else {}
        long_name = first.get("longname") if isinstance(first, dict) else None
        short_name = first.get("shortname") if isinstance(first, dict) else None
        if isinstance(long_name, str) and long_name.strip():
            names[ticker] = long_name.strip()
        elif isinstance(short_name, str) and short_name.strip():
            names[ticker] = short_name.strip()
        else:
            names[ticker] = None
    return names


def build_ticker_search_frame(
    search_results: dict[str, list[dict[str, object]]],
):
    pandas = import_pandas()
    rows: list[dict[str, object]] = []
    for requested_ticker in sorted(search_results):
        for result_index, record in enumerate(
            search_results[requested_ticker], start=1
        ):
            row: dict[str, object] = {
                "RequestedTicker": requested_ticker,
                "ResultIndex": result_index,
            }
            for key, value in sorted(record.items()):
                row[key] = serialize_nested(value)
            rows.append(row)
    if not rows:
        return pandas.DataFrame(columns=["RequestedTicker", "ResultIndex"])
    return (
        pandas.DataFrame(rows)
        .sort_values(["RequestedTicker", "ResultIndex"], kind="stable")
        .reset_index(drop=True)
    )


def build_history_yf_frame(history_frame, name_map: dict[str, str | None]):
    pandas = import_pandas()
    if history_frame.empty:
        return pandas.DataFrame(columns=["Ticker", "Ticker.Name", *HISTORY_COLUMNS[1:]])
    frame = history_frame.copy()
    frame.insert(1, "Ticker.Name", frame["Ticker"].map(name_map))
    return frame.sort_values(
        ["Ticker", "Date"], ascending=[True, False], kind="stable"
    ).reset_index(drop=True)


def build_history_raw_frame(history_yf_frame):
    columns = [
        "Ticker",
        "Ticker.Name",
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "AdjClose",
    ]
    if history_yf_frame.empty:
        return history_yf_frame.rename(columns={"Adj Close": "AdjClose"}).reindex(
            columns=columns
        )
    return history_yf_frame.rename(columns={"Adj Close": "AdjClose"})[columns]


def build_history_adjusted_frame(history_raw_frame):
    pandas = import_pandas()
    columns = [
        "Ticker",
        "Ticker.Name",
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]
    if history_raw_frame.empty:
        return pandas.DataFrame(columns=columns)
    adjusted = history_raw_frame.copy()
    ratio = pandas.Series(pandas.NA, index=adjusted.index, dtype="Float64")
    valid = (
        adjusted["Close"].notna()
        & (adjusted["Close"] != 0)
        & adjusted["AdjClose"].notna()
    )
    ratio.loc[valid] = adjusted.loc[valid, "AdjClose"] / adjusted.loc[valid, "Close"]
    for column in ["Open", "High", "Low"]:
        adjusted[column] = adjusted[column] * ratio
    adjusted["Close"] = adjusted["AdjClose"]
    return adjusted[columns]


def build_history_summary_frame(history_raw_frame):
    pandas = import_pandas()
    columns = ["Ticker", "Ticker.Name", "Min of Date", "Max of Date", "Observations"]
    if history_raw_frame.empty:
        return pandas.DataFrame(columns=columns)
    summary = (
        history_raw_frame.groupby(["Ticker", "Ticker.Name"], dropna=False)["Date"]
        .agg(["min", "max", "count"])
        .reset_index()
        .rename(
            columns={
                "min": "Min of Date",
                "max": "Max of Date",
                "count": "Observations",
            }
        )
    )
    return summary[columns].sort_values("Ticker", kind="stable").reset_index(drop=True)


def transform_history_dataset(
    history_frame,
    dataset: str,
    name_map: dict[str, str | None],
):
    if dataset == "history":
        return history_frame
    history_yf = build_history_yf_frame(history_frame, name_map)
    if dataset == "history_yf":
        return history_yf
    history_raw = build_history_raw_frame(history_yf)
    if dataset == "history_raw":
        return history_raw
    if dataset == "history_adjusted":
        return build_history_adjusted_frame(history_raw)
    if dataset == "history_summary":
        return build_history_summary_frame(history_raw)
    raise ValueError(f"Unsupported history dataset: {dataset}")


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
        return datetime.combine(
            value, datetime.min.time(), tzinfo=timezone.utc
        ).isoformat()
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
        return fetch_with_retries(
            fetcher, args.retry_attempts, args.retry_backoff_seconds
        )
    except RuntimeError as exc:
        warn(f"Yahoo snapshot {label} fetch failed: {exc}")
        return default


def build_snapshot_row(
    ticker: str,
    info: dict[str, object],
    analyst_targets: dict[str, object],
    eps_trend,
    fast_info: dict[str, object],
) -> dict[str, object]:
    row: dict[str, object] = {
        "Ticker": ticker,
        "TickerType": first_present(info.get("typeDisp"), info.get("quoteType")),
        "QuoteTimestamp": extract_timestamp(info.get("regularMarketTime")),
        "LastPrice": first_present(
            info.get("currentPrice"),
            info.get("regularMarketPrice"),
            fast_info.get("lastPrice"),
        ),
        "Open": first_present(info.get("open"), fast_info.get("open")),
        "High": first_present(info.get("dayHigh"), fast_info.get("dayHigh")),
        "Low": first_present(info.get("dayLow"), fast_info.get("dayLow")),
        "Bid": info.get("bid"),
        "Ask": info.get("ask"),
        "BidSize": info.get("bidSize"),
        "AskSize": info.get("askSize"),
        "Volume": first_present(
            info.get("volume"),
            info.get("regularMarketVolume"),
            fast_info.get("lastVolume"),
            fast_info.get("volume"),
        ),
        "AverageVolume": first_present(
            info.get("averageVolume"), fast_info.get("tenDayAverageVolume")
        ),
        "AverageDailyVolume10Day": first_present(
            info.get("averageDailyVolume10Day"), fast_info.get("tenDayAverageVolume")
        ),
        "AverageDailyVolume3Month": first_present(
            info.get("averageDailyVolume3Month"),
            info.get("averageVolume"),
            fast_info.get("threeMonthAverageVolume"),
        ),
        "Beta": info.get("beta"),
        "FiftyDayAverage": first_present(
            info.get("fiftyDayAverage"), fast_info.get("fiftyDayAverage")
        ),
        "TwoHundredDayAverage": first_present(
            info.get("twoHundredDayAverage"), fast_info.get("twoHundredDayAverage")
        ),
        "FiftyTwoWeekLow": first_present(
            info.get("fiftyTwoWeekLow"), fast_info.get("yearLow")
        ),
        "FiftyTwoWeekHigh": first_present(
            info.get("fiftyTwoWeekHigh"), fast_info.get("yearHigh")
        ),
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


def fetch_single_snapshot_row(
    yf, ticker: str, args: argparse.Namespace
) -> dict[str, object]:
    handle = yf.Ticker(ticker)
    info = coerce_mapping(
        fetch_snapshot_endpoint("info", lambda: getattr(handle, "info", {}), args, {})
    )
    analyst_targets = coerce_mapping(
        fetch_snapshot_endpoint(
            "analyst targets", lambda: handle.get_analyst_price_targets(), args, {}
        )
    )
    eps_trend = coerce_frame(
        fetch_snapshot_endpoint("EPS trend", lambda: handle.get_eps_trend(), args, None)
    )
    fast_info = coerce_mapping(
        fetch_snapshot_endpoint(
            "fast info", lambda: dict(getattr(handle, "fast_info", {}) or {}), args, {}
        )
    )
    return build_snapshot_row(ticker, info, analyst_targets, eps_trend, fast_info)


def build_snapshot_frame(rows: list[dict[str, object]]):
    pandas = import_pandas()
    frame = pandas.DataFrame(rows)
    extra_columns = sorted(
        column for column in frame.columns if column not in SNAPSHOT_CANONICAL_COLUMNS
    )
    ordered_columns = [
        column for column in SNAPSHOT_CANONICAL_COLUMNS if column in frame.columns
    ] + extra_columns
    return frame.reindex(columns=ordered_columns)


def fetch_current_snapshot_frame(
    tickers: list[str], args: argparse.Namespace, cache_dir: Path | None
):
    yf = import_yfinance()
    cache_path = snapshot_cache_path(cache_dir, tickers)

    if cache_path is not None and cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            return build_snapshot_frame([row for row in rows if isinstance(row, dict)])

    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {
            executor.submit(fetch_single_snapshot_row, yf, ticker, args): ticker
            for ticker in tickers
        }
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
        cache_path.write_text(
            json.dumps(frame_to_rows(frame), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return frame


def frame_to_rows(frame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in frame.to_dict(orient="records"):
        rows.append({key: serialize_nested(value) for key, value in record.items()})
    return rows


def write_raw_frame(frame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)


def build_coverage(frame, requested_tickers: list[str]) -> dict[str, object]:
    ticker_column = None
    for candidate in ["Ticker", "RequestedTicker"]:
        if candidate in frame.columns:
            ticker_column = candidate
            break
    observed_tickers = (
        sorted(str(value) for value in frame[ticker_column].dropna().unique())
        if ticker_column is not None
        else []
    )
    per_ticker: dict[str, dict[str, object]] = {}
    if ticker_column is not None:
        for ticker in requested_tickers:
            subset = frame[frame[ticker_column] == ticker]
            item: dict[str, object] = {"rows": int(len(subset))}
            if "Date" in subset.columns and not subset.empty:
                dates = (
                    import_pandas()
                    .to_datetime(subset["Date"], errors="coerce")
                    .dropna()
                )
                if not dates.empty:
                    item["observed_start"] = dates.min().isoformat()
                    item["observed_end"] = dates.max().isoformat()
            per_ticker[ticker] = item
    coverage = {
        "claim": "PARTIAL",
        "reason": "Provider response is not silently expanded or exchange-calendar-imputed.",
        "requested_tickers": requested_tickers,
        "observed_tickers": observed_tickers,
        "missing_tickers": sorted(set(requested_tickers) - set(observed_tickers)),
        "per_ticker": per_ticker,
    }
    source_raw_artifact = frame.attrs.get("source_raw_artifact")
    if source_raw_artifact is not None:
        coverage["source_raw_artifact"] = source_raw_artifact
        coverage["exclusions"] = frame.attrs.get("exclusions", [])
    return coverage


def build_request_payload(
    args: argparse.Namespace, tickers: list[str]
) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": "yahoo",
        "transport": "yfinance",
        "dataset": args.dataset,
        "tickers": tickers,
        "retry_attempts": args.retry_attempts,
        "retry_backoff_seconds": args.retry_backoff_seconds,
    }
    if args.dataset in HISTORY_DATASETS:
        payload.update(
            {
                "start": args.start,
                "end": args.end,
                "interval": args.interval,
                "prepost": args.prepost,
                "resolve_names": args.resolve_names,
            }
        )
    if args.dataset == "ticker_search" or args.resolve_names:
        payload["search_count"] = args.search_count
    return payload


def request_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def write_run_manifest(
    output_path: Path,
    request_payload: dict[str, object],
    coverage: dict[str, object],
) -> Path:
    artifact_bytes = output_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "source_run_id": request_id(request_payload),
        "request": request_payload,
        "coverage": coverage,
        "artifact": {
            "name": output_path.name,
            "sha256": sha256(artifact_bytes).hexdigest(),
            "size_bytes": len(artifact_bytes),
        },
    }
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    args = parse_args()
    workspace_root = Path.cwd().resolve()

    try:
        output_path = resolve_output_path(args.output, workspace_root)
        tickers = validate_args(args)
        cache_dir = resolve_cache_dir(args.cache_dir, workspace_root)

        if args.dataset in HISTORY_DATASETS:
            history_frame = fetch_history_frame(tickers, args, cache_dir)
            name_map = {ticker: None for ticker in tickers}
            if args.resolve_names:
                search_results = fetch_search_results(tickers, args, cache_dir)
                name_map.update(build_name_map(search_results))
            frame = transform_history_dataset(history_frame, args.dataset, name_map)
            dataset_metadata = {
                "start": args.start,
                "end": args.end,
                "interval": args.interval,
                "prepost": args.prepost,
            }
        elif args.dataset == "ticker_search":
            frame = build_ticker_search_frame(
                fetch_search_results(tickers, args, cache_dir)
            )
            dataset_metadata = {"search_count": args.search_count}
        else:
            frame = fetch_current_snapshot_frame(tickers, args, cache_dir)
            dataset_metadata = {}

        request_payload = build_request_payload(args, tickers)
        coverage = build_coverage(frame, tickers)

        if args.format == "raw":
            write_raw_frame(frame, output_path)
            manifest_path = write_run_manifest(output_path, request_payload, coverage)
            print(f"Saved raw yfinance artifact to {output_path}")
            print(f"Saved source-run manifest to {manifest_path}")
            print(f"dataset={args.dataset} rows={len(frame)}")
            return 0

        artifact = {
            "schema_version": 1,
            "source_run_id": request_id(request_payload),
            "provider": "yahoo",
            "transport": "yfinance",
            "dataset": args.dataset,
            "tickers": tickers,
            "columns": list(frame.columns),
            "rows": frame_to_rows(frame),
            "coverage": coverage,
        }
        if cache_dir is not None:
            artifact["cache_dir"] = str(cache_dir)
        artifact.update(dataset_metadata)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path = write_run_manifest(output_path, request_payload, coverage)
        print(f"Saved parsed yfinance artifact to {output_path}")
        print(f"Saved source-run manifest to {manifest_path}")
        print(f"dataset={args.dataset} rows={len(frame)}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
