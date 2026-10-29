from __future__ import annotations

import csv
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any

import openpyxl  # type: ignore[import-untyped]
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_attribution_returns.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_attribution_returns", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load the attribution return-source assembler")
assembler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = assembler
SPEC.loader.exec_module(assembler)
Error = assembler.AttributionReturnsError
FIELDS = list(assembler.OUTPUT_FIELDS)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(buffer.getvalue().encode("utf-8"))
    return path


def _old_source(tmp_path: Path) -> Path:
    rows = []
    for series, base in (("account:a", "0.01"), ("provider:p", "0.02")):
        for day in ("2024-01-02", "2024-01-03", "2024-01-04"):
            rows.append(
                {
                    "series_id": series,
                    "date": day,
                    "return": base,
                    "evidence_layer": "OLD",
                    "version_ref": "old-v",
                    "source_artifact_id": "old-s",
                }
            )
    return _write_csv(tmp_path / "old.csv", rows, FIELDS)


def _perf(tmp_path: Path) -> Path:
    rows = [
        {"Date": "2024-01-01", "Total Equity": "1,000.00", "Cash Added": "0.00"},
        {"Date": "2024-01-02", "Total Equity": "1,020.00", "Cash Added": "0.00"},
        {"Date": "2024-01-03", "Total Equity": "1,140.40", "Cash Added": "100.00"},
        {"Date": "2024-01-04", "Total Equity": "1,151.804", "Cash Added": "0.00"},
    ]
    return _write_csv(
        tmp_path / "perf.csv", rows, ["Date", "Total Equity", "Cash Added"]
    )


def _styles(tmp_path: Path) -> Path:
    rows = [
        {"date": "2024-01-02", "AAA": "0.01", "BBB": "0.03"},
        {"date": "2024-01-03", "AAA": "", "BBB": "0.05"},
        {"date": "2024-01-04", "AAA": "0.02", "BBB": "0.04"},
    ]
    return _write_csv(tmp_path / "styles.csv", rows, ["date", "AAA", "BBB"])


def _workbook(tmp_path: Path) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "data"
    sheet.append(["header", None])
    import datetime as dt

    sheet.append([dt.datetime(2024, 1, 2), 0.001])
    sheet.append([dt.datetime(2024, 1, 3), 0.002])
    sheet.append(["not a date", 0.5])
    path = tmp_path / "book.xlsx"
    workbook.save(path)
    return path


def _spec(tmp_path: Path) -> dict[str, Any]:
    old, perf, styles, book = (
        _old_source(tmp_path),
        _perf(tmp_path),
        _styles(tmp_path),
        _workbook(tmp_path),
    )
    identity = {"evidence_layer": "L", "version_ref": "v", "source_artifact_id": "s"}
    return {
        "as_of_date": "2024-02-01",
        "method": "fixture",
        "members": [
            {
                "kind": "carry",
                "path": str(old),
                "sha256": _sha(old),
                "series_ids": ["account:a"],
            },
            {
                "kind": "equity_curve",
                "path": str(perf),
                "sha256": _sha(perf),
                "date_column": "Date",
                "equity_column": "Total Equity",
                "flow_column": "Cash Added",
                "series_id": "provider:p",
                **identity,
                "replaces_from": {
                    "path": str(old),
                    "sha256": _sha(old),
                    "series_id": "provider:p",
                },
            },
            {
                "kind": "csv_series",
                "path": str(styles),
                "sha256": _sha(styles),
                "date_column": "date",
                "value_columns": ["AAA", "BBB"],
                "series_id": "benchmark:mix",
                **identity,
            },
            {
                "kind": "xlsx_column",
                "path": str(book),
                "sha256": _sha(book),
                "sheet": "data",
                "date_column": 1,
                "value_column": 2,
                "series_id": "provider:x",
                **identity,
            },
        ],
    }


def _run(
    tmp_path: Path, spec: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    out = tmp_path / "out"
    result = assembler.assemble(spec_path, out)
    rows = list(
        csv.DictReader(io.StringIO((out / "attribution-returns.csv").read_text()))
    )
    return result, rows


def _series(rows: list[dict[str, str]], series_id: str) -> dict[str, Decimal]:
    return {
        r["date"]: Decimal(r["return"]) for r in rows if r["series_id"] == series_id
    }


def test_assembles_every_member_kind_with_the_governed_columns(tmp_path: Path) -> None:
    result, rows = _run(tmp_path, _spec(tmp_path))
    assert list(rows[0]) == FIELDS
    assert _series(rows, "account:a") == {
        d: Decimal("0.01") for d in ("2024-01-02", "2024-01-03", "2024-01-04")
    }
    equity = _series(rows, "provider:p")
    assert equity["2024-01-02"] == Decimal("0.02")
    assert equity["2024-01-03"] == Decimal("0.02")  # (1140.40 - 100) / 1020 - 1
    assert abs(equity["2024-01-04"] - Decimal("0.01")) < Decimal("1e-12")
    mix = _series(rows, "benchmark:mix")
    assert mix == {"2024-01-02": Decimal("0.02"), "2024-01-04": Decimal("0.03")}
    assert _series(rows, "provider:x") == {
        "2024-01-02": Decimal("0.001"),
        "2024-01-03": Decimal("0.002"),
    }
    assert [s["series_id"] for s in result["series"]][:2] == ["account:a", "provider:p"]


def test_manifest_pins_inputs_and_discloses_the_difference_from_a_replaced_series(
    tmp_path: Path,
) -> None:
    _run(tmp_path, _spec(tmp_path))
    manifest = json.loads((tmp_path / "out" / "source-manifest.json").read_text())
    assert manifest["verification"] == "VERIFIED"
    assert manifest["derived"]["sha256"] == _sha(
        tmp_path / "out" / "attribution-returns.csv"
    )
    reports = {
        r.get("replaced_series_id"): r["difference_from_replaced"]
        for r in manifest["member_reports"]
        if "difference_from_replaced" in r
    }
    diff = reports["provider:p"]
    assert diff["common_dates"] == 3
    assert diff["dates_with_difference_over_threshold"] == 1  # 2024-01-04: 0.02 vs 0.01
    assert Decimal(diff["max_abs_difference"]) == Decimal("0.01")
    assert {a["sha256"] for a in manifest["artifacts"]} >= {_sha(tmp_path / "old.csv")}


def test_refuses_an_input_whose_bytes_differ_from_the_pin(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    (tmp_path / "styles.csv").write_bytes(b"date,AAA,BBB\n2024-01-02,0.5,0.5\n")
    with pytest.raises(Error, match="pinned sha256"):
        _run(tmp_path, spec)


def test_refuses_a_replacement_that_moves_beyond_the_declared_limit(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec["members"][1]["max_abs_difference"] = "0.001"
    with pytest.raises(Error, match="differs from the replaced series"):
        _run(tmp_path, spec)


def test_refuses_duplicate_dates_and_changing_identity(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    duplicate = dict(spec["members"][2])
    duplicate["series_id"] = "account:a"
    spec["members"].append(duplicate)
    with pytest.raises(Error, match="duplicate dates|identity changes"):
        _run(tmp_path, spec)


def test_refuses_carrying_a_series_the_source_lacks_and_unknown_kinds(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec["members"][0]["series_ids"] = ["account:missing"]
    with pytest.raises(Error, match="absent"):
        _run(tmp_path, spec)
    spec = _spec(tmp_path)
    spec["members"][0]["kind"] = "magic"
    with pytest.raises(Error, match="unknown member kind"):
        _run(tmp_path, spec)


def test_restating_is_byte_identical_and_refuses_a_conflicting_existing_output(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    first, _rows = _run(tmp_path, spec)
    second, _rows = _run(tmp_path, spec)
    assert first == second
    (tmp_path / "out" / "attribution-returns.csv").write_bytes(b"x")
    with pytest.raises(Error, match="different bytes"):
        _run(tmp_path, spec)
