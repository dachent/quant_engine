#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://www.westmetall.com"
ROW_PATTERN = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_PATTERN = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
YEAR_SECTION_PATTERN = re.compile(
    r'<a id="y(?P<year>\d{4})"></a>\s*<h2>(?P<title>.*?)</h2>.*?<table\b[^>]*>(?P<table>.*?)</table>',
    re.IGNORECASE | re.DOTALL,
)
TAG_PATTERN = re.compile(r"<[^>]+>")
MONTH_NAMES = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Westmetall chart XML or HTML market-data views.")
    parser.add_argument("--field", help="Westmetall field id, for example LME_Cu_cash.")
    parser.add_argument("--view", required=True, choices=["chart", "table", "averages"])
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    parser.add_argument("--lang", default="en", help="Westmetall language, defaults to en.")
    parser.add_argument("--page-url", help="Existing Westmetall page URL. The script can resolve field from it.")
    return parser.parse_args()


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Codex agent-market-data-terminal/1.0"})
    with urlopen(request, timeout=60) as response:
        return response.read()


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def clean_text(value: str) -> str:
    text = TAG_PATTERN.sub(" ", value)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def parse_number(value: str) -> int | float | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    try:
        return normalize_number(float(cleaned.replace(",", "")))
    except ValueError:
        return None


def infer_language(page_url: str | None, default_lang: str) -> str:
    if not page_url:
        return default_lang
    match = re.match(r"^/(de|en)/", urlparse(page_url).path)
    if match:
        return match.group(1)
    return default_lang


def resolve_field(field: str | None, page_url: str | None) -> str:
    if field:
        return field.strip()
    if page_url:
        query = parse_qs(urlparse(page_url).query)
        values = query.get("field")
        if values:
            return values[0].strip()
    raise ValueError("Provide --field or a --page-url that includes ?field=...")


def build_html_url(field: str, view: str, lang: str) -> str:
    action = "diagram" if view == "chart" else view
    return f"{BASE_URL}/{lang}/markdaten.php?{urlencode({'action': action, 'field': field})}"


def build_chart_api_url(field: str, lang: str) -> str:
    return f"{BASE_URL}/api/marketdata/{lang}/{field}/"


def parse_chart_xml(xml_text: str, field: str, source_url: str) -> dict[str, object]:
    root = ET.fromstring(xml_text)
    title = root.findtext("title") or "Market Data"
    series: list[dict[str, object]] = []

    for line_series in root.findall(".//lineSeries"):
        metadata = dict(line_series.attrib)
        name = metadata.pop("name", "Unnamed series")
        rows: list[dict[str, object]] = []
        for point in line_series.findall("val"):
            x_value = point.attrib.get("x")
            y_value = point.attrib.get("y")
            if not x_value or y_value is None:
                continue
            parsed_value = parse_number(y_value)
            if parsed_value is None:
                continue
            rows.append({"date": x_value.replace("/", "-"), "value": parsed_value})

        series.append({"name": name, "type": "lineSeries", "metadata": metadata, "rows": rows})

    return {
        "provider": "westmetall",
        "view": "chart",
        "field": field,
        "source_url": source_url,
        "title": title,
        "series": series,
    }


def parse_westmetall_date(value: str) -> str:
    return datetime.strptime(value, "%d. %B %Y").date().isoformat()


def parse_table_view(html_text: str, field: str, view: str, source_url: str) -> dict[str, object]:
    matches = list(YEAR_SECTION_PATTERN.finditer(html_text))
    if not matches:
        raise RuntimeError("Could not find Westmetall year sections in the HTML view.")

    title = clean_text(matches[0].group("title")) or "Westmetall Market Data"
    series_map: OrderedDict[str, list[dict[str, object]]] = OrderedDict()

    for match in matches:
        section_year = int(match.group("year"))
        header: list[str] | None = None

        for row_html in ROW_PATTERN.findall(match.group("table")):
            cells = [clean_text(cell) for cell in CELL_PATTERN.findall(row_html)]
            if not cells:
                continue

            if header is None:
                header = cells
                for column_name in header[1:]:
                    if column_name:
                        series_map.setdefault(column_name, [])
                continue

            if cells == header or cells[0].lower() in {"date", "month"} or len(cells) < len(header):
                continue

            if view == "table":
                base_row = {"date": parse_westmetall_date(cells[0]), "year": section_year}
            else:
                month_number = MONTH_NAMES.get(cells[0])
                if month_number is None:
                    continue
                base_row = {"date": f"{section_year}-{month_number:02d}-01", "year": section_year, "month": cells[0]}

            for index, column_name in enumerate(header[1:], start=1):
                if not column_name or index >= len(cells):
                    continue
                parsed_value = parse_number(cells[index])
                if parsed_value is None:
                    continue
                series_map[column_name].append({**base_row, "value": parsed_value})

    return {
        "provider": "westmetall",
        "view": view,
        "field": field,
        "source_url": source_url,
        "title": title,
        "series": [{"name": name, "rows": rows} for name, rows in series_map.items()],
    }


def build_chart_fallback(field: str, lang: str, exc: Exception) -> dict[str, object]:
    fallback_url = build_html_url(field, "table", lang)
    fallback_html = fetch_text(fallback_url)
    fallback_output = parse_table_view(fallback_html, field, "table", fallback_url)
    return {
        "provider": "westmetall",
        "view": "chart",
        "field": field,
        "source_url": build_chart_api_url(field, lang),
        "title": fallback_output.get("title"),
        "series": fallback_output.get("series", []),
        "fallback_source_url": fallback_url,
        "fallback_reason": str(exc),
    }


def main() -> int:
    args = parse_args()
    try:
        field = resolve_field(args.field, args.page_url)
        lang = infer_language(args.page_url, args.lang)
        source_url = build_chart_api_url(field, lang) if args.view == "chart" else build_html_url(field, args.view, lang)
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if args.format == "raw":
            payload = fetch_bytes(source_url)
            output_path.write_bytes(payload)
            print(f"Saved raw Westmetall payload to {output_path}")
            print(f"URL: {source_url}")
            return 0

        if args.view == "chart":
            try:
                payload = fetch_bytes(source_url)
                text = payload.decode("utf-8", errors="replace")
                parsed_output = parse_chart_xml(text, field, source_url)
            except (ET.ParseError, HTTPError, URLError, RuntimeError) as exc:
                parsed_output = build_chart_fallback(field, lang, exc)
        else:
            payload = fetch_bytes(source_url)
            text = payload.decode("utf-8", errors="replace")
            parsed_output = parse_table_view(text, field, args.view, source_url)

        output_path.write_text(json.dumps(parsed_output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed Westmetall artifact to {output_path}")
        print(f"series={len(parsed_output.get('series', []))}")
        return 0
    except (ET.ParseError, HTTPError, URLError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
