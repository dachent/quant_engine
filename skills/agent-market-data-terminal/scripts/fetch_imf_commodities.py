#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HEADLINE_URL = "https://www.imf.org/external/np/res/commod/External_Data.xls"
USER_AGENT = "Codex agent-market-data-terminal/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch the IMF Primary Commodity Prices workbook.")
    parser.add_argument(
        "--url",
        default=HEADLINE_URL,
        help="IMF commodity-prices workbook URL. Defaults to the headline External_Data.xls path.",
    )
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    parser.add_argument(
        "--sheet",
        help="Optional sheet name override. Parsed mode auto-selects the monthly-prices panel when omitted.",
    )
    return parser.parse_args()


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        return response.read()


def select_sheet_name(sheet_names: list[str], override: str | None) -> str:
    if override:
        if override in sheet_names:
            return override
        raise RuntimeError(f"--sheet {override!r} not found in workbook. Sheets: {sheet_names}")
    candidates = [name for name in sheet_names if "month" in name.lower() and "price" in name.lower()]
    if candidates:
        return candidates[0]
    candidates = [name for name in sheet_names if "month" in name.lower()]
    if candidates:
        return candidates[0]
    return sheet_names[0]


def parse_workbook(payload: bytes, source_url: str, override_sheet: str | None) -> dict[str, object]:
    try:
        import xlrd  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Parsed IMF Commodities mode requires xlrd. Install with `pip install xlrd`.") from exc

    book = xlrd.open_workbook(file_contents=payload)
    sheet_names = book.sheet_names()
    selected = select_sheet_name(sheet_names, override_sheet)
    sheet = book.sheet_by_name(selected)

    rows: list[list[object]] = []
    for row_index in range(sheet.nrows):
        row = []
        for col_index in range(sheet.ncols):
            cell = sheet.cell(row_index, col_index)
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    parsed_date = xlrd.xldate_as_datetime(cell.value, book.datemode)
                    row.append(parsed_date.date().isoformat())
                except Exception:
                    row.append(cell.value)
            elif cell.ctype == xlrd.XL_CELL_NUMBER:
                value = float(cell.value)
                row.append(int(value) if value.is_integer() else value)
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                row.append(bool(cell.value))
            elif cell.ctype == xlrd.XL_CELL_EMPTY:
                row.append(None)
            else:
                row.append(str(cell.value))
        rows.append(row)

    header_row_index = 0
    for index, row in enumerate(rows[:10]):
        non_empty = [cell for cell in row if cell not in (None, "")]
        if len(non_empty) >= 3:
            header_row_index = index
            break

    header = [str(cell) if cell is not None else f"column_{idx + 1}" for idx, cell in enumerate(rows[header_row_index])]
    data_rows: list[dict[str, object]] = []
    for row in rows[header_row_index + 1 :]:
        if not row or all(cell in (None, "") for cell in row):
            continue
        record: dict[str, object] = {}
        for idx, column_name in enumerate(header):
            if idx >= len(row):
                continue
            record[column_name] = row[idx]
        data_rows.append(record)

    return {
        "provider": "imf_commodities",
        "source_url": source_url,
        "sheet_names": sheet_names,
        "selected_sheet": selected,
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
            print(f"Saved raw IMF Commodities payload to {output_path}")
            print(f"URL: {args.url}")
            return 0

        artifact = parse_workbook(payload, args.url, args.sheet)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed IMF Commodities artifact to {output_path}")
        print(f"sheet={artifact['selected_sheet']} rows={artifact['row_count']}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
