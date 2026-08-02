"""Normalize public factor and industry feeds into deterministic return inputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


BUILDER_VERSION = "0.1.0"
DATE_PATTERN = re.compile(r"\d{8}")
MISSING_SENTINELS = {Decimal("-99.99"), Decimal("-999")}
FACTOR_COLUMNS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF")
INDUSTRY_TITLE = "Average Value Weighted Returns -- Daily"
OUTPUT_NAMES = (
    "us_factors_daily.csv",
    "europe_factors_daily.csv",
    "us_49_industries_value_weighted_daily.csv",
)
SOURCE_KEYS = (
    "us_factors",
    "us_momentum",
    "europe_factors",
    "europe_momentum",
    "us_industries",
)
EXPECTED_FEEDS = {
    "us_factors": "F-F_Research_Data_5_Factors_2x3_daily_CSV",
    "us_momentum": "F-F_Momentum_Factor_daily_CSV",
    "europe_factors": "Europe_5_Factors_Daily_CSV",
    "europe_momentum": "Europe_Mom_Factor_Daily_CSV",
    "us_industries": "49_Industry_Portfolios_daily_CSV",
}


class PublicSourceError(ValueError):
    """Raised when a public source cannot be normalized without ambiguity."""


@dataclass(frozen=True)
class SourceDocument:
    key: str
    sha256: str
    size_bytes: int
    feed_id: str
    source_url: str
    sections: tuple[Mapping[str, Any], ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_json(path: Path) -> tuple[object, bytes]:
    data = path.read_bytes()
    try:
        return json.loads(data, parse_float=Decimal, parse_int=Decimal), data
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PublicSourceError(f"invalid JSON source {path}: {exc}") from exc


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicSourceError(f"{name} must be non-empty text")
    return value.strip()


def _load_catalog(path: Path) -> tuple[dict[str, Mapping[str, Any]], str, int]:
    value, data = _load_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise PublicSourceError("source catalog must contain a sources list")
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in value["sources"]:
        if not isinstance(item, dict):
            raise PublicSourceError("source catalog entries must be objects")
        feed_id = _text(item.get("feed_id"), "catalog feed_id")
        if feed_id in indexed:
            raise PublicSourceError(f"duplicate catalog feed_id {feed_id}")
        indexed[feed_id] = item
    return indexed, _sha256(data), len(data)


def _load_source(
    key: str, path: Path, catalog: Mapping[str, Mapping[str, Any]]
) -> SourceDocument:
    value, data = _load_json(path)
    if not isinstance(value, dict):
        raise PublicSourceError(f"{key} source must be a JSON object")
    provider = _text(value.get("provider"), f"{key}.provider")
    if provider != "famafrench":
        raise PublicSourceError(f"{key}.provider must be famafrench")
    feed_id = _text(value.get("feed_id"), f"{key}.feed_id")
    expected = EXPECTED_FEEDS[key]
    if feed_id != expected:
        raise PublicSourceError(f"{key}.feed_id must be {expected}, found {feed_id}")
    catalog_item = catalog.get(feed_id)
    if catalog_item is None:
        raise PublicSourceError(f"feed {feed_id} is absent from the source catalog")
    source_url = _text(value.get("source_url"), f"{key}.source_url")
    if source_url != _text(catalog_item.get("source_url"), "catalog source_url"):
        raise PublicSourceError(f"{key}.source_url does not match the source catalog")
    sections = value.get("sections")
    if not isinstance(sections, list) or not sections:
        raise PublicSourceError(f"{key}.sections must be a non-empty list")
    if not all(isinstance(item, dict) for item in sections):
        raise PublicSourceError(f"{key}.sections must contain objects")
    return SourceDocument(
        key=key,
        sha256=_sha256(data),
        size_bytes=len(data),
        feed_id=feed_id,
        source_url=source_url,
        sections=tuple(sections),
    )


def _section(source: SourceDocument, title: str) -> Mapping[str, Any]:
    matches = [item for item in source.sections if item.get("title") == title]
    if len(matches) != 1:
        raise PublicSourceError(
            f"{source.key} must contain exactly one {title!r} section"
        )
    return matches[0]


def _decimal(value: object, context: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PublicSourceError(f"{context} must be numeric or null")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PublicSourceError(f"{context} must be numeric or null") from exc
    if not result.is_finite():
        raise PublicSourceError(f"{context} must be finite")
    if result in MISSING_SENTINELS:
        return None
    return result / Decimal(100)


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f").rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _rows_by_date(
    source: SourceDocument,
    title: str,
    expected_columns: Sequence[str] | None = None,
) -> tuple[tuple[str, ...], dict[str, Mapping[str, object]]]:
    section = _section(source, title)
    columns = section.get("columns")
    rows = section.get("rows")
    if not isinstance(columns, list) or not all(isinstance(x, str) for x in columns):
        raise PublicSourceError(f"{source.key}.{title}.columns must be text")
    column_tuple = tuple(columns)
    if expected_columns is not None and column_tuple != tuple(expected_columns):
        raise PublicSourceError(
            f"{source.key}.{title}.columns mismatch: {column_tuple!r}"
        )
    if not isinstance(rows, list):
        raise PublicSourceError(f"{source.key}.{title}.rows must be a list")
    indexed: dict[str, Mapping[str, object]] = {}
    expected_keys = set(column_tuple)
    for offset, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise PublicSourceError(f"{source.key} row {offset} has an invalid schema")
        period = _text(row.get("period"), f"{source.key} row {offset} period")
        if DATE_PATTERN.fullmatch(period) is None:
            raise PublicSourceError(
                f"{source.key} row {offset} has invalid date {period}"
            )
        if period in indexed:
            raise PublicSourceError(f"{source.key} has duplicate date {period}")
        indexed[period] = row
    if not indexed:
        raise PublicSourceError(f"{source.key} contains no rows")
    return column_tuple, indexed


def _csv_bytes(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _factor_output(
    factors: SourceDocument,
    momentum: SourceDocument,
    momentum_column: str,
) -> tuple[bytes, dict[str, object]]:
    _, factor_rows = _rows_by_date(factors, "Daily", ("period", *FACTOR_COLUMNS))
    _, momentum_rows = _rows_by_date(momentum, "Daily", ("period", momentum_column))
    common_dates = sorted(set(factor_rows) & set(momentum_rows))
    if not common_dates:
        raise PublicSourceError(
            f"{factors.key} and {momentum.key} have no common dates"
        )
    output_rows: list[list[str]] = []
    missing_cells = 0
    for period in common_dates:
        factor = factor_rows[period]
        mom = momentum_rows[period]
        values = [
            _decimal(factor[name], f"{factors.key}.{period}.{name}")
            for name in FACTOR_COLUMNS
        ]
        values.append(
            _decimal(mom[momentum_column], f"{momentum.key}.{period}.momentum")
        )
        missing_cells += sum(value is None for value in values)
        output_rows.append(
            [
                f"{period[0:4]}-{period[4:6]}-{period[6:8]}",
                *(_format_decimal(value) for value in values),
            ]
        )
    return _csv_bytes(
        ("date", "mkt_rf", "smb", "hml", "rmw", "cma", "rf", "mom"),
        output_rows,
    ), {
        "coverage": "COMPLETE_WITHIN_EXACT_DATE_INTERSECTION",
        "first_date": output_rows[0][0],
        "last_date": output_rows[-1][0],
        "row_count": len(output_rows),
        "factor_only_dates_dropped": len(set(factor_rows) - set(momentum_rows)),
        "momentum_only_dates_dropped": len(set(momentum_rows) - set(factor_rows)),
        "missing_cells": missing_cells,
        "unit": "DECIMAL_DAILY_RETURN",
    }


def _industry_output(source: SourceDocument) -> tuple[bytes, dict[str, object]]:
    columns, rows = _rows_by_date(source, INDUSTRY_TITLE)
    industries = columns[1:]
    if len(industries) != 49:
        raise PublicSourceError(
            f"industry source must contain 49 portfolios, found {len(industries)}"
        )
    output_rows: list[list[str]] = []
    missing_cells = 0
    for period in sorted(rows):
        row = rows[period]
        values = [
            _decimal(row[name], f"{source.key}.{period}.{name}") for name in industries
        ]
        missing_cells += sum(value is None for value in values)
        output_rows.append(
            [
                f"{period[0:4]}-{period[4:6]}-{period[6:8]}",
                *(_format_decimal(value) for value in values),
            ]
        )
    return _csv_bytes(("date", *industries), output_rows), {
        "coverage": "PARTIAL_WHERE_PROVIDER_REPORTS_MISSING_PORTFOLIO_RETURN",
        "first_date": output_rows[0][0],
        "last_date": output_rows[-1][0],
        "row_count": len(output_rows),
        "industry_count": len(industries),
        "missing_cells": missing_cells,
        "unit": "DECIMAL_DAILY_RETURN",
        "weighting": "VALUE_WEIGHTED",
    }


def build_bundle(
    catalog_path: Path, source_paths: Mapping[str, Path]
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Build normalized outputs and their canonical manifest without writing."""
    if set(source_paths) != set(SOURCE_KEYS):
        raise PublicSourceError(f"source keys must be exactly {sorted(SOURCE_KEYS)}")
    catalog, catalog_hash, catalog_size = _load_catalog(catalog_path)
    sources = {
        key: _load_source(key, source_paths[key], catalog) for key in SOURCE_KEYS
    }
    us_data, us_coverage = _factor_output(
        sources["us_factors"], sources["us_momentum"], "Mom"
    )
    europe_data, europe_coverage = _factor_output(
        sources["europe_factors"], sources["europe_momentum"], "WML"
    )
    industry_data, industry_coverage = _industry_output(sources["us_industries"])
    outputs = {
        OUTPUT_NAMES[0]: us_data,
        OUTPUT_NAMES[1]: europe_data,
        OUTPUT_NAMES[2]: industry_data,
    }
    coverage = {
        OUTPUT_NAMES[0]: us_coverage,
        OUTPUT_NAMES[1]: europe_coverage,
        OUTPUT_NAMES[2]: industry_coverage,
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "builder": "portfolio-public-decomposition-sources",
        "builder_version": BUILDER_VERSION,
        "catalog": {
            "sha256": catalog_hash,
            "bytes": catalog_size,
        },
        "sources": [
            {
                "source_key": source.key,
                "feed_id": source.feed_id,
                "source_url": source.source_url,
                "sha256": source.sha256,
                "bytes": source.size_bytes,
            }
            for source in sources.values()
        ],
        "outputs": [
            {
                "path": name,
                "sha256": _sha256(outputs[name]),
                "bytes": len(outputs[name]),
                **coverage[name],
            }
            for name in OUTPUT_NAMES
        ],
        "policies": {
            "date_join": "EXACT_INTERSECTION_NO_FILL",
            "missing_observation": "EMPTY_CELL_NEVER_ZERO",
            "provider_percent_conversion": "DIVIDE_BY_100",
            "industry_lens": "RETURN_BASED_INFERENTIAL_NOT_HOLDINGS_AUTHORITY",
        },
    }
    manifest["bundle_id"] = _sha256(_canonical_json(manifest))
    return outputs, manifest


def write_bundle(
    catalog_path: Path, source_paths: Mapping[str, Path], output_root: Path
) -> dict[str, object]:
    """Write a new immutable bundle; existing output paths are never replaced."""
    outputs, manifest = build_bundle(catalog_path, source_paths)
    bundle_id = _text(manifest["bundle_id"], "bundle_id")
    destination = output_root.resolve() / bundle_id
    if destination.exists():
        raise PublicSourceError(f"bundle already exists: {destination}")
    destination.mkdir(parents=True)
    try:
        for name in OUTPUT_NAMES:
            (destination / name).write_bytes(outputs[name])
        (destination / "source-manifest.json").write_bytes(_canonical_json(manifest))
    except Exception:
        for path in sorted(destination.iterdir(), reverse=True):
            path.unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {
        "bundle_id": bundle_id,
        "destination": str(destination),
        "manifest": manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    for key in SOURCE_KEYS:
        parser.add_argument(f"--{key.replace('_', '-')}", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_paths = {key: getattr(args, key) for key in SOURCE_KEYS}
    result = write_bundle(args.catalog, source_paths, args.output_root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
