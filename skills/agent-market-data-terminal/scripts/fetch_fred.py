#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
VALID_TRANSFORMATIONS = {"lin", "pch", "pc1", "log"}
VALID_FREQUENCIES = {"Monthly", "Quarterly", "Annual"}
VALID_AGGREGATIONS = {"avg", "sum", "eop"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download a FRED series using the clean CSV endpoint.")
    parser.add_argument("--series-id", required=True, help="FRED series id, for example GS10 or VIXCLS.")
    parser.add_argument("--start", required=True, help="Start date in YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="End date in YYYY-MM-DD.")
    parser.add_argument("--transformation", choices=sorted(VALID_TRANSFORMATIONS))
    parser.add_argument("--fq", choices=sorted(VALID_FREQUENCIES), help="Frequency override.")
    parser.add_argument("--fam", choices=sorted(VALID_AGGREGATIONS), help="Aggregation method when fq is set.")
    parser.add_argument("--vintage-date", help="Optional vintage date in YYYY-MM-DD.")
    parser.add_argument("--output", required=True, help="Output CSV path.")
    return parser.parse_args()


def validate_iso_date(value: str, label: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be in YYYY-MM-DD format.") from exc
    return value


def build_url(args: argparse.Namespace) -> str:
    params: dict[str, str] = {
        "id": args.series_id.strip(),
        "cosd": validate_iso_date(args.start, "start"),
        "coed": validate_iso_date(args.end, "end"),
    }

    if args.transformation and args.transformation != "lin":
        params["transformation"] = args.transformation
    if args.fq:
        params["fq"] = args.fq
        params["fam"] = args.fam or "avg"
    elif args.fam:
        raise ValueError("--fam requires --fq.")
    if args.vintage_date:
        params["vintage_date"] = validate_iso_date(args.vintage_date, "vintage-date")

    return f"{BASE_URL}?{urlencode(params)}"


def fetch_csv(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Codex agent-market-data-terminal/1.0"})
    with urlopen(request, timeout=60) as response:
        payload = response.read()
    if payload.lstrip().startswith(b"<"):
        raise RuntimeError("FRED returned HTML instead of CSV. Check the series id and parameters.")
    return payload


def main() -> int:
    args = parse_args()
    try:
        url = build_url(args)
        payload = fetch_csv(url)
    except (HTTPError, URLError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)

    row_count = max(payload.decode("utf-8", errors="replace").count("\n") - 1, 0)
    print(f"Saved {row_count} data rows to {output_path}")
    print(f"URL: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
