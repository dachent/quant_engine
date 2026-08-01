#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen


BASE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
USER_AGENT = "Codex agent-market-data-terminal/1.0"
MISSING_SENTINELS = {"-99.99", "-999", "-999.0", "-999.00"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch structured Kenneth French Data Library CSV zip feeds.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--feed-id", help="CSV zip feed stem such as F-F_Research_Data_Factors_CSV.")
    source_group.add_argument("--url", help="Direct Kenneth French _CSV.zip URL, including archive URLs.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def normalize_feed_id(feed_id: str) -> str:
    value = feed_id.strip()
    if not value:
        raise ValueError("--feed-id must not be empty.")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError("--feed-id must be a single zip stem, not a path.")
    if value.lower().endswith(".zip"):
        value = value[:-4]
    if value.upper().endswith("_TXT"):
        raise ValueError("TXT feeds are not supported. Use the CSV zip stem ending in _CSV.")
    if not value.upper().endswith("_CSV"):
        raise ValueError("--feed-id must be the exact CSV zip stem ending in _CSV.")
    return value


def validate_direct_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "mba.tuck.dartmouth.edu":
        raise ValueError("Fama/French URLs must use https://mba.tuck.dartmouth.edu/ direct zip paths.")

    path = parsed.path or ""
    lower_path = path.lower()
    if lower_path.endswith("_txt.zip"):
        raise ValueError("TXT feeds are not supported. Provide a direct _CSV.zip URL instead.")
    if lower_path.endswith(".html") or lower_path.endswith("/"):
        raise ValueError("HTML pages are not supported. Provide a direct _CSV.zip URL instead.")
    if not lower_path.endswith("_csv.zip"):
        raise ValueError("Fama/French access is restricted to direct structured _CSV.zip URLs.")
    if "/pages/faculty/ken.french/" not in path:
        raise ValueError("URL must point to a Kenneth French Data Library zip feed.")
    encoded_path = quote(path, safe="/%._-()")
    return urlunparse((parsed.scheme, parsed.netloc, encoded_path, parsed.params, parsed.query, parsed.fragment))


def build_source_url(args: argparse.Namespace) -> tuple[str, str]:
    if args.feed_id:
        feed_id = normalize_feed_id(args.feed_id)
        return f"{BASE_URL}/{quote(feed_id)}.zip", feed_id

    source_url = validate_direct_url(str(args.url))
    return source_url, Path(urlparse(source_url).path).stem


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def is_header_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(",") and "," in stripped


def split_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        raw_line = line.rstrip("\r")
        if raw_line.strip():
            current.append(raw_line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def split_csv_line(line: str) -> list[str]:
    return next(csv.reader([line]))


def parse_number(value: str) -> int | float | None:
    cleaned = value.strip()
    if cleaned == "":
        return None
    if cleaned in MISSING_SENTINELS:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number.is_integer():
        return int(number)
    return number


def parse_value(value: str) -> object:
    number = parse_number(value)
    if number is not None or value.strip() in MISSING_SENTINELS or value.strip() == "":
        return number
    return clean_text(value)


def normalize_columns(header_cells: list[str]) -> list[str]:
    columns: list[str] = []
    for index, cell in enumerate(header_cells):
        cleaned = clean_text(cell)
        if index == 0:
            columns.append("period")
        elif cleaned:
            columns.append(cleaned)
        else:
            columns.append(f"column_{index + 1}")
    return columns


def infer_title(period_token: str | None, feed_id: str, section_index: int) -> str:
    feed_lower = feed_id.lower()
    token = (period_token or "").strip()
    if re.fullmatch(r"\d{8}", token):
        return "Weekly" if "weekly" in feed_lower else "Daily"
    if re.fullmatch(r"\d{6}", token):
        return "Monthly"
    if re.fullmatch(r"\d{4}", token):
        return "Annual"
    if "weekly" in feed_lower:
        return "Weekly"
    if "daily" in feed_lower:
        return "Daily"
    if "annual" in feed_lower:
        return "Annual"
    return f"Section {section_index}"


def looks_like_title_block(block: list[str], block_text: str) -> bool:
    if not block_text:
        return False
    if len(block) > 2 or len(block_text) > 180:
        return False
    if any(line.strip().endswith(".") for line in block):
        return False
    return True


def parse_table_block(block: list[str], *, pending_title: str | None, feed_id: str, section_index: int) -> dict[str, object]:
    header_index = next((index for index, line in enumerate(block) if is_header_line(line)), None)
    if header_index is None:
        raise RuntimeError("Expected a table header inside a Fama/French section block.")

    title_lines = [clean_text(line) for line in block[:header_index] if clean_text(line)]
    title = " ".join(title_lines) if title_lines else pending_title

    header_cells = split_csv_line(block[header_index])
    columns = normalize_columns(header_cells)
    rows: list[dict[str, object]] = []

    for line in block[header_index + 1 :]:
        if "," not in line:
            continue
        cells = split_csv_line(line)
        if not cells:
            continue
        period_token = clean_text(cells[0])
        if not period_token:
            continue
        if len(cells) > len(columns):
            for column_index in range(len(columns), len(cells)):
                columns.append(f"column_{column_index + 1}")
        row: dict[str, object] = {}
        for index, column in enumerate(columns):
            cell_value = cells[index] if index < len(cells) else ""
            if index == 0:
                row[column] = period_token
            else:
                row[column] = parse_value(cell_value)
        rows.append(row)

    if not rows:
        raise RuntimeError("A Fama/French section did not contain any data rows.")

    return {
        "title": title or infer_title(str(rows[0].get("period")), feed_id, section_index),
        "columns": columns,
        "rows": rows,
    }


def parse_csv_member(text: str, feed_id: str) -> tuple[list[str], list[dict[str, object]]]:
    blocks = split_blocks(text)
    preamble: list[str] = []
    sections: list[dict[str, object]] = []
    pending_title: str | None = None

    for index, block in enumerate(blocks):
        has_header = any(is_header_line(line) for line in block)
        if not has_header:
            block_text = " ".join(clean_text(line) for line in block if clean_text(line))
            if not block_text:
                continue
            next_has_header = index + 1 < len(blocks) and any(is_header_line(line) for line in blocks[index + 1])
            if next_has_header and looks_like_title_block(block, block_text):
                pending_title = block_text
            elif not sections:
                preamble.append(block_text)
            continue

        sections.append(parse_table_block(block, pending_title=pending_title, feed_id=feed_id, section_index=len(sections) + 1))
        pending_title = None

    if not sections:
        raise RuntimeError("Could not find any structured table sections inside the Fama/French CSV feed.")

    return preamble, sections


def load_zip_payload(raw_bytes: bytes) -> tuple[list[str], str, str]:
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
        zip_members = archive.namelist()
        csv_members = [member for member in zip_members if member.lower().endswith(".csv")]
        if not csv_members:
            raise RuntimeError("The Fama/French zip payload did not contain a CSV member.")
        csv_member = csv_members[0]
        text = archive.read(csv_member).decode("utf-8-sig", errors="replace")
    return zip_members, csv_member, text


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        source_url, feed_id = build_source_url(args)
        raw_bytes = fetch_bytes(source_url)

        if args.format == "raw":
            output_path.write_bytes(raw_bytes)
            print(f"Saved raw Fama/French payload to {output_path}")
            print(f"URL: {source_url}")
            return 0

        zip_members, csv_member, text = load_zip_payload(raw_bytes)
        preamble, sections = parse_csv_member(text, feed_id)
        artifact = {
            "provider": "famafrench",
            "source_url": source_url,
            "feed_id": feed_id,
            "zip_members": zip_members,
            "csv_member": csv_member,
            "preamble": preamble,
            "sections": sections,
        }
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed Fama/French artifact to {output_path}")
        print(f"sections={len(sections)}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
