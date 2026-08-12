"""Build, verify, and publish the private coverage-aware portfolio report."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time as time_module
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
from quant_engine.portfolio_ledger import (
    HwmCoverageError,
    decimal_text,
    governed_hwm_coverage,
)


REPORT_SCHEMA_VERSION: Final = 1
_CURRENT_READ_RETRY_DELAYS: Final = (0.25, 0.5, 1.0, 2.0)
BUILDER_VERSION: Final = "1.2.0"
POLICY_VERSION: Final = "PORT-PERF-1"
WORKBOOK_NAME: Final = "portfolio-report.xlsx"
REPORT_DATA_NAME: Final = "portfolio-report-data.json"
HWM_POLICY: Final = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v3.json"
)
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
WORKBOOK_CANONICALIZATION_VERSION: Final = "1"
XLSX_RESULT_CANONICALIZATION_VERSION: Final = "1"
_CANONICAL_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
_CORE_CREATED = re.compile(
    rb"(<(?:[A-Za-z_][\w.-]*:)?created\b[^>]*>)([^<]+)"
    rb"(</(?:[A-Za-z_][\w.-]*:)?created>)"
)
_CORE_MODIFIED = re.compile(
    rb"(<(?:[A-Za-z_][\w.-]*:)?modified\b[^>]*>)([^<]+)"
    rb"(</(?:[A-Za-z_][\w.-]*:)?modified>)"
)
_CHART_UNIQUE_ID = re.compile(
    rb'(<(?:[A-Za-z_][\w.-]*:)?uniqueId\b[^>]*\bval=")'
    rb'\{[0-9A-Fa-f-]{36}\}(?=")'
)
_WORKBOOK_ABS_PATH = re.compile(
    rb'(<(?:[A-Za-z_][\w.-]*:)?absPath\b[^>]*\burl=")[^"]*(?=")'
)
_WORKBOOK_REVISION_ID = re.compile(
    rb'(<(?:[A-Za-z_][\w.-]*:)?revisionPtr\b[^>]*\bdocumentId=")'
    rb'8_\{[0-9A-Fa-f-]{36}\}(?=")'
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


def _canonical_json_array_bytes(value: Sequence[Mapping[str, Any]]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _baseline(root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    for attempt in range(len(_CURRENT_READ_RETRY_DELAYS) + 1):
        try:
            pointer = read_current(root)
            break
        except OSError as exc:
            if attempt == len(_CURRENT_READ_RETRY_DELAYS):
                raise ReportValidationError("baseline pointer is not accepted") from exc
            time_module.sleep(_CURRENT_READ_RETRY_DELAYS[attempt])
        except (ValueError, RuntimeError) as exc:
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


def _attribution(
    root: Path, baseline_ref: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str], Path]:
    pointer = read_current(root)
    catalog_path = (
        root / "generations" / pointer.generation_id / "strategy-attribution.json"
    )
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ReportValidationError("strategy attribution catalog is missing or linked")
    catalog = _load_json(catalog_path, "strategy attribution catalog")
    if catalog.get("catalog_schema_version") != 1:
        raise ReportValidationError("strategy attribution schema is unsupported")
    attribution_baseline = _object(catalog.get("baseline"), "attribution.baseline")
    if attribution_baseline != dict(baseline_ref):
        raise ReportValidationError("strategy attribution baseline identity differs")
    sha256, _ = hash_file(catalog_path)
    return (
        catalog,
        {"catalog_sha256": sha256, "generation_id": pointer.generation_id},
        catalog_path,
    )


def _merge_attribution(
    strategies: Sequence[Mapping[str, Any]], attribution: Mapping[str, Any]
) -> list[dict[str, Any]]:
    benchmarks = {
        str(item.get("strategy_id")): item
        for item in _objects(
            attribution.get("benchmark_assignments"),
            "attribution.benchmark_assignments",
        )
    }
    comparisons = {
        str(item.get("strategy_id")): item
        for item in _objects(attribution.get("comparisons"), "attribution.comparisons")
    }
    costs = {
        str(item.get("strategy_id")): item
        for item in _objects(attribution.get("costs"), "attribution.costs")
    }
    strategy_ids = {str(item.get("strategy_id")) for item in strategies}
    if (
        set(benchmarks) != strategy_ids
        or set(comparisons) != strategy_ids
        or set(costs) != strategy_ids
    ):
        raise ReportValidationError("strategy attribution boundary differs")
    result = []
    for item in strategies:
        strategy_id = str(item["strategy_id"])
        benchmark = benchmarks[strategy_id]
        comparison = comparisons[strategy_id]
        cost = costs[strategy_id]
        metrics_value = comparison.get("metrics")
        metrics = (
            {}
            if metrics_value is None
            else _object(metrics_value, "attribution.comparison.metrics")
        )
        result.append(
            {
                **item,
                "benchmark_assignment_status": benchmark.get("status"),
                "benchmark_id": benchmark.get("benchmark_id"),
                "benchmark_effective_start": benchmark.get("effective_start"),
                "benchmark_effective_end": benchmark.get("effective_end"),
                "benchmark_version_ref": benchmark.get("version_ref"),
                "benchmark_rationale": benchmark.get("rationale"),
                "provider_comparison_status": comparison.get("status"),
                "provider_comparison_disposition": comparison.get("disposition"),
                "provider_comparison_version_compatibility": comparison.get(
                    "version_compatibility_status"
                ),
                "provider_comparison_observations": metrics.get("observations"),
                "provider_comparison_observed_start": metrics.get("observed_start"),
                "provider_comparison_observed_end": metrics.get("observed_end"),
                "provider_account_cumulative_return": metrics.get(
                    "account_cumulative_return"
                ),
                "provider_reference_cumulative_return": metrics.get(
                    "reference_cumulative_return"
                ),
                "provider_cumulative_gap": metrics.get("cumulative_gap"),
                "provider_active_return_annualized": metrics.get(
                    "active_return_annualized"
                ),
                "provider_tracking_error_annualized": metrics.get(
                    "tracking_error_annualized"
                ),
                "provider_information_ratio": metrics.get("information_ratio"),
                "provider_beta": metrics.get("beta"),
                "provider_correlation": metrics.get("correlation"),
                "provider_upside_capture": metrics.get("upside_capture"),
                "provider_downside_capture": metrics.get("downside_capture"),
                "provider_relative_drawdown": metrics.get("relative_drawdown"),
                "cash_drag_status": comparison.get("cash_drag_status"),
                "cash_drag_reason": comparison.get("cash_drag_reason"),
                "legacy_baseline_disposition": comparison.get(
                    "legacy_baseline_disposition"
                ),
                "annual_external_cost_rate_pre_tax": cost.get(
                    "annual_external_cost_rate_pre_tax"
                ),
                "annual_external_cost_usd": cost.get("annual_external_cost_usd"),
                "after_tax_annual_cost_rate": cost.get("after_tax_annual_cost_rate"),
                "after_tax_status": cost.get("after_tax_status"),
                "fees_status": cost.get("fees_status"),
            }
        )
    return result


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
    account_live_paths: dict[tuple[str, str], dict[str, Any]] = {}
    canonical_live = _account_live_return_series(catalog)
    for evidence in _objects(
        catalog.get("strategy_provider_evidence", []),
        "strategy_provider_evidence",
    ):
        if evidence.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy_id = _text(evidence.get("strategy_id"), "evidence.strategy_id")
        canonical = canonical_live.get(strategy_id)
        if canonical is None:
            continue
        account = _object(evidence.get("account"), "evidence.account")
        for scope in _objects(account.get("account_scopes", []), "account_scopes"):
            account_id = _text(scope.get("account_id"), "account_scope.account_id")
            account_live_paths[(account_id, strategy_id)] = {
                "return_path_authority": "CANONICAL_ACCOUNT_LIVE_STRATEGY_RETURNS",
                "return_path_cumulative_return": canonical["cumulative_return"],
                "return_path_evidence_layer": "ACCOUNT_LIVE",
                "return_path_generation_id": evidence.get("generation_id"),
                "return_path_observations": canonical["return_observations"],
                "return_path_observed_end": canonical["observed_end"],
                "return_path_observed_start": canonical["observed_start"],
                "return_path_series_sha256": canonical["return_series_sha256"],
                "return_path_status": "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH",
            }
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
            "pool_id": assignment.get("pool_id"),
            "pool_mapping_status": assignment.get("pool_mapping_status"),
            "strategy_mapping_disposition": assignment.get(
                "strategy_mapping_disposition"
            ),
            "strategy_id": assignment.get("strategy_id"),
            "twr": None,
            "twr_status": "UNAVAILABLE",
        }
        if mapping_status != "MAPPED" or assignment.get("strategy_id") is None:
            base["twr_status"] = "UNMAPPED"
            rows.append(base)
            continue
        strategy_id = _text(assignment.get("strategy_id"), "assignment.strategy_id")
        base.update(account_live_paths.get((account_id, strategy_id), {}))
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
        if any(
            _object(item.get("amount"), "flow.amount").get("status") != "AVAILABLE"
            for item in flows
        ):
            base["twr_status"] = "UNAVAILABLE_FLOW_AMOUNT"
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


def _portfolio_summary(
    catalog: Mapping[str, Any], hwm_summary: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
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
            "status": (
                "COMPLETE_WITHIN_DECLARED_BOUNDARY"
                if hwm_summary is not None and hwm_summary.get("nav_coverage") == "1"
                else "PARTIAL"
            ),
            "unit": "ratio",
            "value": (
                hwm_summary.get("nav_coverage")
                if hwm_summary is not None
                else replay.get("nav_coverage")
            ),
        },
        {
            "metric": "Correlation-one stress",
            "status": "ADVERSE_SCENARIO",
            "unit": "ratio",
            "value": replay.get("correlation_one_stress"),
        },
        {
            "metric": "Full-systematic HWM",
            "status": (
                hwm_summary.get("status") if hwm_summary is not None else "UNAVAILABLE"
            ),
            "unit": "status",
            "value": (
                hwm_summary.get("status")
                if hwm_summary is not None
                else replay.get("full_systematic_hwm_status")
            ),
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


def _account_live_return_series(
    catalog: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Hash and summarize each canonical account-live strategy return path."""

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    providers: dict[str, set[str]] = defaultdict(set)
    for row in _objects(catalog.get("strategy_returns", []), "strategy_returns"):
        if row.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy_id = _text(row.get("strategy_id"), "strategy return.strategy_id")
        day = _date(row.get("date"), "strategy return.date").isoformat()
        value = decimal_text(_decimal(row.get("return"), "strategy return.return"))
        grouped[strategy_id].append({"date": day, "return": value})
        providers[strategy_id].add(
            _text(row.get("provider_id"), "strategy return.provider_id")
        )

    result: dict[str, dict[str, Any]] = {}
    for strategy_id, rows in grouped.items():
        rows.sort(key=lambda item: item["date"])
        dates = [item["date"] for item in rows]
        if len(dates) != len(set(dates)):
            raise ReportValidationError(
                f"account-live strategy returns repeat a date for {strategy_id}"
            )
        with localcontext() as context:
            context.prec = 34
            wealth = Decimal("1")
            peak = wealth
            maximum_drawdown = Decimal("0")
            for item in rows:
                wealth *= Decimal("1") + _decimal(
                    item["return"], "canonical strategy return"
                )
                peak = max(peak, wealth)
                drawdown = (wealth / peak) - Decimal("1")
                maximum_drawdown = min(maximum_drawdown, drawdown)
            cumulative_return = wealth - Decimal("1")
            current_drawdown = (wealth / peak) - Decimal("1")
        result[strategy_id] = {
            "cumulative_return": decimal_text(cumulative_return),
            "current_drawdown": decimal_text(current_drawdown),
            "maximum_drawdown": decimal_text(maximum_drawdown),
            "observed_end": dates[-1],
            "observed_start": dates[0],
            "provider_ids": sorted(providers[strategy_id]),
            "return_observations": len(rows),
            "return_series_schema": "ordered-date-return/1",
            "return_series_sha256": hashlib.sha256(
                _canonical_json_array_bytes(rows)
            ).hexdigest(),
        }
    return result


def _close_decimal(left: object, right: object, field_name: str) -> None:
    difference = abs(_decimal(left, field_name) - _decimal(right, field_name))
    if difference > Decimal("0.000000000000000000000001"):
        raise ReportValidationError(
            f"{field_name} differs from canonical return series"
        )


def _cagr(cumulative_return: str, observed_start: str, observed_end: str) -> str | None:
    start = _date(observed_start, "account-live observed_start")
    end = _date(observed_end, "account-live observed_end")
    elapsed_days = (end - start).days
    cumulative = _decimal(cumulative_return, "account-live cumulative_return")
    if elapsed_days <= 0 or cumulative <= Decimal("-1"):
        return None
    with localcontext() as context:
        context.prec = 34
        annualized = context.power(
            Decimal("1") + cumulative, Decimal("365") / Decimal(elapsed_days)
        ) - Decimal("1")
    return decimal_text(annualized)


def _strategy_provider_evidence(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    series = _account_live_return_series(catalog)
    rows: list[dict[str, Any]] = []
    for original in _objects(
        catalog.get("strategy_provider_evidence", []),
        "strategy_provider_evidence",
    ):
        row = dict(original)
        strategy_id = _text(row.get("strategy_id"), "provider evidence.strategy_id")
        if row.get("evidence_layer") == "ACCOUNT_LIVE":
            canonical = series.get(strategy_id)
            if canonical is None:
                raise ReportValidationError(
                    f"account-live evidence has no canonical return series for {strategy_id}"
                )
            account = _object(row.get("account"), "provider evidence account")
            for field_name in (
                "cumulative_return",
                "current_drawdown",
                "maximum_drawdown",
            ):
                _close_decimal(
                    account.get(field_name),
                    canonical[field_name],
                    f"{strategy_id}.{field_name}",
                )
            for field_name in ("observed_start", "observed_end", "return_observations"):
                if account.get(field_name) != canonical[field_name]:
                    raise ReportValidationError(
                        f"{strategy_id}.{field_name} differs from canonical return series"
                    )
            provider_id = _text(account.get("provider_id"), "account.provider_id")
            if provider_id not in canonical["provider_ids"]:
                raise ReportValidationError(
                    f"{strategy_id}.provider_id differs from canonical return series"
                )
            row["account"] = {
                **account,
                "cumulative_return": canonical["cumulative_return"],
                "current_drawdown": canonical["current_drawdown"],
                "maximum_drawdown": canonical["maximum_drawdown"],
                "return_series_schema": canonical["return_series_schema"],
                "return_series_sha256": canonical["return_series_sha256"],
            }
        rows.append(row)
    return rows


def _strategies(
    catalog: Mapping[str, Any], performance: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    raw_version_catalog = catalog.get("strategy_version_catalog")
    strategy_versions: dict[str, dict[str, Any]] = {}
    if raw_version_catalog is not None:
        version_catalog = _object(raw_version_catalog, "strategy_version_catalog")
        if version_catalog.get("catalog_schema_version") != 2:
            raise ReportValidationError(
                "strategy version catalog schema is unsupported"
            )
        if version_catalog.get("as_of_date") != catalog.get("as_of_date"):
            raise ReportValidationError(
                "strategy version catalog as_of_date differs from baseline"
            )
        strategy_versions = {
            _text(item.get("strategy_id"), "strategy version strategy_id"): item
            for item in _objects(
                version_catalog.get("strategies"), "strategy version catalog"
            )
        }
        if len(strategy_versions) != 9:
            raise ReportValidationError(
                "strategy version catalog must contain exactly 9 strategies"
            )
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
    provider_evidence = {
        str(item["strategy_id"]): item for item in _strategy_provider_evidence(catalog)
    }
    live_source = next(
        (
            item
            for item in _objects(
                catalog.get("source_artifacts", []), "source_artifacts"
            )
            if item.get("source_key") == "strategy-live-metrics"
        ),
        {},
    )
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
        provider_row = provider_evidence.get(strategy_id)
        account_path = (
            _object(provider_row["account"], "provider evidence account")
            if provider_row and provider_row.get("account") is not None
            else {}
        )
        provider_path = (
            _object(provider_row["provider"], "provider evidence provider")
            if provider_row and provider_row.get("provider") is not None
            else {}
        )
        admitted_account_nav = bool(
            provider_row
            and provider_row.get("evidence_layer") == "ACCOUNT_LIVE"
            and account_path.get("observed_start") is not None
            and account_path.get("observed_end") is not None
            and account_path.get("return_observations") is not None
        )
        admitted_provider_nav = bool(
            provider_row
            and provider_row.get("evidence_layer") == "PROVIDER_LIVE"
            and provider_path.get("observed_start") is not None
            and provider_path.get("observed_end") is not None
            and provider_path.get("return_observations") is not None
        )
        canonical_account = account_path if admitted_account_nav else {}
        legacy_account = metrics if provider_row is None and live_row else {}
        account_authority = (
            "CANONICAL_STRATEGY_RETURNS"
            if canonical_account
            else "AUDIT_STRATEGY_LIVE_METRICS"
            if legacy_account
            else None
        )
        account_metrics = canonical_account or legacy_account
        strategy_path = canonical_account if canonical_account else provider_path
        observed_start = account_metrics.get("observed_start") or account_metrics.get(
            "first_date"
        )
        observed_end = account_metrics.get("observed_end") or account_metrics.get(
            "last_date"
        )
        cumulative_return = account_metrics.get("cumulative_return")
        account_cagr = (
            _cagr(cumulative_return, observed_start, observed_end)
            if canonical_account
            and cumulative_return is not None
            and observed_start is not None
            and observed_end is not None
            else account_metrics.get("cagr")
        )
        cost_row = costs.get(strategy_id, {})
        gap_row = gaps.get(strategy_id, {})
        coverage_row = coverage.get(strategy_id, {})
        version = strategy_versions.get(strategy_id, {})
        if strategy_versions and not version:
            raise ReportValidationError(
                f"strategy version catalog is missing {strategy_id}"
            )
        design = (
            _object(version.get("design"), "strategy version design") if version else {}
        )
        configuration = (
            _object(version.get("configuration"), "strategy version configuration")
            if version
            else {}
        )
        backtest = (
            _object(version.get("backtest"), "strategy version backtest")
            if version
            else {}
        )
        live_boundary = (
            _object(version.get("live_boundary"), "strategy version live boundary")
            if version
            else {}
        )
        version_costs = (
            _object(version.get("costs"), "strategy version costs") if version else {}
        )
        benchmark = (
            _object(version.get("benchmark"), "strategy version benchmark")
            if version
            else {}
        )
        source_route = (
            _object(version.get("source_route"), "strategy version source route")
            if version
            else {}
        )
        layer_inventory = (
            [
                {
                    "artifact_sha256": item.get("artifact_sha256"),
                    "disposition": item.get("disposition"),
                    "evidence_layer": item.get("evidence_layer"),
                    "observed_end": item.get("observed_end"),
                    "observed_start": item.get("observed_start"),
                    "source_id": item.get("source_id"),
                    "status": item.get("status"),
                    "version_ref": item.get("version_ref"),
                }
                for item in _objects(
                    version.get("evidence_layers"),
                    "strategy version evidence layers",
                )
            ]
            if version
            else []
        )
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
                "account_live_status": (
                    "AVAILABLE" if account_metrics else "UNAVAILABLE"
                ),
                "account_live_cagr": account_cagr,
                "account_live_cagr_status": (
                    "AVAILABLE_DERIVED_SAME_CANONICAL_PATH"
                    if canonical_account and account_cagr is not None
                    else "AVAILABLE_SOURCE_REPORTED_SAME_AUDIT_VINTAGE"
                    if legacy_account and account_cagr is not None
                    else "UNAVAILABLE"
                ),
                "account_live_cumulative_return": cumulative_return,
                "account_live_max_drawdown": account_metrics.get("maximum_drawdown")
                or account_metrics.get("max_drawdown"),
                "account_live_observed_end": observed_end,
                "account_live_observed_start": observed_start,
                "account_live_return_authority": account_authority,
                "account_live_return_observations": account_metrics.get(
                    "return_observations"
                ),
                "account_live_return_series_schema": account_metrics.get(
                    "return_series_schema"
                ),
                "account_live_return_series_sha256": account_metrics.get(
                    "return_series_sha256"
                ),
                "account_live_source_generation": (
                    account_path.get("generation_id") if canonical_account else None
                ),
                "account_live_source_generation_as_of_date": (
                    account_path.get("generation_as_of_date")
                    if canonical_account
                    else live_source.get("as_of_date")
                ),
                "account_live_source_provider": (
                    account_path.get("provider_id") if canonical_account else None
                ),
                "account_live_source_sha256": (
                    None if canonical_account else live_source.get("sha256")
                ),
                "nav_status": (
                    "AVAILABLE"
                    if admitted_account_nav or admitted_provider_nav
                    else coverage_row.get("status", "UNAVAILABLE")
                ),
                "nav_observed_start": strategy_path.get("observed_start"),
                "nav_observed_end": strategy_path.get("observed_end"),
                "nav_return_observations": strategy_path.get("return_observations"),
                "hwm_current_drawdown": strategy_path.get("current_drawdown"),
                "hwm_max_drawdown": strategy_path.get("maximum_drawdown"),
                "nav_evidence_layer": (
                    provider_row.get("evidence_layer") if provider_row else None
                ),
                "cost_status": cost_row.get("cost_status", "UNAVAILABLE"),
                "implementation_gap_status": gap_row.get("status", "UNAVAILABLE"),
                "version_ref": version.get("version_ref", strategy.get("version_ref")),
                "version_disposition": version.get(
                    "disposition", strategy.get("version_disposition", "UNRESOLVED")
                ),
                "version_disposition_reason": version.get(
                    "disposition_reason",
                    "Legacy provenance has no effective-dated strategy version row.",
                ),
                "version_effective_start": version.get("effective_start"),
                "version_effective_end": version.get("effective_end"),
                "version_compatibility_status": version.get(
                    "compatibility_status", "UNRESOLVED"
                ),
                "version_compatibility_reason": version.get(
                    "compatibility_reason",
                    "Compatibility is unavailable without an effective-dated registry row.",
                ),
                "design_status": design.get("status", "UNAVAILABLE"),
                "design_version_ref": design.get("version_ref"),
                "configuration_status": configuration.get("status", "UNAVAILABLE"),
                "configuration_version_ref": configuration.get("version_ref"),
                "backtest_status": backtest.get("status", "UNAVAILABLE"),
                "backtest_version_ref": backtest.get("version_ref"),
                "live_boundary_status": live_boundary.get("status", "UNAVAILABLE"),
                "live_boundary_observed_start": live_boundary.get("observed_start"),
                "live_boundary_observed_end": live_boundary.get("observed_end"),
                "cost_evidence_status": version_costs.get("status", "UNAVAILABLE"),
                "benchmark_evidence_status": benchmark.get("status", "UNAVAILABLE"),
                "source_route_status": source_route.get("status", "UNAVAILABLE"),
                "source_route_ids": source_route.get("source_ids", []),
                "strategy_evidence_layers": layer_inventory,
                "version_gaps": version.get("gaps", strategy.get("gaps", [])),
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
        amount = _object(record.get("amount"), "transaction.amount")
        amount_status = _text(amount.get("status"), "transaction.amount.status")
        rows.append(
            {
                "account_id": record.get("account_id"),
                "amount_status": amount_status,
                "amount_usd": (
                    decimal_text(_amount(record, "amount"))
                    if amount_status == "AVAILABLE"
                    else None
                ),
                "date": record.get("event_date"),
                "evidence_class": record.get("evidence_class") or "OBSERVED",
                "kind": record.get("kind"),
                "price": record.get("price") or record.get("execution_price"),
                "provider": record.get("provider"),
                "quantity": record.get("quantity"),
                "record_id": record.get("record_id"),
                "record_type": "TRANSACTION",
                "strategy_id": record.get("strategy_id"),
                "symbol": security.get("symbol") or record.get("symbol"),
            }
        )
    for record in _objects(catalog.get("flows"), "flows"):
        amount = _object(record.get("amount"), "flow.amount")
        amount_status = _text(amount.get("status"), "flow.amount.status")
        rows.append(
            {
                "account_id": record.get("account_id"),
                "amount_status": amount_status,
                "amount_usd": (
                    decimal_text(_amount(record, "amount"))
                    if amount_status == "AVAILABLE"
                    else None
                ),
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


def _positions(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _objects(catalog.get("positions"), "positions")
    if not rows:
        return [
            {
                "account_id": None,
                "as_of_date": catalog.get("as_of_date"),
                "evidence_class": None,
                "market_value_usd": None,
                "quantity": None,
                "security_id": None,
                "status": "UNAVAILABLE_NO_ACCEPTED_POSITION_SOURCE",
                "strategy_id": None,
                "symbol": None,
            }
        ]
    latest_by_account: dict[str, str] = {}
    for item in rows:
        account_id = _text(item.get("account_id"), "position.account_id")
        as_of_date = _text(item.get("as_of_date"), "position.as_of_date")
        if (
            account_id not in latest_by_account
            or as_of_date > latest_by_account[account_id]
        ):
            latest_by_account[account_id] = as_of_date
    accepted: list[dict[str, Any]] = []
    for item in rows:
        account_id = _text(item.get("account_id"), "position.account_id")
        as_of_date = _text(item.get("as_of_date"), "position.as_of_date")
        amount = _object(item.get("market_value"), "position.market_value")
        status = _text(amount.get("status"), "position.market_value.status")
        accepted.append(
            {
                "account_id": item.get("account_id"),
                "as_of_date": item.get("as_of_date"),
                "evidence_class": item.get("evidence_class"),
                "market_value_usd": (
                    decimal_text(_amount(item, "market_value"))
                    if status == "AVAILABLE"
                    else None
                ),
                "quantity": item.get("quantity"),
                "security_id": item.get("security_id"),
                "snapshot_status": (
                    "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT"
                    if as_of_date == latest_by_account[account_id]
                    else "HISTORICAL_ACCEPTED_ACCOUNT_SNAPSHOT"
                ),
                "status": (
                    "AVAILABLE_UNMAPPED"
                    if item.get("mapping_status") == "UNMAPPED"
                    else "AVAILABLE_MAPPED"
                ),
                "strategy_id": item.get("strategy_id"),
                "symbol": item.get("symbol"),
            }
        )
    return sorted(
        accepted,
        key=lambda item: (
            str(item["account_id"]),
            str(item["as_of_date"]),
            str(item["security_id"]),
        ),
    )


def _flow_reconciliation(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for flow in _objects(catalog.get("flows"), "flows"):
        groups[(str(flow.get("provider")), str(flow.get("flow_type")))].append(flow)
    rows = []
    for (provider, flow_type), values in sorted(groups.items()):
        available = [
            item
            for item in values
            if _object(item.get("amount"), "flow.amount").get("status") == "AVAILABLE"
        ]
        total = sum((_amount(item, "amount") for item in available), Decimal("0"))
        rows.append(
            {
                "flow_type": flow_type,
                "inferred_count": sum(
                    1 for item in values if item.get("evidence_class") == "INFERRED"
                ),
                "net_amount_usd": decimal_text(total),
                "provider": provider,
                "record_count": len(values),
                "status": (
                    "PARTIAL_UNAVAILABLE_AMOUNTS"
                    if len(available) != len(values)
                    else "PARTIAL_SOURCE_COVERAGE"
                ),
                "unavailable_amount_count": len(values) - len(available),
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
                "value": claim.get("record_count"),
                **(
                    {
                        "disposition": claim.get("disposition"),
                        "evidence_authority": claim.get("disposition", {}).get(
                            "authority"
                        ),
                    }
                    if isinstance(claim.get("disposition"), Mapping)
                    else {}
                ),
            }
        )
    for detail in _objects(
        catalog.get("fact_coverage_matrix", []), "fact_coverage_matrix"
    ):
        rows.append(
            {
                **detail,
                "category": "FACT_COVERAGE_DETAIL",
                "evidence_authority": detail.get("authority"),
                "item": (f"{detail.get('fact_type')}:{detail.get('account_id')}"),
                "known_gap": detail.get("missing_interval_reason"),
                "value": detail.get("record_count"),
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
                **(
                    {
                        "disposition": item.get("disposition"),
                        "resolution_authority": item.get("authority"),
                    }
                    if item.get("disposition") is not None
                    or item.get("authority") is not None
                    else {}
                ),
            }
        )
    for item in _objects(catalog.get("provider_coverage", []), "provider_coverage"):
        claim = _object(item.get("coverage"), "provider coverage")
        limitations = [
            *claim.get("known_gaps", []),
            *claim.get("out_of_boundary", []),
        ]
        rows.append(
            {
                "category": "PROVIDER_COVERAGE",
                "item": item.get("provider_id"),
                "known_gap": "; ".join(str(gap) for gap in limitations),
                "status": claim.get("status"),
                "value": item.get("qualification"),
                **(
                    {
                        "observed_end": claim.get("observed_end"),
                        "observed_start": claim.get("observed_start"),
                    }
                    if "observed_end" in claim or "observed_start" in claim
                    else {}
                ),
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


def _provider_ledger_exceptions(
    catalog: Mapping[str, Any],
    baseline_root: Path,
    provider_ledger_root: Path | None = None,
) -> list[dict[str, Any]]:
    references = [
        item
        for item in _objects(catalog.get("source_artifacts"), "source_artifacts")
        if item.get("source_key") == "provider-ledger-generation"
        and item.get("schema_name") == "provider-ledger-catalog"
    ]
    if not references:
        return []
    if len(references) != 1:
        raise ReportValidationError(
            "baseline provider-ledger catalog reference is ambiguous"
        )
    reference = references[0]
    expected_sha256 = _text(reference.get("sha256"), "provider-ledger sha256")
    candidates: list[tuple[Path, str]] = []
    generations_root = (
        provider_ledger_root
        if provider_ledger_root is not None
        else baseline_root.parent / "provider-ledger"
    ) / "generations"
    if generations_root.is_dir():
        for path in sorted(generations_root.glob("*/provider-ledger.json")):
            if path.is_symlink() or not path.is_file():
                continue
            observed_sha256, _ = hash_file(path)
            if observed_sha256 == expected_sha256:
                candidates.append((path, observed_sha256))
    if len(candidates) != 1:
        raise ReportValidationError(
            "hash-bound provider-ledger catalog is missing or ambiguous"
        )
    path, observed_sha256 = candidates[0]
    generation_id = path.parent.name
    if len(generation_id) != 64 or any(
        character not in "0123456789abcdef" for character in generation_id
    ):
        raise ReportValidationError("provider-ledger generation identity is invalid")
    provider_catalog = _load_json(path, "provider-ledger catalog")
    exceptions = [
        item
        for item in _objects(
            provider_catalog.get("reconciliation_checks"),
            "provider-ledger reconciliation_checks",
        )
        if item.get("status") == "EXCEPTION"
    ]
    return [
        {
            "authority_effect": item.get("authority_effect"),
            "check_id": _text(item.get("check_id"), "provider check.check_id"),
            "details_json": canonical_json_bytes(item).decode("utf-8"),
            "exception_date_count": item.get("exception_date_count"),
            "maximum_absolute_difference": item.get("maximum_absolute_difference"),
            "source_artifact_id": reference.get("artifact_id"),
            "source_as_of_date": reference.get("as_of_date"),
            "source_catalog_sha256": observed_sha256,
            "source_generation_id": generation_id,
            "source_layer": "PROVIDER_LEDGER",
            "status": "EXCEPTION",
            "strategy_id": item.get("strategy_id"),
            "tolerance": item.get("tolerance"),
        }
        for item in exceptions
    ]


def _reconciliation_checks(
    catalog: Mapping[str, Any],
    baseline_root: Path,
    baseline_ref: Mapping[str, Any],
    provider_ledger_root: Path | None = None,
) -> list[dict[str, Any]]:
    rows = [
        {
            "authority_effect": item.get("authority_effect"),
            "check_id": _text(item.get("check_id"), "reconciliation check.check_id"),
            "details_json": canonical_json_bytes(item).decode("utf-8"),
            "exception_date_count": item.get("exception_date_count"),
            "maximum_absolute_difference": item.get("maximum_absolute_difference"),
            "source_artifact_id": "normalized/portfolio-baseline.json",
            "source_as_of_date": catalog.get("as_of_date"),
            "source_catalog_sha256": baseline_ref["catalog_sha256"],
            "source_generation_id": baseline_ref["generation_id"],
            "source_layer": "PORTFOLIO_BASELINE",
            "status": item.get("status"),
            "strategy_id": item.get("strategy_id"),
            "tolerance": item.get("tolerance"),
        }
        for item in _objects(
            catalog.get("reconciliation_checks"), "reconciliation_checks"
        )
    ]
    rows.extend(
        _provider_ledger_exceptions(catalog, baseline_root, provider_ledger_root)
    )
    check_ids = [str(item["check_id"]) for item in rows]
    if len(check_ids) != len(set(check_ids)):
        raise ReportValidationError("report reconciliation check identifiers repeat")
    return rows


def _freshness_assessments(
    path: Path, portfolio_as_of: date
) -> tuple[list[dict[str, Any]], date, str]:
    payload = _load_json(path, "freshness health")
    schema_version = payload.get("assessment_schema_version")
    expected = (
        {"assessment_schema_version", "as_of_date", "assessments", "policy"}
        if schema_version == 2
        else {"assessment_schema_version", "as_of_date", "assessments", "policies"}
    )
    if schema_version not in {2, 3} or set(payload) != expected:
        raise ReportValidationError("freshness health contract differs")
    as_of = _date(payload.get("as_of_date"), "freshness health as_of_date")
    if as_of < portfolio_as_of:
        raise ReportValidationError("freshness health predates the portfolio")
    assessments = _objects(payload.get("assessments"), "freshness assessments")
    if not assessments:
        raise ReportValidationError("freshness assessments are empty")
    evaluated = {
        _date(item.get("evaluated_on"), "freshness evaluated_on")
        for item in assessments
    }
    if evaluated != {as_of}:
        raise ReportValidationError("freshness evaluation date is inconsistent")
    sha256, _size = hash_file(path)
    return assessments, as_of, sha256


def _hwm_policy(evaluation_date: date) -> tuple[dict[str, Any], str]:
    policy = _load_json(HWM_POLICY, "HWM policy")
    if policy.get("schema_version") != 2 or policy.get("policy_id") != "PORT-HWM-3":
        raise ReportValidationError("HWM policy identity is unsupported")
    perimeter = _object(policy.get("material_perimeter"), "material_perimeter")
    exclusions = _objects(perimeter.get("exclusions"), "material_perimeter.exclusions")
    excluded_ids: set[str] = set()
    for item in exclusions:
        strategy_id = _text(item.get("strategy_id"), "HWM exclusion strategy_id")
        if strategy_id in excluded_ids:
            raise ReportValidationError("HWM exclusion is duplicated")
        if item.get("classification") != "EXCLUDED_DE_MINIMIS":
            raise ReportValidationError("HWM exclusion classification differs")
        effective_date = _date(item.get("effective_date"), "HWM exclusion date")
        if effective_date <= evaluation_date:
            excluded_ids.add(strategy_id)
    accepted_evidence = policy.get("accepted_daily_nav_evidence")
    accepted_provider_live = policy.get("accepted_provider_live_strategy_ids")
    if not isinstance(accepted_evidence, list) or not all(
        isinstance(item, str) and item for item in accepted_evidence
    ):
        raise ReportValidationError("HWM accepted evidence differs")
    if not isinstance(accepted_provider_live, list) or not all(
        isinstance(item, str) and item for item in accepted_provider_live
    ):
        raise ReportValidationError("HWM provider-live allowlist differs")
    policy["excluded_strategy_ids"] = sorted(excluded_ids)
    policy["evaluation_date"] = evaluation_date.isoformat()
    sha256, _size = hash_file(HWM_POLICY)
    return policy, sha256


def _reconcile_hwm_coverage(
    coverage: Sequence[Mapping[str, Any]], hwm_summary: Mapping[str, Any]
) -> list[dict[str, Any]]:
    normalized = [dict(row) for row in coverage]
    missing = [str(name) for name in hwm_summary["uncovered_strategies"]]
    details = [
        dict(item)
        for item in hwm_summary.get("uncovered_details", [])
        if isinstance(item, Mapping)
    ]
    detail_by_name = {str(item.get("strategy_name")): item for item in details}
    gap_details = ", ".join(
        f"{name} (${detail_by_name.get(name, {}).get('current_value_usd', 'UNKNOWN')}; "
        f"{detail_by_name.get(name, {}).get('qualification_gap', 'UNQUALIFIED')}; "
        f"last accepted {detail_by_name.get(name, {}).get('last_accepted_date') or 'unavailable'}; "
        f"{detail_by_name.get(name, {}).get('freshness_reason') or 'no freshness reason'})"
        for name in missing
    )
    excluded = [
        dict(item)
        for item in hwm_summary.get("excluded_details", [])
        if isinstance(item, Mapping)
    ]
    exclusion_text = (
        "explicit policy exclusions: "
        + ", ".join(
            f"{item.get('strategy_name')} (${item.get('current_value_usd')}; {item.get('reason')})"
            for item in excluded
        )
        if excluded
        else (
            f"{len(hwm_summary['excluded_strategies'])} strateg"
            f"{'y is' if len(hwm_summary['excluded_strategies']) == 1 else 'ies are'} "
            "explicitly excluded de minimis by policy"
        )
    )
    for row in normalized:
        if row.get("category") == "FACT_COVERAGE" and row.get("item") == "nav":
            row["known_gap"] = (
                (
                    f"{len(missing)} included HWM-perimeter strateg"
                    f"{'y is' if len(missing) == 1 else 'ies are'} not HWM-qualified"
                    + (f": {gap_details}" if gap_details else "")
                )
                if missing
                else "Every included HWM-perimeter strategy is qualified"
            )
            row["known_gap"] += f"; {exclusion_text}"
            row["disposition"] = (
                "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV" if missing else "COMPLETE"
            )
            row["status"] = (
                "PARTIAL" if missing else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
            )
    hwm_rows = [
        row
        for row in normalized
        if row.get("category") == "RECONCILIATION_EXCEPTION"
        and row.get("item") == "full-systematic-hwm-unavailable"
    ]
    if not missing:
        return [row for row in normalized if row not in hwm_rows]
    gap = (
        "BLOCKING_FOR_AUTOMATED_HWM: included HWM-perimeter strategies are not "
        f"qualified; exact uncovered value ${hwm_summary.get('uncovered_value_usd', 'UNKNOWN')}: "
        + gap_details
    )
    if hwm_rows:
        for row in hwm_rows:
            row["known_gap"] = gap
            row["disposition"] = "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV"
            row["resolution_authority"] = (
                "PORT-HWM-3 policy and admitted-lane daily NAV freshness evidence"
            )
            row["status"] = "UNAVAILABLE"
            row["uncovered_details"] = details
            row["value"] = hwm_summary.get("uncovered_value_usd")
    else:
        normalized.append(
            {
                "category": "RECONCILIATION_EXCEPTION",
                "item": "full-systematic-hwm-unavailable",
                "known_gap": gap,
                "disposition": "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV",
                "resolution_authority": (
                    "PORT-HWM-3 policy and admitted-lane daily NAV freshness evidence"
                ),
                "status": "UNAVAILABLE",
                "uncovered_details": details,
                "value": hwm_summary.get("uncovered_value_usd"),
            }
        )
    return normalized


def build_report_data(
    baseline_root: Path,
    freshness_health_path: Path | None = None,
    provider_ledger_root: Path | None = None,
    attribution_root: Path | None = None,
) -> tuple[dict[str, Any], bytes, Path]:
    """Build canonical report data from the currently accepted baseline."""

    catalog, baseline_ref, baseline_catalog_path = _baseline(baseline_root)
    performance = _account_strategy_performance(catalog)
    strategy_provider_evidence = _strategy_provider_evidence(catalog)
    strategy_summary = _strategies(catalog, performance)
    attribution_catalog: dict[str, Any] | None = None
    attribution_ref: dict[str, str] | None = None
    if attribution_root is not None:
        attribution_catalog, attribution_ref, _ = _attribution(
            attribution_root, baseline_ref
        )
        strategy_summary = _merge_attribution(strategy_summary, attribution_catalog)
    coverage = _coverage(catalog)
    hwm_summary: dict[str, Any] | None = None
    hwm_provenance: list[dict[str, Any]] = []
    if freshness_health_path is not None:
        portfolio_as_of = _date(catalog.get("as_of_date"), "portfolio as_of_date")
        freshness, evaluation_date, freshness_sha256 = _freshness_assessments(
            freshness_health_path, portfolio_as_of
        )
        hwm_policy, hwm_policy_sha256 = _hwm_policy(evaluation_date)
        try:
            strategy_summary, hwm_summary = governed_hwm_coverage(
                strategy_summary,
                hwm_policy,
                freshness,
                require_freshness=True,
            )
        except HwmCoverageError as exc:
            raise ReportValidationError(str(exc)) from exc
        coverage = _reconcile_hwm_coverage(coverage, hwm_summary)
        hwm_provenance = [
            {
                "artifact": "portfolio-hwm-policy",
                "as_of_date": evaluation_date.isoformat(),
                "schema": "portfolio-hwm-policy/2",
                "sha256": hwm_policy_sha256,
            },
            {
                "artifact": "freshness-health",
                "as_of_date": evaluation_date.isoformat(),
                "schema": "freshness-assessment",
                "sha256": freshness_sha256,
            },
        ]
    report: dict[str, Any] = {
        "account_summary": _accounts(catalog),
        "allocation_history": _allocations(catalog),
        "as_of_date": catalog.get("as_of_date"),
        "baseline": baseline_ref,
        "corrections": _corrections(catalog),
        "coverage_data_quality": coverage,
        "fact_coverage_matrix": catalog.get("fact_coverage_matrix", []),
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
                "item": "Account-live return authority",
                "value": "Canonical strategy-return rows govern cumulative return, drawdown, CAGR vintage, and the ordered date/return series hash; legacy audit metrics are used only as one internally consistent fallback when no provider-ledger evidence exists.",
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
            {
                "item": "Strategy attribution",
                "value": "Provider/model comparisons use exact common dates only. Version-unresolved results remain descriptive; benchmark, cash-drag, fee, or after-tax gaps stay unavailable rather than becoming zero.",
            },
        ],
        "portfolio_summary": _portfolio_summary(catalog, hwm_summary),
        "observation_windows": catalog.get("observation_windows", []),
        "positions": _positions(catalog),
        "provider_admissions": catalog.get("provider_admissions", []),
        "provider_coverage": catalog.get("provider_coverage", []),
        "provider_strategy_returns": catalog.get("provider_strategy_returns", []),
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
            *hwm_provenance,
            *(
                [
                    {
                        "artifact": "strategy-attribution",
                        "as_of_date": attribution_catalog.get("as_of_date"),
                        "schema": "strategy-attribution/1",
                        "sha256": attribution_ref["catalog_sha256"],
                    }
                ]
                if attribution_catalog is not None and attribution_ref is not None
                else []
            ),
        ],
        "reconciliation_checks": _reconciliation_checks(
            catalog, baseline_root, baseline_ref, provider_ledger_root
        ),
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "strategy_summary": strategy_summary,
        "attribution_dispositions": catalog.get("attribution_dispositions", []),
        "strategy_provider_evidence": strategy_provider_evidence,
        "strategy_returns": catalog.get("strategy_returns", []),
        "transactions_and_flows": _transactions_and_flows(catalog),
        "twr_mwr": performance,
    }
    if hwm_summary is not None:
        report["hwm_summary"] = hwm_summary
    if attribution_catalog is not None and attribution_ref is not None:
        report["strategy_attribution"] = {
            "benchmark_assignments": attribution_catalog["benchmark_assignments"],
            "comparisons": attribution_catalog["comparisons"],
            "costs": attribution_catalog["costs"],
            "generation_id": attribution_ref["generation_id"],
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
            "comments": "Generated from an accepted configured portfolio-data baseline.",
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


def build_candidate(
    baseline_root: Path,
    output_root: Path,
    freshness_health_path: Path | None = None,
    provider_ledger_root: Path | None = None,
    attribution_root: Path | None = None,
) -> dict[str, Any]:
    report, report_bytes, _ = build_report_data(
        baseline_root, freshness_health_path, provider_ledger_root, attribution_root
    )
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


def _canonical_guid(scope: str, index: int, *, zero_prefix: bool = False) -> bytes:
    digest = hashlib.sha256(f"{scope}:{index}".encode("utf-8")).hexdigest().upper()
    if zero_prefix:
        value = f"00000000-{digest[:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:24]}"
    else:
        value = (
            f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-"
            f"{digest[16:20]}-{digest[20:32]}"
        )
    return value.encode("ascii")


def _canonicalize_ooxml_part(
    name: str, content: bytes, counts: dict[str, int]
) -> bytes:
    if name == "docProps/core.xml":
        created = _CORE_CREATED.search(content)
        if created is None:
            raise ReportValidationError("workbook core created timestamp is missing")

        def modified(match: re.Match[bytes]) -> bytes:
            counts["modified_timestamp"] += 1
            return match.group(1) + created.group(2) + match.group(3)

        return _CORE_MODIFIED.sub(modified, content)
    if name == "xl/workbook.xml":

        def abs_path(match: re.Match[bytes]) -> bytes:
            counts["absolute_path"] += 1
            return match.group(1) + b"."

        def revision_id(match: re.Match[bytes]) -> bytes:
            counts["revision_document_id"] += 1
            return (
                match.group(1)
                + b"8_{"
                + _canonical_guid(name, counts["revision_document_id"])
                + b"}"
            )

        content = _WORKBOOK_ABS_PATH.sub(abs_path, content)
        return _WORKBOOK_REVISION_ID.sub(revision_id, content)
    if name.startswith("xl/charts/chart") and name.endswith(".xml"):

        def unique_id(match: re.Match[bytes]) -> bytes:
            counts["chart_unique_id"] += 1
            return (
                match.group(1)
                + b"{"
                + _canonical_guid(name, counts["chart_unique_id"], zero_prefix=True)
                + b"}"
            )

        return _CHART_UNIQUE_ID.sub(unique_id, content)
    return content


def _volatile_field_counts(name: str, content: bytes) -> dict[str, int]:
    counts = {
        "absolute_path": 0,
        "chart_unique_id": 0,
        "modified_timestamp": 0,
        "revision_document_id": 0,
    }
    if not (
        name == "docProps/core.xml"
        or name == "xl/workbook.xml"
        or (name.startswith("xl/charts/chart") and name.endswith(".xml"))
    ):
        return counts
    root = ET.fromstring(content)
    for item in root.iter():
        local_name = _local_name(item.tag)
        if name == "docProps/core.xml" and local_name == "modified":
            counts["modified_timestamp"] += 1
        elif name == "xl/workbook.xml" and local_name == "absPath":
            counts["absolute_path"] += 1
        elif name == "xl/workbook.xml" and local_name == "revisionPtr":
            counts["revision_document_id"] += 1
        elif name.startswith("xl/charts/chart") and local_name == "uniqueId":
            counts["chart_unique_id"] += 1
    return counts


def canonicalize_workbook(
    path: Path, evidence_path: Path | None = None
) -> dict[str, Any]:
    """Normalize only native-Excel package fields proven volatile across runs."""
    if path.is_symlink() or not path.is_file():
        raise ReportValidationError("Excel workbook is missing or linked")
    counts = {
        "absolute_path": 0,
        "chart_unique_id": 0,
        "modified_timestamp": 0,
        "revision_document_id": 0,
    }
    discovered = {key: 0 for key in counts}
    try:
        with zipfile.ZipFile(path) as source:
            entries = source.infolist()
            names = [item.filename for item in entries]
            if len(names) != len(set(names)):
                raise ReportValidationError("workbook contains duplicate ZIP entries")
            output = io.BytesIO()
            with zipfile.ZipFile(
                output,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
            ) as destination:
                for item in entries:
                    source_content = source.read(item.filename)
                    for key, value in _volatile_field_counts(
                        item.filename, source_content
                    ).items():
                        discovered[key] += value
                    content = _canonicalize_ooxml_part(
                        item.filename, source_content, counts
                    )
                    canonical = zipfile.ZipInfo(item.filename, _CANONICAL_ZIP_TIMESTAMP)
                    canonical.compress_type = zipfile.ZIP_DEFLATED
                    canonical.create_system = 0
                    canonical.external_attr = 0
                    destination.writestr(canonical, content, compresslevel=9)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        raise ReportValidationError("workbook cannot be canonicalized") from exc
    if discovered["modified_timestamp"] != 1:
        raise ReportValidationError("workbook core modified timestamp count differs")
    if counts != discovered:
        raise ReportValidationError(
            "workbook volatile-field canonicalization is incomplete: "
            f"discovered={discovered!r}, normalized={counts!r}"
        )
    canonical_bytes = output.getvalue()
    if path.read_bytes() != canonical_bytes:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".canonical-", suffix=".xlsx", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    result = {
        "canonicalization_version": WORKBOOK_CANONICALIZATION_VERSION,
        "normalized_fields": counts,
        "workbook_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        "zip_entry_count": len(entries),
    }
    if evidence_path is not None:
        _write_exact(evidence_path, canonical_json_bytes(result))
    return result


def canonicalize_xlsx_result(
    path: Path, evidence_path: Path | None = None
) -> dict[str, Any]:
    """Normalize random xlsx-win run telemetry while retaining every outcome."""
    payload = _load_json(path, "xlsx-win result")
    if payload.get("schema_version") != "2.0":
        raise ReportValidationError("xlsx-win result schema differs")
    idempotency_key = _text(
        payload.get("idempotency_key"), "xlsx-win result idempotency_key"
    )
    run_id = _text(payload.get("run_id"), "xlsx-win result run_id")
    if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise ReportValidationError("xlsx-win result run_id is invalid")
    steps = payload.get("steps")
    if not isinstance(steps, list) or len(steps) != 3:
        raise ReportValidationError("xlsx-win result steps differ")
    expected = (
        (0, "open", "Opened canonical staged workbook."),
        (1, "recalc", "Calculation reached xlDone."),
        (2, "save_as", "Saved canonical staged workbook."),
    )
    for step, (index, step_type, message) in zip(steps, expected, strict=True):
        if not isinstance(step, dict):
            raise ReportValidationError("xlsx-win result step is invalid")
        if (
            step.get("step_index") != index
            or step.get("type") != step_type
            or step.get("status") != "succeeded"
        ):
            raise ReportValidationError("xlsx-win result step outcome differs")
        step["message"] = message
    if payload.get("final_state") != "SUCCEEDED" or payload.get("ok") is not True:
        raise ReportValidationError("xlsx-win result is not successful")
    invariants = payload.get("invariants")
    if not isinstance(invariants, list):
        raise ReportValidationError("xlsx-win result invariants are invalid")
    payload["run_id"] = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    content = canonical_json_bytes(payload)
    if path.read_bytes() != content:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".canonical-xlsx-result-", suffix=".json", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    result = {
        "canonicalization_version": XLSX_RESULT_CANONICALIZATION_VERSION,
        "normalized_message_count": 2,
        "normalized_run_id": True,
        "xlsx_result_sha256": hashlib.sha256(content).hexdigest(),
    }
    if evidence_path is not None:
        _write_exact(evidence_path, canonical_json_bytes(result))
    return result


def _validate_canonicalization_evidence(candidate_dir: Path, workbook: Path) -> None:
    evidence = _load_json(
        candidate_dir / "workbook-canonicalization.json",
        "workbook canonicalization evidence",
    )
    if evidence.get("canonicalization_version") != WORKBOOK_CANONICALIZATION_VERSION:
        raise ReportValidationError("workbook canonicalization version differs")
    sha256, _ = hash_file(workbook)
    if evidence.get("workbook_sha256") != sha256:
        raise ReportValidationError("workbook canonicalization hash differs")
    fields = evidence.get("normalized_fields")
    if not isinstance(fields, dict) or fields.get("modified_timestamp") != 1:
        raise ReportValidationError("workbook canonicalization evidence is incomplete")


def verify_candidate(
    baseline_root: Path,
    candidate_dir: Path,
    excel_workbook: Path,
    freshness_health_path: Path | None = None,
    provider_ledger_root: Path | None = None,
    attribution_root: Path | None = None,
) -> dict[str, Any]:
    report, report_bytes, _ = build_report_data(
        baseline_root, freshness_health_path, provider_ledger_root, attribution_root
    )
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
    freshness_health_path: Path | None = None,
    provider_ledger_root: Path | None = None,
    attribution_root: Path | None = None,
) -> dict[str, Any]:
    _validate_canonicalization_evidence(candidate_dir, excel_workbook)
    verification = verify_candidate(
        baseline_root,
        candidate_dir,
        excel_workbook,
        freshness_health_path,
        provider_ledger_root,
        attribution_root,
    )
    run = _load_json(xlsx_result, "xlsx-win run result")
    validation = _load_json(validation_result, "xlsx-win validation result")
    if run.get("ok") is not True:
        raise ReportValidationError("xlsx-win top-level result is not ok")
    if validation.get("all_passed") is not True:
        raise ReportValidationError("xlsx-win workbook invariants did not all pass")
    report, report_bytes, baseline_catalog_path = build_report_data(
        baseline_root, freshness_health_path, provider_ledger_root, attribution_root
    )
    as_of = _date(report.get("as_of_date"), "report.as_of_date")
    verification_bytes = canonical_json_bytes(verification)
    contract_path = candidate_dir / "workbook-contract.json"
    artifacts = []
    artifact_specs = [
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
            candidate_dir / "workbook-canonicalization.json",
            "evidence/workbook-canonicalization.json",
            "portfolio-report-workbook-canonicalization",
            WORKBOOK_CANONICALIZATION_VERSION,
        ),
        (
            candidate_dir / "xlsx-result-canonicalization.json",
            "evidence/xlsx-result-canonicalization.json",
            "xlsx-win-result-canonicalization",
            XLSX_RESULT_CANONICALIZATION_VERSION,
        ),
        (
            contract_path,
            "evidence/workbook-contract.json",
            "xlsx-win-validation-contract",
            "1",
        ),
    ]
    if attribution_root is not None:
        _attribution_catalog, _attribution_ref, attribution_catalog_path = _attribution(
            attribution_root, report["baseline"]
        )
        artifact_specs.append(
            (
                attribution_catalog_path,
                "input/strategy-attribution.json",
                "strategy-attribution",
                "1",
            )
        )
    if freshness_health_path is not None:
        artifact_specs.extend(
            [
                (
                    freshness_health_path,
                    "input/freshness-health.json",
                    "freshness-assessment",
                    "3",
                ),
                (
                    HWM_POLICY,
                    "input/portfolio-hwm-policy.json",
                    "portfolio-hwm-policy",
                    "2",
                ),
            ]
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
    _copy_exact(
        candidate_dir / "workbook-canonicalization.json",
        generation_dir / "workbook-canonicalization.json",
    )
    _copy_exact(
        candidate_dir / "xlsx-result-canonicalization.json",
        generation_dir / "xlsx-result-canonicalization.json",
    )
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
    build.add_argument("--freshness-health", type=Path)
    build.add_argument("--provider-ledger-root", type=Path)
    build.add_argument("--attribution-root", type=Path)
    build.add_argument("--output-root", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--baseline-root", type=Path, required=True)
    verify.add_argument("--candidate-dir", type=Path, required=True)
    verify.add_argument("--excel-workbook", type=Path, required=True)
    verify.add_argument("--freshness-health", type=Path)
    verify.add_argument("--provider-ledger-root", type=Path)
    verify.add_argument("--attribution-root", type=Path)
    canonicalize = commands.add_parser("canonicalize")
    canonicalize.add_argument("--workbook", type=Path, required=True)
    canonicalize.add_argument("--evidence", type=Path, required=True)
    canonicalize_result = commands.add_parser("canonicalize-xlsx-result")
    canonicalize_result.add_argument("--result", type=Path, required=True)
    canonicalize_result.add_argument("--evidence", type=Path, required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--baseline-root", type=Path, required=True)
    publish.add_argument("--candidate-dir", type=Path, required=True)
    publish.add_argument("--excel-workbook", type=Path, required=True)
    publish.add_argument("--xlsx-result", type=Path, required=True)
    publish.add_argument("--validation-result", type=Path, required=True)
    publish.add_argument("--output-root", type=Path, required=True)
    publish.add_argument("--freshness-health", type=Path)
    publish.add_argument("--provider-ledger-root", type=Path)
    publish.add_argument("--attribution-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_candidate(
                args.baseline_root,
                args.output_root,
                args.freshness_health,
                args.provider_ledger_root,
                args.attribution_root,
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.baseline_root,
                args.candidate_dir,
                args.excel_workbook,
                args.freshness_health,
                args.provider_ledger_root,
                args.attribution_root,
            )
        elif args.command == "canonicalize":
            result = canonicalize_workbook(args.workbook, args.evidence)
        elif args.command == "canonicalize-xlsx-result":
            result = canonicalize_xlsx_result(args.result, args.evidence)
        else:
            result = publish_report(
                args.baseline_root,
                args.candidate_dir,
                args.excel_workbook,
                args.xlsx_result,
                args.validation_result,
                args.output_root,
                args.freshness_health,
                args.provider_ledger_root,
                args.attribution_root,
            )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"portfolio-report: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
