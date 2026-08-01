#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data"
USER_AGENT = "Codex agent-market-data-terminal/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch public BLS v1 time series data without registration.")
    parser.add_argument("--series-id", dest="series_ids", action="append", required=True, help="BLS series ID. Repeat for multiple series.")
    parser.add_argument("--start-year", help="Optional start year for bounded requests.")
    parser.add_argument("--end-year", help="Optional end year for bounded requests.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def normalize_year(label: str, value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if len(cleaned) != 4 or not cleaned.isdigit():
        raise ValueError(f"{label} must be formatted as YYYY.")
    return cleaned


def request_bytes(method: str, url: str, *, body: bytes | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=60) as response:
        return response.read()


def build_request(series_ids: list[str], start_year: str | None, end_year: str | None) -> tuple[str, str, bytes | None]:
    if len(series_ids) == 1 and start_year is None and end_year is None:
        return "GET", f"{BASE_URL}/{series_ids[0]}", None

    payload: dict[str, object] = {"seriesid": series_ids}
    if start_year is not None:
        payload["startyear"] = start_year
    if end_year is not None:
        payload["endyear"] = end_year
    return "POST", f"{BASE_URL}/", json.dumps(payload).encode("utf-8")


def normalize_footnotes(items: list[dict[str, object]] | None) -> list[dict[str, object]]:
    if not items:
        return []
    cleaned: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = {key: value for key, value in item.items() if value not in ("", None)}
        if normalized:
            cleaned.append(normalized)
    return cleaned


def parse_payload(payload: dict[str, object], method: str, request_url: str) -> dict[str, object]:
    results = payload.get("Results", {})
    series_items = results.get("series", []) if isinstance(results, dict) else []
    parsed_series: list[dict[str, object]] = []

    for series in series_items:
        if not isinstance(series, dict):
            continue
        observations: list[dict[str, object]] = []
        for item in series.get("data", []):
            if not isinstance(item, dict):
                continue
            observation = {
                "year": item.get("year"),
                "period": item.get("period"),
                "periodName": item.get("periodName"),
                "value": item.get("value"),
                "footnotes": normalize_footnotes(item.get("footnotes")),
            }
            if "latest" in item:
                observation["latest"] = item.get("latest")
            observations.append(observation)

        parsed_series.append(
            {
                "series_id": series.get("seriesID"),
                "observations": observations,
            }
        )

    return {
        "provider": "bls",
        "api_version": "v1",
        "request_method": method,
        "request_url": request_url,
        "status": payload.get("status"),
        "response_time": payload.get("responseTime"),
        "messages": payload.get("message"),
        "series": parsed_series,
    }


def main() -> int:
    args = parse_args()
    start_year = normalize_year("--start-year", args.start_year)
    end_year = normalize_year("--end-year", args.end_year)
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        method, request_url, body = build_request(args.series_ids, start_year, end_year)
        raw_bytes = request_bytes(method, request_url, body=body)

        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw BLS payload to {output_path}")
            print(f"URL: {request_url}")
            return 0

        payload = json.loads(raw_bytes.decode("utf-8"))
        artifact = parse_payload(payload, method, request_url)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed BLS artifact to {output_path}")
        print(f"series={len(artifact['series'])}")
        return 0
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
