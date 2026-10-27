"""Gate: a provider fact present in the catalog must reach the ledger row (SRC-FACT-ADMISSION-001).

``tools/provider_ledger._project_account_facts`` copies a fixed set of source
facts (execution commission, fees, time of day, timing granularity) onto each
ledger row when the source states them. Nothing failed if a later change stopped
copying one. This check compares catalog rows with the projected ledger rows and
reports every fact the catalog states that the ledger row lacks or changed, so a
dropped fact fails a gate instead of silently disappearing.

Library use: ``dropped_facts(catalog_rows, ledger_rows)``. CLI use compares two
JSON files, each holding a list of rows (or an object and ``--*-key``), and exits 1
on any dropped fact.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sys
from typing import Any

# Must equal the field tuple copied by provider_ledger._project_account_facts;
# tests/provider_ledger/test_fact_admission_gate.py reads that tuple from the
# source and fails when the two differ.
ADMITTED_FIELDS = (
    "close_price",
    "commission",
    "event_time",
    "executed_time",
    "fees_and_commission",
    "mark_price",
    "order_reference",
    "timing_granularity",
)


def dropped_facts(
    catalog_rows: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str] = ADMITTED_FIELDS,
) -> list[str]:
    """Return one finding per catalog fact the ledger does not carry exactly."""

    by_source = {row.get("source_record_id"): row for row in ledger_rows}
    findings: list[str] = []
    for row in catalog_rows:
        record_id = row.get("record_id")
        stated = [field for field in fields if row.get(field) is not None]
        if not stated:
            continue
        ledger_row = by_source.get(record_id)
        if ledger_row is None:
            findings.append(f"{record_id}: ledger row missing for {', '.join(stated)}")
            continue
        for field in stated:
            if field not in ledger_row:
                findings.append(f"{record_id}: {field} dropped")
            elif ledger_row[field] != row[field]:
                findings.append(f"{record_id}: {field} changed")
    return findings


def _rows(path: Path, key: str | None) -> list[Mapping[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if key is not None:
        value = value[key]
    if not isinstance(value, list):
        raise SystemExit(f"{path}: expected a list of rows")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--catalog-key")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--ledger-key")
    args = parser.parse_args(argv)
    findings = dropped_facts(
        _rows(args.catalog, args.catalog_key), _rows(args.ledger, args.ledger_key)
    )
    for finding in findings:
        print(finding, file=sys.stderr)
    print(f"fact-admission: {'FAIL' if findings else 'PASS'} ({len(findings)} dropped)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
