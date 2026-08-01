#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://www.multpl.com"
ROW_PATTERN = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_PATTERN = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r"<[^>]+>")
TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
CURRENT_PATTERN = re.compile(r'<div id="current">(.*?)</div>\s*</div>|<div id="current">(.*?)</div>', re.IGNORECASE | re.DOTALL)
CURRENT_TITLE_PATTERN = re.compile(r'<span class="currentTitle">(.*?)</span>', re.IGNORECASE | re.DOTALL)
TIMESTAMP_PATTERN = re.compile(r'<div id="timestamp">(.*?)</div>', re.IGNORECASE | re.DOTALL)
CHANGE_PATTERN = re.compile(r'<span class="([^"]+)">(.*?)</span>', re.IGNORECASE | re.DOTALL)
ESTIMATE_PATTERN = re.compile(r'<abbr[^>]*title="Estimate"[^>]*>', re.IGNORECASE)
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
EPOCH = date(1970, 1, 1)
SCALE_TYPE_MAP = {
    0: "linear",
    1: "logarithmic",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch raw or parsed Multpl chart, table, or Atom views.")
    parser.add_argument("--page-url", required=True, help="Base Multpl chart page URL.")
    parser.add_argument("--view", required=True, choices=["chart", "table-year", "table-month", "atom"])
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
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


def parse_number(value: str | int | float | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return normalize_number(float(value))
    cleaned = clean_text(value).replace("†", "").replace("%", "").strip()
    if not cleaned:
        return None
    try:
        return normalize_number(float(cleaned.replace(",", "")))
    except ValueError:
        return None


def canonicalize_base_url(page_url: str) -> str:
    parsed = urlparse(page_url)
    path = parsed.path.rstrip("/")
    for suffix in ("/table/by-year", "/table/by-month", "/atom"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path:
        raise ValueError("Multpl page URL must include a metric path.")
    return f"{parsed.scheme or 'https'}://{parsed.netloc or 'www.multpl.com'}{path}"


def view_url(base_url: str, view: str) -> str:
    if view == "chart":
        return base_url
    if view == "table-year":
        return f"{base_url}/table/by-year"
    if view == "table-month":
        return f"{base_url}/table/by-month"
    return f"{base_url}/atom"


def extract_title(html_text: str) -> str:
    h1_match = H1_PATTERN.search(html_text)
    if h1_match:
        return clean_text(h1_match.group(1))
    title_match = TITLE_PATTERN.search(html_text)
    if title_match:
        title = clean_text(title_match.group(1))
        return re.sub(r"\s*-\s*Multpl\s*$", "", title)
    return "Multpl"


def extract_current_html(document_text: str) -> str | None:
    match = CURRENT_PATTERN.search(document_text)
    if not match:
        return None
    return match.group(1) or match.group(2)


def parse_current_block(current_html: str | None) -> dict[str, object] | None:
    if not current_html:
        return None

    current_title_match = CURRENT_TITLE_PATTERN.search(current_html)
    title = clean_text(current_title_match.group(1)) if current_title_match else None

    timestamp_match = TIMESTAMP_PATTERN.search(current_html)
    timestamp = clean_text(timestamp_match.group(1)) if timestamp_match else None

    change_match = CHANGE_PATTERN.search(current_html)
    change_class = change_match.group(1) if change_match else None
    change_text = clean_text(change_match.group(2)) if change_match else ""
    change = None
    change_percent = None
    if change_text:
        percent_match = re.search(r"\(([^)]+)%\)", change_text)
        change_percent = parse_number(percent_match.group(1)) if percent_match else None
        change_cleaned = re.sub(r"\([^)]*\)", "", change_text).strip()
        change = parse_number(change_cleaned)

    value_html = current_html
    value_html = re.sub(r"<b[^>]*>.*?</b>", " ", value_html, flags=re.IGNORECASE | re.DOTALL)
    value_html = re.sub(r"<span[^>]*>.*?</span>", " ", value_html, flags=re.IGNORECASE | re.DOTALL)
    value_html = re.sub(r'<div id="timestamp">.*?</div>', " ", value_html, flags=re.IGNORECASE | re.DOTALL)
    value = parse_number(value_html)

    return {
        "label": title,
        "value": value,
        "change": change,
        "change_percent": change_percent,
        "change_class": change_class,
        "timestamp": timestamp,
    }


def extract_pi_literal(html_text: str) -> str:
    anchor = html_text.find("let pi =")
    if anchor < 0:
        raise RuntimeError("Could not find Multpl inline `pi` payload.")

    start = html_text.find("[", anchor)
    if start < 0:
        raise RuntimeError("Could not find the start of the Multpl `pi` array.")

    depth = 0
    in_string = False
    escape = False
    quote_char = ""

    for index in range(start, len(html_text)):
        char = html_text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote_char:
                in_string = False
            continue

        if char in {'"', "'"}:
            in_string = True
            quote_char = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return html_text[start : index + 1]

    raise RuntimeError("Could not parse the full Multpl `pi` array.")


def load_js_array(literal: str) -> list[object]:
    try:
        parsed = json.loads(literal)
    except json.JSONDecodeError:
        python_literal = re.sub(r"\bnull\b", "None", literal)
        python_literal = re.sub(r"\btrue\b", "True", python_literal)
        python_literal = re.sub(r"\bfalse\b", "False", python_literal)
        parsed = ast.literal_eval(python_literal)
    if not isinstance(parsed, list):
        raise RuntimeError("Multpl `pi` payload was not a list.")
    return parsed


def to_iso_date(days_since_epoch: int | float) -> str:
    return (EPOCH + timedelta(days=int(days_since_epoch))).isoformat()


def build_chart_series(series_name: str, x_values: list[object], y_values: object) -> list[dict[str, object]]:
    if not isinstance(x_values, list):
        raise RuntimeError("Multpl `pi` x-values were not a list.")

    if isinstance(y_values, list) and y_values and isinstance(y_values[0], list):
        series_payload = []
        for index, values in enumerate(y_values, start=1):
            rows = []
            for x_value, y_value in zip(x_values, values):
                parsed_value = parse_number(y_value)
                if parsed_value is None:
                    continue
                rows.append({"date": to_iso_date(x_value), "value": parsed_value})
            series_payload.append({"name": f"{series_name} {index}", "rows": rows})
        return series_payload

    if not isinstance(y_values, list):
        raise RuntimeError("Multpl `pi` y-values were not a list.")

    rows = []
    for x_value, y_value in zip(x_values, y_values):
        parsed_value = parse_number(y_value)
        if parsed_value is None:
            continue
        rows.append({"date": to_iso_date(x_value), "value": parsed_value})
    return [{"name": series_name, "rows": rows}]


def parse_chart_page(source_url: str, html_text: str) -> dict[str, object]:
    title = extract_title(html_text)
    current = parse_current_block(extract_current_html(html_text))
    pi = load_js_array(extract_pi_literal(html_text))
    if len(pi) < 7:
        raise RuntimeError("Multpl `pi` payload did not include the expected seven elements.")

    x_values, y_values, y_axis_min, y_axis_max, suffix, scale_type_code, annotations = pi[:7]
    series = build_chart_series(title, x_values, y_values)
    parsed_scale_code = parse_number(scale_type_code)
    scale_type_value = int(parsed_scale_code) if isinstance(parsed_scale_code, int) else parsed_scale_code
    scale_name = SCALE_TYPE_MAP.get(scale_type_value, "unknown")

    return {
        "provider": "multpl",
        "view": "chart",
        "source_url": source_url,
        "title": title,
        "current": current,
        "series": series,
        "y_axis_min": parse_number(y_axis_min),
        "y_axis_max": parse_number(y_axis_max),
        "suffix": suffix if isinstance(suffix, str) else str(suffix),
        "scale_type": scale_name,
        "scale_type_code": scale_type_value,
        "annotations": annotations,
    }


def parse_table_date(value: str) -> str:
    return datetime.strptime(value, "%b %d, %Y").date().isoformat()


def parse_table_page(source_url: str, html_text: str, view: str) -> dict[str, object]:
    table_match = re.search(r'<table id="datatable".*?</table>', html_text, re.IGNORECASE | re.DOTALL)
    if not table_match:
        raise RuntimeError("Could not find the Multpl data table.")

    rows = []
    for row_html in ROW_PATTERN.findall(table_match.group(0)):
        cells = CELL_PATTERN.findall(row_html)
        if len(cells) < 2:
            continue
        date_text = clean_text(cells[0])
        if date_text == "Date":
            continue
        is_estimate = bool(ESTIMATE_PATTERN.search(cells[1]))
        value = parse_number(cells[1])
        if value is None:
            continue
        rows.append({"date": parse_table_date(date_text), "value": value, "is_estimate": is_estimate})

    return {
        "provider": "multpl",
        "view": view,
        "source_url": source_url,
        "title": extract_title(html_text),
        "rows": rows,
    }


def parse_atom(source_url: str, xml_text: str) -> dict[str, object]:
    root = ET.fromstring(xml_text)
    title = root.findtext("atom:title", default="Multpl", namespaces=ATOM_NS)
    updated_at = root.findtext("atom:updated", default=None, namespaces=ATOM_NS)
    entry = root.find("atom:entry", ATOM_NS)
    entry_updated = entry.findtext("atom:updated", default=None, namespaces=ATOM_NS) if entry is not None else None
    entry_content = entry.findtext("atom:content", default="", namespaces=ATOM_NS) if entry is not None else ""
    current = parse_current_block(html.unescape(entry_content))

    return {
        "provider": "multpl",
        "view": "atom",
        "source_url": source_url,
        "title": html.unescape(title),
        "updated_at": updated_at,
        "entry_updated_at": entry_updated,
        "current": current,
    }


def main() -> int:
    args = parse_args()
    try:
        base_url = canonicalize_base_url(args.page_url)
        source_url = view_url(base_url, args.view)
        payload = fetch_bytes(source_url)

        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if args.format == "raw":
            output_path.write_bytes(payload)
            print(f"Saved raw Multpl payload to {output_path}")
            print(f"URL: {source_url}")
            return 0

        text = payload.decode("utf-8", errors="replace")
        if args.view == "chart":
            parsed_output = parse_chart_page(source_url, text)
        elif args.view in {"table-year", "table-month"}:
            parsed_output = parse_table_page(source_url, text, args.view)
        else:
            parsed_output = parse_atom(source_url, text)

        output_path.write_text(json.dumps(parsed_output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed Multpl artifact to {output_path}")
        if "rows" in parsed_output:
            print(f"rows={len(parsed_output['rows'])}")
        elif "series" in parsed_output:
            print(f"series={len(parsed_output['series'])}")
        return 0
    except (ET.ParseError, HTTPError, URLError, RuntimeError, ValueError, SyntaxError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
