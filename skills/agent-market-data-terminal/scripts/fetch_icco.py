#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HOMEPAGE_URL = "https://www.icco.org/"
STATISTICS_URL = "https://www.icco.org/statistics/"
USER_AGENT = "Codex agent-market-data-terminal/1.0"
TAG_PATTERN = re.compile(r"<[^>]+>")
ROW_PATTERN = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_PATTERN = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TABLE_PATTERN = re.compile(r"<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch ICCO daily cocoa price data from the public homepage or statistics page.")
    parser.add_argument(
        "--url",
        default=HOMEPAGE_URL,
        help="ICCO page URL or subscriber bulk-data URL. Defaults to the public homepage.",
    )
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def clean_text(value: str) -> str:
    text = TAG_PATTERN.sub(" ", value)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_number(value: str) -> float | int | None:
    cleaned = clean_text(value).replace(",", "")
    if not cleaned or cleaned in {"-", "--", "n/a", "N/A"}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def parse_html_tables(text: str, source_url: str) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table_match in TABLE_PATTERN.finditer(text):
        rows: list[list[str]] = []
        for row_html in ROW_PATTERN.findall(table_match.group(1)):
            cells = [clean_text(cell) for cell in CELL_PATTERN.findall(row_html)]
            if cells:
                rows.append(cells)
        if not rows:
            continue
        header = rows[0]
        data_rows: list[dict[str, object]] = []
        for cells in rows[1:]:
            row_obj: dict[str, object] = {}
            for index, column_name in enumerate(header):
                if index >= len(cells):
                    continue
                key = column_name or f"column_{index + 1}"
                value = cells[index]
                parsed = parse_number(value)
                row_obj[key] = parsed if parsed is not None else value
            if row_obj:
                data_rows.append(row_obj)
        tables.append({
            "header": header,
            "rows": data_rows,
            "row_count": len(data_rows),
        })

    return {
        "provider": "icco",
        "source_url": source_url,
        "tables": tables,
        "table_count": len(tables),
    }


def detect_format(url: str, payload: bytes) -> str:
    path_lower = urlparse(url).path.lower()
    if path_lower.endswith(".csv"):
        return "csv"
    if path_lower.endswith((".xls", ".xlsx")):
        return "xls"
    head = payload.lstrip()[:200].lower()
    if head.startswith(b"<"):
        return "html"
    if b"," in head and b"\n" in head and not head.startswith(b"pk"):
        return "csv"
    return "binary"


def parse_csv_payload(text: str, source_url: str) -> dict[str, object]:
    import csv
    reader = csv.reader(text.splitlines())
    all_rows = [row for row in reader if row]
    if not all_rows:
        raise RuntimeError("ICCO CSV payload had no rows.")
    header = all_rows[0]
    data_rows: list[dict[str, object]] = []
    for cells in all_rows[1:]:
        row_obj: dict[str, object] = {}
        for index, column_name in enumerate(header):
            if index >= len(cells):
                continue
            key = column_name.strip() or f"column_{index + 1}"
            value = cells[index]
            parsed = parse_number(value)
            row_obj[key] = parsed if parsed is not None else value.strip()
        data_rows.append(row_obj)
    return {
        "provider": "icco",
        "source_url": source_url,
        "format": "csv",
        "header": header,
        "rows": data_rows,
        "row_count": len(data_rows),
    }


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        payload = fetch_bytes(args.url)
        if args.format == "raw":
            output_path.write_bytes(payload)
            print(f"Saved raw ICCO payload to {output_path}")
            print(f"URL: {args.url}")
            return 0

        fmt = detect_format(args.url, payload)
        if fmt == "csv":
            artifact = parse_csv_payload(payload.decode("utf-8-sig", errors="replace"), args.url)
        elif fmt == "html":
            artifact = parse_html_tables(payload.decode("utf-8", errors="replace"), args.url)
        elif fmt == "xls":
            raise RuntimeError(
                "ICCO XLS parsing is out of scope here. Save the file with --format raw and parse separately, "
                "or use worldbank_pinksheet / imf_commodities for monthly bulk history."
            )
        else:
            raise RuntimeError(f"Unrecognized ICCO payload format for {args.url}.")

        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed ICCO artifact to {output_path}")
        if "tables" in artifact:
            print(f"tables={artifact.get('table_count', 0)}")
        else:
            print(f"rows={artifact.get('row_count', 0)}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
