#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2"
USER_AGENT = "Codex agent-market-data-terminal/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch no-auth Treasury FiscalData JSON endpoints.")
    parser.add_argument("--endpoint", required=True, help="FiscalData endpoint path such as /accounting/od/avg_interest_rates.")
    parser.add_argument("--fields", help="Comma-separated FiscalData fields.")
    parser.add_argument("--filter", dest="filter_value", help="FiscalData filter expression.")
    parser.add_argument("--sort", help="FiscalData sort expression.")
    parser.add_argument("--page-size", type=int, help="Optional page[size] value.")
    parser.add_argument("--page-number", type=int, help="Optional page[number] value.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def normalize_endpoint(endpoint: str) -> str:
    cleaned = endpoint.strip()
    if not cleaned:
        raise ValueError("--endpoint may not be empty.")
    return "/" + cleaned.lstrip("/")


def build_request_url(args: argparse.Namespace) -> str:
    endpoint = normalize_endpoint(args.endpoint)
    query_items: list[tuple[str, str]] = []
    if args.fields:
        query_items.append(("fields", args.fields))
    if args.filter_value:
        query_items.append(("filter", args.filter_value))
    if args.sort:
        query_items.append(("sort", args.sort))
    if args.page_size is not None:
        query_items.append(("page[size]", str(args.page_size)))
    if args.page_number is not None:
        query_items.append(("page[number]", str(args.page_number)))

    query_string = urlencode(query_items)
    return f"{BASE_URL}{endpoint}" + (f"?{query_string}" if query_string else "")


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    args = parse_args()
    request_url = build_request_url(args)
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        raw_bytes = fetch_bytes(request_url)
        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw FiscalData payload to {output_path}")
            print(f"URL: {request_url}")
            return 0

        payload = json.loads(raw_bytes.decode("utf-8"))
        artifact = {
            "provider": "fiscaldata",
            "endpoint": normalize_endpoint(args.endpoint),
            "request_url": request_url,
            "data": payload.get("data"),
            "meta": payload.get("meta"),
            "links": payload.get("links"),
        }
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        row_count = len(artifact["data"]) if isinstance(artifact["data"], list) else 0
        print(f"Saved parsed FiscalData artifact to {output_path}")
        print(f"rows={row_count}")
        return 0
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
