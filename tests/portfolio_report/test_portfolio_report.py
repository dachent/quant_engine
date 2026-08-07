from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import re
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


def _excel_saved_package(
    path: Path,
    *,
    modified: str,
    chart_guid: str,
    temporary_root: str,
    revision_guid: str,
    semantic_value: str = "same",
    zip_second: int = 0,
) -> None:
    parts = {
        "docProps/core.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="urn:core" '
            'xmlns:dcterms="urn:terms" xmlns:xsi="urn:xsi">'
            '<dcterms:created xsi:type="dcterms:W3CDTF">'
            "2026-08-03T00:00:00Z</dcterms:created>"
            '<dcterms:modified xsi:type="dcterms:W3CDTF">'
            f"{modified}</dcterms:modified></cp:coreProperties>"
        ).encode(),
        "xl/charts/chart1.xml": (
            '<c:chartSpace xmlns:c="urn:chart" xmlns:c16="urn:chart16">'
            "<c:extLst><c:ext><c16:uniqueId "
            f'val="{{{chart_guid}}}"/></c:ext></c:extLst></c:chartSpace>'
        ).encode(),
        "xl/workbook.xml": (
            '<workbook xmlns:x15ac="urn:abs" xmlns:xr="urn:revision">'
            f'<x15ac:absPath url="{temporary_root}"/>'
            f'<xr:revisionPtr documentId="8_{{{revision_guid}}}"/>'
            "</workbook>"
        ).encode(),
        "xl/worksheets/sheet1.xml": f"<sheet>{semantic_value}</sheet>".encode(),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, (2026, 8, 3, 0, 0, zip_second))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def test_native_excel_workbook_canonicalization_is_deterministic_and_bounded(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.xlsx"
    second = tmp_path / "second.xlsx"
    changed = tmp_path / "changed.xlsx"
    _excel_saved_package(
        first,
        modified="2026-08-04T05:23:48Z",
        chart_guid="00000000-CC01-4263-A5C9-803DDF0C079C",
        temporary_root="C:\\Temp\\xlsx-win-stage-first\\",
        revision_guid="878E45FB-7799-4293-81FE-29F635965A3D",
    )
    _excel_saved_package(
        second,
        modified="2026-08-04T05:58:22Z",
        chart_guid="00000000-DD23-47F6-8C5E-35307AE221C8",
        temporary_root="C:\\Temp\\xlsx-win-stage-second\\",
        revision_guid="90E8A27E-6EEF-4295-BB7E-DD3062E36063",
        zip_second=2,
    )
    _excel_saved_package(
        changed,
        modified="2026-08-04T05:58:22Z",
        chart_guid="00000000-DD23-47F6-8C5E-35307AE221C8",
        temporary_root="C:\\Temp\\xlsx-win-stage-second\\",
        revision_guid="90E8A27E-6EEF-4295-BB7E-DD3062E36063",
        semantic_value="different financial content",
        zip_second=2,
    )

    first_result = reporter.canonicalize_workbook(
        first, tmp_path / "first-evidence.json"
    )
    second_result = reporter.canonicalize_workbook(
        second, tmp_path / "second-evidence.json"
    )
    changed_result = reporter.canonicalize_workbook(changed)

    assert first.read_bytes() == second.read_bytes()
    assert first_result == second_result
    assert changed_result["workbook_sha256"] != first_result["workbook_sha256"]
    assert reporter.canonicalize_workbook(first) == first_result


def test_canonicalization_fails_closed_on_unrecognized_volatile_field(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "unrecognized.xlsx"
    _excel_saved_package(
        workbook,
        modified="2026-08-04T05:23:48Z",
        chart_guid="not-a-guid-xxxxxxxxxxxxxxxxxxxxxxxxx",
        temporary_root="C:\\Temp\\xlsx-win-stage-first\\",
        revision_guid="878E45FB-7799-4293-81FE-29F635965A3D",
    )

    with pytest.raises(
        reporter.ReportValidationError, match="canonicalization is incomplete"
    ):
        reporter.canonicalize_workbook(workbook)


def _xlsx_result(run_id: str, temporary_root: str) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "run_id": run_id,
        "idempotency_key": "portfolio-report-synthetic",
        "final_state": "SUCCEEDED",
        "steps": [
            {
                "step_index": 0,
                "type": "open",
                "status": "succeeded",
                "message": f"Opened '{temporary_root}\\portfolio-report.xlsx'.",
            },
            {
                "step_index": 1,
                "type": "recalc",
                "status": "succeeded",
                "message": "Calculation reached xlDone.",
            },
            {
                "step_index": 2,
                "type": "save_as",
                "status": "succeeded",
                "message": f"Saved to '{temporary_root}\\saved.xlsx'.",
            },
        ],
        "invariants": [],
        "ok": True,
    }


def test_xlsx_result_canonicalization_removes_only_random_telemetry(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first-result.json"
    second = tmp_path / "second-result.json"
    first.write_text(
        json.dumps(_xlsx_result("a" * 32, r"C:\Temp\xlsx-first")), encoding="utf-8"
    )
    second.write_text(
        json.dumps(_xlsx_result("b" * 32, r"C:\Temp\xlsx-second")),
        encoding="utf-8",
    )

    first_result = reporter.canonicalize_xlsx_result(
        first, tmp_path / "first-result-evidence.json"
    )
    second_result = reporter.canonicalize_xlsx_result(
        second, tmp_path / "second-result-evidence.json"
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_result == second_result
    assert reporter.canonicalize_xlsx_result(first) == first_result
    failed = tmp_path / "failed-result.json"
    payload = _xlsx_result("c" * 32, r"C:\Temp\xlsx-failed")
    payload["steps"][1]["status"] = "failed"
    failed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(reporter.ReportValidationError, match="outcome differs"):
        reporter.canonicalize_xlsx_result(failed)


def test_provider_coverage_retains_declared_out_of_boundary_limitations() -> None:
    rows = reporter._coverage(
        {
            "calendar_coverage": [],
            "fact_coverage": [],
            "provider_coverage": [
                {
                    "coverage": {
                        "known_gaps": ["missing activity type"],
                        "out_of_boundary": ["checking account excluded"],
                        "status": "PARTIAL",
                    },
                    "provider_id": "synthetic",
                    "qualification": "ACCOUNT_LIVE",
                }
            ],
            "reconciliation_exceptions": [],
        }
    )

    assert rows == [
        {
            "category": "PROVIDER_COVERAGE",
            "item": "synthetic",
            "known_gap": "missing activity type; checking account excluded",
            "status": "PARTIAL",
            "value": "ACCOUNT_LIVE",
        }
    ]


def test_positions_preserve_history_and_mark_latest_snapshot_per_account() -> None:
    catalog = {
        "positions": [
            {
                "account_id": "account-a",
                "as_of_date": "2026-07-01",
                "mapping_status": "MAPPED",
                "market_value": _amount("100"),
                "quantity": "1",
                "security_id": "security-old",
                "strategy_id": "strategy-1",
            },
            {
                "account_id": "account-a",
                "as_of_date": "2026-08-01",
                "mapping_status": "MAPPED",
                "market_value": _amount("110"),
                "quantity": "1",
                "security_id": "security-new",
                "strategy_id": "strategy-1",
            },
            {
                "account_id": "account-b",
                "as_of_date": "2026-07-15",
                "mapping_status": "MAPPED",
                "market_value": _amount("25"),
                "quantity": "2",
                "security_id": "security-b",
                "strategy_id": "strategy-2",
            },
        ]
    }

    rows = reporter._positions(catalog)

    assert {(row["account_id"], row["as_of_date"]) for row in rows} == {
        ("account-a", "2026-07-01"),
        ("account-a", "2026-08-01"),
        ("account-b", "2026-07-15"),
    }
    assert {row["security_id"] for row in rows} == {
        "security-old",
        "security-new",
        "security-b",
    }
    assert {row["security_id"]: row["snapshot_status"] for row in rows} == {
        "security-old": "HISTORICAL_ACCEPTED_ACCOUNT_SNAPSHOT",
        "security-new": "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT",
        "security-b": "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT",
    }


def test_performance_preserves_pool_disposition_and_account_live_return_path() -> None:
    catalog = {
        "assignments": [
            {
                "account_id": "account-live",
                "assignment_id": "assignment-live",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "pool_id": "systematic-capital",
                "pool_mapping_status": "MAPPED",
                "strategy_id": "strategy-live",
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            },
            {
                "account_id": "account-reserve",
                "assignment_id": "assignment-reserve",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "UNMAPPED",
                "pool_id": "reserve-capital",
                "pool_mapping_status": "MAPPED",
                "strategy_id": None,
                "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            },
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {"observed_start": "2026-01-01"},
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-live",
            }
        ],
        "strategy_returns": [
            {
                "date": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.01",
                "strategy_id": "strategy-live",
            },
            {
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.02",
                "strategy_id": "strategy-live",
            },
        ],
    }

    rows = reporter._account_strategy_performance(catalog)
    live = next(row for row in rows if row["assignment_id"] == "assignment-live")
    reserve = next(row for row in rows if row["assignment_id"] == "assignment-reserve")

    assert live["twr_status"] == "UNAVAILABLE_INSUFFICIENT_NAV"
    assert live["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
    assert live["return_path_observations"] == 2
    assert live["return_path_cumulative_return"] == "0.0302"
    assert (
        live["return_path_account_binding"]
        == "UNIQUE_CURRENT_EFFECTIVE_DATED_ASSIGNMENT"
    )
    assert live["return_path_assignment_id"] == "assignment-live"
    assert reserve["pool_id"] == "reserve-capital"
    assert reserve["pool_mapping_status"] == "MAPPED"
    assert reserve["strategy_mapping_disposition"] == "RESERVE_NOT_APPLICABLE"


def test_performance_rejects_ambiguous_account_live_path() -> None:
    catalog = {
        "assignments": [
            {
                "account_id": f"account-{suffix}",
                "assignment_id": f"assignment-{suffix}",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "pool_id": "systematic-capital",
                "pool_mapping_status": "MAPPED",
                "strategy_id": "strategy-live",
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
            for suffix in ("a", "b")
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {"observed_start": "2026-01-01"},
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-live",
            }
        ],
        "strategy_returns": [
            {
                "date": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.01",
                "strategy_id": "strategy-live",
            },
            {
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.02",
                "strategy_id": "strategy-live",
            },
        ],
    }

    rows = reporter._account_strategy_performance(catalog)

    assert len(rows) == 2
    assert all("return_path_status" not in row for row in rows)


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


def test_baseline_retries_transient_pointer_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _published_baseline(tmp_path)
    attempts = 0

    def flaky_read_current(path: Path) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient provider lock")
        return read_current(path)

    monkeypatch.setattr(reporter, "read_current", flaky_read_current)
    monkeypatch.setattr(reporter.time_module, "sleep", lambda _seconds: None)

    catalog, identity, _path = reporter._baseline(root)

    assert attempts == 3
    assert identity["generation_id"] == read_current(root).generation_id
    assert catalog["baseline_schema_version"] == 1


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


def test_effective_dated_strategy_version_registry_projects_into_report(
    tmp_path: Path,
) -> None:
    catalog = _baseline_catalog()
    unavailable = {
        "source_ids": ["synthetic-source"],
        "status": "UNAVAILABLE",
        "version_ref": None,
    }
    catalog["strategy_version_catalog"] = {
        "as_of_date": catalog["as_of_date"],
        "catalog_schema_version": 2,
        "strategies": [
            {
                "backtest": dict(unavailable),
                "benchmark": dict(unavailable),
                "compatibility_reason": "SYNTHETIC compatibility is unresolved.",
                "compatibility_status": "UNRESOLVED",
                "configuration": dict(unavailable),
                "costs": dict(unavailable),
                "design": dict(unavailable),
                "disposition": "UNRESOLVED",
                "disposition_reason": "SYNTHETIC authoritative version is absent.",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "evidence_layers": [
                    {
                        "artifact_sha256": None,
                        "disposition": "SYNTHETIC account-live route.",
                        "evidence_layer": "ACCOUNT_LIVE",
                        "observed_end": None,
                        "observed_start": None,
                        "source_id": "synthetic-source",
                        "status": "REFERENCE_ONLY",
                        "version_ref": None,
                    }
                ],
                "gaps": ["SYNTHETIC authoritative version is absent."],
                "live_boundary": {
                    "source_ids": ["synthetic-source"],
                    "status": "EVIDENCED" if index == 1 else "UNAVAILABLE",
                    "version_ref": f"live-v{index}" if index == 1 else None,
                    "observed_start": "2026-01-01" if index == 1 else None,
                    "observed_end": "2026-01-03" if index == 1 else None,
                },
                "source_route": {
                    "source_ids": ["synthetic-source"],
                    "status": "REFERENCE_ONLY",
                    "version_ref": None,
                },
                "strategy_id": f"strategy-{index}",
                "version_ref": None,
            }
            for index in range(1, 10)
        ],
    }
    baseline_root = _published_baseline(tmp_path, catalog)

    report, _, _ = reporter.build_report_data(baseline_root)
    first = next(
        item
        for item in report["strategy_summary"]
        if item["strategy_id"] == "strategy-1"
    )

    assert first["version_disposition"] == "UNRESOLVED"
    assert first["version_effective_start"] == "2026-01-01"
    assert first["live_boundary_status"] == "EVIDENCED"
    assert first["live_boundary_observed_end"] == "2026-01-03"
    assert first["version_compatibility_status"] == "UNRESOLVED"
    assert first["live_plus_backtest_status"] == (
        "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    )
    assert first["live_plus_backtest_reason"].startswith("UNRESOLVED:")
    assert first["strategy_evidence_layers"][0]["evidence_layer"] == "ACCOUNT_LIVE"


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
    reporter.canonicalize_workbook(
        workbook, candidate / "workbook-canonicalization.json"
    )
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
    run_result.write_text(
        json.dumps(_xlsx_result("d" * 32, r"C:\Temp\xlsx-publish")),
        encoding="utf-8",
    )
    validation.write_text(json.dumps({"all_passed": True}), encoding="utf-8")
    reporter.canonicalize_workbook(
        workbook, candidate / "workbook-canonicalization.json"
    )
    reporter.canonicalize_xlsx_result(
        run_result, candidate / "xlsx-result-canonicalization.json"
    )
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


def _bound_freshness_payload() -> dict[str, object]:
    return {
        "assessment_schema_version": 3,
        "as_of_date": "2026-08-07",
        "assessments": [{"evaluated_on": "2026-08-07"}],
        "policies": [],
        "provider_ledger": {
            "as_of_date": "2026-08-03",
            "catalog_sha256": "a" * 64,
            "catalog_size_bytes": 123,
            "generation_id": "b" * 64,
            "manifest_sha256": "c" * 64,
            "manifest_size_bytes": 456,
        },
    }


def test_freshness_assessments_accept_bound_provider_ledger(tmp_path: Path) -> None:
    freshness = tmp_path / "freshness-health.json"
    freshness.write_text(json.dumps(_bound_freshness_payload()), encoding="utf-8")

    assessments, as_of, sha256 = reporter._freshness_assessments(
        freshness, date(2026, 8, 3)
    )

    assert assessments == [{"evaluated_on": "2026-08-07"}]
    assert as_of == date(2026, 8, 7)
    assert re.fullmatch(r"[0-9a-f]{64}", sha256)


def test_freshness_assessments_reject_future_provider_ledger(tmp_path: Path) -> None:
    payload = _bound_freshness_payload()
    ledger = payload["provider_ledger"]
    assert isinstance(ledger, dict)
    ledger["as_of_date"] = "2026-08-08"
    freshness = tmp_path / "freshness-health.json"
    freshness.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        reporter.ReportValidationError,
        match="provider ledger postdates the evaluation",
    ):
        reporter._freshness_assessments(freshness, date(2026, 8, 3))


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


def test_provider_live_nav_is_available_without_becoming_account_live() -> None:
    catalog = {
        "allocations": [
            {
                "current_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "100",
                },
                "scope": "SYSTEMATIC",
                "strategy_id": "robuxio-crypto",
                "systematic_weight": "1",
            }
        ],
        "costs": [],
        "implementation_gaps": [],
        "strategies": [
            {
                "economic_family": "crypto",
                "name": "Robuxio Crypto",
                "strategy_id": "robuxio-crypto",
                "version_ref": None,
            }
        ],
        "strategy_live_metrics": [],
        "strategy_nav_coverage": [],
        "strategy_provider_evidence": [
            {
                "evidence_layer": "PROVIDER_LIVE",
                "generation_id": "a" * 64,
                "provider": {
                    "cumulative_return": "0.2",
                    "current_drawdown": "-0.1",
                    "maximum_drawdown": "-0.3",
                    "observed_end": "2026-01-31",
                    "observed_start": "2025-01-01",
                    "return_observations": 250,
                },
                "provider_id": "matrixport",
                "strategy_id": "robuxio-crypto",
            }
        ],
    }

    rows = reporter._strategies(catalog, [])

    assert rows[0]["nav_status"] == "AVAILABLE"
    assert rows[0]["nav_evidence_layer"] == "PROVIDER_LIVE"
    assert rows[0]["account_live_status"] == "UNAVAILABLE"
    assert rows[0]["account_live_cumulative_return"] is None
    assert rows[0]["hwm_max_drawdown"] == "-0.3"


def test_account_live_summary_uses_one_hash_bound_canonical_return_path() -> None:
    catalog = {
        "allocations": [
            {
                "current_value": _amount("100"),
                "scope": "SYSTEMATIC",
                "strategy_id": "strategy-1",
                "systematic_weight": "1",
            }
        ],
        "costs": [],
        "implementation_gaps": [],
        "source_artifacts": [
            {
                "as_of_date": "2026-01-03",
                "sha256": "f" * 64,
                "source_key": "strategy-live-metrics",
            }
        ],
        "strategies": [
            {
                "economic_family": "synthetic",
                "name": "Synthetic Strategy",
                "strategy_id": "strategy-1",
                "version_ref": None,
            }
        ],
        "strategy_live_metrics": [
            {
                "metrics": {
                    "cagr": "9.9",
                    "cumulative_return": "8.8",
                    "first_date": "2024-01-01",
                    "last_date": "2024-12-31",
                    "max_drawdown": "-0.9",
                    "return_observations": 99,
                },
                "strategy_id": "strategy-1",
            }
        ],
        "strategy_nav_coverage": [],
        "strategy_provider_evidence": [
            {
                "account": {
                    "cumulative_return": "0.1000000000000000000000001",
                    "current_drawdown": "0",
                    "generation_as_of_date": "2026-01-02",
                    "generation_id": "a" * 64,
                    "maximum_drawdown": "0",
                    "observed_end": "2026-01-01",
                    "observed_start": "2025-01-01",
                    "provider_id": "synthetic-broker",
                    "return_observations": 2,
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_as_of_date": "2026-01-02",
                "generation_id": "a" * 64,
                "provider_id": "synthetic-broker",
                "strategy_id": "strategy-1",
            }
        ],
        "strategy_returns": [
            {
                "date": "2025-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic-broker",
                "return": "0.000",
                "strategy_id": "strategy-1",
            },
            {
                "date": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic-broker",
                "return": "0.1000",
                "strategy_id": "strategy-1",
            },
        ],
    }

    rows = reporter._strategies(catalog, [])

    expected_series = [
        {"date": "2025-01-01", "return": "0"},
        {"date": "2026-01-01", "return": "0.1"},
    ]
    row = rows[0]
    assert row["account_live_return_authority"] == "CANONICAL_STRATEGY_RETURNS"
    assert row["account_live_cumulative_return"] == "0.1"
    assert row["account_live_cagr"] == "0.1"
    assert row["account_live_cagr_status"] == ("AVAILABLE_DERIVED_SAME_CANONICAL_PATH")
    assert row["account_live_max_drawdown"] == "0"
    assert row["account_live_observed_start"] == "2025-01-01"
    assert row["account_live_observed_end"] == "2026-01-01"
    assert row["account_live_return_observations"] == 2
    assert row["account_live_source_generation_as_of_date"] == "2026-01-02"
    assert (
        row["account_live_return_series_sha256"]
        == hashlib.sha256(
            reporter._canonical_json_array_bytes(expected_series)
        ).hexdigest()
    )
    assert row["account_live_return_series_schema"] == "ordered-date-return/1"
    assert row["account_live_source_sha256"] is None


def test_account_live_evidence_rejects_return_series_vintage_mismatch() -> None:
    catalog = {
        "strategy_provider_evidence": [
            {
                "account": {
                    "cumulative_return": "0.1",
                    "current_drawdown": "0",
                    "maximum_drawdown": "0",
                    "observed_end": "2026-01-01",
                    "observed_start": "2025-01-01",
                    "provider_id": "synthetic",
                    "return_observations": 3,
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "strategy_id": "strategy-1",
            }
        ],
        "strategy_returns": [
            {
                "date": "2025-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0",
                "strategy_id": "strategy-1",
            },
            {
                "date": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.1",
                "strategy_id": "strategy-1",
            },
        ],
    }

    with pytest.raises(
        reporter.ReportValidationError,
        match="return_observations differs from canonical return series",
    ):
        reporter._strategy_provider_evidence(catalog)


def test_legacy_account_metrics_remain_one_explicit_audit_vintage() -> None:
    catalog = {
        "allocations": [
            {
                "current_value": _amount("100"),
                "scope": "SYSTEMATIC",
                "strategy_id": "strategy-1",
                "systematic_weight": "1",
            }
        ],
        "costs": [],
        "implementation_gaps": [],
        "source_artifacts": [
            {
                "as_of_date": "2025-12-31",
                "sha256": "c" * 64,
                "source_key": "strategy-live-metrics",
            }
        ],
        "strategies": [
            {
                "economic_family": "synthetic",
                "name": "Synthetic Strategy",
                "strategy_id": "strategy-1",
                "version_ref": None,
            }
        ],
        "strategy_live_metrics": [
            {
                "metrics": {
                    "cagr": "0.11",
                    "cumulative_return": "0.1",
                    "first_date": "2025-01-01",
                    "last_date": "2025-12-31",
                    "max_drawdown": "-0.2",
                    "return_observations": 252,
                },
                "strategy_id": "strategy-1",
            }
        ],
        "strategy_nav_coverage": [],
        "strategy_provider_evidence": [],
        "strategy_returns": [],
    }

    row = reporter._strategies(catalog, [])[0]

    assert row["account_live_return_authority"] == "AUDIT_STRATEGY_LIVE_METRICS"
    assert row["account_live_cagr"] == "0.11"
    assert row["account_live_cumulative_return"] == "0.1"
    assert row["account_live_max_drawdown"] == "-0.2"
    assert row["account_live_observed_start"] == "2025-01-01"
    assert row["account_live_observed_end"] == "2025-12-31"
    assert row["account_live_source_generation_as_of_date"] == "2025-12-31"
    assert row["account_live_source_sha256"] == "c" * 64
    assert row["account_live_return_series_sha256"] is None


def test_report_preserves_every_hash_bound_provider_reconciliation_exception(
    tmp_path: Path,
) -> None:
    provider_generation = "e" * 64
    provider_catalog = {
        "reconciliation_checks": [
            {"check_id": "provider-pass", "status": "PASS"},
            {
                "authority_effect": "ACCOUNT_PATH_RETAINED",
                "check_id": "provider-exception-a",
                "exception_date_count": 2,
                "maximum_absolute_difference": "0.25",
                "status": "EXCEPTION",
                "strategy_id": "strategy-1",
                "tolerance": "0.000001",
            },
            {
                "authority_effect": "ACCOUNT_PATH_RETAINED",
                "check_id": "provider-exception-b",
                "exception_date_count": 1,
                "maximum_absolute_difference": "0.5",
                "status": "EXCEPTION",
                "strategy_id": "strategy-2",
                "tolerance": "0.000001",
            },
        ]
    }
    provider_bytes = canonical_json_bytes(provider_catalog)
    provider_sha256 = hashlib.sha256(provider_bytes).hexdigest()
    provider_dir = tmp_path / "provider-ledger" / "generations" / provider_generation
    provider_dir.mkdir(parents=True)
    (provider_dir / "provider-ledger.json").write_bytes(provider_bytes)
    catalog = _baseline_catalog()
    catalog["source_artifacts"].append(
        {
            "artifact_id": "providers/provider-ledger/provider-ledger.json",
            "as_of_date": "2026-01-03",
            "schema_name": "provider-ledger-catalog",
            "schema_version": "1",
            "sha256": provider_sha256,
            "source_key": "provider-ledger-generation",
        }
    )
    baseline_root = _published_baseline(tmp_path, catalog)

    report, _, _ = reporter.build_report_data(baseline_root)

    rows = [
        item
        for item in report["reconciliation_checks"]
        if item["source_layer"] == "PROVIDER_LEDGER"
    ]
    assert [item["check_id"] for item in rows] == [
        "provider-exception-a",
        "provider-exception-b",
    ]
    assert all(item["status"] == "EXCEPTION" for item in rows)
    assert all(item["source_generation_id"] == provider_generation for item in rows)
    assert all(item["source_catalog_sha256"] == provider_sha256 for item in rows)
    assert (
        json.loads(rows[0]["details_json"])
        == provider_catalog["reconciliation_checks"][1]
    )


def test_report_accepts_explicit_non_sibling_provider_ledger_root(
    tmp_path: Path,
) -> None:
    provider_generation = "f" * 64
    provider_catalog = {
        "reconciliation_checks": [
            {
                "authority_effect": "ACCOUNT_PATH_RETAINED",
                "check_id": "provider-explicit-root",
                "status": "EXCEPTION",
                "strategy_id": "strategy-1",
            }
        ]
    }
    provider_bytes = canonical_json_bytes(provider_catalog)
    provider_sha256 = hashlib.sha256(provider_bytes).hexdigest()
    provider_root = tmp_path / "stages" / "ledger"
    provider_dir = provider_root / "generations" / provider_generation
    provider_dir.mkdir(parents=True)
    (provider_dir / "provider-ledger.json").write_bytes(provider_bytes)
    catalog = _baseline_catalog()
    catalog["source_artifacts"].append(
        {
            "artifact_id": "providers/provider-ledger/provider-ledger.json",
            "as_of_date": "2026-01-03",
            "schema_name": "provider-ledger-catalog",
            "schema_version": "1",
            "sha256": provider_sha256,
            "source_key": "provider-ledger-generation",
        }
    )
    baseline_root = _published_baseline(tmp_path / "baseline-scope", catalog)

    report, _, _ = reporter.build_report_data(
        baseline_root, provider_ledger_root=provider_root
    )

    rows = [
        item
        for item in report["reconciliation_checks"]
        if item["source_layer"] == "PROVIDER_LEDGER"
    ]
    assert [item["check_id"] for item in rows] == ["provider-explicit-root"]
    assert rows[0]["source_generation_id"] == provider_generation


def test_report_fails_closed_when_provider_reconciliation_source_is_missing(
    tmp_path: Path,
) -> None:
    catalog = _baseline_catalog()
    catalog["source_artifacts"].append(
        {
            "artifact_id": "providers/provider-ledger/provider-ledger.json",
            "as_of_date": "2026-01-03",
            "schema_name": "provider-ledger-catalog",
            "schema_version": "1",
            "sha256": "d" * 64,
            "source_key": "provider-ledger-generation",
        }
    )
    baseline_root = _published_baseline(tmp_path, catalog)

    with pytest.raises(
        reporter.ReportValidationError,
        match="hash-bound provider-ledger catalog is missing or ambiguous",
    ):
        reporter.build_report_data(baseline_root)
