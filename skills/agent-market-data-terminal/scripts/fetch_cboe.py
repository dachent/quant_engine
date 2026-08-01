#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "Codex agent-market-data-terminal/1.0"
ARCHIVE_PAGE_URL = "https://www.cboe.com/en/markets/us/futures/market-statistics/historical-data/settlement-archive/"
ARCHIVE_LINK_PATTERN = re.compile(
    r"<a[^>]+href=[\"'](?P<href>https://cdn\.cboe\.com/resources/futures/archive/volume-and-price/[^\"']+\.csv)[\"'][^>]*>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
ARCHIVE_FILE_PATTERN = re.compile(r"CFE_(?P<month_code>[FGHJKMNQUVXZ])(?P<year>\d{2})_(?P<product>[A-Z0-9]+)\.csv$", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch direct CBOE CSV datasets or discover futures archive files.")
    parser.add_argument("--mode", choices=["direct", "futures-archive"], default="direct")
    parser.add_argument("--url", help="Direct CBOE CSV endpoint URL. Required in direct mode.")
    parser.add_argument("--year", type=int, help="Optional four-digit year filter for futures archive mode.")
    parser.add_argument("--product", help="Optional product code filter for futures archive mode, such as VX.")
    parser.add_argument("--match", help="Optional case-insensitive substring filter for archive labels or URLs.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.mode == "direct":
        if not args.url:
            raise ValueError("--url is required in direct mode.")
        if args.year is not None or args.product or args.match:
            raise ValueError("--year, --product, and --match are only valid in futures-archive mode.")
        return

    if args.url:
        raise ValueError("--url is only valid in direct mode.")
    if args.year is not None and (args.year < 1900 or args.year > 2099):
        raise ValueError("--year must be a four-digit calendar year.")


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def infer_dataset_name(url: str) -> str:
    stem = Path(urlparse(url).path).stem
    return stem or "cboe_dataset"


def parse_scalar(value: str) -> object:
    cleaned = value.strip()
    if cleaned == "":
        return None
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d+\.\d+", cleaned):
        return float(cleaned)
    return cleaned


def parse_csv_payload(raw_bytes: bytes, source_url: str) -> dict[str, object]:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = [{key: parse_scalar(value) for key, value in row.items()} for row in reader]
    return {
        "provider": "cboe",
        "source_url": source_url,
        "dataset": infer_dataset_name(source_url),
        "columns": reader.fieldnames or [],
        "rows": rows,
    }


def clean_label(value: str) -> str:
    text = TAG_PATTERN.sub(" ", value)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def discover_archive_links() -> list[dict[str, object]]:
    html_text = fetch_text(ARCHIVE_PAGE_URL)
    links: list[dict[str, object]] = []
    seen: set[str] = set()

    for match in ARCHIVE_LINK_PATTERN.finditer(html_text):
        href = html.unescape(match.group("href").strip())
        if href in seen:
            continue
        seen.add(href)

        file_name = Path(urlparse(href).path).name
        file_match = ARCHIVE_FILE_PATTERN.match(file_name)
        if not file_match:
            continue

        archive_year = 2000 + int(file_match.group("year"))
        links.append(
            {
                "url": href,
                "label": clean_label(match.group("label")),
                "file_name": file_name,
                "dataset": infer_dataset_name(href),
                "product": file_match.group("product").upper(),
                "year": archive_year,
                "month_code": file_match.group("month_code").upper(),
            }
        )

    return links


def filter_archive_links(
    links: list[dict[str, object]],
    *,
    year: int | None,
    product: str | None,
    match_text: str | None,
) -> list[dict[str, object]]:
    filtered = list(links)

    if year is not None:
        filtered = [item for item in filtered if item.get("year") == year]

    if product:
        product_upper = product.upper()
        filtered = [item for item in filtered if str(item.get("product", "")).upper() == product_upper]

    if match_text:
        needle = match_text.lower()
        filtered = [
            item
            for item in filtered
            if needle in str(item.get("label", "")).lower()
            or needle in str(item.get("url", "")).lower()
            or needle in str(item.get("file_name", "")).lower()
        ]

    return filtered


def choose_archive_link(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    links = discover_archive_links()
    matches = filter_archive_links(links, year=args.year, product=args.product, match_text=args.match)
    if not matches:
        raise ValueError("No CBOE futures archive files matched the requested filters.")
    return str(matches[0]["url"]), {"archive_page_url": ARCHIVE_PAGE_URL, "matches": matches}


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)

        if args.mode == "direct":
            source_url = str(args.url)
            archive_context: dict[str, object] | None = None
        else:
            source_url, archive_context = choose_archive_link(args)

        raw_bytes = fetch_bytes(source_url)
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw CBOE payload to {output_path}")
            print(f"URL: {source_url}")
            return 0

        parsed = parse_csv_payload(raw_bytes, source_url)
        parsed["mode"] = args.mode
        if archive_context is not None:
            parsed["archive_page_url"] = archive_context["archive_page_url"]
            parsed["archive_matches"] = archive_context["matches"]

        output_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed CBOE artifact to {output_path}")
        print(f"rows={len(parsed['rows'])}")
        return 0
    except (HTTPError, URLError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
