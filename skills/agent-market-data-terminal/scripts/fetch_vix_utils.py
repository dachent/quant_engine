#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Any


DATASETS = {
    "futures-records",
    "futures-wide",
    "futures-m1m2",
    "spot-records",
    "spot-wide",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch VIX futures/cash datasets through dougransom/vix_utils.")
    parser.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    parser.add_argument("--cache-dir", help="Optional workspace-local cache directory for vix_utils downloads.")
    return parser.parse_args()


def import_optional(module_name: str, package_hint: str):
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"{package_hint} is required for this helper.") from exc


def configure_cache_dir(cache_dir: Path) -> None:
    location_module = import_optional("vix_utils.location", "The optional 'vix_utils' package")
    cache_dir.mkdir(parents=True, exist_ok=True)

    override = getattr(location_module, "override_data_dir", None)
    if callable(override):
        try:
            override(cache_dir)
        except Exception:
            pass

    setattr(location_module, "__override_data_dir__", cache_dir)


def load_dataset(dataset: str, cache_dir: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        pandas = import_optional("pandas", "The optional 'pandas' dependency")
        vix_utils = import_optional("vix_utils", "The optional 'vix_utils' package")
        configure_cache_dir(cache_dir)
        logging.getLogger().setLevel(logging.ERROR)

        if dataset == "futures-records":
            return vix_utils.load_vix_term_structure()

        if dataset == "spot-records":
            return vix_utils.get_vix_index_histories()

        if dataset == "futures-wide":
            return vix_utils.pivot_futures_on_monthly_tenor(vix_utils.load_vix_term_structure())

        if dataset == "spot-wide":
            return vix_utils.pivot_spot_term_structure_on_symbol(vix_utils.get_vix_index_histories())

        if dataset == "futures-m1m2":
            continuous = import_optional("vix_utils.continuous_maturity", "The optional 'vix_utils' package")
            monthly_wide = vix_utils.pivot_futures_on_monthly_tenor(vix_utils.load_vix_term_structure())
            return continuous.continuous_maturity_one_month(monthly_wide)

    raise ValueError(f"Unsupported dataset: {dataset}")


def flatten_column_name(column: Any) -> str:
    if isinstance(column, tuple):
        parts = [str(part) for part in column if part not in ("", None)]
        return "|".join(parts) or "column"
    if column in ("", None):
        return "column"
    return str(column)


def serialize_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if isinstance(value, float) and value != value:
        return None
    return value


def dataframe_to_rows(frame) -> tuple[list[str], list[dict[str, Any]]]:
    pandas = import_optional("pandas", "The optional 'pandas' dependency")
    normalized = frame.copy()

    if not isinstance(normalized.index, pandas.RangeIndex):
        normalized = normalized.reset_index()

    if hasattr(normalized.columns, "to_flat_index"):
        normalized.columns = [flatten_column_name(column) for column in normalized.columns.to_flat_index()]
    else:
        normalized.columns = [flatten_column_name(column) for column in normalized.columns]

    rows = []
    for record in normalized.to_dict(orient="records"):
        rows.append({key: serialize_value(value) for key, value in record.items()})
    return list(normalized.columns), rows


def write_raw_frame(frame, output_path: Path) -> None:
    pandas = import_optional("pandas", "The optional 'pandas' dependency")
    include_index = not isinstance(frame.index, pandas.RangeIndex)
    frame.to_csv(output_path, index=include_index)


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else (Path.cwd() / "tmp" / "vix_utils-cache").resolve()

    try:
        frame = load_dataset(args.dataset, cache_dir)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if args.format == "raw":
            write_raw_frame(frame, output_path)
            print(f"Saved raw vix_utils dataset to {output_path}")
            print(f"dataset={args.dataset} cache_dir={cache_dir}")
            return 0

        columns, rows = dataframe_to_rows(frame)
        artifact = {
            "provider": "vix_utils",
            "dataset": args.dataset,
            "cache_dir": str(cache_dir),
            "columns": columns,
            "rows": rows,
        }
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed vix_utils artifact to {output_path}")
        print(f"dataset={args.dataset} rows={len(rows)} cache_dir={cache_dir}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
