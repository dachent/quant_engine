from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
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
VALID_LAYOUTS = {"stacked", "per_ticker", "both"}
VALID_FORMATS = {"csv", "parquet", "feather"}
DATASET_ORDER = [
    "history_yf",
    "history_raw",
    "history_adjusted",
    "history_summary",
    "ticker_search",
    "current_snapshot",
]
TOP_LEVEL_KEYS = {"source", "history", "lookup", "fetch", "outputs"}
OUTPUTS_KEYS = {"root_dir", "format", "datasets"}
DATASET_CONFIG_KEYS = {"enabled", "layout", "drop_identity_columns_per_ticker"}
LEGACY_TOP_LEVEL_KEYS = {"validation"}
LEGACY_DATASET_KEYS = {
    "yf_history",
    "raw_marketdata",
    "stg_adjmarketdata",
    "summary",
    "ticker_lookup",
}
LEGACY_DATASET_CONFIG_KEYS = {"formats", "path"}


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    tickers: list[str]


@dataclass(frozen=True)
class HistoryConfig:
    start_date: date
    end_date: date
    interval: str
    prepost: bool


@dataclass(frozen=True)
class LookupConfig:
    resolve_names: bool
    full_quote_results: bool
    max_results: int


@dataclass(frozen=True)
class FetchConfig:
    batch_size: int
    threads: int
    timeout_seconds: int
    retry_attempts: int
    retry_backoff_seconds: float
    cache_dir: Path | None


@dataclass(frozen=True)
class DatasetOutputConfig:
    enabled: bool
    layout: str
    drop_identity_columns_per_ticker: bool


@dataclass(frozen=True)
class OutputsConfig:
    root_dir: Path
    format: str
    datasets: dict[str, DatasetOutputConfig]


@dataclass(frozen=True)
class ExportConfig:
    config_path: Path
    workspace_root: Path
    source: SourceConfig
    history: HistoryConfig
    lookup: LookupConfig
    fetch: FetchConfig
    outputs: OutputsConfig


def load_config(path: str | Path) -> ExportConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ConfigurationError("Config root must be a mapping.")

    _reject_removed_keys(raw, LEGACY_TOP_LEVEL_KEYS, "config")
    _reject_unknown_keys(raw, TOP_LEVEL_KEYS, "config")

    source_raw = _require_mapping(raw, "source")
    history_raw = _require_mapping(raw, "history")
    lookup_raw = _require_mapping(raw, "lookup")
    fetch_raw = _require_mapping(raw, "fetch")
    outputs_raw = _require_mapping(raw, "outputs")

    tickers_raw = source_raw.get("tickers")
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise ConfigurationError("source.tickers must be a non-empty list.")
    tickers: list[str] = []
    seen: set[str] = set()
    for item in tickers_raw:
        ticker = str(item).strip()
        if not ticker:
            continue
        if ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    if not tickers:
        raise ConfigurationError("source.tickers must contain at least one non-empty ticker.")

    history = HistoryConfig(
        start_date=_parse_date(history_raw.get("start_date"), "history.start_date"),
        end_date=_parse_date(history_raw.get("end_date"), "history.end_date"),
        interval=_require_interval(history_raw, "interval"),
        prepost=_require_bool_with_default(history_raw, "prepost", False),
    )
    if history.start_date > history.end_date:
        raise ConfigurationError("history.start_date must be on or before history.end_date.")

    lookup = LookupConfig(
        resolve_names=_require_bool_with_default(lookup_raw, "resolve_names", True),
        full_quote_results=_require_bool_with_default(lookup_raw, "full_quote_results", False),
        max_results=_require_int(lookup_raw, "max_results", minimum=1),
    )

    fetch = FetchConfig(
        batch_size=_require_int(fetch_raw, "batch_size", minimum=1),
        threads=_require_int(fetch_raw, "threads", minimum=1),
        timeout_seconds=_require_int(fetch_raw, "timeout_seconds", minimum=1),
        retry_attempts=_require_int(fetch_raw, "retry_attempts", minimum=0),
        retry_backoff_seconds=_require_float(fetch_raw, "retry_backoff_seconds", minimum=0.0),
        cache_dir=_resolve_workspace_write_path(config_path.parent, fetch_raw.get("cache_dir"), "fetch.cache_dir"),
    )

    _reject_unknown_keys(outputs_raw, OUTPUTS_KEYS, "outputs")
    output_root_dir = _resolve_workspace_write_path(config_path.parent, outputs_raw.get("root_dir"), "outputs.root_dir")
    if output_root_dir.suffix:
        raise ConfigurationError("outputs.root_dir must be directory-like.")
    output_format = _normalize_format(outputs_raw.get("format"), "outputs.format")

    datasets_raw = outputs_raw.get("datasets")
    if not isinstance(datasets_raw, dict):
        raise ConfigurationError("outputs.datasets must be a mapping.")
    _reject_removed_keys(datasets_raw, LEGACY_DATASET_KEYS, "outputs.datasets")
    _reject_unknown_keys(datasets_raw, set(DATASET_ORDER), "outputs.datasets")

    datasets: dict[str, DatasetOutputConfig] = {}
    for dataset_name in DATASET_ORDER:
        dataset_cfg_raw = datasets_raw.get(dataset_name) or {}
        if not isinstance(dataset_cfg_raw, dict):
            raise ConfigurationError(f"outputs.datasets.{dataset_name} must be a mapping.")
        _reject_removed_keys(dataset_cfg_raw, LEGACY_DATASET_CONFIG_KEYS, f"outputs.datasets.{dataset_name}")
        _reject_unknown_keys(dataset_cfg_raw, DATASET_CONFIG_KEYS, f"outputs.datasets.{dataset_name}")
        enabled = _require_bool_with_default(dataset_cfg_raw, "enabled", False)
        layout = str(dataset_cfg_raw.get("layout", "stacked")).strip()
        if layout not in VALID_LAYOUTS:
            raise ConfigurationError(
                f"outputs.datasets.{dataset_name}.layout must be one of {sorted(VALID_LAYOUTS)}."
            )
        datasets[dataset_name] = DatasetOutputConfig(
            enabled=enabled,
            layout=layout,
            drop_identity_columns_per_ticker=_require_bool_with_default(
                dataset_cfg_raw,
                "drop_identity_columns_per_ticker",
                True,
            ),
        )

    if datasets["ticker_search"].enabled and not lookup.full_quote_results:
        raise ConfigurationError("lookup.full_quote_results must be true when outputs.datasets.ticker_search.enabled is true.")

    return ExportConfig(
        config_path=config_path,
        workspace_root=WORKSPACE_ROOT,
        source=SourceConfig(tickers=tickers),
        history=history,
        lookup=lookup,
        fetch=fetch,
        outputs=OutputsConfig(root_dir=output_root_dir, format=output_format, datasets=datasets),
    )


def _require_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a mapping.")
    return value


def _require_bool_with_default(parent: dict[str, Any], key: str, default: bool) -> bool:
    if key not in parent:
        return default
    value = parent.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean.")
    return value


def _require_int(parent: dict[str, Any], key: str, minimum: int | None = None) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{key} must be an integer.")
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{key} must be >= {minimum}.")
    return value


def _require_float(parent: dict[str, Any], key: str, minimum: float | None = None) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{key} must be a number.")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigurationError(f"{key} must be >= {minimum}.")
    return number


def _require_interval(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string.")
    interval = value.strip()
    if interval not in VALID_INTERVALS:
        raise ConfigurationError(f"{key} must be one of {sorted(VALID_INTERVALS)}.")
    return interval


def _normalize_format(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string.")
    fmt = value.strip().lower()
    if fmt not in VALID_FORMATS:
        raise ConfigurationError(f"{key} must be one of {sorted(VALID_FORMATS)}.")
    return fmt


def _parse_date(value: Any, key: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be an ISO date string or a supported token.")
    token = value.strip().lower()
    today = date.today()
    if token == "today":
        return today
    if token == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an ISO date string or a supported token.") from exc


def _resolve_workspace_write_path(base_dir: Path, raw_value: Any, key: str) -> Path:
    if raw_value in (None, ""):
        raise ConfigurationError(f"{key} must be set.")
    path = Path(str(raw_value)).expanduser()
    path = ((base_dir / path) if not path.is_absolute() else path).resolve()
    if not _is_within_workspace(path):
        raise ConfigurationError(f"{key} must resolve inside {WORKSPACE_ROOT}.")
    return path


def _is_within_workspace(path: Path) -> bool:
    try:
        path.relative_to(WORKSPACE_ROOT)
        return True
    except ValueError:
        return False


def _reject_removed_keys(parent: dict[str, Any], removed_keys: set[str], key: str) -> None:
    stale_keys = sorted(item for item in parent if item in removed_keys)
    if stale_keys:
        raise ConfigurationError(f"{key} contains removed keys: {stale_keys}.")


def _reject_unknown_keys(parent: dict[str, Any], allowed_keys: set[str], key: str) -> None:
    unknown_keys = sorted(item for item in parent if item not in allowed_keys)
    if unknown_keys:
        raise ConfigurationError(f"{key} contains unsupported keys: {unknown_keys}.")
