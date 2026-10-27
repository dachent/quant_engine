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


def _provider_layout(workspace: Path) -> dict[str, tuple[Path, str]]:
    """Provider id -> (data root, catalog filename).

    Catalog filenames come from provider_ledger's own table (imported lazily, so
    the two cannot drift); the data root is the workspace directory named
    ``data<provider id>`` (case-insensitive).
    """

    import provider_ledger  # lazy: large module, only needed for workspace mode

    roots = {
        path.name.lower(): path
        for path in workspace.iterdir()
        if path.is_dir() and path.name.lower().startswith("data")
    }
    return {
        provider: (roots["data" + provider], filename)
        for provider, (
            filename,
            _versions,
        ) in provider_ledger._PROVIDER_CATALOGS.items()  # noqa: SLF001
        if "data" + provider in roots
    }


def check_ledger_generation(ledger_path: Path, workspace: Path) -> dict[str, Any]:
    """Run the gate on a bound provider-ledger generation and its provider catalogs.

    For every provider the ledger binds (`provider_generations`), the catalog is
    read from ``<workspace>/data<Provider>/generations/<generation id>/`` and its
    transactions are compared with the ledger's transactions for that provider.
    A catalog row that the ledger intentionally leaves out (dated after the
    ledger's financial as-of, or named in the ledger's own quality findings) is
    counted as excluded, not as a drop. A provider whose catalog is not on disk is
    reported as skipped, never as passed. Status is FAIL on any dropped fact,
    SKIPPED when no provider could be checked, PASS otherwise.
    """

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    as_of = str(ledger.get("as_of_date", ""))[:10]
    excluded_ids = {
        row.get("record_id") for row in ledger.get("quality_findings", [])
    } - {None}
    result: dict[str, Any] = {"providers": {}, "findings": []}
    layouts = _provider_layout(workspace)
    checked = 0
    for binding in ledger.get("provider_generations", []):
        provider = binding.get("provider_id")
        layout = layouts.get(provider)
        if layout is None:
            result["providers"][provider] = "SKIPPED_CATALOG_NOT_ON_DISK"
            continue
        catalog_path = layout[0] / "generations" / binding["generation_id"] / layout[1]
        if not catalog_path.is_file():
            result["providers"][provider] = "SKIPPED_CATALOG_NOT_ON_DISK"
            continue
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        ledger_rows = [
            row
            for row in ledger.get("transactions", [])
            if row.get("provider_id") == provider
        ]
        present = {row.get("source_record_id") for row in ledger_rows}
        retained = []
        excluded = 0
        for row in catalog.get("transactions", []):
            day = str(row.get("event_date") or row.get("effective_date") or "")[:10]
            if row.get("record_id") not in present and (
                (as_of and day > as_of) or row.get("record_id") in excluded_ids
            ):
                excluded += 1
                continue
            retained.append(row)
        findings = dropped_facts(retained, ledger_rows)
        result["findings"].extend(f"{provider}: {item}" for item in findings)
        result["providers"][provider] = (
            f"{'FAIL' if findings else 'PASS'} ({len(retained)} rows, {excluded} excluded)"
        )
        checked += 1
    result["status"] = (
        "FAIL" if result["findings"] else ("PASS" if checked else "SKIPPED")
    )
    return result


def _rows(path: Path, key: str | None) -> list[Mapping[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if key is not None:
        value = value[key]
    if not isinstance(value, list):
        raise SystemExit(f"{path}: expected a list of rows")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--catalog-key")
    parser.add_argument(
        "--ledger",
        type=Path,
        help="rows file, or a ledger generation json with --workspace",
    )
    parser.add_argument("--ledger-key")
    parser.add_argument(
        "--workspace",
        type=Path,
        help="check a bound ledger generation against provider catalogs",
    )
    args = parser.parse_args(argv)
    if args.workspace is not None:
        if args.ledger is None:
            parser.error("--workspace needs --ledger <provider-ledger.json>")
        outcome = check_ledger_generation(args.ledger, args.workspace)
        for finding in outcome["findings"]:
            print(finding, file=sys.stderr)
        for provider, state in sorted(outcome["providers"].items()):
            print(f"{provider}: {state}")
        print(f"fact-admission: {outcome['status']}")
        return 1 if outcome["status"] == "FAIL" else 0
    if args.catalog is None or args.ledger is None:
        parser.error("--catalog and --ledger are required without --workspace")
    findings = dropped_facts(
        _rows(args.catalog, args.catalog_key), _rows(args.ledger, args.ledger_key)
    )
    for finding in findings:
        print(finding, file=sys.stderr)
    print(f"fact-admission: {'FAIL' if findings else 'PASS'} ({len(findings)} dropped)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
