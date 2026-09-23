"""Normalize public factor and industry feeds into deterministic return inputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


BUILDER_VERSION = "0.3.0"
FRESHNESS_POLICY_ID = "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1"
PUBLICATION_LAG_CALENDAR_DAYS = 45
DATE_PATTERN = re.compile(r"\d{8}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
)
MISSING_SENTINELS = {Decimal("-99.99"), Decimal("-999")}
FACTOR_RETURN_COLUMNS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA")
FACTOR_COLUMNS = (*FACTOR_RETURN_COLUMNS, "RF")
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
    manifest_sha256: str
    manifest_size_bytes: int
    source_run_id: str
    raw_name: str
    raw_sha256: str
    raw_size_bytes: int
    adapter: Mapping[str, object]
    response: Mapping[str, object]
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


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise PublicSourceError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0 or parsed != value:
        raise PublicSourceError(f"{name} must be a positive integer")
    return parsed


def _utc_datetime(value: object, name: str) -> datetime:
    timestamp = _text(value, name)
    if UTC_TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        raise PublicSourceError(f"{name} is not RFC3339 UTC")
    try:
        return datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise PublicSourceError(f"{name} is not RFC3339 UTC") from exc


def _date_value(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise PublicSourceError(f"{name} is not an ISO date") from exc
    if parsed.isoformat() != raw:
        raise PublicSourceError(f"{name} is not an ISO date")
    return parsed


def _month_end(value: date) -> date:
    next_month = (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def _required_observation_month_end(evaluated_on: date) -> date:
    candidate = _month_end(evaluated_on)
    while candidate + timedelta(days=PUBLICATION_LAG_CALENDAR_DAYS) > evaluated_on:
        candidate = _month_end(candidate.replace(day=1) - timedelta(days=1))
    return candidate


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
    key: str,
    path: Path,
    manifest_path: Path,
    catalog: Mapping[str, Mapping[str, Any]],
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
    manifest_value, manifest_data = _load_json(manifest_path)
    if not isinstance(manifest_value, dict):
        raise PublicSourceError(f"{key} source-run manifest must be an object")
    expected_manifest_keys = {
        "adapter",
        "artifact",
        "coverage",
        "raw_artifact",
        "request",
        "response",
        "schema_version",
        "source_run_id",
    }
    if (
        set(manifest_value) != expected_manifest_keys
        or manifest_value.get("schema_version") != 1
    ):
        raise PublicSourceError(f"{key} source-run manifest contract differs")
    adapter = manifest_value.get("adapter")
    if adapter != {
        "id": "agent-market-data-terminal/famafrench",
        "version": "1.2.0",
    }:
        raise PublicSourceError(f"{key} source adapter identity differs")
    request = manifest_value.get("request")
    if request != {
        "feed_id": feed_id,
        "format": "parsed",
        "provider": "famafrench",
        "source_url": source_url,
    }:
        raise PublicSourceError(f"{key} source-run request differs")
    artifact = manifest_value.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "name",
        "sha256",
        "size_bytes",
    }:
        raise PublicSourceError(f"{key} source-run artifact contract differs")
    if (
        artifact.get("name") != path.name
        or artifact.get("sha256") != _sha256(data)
        or artifact.get("size_bytes") != len(data)
    ):
        raise PublicSourceError(f"{key} source differs from its source-run manifest")
    raw_artifact = manifest_value.get("raw_artifact")
    if not isinstance(raw_artifact, dict) or set(raw_artifact) != {
        "name",
        "sha256",
        "size_bytes",
        "zip_members",
    }:
        raise PublicSourceError(f"{key} raw source identity contract differs")
    raw_name = _text(raw_artifact.get("name"), f"{key}.raw_artifact.name")
    if Path(raw_name).name != raw_name or "/" in raw_name or "\\" in raw_name:
        raise PublicSourceError(f"{key} raw source name must be a file name")
    raw_sha256 = _text(raw_artifact.get("sha256"), f"{key}.raw_artifact.sha256")
    if SHA256_PATTERN.fullmatch(raw_sha256) is None:
        raise PublicSourceError(f"{key} raw source hash is invalid")
    raw_size_bytes = _integer(
        raw_artifact.get("size_bytes"), f"{key}.raw_artifact.size_bytes"
    )
    raw_path = manifest_path.resolve().parent / raw_name
    if raw_path.is_symlink() or not raw_path.is_file():
        raise PublicSourceError(f"{key} retained raw source is missing")
    raw_data = raw_path.read_bytes()
    if len(raw_data) != raw_size_bytes or _sha256(raw_data) != raw_sha256:
        raise PublicSourceError(f"{key} retained raw source identity differs")
    declared_inventory = raw_artifact.get("zip_members")
    if not isinstance(declared_inventory, list):
        raise PublicSourceError(f"{key} ZIP member inventory is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(raw_data)) as archive:
            actual_inventory = [
                {
                    "name": member.filename,
                    "sha256": _sha256(archive.read(member)),
                    "size_bytes": member.file_size,
                }
                for member in sorted(archive.infolist(), key=lambda item: item.filename)
                if not member.is_dir()
            ]
    except zipfile.BadZipFile as exc:
        raise PublicSourceError(f"{key} retained raw source is not a ZIP") from exc
    if declared_inventory != actual_inventory:
        raise PublicSourceError(f"{key} ZIP member inventory differs")
    coverage = manifest_value.get("coverage")
    if (
        not isinstance(coverage, dict)
        or coverage.get("claim") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    ):
        raise PublicSourceError(f"{key} source-run coverage is not accepted")
    csv_member = _text(coverage.get("csv_member"), f"{key}.coverage.csv_member")
    csv_member_sha256 = _text(
        coverage.get("csv_member_sha256"), f"{key}.coverage.csv_member_sha256"
    )
    csv_member_size = _integer(
        coverage.get("csv_member_size_bytes"),
        f"{key}.coverage.csv_member_size_bytes",
    )
    selected_inventory = [
        item for item in actual_inventory if item.get("name") == csv_member
    ]
    if len(selected_inventory) != 1 or selected_inventory[0] != {
        "name": csv_member,
        "sha256": csv_member_sha256,
        "size_bytes": csv_member_size,
    }:
        raise PublicSourceError(f"{key} parsed CSV member identity differs")
    response = manifest_value.get("response")
    response_keys = {
        "captured_at_utc",
        "content_type",
        "etag",
        "final_url",
        "last_modified",
        "status",
    }
    if not isinstance(response, dict) or set(response) != response_keys:
        raise PublicSourceError(f"{key} HTTP response identity contract differs")
    captured_at = _text(response.get("captured_at_utc"), f"{key}.captured_at_utc")
    _utc_datetime(captured_at, f"{key} capture timestamp")
    content_type = _text(response.get("content_type"), f"{key}.content_type")
    final_url = _text(response.get("final_url"), f"{key}.final_url")
    status = _integer(response.get("status"), f"{key}.status")
    if status != 200 or final_url != source_url:
        raise PublicSourceError(f"{key} HTTP response identity is not accepted")
    for optional_header in ("etag", "last_modified"):
        header = response.get(optional_header)
        if header is not None and not isinstance(header, str):
            raise PublicSourceError(f"{key}.{optional_header} must be text or null")
    normalized_response: dict[str, object] = {
        "captured_at_utc": captured_at,
        "content_type": content_type,
        "etag": response.get("etag"),
        "final_url": final_url,
        "last_modified": response.get("last_modified"),
        "status": status,
    }
    source_run_id = _text(manifest_value.get("source_run_id"), f"{key}.source_run_id")
    expected_run_id = _sha256(
        _canonical_json(
            {
                "adapter": adapter,
                "raw_artifact": {
                    "sha256": raw_sha256,
                    "size_bytes": raw_size_bytes,
                    "zip_members": actual_inventory,
                },
                "request": request,
                "response": {
                    "content_type": content_type,
                    "etag": normalized_response["etag"],
                    "final_url": final_url,
                    "last_modified": normalized_response["last_modified"],
                    "status": status,
                },
            }
        )
    )
    if source_run_id != expected_run_id:
        raise PublicSourceError(f"{key} source-run identity differs")
    return SourceDocument(
        key=key,
        sha256=_sha256(data),
        size_bytes=len(data),
        manifest_sha256=_sha256(manifest_data),
        manifest_size_bytes=len(manifest_data),
        source_run_id=source_run_id,
        raw_name=raw_name,
        raw_sha256=raw_sha256,
        raw_size_bytes=raw_size_bytes,
        adapter=adapter,
        response=normalized_response,
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


def _admit_publication_boundary(
    name: str, data: bytes, eligible_month_end: date
) -> tuple[bytes, date, date, int, int]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if len(rows) < 2:
        raise PublicSourceError(f"{name} has no normalized observations")
    header, observations = rows[0], rows[1:]
    dated_rows = [
        (_date_value(row[0], f"{name} observation date"), row) for row in observations
    ]
    provider_available_end = dated_rows[-1][0]
    eligible_rows = [
        (day, row)
        for day, row in dated_rows
        if (day.year, day.month) == (eligible_month_end.year, eligible_month_end.month)
        and (name == OUTPUT_NAMES[2] or all(value != "" for value in row[1:]))
    ]
    if not eligible_rows:
        raise PublicSourceError(
            f"{name} REQUIRED_PUBLICATION_MONTH_ABSENT: {eligible_month_end:%Y-%m}"
        )
    accepted_observation_end = max(day for day, _row in eligible_rows)
    admitted_rows = [row for day, row in dated_rows if day <= accepted_observation_end]
    missing_cells = sum(value == "" for row in admitted_rows for value in row[1:])
    return (
        _csv_bytes(header, admitted_rows),
        provider_available_end,
        accepted_observation_end,
        len(admitted_rows),
        missing_cells,
    )


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
        risk_free = _decimal(factor["RF"], f"{factors.key}.{period}.RF")
        if risk_free is None:
            raise PublicSourceError(f"{factors.key}.{period}.RF cannot be missing")
        values: list[Decimal | None] = [risk_free]
        values.extend(
            _decimal(factor[name], f"{factors.key}.{period}.{name}")
            for name in FACTOR_RETURN_COLUMNS
        )
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
        ("date", "rf", "mkt_rf", "smb", "hml", "rmw", "cma", "mom"),
        output_rows,
    ), {
        "coverage": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        "declared_boundary": "EXACT_DATE_INTERSECTION",
        "first_date": output_rows[0][0],
        "last_date": output_rows[-1][0],
        "row_count": len(output_rows),
        "factor_only_dates_dropped": len(set(factor_rows) - set(momentum_rows)),
        "momentum_only_dates_dropped": len(set(momentum_rows) - set(factor_rows)),
        "missing_cells": missing_cells,
        "unit": "DECIMAL_DAILY_RETURN",
    }


def _industry_output(
    source: SourceDocument, risk_free_source: SourceDocument
) -> tuple[bytes, dict[str, object]]:
    columns, rows = _rows_by_date(source, INDUSTRY_TITLE)
    _, factor_rows = _rows_by_date(
        risk_free_source, "Daily", ("period", *FACTOR_COLUMNS)
    )
    industries = columns[1:]
    if len(industries) != 49:
        raise PublicSourceError(
            f"industry source must contain 49 portfolios, found {len(industries)}"
        )
    output_rows: list[list[str]] = []
    missing_cells = 0
    common_dates = sorted(set(rows) & set(factor_rows))
    if not common_dates:
        raise PublicSourceError(
            "industry and US risk-free sources have no common dates"
        )
    for period in common_dates:
        row = rows[period]
        risk_free = _decimal(
            factor_rows[period]["RF"], f"{risk_free_source.key}.{period}.RF"
        )
        if risk_free is None:
            raise PublicSourceError(
                f"{risk_free_source.key}.{period}.RF cannot be missing"
            )
        values = [
            _decimal(row[name], f"{source.key}.{period}.{name}") for name in industries
        ]
        missing_cells += sum(value is None for value in values)
        output_rows.append(
            [
                f"{period[0:4]}-{period[4:6]}-{period[6:8]}",
                _format_decimal(risk_free),
                *(_format_decimal(value) for value in values),
            ]
        )
    return _csv_bytes(("date", "rf", *industries), output_rows), {
        "coverage": "PARTIAL",
        "declared_boundary": "EXACT_DATE_INTERSECTION",
        "first_date": output_rows[0][0],
        "last_date": output_rows[-1][0],
        "row_count": len(output_rows),
        "industry_count": len(industries),
        "industry_only_dates_dropped": len(set(rows) - set(factor_rows)),
        "risk_free_only_dates_dropped": len(set(factor_rows) - set(rows)),
        "missing_cells": missing_cells,
        "unit": "DECIMAL_DAILY_RETURN",
        "weighting": "VALUE_WEIGHTED",
    }


def build_bundle(
    catalog_path: Path,
    source_paths: Mapping[str, Path],
    source_manifest_paths: Mapping[str, Path],
    run_started_at_utc: str,
    run_ended_at_utc: str,
    evaluation_date: str,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Build normalized outputs and their canonical manifest without writing."""
    if set(source_paths) != set(SOURCE_KEYS):
        raise PublicSourceError(f"source keys must be exactly {sorted(SOURCE_KEYS)}")
    if set(source_manifest_paths) != set(SOURCE_KEYS):
        raise PublicSourceError(
            f"source manifest keys must be exactly {sorted(SOURCE_KEYS)}"
        )
    catalog, catalog_hash, catalog_size = _load_catalog(catalog_path)
    sources = {
        key: _load_source(key, source_paths[key], source_manifest_paths[key], catalog)
        for key in SOURCE_KEYS
    }
    run_started_at = _utc_datetime(run_started_at_utc, "run_started_at_utc")
    run_ended_at = _utc_datetime(run_ended_at_utc, "run_ended_at_utc")
    evaluated_on = _date_value(evaluation_date, "evaluation_date")
    if run_ended_at < run_started_at:
        raise PublicSourceError("run_ended_at_utc precedes run_started_at_utc")
    capture_times = {
        key: _utc_datetime(
            source.response.get("captured_at_utc"), f"{key} captured_at_utc"
        )
        for key, source in sources.items()
    }
    outside_run = [
        key
        for key, captured_at in capture_times.items()
        if captured_at < run_started_at or captured_at > run_ended_at
    ]
    if outside_run:
        raise PublicSourceError(
            "source receipts are outside the current run window: "
            + ", ".join(sorted(outside_run))
        )
    us_data, us_coverage = _factor_output(
        sources["us_factors"], sources["us_momentum"], "Mom"
    )
    europe_data, europe_coverage = _factor_output(
        sources["europe_factors"], sources["europe_momentum"], "WML"
    )
    industry_data, industry_coverage = _industry_output(
        sources["us_industries"], sources["us_factors"]
    )
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
    required_month_end = _required_observation_month_end(evaluated_on)
    publication_deadline = required_month_end + timedelta(
        days=PUBLICATION_LAG_CALENDAR_DAYS
    )
    freshness: dict[str, dict[str, object]] = {}
    for name in OUTPUT_NAMES:
        (
            outputs[name],
            provider_available_end,
            accepted_observation_end,
            admitted_row_count,
            admitted_missing_cells,
        ) = _admit_publication_boundary(name, outputs[name], required_month_end)
        coverage[name]["last_date"] = accepted_observation_end.isoformat()
        coverage[name]["row_count"] = admitted_row_count
        coverage[name]["missing_cells"] = admitted_missing_cells
        freshness[name] = {
            "state": "CURRENT",
            "eligible_month": required_month_end.strftime("%Y-%m"),
            "eligible_month_end": required_month_end.isoformat(),
            "publication_deadline": publication_deadline.isoformat(),
            "provider_available_end": provider_available_end.isoformat(),
            "accepted_observation_end": accepted_observation_end.isoformat(),
        }
    receipt_manifest_sha256s = {
        key: source.manifest_sha256 for key, source in sources.items()
    }
    acquisition_run_payload = {
        "evaluation_date": evaluated_on.isoformat(),
        "run_started_at_utc": run_started_at_utc,
        "run_ended_at_utc": run_ended_at_utc,
        "receipt_manifest_sha256s": receipt_manifest_sha256s,
    }
    acquisition_run_id = _sha256(_canonical_json(acquisition_run_payload))
    manifest: dict[str, object] = {
        "schema_version": 2,
        "builder": "portfolio-public-decomposition-sources",
        "builder_version": BUILDER_VERSION,
        "catalog": {
            "sha256": catalog_hash,
            "bytes": catalog_size,
        },
        "acquisition_run": {
            "run_id": acquisition_run_id,
            "evaluation_date": evaluated_on.isoformat(),
            "run_started_at_utc": run_started_at_utc,
            "run_ended_at_utc": run_ended_at_utc,
            "acquisition_requirement": "EVERY_SOURCE_CAPTURED_WITHIN_CURRENT_RUN",
            "capture_window_first_utc": min(capture_times.values())
            .isoformat()
            .replace("+00:00", "Z"),
            "capture_window_last_utc": max(capture_times.values())
            .isoformat()
            .replace("+00:00", "Z"),
            "receipt_manifest_sha256s": receipt_manifest_sha256s,
        },
        "freshness_policy": {
            "policy_id": FRESHNESS_POLICY_ID,
            "observation_cadence": "DAILY",
            "publication_cadence": "MONTHLY",
            "deadline_anchor": "CALENDAR_MONTH_END",
            "publication_lag_days": PUBLICATION_LAG_CALENDAR_DAYS,
            "calendar": "CALENDAR_DAY",
            "deadline_inclusive": True,
            "boundary_rule": "LATEST_PROVIDER_VALID_DAY_IN_ELIGIBLE_MONTH",
            "receipt_rule": "CAPTURED_WITHIN_CURRENT_RUN",
        },
        "sources": [
            {
                "source_key": source.key,
                "feed_id": source.feed_id,
                "source_url": source.source_url,
                "sha256": source.sha256,
                "bytes": source.size_bytes,
                "source_run_id": source.source_run_id,
                "source_manifest_sha256": source.manifest_sha256,
                "source_manifest_bytes": source.manifest_size_bytes,
                "raw_sha256": source.raw_sha256,
                "raw_bytes": source.raw_size_bytes,
                "raw_name": source.raw_name,
                "adapter": dict(source.adapter),
                "response": dict(source.response),
            }
            for source in sources.values()
        ],
        "outputs": [
            {
                "path": name,
                "sha256": _sha256(outputs[name]),
                "bytes": len(outputs[name]),
                **coverage[name],
                "freshness": freshness[name],
            }
            for name in OUTPUT_NAMES
        ],
        "policies": {
            "date_join": "EXACT_INTERSECTION_NO_FILL",
            "missing_observation": "EMPTY_CELL_NEVER_ZERO",
            "provider_percent_conversion": "DIVIDE_BY_100",
            "industry_lens": "RETURN_BASED_INFERENTIAL_NOT_HOLDINGS_AUTHORITY",
            "freshness": FRESHNESS_POLICY_ID,
        },
    }
    manifest["bundle_id"] = _sha256(_canonical_json(manifest))
    return outputs, manifest


def write_bundle(
    catalog_path: Path,
    source_paths: Mapping[str, Path],
    source_manifest_paths: Mapping[str, Path],
    output_root: Path,
    run_started_at_utc: str,
    run_ended_at_utc: str,
    evaluation_date: str,
) -> dict[str, object]:
    """Write a new immutable bundle; existing output paths are never replaced."""
    outputs, manifest = build_bundle(
        catalog_path,
        source_paths,
        source_manifest_paths,
        run_started_at_utc,
        run_ended_at_utc,
        evaluation_date,
    )
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
        parser.add_argument(
            f"--{key.replace('_', '-')}-manifest", type=Path, required=True
        )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-started-at-utc", required=True)
    parser.add_argument("--run-ended-at-utc", required=True)
    parser.add_argument("--evaluation-date", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_paths = {key: getattr(args, key) for key in SOURCE_KEYS}
    source_manifest_paths = {
        key: getattr(args, f"{key}_manifest") for key in SOURCE_KEYS
    }
    result = write_bundle(
        args.catalog,
        source_paths,
        source_manifest_paths,
        args.output_root,
        args.run_started_at_utc,
        args.run_ended_at_utc,
        args.evaluation_date,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
