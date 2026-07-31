"""Build, verify, and publish the private coverage-aware portfolio report."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Final, Mapping, Sequence
from xml.etree import ElementTree as ET
import zipfile

import xlsxwriter  # type: ignore[import-untyped]

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import decimal_text


REPORT_SCHEMA_VERSION: Final = 1
BUILDER_VERSION: Final = "1.0.0"
POLICY_VERSION: Final = "PORT-PERF-1"
WORKBOOK_NAME: Final = "portfolio-report.xlsx"
REPORT_DATA_NAME: Final = "portfolio-report-data.json"
SHEET_NAMES: Final = (
    "Portfolio Summary",
    "Account Summary",
    "Strategy Summary",
    "Positions",
    "Transactions and Flows",
    "TWR MWR",
    "Allocation History",
    "Flow Reconciliation",
    "Source Reconciliation",
    "Coverage Data Quality",
    "Corrections",
    "Methodology",
    "Provenance",
)
TABLE_NAMES: Final = (
    "tblPortfolioSummary",
    "tblAccounts",
    "tblStrategies",
    "tblPositions",
    "tblTransactionsFlows",
    "tblTwrMwr",
    "tblAllocations",
    "tblFlowRecon",
    "tblSourceRecon",
    "tblCoverage",
    "tblCorrections",
    "tblMethodology",
    "tblProvenance",
)


class ReportValidationError(ValueError):
    """The accepted baseline or generated report violates the report contract."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportValidationError(f"{field_name} must be non-empty text")
    return value.strip()


def _date(value: object, field_name: str) -> date:
    accepted = _text(value, field_name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise ReportValidationError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != accepted:
        raise ReportValidationError(f"{field_name} must use canonical ISO format")
    return parsed


def _decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise ReportValidationError(f"{field_name} must be decimal text")
    try:
        accepted = Decimal(value)
    except InvalidOperation as exc:
        raise ReportValidationError(f"{field_name} must be decimal text") from exc
    if not accepted.is_finite():
        raise ReportValidationError(f"{field_name} must be finite")
    return accepted


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReportValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReportValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError(f"{field_name} is not readable JSON") from exc
    return _object(decoded, field_name)


def _baseline(root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError("baseline pointer is not accepted") from exc
    catalog_path = (
        root / "generations" / pointer.generation_id / "portfolio-baseline.json"
    )
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ReportValidationError("baseline catalog is missing or linked")
    catalog = _load_json(catalog_path, "baseline catalog")
    if catalog.get("baseline_schema_version") != 1:
        raise ReportValidationError("baseline schema version is unsupported")
    if catalog.get("policy_version") != POLICY_VERSION:
        raise ReportValidationError("baseline performance policy differs")
    sha256, _ = hash_file(catalog_path)
    return (
        catalog,
        {"catalog_sha256": sha256, "generation_id": pointer.generation_id},
        catalog_path,
    )


def _amount(record: Mapping[str, Any], field_name: str) -> Decimal:
    amount = _object(record.get(field_name), field_name)
    if amount.get("status") != "AVAILABLE":
        raise ReportValidationError(f"{field_name} is not available")
    return _decimal(amount.get("value"), f"{field_name}.value")


def _inside(day: date, start: date | None, end: date | None) -> bool:
    return (start is None or day >= start) and (end is None or day <= end)


def _account_strategy_performance(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    nav_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in _objects(catalog.get("nav_observations"), "nav_observations"):
        nav_by_account[_text(record.get("account_id"), "nav.account_id")].append(record)
    flows_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in _objects(catalog.get("flows"), "flows"):
        flows_by_account[_text(record.get("account_id"), "flow.account_id")].append(
            record
        )
    confirmed_missing: dict[str, set[date]] = defaultdict(set)
    for record in _objects(catalog.get("calendar_coverage"), "calendar_coverage"):
        if record.get("status") == "MISSING_ON_PROVIDER_OBSERVED_DATE":
            confirmed_missing[
                _text(record.get("account_id"), "calendar.account_id")
            ].add(_date(record.get("date"), "calendar.date"))

    rows: list[dict[str, Any]] = []
    assignments = _objects(catalog.get("assignments"), "assignments")
    for assignment in assignments:
        account_id = _text(assignment.get("account_id"), "assignment.account_id")
        mapping_status = _text(
            assignment.get("mapping_status"), "assignment.mapping_status"
        )
        start = (
            None
            if assignment.get("effective_start") is None
            else _date(assignment.get("effective_start"), "assignment.start")
        )
        end = (
            None
            if assignment.get("effective_end") is None
            else _date(assignment.get("effective_end"), "assignment.end")
        )
        base: dict[str, Any] = {
            "account_id": account_id,
            "assignment_id": _text(
                assignment.get("assignment_id"), "assignment.assignment_id"
            ),
            "effective_end": end.isoformat() if end is not None else None,
            "effective_start": start.isoformat() if start is not None else None,
            "flow_count": 0,
            "flow_timing": "DATE_ONLY_END_OF_DAY_INFERRED_TIMING",
            "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
            "mwr_xirr": None,
            "nav_observations": 0,
            "strategy_id": assignment.get("strategy_id"),
            "twr": None,
            "twr_status": "UNAVAILABLE",
        }
        if mapping_status != "MAPPED" or assignment.get("strategy_id") is None:
            base["twr_status"] = "UNMAPPED"
            rows.append(base)
            continue
        strategy_id = _text(assignment.get("strategy_id"), "assignment.strategy_id")
        nav = [
            item
            for item in nav_by_account.get(account_id, [])
            if _inside(_date(item.get("as_of_date"), "nav.date"), start, end)
        ]
        nav.sort(key=lambda item: _date(item.get("as_of_date"), "nav.date"))
        flows = [
            item
            for item in flows_by_account.get(account_id, [])
            if item.get("strategy_id") == strategy_id
            and _inside(_date(item.get("event_date"), "flow.date"), start, end)
        ]
        base["flow_count"] = len(flows)
        base["nav_observations"] = len(nav)
        if len(nav) < 2:
            base["twr_status"] = "UNAVAILABLE_INSUFFICIENT_NAV"
            rows.append(base)
            continue
        observed_start = _date(nav[0].get("as_of_date"), "nav.date")
        observed_end = _date(nav[-1].get("as_of_date"), "nav.date")
        base["observed_start"] = observed_start.isoformat()
        base["observed_end"] = observed_end.isoformat()
        if any(
            observed_start <= day <= observed_end
            for day in confirmed_missing[account_id]
        ):
            base["twr_status"] = "UNAVAILABLE_MISSING_NAV"
            rows.append(base)
            continue
        nav_dates = {_date(item.get("as_of_date"), "nav.date") for item in nav}
        if any(
            _date(item.get("event_date"), "flow.date") not in nav_dates
            for item in flows
        ):
            base["twr_status"] = "UNAVAILABLE_FLOW_WITHOUT_SAME_DATE_NAV"
            rows.append(base)
            continue
        flow_by_date: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
        for flow in flows:
            flow_by_date[_date(flow.get("event_date"), "flow.date")] += _amount(
                flow, "amount"
            )
        product = Decimal("1")
        with localcontext() as context:
            context.prec = 34
            for prior_record, current_record in zip(nav, nav[1:]):
                prior = _amount(prior_record, "nav")
                current = _amount(current_record, "nav")
                if prior <= 0:
                    base["twr_status"] = "UNAVAILABLE_NONPOSITIVE_PRIOR_NAV"
                    break
                current_day = _date(current_record.get("as_of_date"), "nav.date")
                product *= (current - flow_by_date[current_day]) / prior
            else:
                base["twr"] = decimal_text(product - Decimal("1"))
                base["twr_status"] = "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
        rows.append(base)
    return sorted(
        rows,
        key=lambda item: (
            str(item["account_id"]),
            str(item.get("effective_start") or ""),
            str(item.get("assignment_id")),
        ),
    )


def _latest_nav(catalog: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    latest: dict[str, tuple[str, str]] = {}
    for record in _objects(catalog.get("nav_observations"), "nav_observations"):
        account_id = _text(record.get("account_id"), "nav.account_id")
        day = _text(record.get("as_of_date"), "nav.date")
        value = decimal_text(_amount(record, "nav"))
        if account_id not in latest or day > latest[account_id][0]:
            latest[account_id] = (day, value)
    return latest


def _portfolio_summary(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    replay = _object(catalog.get("replay"), "replay")
    return [
        {
            "metric": "Baseline replay",
            "status": replay.get("status"),
            "unit": "status",
            "value": replay.get("status"),
        },
        {
            "metric": "Total liquid",
            "status": "AVAILABLE",
            "unit": "USD",
            "value": replay.get("total_liquid_usd"),
        },
        {
            "metric": "Systematic capital",
            "status": "AVAILABLE",
            "unit": "USD",
            "value": replay.get("systematic_total_usd"),
        },
        {
            "metric": "Reserve",
            "status": "AVAILABLE",
            "unit": "USD",
            "value": replay.get("reserve_total_usd"),
        },
        {
            "metric": "NAV coverage",
            "status": "PARTIAL",
            "unit": "ratio",
            "value": replay.get("nav_coverage"),
        },
        {
            "metric": "Correlation-one stress",
            "status": "ADVERSE_SCENARIO",
            "unit": "ratio",
            "value": replay.get("correlation_one_stress"),
        },
        {
            "metric": "Full-systematic HWM",
            "status": "UNAVAILABLE",
            "unit": "status",
            "value": replay.get("full_systematic_hwm_status"),
        },
        {
            "metric": "Advisory readiness",
            "status": "ADVISORY",
            "unit": "status",
            "value": replay.get("portfolio_action"),
        },
    ]


def _accounts(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    latest = _latest_nav(catalog)
    reserve = next(
        item
        for item in _objects(catalog.get("allocations"), "allocations")
        if item.get("scope") == "RESERVE"
    )
    rows: list[dict[str, Any]] = []
    for account in _objects(catalog.get("accounts"), "accounts"):
        account_id = _text(account.get("account_id"), "account.account_id")
        role = _text(account.get("role"), "account.role")
        if role == "RESERVE":
            value = decimal_text(_amount(reserve, "current_value"))
            as_of = _text(reserve.get("as_of_date"), "reserve.as_of_date")
            status = "AVAILABLE_MANDATE_CLASSIFICATION"
        elif account_id in latest:
            as_of, value = latest[account_id]
            status = "AVAILABLE_LATEST_OBSERVATION"
        else:
            as_of, value, status = None, None, "UNAVAILABLE_NO_NAV"
        rows.append(
            {
                "account_id": account_id,
                "as_of_date": as_of,
                "base_currency": account.get("base_currency"),
                "latest_value_usd": value,
                "provider": account.get("provider"),
                "role": role,
                "status": status,
            }
        )
    return sorted(rows, key=lambda item: str(item["account_id"]))


def _strategies(
    catalog: Mapping[str, Any], performance: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    allocations = {
        str(item["strategy_id"]): item
        for item in _objects(catalog.get("allocations"), "allocations")
        if item.get("scope") == "SYSTEMATIC"
    }
    coverage = {
        str(item["strategy_id"]): item
        for item in _objects(
            catalog.get("strategy_nav_coverage"), "strategy_nav_coverage"
        )
    }
    costs = {
        str(item["strategy_id"]): item
        for item in _objects(catalog.get("costs"), "costs")
    }
    gaps = {
        str(item["strategy_id"]): item
        for item in _objects(catalog.get("implementation_gaps"), "implementation_gaps")
    }
    live = {
        str(item["strategy_id"]): item
        for item in _objects(
            catalog.get("strategy_live_metrics"), "strategy_live_metrics"
        )
        if item.get("strategy_id") is not None
    }
    performance_count: dict[str, tuple[int, int]] = {}
    for strategy_id in allocations:
        items = [item for item in performance if item.get("strategy_id") == strategy_id]
        performance_count[strategy_id] = (
            sum(
                1
                for item in items
                if str(item.get("twr_status", "")).startswith("AVAILABLE")
            ),
            len(items),
        )
    rows: list[dict[str, Any]] = []
    for strategy in _objects(catalog.get("strategies"), "strategies"):
        strategy_id = _text(strategy.get("strategy_id"), "strategy.strategy_id")
        allocation = allocations[strategy_id]
        live_row = live.get(strategy_id)
        metrics = _object(live_row.get("metrics"), "live.metrics") if live_row else {}
        cost_row = costs.get(strategy_id, {})
        gap_row = gaps.get(strategy_id, {})
        coverage_row = coverage.get(strategy_id, {})
        available_segments, total_segments = performance_count[strategy_id]
        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": strategy.get("name"),
                "economic_family": strategy.get("economic_family"),
                "current_value_usd": decimal_text(_amount(allocation, "current_value")),
                "systematic_weight": allocation.get("systematic_weight"),
                "available_twr_segments": available_segments,
                "total_assignment_segments": total_segments,
                "account_live_status": "AVAILABLE" if live_row else "UNAVAILABLE",
                "account_live_cagr": metrics.get("cagr"),
                "account_live_cumulative_return": metrics.get("cumulative_return"),
                "account_live_max_drawdown": metrics.get("max_drawdown"),
                "nav_status": coverage_row.get("status", "UNAVAILABLE"),
                "cost_status": cost_row.get("cost_status", "UNAVAILABLE"),
                "implementation_gap_status": gap_row.get("status", "UNAVAILABLE"),
                "version_ref": strategy.get("version_ref"),
            }
        )
    return sorted(rows, key=lambda item: str(item["strategy_id"]))


def _transactions_and_flows(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    securities = {
        str(item["security_id"]): item
        for item in _objects(catalog.get("securities"), "securities")
    }
    rows: list[dict[str, Any]] = []
    for record in _objects(catalog.get("transactions"), "transactions"):
        security = securities.get(str(record.get("security_id")), {})
        rows.append(
            {
                "account_id": record.get("account_id"),
                "amount_usd": decimal_text(_amount(record, "amount")),
                "date": record.get("event_date"),
                "evidence_class": "OBSERVED",
                "kind": record.get("kind"),
                "price": record.get("price"),
                "provider": record.get("provider"),
                "quantity": record.get("quantity"),
                "record_id": record.get("record_id"),
                "record_type": "TRANSACTION",
                "strategy_id": record.get("strategy_id"),
                "symbol": security.get("symbol"),
            }
        )
    for record in _objects(catalog.get("flows"), "flows"):
        rows.append(
            {
                "account_id": record.get("account_id"),
                "amount_usd": decimal_text(_amount(record, "amount")),
                "date": record.get("event_date"),
                "evidence_class": record.get("evidence_class"),
                "kind": record.get("flow_type"),
                "price": None,
                "provider": record.get("provider"),
                "quantity": None,
                "record_id": record.get("record_id"),
                "record_type": "FLOW",
                "strategy_id": record.get("strategy_id"),
                "symbol": None,
            }
        )
    return sorted(rows, key=lambda item: (str(item["date"]), str(item["record_id"])))


def _flow_reconciliation(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for flow in _objects(catalog.get("flows"), "flows"):
        groups[(str(flow.get("provider")), str(flow.get("flow_type")))].append(flow)
    rows = []
    for (provider, flow_type), values in sorted(groups.items()):
        total = sum((_amount(item, "amount") for item in values), Decimal("0"))
        rows.append(
            {
                "flow_type": flow_type,
                "inferred_count": sum(
                    1 for item in values if item.get("evidence_class") == "INFERRED"
                ),
                "net_amount_usd": decimal_text(total),
                "provider": provider,
                "record_count": len(values),
                "status": "PARTIAL_SOURCE_COVERAGE",
            }
        )
    return rows


def _allocations(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in _objects(catalog.get("allocations"), "allocations"):
        rows.append(
            {
                "account_id": item.get("account_id"),
                "allocation_id": item.get("allocation_id"),
                "as_of_date": item.get("as_of_date"),
                "current_value_usd": decimal_text(_amount(item, "current_value")),
                "scope": item.get("scope"),
                "source_key": item.get("source_key"),
                "strategy_id": item.get("strategy_id"),
                "systematic_weight": item.get("systematic_weight"),
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            str(item["as_of_date"]),
            str(item["scope"]),
            str(item["strategy_id"] or ""),
        ),
    )


def _coverage(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for claim in _objects(catalog.get("fact_coverage"), "fact_coverage"):
        rows.append(
            {
                "category": "FACT_COVERAGE",
                "item": claim.get("fact_type"),
                "known_gap": "; ".join(
                    str(item) for item in claim.get("known_gaps", [])
                ),
                "status": claim.get("status"),
                "value": None,
            }
        )
    counts: dict[str, int] = defaultdict(int)
    for item in _objects(catalog.get("calendar_coverage"), "calendar_coverage"):
        counts[_text(item.get("status"), "calendar.status")] += 1
    for status, count in sorted(counts.items()):
        rows.append(
            {
                "category": "CALENDAR_EVIDENCE",
                "item": status,
                "known_gap": None,
                "status": "OBSERVED_COUNT",
                "value": count,
            }
        )
    for item in _objects(
        catalog.get("reconciliation_exceptions"), "reconciliation_exceptions"
    ):
        rows.append(
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": item.get("exception_id"),
                "known_gap": item.get("severity"),
                "status": item.get("status"),
                "value": item.get("record_count"),
            }
        )
    return rows


def _corrections(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in _objects(catalog.get("corrections"), "corrections"):
        fields = _object(item.get("fields"), "correction.fields")
        rows.append(
            {
                "correction_id": item.get("correction_id"),
                "evidence": fields.get("evidence"),
                "field": fields.get("field"),
                "new_value": fields.get("new_value"),
                "old_value": fields.get("old_value"),
                "source_id": item.get("source_id"),
            }
        )
    return sorted(rows, key=lambda item: str(item["correction_id"]))


def build_report_data(
    baseline_root: Path,
) -> tuple[dict[str, Any], bytes, Path]:
    """Build canonical report data from the currently accepted baseline."""

    catalog, baseline_ref, baseline_catalog_path = _baseline(baseline_root)
    performance = _account_strategy_performance(catalog)
    report: dict[str, Any] = {
        "account_summary": _accounts(catalog),
        "allocation_history": _allocations(catalog),
        "as_of_date": catalog.get("as_of_date"),
        "baseline": baseline_ref,
        "corrections": _corrections(catalog),
        "coverage_data_quality": _coverage(catalog),
        "flow_reconciliation": _flow_reconciliation(catalog),
        "methodology": [
            {"item": "Policy", "value": POLICY_VERSION},
            {
                "item": "TWR",
                "value": "Per mapped account-strategy segment; date-only flows use end-of-day timing and are labeled INFERRED_TIMING.",
            },
            {
                "item": "MWR",
                "value": "UNAVAILABLE unless complete eligible flows, signs, and terminal NAV are proven; the first baseline does not meet that gate.",
            },
            {
                "item": "Evidence layers",
                "value": "Account-live, provider-live, predecessor, shadow/OOS, backtest, official-vehicle, and conceptual-stress evidence remain separate.",
            },
            {
                "item": "Missing data",
                "value": "Missing NAV, flows, FX, costs, positions, or mappings yields UNAVAILABLE, PARTIAL, FAILED, or UNMAPPED; no interpolation.",
            },
            {
                "item": "Reserve",
                "value": "Included in total liquid and excluded from systematic strategy risk.",
            },
            {
                "item": "Workbook authority",
                "value": "Workbook formulas are display/reconciliation checks. Canonical JSON and tested code are calculation authority.",
            },
        ],
        "portfolio_summary": _portfolio_summary(catalog),
        "positions": [
            {
                "account_id": None,
                "as_of_date": catalog.get("as_of_date"),
                "market_value_usd": None,
                "quantity": None,
                "security_id": None,
                "status": "UNAVAILABLE_NO_ACCEPTED_POSITION_SOURCE",
                "strategy_id": None,
            }
        ],
        "provenance": [
            {
                "artifact": "baseline-generation",
                "as_of_date": catalog.get("as_of_date"),
                "schema": "generation",
                "sha256": baseline_ref["generation_id"],
            },
            {
                "artifact": "baseline-catalog",
                "as_of_date": catalog.get("as_of_date"),
                "schema": "portfolio-baseline-catalog/1",
                "sha256": baseline_ref["catalog_sha256"],
            },
            *[
                {
                    "artifact": item.get("source_key"),
                    "as_of_date": item.get("as_of_date"),
                    "schema": f"{item.get('schema_name')}/{item.get('schema_version')}",
                    "sha256": item.get("sha256"),
                }
                for item in _objects(
                    catalog.get("source_artifacts"), "source_artifacts"
                )
            ],
        ],
        "reconciliation_checks": catalog.get("reconciliation_checks", []),
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "strategy_summary": _strategies(catalog, performance),
        "transactions_and_flows": _transactions_and_flows(catalog),
        "twr_mwr": performance,
    }
    return report, canonical_json_bytes(report), baseline_catalog_path


def _excel_value(value: object, *, header: str | None = None) -> object:
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return value
        financial_display = header is not None and (
            header.endswith("_usd")
            or header.endswith("_ratio")
            or header
            in {
                "account_live_cagr",
                "account_live_cumulative_return",
                "account_live_max_drawdown",
                "mwr_xirr",
                "systematic_weight",
                "twr",
            }
        )
        if parsed.is_finite() and (len(value) < 30 or financial_display):
            return float(parsed)
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _headers(rows: Sequence[Mapping[str, Any]], fallback: Sequence[str]) -> list[str]:
    if not rows:
        return list(fallback)
    return list(rows[0])


def _source_format(header: str, formats: Mapping[str, Any]) -> Any:
    if header.endswith("_usd") or header in {"amount_usd", "current_value_usd"}:
        return formats["source_currency"]
    if header in {
        "account_live_cagr",
        "account_live_cumulative_return",
        "account_live_max_drawdown",
        "mwr_xirr",
        "systematic_weight",
        "twr",
    } or header.endswith("_ratio"):
        return formats["source_percent"]
    if any(
        token in header
        for token in ("boundary", "gap", "methodology", "status", "timing")
    ):
        return formats["source_wrap"]
    return formats["source"]


def _write_table(
    workbook: Any,
    worksheet: Any,
    *,
    title: str,
    subtitle: str,
    rows: Sequence[Mapping[str, Any]],
    table_name: str,
    fallback_headers: Sequence[str],
    formats: Mapping[str, Any],
) -> tuple[int, int]:
    worksheet.hide_gridlines(2)
    worksheet.freeze_panes(5, 0)
    worksheet.set_tab_color("#1F4E78")
    worksheet.merge_range("A1:H1", title, formats["title"])
    worksheet.merge_range("A2:H2", subtitle, formats["subtitle"])
    worksheet.write("A3", "READ-ONLY / ADVISORY", formats["advisory"])
    headers = _headers(rows, fallback_headers)
    data = [
        [_excel_value(row.get(header), header=header) for header in headers]
        for row in rows
    ]
    if not data:
        data = [[""] * len(headers)]
    worksheet.add_table(
        4,
        0,
        4 + len(data),
        len(headers) - 1,
        {
            "name": table_name,
            "style": "Table Style Medium 2",
            "columns": [
                {
                    "header": header.replace("_", " ").title(),
                    "format": _source_format(header, formats),
                }
                for header in headers
            ],
            "data": data,
        },
    )
    for column, header in enumerate(headers):
        width = min(
            44,
            max(
                12,
                len(header) + 3,
                *(len(str(row.get(header, ""))) + 2 for row in rows[:50]),
            ),
        )
        worksheet.set_column(column, column, width, _source_format(header, formats))
    return len(data), len(headers)


def _workbook(report: Mapping[str, Any], path: Path) -> dict[str, Any]:
    workbook = xlsxwriter.Workbook(path, {"constant_memory": False})
    workbook.set_properties(
        {
            "title": "quant_engine Unified Portfolio Report",
            "subject": "Read-only coverage-aware portfolio baseline",
            "author": "quant_engine",
            "company": "quant_engine",
            "comments": "Generated from an accepted private dataPortfolio baseline.",
            "created": datetime.combine(
                date.fromisoformat(str(report["as_of_date"])), time.min
            ),
        }
    )
    workbook.set_calc_mode("auto")
    formats = {
        "title": workbook.add_format(
            {
                "bold": True,
                "font_name": "Arial",
                "font_size": 18,
                "font_color": "#FFFFFF",
                "bg_color": "#17365D",
                "align": "left",
                "valign": "vcenter",
            }
        ),
        "subtitle": workbook.add_format(
            {
                "font_name": "Arial",
                "font_size": 10,
                "font_color": "#404040",
                "bg_color": "#D9EAF7",
                "text_wrap": True,
            }
        ),
        "advisory": workbook.add_format(
            {
                "bold": True,
                "font_name": "Arial",
                "font_color": "#9C0006",
                "bg_color": "#FFC7CE",
            }
        ),
        "source": workbook.add_format({"font_name": "Arial", "font_color": "#0000FF"}),
        "source_currency": workbook.add_format(
            {
                "font_name": "Arial",
                "font_color": "#0000FF",
                "num_format": "$#,##0;($#,##0);-",
            }
        ),
        "source_percent": workbook.add_format(
            {
                "font_name": "Arial",
                "font_color": "#0000FF",
                "num_format": "0.0%;(0.0%);-",
            }
        ),
        "source_wrap": workbook.add_format(
            {
                "font_name": "Arial",
                "font_color": "#0000FF",
                "text_wrap": True,
                "valign": "top",
            }
        ),
        "formula": workbook.add_format(
            {"font_name": "Arial", "font_color": "#008000", "bold": True}
        ),
        "currency": workbook.add_format(
            {"font_name": "Arial", "num_format": "$#,##0;($#,##0);-"}
        ),
        "percent": workbook.add_format(
            {"font_name": "Arial", "num_format": "0.0%;(0.0%);-"}
        ),
        "kpi_label": workbook.add_format(
            {
                "bold": True,
                "font_name": "Arial",
                "font_color": "#17365D",
                "bg_color": "#D9EAF7",
                "border": 1,
            }
        ),
        "kpi_value": workbook.add_format(
            {"bold": True, "font_name": "Arial", "font_size": 12, "border": 1}
        ),
    }
    table_specs = [
        ("Account Summary", "account_summary", "tblAccounts", ("account_id",)),
        ("Strategy Summary", "strategy_summary", "tblStrategies", ("strategy_id",)),
        ("Positions", "positions", "tblPositions", ("status",)),
        (
            "Transactions and Flows",
            "transactions_and_flows",
            "tblTransactionsFlows",
            ("record_id",),
        ),
        (
            "TWR MWR",
            "twr_mwr",
            "tblTwrMwr",
            ("account_id", "strategy_id", "twr_status"),
        ),
        (
            "Allocation History",
            "allocation_history",
            "tblAllocations",
            ("allocation_id",),
        ),
        ("Flow Reconciliation", "flow_reconciliation", "tblFlowRecon", ("provider",)),
        (
            "Source Reconciliation",
            "reconciliation_checks",
            "tblSourceRecon",
            ("check_id", "status"),
        ),
        (
            "Coverage Data Quality",
            "coverage_data_quality",
            "tblCoverage",
            ("category", "item", "status"),
        ),
        ("Corrections", "corrections", "tblCorrections", ("correction_id",)),
        ("Methodology", "methodology", "tblMethodology", ("item", "value")),
        ("Provenance", "provenance", "tblProvenance", ("artifact", "sha256")),
    ]

    summary = workbook.add_worksheet("Portfolio Summary")
    summary.hide_gridlines(2)
    summary.set_tab_color("#C69214")
    summary.merge_range("A1:H1", "Unified Portfolio Baseline", formats["title"])
    summary.merge_range(
        "A2:H2",
        f"Accepted data as of {report['as_of_date']} | Calculations: {POLICY_VERSION}",
        formats["subtitle"],
    )
    summary.write("A3", "READ-ONLY / ADVISORY", formats["advisory"])
    summary.write("A4", "Advisory readiness", formats["kpi_label"])
    readiness = next(
        row["value"]
        for row in report["portfolio_summary"]
        if row["metric"] == "Advisory readiness"
    )
    summary.write("B4", readiness, formats["kpi_value"])
    summary.write("D4", "Workbook total check", formats["kpi_label"])
    summary.write_formula(
        "E4",
        '=IF(ABS(SUM(tblStrategies[Current Value Usd])+SUMIFS(tblAllocations[Current Value Usd],tblAllocations[Scope],"RESERVE")-D8)<0.02,"PASS","FAIL")',
        formats["formula"],
        "PASS",
    )
    summary_rows = list(report["portfolio_summary"])
    headers = list(summary_rows[0])
    data = [[_excel_value(row[header]) for header in headers] for row in summary_rows]
    summary.add_table(
        5,
        0,
        5 + len(data),
        len(headers) - 1,
        {
            "name": "tblPortfolioSummary",
            "style": "Table Style Medium 2",
            "columns": [
                {"header": header.replace("_", " ").title()} for header in headers
            ],
            "data": data,
        },
    )
    summary.set_column("A:A", 30)
    summary.set_column("B:B", 22)
    summary.set_column("C:D", 28)
    for row_index, row in enumerate(summary_rows, start=6):
        if row["unit"] == "USD":
            summary.set_row(row_index, None, formats["source"])
            summary.write_number(
                row_index, 3, float(Decimal(str(row["value"]))), formats["currency"]
            )
        elif row["unit"] == "ratio":
            summary.write_number(
                row_index, 3, float(Decimal(str(row["value"]))), formats["percent"]
            )
    for index, sheet_name in enumerate(SHEET_NAMES[1:], start=16):
        summary.write_url(index, 0, f"internal:'{sheet_name}'!A1", string=sheet_name)

    for sheet_name, key, table_name, fallback in table_specs:
        worksheet = workbook.add_worksheet(sheet_name)
        _write_table(
            workbook,
            worksheet,
            title=sheet_name,
            subtitle=f"Generated from accepted baseline {report['baseline']['generation_id']}; unavailable facts remain explicit.",
            rows=list(report[key]),
            table_name=table_name,
            fallback_headers=fallback,
            formats=formats,
        )
        worksheet.write_url(
            "H3", "internal:'Portfolio Summary'!A1", string="Back to summary"
        )

    strategy_sheet = workbook.get_worksheet_by_name("Strategy Summary")
    strategy_rows = list(report["strategy_summary"])
    chart = workbook.add_chart({"type": "bar"})
    chart.add_series(
        {
            "name": "Current value (USD)",
            "categories": ["Strategy Summary", 5, 1, 4 + len(strategy_rows), 1],
            "values": ["Strategy Summary", 5, 3, 4 + len(strategy_rows), 3],
            "fill": {"color": "#5B9BD5"},
        }
    )
    chart.set_title({"name": "Current systematic allocation"})
    chart.set_legend({"none": True})
    chart.set_style(10)
    strategy_sheet.insert_chart("A17", chart, {"x_scale": 1.25, "y_scale": 1.15})
    workbook.close()
    return {"formula_count_expected_min": 1, "sheet_count": len(SHEET_NAMES)}


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise GenerationConflictError(f"candidate file differs: {path.name}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _build_workbook_exact(report: Mapping[str, Any], path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".report-", suffix=".xlsx", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        evidence = _workbook(report, temporary)
        content = temporary.read_bytes()
        _write_exact(path, content)
        return {**evidence, "workbook_sha256": hashlib.sha256(content).hexdigest()}
    finally:
        temporary.unlink(missing_ok=True)


def build_candidate(baseline_root: Path, output_root: Path) -> dict[str, Any]:
    report, report_bytes, _ = build_report_data(baseline_root)
    report_id = hashlib.sha256(report_bytes).hexdigest()
    candidate = output_root / "candidates" / report_id
    data_path = candidate / REPORT_DATA_NAME
    workbook_path = candidate / WORKBOOK_NAME
    excel_path = candidate / "excel" / WORKBOOK_NAME
    _write_exact(data_path, report_bytes)
    workbook_evidence = _build_workbook_exact(report, workbook_path)
    contract = {
        "min_row_counts": {
            "tblAccounts": 1,
            "tblAllocations": 10,
            "tblCoverage": 1,
            "tblMethodology": 7,
            "tblPortfolioSummary": 8,
            "tblPositions": 1,
            "tblProvenance": 2,
            "tblStrategies": 9,
            "tblTwrMwr": 1,
        },
        "prohibit_visible_errors": True,
        "required_sheets": list(SHEET_NAMES),
        "required_tables": list(TABLE_NAMES),
        "sentinel_cells": [
            {
                "cell": "B4",
                "expected": report["portfolio_summary"][-1]["value"],
                "sheet": "Portfolio Summary",
            },
            {"cell": "E4", "expected": "PASS", "sheet": "Portfolio Summary"},
            {"cell": "A6", "expected": "Policy", "sheet": "Methodology"},
            {"cell": "B6", "expected": POLICY_VERSION, "sheet": "Methodology"},
        ],
    }
    job = {
        "idempotency_key": f"portfolio-report-{report_id[:32]}",
        "schema_version": "2.0",
        "steps": [
            {
                "type": "open",
                "read_only": False,
                "update_links": False,
                "workbook_path": str(workbook_path),
            },
            {"type": "recalc", "mode": "full_rebuild", "timeout_seconds": 120},
            {"type": "save_as", "output_path": str(excel_path), "overwrite": True},
        ],
    }
    _write_exact(candidate / "workbook-contract.json", canonical_json_bytes(contract))
    job_bytes = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    _write_exact(candidate / "xlsx-job.json", job_bytes)
    return {
        "account_count": len(report["account_summary"]),
        "candidate_dir": str(candidate),
        "initial_workbook_sha256": workbook_evidence["workbook_sha256"],
        "report_data_sha256": report_id,
        "report_id": report_id,
        "strategy_count": len(report["strategy_summary"]),
        "twr_available_count": sum(
            1
            for item in report["twr_mwr"]
            if str(item["twr_status"]).startswith("AVAILABLE")
        ),
        "twr_row_count": len(report["twr_mwr"]),
        "workbook_path": str(workbook_path),
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def verify_workbook(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReportValidationError("Excel workbook is missing or linked")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if any(name.lower().startswith("xl/externallinks/") for name in names):
                raise ReportValidationError("workbook contains external links")
            if any(name.lower() == "xl/connections.xml" for name in names):
                raise ReportValidationError("workbook contains connections")
            if any(name.lower() == "xl/vbaproject.bin" for name in names):
                raise ReportValidationError("workbook contains macros")
            root = ET.fromstring(archive.read("xl/workbook.xml"))
            sheets = [
                element.attrib["name"]
                for element in root.iter()
                if _local_name(element.tag) == "sheet"
            ]
            if sheets != list(SHEET_NAMES):
                raise ReportValidationError("workbook sheets differ")
            formulas = 0
            for name in names:
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                    sheet_root = ET.fromstring(archive.read(name))
                    formulas += sum(
                        1 for item in sheet_root.iter() if _local_name(item.tag) == "f"
                    )
            if formulas < 1:
                raise ReportValidationError("workbook has no reconciliation formula")
            table_count = sum(
                1
                for name in names
                if name.startswith("xl/tables/table") and name.endswith(".xml")
            )
            if table_count != len(TABLE_NAMES):
                raise ReportValidationError("workbook table count differs")
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        raise ReportValidationError("workbook is not valid OOXML") from exc
    sha256, size = hash_file(path)
    return {
        "external_links": 0,
        "formula_count": formulas,
        "macro_parts": 0,
        "sheet_count": len(sheets),
        "size_bytes": size,
        "table_count": table_count,
        "workbook_sha256": sha256,
    }


def verify_candidate(
    baseline_root: Path, candidate_dir: Path, excel_workbook: Path
) -> dict[str, Any]:
    report, report_bytes, _ = build_report_data(baseline_root)
    report_id = hashlib.sha256(report_bytes).hexdigest()
    if candidate_dir.name != report_id:
        raise ReportValidationError("candidate directory identity differs")
    data_path = candidate_dir / REPORT_DATA_NAME
    if data_path.is_symlink() or data_path.read_bytes() != report_bytes:
        raise ReportValidationError(
            "candidate report data differs from current baseline"
        )
    workbook_evidence = verify_workbook(excel_workbook)
    return {
        "account_count": len(report["account_summary"]),
        "baseline_generation_id": report["baseline"]["generation_id"],
        "report_data_sha256": report_id,
        "report_id": report_id,
        "strategy_count": len(report["strategy_summary"]),
        "twr_available_count": sum(
            1
            for item in report["twr_mwr"]
            if str(item["twr_status"]).startswith("AVAILABLE")
        ),
        "twr_row_count": len(report["twr_mwr"]),
        **workbook_evidence,
    }


def _copy_exact(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != content:
            raise GenerationConflictError(
                f"generation artifact differs: {destination.name}"
            )
        return
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with (
        source.open("rb") as input_handle,
        os.fdopen(descriptor, "wb") as output_handle,
    ):
        shutil.copyfileobj(input_handle, output_handle)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def publish_report(
    baseline_root: Path,
    candidate_dir: Path,
    excel_workbook: Path,
    xlsx_result: Path,
    validation_result: Path,
    output_root: Path,
) -> dict[str, Any]:
    verification = verify_candidate(baseline_root, candidate_dir, excel_workbook)
    run = _load_json(xlsx_result, "xlsx-win run result")
    validation = _load_json(validation_result, "xlsx-win validation result")
    if run.get("ok") is not True:
        raise ReportValidationError("xlsx-win top-level result is not ok")
    if validation.get("all_passed") is not True:
        raise ReportValidationError("xlsx-win workbook invariants did not all pass")
    report, report_bytes, baseline_catalog_path = build_report_data(baseline_root)
    as_of = _date(report.get("as_of_date"), "report.as_of_date")
    verification_bytes = canonical_json_bytes(verification)
    contract_path = candidate_dir / "workbook-contract.json"
    artifacts = []
    artifact_specs = (
        (
            baseline_catalog_path,
            "input/portfolio-baseline.json",
            "portfolio-baseline-catalog",
            "1",
        ),
        (
            candidate_dir / REPORT_DATA_NAME,
            "normalized/portfolio-report-data.json",
            "portfolio-report-data",
            "1",
        ),
        (
            excel_workbook,
            "report/portfolio-report.xlsx",
            "portfolio-report-workbook",
            "1",
        ),
        (xlsx_result, "evidence/xlsx-win-result.json", "xlsx-win-result", "2.0"),
        (
            validation_result,
            "evidence/workbook-validation.json",
            "xlsx-win-validation",
            "1",
        ),
        (
            contract_path,
            "evidence/workbook-contract.json",
            "xlsx-win-validation-contract",
            "1",
        ),
    )
    for path, artifact_id, schema_name, schema_version in artifact_specs:
        artifacts.append(
            SourceArtifact.from_path(
                path,
                artifact_id=artifact_id,
                as_of_date=as_of,
                schema_name=schema_name,
                schema_version=schema_version,
            )
        )
    artifacts.append(
        SourceArtifact(
            artifact_id="evidence/report-verification.json",
            sha256=hashlib.sha256(verification_bytes).hexdigest(),
            size_bytes=len(verification_bytes),
            as_of_date=as_of,
            schema_name="portfolio-report-verification",
            schema_version="1",
        )
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-report-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="accepted baseline report data plus explicit workbook coverage and validation dispositions",
            fact_types=(
                "performance",
                "portfolio report",
                "reconciliation",
                "workbook validation",
            ),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    generation_dir = manifest_path.parent
    _copy_exact(candidate_dir / REPORT_DATA_NAME, generation_dir / REPORT_DATA_NAME)
    _copy_exact(excel_workbook, generation_dir / WORKBOOK_NAME)
    _copy_exact(xlsx_result, generation_dir / "xlsx-win-result.json")
    _copy_exact(validation_result, generation_dir / "workbook-validation.json")
    _copy_exact(contract_path, generation_dir / "workbook-contract.json")
    _write_exact(generation_dir / "report-verification.json", verification_bytes)
    pointer = publish_generation(output_root, manifest.generation_id)
    return {
        "generation_id": manifest.generation_id,
        "publication_state": pointer.state.value,
        "report_data_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "strategy_count": verification["strategy_count"],
        "twr_available_count": verification["twr_available_count"],
        "twr_row_count": verification["twr_row_count"],
        "workbook_sha256": verification["workbook_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--baseline-root", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--baseline-root", type=Path, required=True)
    verify.add_argument("--candidate-dir", type=Path, required=True)
    verify.add_argument("--excel-workbook", type=Path, required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--baseline-root", type=Path, required=True)
    publish.add_argument("--candidate-dir", type=Path, required=True)
    publish.add_argument("--excel-workbook", type=Path, required=True)
    publish.add_argument("--xlsx-result", type=Path, required=True)
    publish.add_argument("--validation-result", type=Path, required=True)
    publish.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_candidate(args.baseline_root, args.output_root)
        elif args.command == "verify":
            result = verify_candidate(
                args.baseline_root, args.candidate_dir, args.excel_workbook
            )
        else:
            result = publish_report(
                args.baseline_root,
                args.candidate_dir,
                args.excel_workbook,
                args.xlsx_result,
                args.validation_result,
                args.output_root,
            )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"portfolio-report: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
