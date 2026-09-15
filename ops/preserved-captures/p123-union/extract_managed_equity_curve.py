#!/usr/bin/env python3
"""
Extract equity curve CSVs from managed strategy perfdata JSONs.

Managed strategy app/investment/perfdata JSON format:
  {"date": "2026-01-28", "dateLast": "2026-08-13",
   "data": [[code, equity_value, cash_added], ...]}

Code 0 = inception, code 1 = daily, code 3 = weekly mark.
ALL rows (including weekly) represent sequential business days.
This script extracts code=1 (daily) rows only and maps them to
consecutive business days starting from the inception date.

Output CSV: strategy_{id}_perf.csv (matches generation.py _perf token
→ equity_curve family → har_equity_curve.parquet).
Columns: Date, Total Equity, Cash Added
"""

import json
import os
import csv
import re
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESPONSES_DIR = os.path.join(SCRIPT_DIR, "json", "p123_api_responses")
CSV_DIR = os.path.join(SCRIPT_DIR, "csv")


def is_business_day(d: datetime) -> bool:
    return d.weekday() < 5


def extract_equity_curve(json_path: str, strategy_id: str) -> tuple[str, int] | None:
    """Extract daily equity curve CSV from managed perfdata JSON."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "data" not in data:
        return None

    rows_raw = data["data"]
    start_str = data.get("date", "")

    if not start_str:
        return None

    current_date = datetime.strptime(start_str, "%Y-%m-%d")

    # Advance to first business day if start is a weekend
    while not is_business_day(current_date):
        current_date += timedelta(days=1)

    # Walk through rows: every row advances one business day.
    # We only keep code=1 (daily) rows.
    result_rows = []
    for code, equity, cash in rows_raw:
        if code == 1:
            result_rows.append((current_date.strftime("%Y-%m-%d"), equity, cash))
        # Advance to next business day
        current_date += timedelta(days=1)
        while not is_business_day(current_date):
            current_date += timedelta(days=1)

    if not result_rows:
        return None

    csv_name = f"strategy_{strategy_id}_perf.csv"
    csv_path = os.path.join(CSV_DIR, csv_name)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Total Equity", "Cash Added"])
        for date_str, equity, cash in result_rows:
            writer.writerow([date_str, str(equity), str(cash)])

    return csv_path, len(result_rows)


def find_best_perfdata(strategy_id: str) -> str | None:
    """Pick the perfdata JSON with the most data rows for a strategy."""
    candidates = []
    for f in os.listdir(RESPONSES_DIR):
        if f.startswith(f"strategy_{strategy_id}_app_investment_perfdata"):
            candidates.append(os.path.join(RESPONSES_DIR, f))

    if not candidates:
        return None

    best = None
    best_rows = 0
    for c in candidates:
        try:
            with open(c, "r", encoding="utf-8") as f:
                d = json.load(f)
            rows = len(d.get("data", []))
            if rows > best_rows:
                best_rows = rows
                best = c
        except (json.JSONDecodeError, OSError):
            continue

    return best


def main():
    os.makedirs(CSV_DIR, exist_ok=True)

    strategy_ids = set()
    for f in os.listdir(RESPONSES_DIR):
        m = re.match(r"strategy_(\d+)_app_investment_perfdata", f)
        if m:
            strategy_ids.add(m.group(1))

    if not strategy_ids:
        print("No managed strategy perfdata JSONs found.")
        return

    print(f"Found {len(strategy_ids)} managed strategies with perfdata:")
    total_rows = 0

    for sid in sorted(strategy_ids):
        json_path = find_best_perfdata(sid)
        if json_path is None:
            print(f"  {sid}: NO perfdata JSON found")
            continue

        result = extract_equity_curve(json_path, sid)
        if result is None:
            print(f"  {sid}: FAILED to extract equity curve")
            continue

        csv_path, rows = result
        total_rows += rows
        print(f"  {sid}: {os.path.basename(csv_path)} ({rows} rows)")

    print(f"\nTotal equity curve rows: {total_rows}")


if __name__ == "__main__":
    main()
