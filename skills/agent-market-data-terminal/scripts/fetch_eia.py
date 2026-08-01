#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen


TAG_PATTERN = re.compile(r"<[^>]+>")
HISTORY_LINK_PATTERN = re.compile(
    r"href\s*=\s*(?:\"(?P<double>[^\"]*LeafHandler\.ashx[^\"]*)\"|'(?P<single>[^']*LeafHandler\.ashx[^']*)'|(?P<bare>[^\"'\s>]*LeafHandler\.ashx[^\"'\s>]*))",
    re.IGNORECASE,
)
DOWNLOAD_LINK_PATTERN = re.compile(
    r"href\s*=\s*(?:\"(?P<double>[^\"]*\.xls)\"|'(?P<single>[^']*\.xls)'|(?P<bare>[^\"'\s>]*\.xls))",
    re.IGNORECASE,
)
TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_REFRESH_PATTERN = re.compile(
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=(?P<target>[^"\']+)["\']',
    re.IGNORECASE,
)
SUMMARY_SECTION_PATTERN = re.compile(
    r"<!--Start of Inner Data Table -->(.*?)<!--End of Inner Data Table -->",
    re.IGNORECASE | re.DOTALL,
)
HISTORY_FILE_PATTERN = re.compile(r"(?P<series>.+?)(?P<freq>[DWMA])\.(?:htm|xls)$", re.IGNORECASE)
MISSING_MARKERS = {"", "-", "--", "NA", "W"}
FREQUENCY_NAME_TO_CODE = {
    "daily": "D",
    "weekly": "W",
    "monthly": "M",
    "annual": "A",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch raw or parsed EIA DNAV summary/history pages.")
    parser.add_argument(
        "--page-url",
        required=True,
        help="EIA DNAV summary page, static history page, or LeafHandler history URL.",
    )
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    parser.add_argument("--prefer", choices=["xls", "html"], default="xls", help="Raw-mode preference, defaults to xls.")
    parser.add_argument(
        "--source",
        choices=["auto", "xls", "html"],
        default="auto",
        help="Parsed history source policy: auto prefers XLS, xls requires XLS, html forces HTML parsing.",
    )
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


def clean_scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    return clean_text(str(value))


def normalize_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def parse_number(value: str) -> int | float | None:
    cleaned = clean_text(value).replace("†", "").strip()
    if cleaned in MISSING_MARKERS:
        return None
    try:
        return normalize_number(float(cleaned.replace(",", "")))
    except ValueError:
        return None


def parse_numeric_cell(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value):
            return None
        return normalize_number(float(value))
    return parse_number(str(value))


def parse_short_date(value: str) -> str:
    return datetime.strptime(value, "%m/%d/%y").date().isoformat()


def parse_release_date(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = clean_text(value)
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def extract_title(html_text: str) -> str:
    match = TITLE_PATTERN.search(html_text)
    return clean_text(match.group(1)) if match else "EIA DNAV"


def extract_meta_refresh_url(html_text: str, source_url: str) -> str | None:
    match = META_REFRESH_PATTERN.search(html_text)
    if not match:
        return None
    return urljoin(source_url, html.unescape(match.group("target").strip()))


def extract_href(match: re.Match[str]) -> str | None:
    for key in ("double", "single", "bare"):
        value = match.group(key)
        if value:
            return value
    return None


def extract_download_url(html_text: str, source_url: str) -> str | None:
    match = DOWNLOAD_LINK_PATTERN.search(html_text)
    href = extract_href(match) if match else None
    return urljoin(source_url, html.unescape(href)) if href else None


def extract_release_dates(html_text: str) -> tuple[str | None, str | None]:
    release_match = re.search(r"Release Date:\s*([^<]+)", html_text, re.IGNORECASE)
    next_release_match = re.search(r"Next Release Date:\s*([^<]+)", html_text, re.IGNORECASE)
    return (
        parse_release_date(release_match.group(1)) if release_match else None,
        parse_release_date(next_release_match.group(1)) if next_release_match else None,
    )


class TopLevelTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.target_table_depth: int | None = None
        self.in_row = False
        self.current_row_class = ""
        self.current_row: list[dict[str, object]] = []
        self.rows: list[dict[str, object]] = []
        self.capture_cell = False
        self.current_cell_parts: list[str] = []
        self.current_cell_attrs: dict[str, str] = {}
        self.current_cell_tag = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "table":
            self.table_depth += 1
            if self.target_table_depth is None:
                self.target_table_depth = self.table_depth

        if tag == "tr" and self.table_depth == self.target_table_depth and not self.in_row:
            self.in_row = True
            self.current_row_class = attr_map.get("class", "")
            self.current_row = []
            return

        if tag in {"td", "th"} and self.in_row and self.table_depth == self.target_table_depth and not self.capture_cell:
            self.capture_cell = True
            self.current_cell_parts = [self.get_starttag_text()]
            self.current_cell_attrs = attr_map
            self.current_cell_tag = tag
            return

        if self.capture_cell:
            self.current_cell_parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if self.capture_cell:
            self.current_cell_parts.append(f"</{tag}>")

        if tag == self.current_cell_tag and self.capture_cell and self.in_row and self.table_depth == self.target_table_depth:
            cell_html = "".join(self.current_cell_parts)
            self.current_row.append(
                {
                    "tag": self.current_cell_tag,
                    "attrs": self.current_cell_attrs,
                    "html": cell_html,
                    "text": clean_text(cell_html),
                }
            )
            self.capture_cell = False
            self.current_cell_parts = []
            self.current_cell_attrs = {}
            self.current_cell_tag = ""
            return

        if tag == "tr" and self.in_row and self.table_depth == self.target_table_depth:
            self.rows.append({"class": self.current_row_class, "cells": self.current_row})
            self.in_row = False
            self.current_row_class = ""
            self.current_row = []
            return

        if tag == "table":
            self.table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.capture_cell:
            self.current_cell_parts.append(data)


def page_type_for(page_url: str) -> str:
    path = urlparse(page_url).path.lower()
    return "history" if "/hist/" in path else "summary"


def parse_summary_page(page_url: str, html_text: str, download_url: str | None) -> dict[str, object]:
    section_match = SUMMARY_SECTION_PATTERN.search(html_text)
    summary_section = section_match.group(1) if section_match else html_text
    parser = TopLevelTableParser()
    parser.feed(summary_section)
    parser.close()

    header_dates: list[str] = []
    release_date, next_release_date = extract_release_dates(html_text)
    current_category: str | None = None
    rows: list[dict[str, object]] = []

    for parsed_row in parser.rows:
        cells = parsed_row.get("cells", [])
        if not cells:
            continue

        first_cell = cells[0]
        label = str(first_cell.get("text", ""))
        if not label:
            continue

        cell_class = str(first_cell.get("attrs", {}).get("class", ""))
        if label == "Product by Area":
            header_dates = [parse_short_date(str(cell.get("text", ""))) for cell in cells[1:-1] if str(cell.get("text", "")).strip()]
            continue

        if cell_class == "DataStub2":
            current_category = label
            continue

        if cell_class == "DataStub":
            history_cell_html = str(cells[-1].get("html", "")) if cells else ""
            history_match = HISTORY_LINK_PATTERN.search(history_cell_html)
            history_href = extract_href(history_match) if history_match else None
            history_range = str(cells[-1].get("text", "")) if cells else None
            values = []
            for header_date, value_cell in zip(header_dates, cells[1:-1]):
                values.append({"date": header_date, "value": parse_number(str(value_cell.get("text", "")))})
            rows.append(
                {
                    "category": current_category,
                    "label": label,
                    "values": values,
                    "history_url": urljoin(page_url, html.unescape(history_href)) if history_href else None,
                    "history_range": history_range or None,
                }
            )

    return {
        "provider": "eia",
        "page_type": "summary",
        "source_url": page_url,
        "download_url": download_url,
        "title": extract_title(html_text),
        "release_date": release_date,
        "next_release_date": next_release_date,
        "parsed_from": "html",
        "rows": rows,
    }


def infer_history_details(page_url: str, download_url: str | None = None) -> tuple[str | None, str]:
    parsed_url = urlparse(page_url)
    query = parse_qs(parsed_url.query)
    series_id = query.get("s", [None])[0]
    frequency = query.get("f", [None])[0]

    for candidate_url in (page_url, download_url):
        if not candidate_url:
            continue
        basename = Path(urlparse(candidate_url).path).name
        match = HISTORY_FILE_PATTERN.match(basename)
        if match:
            if not series_id:
                series_id = match.group("series").upper()
            if not frequency:
                frequency = match.group("freq").upper()

    if not frequency:
        raise RuntimeError("Could not determine EIA history frequency from the URL or linked XLS.")
    return series_id.upper() if series_id else None, frequency.upper()


def parse_daily_week_label(value: str) -> date | None:
    match = re.match(r"^(\d{4})\s+([A-Za-z]{3})-\s*(\d{1,2})\s+to\s+[A-Za-z]{3}-\s*\d{1,2}$", value)
    if not match:
        return None
    year_value, month_name, day_value = match.groups()
    month_number = datetime.strptime(month_name, "%b").month
    return date(int(year_value), month_number, int(day_value))


def parse_daily_history_rows(row_cells: list[str]) -> list[dict[str, object]]:
    week_start = parse_daily_week_label(row_cells[0])
    if week_start is None:
        return []
    rows = []
    for index, cell in enumerate(row_cells[1:6]):
        parsed_value = parse_number(cell)
        if parsed_value is None:
            continue
        rows.append({"date": (week_start + timedelta(days=index)).isoformat(), "value": parsed_value})
    return rows


def parse_weekly_history_rows(row_cells: list[str]) -> list[dict[str, object]]:
    match = re.match(r"^(\d{4})-([A-Za-z]{3})$", row_cells[0])
    if not match:
        return []
    year_value = int(match.group(1))
    rows = []
    for index in range(1, len(row_cells), 2):
        if index + 1 >= len(row_cells):
            break
        end_date_text = row_cells[index]
        parsed_value = parse_number(row_cells[index + 1])
        if parsed_value is None or not end_date_text:
            continue
        month_value, day_value = [int(part) for part in end_date_text.split("/")]
        rows.append({"date": date(year_value, month_value, day_value).isoformat(), "value": parsed_value})
    return rows


def parse_monthly_history_rows(row_cells: list[str]) -> list[dict[str, object]]:
    if not re.match(r"^\d{4}$", row_cells[0]):
        return []
    year_value = int(row_cells[0])
    rows = []
    for month_number, cell in enumerate(row_cells[1:13], start=1):
        parsed_value = parse_number(cell)
        if parsed_value is None:
            continue
        rows.append({"date": f"{year_value}-{month_number:02d}-01", "value": parsed_value})
    return rows


def parse_annual_history_rows(row_cells: list[str]) -> list[dict[str, object]]:
    match = re.match(r"^(\d{4})'s$", row_cells[0])
    if not match:
        return []
    start_year = int(match.group(1))
    rows = []
    for offset, cell in enumerate(row_cells[1:11]):
        parsed_value = parse_number(cell)
        if parsed_value is None:
            continue
        rows.append({"date": f"{start_year + offset}-01-01", "value": parsed_value})
    return rows


def parse_history_page_html(page_url: str, html_text: str, download_url: str | None) -> dict[str, object]:
    series_id, frequency = infer_history_details(page_url, download_url)
    release_date, next_release_date = extract_release_dates(html_text)
    rows: list[dict[str, object]] = []
    table_match = re.search(r"<table[^>]*class=['\"]FloatTitle['\"][^>]*>.*?</table>", html_text, re.IGNORECASE | re.DOTALL)
    if not table_match:
        table_match = re.search(r"<table\s+SUMMARY=(?:['\"].*?['\"]|[^>]+)>.*?</table>", html_text, re.IGNORECASE | re.DOTALL)
    if not table_match:
        raise RuntimeError("Could not find the EIA history data table.")

    parser = TopLevelTableParser()
    parser.feed(table_match.group(0))
    parser.close()

    for parsed_row in parser.rows:
        cells = [str(cell.get("text", "")) for cell in parsed_row.get("cells", [])]
        if not cells or len(cells) < 2:
            continue

        if frequency == "D":
            rows.extend(parse_daily_history_rows(cells))
        elif frequency == "W":
            rows.extend(parse_weekly_history_rows(cells))
        elif frequency == "M":
            rows.extend(parse_monthly_history_rows(cells))
        elif frequency == "A":
            rows.extend(parse_annual_history_rows(cells))

    return {
        "provider": "eia",
        "page_type": "history",
        "source_url": page_url,
        "download_url": download_url,
        "title": extract_title(html_text),
        "series_id": series_id,
        "frequency": frequency,
        "release_date": release_date,
        "next_release_date": next_release_date,
        "parsed_from": "html_fallback",
        "rows": rows,
    }


def import_xlrd():
    try:
        import xlrd  # type: ignore
    except ImportError as exc:
        raise RuntimeError("EIA XLS parsing requires the optional 'xlrd' package.") from exc
    return xlrd


def normalize_frequency_code(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = clean_text(value).lower()
    return FREQUENCY_NAME_TO_CODE.get(cleaned, cleaned.upper())


def extract_workbook_metadata(contents_sheet) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {
        "title": None,
        "frequency": None,
        "release_date": None,
        "next_release_date": None,
        "excel_file_name": None,
        "available_from_web_page": None,
    }
    for row_index in range(contents_sheet.nrows):
        row = [clean_scalar(value) for value in contents_sheet.row_values(row_index)]
        if len(row) > 1 and row[1] == "Data 1":
            metadata["title"] = row[2] if len(row) > 2 else None
            metadata["frequency"] = row[4] if len(row) > 4 else None
            continue

        label = row[1].rstrip(":").lower() if len(row) > 1 else ""
        value = row[2] if len(row) > 2 else None
        if label == "release date":
            metadata["release_date"] = value
        elif label == "next release date":
            metadata["next_release_date"] = value
        elif label == "excel file name":
            metadata["excel_file_name"] = value
        elif label == "available from web page":
            metadata["available_from_web_page"] = value
    return metadata


def xls_date_to_iso(xlrd_module, workbook, value: object, frequency: str) -> str:
    if isinstance(value, (int, float)):
        parsed_date = xlrd_module.xldate.xldate_as_datetime(float(value), workbook.datemode).date()
    else:
        text_value = clean_scalar(value)
        if not text_value:
            raise RuntimeError("Encountered an empty EIA XLS date cell.")
        if frequency in {"D", "W"}:
            parsed_date = datetime.strptime(text_value, "%b %d, %Y").date()
        elif frequency == "M":
            parsed_date = datetime.strptime(text_value, "%b-%Y").date()
        elif frequency == "A":
            year_match = re.match(r"^\d{4}$", text_value)
            if not year_match:
                raise RuntimeError(f"Unexpected annual EIA XLS date value: {text_value}")
            return f"{text_value}-01-01"
        else:
            raise RuntimeError(f"Unsupported EIA history frequency: {frequency}")

    if frequency == "M":
        return f"{parsed_date.year}-{parsed_date.month:02d}-01"
    if frequency == "A":
        return f"{parsed_date.year}-01-01"
    return parsed_date.isoformat()


def extract_history_rows_from_workbook(workbook, xlrd_module, frequency: str) -> tuple[str | None, str, list[dict[str, object]]]:
    try:
        data_sheet = workbook.sheet_by_name("Data 1")
    except Exception as exc:
        raise RuntimeError("EIA XLS workbook did not include the expected 'Data 1' worksheet.") from exc

    if data_sheet.nrows < 4 or data_sheet.ncols < 2:
        raise RuntimeError("EIA XLS workbook did not contain the expected 'Data 1' layout.")

    sourcekey = clean_scalar(data_sheet.cell_value(1, 1)) or None
    value_label = clean_scalar(data_sheet.cell_value(2, 1)) or "EIA Series"
    rows: list[dict[str, object]] = []

    for row_index in range(3, data_sheet.nrows):
        date_cell = data_sheet.cell_value(row_index, 0)
        value_cell = data_sheet.cell_value(row_index, 1)
        if clean_scalar(date_cell) == "":
            continue
        parsed_value = parse_numeric_cell(value_cell)
        if parsed_value is None:
            continue
        rows.append({"date": xls_date_to_iso(xlrd_module, workbook, date_cell, frequency), "value": parsed_value})

    return sourcekey, value_label, rows


def parse_history_page_xls(page_url: str, download_url: str, html_text: str) -> dict[str, object]:
    release_date, next_release_date = extract_release_dates(html_text)
    inferred_series_id, inferred_frequency = infer_history_details(page_url, download_url)
    xlrd_module = import_xlrd()
    workbook = xlrd_module.open_workbook(file_contents=fetch_bytes(download_url))

    try:
        contents_sheet = workbook.sheet_by_name("Contents")
    except Exception as exc:
        raise RuntimeError("EIA XLS workbook did not include the expected 'Contents' worksheet.") from exc

    workbook_metadata = extract_workbook_metadata(contents_sheet)
    frequency = normalize_frequency_code(workbook_metadata.get("frequency")) or inferred_frequency
    sourcekey, value_label, rows = extract_history_rows_from_workbook(workbook, xlrd_module, frequency)

    return {
        "provider": "eia",
        "page_type": "history",
        "source_url": page_url,
        "download_url": download_url,
        "title": value_label or workbook_metadata.get("title") or extract_title(html_text),
        "series_id": sourcekey or inferred_series_id,
        "frequency": frequency,
        "release_date": parse_release_date(workbook_metadata.get("release_date")) or release_date,
        "next_release_date": parse_release_date(workbook_metadata.get("next_release_date")) or next_release_date,
        "parsed_from": "xls",
        "rows": rows,
    }


def parse_history_page(page_url: str, html_text: str, download_url: str | None, source: str) -> dict[str, object]:
    if source == "html":
        return parse_history_page_html(page_url, html_text, download_url)

    if not download_url:
        if source == "xls":
            raise RuntimeError("EIA history page does not expose a linked XLS download.")
        return parse_history_page_html(page_url, html_text, download_url)

    try:
        return parse_history_page_xls(page_url, download_url, html_text)
    except RuntimeError as exc:
        if source == "xls":
            raise RuntimeError(f"EIA XLS parsing failed: {exc}") from exc
        parsed_output = parse_history_page_html(page_url, html_text, download_url)
        parsed_output["xls_fallback_reason"] = str(exc)
        return parsed_output


def save_raw_output(source_url: str, preferred_url: str | None, prefer: str, output_path: Path) -> str:
    if prefer == "xls" and preferred_url:
        output_path.write_bytes(fetch_bytes(preferred_url))
        return preferred_url
    output_path.write_text(fetch_text(source_url), encoding="utf-8")
    return source_url


def main() -> int:
    args = parse_args()
    try:
        source_url = args.page_url
        page_kind = page_type_for(source_url)
        html_text = fetch_text(source_url)
        effective_url = source_url
        redirect_url = extract_meta_refresh_url(html_text, source_url) if page_kind == "history" else None
        if redirect_url:
            effective_url = redirect_url
            html_text = fetch_text(effective_url)
        download_url = extract_download_url(html_text, effective_url)
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if args.format == "raw":
            saved_url = save_raw_output(effective_url, download_url, args.prefer, output_path)
            print(f"Saved raw EIA payload to {output_path}")
            print(f"URL: {saved_url}")
            return 0

        if page_kind == "summary":
            if args.source == "xls":
                raise ValueError("Parsed summary pages support HTML output only. Use --format raw for the linked XLS.")
            parsed_output = parse_summary_page(source_url, html_text, download_url)
        else:
            parsed_output = parse_history_page(source_url, html_text, download_url, args.source)

        output_path.write_text(json.dumps(parsed_output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed EIA artifact to {output_path}")
        print(f"page_type={page_kind} rows={len(parsed_output.get('rows', []))}")
        return 0
    except (HTTPError, URLError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
