#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://stats.oecd.org/SDMX-JSON/data"
USER_AGENT = "Codex agent-market-data-terminal/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch no-auth OECD SDMX-JSON datasets.")
    parser.add_argument("--dataset", required=True, help="OECD dataset id such as MEI_CLI.")
    parser.add_argument("--series-key", required=True, help="OECD series key such as LOLITONO.USA.M.")
    parser.add_argument("--start-time", help="Optional startTime such as 2000-01.")
    parser.add_argument("--end-time", help="Optional endTime such as 2024-12.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def build_url(dataset: str, series_key: str, start_time: str | None, end_time: str | None) -> str:
    params: list[tuple[str, str]] = []
    if start_time:
        params.append(("startTime", start_time))
    if end_time:
        params.append(("endTime", end_time))
    query = urlencode(params)
    suffix = f"?{query}" if query else ""
    return f"{BASE_URL}/{dataset}/{series_key}/all{suffix}"


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def extract_dimension_value(dimension: dict[str, object], index_value: int) -> tuple[object, object]:
    values = dimension.get("values", [])
    if not isinstance(values, list) or index_value >= len(values):
        return index_value, None
    selected = values[index_value]
    if not isinstance(selected, dict):
        return selected, None
    return selected.get("id"), selected.get("name")


def extract_attribute_value(attribute: dict[str, object], index_value: int | None) -> object:
    if index_value is None:
        return None
    values = attribute.get("values", [])
    if not isinstance(values, list) or index_value >= len(values):
        return index_value
    selected = values[index_value]
    if not isinstance(selected, dict):
        return selected
    if selected.get("name") not in (None, ""):
        return selected.get("name")
    return selected.get("id")


def split_index_key(key: str, expected_length: int) -> list[int | None]:
    if expected_length == 0:
        return []
    parts = key.split(":")
    result: list[int | None] = []
    for index in range(expected_length):
        if index >= len(parts) or parts[index] == "":
            result.append(None)
            continue
        result.append(int(parts[index]))
    return result


def flatten_payload(dataset: str, series_key: str, request_url: str, payload: dict[str, object]) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("OECD payload did not include the expected 'data' object.")

    data_sets = data.get("dataSets", [])
    structures = data.get("structures", [])
    if not isinstance(data_sets, list) or not data_sets or not isinstance(structures, list) or not structures:
        raise RuntimeError("OECD payload did not include the expected SDMX structures.")

    structure = structures[0]
    if not isinstance(structure, dict):
        raise RuntimeError("OECD payload returned an unexpected structure definition.")

    dimensions = structure.get("dimensions", {})
    attributes = structure.get("attributes", {})
    series_dimensions = dimensions.get("series", []) if isinstance(dimensions, dict) else []
    observation_dimensions = dimensions.get("observation", []) if isinstance(dimensions, dict) else []
    series_attributes = attributes.get("series", []) if isinstance(attributes, dict) else []
    observation_attributes = attributes.get("observation", []) if isinstance(attributes, dict) else []

    data_set = data_sets[0]
    if not isinstance(data_set, dict):
        raise RuntimeError("OECD payload returned an unexpected data set object.")
    series_map = data_set.get("series", {})
    if not isinstance(series_map, dict):
        raise RuntimeError("OECD payload returned an unexpected series map.")

    rows: list[dict[str, object]] = []
    for series_index_key, series_payload in series_map.items():
        if not isinstance(series_payload, dict):
            continue
        series_indexes = split_index_key(str(series_index_key), len(series_dimensions))
        base_row: dict[str, object] = {}

        for dimension, index_value in zip(series_dimensions, series_indexes):
            if not isinstance(dimension, dict):
                continue
            dim_id = str(dimension.get("id"))
            raw_value, label = extract_dimension_value(dimension, index_value or 0) if index_value is not None else (None, None)
            base_row[dim_id] = raw_value
            if label not in (None, "", raw_value):
                base_row[f"{dim_id}_label"] = label

        series_attribute_indexes = series_payload.get("attributes", [])
        if isinstance(series_attribute_indexes, list):
            for attribute, index_value in zip(series_attributes, series_attribute_indexes):
                if not isinstance(attribute, dict):
                    continue
                base_row[str(attribute.get("id"))] = extract_attribute_value(attribute, index_value)

        observations = series_payload.get("observations", {})
        if not isinstance(observations, dict):
            continue

        for observation_index_key, observation_payload in observations.items():
            observation_indexes = split_index_key(str(observation_index_key), len(observation_dimensions))
            row = dict(base_row)
            if isinstance(observation_payload, list) and observation_payload:
                row["value"] = observation_payload[0]
                observation_attribute_indexes = observation_payload[1:]
            else:
                row["value"] = observation_payload
                observation_attribute_indexes = []

            for dimension, index_value in zip(observation_dimensions, observation_indexes):
                if not isinstance(dimension, dict):
                    continue
                dim_id = str(dimension.get("id"))
                raw_value, label = extract_dimension_value(dimension, index_value or 0) if index_value is not None else (None, None)
                row[dim_id] = raw_value
                if label not in (None, "", raw_value):
                    row[f"{dim_id}_label"] = label

            for attribute, index_value in zip(observation_attributes, observation_attribute_indexes):
                if not isinstance(attribute, dict):
                    continue
                row[str(attribute.get("id"))] = extract_attribute_value(attribute, index_value)

            rows.append(row)

    return {
        "provider": "oecd",
        "dataset": dataset,
        "series_key": series_key,
        "request_url": request_url,
        "meta": payload.get("meta"),
        "errors": payload.get("errors"),
        "rows": rows,
    }


def main() -> int:
    args = parse_args()
    request_url = build_url(args.dataset, args.series_key, args.start_time, args.end_time)
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        raw_bytes = fetch_bytes(request_url)
        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw OECD payload to {output_path}")
            print(f"URL: {request_url}")
            return 0

        payload = json.loads(raw_bytes.decode("utf-8"))
        artifact = flatten_payload(args.dataset, args.series_key, request_url, payload)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed OECD artifact to {output_path}")
        print(f"rows={len(artifact['rows'])}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
