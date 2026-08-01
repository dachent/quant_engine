#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.worldbank.org/v2"
USER_AGENT = "Codex agent-market-data-terminal/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch no-auth World Bank indicator data.")
    parser.add_argument("--country", required=True, help="Country code or 'all'.")
    parser.add_argument("--indicator", required=True, help="Indicator code such as NY.GDP.MKTP.CD.")
    parser.add_argument("--date", help="Optional date range such as 2000:2024.")
    parser.add_argument("--mrv", type=int, help="Optional most-recent-values count.")
    parser.add_argument("--per-page", type=int, help="Optional page size for parsed-mode pagination.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def build_url(country: str, indicator: str, *, date_value: str | None, mrv: int | None, per_page: int | None, page: int | None = None) -> str:
    params = [("format", "json")]
    if date_value:
        params.append(("date", date_value))
    if mrv is not None:
        params.append(("mrv", str(mrv)))
    if per_page is not None:
        params.append(("per_page", str(per_page)))
    if page is not None:
        params.append(("page", str(page)))
    query = urlencode(params)
    return f"{BASE_URL}/country/{country}/indicator/{indicator}?{query}"


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def flatten_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "indicator_id": row.get("indicator", {}).get("id") if isinstance(row.get("indicator"), dict) else None,
        "indicator_name": row.get("indicator", {}).get("value") if isinstance(row.get("indicator"), dict) else None,
        "country_id": row.get("country", {}).get("id") if isinstance(row.get("country"), dict) else None,
        "country_name": row.get("country", {}).get("value") if isinstance(row.get("country"), dict) else None,
        "countryiso3code": row.get("countryiso3code"),
        "date": row.get("date"),
        "value": row.get("value"),
        "unit": row.get("unit"),
        "obs_status": row.get("obs_status"),
        "decimal": row.get("decimal"),
    }


def fetch_all_pages(country: str, indicator: str, *, date_value: str | None, mrv: int | None, per_page: int | None) -> tuple[str, dict[str, object] | None, list[dict[str, object]]]:
    first_url = build_url(country, indicator, date_value=date_value, mrv=mrv, per_page=per_page)
    first_payload = json.loads(fetch_bytes(first_url).decode("utf-8"))
    if not isinstance(first_payload, list) or len(first_payload) != 2:
        raise RuntimeError("World Bank returned an unexpected payload shape.")

    metadata = first_payload[0] if isinstance(first_payload[0], dict) else None
    rows = first_payload[1] if isinstance(first_payload[1], list) else []
    combined_rows = list(rows)

    total_pages = int(metadata.get("pages", 1)) if metadata else 1
    for page_number in range(2, total_pages + 1):
        page_url = build_url(country, indicator, date_value=date_value, mrv=mrv, per_page=per_page, page=page_number)
        page_payload = json.loads(fetch_bytes(page_url).decode("utf-8"))
        if isinstance(page_payload, list) and len(page_payload) == 2 and isinstance(page_payload[1], list):
            combined_rows.extend(page_payload[1])

    return first_url, metadata, [row for row in combined_rows if isinstance(row, dict)]


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        request_url = build_url(args.country, args.indicator, date_value=args.date, mrv=args.mrv, per_page=args.per_page)
        raw_bytes = fetch_bytes(request_url)

        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw World Bank payload to {output_path}")
            print(f"URL: {request_url}")
            return 0

        request_url, metadata, rows = fetch_all_pages(args.country, args.indicator, date_value=args.date, mrv=args.mrv, per_page=args.per_page)
        artifact = {
            "provider": "worldbank",
            "source_url": request_url,
            "metadata": metadata,
            "rows": [flatten_row(row) for row in rows],
        }
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed World Bank artifact to {output_path}")
        print(f"rows={len(artifact['rows'])}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
