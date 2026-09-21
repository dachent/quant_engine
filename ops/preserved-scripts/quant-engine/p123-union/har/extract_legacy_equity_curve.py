#!/usr/bin/env python3
"""
Extract equity curve CSVs from the 07-31 full generation's har_equity_curve.parquet.

Reads generation 78882cb5's equity curve table and produces one
strategy_{entity_id}_perf.csv per entity with columns:
  Date, Total Equity, Cash Added

These CSVs are placed alongside the 6 managed-strategy equity curve CSVs
in the har-workspace csv/ directory.
"""

import os
import csv
from pathlib import Path

# Paths
FULL_GEN_ROOT = Path(
    "C:/Users/BorisVaisman/V Capital/investing - Documents/"
    "_quant_engine/dataPortfolio123/generations"
    "/78882cb57a78b76a5f449ba7024533bd439d83d47c31c3184ae2062daf1b0c94"
)
CSV_DIR = Path(__file__).resolve().parent / "csv"


def main():
    os.makedirs(CSV_DIR, exist_ok=True)

    try:
        import pyarrow.parquet as pq
    except ImportError:
        # Fall back to pandas
        import pandas as pd
        df = pd.read_parquet(
            FULL_GEN_ROOT / "tables/har_equity_curve.parquet",
            columns=["_entity_id", "Date", "Total Equity", "Cash Added"],
        )
        table = df
    else:
        table = pq.read_table(
            FULL_GEN_ROOT / "tables/har_equity_curve.parquet",
            columns=["_entity_id", "Date", "Total Equity", "Cash Added"],
        ).to_pandas()

    entities = sorted(table["_entity_id"].unique())
    print(f"Found {len(entities)} entities in 07-31 full equity curve")

    written = 0
    skipped = 0
    total_rows = 0

    for entity_id in entities:
        subset = table[table["_entity_id"] == entity_id]

        # Filter out rows with empty Dates
        subset = subset[subset["Date"].notna() & (subset["Date"] != "")]
        subset = subset.sort_values("Date")

        if len(subset) == 0:
            skipped += 1
            continue

        # Skip entities that already have a perf CSV (managed strategies)
        csv_name = f"strategy_{entity_id}_perf.csv"
        csv_path = CSV_DIR / csv_name

        # Always write — the 07-31 data is authoritative for shared strategies.
        # Managed strategies are re-extracted separately from perfdata JSON.
        # If both exist (e.g. a managed strategy also appears in 07-31),
        # we keep both — the generation pipeline deduplicates rows anyway.
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Total Equity", "Cash Added"])
            for _, row in subset.iterrows():
                date_val = str(row["Date"]) if row["Date"] else ""
                equity = str(row["Total Equity"]) if row["Total Equity"] else ""
                cash = str(row["Cash Added"]) if row["Cash Added"] else "0"
                if date_val and equity:
                    writer.writerow([date_val, equity, cash])

        written += 1
        total_rows += len(subset)

    print(f"Written: {written} CSVs, Skipped: {skipped}, Total rows: {total_rows}")


if __name__ == "__main__":
    main()
