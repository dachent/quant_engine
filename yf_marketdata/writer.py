from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import DatasetOutputConfig, OutputsConfig
from .transform import sort_per_ticker_frame


IDENTITY_COLUMNS = ["Ticker", "Ticker.Name"]


@dataclass(frozen=True)
class WrittenArtifact:
    dataset: str
    layout: str
    format: str
    path: Path
    rows: int


def write_dataset(
    dataset_name: str,
    frame: pd.DataFrame,
    dataset_config: DatasetOutputConfig,
    outputs_config: OutputsConfig,
) -> list[WrittenArtifact]:
    if not dataset_config.enabled:
        return []
    artifacts: list[WrittenArtifact] = []
    if dataset_config.layout in {"stacked", "both"}:
        stacked_path = _resolve_stacked_base_path(dataset_name, outputs_config)
        artifacts.extend(_write_stacked(dataset_name, frame, stacked_path, outputs_config.format))
    if dataset_config.layout in {"per_ticker", "both"}:
        artifacts.extend(_write_per_ticker(dataset_name, frame, outputs_config.root_dir, dataset_config, outputs_config.format))
    return artifacts


def sanitize_file_name(value: str) -> str:
    output = value
    for bad_char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        output = output.replace(bad_char, "_")
    return output


def _write_stacked(dataset_name: str, frame: pd.DataFrame, base_path: Path, output_format: str) -> list[WrittenArtifact]:
    output_path = _materialize_format_path(base_path, output_format)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_frame(frame, output_path, output_format)
    return [WrittenArtifact(dataset=dataset_name, layout="stacked", format=output_format, path=output_path, rows=len(frame))]


def _write_per_ticker(
    dataset_name: str,
    frame: pd.DataFrame,
    output_dir: Path,
    dataset_config: DatasetOutputConfig,
    output_format: str,
) -> list[WrittenArtifact]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[WrittenArtifact] = []
    if frame.empty:
        empty_path = output_dir / f"{dataset_name}__empty"
        output_path = _materialize_format_path(empty_path, output_format)
        _write_frame(frame, output_path, output_format)
        artifacts.append(
            WrittenArtifact(dataset=dataset_name, layout="per_ticker", format=output_format, path=output_path, rows=0)
        )
        return artifacts

    if "Ticker" not in frame.columns:
        raise ValueError(f"Dataset {dataset_name} cannot be written per ticker because it has no Ticker column.")

    for ticker, ticker_frame in frame.groupby("Ticker", sort=True, dropna=False):
        per_ticker_frame = sort_per_ticker_frame(ticker_frame)
        if dataset_config.drop_identity_columns_per_ticker:
            removable = [column for column in IDENTITY_COLUMNS if column in per_ticker_frame.columns]
            per_ticker_frame = per_ticker_frame.drop(columns=removable)
        base_path = output_dir / f"{dataset_name}__{sanitize_file_name(str(ticker))}"
        output_path = _materialize_format_path(base_path, output_format)
        _write_frame(per_ticker_frame, output_path, output_format)
        artifacts.append(
            WrittenArtifact(
                dataset=dataset_name,
                layout="per_ticker",
                format=output_format,
                path=output_path,
                rows=len(per_ticker_frame),
            )
        )
    return artifacts


def _materialize_format_path(base_path: Path, fmt: str) -> Path:
    if base_path.suffix.lower() == f".{fmt}":
        return base_path
    return base_path.with_suffix(f".{fmt}")


def _resolve_stacked_base_path(dataset_name: str, outputs_config: OutputsConfig) -> Path:
    return outputs_config.root_dir / dataset_name


def _write_frame(frame: pd.DataFrame, path: Path, fmt: str) -> None:
    if fmt == "csv":
        csv_frame = _format_for_csv(frame)
        csv_frame.to_csv(path, index=False, encoding="utf-8")
        return
    if fmt == "parquet":
        frame.to_parquet(path, index=False)
        return
    if fmt == "feather":
        frame.reset_index(drop=True).to_feather(path)
        return
    raise ValueError(f"Unsupported format: {fmt}")


def _format_for_csv(frame: pd.DataFrame) -> pd.DataFrame:
    csv_frame = frame.copy()
    for column in csv_frame.columns:
        series = csv_frame[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            non_null = series.dropna()
            if non_null.empty or (non_null.dt.normalize() == non_null).all():
                csv_frame[column] = series.dt.strftime("%Y-%m-%d")
            else:
                csv_frame[column] = series.dt.strftime("%Y-%m-%d %H:%M:%S")
    return csv_frame
