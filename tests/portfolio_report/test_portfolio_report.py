from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_report.py"
SPEC = importlib.util.spec_from_file_location("portfolio_report", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio report builder")
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _baseline_catalog() -> dict[str, Any]:
    strategies = [
        {
            "aliases": [f"Synthetic Strategy {index}"],
            "cost_status": "KNOWN" if index == 1 else "UNAVAILABLE",
            "coverage_status": "PARTIAL",
            "economic_family": f"SYNTHETIC FAMILY {index}",
            "evidence_routes": [
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "purpose": "SYNTHETIC evidence",
                    "source_id": "synthetic-source",
                }
            ],
            "gaps": ["SYNTHETIC gap"],
            "name": f"Synthetic Strategy {index}",
            "strategy_id": f"strategy-{index}",
            "version_ref": None,
        }
        for index in range(1, 10)
    ]
    assignments = [
        {
            "account_id": "account-a",
            "assignment_id": "assignment-a",
            "effective_end": "2026-01-03",
            "effective_start": "2026-01-01",
            "mapping_status": "MAPPED",
            "observed_label": "Synthetic Strategy 1",
            "source_classification": "SYNTHETIC",
            "source_id": "synthetic-source",
            "strategy_id": "strategy-1",
        },
        {
            "account_id": "account-b",
            "assignment_id": "assignment-b",
            "effective_end": None,
            "effective_start": "2026-01-01",
            "mapping_status": "MAPPED",
            "observed_label": "Synthetic Strategy 2",
            "source_classification": "SYNTHETIC",
            "source_id": "synthetic-source",
            "strategy_id": "strategy-2",
        },
        {
            "account_id": "account-b",
            "assignment_id": "assignment-general",
            "effective_end": "2025-12-31",
            "effective_start": None,
            "mapping_status": "UNMAPPED",
            "observed_label": "General",
            "source_classification": "SYNTHETIC",
            "source_id": "synthetic-source",
            "strategy_id": None,
        },
    ]
    nav = [
        {
            "account_id": "account-a",
            "as_of_date": "2026-01-01",
            "nav": _amount("100"),
            "provider": "SYNTHETIC",
            "record_id": "nav-1",
            "source_key": "synthetic-nav",
        },
        {
            "account_id": "account-a",
            "as_of_date": "2026-01-02",
            "nav": _amount("115"),
            "provider": "SYNTHETIC",
            "record_id": "nav-2",
            "source_key": "synthetic-nav",
        },
        {
            "account_id": "account-a",
            "as_of_date": "2026-01-03",
            "nav": _amount("121"),
            "provider": "SYNTHETIC",
            "record_id": "nav-3",
            "source_key": "synthetic-nav",
        },
    ]
    allocations = [
        {
            "allocation_id": f"allocation-{index}",
            "as_of_date": "2026-01-03",
            "current_value": _amount("10"),
            "scope": "SYSTEMATIC",
            "source_key": "synthetic-allocation",
            "strategy_id": f"strategy-{index}",
            "systematic_weight": str(Decimal("1") / Decimal("9")),
        }
        for index in range(1, 10)
    ]
    allocations.append(
        {
            "account_id": "reserve",
            "allocation_id": "allocation-reserve",
            "as_of_date": "2026-01-03",
            "current_value": _amount("10"),
            "scope": "RESERVE",
            "source_key": "synthetic-allocation",
            "strategy_id": None,
            "systematic_weight": None,
        }
    )
    return {
        "accounts": [
            {
                "account_id": "account-a",
                "base_currency": "USD",
                "provider": "SYNTHETIC",
                "role": "INVESTMENT",
            },
            {
                "account_id": "account-b",
                "base_currency": "USD",
                "provider": "SYNTHETIC",
                "role": "INVESTMENT",
            },
            {
                "account_id": "reserve",
                "base_currency": "USD",
                "provider": "INTERNAL",
                "role": "RESERVE",
            },
        ],
        "allocations": allocations,
        "as_of_date": "2026-01-03",
        "assignments": assignments,
        "assumptions": [],
        "baseline_schema_version": 1,
        "calendar_coverage": [
            {
                "account_id": "account-a",
                "date": day,
                "provider": "SYNTHETIC",
                "status": "OBSERVED",
            }
            for day in ("2026-01-01", "2026-01-02", "2026-01-03")
        ]
        + [
            {
                "account_id": "account-b",
                "date": None,
                "provider": "SYNTHETIC",
                "status": "NO_ACCOUNT_NAV_OBSERVATIONS",
            }
        ],
        "corrections": [
            {
                "correction_id": "correction-1",
                "fields": {
                    "account": "SYNTHETIC ACCOUNT",
                    "evidence": "SYNTHETIC",
                    "field": "start_date",
                    "institution": "SYNTHETIC",
                    "new_value": "2026-01-01",
                    "old_value": "2025-12-31",
                    "strategy": "Synthetic Strategy 1",
                },
                "source_id": "synthetic-source",
            }
        ],
        "costs": [
            {
                "annual_external_cost_usd": "1" if index == 1 else None,
                "cost_status": "known" if index == 1 else "unknown",
                "strategy_id": f"strategy-{index}",
            }
            for index in range(1, 10)
        ],
        "evidence_layer_metrics": [],
        "evidence_links": [],
        "fact_coverage": [
            {
                "fact_type": "accounts",
                "known_gaps": [],
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            },
            {
                "fact_type": "nav",
                "known_gaps": ["SYNTHETIC partial"],
                "status": "PARTIAL",
            },
            {
                "fact_type": "positions",
                "known_gaps": ["SYNTHETIC unavailable"],
                "status": "FAILED",
            },
        ],
        "flows": [
            {
                "account_id": "account-a",
                "amount": _amount("10"),
                "event_date": "2026-01-02",
                "evidence_class": "OBSERVED",
                "flow_type": "EXTERNAL_FLOW",
                "mapping_status": "MAPPED",
                "provider": "SYNTHETIC",
                "record_id": "flow-1",
                "source_key": "synthetic-flow",
                "strategy_id": "strategy-1",
            }
        ],
        "implementation_gaps": [
            {
                "account_layer": "SYNTHETIC account",
                "metrics": {},
                "note": "SYNTHETIC",
                "provider_layer": "SYNTHETIC provider",
                "status": "partial",
                "strategy_id": f"strategy-{index}",
            }
            for index in range(1, 10)
        ],
        "nav_observations": nav,
        "policy_version": "PORT-PERF-1",
        "positions": [],
        "provenance": {"catalog_sha256": "1" * 64, "generation_id": "2" * 64},
        "reconciliation_checks": [
            {"check_id": "nav", "status": "PASS"},
            {"check_id": "allocation", "status": "PASS"},
            {"check_id": "total", "status": "PASS"},
        ],
        "reconciliation_exceptions": [
            {
                "exception_id": "positions-unavailable",
                "fact_type": "positions",
                "severity": "BLOCKING",
                "status": "UNAVAILABLE",
            }
        ],
        "replay": {
            "correlation_one_stress": "0.37",
            "full_systematic_hwm_status": "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV",
            "nav_coverage": "0.5",
            "portfolio_action": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            "reserve_total_usd": "10",
            "status": "PASS",
            "systematic_total_usd": "90",
            "total_liquid_usd": "100",
        },
        "securities": [
            {"currency": "USD", "security_id": "security-1", "symbol": "SYNTH"}
        ],
        "source_artifacts": [
            {
                "artifact_id": "source-1",
                "as_of_date": "2026-01-03",
                "schema_name": "synthetic",
                "schema_version": "1",
                "sha256": "3" * 64,
                "source_key": "synthetic",
            }
        ],
        "strategies": strategies,
        "strategy_live_metrics": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "metrics": {
                    "cagr": "0.1",
                    "cumulative_return": "0.05",
                    "max_drawdown": "-0.02",
                },
                "observed_label": "Synthetic Strategy 1",
                "strategy_id": "strategy-1",
            }
        ],
        "strategy_nav_coverage": [
            {
                "current_value_usd": "10",
                "daily_end": "2026-01-03" if index == 1 else None,
                "daily_start": "2026-01-01" if index == 1 else None,
                "known_gap": None if index == 1 else "SYNTHETIC missing NAV",
                "return_observations": "3" if index == 1 else None,
                "status": "AVAILABLE" if index == 1 else "UNAVAILABLE",
                "strategy_id": f"strategy-{index}",
            }
            for index in range(1, 10)
        ],
        "transactions": [
            {
                "account_id": "account-a",
                "amount": _amount("-5"),
                "event_date": "2026-01-02",
                "kind": "BUY_FILL",
                "mapping_status": "MAPPED",
                "price": "5",
                "provider": "SYNTHETIC",
                "quantity": "1",
                "record_id": "transaction-1",
                "security_id": "security-1",
                "source_key": "synthetic-transaction",
                "strategy_id": "strategy-1",
            }
        ],
        "unmapped_records": [assignments[-1]],
    }


def _published_baseline(tmp_path: Path, catalog: dict[str, Any] | None = None) -> Path:
    root = tmp_path / "baseline"
    catalog = _baseline_catalog() if catalog is None else catalog
    content = canonical_json_bytes(catalog)
    artifact = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        as_of_date=date(2026, 1, 3),
        schema_name="portfolio-baseline-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 1, 3),
        adapter=AdapterIdentity("synthetic-baseline", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="SYNTHETIC baseline",
            fact_types=("portfolio",),
            observed_start=date(2026, 1, 1),
            observed_end=date(2026, 1, 3),
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-baseline.json").write_bytes(content)
    publish_generation(root, manifest.generation_id)
    return root


def test_report_data_has_hand_checked_twr_and_coverage_failures(tmp_path: Path) -> None:
    baseline_root = _published_baseline(tmp_path)
    report, first_bytes, _ = reporter.build_report_data(baseline_root)
    _, second_bytes, _ = reporter.build_report_data(baseline_root)

    assert first_bytes == second_bytes
    mapped = next(
        item for item in report["twr_mwr"] if item["assignment_id"] == "assignment-a"
    )
    with reporter.localcontext() as context:
        context.prec = 34
        expected = ((Decimal("115") - Decimal("10")) / Decimal("100")) * (
            Decimal("121") / Decimal("115")
        ) - Decimal("1")
    assert mapped["twr"] == reporter.decimal_text(expected)
    assert mapped["twr_status"] == "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
    assert mapped["mwr_xirr"] is None
    assert mapped["mwr_status"] == "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE"
    missing = next(
        item for item in report["twr_mwr"] if item["assignment_id"] == "assignment-b"
    )
    assert missing["twr"] is None
    assert missing["twr_status"] == "UNAVAILABLE_INSUFFICIENT_NAV"
    unmapped = next(
        item
        for item in report["twr_mwr"]
        if item["assignment_id"] == "assignment-general"
    )
    assert unmapped["twr_status"] == "UNMAPPED"
    assert report["positions"][0]["status"] == "UNAVAILABLE_NO_ACCEPTED_POSITION_SOURCE"
    assert list(report["strategy_summary"][0])[:4] == [
        "strategy_id",
        "strategy_name",
        "economic_family",
        "current_value_usd",
    ]


def test_missing_strategy_evidence_is_unavailable_not_inferred(tmp_path: Path) -> None:
    catalog = _baseline_catalog()
    catalog["implementation_gaps"] = []
    catalog["costs"] = []
    catalog["strategy_nav_coverage"] = []
    baseline_root = _published_baseline(tmp_path, catalog)

    report, _, _ = reporter.build_report_data(baseline_root)

    strategy = next(
        item
        for item in report["strategy_summary"]
        if item["strategy_id"] == "strategy-1"
    )
    assert strategy["implementation_gap_status"] == "UNAVAILABLE"
    assert strategy["cost_status"] == "UNAVAILABLE"
    assert strategy["nav_status"] == "UNAVAILABLE"


def test_transaction_without_amount_remains_explicitly_unavailable(
    tmp_path: Path,
) -> None:
    catalog = _baseline_catalog()
    catalog["transactions"].append(
        {
            "account_id": "account-a",
            "amount": {"currency": "USD", "status": "UNAVAILABLE", "value": None},
            "event_date": "2026-01-03",
            "evidence_class": "WCH_HISTORICAL",
            "kind": "Stock Split",
            "mapping_status": "UNMAPPED",
            "provider": "SYNTHETIC",
            "quantity": "2",
            "record_id": "transaction-no-amount",
            "security_id": "security-1",
            "source_key": "synthetic-transaction",
            "strategy_id": None,
        }
    )
    baseline_root = _published_baseline(tmp_path, catalog)

    report, _, _ = reporter.build_report_data(baseline_root)

    row = next(
        item
        for item in report["transactions_and_flows"]
        if item["record_id"] == "transaction-no-amount"
    )
    assert row["amount_status"] == "UNAVAILABLE"
    assert row["amount_usd"] is None


def test_high_precision_performance_is_numeric_only_in_excel_presentation() -> None:
    exact = "0.001932516064483270690775302874751"

    assert reporter._excel_value(exact) == exact
    assert reporter._excel_value(exact, header="twr") == pytest.approx(
        0.0019325160644832707
    )


def test_workbook_is_deterministic_structured_and_has_no_external_surface(
    tmp_path: Path,
) -> None:
    baseline_root = _published_baseline(tmp_path)
    output_root = tmp_path / "reporting"

    first = reporter.build_candidate(baseline_root, output_root)
    second = reporter.build_candidate(baseline_root, output_root)

    assert first == second
    candidate = Path(first["candidate_dir"])
    workbook = Path(first["workbook_path"])
    evidence = reporter.verify_candidate(baseline_root, candidate, workbook)
    assert evidence["sheet_count"] == 13
    assert evidence["table_count"] == 13
    assert evidence["external_links"] == 0
    assert evidence["macro_parts"] == 0
    assert evidence["formula_count"] >= 1
    with zipfile.ZipFile(workbook) as archive:
        widths = []
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                root = ET.fromstring(archive.read(name))
                widths.extend(
                    float(element.attrib["width"])
                    for element in root.iter()
                    if reporter._local_name(element.tag) == "col"
                    and "width" in element.attrib
                )
    assert widths
    assert max(widths) <= 45
    contract = json.loads((candidate / "workbook-contract.json").read_text())
    assert contract["required_sheets"] == list(reporter.SHEET_NAMES)
    assert set(contract["required_tables"]) == set(reporter.TABLE_NAMES)
    assert "expected_calculation_mode" not in contract
    assert {
        "cell": "E4",
        "expected": "PASS",
        "sheet": "Portfolio Summary",
    } in contract["sentinel_cells"]
    job_text = (candidate / "xlsx-job.json").read_text()
    job = json.loads(job_text)
    assert [step["type"] for step in job["steps"]] == ["open", "recalc", "save_as"]
    assert all(step["type"] != "refresh" for step in job["steps"])
    assert job_text.index('{"type":"open"') < job_text.index("workbook_path")
    assert job_text.index('{"type":"recalc"') < job_text.index("full_rebuild")
    assert job_text.index('{"type":"save_as"') < job_text.index("output_path")


def test_candidate_drift_and_failed_excel_evidence_block_publication(
    tmp_path: Path,
) -> None:
    baseline_root = _published_baseline(tmp_path)
    output_root = tmp_path / "reporting"
    built = reporter.build_candidate(baseline_root, output_root)
    candidate = Path(built["candidate_dir"])
    workbook = Path(built["workbook_path"])

    (candidate / reporter.REPORT_DATA_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(reporter.ReportValidationError, match="differs"):
        reporter.verify_candidate(baseline_root, candidate, workbook)

    shutil.rmtree(candidate)
    built = reporter.build_candidate(baseline_root, output_root)
    candidate = Path(built["candidate_dir"])
    workbook = Path(built["workbook_path"])
    run_result = candidate / "xlsx-result.json"
    validation = candidate / "validation.json"
    run_result.write_text(json.dumps({"ok": False}), encoding="utf-8")
    validation.write_text(json.dumps({"all_passed": True}), encoding="utf-8")
    with pytest.raises(reporter.ReportValidationError, match="not ok"):
        reporter.publish_report(
            baseline_root,
            candidate,
            workbook,
            run_result,
            validation,
            tmp_path / "published",
        )


def test_publish_creates_immutable_generation_and_pointer(tmp_path: Path) -> None:
    baseline_root = _published_baseline(tmp_path)
    candidate_root = tmp_path / "reporting-candidates"
    built = reporter.build_candidate(baseline_root, candidate_root)
    candidate = Path(built["candidate_dir"])
    workbook = Path(built["workbook_path"])
    run_result = candidate / "xlsx-result.json"
    validation = candidate / "validation.json"
    run_result.write_text(json.dumps({"ok": True}), encoding="utf-8")
    validation.write_text(json.dumps({"all_passed": True}), encoding="utf-8")
    published_root = tmp_path / "published"

    result = reporter.publish_report(
        baseline_root,
        candidate,
        workbook,
        run_result,
        validation,
        published_root,
    )
    repeated = reporter.publish_report(
        baseline_root,
        candidate,
        workbook,
        run_result,
        validation,
        published_root,
    )

    assert result == repeated
    assert result["publication_state"] == "PUBLISHED"
    assert read_current(published_root).generation_id == result["generation_id"]
    generation = published_root / "generations" / result["generation_id"]
    assert (generation / reporter.WORKBOOK_NAME).is_file()
    assert (generation / reporter.REPORT_DATA_NAME).is_file()
    assert (generation / "report-verification.json").is_file()


def test_report_schema_is_closed_and_keeps_mwr_unavailable() -> None:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "report-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["additionalProperties"] is False
    assert (
        schema["properties"]["strategies"]
        if "strategies" in schema["properties"]
        else True
    )
    assert schema["properties"]["strategy_summary"]["minItems"] == 9
    assert schema["properties"]["strategy_summary"]["maxItems"] == 9
    assert schema["$defs"]["performance"]["properties"]["mwr_xirr"]["type"] == "null"
    assert (
        schema["$defs"]["performance"]["properties"]["mwr_status"]["const"]
        == "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE"
    )


def test_cli_output_contains_only_sanitized_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_root = _published_baseline(tmp_path)
    output_root = tmp_path / "reporting"

    assert (
        reporter.main(
            [
                "build",
                "--baseline-root",
                str(baseline_root),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["strategy_count"] == 9
    assert "Synthetic Strategy" not in output
    assert "SYNTHETIC ACCOUNT" not in output
