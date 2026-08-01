#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CHART_FREQUENCIES = {
    "D",
    "M",
    "INDEXMONTHLY",
    "INDEXMONTHLYINFLATION",
    "INDEXANNUALAVG",
    "INDEXANNUALCHANGE",
    "A",
    "AC",
    "ANNUALAVG",
    "YTD",
    "YTDCHANGE",
    "Q",
    "QC",
    "DEFAULT",
    "INFLATIONADJUSTED",
}
PAGE_ID_PATTERNS = [
    re.compile(r"generateChart\('(?P<page_id>\d+)'"),
    re.compile(r'var pageId = "(?P<page_id>\d+)"'),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Macrotrends economic-data JSON for a page and chart frequency.")
    parser.add_argument("--page-url", required=True, help="Macrotrends dataset page URL.")
    parser.add_argument("--chart-frequency", required=True, choices=sorted(CHART_FREQUENCIES))
    parser.add_argument("--output", required=True, help="Output JSON path.")
    return parser.parse_args()


def fetch_text(url: str, *, referer: str | None = None) -> str:
    headers = {"User-Agent": "Codex agent-market-data-terminal/1.0"}
    if referer:
        headers["Referer"] = referer
    request = Request(url, headers=headers)
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_page_id(page_url: str, html: str) -> str:
    for pattern in PAGE_ID_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group("page_id")

    parsed = urlparse(page_url)
    path_match = re.search(r"/datasets/(?P<page_id>\d+)/", parsed.path)
    if path_match:
        return path_match.group("page_id")

    raise RuntimeError("Could not extract Macrotrends page_id from the page HTML or URL.")


def fetch_payload(page_url: str, page_id: str, chart_frequency: str) -> dict[str, object]:
    endpoint = f"https://www.macrotrends.net/economic-data/{page_id}/{chart_frequency}"
    raw = fetch_text(endpoint, referer=page_url)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "data" not in payload or "metadata" not in payload:
        raise RuntimeError("Macrotrends endpoint returned an unexpected payload.")

    return {
        "page_url": page_url,
        "page_id": page_id,
        "chart_frequency": chart_frequency,
        "endpoint": endpoint,
        "metadata": payload.get("metadata"),
        "data": payload.get("data"),
    }


def main() -> int:
    args = parse_args()
    try:
        page_html = fetch_text(args.page_url)
        page_id = extract_page_id(args.page_url, page_html)
        payload = fetch_payload(args.page_url, page_id, args.chart_frequency)
    except (HTTPError, URLError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(payload.get('data', []))} rows to {output_path}")
    print(f"page_id={page_id} chart_frequency={args.chart_frequency}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
