from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

from .config import DATASET_ORDER, ExportConfig, load_config
from .provider import YahooFinanceProvider
from .transform import (
    build_adjusted_marketdata_frame,
    build_current_snapshot_frame,
    build_name_map,
    build_raw_marketdata_frame,
    build_summary_frame,
    build_ticker_lookup_frame,
    enrich_history_with_names,
)
from .writer import WrittenArtifact, write_dataset


LOGGER = logging.getLogger("yf_marketdata")


@dataclass(frozen=True)
class ExportRun:
    artifacts: list[WrittenArtifact]
    row_counts: dict[str, int]


def run_export(config: ExportConfig, provider: YahooFinanceProvider | None = None) -> ExportRun:
    _configure_logging()
    active_provider = provider or YahooFinanceProvider(config)

    need_search = config.lookup.resolve_names or config.outputs.datasets["ticker_search"].enabled
    need_history = any(
        config.outputs.datasets[dataset_name].enabled
        for dataset_name in ["history_yf", "history_raw", "history_adjusted", "history_summary"]
    )
    need_current_snapshot = config.outputs.datasets["current_snapshot"].enabled
    search_results: dict[str, list[dict[str, object]]] = {}
    name_map: dict[str, str | None] = {ticker: None for ticker in config.source.tickers}
    if need_search:
        max_results = config.lookup.max_results if config.lookup.full_quote_results else 1
        LOGGER.info("Resolving search metadata for %s tickers", len(config.source.tickers))
        search_results = active_provider.fetch_search_results(config.source.tickers, max_results=max_results)
        if config.lookup.resolve_names:
            name_map.update(build_name_map(search_results))

    if need_history:
        LOGGER.info("Fetching history for %s tickers", len(config.source.tickers))
        history_yf_source = active_provider.fetch_history(config.source.tickers)
        history_yf = enrich_history_with_names(history_yf_source, name_map)
        history_raw = build_raw_marketdata_frame(history_yf)
        history_adjusted = build_adjusted_marketdata_frame(history_raw)
        history_summary = build_summary_frame(history_raw)
    else:
        history_yf = enrich_history_with_names(active_provider.fetch_history([]), name_map)
        history_raw = build_raw_marketdata_frame(history_yf)
        history_adjusted = build_adjusted_marketdata_frame(history_raw)
        history_summary = build_summary_frame(history_raw)

    ticker_search = (
        build_ticker_lookup_frame(search_results)
        if config.lookup.full_quote_results and config.outputs.datasets["ticker_search"].enabled
        else build_ticker_lookup_frame({})
    )
    if need_current_snapshot:
        LOGGER.info("Fetching current snapshot for %s tickers", len(config.source.tickers))
        current_snapshot_inputs = active_provider.fetch_current_snapshot_inputs(config.source.tickers)
        current_snapshot = build_current_snapshot_frame(config.source.tickers, name_map, current_snapshot_inputs, history_yf)
    else:
        current_snapshot = build_current_snapshot_frame([], name_map, {}, history_yf)

    datasets = {
        "history_yf": history_yf,
        "history_raw": history_raw,
        "history_adjusted": history_adjusted,
        "history_summary": history_summary,
        "ticker_search": ticker_search,
        "current_snapshot": current_snapshot,
    }

    artifacts: list[WrittenArtifact] = []
    for dataset_name in DATASET_ORDER:
        artifacts.extend(write_dataset(dataset_name, datasets[dataset_name], config.outputs.datasets[dataset_name], config.outputs))

    row_counts = {dataset_name: len(frame) for dataset_name, frame in datasets.items()}
    return ExportRun(artifacts=artifacts, row_counts=row_counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Yahoo Finance historical market data using a YAML config.")
    parser.add_argument("config", help="Path to the YAML config file.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    result = run_export(config)
    print("Artifacts:")
    for artifact in result.artifacts:
        print(f"- {artifact.dataset} [{artifact.layout}/{artifact.format}] -> {artifact.path} ({artifact.rows} rows)")
    print("Row counts:")
    for dataset_name, row_count in result.row_counts.items():
        print(f"- {dataset_name}: {row_count}")
    return 0


def _configure_logging() -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
