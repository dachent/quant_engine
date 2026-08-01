#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://home.treasury.gov"
TEXT_VIEW_PATH = "/resource-center/data-chart-center/interest-rates/TextView"
USER_AGENT = "Codex agent-market-data-terminal/1.0"
DATASETS = {
    "daily_treasury_yield_curve",
    "daily_treasury_bill_rates",
    "daily_treasury_long_term_rate",
    "daily_treasury_real_yield_curve",
    "daily_treasury_real_long_term",
}
CSV_LINK_PATTERN = re.compile(r'href="(?P<href>[^"]*daily-treasury-rates\.csv[^"]*?_format=csv)"', re.IGNORECASE)
XML_LINK_PATTERN = re.compile(r'href="(?P<href>[^"]*/pages/xml\?[^"]*)"', re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Treasury daily rates XML or CSV feeds.")
    parser.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--year", help="Year scope such as 2024.")
    scope.add_argument("--month", help="Month scope in YYYYMM format such as 202403.")
    parser.add_argument("--view", choices=["xml", "csv"], default="xml")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.year and not re.fullmatch(r"\d{4}", args.year):
        raise ValueError("--year must be formatted as YYYY.")
    if args.month and not re.fullmatch(r"\d{6}", args.month):
        raise ValueError("--month must be formatted as YYYYMM.")


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def build_text_view_url(args: argparse.Namespace) -> str:
    params = [("type", args.dataset)]
    if args.year:
        params.append(("field_tdr_date_value", args.year))
    else:
        params.append(("field_tdr_date_value", args.month))
    return f"{BASE_URL}{TEXT_VIEW_PATH}?{urlencode(params)}"


def resolve_data_url(args: argparse.Namespace, text_view_url: str) -> str:
    html_text = fetch_text(text_view_url)
    pattern = XML_LINK_PATTERN if args.view == "xml" else CSV_LINK_PATTERN
    match = pattern.search(html_text)
    if not match:
        raise RuntimeError(f"Could not find the Treasury {args.view.upper()} link on the TextView page.")
    return urljoin(text_view_url, html.unescape(match.group("href")))


def parse_scalar(value: str | None) -> object:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned == "":
        return None
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d+\.\d+", cleaned):
        return float(cleaned)
    return cleaned


def parse_csv_payload(raw_bytes: bytes) -> tuple[list[str], list[dict[str, object]]]:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = [{key: parse_scalar(value) for key, value in row.items()} for row in reader]
    return reader.fieldnames or [], rows


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml_payload(raw_bytes: bytes) -> tuple[list[str], list[dict[str, object]], str | None]:
    root = ET.fromstring(raw_bytes)
    title = None
    title_node = root.find("{http://www.w3.org/2005/Atom}title")
    if title_node is not None and title_node.text:
        title = title_node.text.strip()

    rows: list[dict[str, object]] = []
    columns: list[str] = []

    for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
        properties = entry.find(".//{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties")
        if properties is None:
            continue
        row: dict[str, object] = {}
        for child in list(properties):
            key = local_name(child.tag)
            row[key] = parse_scalar(child.text)
        if row and not columns:
            columns = list(row.keys())
        if row:
            rows.append(row)

    return columns, rows, title


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        validate_args(args)
        text_view_url = build_text_view_url(args)
        data_url = resolve_data_url(args, text_view_url)
        raw_bytes = fetch_bytes(data_url)

        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw Treasury rates payload to {output_path}")
            print(f"URL: {data_url}")
            return 0

        if args.view == "csv":
            columns, rows = parse_csv_payload(raw_bytes)
            feed_title = None
        else:
            columns, rows, feed_title = parse_xml_payload(raw_bytes)

        artifact = {
            "provider": "treasury_rates",
            "dataset": args.dataset,
            "view": args.view,
            "text_view_url": text_view_url,
            "source_url": data_url,
            "columns": columns,
            "rows": rows,
        }
        if args.year:
            artifact["year"] = args.year
        if args.month:
            artifact["month"] = args.month
        if feed_title:
            artifact["feed_title"] = feed_title

        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed Treasury rates artifact to {output_path}")
        print(f"rows={len(rows)}")
        return 0
    except (ET.ParseError, HTTPError, URLError, OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
