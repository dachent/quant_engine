"""Assemble the strategy-attribution return source from pinned inputs.

`portfolio_attribution.py` reads one CSV of reference series
(`series_id,date,return,evidence_layer,version_ref,source_artifact_id`) pinned
by sha256 in the attribution spec. The retained CSV was a one-off built on
2026-08-07 by a script that was never committed, so nothing could refresh a
series when its source grew (a stale comparison window was the visible result).

This tool rebuilds the file from a spec. Every input is named with its sha256
and refused if the bytes differ; every series states its own evidence layer,
version reference and source label; series carried unchanged from an earlier
source are copied row for row. A member can name a carried series it replaces
and the tool records how the new source differs from the old one on the shared
dates (count, maximum difference, number beyond a threshold) so a vintage
change is disclosed rather than silent.

Member kinds (all data in the spec, none in code):

* ``carry``: rows of listed series copied verbatim from an earlier source CSV.
* ``csv_series``: a return column of a CSV, optionally filtered by column
  values, published as one series.
* ``equity_curve``: daily returns from a total-equity column adjusted for a
  cash-added column (``(equity - added) / previous equity - 1``).
* ``xlsx_column``: a date column and a return column of one worksheet.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Any, Final


OUTPUT_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "version_ref",
    "source_artifact_id",
)
EXCEL_EPOCH: Final = date(1899, 12, 30)
DIFFERENCE_THRESHOLD: Final = Decimal("0.000001")


class AttributionReturnsError(ValueError):
    """The spec or one of its pinned inputs violates the source contract."""


def _pinned(entry: dict[str, Any], label: str) -> tuple[Path, bytes]:
    path = Path(str(entry.get("path") or ""))
    if path.is_symlink() or not path.is_file():
        raise AttributionReturnsError(f"{label} is not a regular file: {path}")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
        raise AttributionReturnsError(f"{label} differs from its pinned sha256")
    return path, content


def _decimal(value: object, label: str) -> Decimal:
    text = str(value).replace(",", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise AttributionReturnsError(f"{label} is not numeric: {value!r}") from exc
    if not number.is_finite():
        raise AttributionReturnsError(f"{label} is not finite")
    return number


def _day(value: object, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return EXCEL_EPOCH + timedelta(days=int(value))
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise AttributionReturnsError(f"{label} is not a date: {value!r}") from exc


def _identity(member: dict[str, Any]) -> tuple[str, str, str, str]:
    values = [
        str(member.get(key) or "")
        for key in ("series_id", "evidence_layer", "version_ref", "source_artifact_id")
    ]
    if not all(values):
        raise AttributionReturnsError("a member lacks series_id or identity fields")
    return values[0], values[1], values[2], values[3]


def _csv_rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def _carry(member: dict[str, Any], content: bytes) -> list[dict[str, str]]:
    wanted = [str(item) for item in member.get("series_ids") or []]
    rows = [row for row in _csv_rows(content) if row["series_id"] in wanted]
    missing = set(wanted) - {row["series_id"] for row in rows}
    if missing:
        raise AttributionReturnsError(f"carried series are absent: {sorted(missing)}")
    return rows


def _csv_series(member: dict[str, Any], content: bytes) -> list[dict[str, str]]:
    """One series from a CSV column, or the equal-weight mean of several columns.

    A row where any named column is empty is skipped: a composite is defined
    only where every component has a return. Component weights are equal and
    reset every day (a daily-rebalanced composite).
    """

    series_id, layer, version, source = _identity(member)
    where = member.get("where") or {}
    columns = member.get("value_columns") or [member["value_column"]]
    out = []
    for row in _csv_rows(content):
        if any(row.get(key) != value for key, value in where.items()):
            continue
        cells = [row.get(column, "") for column in columns]
        if any(str(cell).strip() == "" for cell in cells):
            continue
        values = [_decimal(cell, series_id) for cell in cells]
        out.append(
            {
                "series_id": series_id,
                "date": _day(row[member["date_column"]], series_id).isoformat(),
                "return": format(sum(values) / len(values), "f"),
                "evidence_layer": layer,
                "version_ref": version,
                "source_artifact_id": source,
            }
        )
    return out


def _equity_curve(member: dict[str, Any], content: bytes) -> list[dict[str, str]]:
    series_id, layer, version, source = _identity(member)
    rows = _csv_rows(content)
    out = []
    for previous, current in zip(rows, rows[1:], strict=False):
        before = _decimal(previous[member["equity_column"]], series_id)
        after = _decimal(current[member["equity_column"]], series_id)
        added = _decimal(current[member["flow_column"]], series_id)
        if before <= 0:
            raise AttributionReturnsError(f"{series_id}: equity is not positive")
        out.append(
            {
                "series_id": series_id,
                "date": _day(current[member["date_column"]], series_id).isoformat(),
                "return": format((after - added) / before - 1, "f"),
                "evidence_layer": layer,
                "version_ref": version,
                "source_artifact_id": source,
            }
        )
    return out


def _xlsx_column(member: dict[str, Any], content: bytes) -> list[dict[str, str]]:
    import openpyxl  # type: ignore[import-untyped]  # noqa: PLC0415

    series_id, layer, version, source = _identity(member)
    workbook = openpyxl.load_workbook(
        io.BytesIO(content), read_only=True, data_only=True
    )
    sheet = workbook[str(member["sheet"])]
    date_index = int(member["date_column"]) - 1
    value_index = int(member["value_column"]) - 1
    out = []
    for row in sheet.iter_rows(values_only=True):
        if len(row) <= max(date_index, value_index):
            continue
        stamp, value = row[date_index], row[value_index]
        if not isinstance(stamp, (datetime, date)) or not isinstance(
            value, (int, float)
        ):
            continue
        out.append(
            {
                "series_id": series_id,
                "date": _day(stamp, series_id).isoformat(),
                "return": format(Decimal(repr(float(value))), "f"),
                "evidence_layer": layer,
                "version_ref": version,
                "source_artifact_id": source,
            }
        )
    return out


_KINDS: Final = {
    "carry": _carry,
    "csv_series": _csv_series,
    "equity_curve": _equity_curve,
    "xlsx_column": _xlsx_column,
}


def _compare(
    new_rows: list[dict[str, str]], old_rows: list[dict[str, str]]
) -> dict[str, Any]:
    old = {r["date"]: Decimal(r["return"]) for r in old_rows}
    new = {r["date"]: Decimal(r["return"]) for r in new_rows}
    common = sorted(set(old) & set(new))
    diffs = [abs(old[d] - new[d]) for d in common]
    return {
        "common_dates": len(common),
        "dates_only_in_new": len(set(new) - set(old)),
        "dates_only_in_old": len(set(old) - set(new)),
        "dates_with_difference_over_threshold": sum(
            1 for d in diffs if d > DIFFERENCE_THRESHOLD
        ),
        "difference_threshold": format(DIFFERENCE_THRESHOLD, "f"),
        "max_abs_difference": format(max(diffs), "f") if diffs else None,
    }


def assemble(spec_path: Path, output_dir: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_bytes())
    members = spec.get("members")
    if not isinstance(members, list) or not members:
        raise AttributionReturnsError("spec members differ")
    as_of = _day(spec.get("as_of_date"), "as_of_date").isoformat()
    inputs: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, str]] = []
    reports: list[dict[str, Any]] = []
    for member in members:
        kind = str(member.get("kind"))
        if kind not in _KINDS:
            raise AttributionReturnsError(f"unknown member kind: {kind}")
        path, content = _pinned(member, f"{kind} input")
        inputs[str(path)] = {
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        produced = _KINDS[kind](member, content)
        report: dict[str, Any] = {
            "kind": kind,
            "input_sha256": inputs[str(path)]["sha256"],
        }
        if kind != "carry" and not produced:
            raise AttributionReturnsError(f"{member.get('series_id')}: no rows")
        reports.append(report)
        rows.extend(produced)
    # Disclosure: the new member against the row set it replaces.
    by_series: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_series.setdefault(row["series_id"], []).append(row)
    for member, report in zip(members, reports, strict=True):
        replaced = member.get("replaces_from")
        if replaced is not None:
            old_path, old_content = _pinned(replaced, "replaced source")
            old = [
                r
                for r in _csv_rows(old_content)
                if r["series_id"] == replaced["series_id"]
            ]
            new = by_series[str(member["series_id"])]
            report["replaced_series_id"] = replaced["series_id"]
            report["difference_from_replaced"] = _compare(new, old)
            limit = member.get("max_abs_difference")
            if (
                limit is not None
                and report["difference_from_replaced"]["max_abs_difference"]
                not in (None,)
                and Decimal(report["difference_from_replaced"]["max_abs_difference"])
                > Decimal(str(limit))
            ):
                raise AttributionReturnsError(
                    f"{member['series_id']}: differs from the replaced series beyond "
                    f"{limit}"
                )
            inputs[str(old_path)] = {
                "path": str(old_path),
                "sha256": hashlib.sha256(old_content).hexdigest(),
                "size_bytes": len(old_content),
            }
    ordered: list[dict[str, str]] = []
    seen_series: list[str] = []
    for row in rows:
        if row["series_id"] not in seen_series:
            seen_series.append(row["series_id"])
    for series_id in seen_series:
        series_rows = sorted(by_series[series_id], key=lambda r: r["date"])
        dates = [r["date"] for r in series_rows]
        if len(dates) != len(set(dates)):
            raise AttributionReturnsError(f"{series_id}: duplicate dates")
        identities = {
            (r["evidence_layer"], r["version_ref"], r["source_artifact_id"])
            for r in series_rows
        }
        if len(identities) != 1:
            raise AttributionReturnsError(
                f"{series_id}: identity changes within series"
            )
        ordered.extend(series_rows)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(ordered)
    csv_bytes = buffer.getvalue().encode("utf-8")
    summary = []
    for series_id in seen_series:
        dates = [r["date"] for r in ordered if r["series_id"] == series_id]
        summary.append(
            {
                "series_id": series_id,
                "rows": len(dates),
                "first_date": dates[0],
                "last_date": dates[-1],
            }
        )
    manifest: dict[str, Any] = {
        "artifacts": sorted(inputs.values(), key=lambda item: item["path"]),
        "as_of_date": as_of,
        "derived": {
            "path": "attribution-returns.csv",
            "row_count": len(ordered),
            "series_count": len(seen_series),
            "sha256": hashlib.sha256(csv_bytes).hexdigest(),
        },
        "member_reports": reports,
        "method": str(spec.get("method") or ""),
        "series": summary,
        "verification": "VERIFIED",
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("attribution-returns.csv", csv_bytes),
        ("source-manifest.json", manifest_bytes),
    ):
        target = output_dir / name
        if target.exists() and target.read_bytes() != content:
            raise AttributionReturnsError(f"{name} exists with different bytes")
        target.write_bytes(content)
    return {
        "attribution_returns_sha256": manifest["derived"]["sha256"],
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "series": summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = assemble(args.spec, args.output_dir)
    except (AttributionReturnsError, OSError, KeyError) as exc:
        print(f"portfolio-attribution-returns: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
