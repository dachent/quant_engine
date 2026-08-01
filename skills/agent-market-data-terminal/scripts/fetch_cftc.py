#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_AGENT = "Codex agent-market-data-terminal/1.0"

ANNUAL_PATTERNS: dict[str, str] = {
    "legacy_futures_only": "https://www.cftc.gov/files/dea/history/deacot{year}.zip",
    "legacy_combined": "https://www.cftc.gov/files/dea/history/dea_fut_xls_{year}.zip",
    "disaggregated_futures_only": "https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_{year}.zip",
    "disaggregated_combined": "https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_{year}.zip",
    "tff_futures_only": "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip",
    "tff_combined": "https://www.cftc.gov/files/dea/history/com_fin_txt_{year}.zip",
    "supplemental_cit": "https://www.cftc.gov/files/dea/history/dea_cit_txt_{year}.zip",
}

BULK_URLS: dict[str, str] = {
    "legacy_futures_only": "https://www.cftc.gov/files/dea/history/deacot1986_2016.zip",
    "disaggregated_futures_only": "https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_2006_2016.zip",
    "disaggregated_combined": "https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_2006_2016.zip",
    "tff_futures_only": "https://www.cftc.gov/files/dea/history/fin_fut_txt_2006_2016.zip",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch CFTC Commitments of Traders archives.")
    parser.add_argument(
        "--family",
        choices=sorted(ANNUAL_PATTERNS.keys()),
        default="legacy_futures_only",
        help="COT report family. Defaults to legacy_futures_only.",
    )
    parser.add_argument("--year", type=int, help="Annual archive year. Required unless --bulk is set.")
    parser.add_argument(
        "--bulk",
        action="store_true",
        help="Request the multi-year bulk archive when one exists for the chosen family.",
    )
    parser.add_argument("--url", help="Explicit override URL.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def resolve_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if args.bulk:
        bulk = BULK_URLS.get(args.family)
        if not bulk:
            raise ValueError(f"No bulk archive registered for family {args.family!r}.")
        return bulk
    if args.year is None:
        raise ValueError("--year is required unless --bulk or --url is set.")
    pattern = ANNUAL_PATTERNS.get(args.family)
    if not pattern:
        raise ValueError(f"No annual archive pattern registered for family {args.family!r}.")
    return pattern.format(year=args.year)


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        return response.read()


def select_data_member(members: list[str]) -> str:
    text_candidates = [m for m in members if m.lower().endswith((".txt", ".csv"))]
    if text_candidates:
        return sorted(text_candidates, key=len)[0]
    return members[0]


def parse_archive(payload: bytes, source_url: str, family: str) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.namelist()
        if not members:
            raise RuntimeError("CFTC archive contained no members.")
        selected = select_data_member(members)
        with archive.open(selected) as fp:
            raw = fp.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(text.splitlines())
    all_rows = [row for row in reader if row]
    if not all_rows:
        raise RuntimeError(f"CFTC member {selected!r} contained no rows.")

    header = [cell.strip() for cell in all_rows[0]]
    data_rows: list[dict[str, str]] = []
    for cells in all_rows[1:]:
        record: dict[str, str] = {}
        for index, column_name in enumerate(header):
            if index >= len(cells):
                continue
            key = column_name or f"column_{index + 1}"
            record[key] = cells[index]
        data_rows.append(record)

    return {
        "provider": "cftc",
        "source_url": source_url,
        "family": family,
        "archive_members": members,
        "selected_member": selected,
        "header": header,
        "rows": data_rows,
        "row_count": len(data_rows),
    }


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        source_url = resolve_url(args)
        payload = fetch_bytes(source_url)
        if args.format == "raw":
            output_path.write_bytes(payload)
            print(f"Saved raw CFTC archive to {output_path}")
            print(f"URL: {source_url}")
            return 0

        artifact = parse_archive(payload, source_url, args.family)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed CFTC artifact to {output_path}")
        print(f"member={artifact['selected_member']} rows={artifact['row_count']}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
