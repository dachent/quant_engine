"""Build, verify, and publish the private coverage-aware portfolio report."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time as time_module
from typing import Any, Final, Mapping, Sequence, cast
from xml.etree import ElementTree as ET
import zipfile

from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils.cell import range_boundaries  # type: ignore[import-untyped]
import xlsxwriter  # type: ignore[import-untyped]
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
    ValidationError as JsonSchemaValidationError,
)

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    generation_inventory_sha256,
    hash_file,
    publish_generation,
    read_current,
    read_generation,
    read_generation_bundle,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import (
    HwmCoverageError,
    HwmInputsContractError,
    compute_full_systematic_hwm_index,
    decimal_text,
    governed_hwm_coverage,
    hwm_policy_exclusions,
    validate_hwm_inputs_envelope,
    validate_as_held_hwm_receipt_generation_binding,
)
from quant_engine.portfolio_ledger.hwm_governed_execution import (
    verify_governed_hwm_result,
)
from portfolio_private_stage import validate_private_stage_target
from portfolio_invested_capital import (
    CapitalValidationError,
    governed_hwm_input_policy,
    resolve_financial_input_authority,
    verify_governed_capital_result,
)


REPORT_SCHEMA_VERSION: Final = 1
_CURRENT_READ_RETRY_DELAYS: Final = (0.25, 0.5, 1.0, 2.0)
BUILDER_VERSION: Final = "1.5.0"
POLICY_VERSION: Final = "PORT-PERF-1"
WORKBOOK_NAME: Final = "portfolio-report.xlsx"
REPORT_DATA_NAME: Final = "portfolio-report-data.json"
INVESTED_CAPITAL_RESULT_NAME: Final = "invested-capital.json"
INVESTED_CAPITAL_INPUT_NAME: Final = "capital-input-generation.json"
REPORT_DELIVERY_NAME: Final = "report-delivery.json"
REPORT_DELIVERY_SCHEMA_VERSION: Final = 1
NON_AUTHORITY_HUMAN_DELIVERY: Final = "NON_AUTHORITY_HUMAN_DELIVERY"
CURRENT_REQUIRED_STRATEGY_IDS: Final = frozenset(
    {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    }
)
CURRENT_EXCLUDED_STRATEGY_IDS: Final = frozenset(
    {"echotrade-growth-pivots", "composer-holy-grail"}
)
CURRENT_STRATEGY_CONTROL_IDS: Final = (
    CURRENT_REQUIRED_STRATEGY_IDS | CURRENT_EXCLUDED_STRATEGY_IDS
)
CURRENT_PRODUCT_EPOCH_COUNT: Final = 13
CURRENT_EPOCH_CROSSWALK_AS_OF_DATE: Final = "2026-08-16"
CURRENT_EPOCH_CROSSWALK_CUTOFF_DATE: Final = "2026-08-13"
EPOCH_VERIFICATION_SCHEMA: Final = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "account-manifest-independent-verification.schema.json"
)
EPOCH_MANIFEST_SCHEMA: Final = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "account-strategy-manifest.schema.json"
)
EPOCH_RETRIEVAL_RECEIPT_SCHEMA: Final = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "account-interval-retrieval-receipt.schema.json"
)
HWM_POLICY: Final = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v3.json"
)
_PORTABLE_HWM_POLICY: Final = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v4.json"
)
_PORTABLE_HWM_PRODUCT_CONTRACT: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-financial-mvp-source-v1.json"
)
DEPENDENCE_POLICY: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-dependence-policy-v1.json"
)
_HWM_POLICY_PRODUCT_AUTHORITY_CONTRACT: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-financial-mvp-source-v1.json"
)
_PORTABLE_HWM_PERFORMANCE_POLICY: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-performance-policy-v1.json"
)
_P123_ENTITY_REF_RE: Final = re.compile(r"\bp123-entity:\d+\b", re.IGNORECASE)
_P123_ENTITY_TEXT_RE: Final = re.compile(r"\bPortfolio123 entity \d+\b", re.IGNORECASE)
_PROVIDER_ENTITY_TEXT_RE: Final = re.compile(r"\bentity \d{5,}\b", re.IGNORECASE)
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
    "Dependence",
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
    "tblDependence",
    "tblMethodology",
    "tblProvenance",
)
CAPITAL_SHEET_NAMES: Final = ("Invested Capital Daily", "Invested Capital Current")
CAPITAL_TABLE_NAMES: Final = ("tblInvestedCapitalDaily", "tblInvestedCapitalCurrent")
DECOMPOSITION_SHEET_NAMES: Final = ("Decomposition Cells", "Decomposition Factors")
DECOMPOSITION_TABLE_NAMES: Final = ("tblDecompositionCells", "tblDecompositionFactors")
CAPITAL_RECONCILIATION_FORMULA: Final = (
    "=IF(ABS(SUMIFS(tblInvestedCapitalDaily[Invested Capital Usd],"
    "tblInvestedCapitalDaily[Date],INDEX(tblInvestedCapitalDaily[Date],"
    "ROWS(tblInvestedCapitalDaily[Date])),tblInvestedCapitalDaily[Strategy Id],"
    '"TOTAL")-SUMIFS(tblInvestedCapitalCurrent[Invested Capital Usd],'
    'tblInvestedCapitalCurrent[Strategy Id],"TOTAL"))<0.01,"PASS","FAIL")'
)
WORKBOOK_CANONICALIZATION_VERSION: Final = "1"
XLSX_RESULT_CANONICALIZATION_VERSION: Final = "2"
WORKBOOK_EVIDENCE_BINDING_VERSION: Final = 1
WORKBOOK_PARITY_VERSION: Final = 1
WORKBOOK_DELIVERY_EVIDENCE_STATUS: Final = "WORKBOOK_DELIVERY_EVIDENCE_ACCEPTED"
WORKBOOK_NON_AUTHORITY_DELIVERY: Final = "ELIGIBLE_FOR_NON_AUTHORITY_DELIVERY"
WORKBOOK_DELIVERY_EVIDENCE_VERSION: Final = 1
EPOCH_MANIFEST_WORKBOOK_NAME: Final = "account-strategy-manifest.json"
EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME: Final = "epoch-manifest-source.json"
EPOCH_CROSSWALK_WORKBOOK_NAME: Final = "account-strategy-current-epoch-crosswalk.json"
FLOW_CLASSIFICATION_POLICY_CONTRACT: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "account-strategy-flow-classification-policy-v1.json"
)
PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH: Final = (
    "input/account-strategy-current-epoch-crosswalk.json"
)
WORKBOOK_TOTAL_FORMULA: Final = (
    "=IF(ABS(SUM(tblStrategies[Current Value Usd])+"
    'SUMIFS(tblAllocations[Current Value Usd],tblAllocations[Scope],"RESERVE")'
    "+SUMIFS(tblPortfolioSummary[Value],tblPortfolioSummary[Metric],"
    '"Retained excluded systematic balance")'
    "-SUMIFS(tblPortfolioSummary[Value],tblPortfolioSummary[Metric],"
    '"Total liquid"))<0.02,"PASS","FAIL")'
)
WORKBOOK_TABLE_SPECS: Final[tuple[tuple[str, str, str, tuple[str, ...]], ...]] = (
    (
        "Portfolio Summary",
        "portfolio_summary",
        "tblPortfolioSummary",
        ("metric", "status", "unit", "value"),
    ),
    ("Account Summary", "account_summary", "tblAccounts", ("account_id",)),
    (
        "Strategy Summary",
        "strategy_summary",
        "tblStrategies",
        ("strategy_id",),
    ),
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
    (
        "Flow Reconciliation",
        "flow_reconciliation",
        "tblFlowRecon",
        ("provider",),
    ),
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
    (
        "Dependence",
        "dependence_rows",
        "tblDependence",
        ("evidence_mode", "left", "right", "status"),
    ),
    ("Methodology", "methodology", "tblMethodology", ("item", "value")),
    ("Provenance", "provenance", "tblProvenance", ("artifact", "sha256")),
)
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


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReportValidationError(f"{field_name} must be a finite number")
    accepted = float(value)
    if not math.isfinite(accepted):
        raise ReportValidationError(f"{field_name} must be a finite number")
    return accepted


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReportValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReportValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _public_report_value(value: object) -> object:
    """Remove raw Portfolio123 entity identifiers from operator-facing output."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if key == "entity_id" and item is not None
                else _public_report_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_report_value(item) for item in value]
    if isinstance(value, str):
        sanitized = _P123_ENTITY_TEXT_RE.sub("Portfolio123 connected entity", value)
        sanitized = _PROVIDER_ENTITY_TEXT_RE.sub("connected entity", sanitized)
        return _P123_ENTITY_REF_RE.sub("p123-entity:[redacted]", sanitized)
    return value


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


def _baseline(
    root: Path, generation_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    if generation_id is None:
        for attempt in range(len(_CURRENT_READ_RETRY_DELAYS) + 1):
            try:
                pointer = read_current(root)
                generation_id = pointer.generation_id
                break
            except OSError as exc:
                if attempt == len(_CURRENT_READ_RETRY_DELAYS):
                    raise ReportValidationError(
                        "baseline pointer is not accepted"
                    ) from exc
                time_module.sleep(_CURRENT_READ_RETRY_DELAYS[attempt])
            except (ValueError, RuntimeError) as exc:
                raise ReportValidationError("baseline pointer is not accepted") from exc
    if (
        not isinstance(generation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", generation_id) is None
    ):
        raise ReportValidationError("baseline generation identity differs")
    try:
        manifest = read_generation(root, generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError("baseline generation is not accepted") from exc
    manifest_path = root / "generations" / generation_id / "generation.json"
    if (
        manifest.generation_id != generation_id
        or manifest.verification is not VerificationStatus.VERIFIED
        or _regular_file_bytes(manifest_path, "baseline generation manifest")
        != manifest.to_json_bytes()
    ):
        raise ReportValidationError("baseline generation contract differs")
    catalog_path = root / "generations" / generation_id / "portfolio-baseline.json"
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ReportValidationError("baseline catalog is missing or linked")
    catalog = _load_json(catalog_path, "baseline catalog")
    if catalog.get("baseline_schema_version") != 1:
        raise ReportValidationError("baseline schema version is unsupported")
    if catalog.get("policy_version") != POLICY_VERSION:
        raise ReportValidationError("baseline performance policy differs")
    sha256, size_bytes = hash_file(catalog_path)
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/portfolio-baseline.json"
        ),
        None,
    )
    if artifact is None or (
        artifact.sha256,
        artifact.size_bytes,
        artifact.schema_name,
        artifact.schema_version,
    ) != (
        sha256,
        size_bytes,
        "portfolio-baseline-catalog",
        "1",
    ):
        raise ReportValidationError("baseline artifact identity differs")
    return (
        catalog,
        {"catalog_sha256": sha256, "generation_id": generation_id},
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
    if catalog.get("catalog_schema_version") not in {1, 2}:
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


def _dependence_report(
    root: Path | None,
    *,
    generation_dir: Path | None = None,
    calculation_cutoff_date: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str], Path]:
    try:
        if generation_dir is None:
            if root is None:
                raise ValueError("dependence root is required")
            pointer = read_current(root)
            generation_id = pointer.generation_id
            generation_root = root / "generations" / generation_id
        else:
            if generation_dir.is_symlink() or not generation_dir.is_dir():
                raise ValueError("explicit dependence generation is not a directory")
            generation_id = generation_dir.name
            if re.fullmatch(r"[0-9a-f]{64}", generation_id) is None:
                raise ValueError("explicit dependence generation identity differs")
            inferred_root = generation_dir.parent.parent
            if generation_dir.parent.name != "generations":
                raise ValueError("explicit dependence generation layout differs")
            if (
                root is not None
                and generation_dir.resolve()
                != (root / "generations" / generation_id).resolve()
            ):
                raise ValueError("explicit dependence generation root differs")
            root = inferred_root if root is None else root
            generation_root = generation_dir
        manifest = read_generation(root, generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError("dependence generation is not accepted") from exc
    if (
        manifest.generation_id != generation_id
        or manifest.verification is not VerificationStatus.VERIFIED
        or manifest.adapter.name != "portfolio-correlation"
        or manifest.adapter.version != "1.6.0"
    ):
        raise ReportValidationError("dependence generation contract differs")
    correlation_path = generation_root / "correlation.json"
    manifest_path = generation_root / "generation.json"
    if (
        _regular_file_bytes(manifest_path, "dependence generation manifest")
        != manifest.to_json_bytes()
    ):
        raise ReportValidationError("dependence generation manifest differs")
    if correlation_path.is_symlink() or not correlation_path.is_file():
        raise ReportValidationError("dependence artifact is missing or linked")
    content = correlation_path.read_bytes()
    correlation_sha256 = hashlib.sha256(content).hexdigest()
    artifacts = {item.artifact_id: item for item in manifest.artifacts}
    artifact = artifacts.get("normalized/correlation.json")
    if artifact is None or (
        artifact.sha256,
        artifact.size_bytes,
        artifact.schema_name,
        artifact.schema_version,
    ) != (
        correlation_sha256,
        len(content),
        "portfolio-correlation-analysis",
        "2",
    ):
        raise ReportValidationError("dependence artifact identity differs")
    correlation = _load_json(correlation_path, "dependence correlation")
    if (
        correlation.get("analytics_schema_version") != 2
        or correlation.get("default_evidence_mode") != "LIVE_ONLY"
    ):
        raise ReportValidationError("dependence analytics contract differs")
    cutoff = (
        None
        if calculation_cutoff_date is None
        else _date(calculation_cutoff_date, "calculation cutoff date").isoformat()
    )
    if cutoff is not None:
        if (
            correlation.get("complete_through") != cutoff
            or manifest.coverage.observed_end is None
            or manifest.coverage.observed_end.isoformat() != cutoff
        ):
            raise ReportValidationError("dependence calculation cutoff differs")
    modes = _object(correlation.get("dependence_modes"), "dependence modes")
    if set(modes) != {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
        raise ReportValidationError("dependence evidence mode set differs")
    if modes["LIVE_ONLY"] != correlation.get("dependence"):
        raise ReportValidationError("dependence default mode differs")
    source = _object(correlation.get("source"), "dependence source")
    if set(source) != {"artifact_id", "sha256", "size_bytes"}:
        raise ReportValidationError("dependence source identity differs")
    source_artifact_id = _text(
        source.get("artifact_id"), "dependence source artifact_id"
    )
    source_sha256 = _text(source.get("sha256"), "dependence source sha256")
    source_size = source.get("size_bytes")
    if len(source_sha256) != 64 or not isinstance(source_size, int) or source_size <= 0:
        raise ReportValidationError("dependence source identity is invalid")
    source_identity = {
        "artifact_id": source_artifact_id,
        "sha256": source_sha256,
        "size_bytes": source_size,
    }
    series_ids: list[str] | None = None
    rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    expected_pairs: set[frozenset[str]] | None = None
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        payload = _object(modes[mode], f"dependence mode {mode}")
        raw_series = payload.get("series_ids")
        if not isinstance(raw_series, list):
            raise ReportValidationError("dependence series boundary is invalid")
        current_series = [_text(item, "dependence series_id") for item in raw_series]
        if len(current_series) != 7 or len(set(current_series)) != 7:
            raise ReportValidationError("dependence series boundary differs")
        if series_ids is None:
            series_ids = current_series
        elif current_series != series_ids:
            raise ReportValidationError("dependence mode series order differs")
        if cutoff is not None:
            for field_name in ("series_composition", "performance"):
                series_payload = _object(
                    payload.get(field_name), f"dependence mode {mode} {field_name}"
                )
                if set(series_payload) != set(current_series) or any(
                    not isinstance(series_payload.get(series_id), Mapping)
                    or series_payload[series_id].get("observed_end") != cutoff
                    for series_id in current_series
                ):
                    raise ReportValidationError("dependence mode/series cutoff differs")
        observed = _objects(payload.get("observed_cells"), "dependence observed cells")
        blends = _objects(payload.get("blend_cells"), "dependence blend cells")
        implementation = _objects(
            payload.get("implementation_cells"), "dependence implementation cells"
        )

        def _index(
            values: Sequence[Mapping[str, Any]], label: str
        ) -> dict[frozenset[str], Mapping[str, Any]]:
            result: dict[frozenset[str], Mapping[str, Any]] = {}
            for value in values:
                key = frozenset(
                    (
                        _text(value.get("left"), f"{label} left"),
                        _text(value.get("right"), f"{label} right"),
                    )
                )
                if len(key) != 2 or key in result:
                    raise ReportValidationError(
                        f"dependence {label} pair boundary differs"
                    )
                result[key] = value
            return result

        observed_by_pair = _index(observed, "observed")
        blend_by_pair = _index(blends, "blend")
        implementation_by_pair = _index(implementation, "implementation")
        if (
            not (
                set(observed_by_pair)
                == set(blend_by_pair)
                == set(implementation_by_pair)
            )
            or len(observed_by_pair) != 21
        ):
            raise ReportValidationError("dependence pair-mode coverage differs")
        if expected_pairs is None:
            expected_pairs = set(observed_by_pair)
        elif set(observed_by_pair) != expected_pairs:
            raise ReportValidationError("dependence mode pair sets differ")
        for key, observed_row in observed_by_pair.items():
            blend = blend_by_pair[key]
            implementation_row = implementation_by_pair[key]
            coverage = _object(observed_row.get("coverage"), "dependence coverage")
            rows.append(
                {
                    "evidence_mode": mode,
                    "left": observed_row["left"],
                    "right": observed_row["right"],
                    "status": observed_row["status"],
                    "observations": coverage.get("common_observations"),
                    "observed_start": coverage.get("observed_start"),
                    "observed_end": coverage.get("observed_end"),
                    "pearson": observed_row.get("pearson"),
                    "spearman": observed_row.get("spearman"),
                    "decision_correlation": blend.get("decision_correlation"),
                    "decision_status": blend.get("decision_status"),
                    "implementation_status": implementation_row.get("status"),
                    "mapping_scope": implementation_row.get("mapping_scope"),
                    "shared_providers": ", ".join(
                        str(item)
                        for item in implementation_row.get("shared_providers", [])
                    ),
                    "unassessed_dimensions": ", ".join(
                        str(item)
                        for item in implementation_row.get("unassessed_dimensions", [])
                    ),
                    "horizons_json": canonical_json_bytes(
                        observed_row.get("horizons", {})
                    )
                    .decode("utf-8")
                    .strip(),
                    "rolling_json": canonical_json_bytes(
                        observed_row.get("rolling_pearson", {})
                    )
                    .decode("utf-8")
                    .strip(),
                    "downside_json": canonical_json_bytes(
                        observed_row.get("downside", {})
                    )
                    .decode("utf-8")
                    .strip(),
                    "drawdown_regimes_json": canonical_json_bytes(
                        observed_row.get("drawdown_regimes", {})
                    )
                    .decode("utf-8")
                    .strip(),
                    "joint_tail_json": canonical_json_bytes(
                        observed_row.get("joint_tail", {})
                    )
                    .decode("utf-8")
                    .strip(),
                }
            )
        summaries[mode] = {
            "central_case": payload.get("central_case"),
            "correlation_one": payload.get("correlation_one"),
            "drawdown_matrices": payload.get("drawdown_matrices"),
            "horizon_matrices": payload.get("horizon_matrices"),
            "matrices": payload.get("matrices"),
            "performance": payload.get("performance"),
            "series_composition": payload.get("series_composition"),
        }
    assert series_ids is not None
    rows.sort(
        key=lambda row: (
            ("LIVE_ONLY", "LIVE_PLUS_BACKTEST").index(str(row["evidence_mode"])),
            series_ids.index(str(row["left"])),
            series_ids.index(str(row["right"])),
        )
    )
    manifest_sha256, _ = hash_file(manifest_path)
    identity = {
        "correlation_sha256": correlation_sha256,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest_sha256,
    }
    projection = {
        "analytics_hash": _text(
            correlation.get("analytics_hash"), "dependence analytics_hash"
        ),
        "as_of_date": _date(
            correlation.get("as_of_date"), "dependence as_of_date"
        ).isoformat(),
        "default_evidence_mode": "LIVE_ONLY",
        "identity": identity,
        "mode_count": 2,
        "mode_payload_sha256": hashlib.sha256(canonical_json_bytes(modes)).hexdigest(),
        "mode_summaries": summaries,
        "pair_count": 21,
        "pair_mode_count": 42,
        "schema_version": 1,
        "series_ids": series_ids,
        "source": source_identity,
        "status": "AVAILABLE",
    }
    if cutoff is not None:
        projection["complete_through"] = cutoff
    return projection, rows, identity, correlation_path


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
    raw_benchmark_comparisons = attribution.get("benchmark_comparisons", [])
    benchmark_comparisons = {
        str(item.get("strategy_id")): item
        for item in _objects(
            raw_benchmark_comparisons,
            "attribution.benchmark_comparisons",
        )
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
        or (benchmark_comparisons and set(benchmark_comparisons) != strategy_ids)
    ):
        raise ReportValidationError("strategy attribution boundary differs")
    result = []
    for item in strategies:
        strategy_id = str(item["strategy_id"])
        benchmark = benchmarks[strategy_id]
        comparison = comparisons[strategy_id]
        implementation = item.get("implementation_reconciliation")
        if isinstance(implementation, Mapping):
            implementation_metrics = implementation.get("return_reconciliation")
            if (
                isinstance(implementation_metrics, Mapping)
                and implementation_metrics.get("status")
                == "UNAVAILABLE_POLICY_NOT_ADMITTED"
            ):
                _validate_policy_unavailable_comparison(
                    implementation_metrics, strategy_id=strategy_id
                )
                comparison = {
                    **comparison,
                    "status": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                    "disposition": "Current selected source policy excludes provider returns; actual account evidence is retained separately.",
                    "version_compatibility_status": None,
                    "metrics": None,
                }
        benchmark_comparison = benchmark_comparisons.get(strategy_id, {})
        cost = costs[strategy_id]
        metrics_value = comparison.get("metrics")
        metrics = (
            {}
            if metrics_value is None
            else _object(metrics_value, "attribution.comparison.metrics")
        )
        benchmark_metrics_value = benchmark_comparison.get("metrics")
        benchmark_metrics = (
            {}
            if benchmark_metrics_value is None
            else _object(
                benchmark_metrics_value,
                "attribution.benchmark_comparison.metrics",
            )
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
                "benchmark_comparison_status": benchmark_comparison.get(
                    "status", "UNAVAILABLE"
                ),
                "benchmark_comparison_disposition": benchmark_comparison.get(
                    "disposition",
                    "No exact-date benchmark-relative comparison is admitted.",
                ),
                "benchmark_comparison_observations": benchmark_metrics.get(
                    "observations"
                ),
                "benchmark_comparison_observed_start": benchmark_metrics.get(
                    "observed_start"
                ),
                "benchmark_comparison_observed_end": benchmark_metrics.get(
                    "observed_end"
                ),
                "benchmark_account_cumulative_return": benchmark_metrics.get(
                    "account_cumulative_return"
                ),
                "benchmark_reference_cumulative_return": benchmark_metrics.get(
                    "reference_cumulative_return"
                ),
                "benchmark_cumulative_gap": benchmark_metrics.get("cumulative_gap"),
                "benchmark_active_return_annualized": benchmark_metrics.get(
                    "active_return_annualized"
                ),
                "benchmark_tracking_error_annualized": benchmark_metrics.get(
                    "tracking_error_annualized"
                ),
                "benchmark_information_ratio": benchmark_metrics.get(
                    "information_ratio"
                ),
                "benchmark_beta": benchmark_metrics.get("beta"),
                "benchmark_correlation": benchmark_metrics.get("correlation"),
                "benchmark_upside_capture": benchmark_metrics.get("upside_capture"),
                "benchmark_downside_capture": benchmark_metrics.get("downside_capture"),
                "benchmark_relative_drawdown": benchmark_metrics.get(
                    "relative_drawdown"
                ),
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


def _account_strategy_performance(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
    current_epoch_tuples: set[tuple[str, str, str, str | None]] | None = None,
    current_epoch_details: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]]
    | None = None,
    *,
    calculation_cutoff_date: str | None = None,
) -> list[dict[str, Any]]:
    cutoff = (
        None
        if calculation_cutoff_date is None
        else _date(calculation_cutoff_date, "calculation cutoff date")
    )
    admitted_epoch_keys = {
        key
        for key, detail in (current_epoch_details or {}).items()
        if detail.get("epoch_admission_status") == "FUNDED"
    }
    admitted_account_ids = {key[0] for key in admitted_epoch_keys}
    admitted_strategy_ids = {key[1] for key in admitted_epoch_keys}
    admission_governed = current_epoch_details is not None

    nav_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in _objects(catalog.get("nav_observations"), "nav_observations"):
        if admission_governed and record.get("account_id") not in admitted_account_ids:
            continue
        if cutoff is not None and _date(record.get("as_of_date"), "nav.date") > cutoff:
            continue
        nav_by_account[_text(record.get("account_id"), "nav.account_id")].append(record)
    flows_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in _objects(catalog.get("flows"), "flows"):
        if admission_governed and record.get("account_id") not in admitted_account_ids:
            continue
        if cutoff is not None and _date(record.get("event_date"), "flow.date") > cutoff:
            continue
        flows_by_account[_text(record.get("account_id"), "flow.account_id")].append(
            record
        )
    confirmed_missing: dict[str, set[date]] = defaultdict(set)
    for record in _objects(catalog.get("calendar_coverage"), "calendar_coverage"):
        if admission_governed and record.get("account_id") not in admitted_account_ids:
            continue
        if cutoff is not None and _date(record.get("date"), "calendar.date") > cutoff:
            continue
        if record.get("status") == "MISSING_ON_PROVIDER_OBSERVED_DATE":
            confirmed_missing[
                _text(record.get("account_id"), "calendar.account_id")
            ].add(_date(record.get("date"), "calendar.date"))

    rows: list[dict[str, Any]] = []
    assignments = _objects(catalog.get("assignments"), "assignments")
    current_assignments_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in assignments:
        if (
            assignment.get("mapping_status") == "MAPPED"
            and assignment.get("strategy_id") is not None
            and assignment.get("effective_end") is None
            and _is_current_product_strategy(
                _text(assignment.get("strategy_id"), "assignment.strategy_id"),
                strategy_product_controls,
            )
            and (
                current_epoch_tuples is None
                or (
                    assignment.get("account_id"),
                    assignment.get("strategy_id"),
                    assignment.get("effective_start"),
                    assignment.get("effective_end"),
                )
                in current_epoch_tuples
            )
            and (
                not admission_governed
                or (
                    assignment.get("account_id"),
                    assignment.get("strategy_id"),
                    assignment.get("effective_start"),
                    assignment.get("effective_end"),
                )
                in admitted_epoch_keys
            )
        ):
            current_assignments_by_strategy[
                _text(assignment.get("strategy_id"), "assignment.strategy_id")
            ].append(assignment)
    account_live_paths: dict[tuple[str, str], dict[str, Any]] = {}
    return_catalog = catalog
    if admission_governed:
        return_catalog = {
            **catalog,
            "strategy_returns": [
                row
                for row in _objects(
                    catalog.get("strategy_returns", []), "strategy_returns"
                )
                if row.get("strategy_id") in admitted_strategy_ids
            ],
        }
    canonical_live = _account_live_return_series(
        return_catalog,
        strategy_product_controls,
        calculation_cutoff_date=calculation_cutoff_date,
    )
    for evidence in _objects(
        catalog.get("strategy_provider_evidence", []),
        "strategy_provider_evidence",
    ):
        if (
            admission_governed
            and evidence.get("strategy_id") not in admitted_strategy_ids
        ):
            continue
        if evidence.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy_id = _text(evidence.get("strategy_id"), "evidence.strategy_id")
        canonical = canonical_live.get(strategy_id)
        if canonical is None:
            continue
        account = _object(evidence.get("account"), "evidence.account")
        account_scopes = _objects(account.get("account_scopes", []), "account_scopes")
        scoped_accounts: list[tuple[str, str, str | None]] = [
            (
                _text(scope.get("account_id"), "account_scope.account_id"),
                "EVIDENCE_ACCOUNT_SCOPE",
                None,
            )
            for scope in account_scopes
        ]
        if not scoped_accounts:
            current_assignments = current_assignments_by_strategy.get(strategy_id, [])
            if len(current_assignments) == 1:
                assignment = current_assignments[0]
                assignment_start = assignment.get("effective_start")
                observed_start = account.get("observed_start")
                if (
                    assignment_start is not None
                    and observed_start is not None
                    and _date(observed_start, "evidence.account.observed_start")
                    >= _date(assignment_start, "assignment.effective_start")
                ):
                    scoped_accounts = [
                        (
                            _text(
                                assignment.get("account_id"),
                                "assignment.account_id",
                            ),
                            "UNIQUE_CURRENT_EFFECTIVE_DATED_ASSIGNMENT",
                            _text(
                                assignment.get("assignment_id"),
                                "assignment.assignment_id",
                            ),
                        )
                    ]
        for account_id, binding, assignment_id in scoped_accounts:
            account_live_paths[(account_id, strategy_id)] = {
                "return_path_account_binding": binding,
                "return_path_assignment_id": assignment_id,
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
            "account_label": None,
            "account_id": account_id,
            "assignment_id": _text(
                assignment.get("assignment_id"), "assignment.assignment_id"
            ),
            "effective_end": end.isoformat() if end is not None else None,
            "effective_start": start.isoformat() if start is not None else None,
            "flow_count": 0,
            "flow_timing": "DATE_ONLY_END_OF_DAY_INFERRED_TIMING",
            "last_four": None,
            "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
            "mwr_xirr": None,
            "nav_observations": 0,
            "observed_end": None,
            "observed_start": None,
            "pool_id": assignment.get("pool_id"),
            "pool_mapping_status": assignment.get("pool_mapping_status"),
            "strategy_mapping_disposition": assignment.get(
                "strategy_mapping_disposition"
            ),
            "strategy_id": assignment.get("strategy_id"),
            "twr": None,
            "twr_status": "UNAVAILABLE",
        }
        epoch_key = cast(
            tuple[str, str, str, str | None],
            (
                account_id,
                assignment.get("strategy_id"),
                assignment.get("effective_start"),
                assignment.get("effective_end"),
            ),
        )
        if current_epoch_details is not None:
            detail = current_epoch_details.get(epoch_key)
            base.update(
                {
                    "account_label": detail["account_label"] if detail else None,
                    "last_four": detail["last_four"] if detail else None,
                    "epoch_admission_status": (
                        detail["epoch_admission_status"] if detail else None
                    ),
                    "epoch_output_status": (
                        detail["epoch_output_status"] if detail else None
                    ),
                    "epoch_blocking_reason": (
                        detail["epoch_blocking_reason"] if detail else None
                    ),
                    "return_path_account_binding": None,
                    "return_path_assignment_id": None,
                    "return_path_authority": None,
                    "return_path_cumulative_return": None,
                    "return_path_evidence_layer": None,
                    "return_path_generation_id": None,
                    "return_path_observations": None,
                    "return_path_observed_end": None,
                    "return_path_observed_start": None,
                    "return_path_series_sha256": None,
                    "return_path_status": None,
                }
            )
            if detail is not None and detail["epoch_admission_status"] == "BLOCKED":
                base.update(
                    {
                        "flow_count": None,
                        "flow_timing": None,
                        "mwr_status": "BLOCKED_ADMISSION",
                        "mwr_xirr": None,
                        "nav_observations": None,
                        "observed_end": None,
                        "observed_start": None,
                        "return_path_account_binding": None,
                        "return_path_assignment_id": None,
                        "return_path_authority": None,
                        "return_path_cumulative_return": None,
                        "return_path_evidence_layer": None,
                        "return_path_generation_id": None,
                        "return_path_observations": None,
                        "return_path_observed_end": None,
                        "return_path_observed_start": None,
                        "return_path_series_sha256": None,
                        "return_path_status": "BLOCKED_ADMISSION",
                        "twr": None,
                        "twr_status": "BLOCKED_ADMISSION",
                    }
                )
                rows.append(base)
                continue
        if mapping_status != "MAPPED" or assignment.get("strategy_id") is None:
            base["twr_status"] = "UNMAPPED"
            rows.append(base)
            continue
        strategy_id = _text(assignment.get("strategy_id"), "assignment.strategy_id")
        if not _is_current_product_strategy(strategy_id, strategy_product_controls):
            base["twr_status"] = "HISTORICAL_ONLY_EXCLUDED_FROM_CURRENT_PRODUCT"
            rows.append(base)
            continue
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
    catalog: Mapping[str, Any],
    hwm_summary: Mapping[str, Any] | None = None,
    dependence_analysis: Mapping[str, Any] | None = None,
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    replay = _object(catalog.get("replay"), "replay")
    total_liquid = replay.get("total_liquid_usd")
    systematic_total = replay.get("systematic_total_usd")
    reserve_total = replay.get("reserve_total_usd")
    retained_excluded_systematic_balance: str | None = None
    if strategy_product_controls is not None:
        replay_total_liquid = _decimal(total_liquid, "replay.total_liquid_usd")
        replay_systematic = _decimal(systematic_total, "replay.systematic_total_usd")
        replay_reserve = _decimal(reserve_total, "replay.reserve_total_usd")
        if replay_total_liquid != replay_systematic + replay_reserve:
            raise ReportValidationError("replay total liquid does not reconcile")
        raw_excluded_systematic = Decimal("0")
        for allocation in _objects(catalog.get("allocations"), "allocations"):
            if allocation.get("scope") != "SYSTEMATIC":
                continue
            strategy_id = allocation.get("strategy_id")
            if (
                not isinstance(strategy_id, str)
                or strategy_id not in strategy_product_controls
            ):
                raise ReportValidationError(
                    "excluded systematic allocation references an unknown product strategy"
                )
            requirement = strategy_product_controls[strategy_id].get(
                "product_requirement"
            )
            if requirement == "REQUIRED":
                continue
            if requirement != "EXCLUDED":
                raise ReportValidationError(
                    "systematic allocation product requirement is invalid"
                )
            excluded_current_value = _object(
                allocation.get("current_value"),
                "excluded systematic allocation.current_value",
            )
            if excluded_current_value.get("status") != "AVAILABLE":
                raise ReportValidationError(
                    "excluded systematic allocation.current_value is not available"
                )
            raw_excluded_systematic += _decimal(
                excluded_current_value.get("value"),
                "excluded systematic allocation.current_value.value",
            )
        current_allocations = _allocations(catalog, strategy_product_controls)
        systematic = sum(
            (
                _decimal(row["current_value_usd"], "allocation.current_value_usd")
                for row in current_allocations
                if row.get("scope") == "SYSTEMATIC"
            ),
            Decimal("0"),
        )
        reserve = sum(
            (
                _decimal(row["current_value_usd"], "allocation.current_value_usd")
                for row in current_allocations
                if row.get("scope") == "RESERVE"
            ),
            Decimal("0"),
        )
        if systematic > replay_systematic:
            raise ReportValidationError(
                "retained excluded systematic balance is negative"
            )
        retained_excluded = replay_systematic - systematic
        if raw_excluded_systematic != retained_excluded:
            raise ReportValidationError(
                "retained excluded systematic allocations differ"
            )
        if reserve != replay_reserve:
            raise ReportValidationError(
                "current reserve allocation differs from replay"
            )
        if systematic + retained_excluded + replay_reserve != replay_total_liquid:
            raise ReportValidationError(
                "portfolio summary total liquid does not reconcile"
            )
        systematic_total = decimal_text(systematic)
        retained_excluded_systematic_balance = decimal_text(retained_excluded)
        reserve_total = decimal_text(replay_reserve)
        total_liquid = decimal_text(replay_total_liquid)
    # The coverage fields exist only when a freshness assessment was
    # supplied; a numerical-only hwm_summary (no freshness) falls back to
    # the replayed values exactly as the pre-envelope report did.
    nav_coverage = hwm_summary.get("nav_coverage") if hwm_summary is not None else None
    coverage_status = hwm_summary.get("status") if hwm_summary is not None else None
    correlation_one_stress = replay.get("correlation_one_stress")
    if (
        dependence_analysis is not None
        and dependence_analysis.get("status") == "AVAILABLE"
    ):
        mode_summaries = _object(
            dependence_analysis.get("mode_summaries"),
            "dependence_analysis.mode_summaries",
        )
        live_only = _object(
            mode_summaries.get("LIVE_ONLY"),
            "dependence_analysis.mode_summaries.LIVE_ONLY",
        )
        correlation_one = _object(
            live_only.get("correlation_one"),
            "dependence_analysis.mode_summaries.LIVE_ONLY.correlation_one",
        )
        correlation_one_stress = _text(
            correlation_one.get("loss"),
            "dependence_analysis.mode_summaries.LIVE_ONLY.correlation_one.loss",
        )
        _decimal(
            correlation_one_stress,
            "dependence_analysis.mode_summaries.LIVE_ONLY.correlation_one.loss",
        )
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
            "value": total_liquid,
        },
        {
            "metric": "Systematic capital",
            "status": "AVAILABLE",
            "unit": "USD",
            "value": systematic_total,
        },
        *(
            [
                {
                    "metric": "Retained excluded systematic balance",
                    "status": "AVAILABLE",
                    "unit": "USD",
                    "value": retained_excluded_systematic_balance,
                }
            ]
            if retained_excluded_systematic_balance is not None
            else []
        ),
        {
            "metric": "Reserve",
            "status": "AVAILABLE",
            "unit": "USD",
            "value": reserve_total,
        },
        {
            "metric": "NAV coverage",
            "status": (
                "COMPLETE_WITHIN_DECLARED_BOUNDARY"
                if nav_coverage == "1"
                else "PARTIAL"
            ),
            "unit": "ratio",
            "value": (
                nav_coverage if nav_coverage is not None else replay.get("nav_coverage")
            ),
        },
        {
            "metric": "Correlation-one stress",
            "status": "ADVERSE_SCENARIO",
            "unit": "ratio",
            "value": correlation_one_stress,
        },
        {
            "metric": "Full-systematic HWM",
            "status": (
                coverage_status if coverage_status is not None else "UNAVAILABLE"
            ),
            "unit": "status",
            "value": (
                coverage_status
                if coverage_status is not None
                else replay.get("full_systematic_hwm_status")
            ),
        },
        {
            "metric": "Advisory readiness",
            "status": "ADVISORY",
            "unit": "status",
            # The readiness surface is this report row: the value is the
            # baseline replay of the combined-control-status source (the
            # replay itself is a frozen-input check in the baseline builder
            # and is deliberately unchanged).  Per the performance-policy
            # ADR, an in-scope HWM perimeter whose numerical composite is
            # UNAVAILABLE gates readiness to DO_NOT_EXPAND_SYSTEMATIC_RISK;
            # the gate only ever tightens the replayed value and changes no
            # other readiness semantics.
            "value": (
                "DO_NOT_EXPAND_SYSTEMATIC_RISK"
                if hwm_summary is not None
                and hwm_summary.get("hwm_numerical_status") == "UNAVAILABLE"
                else replay.get("portfolio_action")
            ),
        },
    ]


def _current_product_account_ids(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None,
) -> set[str] | None:
    """Derive account admission from current, mapped strategy membership."""

    if strategy_product_controls is None:
        return None
    return {
        _text(assignment.get("account_id"), "assignment.account_id")
        for assignment in _objects(catalog.get("assignments"), "assignments")
        if assignment.get("mapping_status") == "MAPPED"
        and assignment.get("effective_end") is None
        and isinstance(assignment.get("strategy_id"), str)
        and _is_current_product_strategy(
            _text(assignment.get("strategy_id"), "assignment.strategy_id"),
            strategy_product_controls,
        )
    }


def _validate_report_account_manifest(
    account_manifest: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, Any]],
    *,
    current_account_ids: set[str] | None = None,
    crosswalk: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> set[tuple[str, str, str, str | None]]:
    """Validate exact current account-strategy epoch identity.

    The explicit current-product crosswalk is the join authority.  A pair-only
    check is insufficient because the same account may legitimately have two
    adjacent strategy epochs, and date changes would otherwise pass unnoticed.
    """

    if set(strategy_product_controls) != CURRENT_STRATEGY_CONTROL_IDS:
        raise ReportValidationError("report account manifest controls are not exact")
    if crosswalk is None:
        raise ReportValidationError(
            "report account manifest requires an explicit current epoch crosswalk"
        )
    rows = _objects(account_manifest.get("rows"), "account_manifest.rows")
    if len(rows) != CURRENT_PRODUCT_EPOCH_COUNT:
        raise ReportValidationError(
            "report account manifest must contain exactly 13 rows"
        )

    crosswalk_rows = (
        _objects(crosswalk.get("rows"), "account crosswalk.rows")
        if isinstance(crosswalk, Mapping)
        else _objects(crosswalk, "account crosswalk")
    )
    admitted = [
        row
        for row in crosswalk_rows
        if row.get("product_epoch_disposition") == "INCLUDED_PRODUCT_EPOCH"
    ]
    if len(admitted) != CURRENT_PRODUCT_EPOCH_COUNT:
        raise ReportValidationError(
            "account crosswalk must contain exactly 13 admitted rows"
        )

    def epoch_tuple(
        row: Mapping[str, Any], field_name: str
    ) -> tuple[str, str, str, str | None]:
        account_id = row.get("account_id")
        strategy_id = row.get("strategy_id")
        if not isinstance(account_id, str) or not account_id:
            raise ReportValidationError(f"{field_name} account_id must be text")
        if (
            not isinstance(strategy_id, str)
            or strategy_id not in CURRENT_REQUIRED_STRATEGY_IDS
        ):
            raise ReportValidationError(f"{field_name} strategy is excluded or invalid")
        if "effective_start" not in row or "effective_end" not in row:
            raise ReportValidationError(f"{field_name} epoch tuple is incomplete")
        effective_start = row.get("effective_start")
        effective_end = row.get("effective_end")
        if not isinstance(effective_start, str):
            raise ReportValidationError(f"{field_name} epoch tuple is invalid")
        try:
            start = date.fromisoformat(effective_start)
        except ValueError as exc:
            raise ReportValidationError(f"{field_name} epoch tuple is invalid") from exc
        if start.isoformat() != effective_start:
            raise ReportValidationError(f"{field_name} epoch tuple is not canonical")
        if effective_end is not None:
            if not isinstance(effective_end, str):
                raise ReportValidationError(f"{field_name} epoch tuple is invalid")
            try:
                end = date.fromisoformat(effective_end)
            except ValueError as exc:
                raise ReportValidationError(
                    f"{field_name} epoch tuple is invalid"
                ) from exc
            if end.isoformat() != effective_end or end < start:
                raise ReportValidationError(f"{field_name} epoch tuple is invalid")
        return account_id, strategy_id, effective_start, effective_end

    expected: set[tuple[str, str, str, str | None]] = set()
    intervals_by_account: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for row in admitted:
        key = epoch_tuple(row, "account crosswalk")
        if key in expected:
            raise ReportValidationError("account crosswalk identities are duplicated")
        expected.add(key)
        intervals_by_account[key[0]].append(
            (
                date.fromisoformat(key[2]),
                None if key[3] is None else date.fromisoformat(key[3]),
            )
        )
    for intervals in intervals_by_account.values():
        intervals.sort(key=lambda item: item[0])
        for previous, current in zip(intervals, intervals[1:]):
            if previous[1] is None or current[0] <= previous[1]:
                raise ReportValidationError("account crosswalk epochs overlap")

    actual: set[tuple[str, str, str, str | None]] = set()
    for row in rows:
        key = epoch_tuple(row, "report account manifest")
        account_id = key[0]
        if current_account_ids is not None and account_id not in current_account_ids:
            raise ReportValidationError("report account manifest account is unknown")
        if key in actual:
            raise ReportValidationError(
                "report account manifest identities are duplicated"
            )
        actual.add(key)
    if actual != expected:
        raise ReportValidationError(
            "report account manifest differs from admitted crosswalk"
        )
    return actual


def _accounts(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
    current_epoch_account_details: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    latest = _latest_nav(catalog)
    synthetic_controls = strategy_product_controls is not None and all(
        re.fullmatch(r"strategy-[0-9]+", strategy_id)
        for strategy_id in strategy_product_controls
    )
    current_account_ids = (
        _current_product_account_ids(catalog, strategy_product_controls)
        if strategy_product_controls is not None
        and current_epoch_account_details is None
        and not synthetic_controls
        else None
    )
    reserve = next(
        item
        for item in _objects(catalog.get("allocations"), "allocations")
        if item.get("scope") == "RESERVE"
    )
    rows: list[dict[str, Any]] = []
    for account in _objects(catalog.get("accounts"), "accounts"):
        account_id = _text(account.get("account_id"), "account.account_id")
        role = _text(account.get("role"), "account.role")
        if role != "RESERVE" and (
            (
                current_epoch_account_details is not None
                and account_id not in current_epoch_account_details
            )
            or (
                current_account_ids is not None
                and account_id not in current_account_ids
            )
        ):
            continue
        if role == "RESERVE":
            value = decimal_text(_amount(reserve, "current_value"))
            as_of = _text(reserve.get("as_of_date"), "reserve.as_of_date")
            status = "AVAILABLE_MANDATE_CLASSIFICATION"
        elif account_id in latest:
            as_of, value = latest[account_id]
            status = "AVAILABLE_LATEST_OBSERVATION"
        else:
            as_of, value, status = None, None, "UNAVAILABLE_NO_NAV"
        account_row = {
            "account_id": account_id,
            "as_of_date": as_of,
            "base_currency": account.get("base_currency"),
            "latest_value_usd": value,
            "provider": account.get("provider"),
            "role": role,
            "status": status,
        }
        if current_epoch_account_details is not None:
            detail = current_epoch_account_details.get(account_id)
            account_row["account_label"] = detail["account_label"] if detail else None
            account_row["last_four"] = detail["last_four"] if detail else None
        rows.append(account_row)
    return sorted(rows, key=lambda item: str(item["account_id"]))


def _is_current_product_strategy(
    strategy_id: str,
    strategy_product_controls: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    """Return whether lifecycle authority admits a strategy to current outputs."""

    if strategy_product_controls is None:
        return True
    control = strategy_product_controls.get(strategy_id)
    return control is not None and control.get("product_requirement") == "REQUIRED"


def _is_governed_financial_mvp2_path(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Identify the real current financial perimeter from bound authority."""

    catalog_strategy_ids = {
        _text(item.get("strategy_id"), "strategy.strategy_id")
        for item in _objects(catalog.get("strategies"), "strategies")
    }
    return (
        catalog_strategy_ids == CURRENT_STRATEGY_CONTROL_IDS
        and set(strategy_product_controls) == CURRENT_STRATEGY_CONTROL_IDS
    )


def _epoch_hpr_receipts(
    catalog: Mapping[str, Any],
    *,
    calculation_cutoff_date: str | None,
) -> dict[str, dict[str, Any]]:
    """Validate the separate Matrixport whole-epoch HPR receipt surface.

    The receipt is intentionally not a strategy-return row: correlation and
    decomposition consume the daily-only series, while only endpoint
    cumulative wealth/TWR may use its exact no-flow multi-day links.  Path-
    dependent drawdown and HWM consumers may not.
    """

    raw_receipts = catalog.get("epoch_hpr_receipts")
    if raw_receipts is None:
        return {}
    receipts: dict[str, dict[str, Any]] = {}
    expected_outer_keys = {
        "evidence_layer",
        "receipt",
        "source_generation_id",
        "source_id",
        "strategy_id",
    }
    expected_receipt_keys = {
        "admission_cutoff",
        "daily_cumulative_return",
        "daily_return_observations",
        "daily_return_series_schema",
        "daily_return_series_sha256",
        "epoch_cumulative_return",
        "epoch_holding_period_link_observations",
        "observed_end",
        "observed_start",
        "source_gap_policy_sha256",
        "span_return_receipts",
    }
    expected_span_keys = {
        "end_endpoint_sha256",
        "missing_source_date",
        "return",
        "span_days",
        "span_end_date",
        "span_start_date",
        "start_endpoint_sha256",
        "subscription_events_in_span",
    }
    for outer in _objects(raw_receipts, "epoch_hpr_receipts"):
        if set(outer) != expected_outer_keys:
            raise ReportValidationError("epoch HPR receipt keys differ")
        strategy_id = _text(outer.get("strategy_id"), "epoch HPR strategy_id")
        if strategy_id in receipts:
            raise ReportValidationError("epoch HPR receipt strategy is duplicated")
        if (
            outer.get("evidence_layer") != "ACCOUNT_LIVE"
            or outer.get("source_id") != "matrixport-account-generation"
            or re.fullmatch(r"[0-9a-f]{64}", str(outer.get("source_generation_id")))
            is None
        ):
            raise ReportValidationError("epoch HPR receipt authority differs")
        receipt = _object(outer.get("receipt"), "epoch HPR receipt")
        if set(receipt) != expected_receipt_keys:
            raise ReportValidationError("epoch HPR receipt keys differ")
        if (
            receipt.get("daily_return_series_schema") != "ordered-date-return/1"
            or not isinstance(receipt.get("daily_return_observations"), int)
            or receipt["daily_return_observations"] < 1
            or not isinstance(
                receipt.get("epoch_holding_period_link_observations"), int
            )
            or receipt["epoch_holding_period_link_observations"]
            < receipt["daily_return_observations"]
        ):
            raise ReportValidationError("epoch HPR receipt observations differ")
        for field_name in (
            "daily_return_series_sha256",
            "source_gap_policy_sha256",
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field_name))) is None:
                raise ReportValidationError("epoch HPR receipt hash differs")
        start = _date(receipt.get("observed_start"), "epoch HPR observed_start")
        end = _date(receipt.get("observed_end"), "epoch HPR observed_end")
        if end < start:
            raise ReportValidationError("epoch HPR receipt interval differs")
        cutoff = receipt.get("admission_cutoff")
        if cutoff is not None and _date(cutoff, "epoch HPR admission_cutoff") != end:
            raise ReportValidationError("epoch HPR receipt cutoff differs")
        out_of_window = False
        if calculation_cutoff_date is not None and cutoff != calculation_cutoff_date:
            # The calculation runs through the latest date common to every required
            # series.  A receipt may legitimately describe evidence past that date
            # (a ragged perimeter); that evidence is not used.  A receipt that stops
            # BEFORE the window still means the epoch is short and remains an error.
            if _date(cutoff, "epoch HPR admission_cutoff") < _date(
                calculation_cutoff_date, "calculation cutoff date"
            ):
                raise ReportValidationError("epoch HPR receipt cutoff differs")
            out_of_window = True
        _decimal(receipt.get("daily_cumulative_return"), "epoch HPR daily return")
        _decimal(receipt.get("epoch_cumulative_return"), "epoch HPR return")
        receipt = {**receipt, "_out_of_window": out_of_window}
        spans = _objects(receipt.get("span_return_receipts"), "epoch HPR spans")
        prior_end: date | None = None
        for span in spans:
            if set(span) != expected_span_keys:
                raise ReportValidationError("epoch HPR span keys differ")
            span_start = _date(span.get("span_start_date"), "epoch HPR span start")
            span_end = _date(span.get("span_end_date"), "epoch HPR span end")
            missing = _date(span.get("missing_source_date"), "epoch HPR span gap")
            if (
                span_end <= span_start
                or span.get("span_days") != (span_end - span_start).days
                or span.get("span_days") != 2
                or missing <= span_start
                or missing >= span_end
                or span.get("subscription_events_in_span") != 0
                or _decimal(span.get("return"), "epoch HPR span return") <= -1
                or (prior_end is not None and span_start <= prior_end)
            ):
                raise ReportValidationError("epoch HPR span differs")
            for field_name in ("start_endpoint_sha256", "end_endpoint_sha256"):
                if re.fullmatch(r"[0-9a-f]{64}", str(span.get(field_name))) is None:
                    raise ReportValidationError("epoch HPR span hash differs")
            prior_end = span_end
        if receipt["epoch_holding_period_link_observations"] != (
            receipt["daily_return_observations"] + len(spans)
        ):
            raise ReportValidationError("epoch HPR receipt link count differs")
        receipts[strategy_id] = receipt
    return receipts


def _account_live_return_series(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    calculation_cutoff_date: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Hash and summarize each canonical account-live strategy return path."""

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    providers: dict[str, set[str]] = defaultdict(set)
    cutoff = (
        None
        if calculation_cutoff_date is None
        else _date(calculation_cutoff_date, "calculation cutoff date")
    )
    for row in _objects(catalog.get("strategy_returns", []), "strategy_returns"):
        if row.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy_id = _text(row.get("strategy_id"), "strategy return.strategy_id")
        if not _is_current_product_strategy(strategy_id, strategy_product_controls):
            continue
        parsed_day = _date(row.get("date"), "strategy return.date")
        if cutoff is not None and parsed_day > cutoff:
            continue
        day = parsed_day.isoformat()
        value = decimal_text(_decimal(row.get("return"), "strategy return.return"))
        grouped[strategy_id].append({"date": day, "return": value})
        providers[strategy_id].add(
            _text(row.get("provider_id"), "strategy return.provider_id")
        )

    epoch_receipts = _epoch_hpr_receipts(
        catalog, calculation_cutoff_date=calculation_cutoff_date
    )
    if set(epoch_receipts) - set(grouped):
        raise ReportValidationError("epoch HPR receipt has no daily path")
    result: dict[str, dict[str, Any]] = {}
    for strategy_id, rows in grouped.items():
        rows.sort(key=lambda item: item["date"])
        dates = [item["date"] for item in rows]
        if len(dates) != len(set(dates)):
            raise ReportValidationError(
                f"account-live strategy returns repeat a date for {strategy_id}"
            )
        epoch_receipt = epoch_receipts.get(strategy_id)
        with localcontext() as context:
            # Matrixport's retained source arithmetic is a 28-significant-
            # digit Decimal path.  Replaying its HPR receipt at a wider local
            # precision changes the final digit and breaks the receipt binding.
            context.prec = 28 if epoch_receipt is not None else 34
            wealth = Decimal("1")
            peak = wealth
            maximum_drawdown = Decimal("0")
            spans_by_start: dict[str, list[dict[str, Any]]] = defaultdict(list)
            path_dependent_metrics_available = True
            if epoch_receipt is not None:
                out_of_window = bool(epoch_receipt.get("_out_of_window"))
                if not out_of_window and (
                    epoch_receipt["daily_return_observations"] != len(rows)
                    or epoch_receipt["observed_start"] != dates[0]
                    or epoch_receipt["observed_end"] != dates[-1]
                    or epoch_receipt["daily_return_series_sha256"]
                    != hashlib.sha256(_canonical_json_array_bytes(rows)).hexdigest()
                ):
                    raise ReportValidationError("epoch HPR daily path differs")
                daily_wealth = Decimal("1")
                for item in rows:
                    daily_wealth *= Decimal(1) + _decimal(
                        item["return"], "canonical strategy return"
                    )
                if not out_of_window and decimal_text(
                    daily_wealth - Decimal(1)
                ) != decimal_text(
                    _decimal(
                        epoch_receipt["daily_cumulative_return"],
                        "epoch HPR daily return",
                    )
                ):
                    raise ReportValidationError("epoch HPR daily return differs")
                for span in _objects(
                    epoch_receipt["span_return_receipts"], "epoch HPR spans"
                ):
                    if out_of_window and str(span["span_end_date"]) > dates[-1]:
                        continue
                    spans_by_start[str(span["span_start_date"])].append(span)
                path_dependent_metrics_available = not bool(spans_by_start)
                if not out_of_window and any(
                    start not in dates
                    or any(
                        start < observed <= str(span["span_end_date"])
                        for observed in dates
                    )
                    for start, spans in spans_by_start.items()
                    for span in spans
                ):
                    raise ReportValidationError("epoch HPR span does not replace a gap")
            for item in rows:
                wealth *= Decimal("1") + _decimal(
                    item["return"], "canonical strategy return"
                )
                if path_dependent_metrics_available:
                    peak = max(peak, wealth)
                    drawdown = (wealth / peak) - Decimal("1")
                    maximum_drawdown = min(maximum_drawdown, drawdown)
                for span in spans_by_start.get(item["date"], []):
                    wealth *= Decimal(1) + _decimal(
                        span["return"], "epoch HPR span return"
                    )
            cumulative_return = wealth - Decimal("1")
            current_drawdown = (
                (wealth / peak) - Decimal("1")
                if path_dependent_metrics_available
                else None
            )
            if (
                epoch_receipt is not None
                and not epoch_receipt.get("_out_of_window")
                and decimal_text(cumulative_return)
                != decimal_text(
                    _decimal(
                        epoch_receipt["epoch_cumulative_return"], "epoch HPR return"
                    )
                )
            ):
                raise ReportValidationError("epoch HPR return differs")
        result[strategy_id] = {
            "cumulative_return": decimal_text(cumulative_return),
            "daily_cumulative_return": decimal_text(daily_wealth - Decimal("1"))
            if epoch_receipt is not None
            else decimal_text(cumulative_return),
            "cumulative_return_label": (
                "EPOCH_CUMULATIVE_TWR_WITH_HASH_BOUND_HPR_GAPS"
                if epoch_receipt is not None and epoch_receipt["span_return_receipts"]
                else "DAILY_GEOMETRIC_TWR"
            ),
            "current_drawdown": (
                decimal_text(current_drawdown) if current_drawdown is not None else None
            ),
            "maximum_drawdown": (
                decimal_text(maximum_drawdown)
                if path_dependent_metrics_available
                else None
            ),
            "path_dependent_metrics_status": (
                "AVAILABLE" if path_dependent_metrics_available else "UNAVAILABLE"
            ),
            "path_dependent_metrics_reason": (
                None
                if path_dependent_metrics_available
                else "MISSING_DAILY_POINT_CAN_HIDE_PEAK"
            ),
            "observed_end": dates[-1],
            "observed_start": dates[0],
            "provider_ids": sorted(providers[strategy_id]),
            "return_observations": len(rows),
            "return_series_schema": "ordered-date-return/1",
            "return_series_sha256": hashlib.sha256(
                _canonical_json_array_bytes(rows)
            ).hexdigest(),
            "epoch_holding_period_link_observations": (
                epoch_receipts[strategy_id]["epoch_holding_period_link_observations"]
                if strategy_id in epoch_receipts
                else len(rows)
            ),
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


def _report_product_controls(
    catalog: Mapping[str, Any],
    path: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str] | None]:
    """Load lifecycle controls when this catalog belongs to the product contract."""

    if path is None:
        path = _PORTABLE_HWM_PRODUCT_CONTRACT
    payload = _load_json(path, "financial MVP contract")
    schema_version = payload.get("contract_schema_version")
    product = _object(payload.get("product"), "financial MVP product")
    if schema_version not in {6, 7} or product.get("product_id") != "FINANCIAL_MVP":
        raise ReportValidationError("financial MVP contract identity differs")
    controls: dict[str, dict[str, Any]] = {}
    for item in _objects(payload.get("strategies"), "financial MVP strategies"):
        strategy_id = _text(item.get("strategy_id"), "financial MVP strategy_id")
        if strategy_id in controls:
            raise ReportValidationError("financial MVP strategy is duplicated")
        lifecycle = _text(
            item.get("lifecycle_status"), "financial MVP lifecycle_status"
        )
        requirement = _text(
            item.get("product_requirement"), "financial MVP product_requirement"
        )
        funded_membership = _object(
            item.get("funded_membership"), "financial MVP funded_membership"
        )
        funded = _text(
            funded_membership.get("status"),
            "financial MVP funded_membership.status",
        )
        funded_start = (
            None
            if funded_membership.get("effective_start") is None
            else _date(
                funded_membership.get("effective_start"),
                "financial MVP funded_membership.effective_start",
            )
        )
        funded_end = (
            None
            if funded_membership.get("effective_end") is None
            else _date(
                funded_membership.get("effective_end"),
                "financial MVP funded_membership.effective_end",
            )
        )
        if (
            funded_start is not None
            and funded_end is not None
            and funded_end < funded_start
        ):
            raise ReportValidationError(
                "financial MVP funded_membership interval is reversed"
            )
        if requirement == "REQUIRED":
            if lifecycle != "ACTIVE" or funded != "EVIDENCED":
                raise ReportValidationError(
                    "required financial MVP strategy lifecycle differs"
                )
        elif requirement == "EXCLUDED":
            if (
                lifecycle
                not in {
                    "DEPRECATED_HISTORY_ONLY",
                    "IMMATERIAL_EXCLUDED",
                }
                or funded != "EXCLUDED"
            ):
                raise ReportValidationError(
                    "excluded financial MVP strategy lifecycle differs"
                )
        else:
            raise ReportValidationError("financial MVP strategy requirement differs")
        if (
            item.get("effective_product_interval") is None
            and strategy_id in CURRENT_STRATEGY_CONTROL_IDS
        ):
            raise ReportValidationError(
                "financial MVP effective_product_interval must be an object"
            )
        product_interval = (
            _object(
                item.get("effective_product_interval"),
                "financial MVP effective_product_interval",
            )
            if item.get("effective_product_interval") is not None
            else {}
        )
        product_start = (
            _date(
                product_interval.get("effective_start"),
                "financial MVP effective_product_interval.effective_start",
            )
            if product_interval
            else None
        )
        product_end = (
            None
            if not product_interval or product_interval.get("effective_end") is None
            else _date(
                product_interval.get("effective_end"),
                "financial MVP effective_product_interval.effective_end",
            )
        )
        if (
            product_start is not None
            and product_end is not None
            and product_end < product_start
        ):
            raise ReportValidationError(
                "financial MVP effective_product_interval is reversed"
            )
        if strategy_id in CURRENT_REQUIRED_STRATEGY_IDS and (
            funded_start is None
            or funded_end is not None
            or product_start is None
            or product_start.isoformat() != "2026-08-17"
            or product_end is not None
        ):
            raise ReportValidationError(
                "required financial MVP strategy interval differs"
            )
        if strategy_id == "echotrade-growth-pivots":
            outcomes = _object(
                item.get("outcome_dispositions"),
                "EchoTrade outcome_dispositions",
            )
            if (
                lifecycle != "DEPRECATED_HISTORY_ONLY"
                or requirement != "EXCLUDED"
                or funded != "EXCLUDED"
                or funded_start is not None
                or funded_end is not None
                or product_start is None
                or product_start.isoformat() != "2025-09-27"
                or product_end is None
                or product_end.isoformat() != "2025-11-24"
                or set(outcomes)
                != {
                    "AS_HELD_COMPOSITE_HWM",
                    "INVESTED_CAPITAL",
                    "DECOMPOSITION",
                    "DEPENDENCE",
                }
                or set(outcomes.values()) != {"EXCLUDED_BY_ACCEPTED_POLICY"}
            ):
                raise ReportValidationError(
                    "deprecated EchoTrade owner authority differs"
                )
        if strategy_id == "composer-holy-grail" and (
            lifecycle != "IMMATERIAL_EXCLUDED"
            or requirement != "EXCLUDED"
            or funded != "EXCLUDED"
            or funded_start is not None
            or funded_end is not None
            or product_start is None
            or product_start.isoformat() != "2026-08-17"
            or product_end is None
            or product_end.isoformat() != "2026-08-17"
        ):
            raise ReportValidationError("Composer owner exclusion differs")
        controls[strategy_id] = {
            "funded_membership_status": funded,
            "funded_membership_effective_start": (
                funded_start.isoformat() if funded_start is not None else None
            ),
            "funded_membership_effective_end": (
                funded_end.isoformat() if funded_end is not None else None
            ),
            "lifecycle_status": lifecycle,
            "product_requirement": requirement,
            "effective_product_interval_start": (
                product_start.isoformat() if product_start is not None else None
            ),
            "effective_product_interval_end": (
                product_end.isoformat() if product_end is not None else None
            ),
        }
    catalog_ids = {
        _text(item.get("strategy_id"), "strategy.strategy_id")
        for item in _objects(catalog.get("strategies"), "strategies")
    }
    overlap = catalog_ids & set(controls)
    if not overlap:
        raise ReportValidationError(
            "financial MVP strategy perimeter has no approved product overlap"
        )
    synthetic_ids = all(
        re.fullmatch(r"strategy-[0-9]+", strategy_id) for strategy_id in controls
    )
    if set(controls) != CURRENT_STRATEGY_CONTROL_IDS and not synthetic_ids:
        raise ReportValidationError(
            "financial MVP contract must contain exact financial MVP strategy control IDs"
        )
    if not synthetic_ids and sum(
        control["product_requirement"] == "REQUIRED" for control in controls.values()
    ) != len(CURRENT_REQUIRED_STRATEGY_IDS):
        raise ReportValidationError(
            "financial MVP contract must contain 7 required strategies"
        )
    if catalog_ids != set(controls):
        raise ReportValidationError("financial MVP strategy perimeter differs")
    census = _object(payload.get("current_census"), "financial MVP current_census")
    census_as_of = _date(census.get("as_of"), "financial MVP current_census.as_of")
    sha256, _size = hash_file(path)
    return controls, {
        "artifact": "financial-mvp-contract",
        "as_of_date": census_as_of.isoformat(),
        "schema": f"portfolio-financial-mvp-source/{schema_version}",
        "sha256": sha256,
    }


def _current_product_baseline_projection(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Project owner-governed current HWM inputs from a retained baseline."""

    required_ids = {
        strategy_id
        for strategy_id, control in strategy_product_controls.items()
        if control.get("product_requirement") == "REQUIRED"
    }
    if len(required_ids) != 7:
        raise ReportValidationError(
            "current-product baseline projection requires 7 strategies"
        )

    def current_strategy_rows(value: Any, field_name: str) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for row in _objects(value, field_name):
            strategy_id = row.get("strategy_id")
            if not isinstance(strategy_id, str):
                raise ReportValidationError(f"{field_name} strategy_id must be text")
            control = strategy_product_controls.get(strategy_id)
            if control is None:
                raise ReportValidationError(
                    f"{field_name} references an unknown product strategy"
                )
            if strategy_id in required_ids:
                observation_date = _date(row.get("date"), f"{field_name}.date")
                funded_start = (
                    None
                    if control.get("funded_membership_effective_start") is None
                    else _date(
                        control.get("funded_membership_effective_start"),
                        f"{strategy_id}.funded_membership_effective_start",
                    )
                )
                funded_end = (
                    None
                    if control.get("funded_membership_effective_end") is None
                    else _date(
                        control.get("funded_membership_effective_end"),
                        f"{strategy_id}.funded_membership_effective_end",
                    )
                )
                if not _inside(observation_date, funded_start, funded_end):
                    continue
                projected.append(dict(row))
        return projected

    allocations: list[dict[str, Any]] = []
    for row in _objects(catalog.get("allocations"), "allocations"):
        scope = row.get("scope")
        strategy_id = row.get("strategy_id")
        if scope == "RESERVE":
            if strategy_id is not None:
                raise ReportValidationError(
                    "reserve allocation must not reference a strategy"
                )
            allocations.append(dict(row))
            continue
        if scope != "SYSTEMATIC" or not isinstance(strategy_id, str):
            raise ReportValidationError(
                "current systematic allocation strategy_id must be text"
            )
        control = strategy_product_controls.get(strategy_id)
        if control is None:
            raise ReportValidationError(
                "current systematic allocation references an unknown product strategy"
            )
        if strategy_id in required_ids:
            allocations.append(dict(row))

    projection = dict(catalog)
    projection["allocations"] = allocations
    projection["strategy_returns"] = current_strategy_rows(
        catalog.get("strategy_returns", []), "strategy_returns"
    )
    raw_hwm_inputs = catalog.get("hwm_inputs")
    if raw_hwm_inputs is not None:
        hwm_inputs = dict(_object(raw_hwm_inputs, "hwm_inputs"))
        hwm_inputs["periods"] = current_strategy_rows(
            hwm_inputs.get("periods"), "hwm_inputs.periods"
        )
        projection["hwm_inputs"] = hwm_inputs
    return projection


def _current_product_fact_rows(
    rows: Sequence[Mapping[str, Any]],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None,
    current_account_ids: set[str] | None,
    field_name: str,
) -> list[dict[str, Any]]:
    """Filter current-product facts by explicit strategy and account authority."""

    if strategy_product_controls is None and current_account_ids is None:
        return [dict(row) for row in rows]
    legacy_synthetic_controls = strategy_product_controls is not None and all(
        re.fullmatch(r"strategy-[0-9]+", strategy_id)
        for strategy_id in strategy_product_controls
    )
    accepted: list[dict[str, Any]] = []
    for row in rows:
        strategy_id = row.get("strategy_id")
        account_id = row.get("account_id")
        if strategy_id is None and account_id is None:
            continue
        if (
            strategy_product_controls is not None
            and strategy_id is None
            and not legacy_synthetic_controls
        ):
            continue
        if strategy_id is not None and not isinstance(strategy_id, str):
            raise ReportValidationError(f"{field_name} strategy_id is invalid")
        if account_id is not None and not isinstance(account_id, str):
            raise ReportValidationError(f"{field_name} account_id is invalid")
        if strategy_product_controls is not None and isinstance(strategy_id, str):
            control = strategy_product_controls.get(strategy_id)
            if control is None:
                raise ReportValidationError(
                    f"{field_name} references an unknown product strategy"
                )
            if control.get("product_requirement") != "REQUIRED":
                continue
        if current_account_ids is not None:
            if account_id is None or account_id not in current_account_ids:
                continue
        accepted.append(dict(row))
    return accepted


def _strategy_provider_evidence(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
    *,
    calculation_cutoff_date: str | None = None,
) -> list[dict[str, Any]]:
    cutoff = (
        None
        if calculation_cutoff_date is None
        else _date(calculation_cutoff_date, "calculation cutoff date")
    )
    series = _account_live_return_series(
        catalog,
        strategy_product_controls,
        calculation_cutoff_date=calculation_cutoff_date,
    )
    # Resolve the effective-dated strategy version for each strategy from the
    # governed catalog so the panel can verify series identity downstream.
    strategy_versions: dict[str, str] = {}
    version_catalog = catalog.get("strategy_version_catalog")
    if version_catalog is not None:
        version_data = _object(version_catalog, "strategy_version_catalog")
        for item in _objects(
            version_data.get("strategies"), "strategy version catalog"
        ):
            sid = _text(item.get("strategy_id"), "strategy version strategy_id")
            vref = item.get("version_ref")
            if vref is not None:
                strategy_versions[sid] = _text(vref, f"strategy version {sid}")

    rows: list[dict[str, Any]] = []
    for original in _objects(
        catalog.get("strategy_provider_evidence", []),
        "strategy_provider_evidence",
    ):
        row = dict(original)
        strategy_id = _text(row.get("strategy_id"), "provider evidence.strategy_id")
        row["strategy_version"] = strategy_versions.get(strategy_id)
        product_control = (
            strategy_product_controls.get(strategy_id)
            if strategy_product_controls is not None
            else None
        )
        if product_control is not None:
            row.update(product_control)
            lifecycle = product_control["lifecycle_status"]
            if lifecycle == "DEPRECATED_HISTORY_ONLY":
                row["product_evidence_disposition"] = (
                    "HISTORICAL_ONLY_EXCLUDED_FROM_CURRENT_PRODUCT"
                )
            elif lifecycle == "IMMATERIAL_EXCLUDED":
                row["product_evidence_disposition"] = (
                    "IMMATERIAL_EXCLUDED_FROM_CURRENT_PRODUCT"
                )
            else:
                row["product_evidence_disposition"] = "REQUIRED_CURRENT_PRODUCT"
        if row.get("evidence_layer") == "ACCOUNT_LIVE":
            canonical = series.get(strategy_id)
            if canonical is None:
                if (
                    product_control is not None
                    and product_control["product_requirement"] == "EXCLUDED"
                ):
                    rows.append(row)
                    continue
                raise ReportValidationError(
                    f"account-live evidence has no canonical return series for {strategy_id}"
                )
            account = _object(row.get("account"), "provider evidence account")
            if cutoff is None:
                for field_name in (
                    "cumulative_return",
                    "current_drawdown",
                    "maximum_drawdown",
                ):
                    expected_value = canonical[field_name]
                    if (
                        field_name == "cumulative_return"
                        and canonical["path_dependent_metrics_status"] == "UNAVAILABLE"
                    ):
                        expected_value = canonical["daily_cumulative_return"]
                    if expected_value is None:
                        continue
                    _close_decimal(
                        account.get(field_name),
                        expected_value,
                        f"{strategy_id}.{field_name}",
                    )
                for field_name in (
                    "observed_start",
                    "observed_end",
                    "return_observations",
                ):
                    if account.get(field_name) != canonical[field_name]:
                        raise ReportValidationError(
                            f"{strategy_id}.{field_name} differs from canonical return series"
                        )
                normalized_scopes = account.get("account_scopes")
            else:
                raw_scopes = account.get("account_scopes")
                normalized_scopes = None
                if raw_scopes is not None:
                    normalized_scopes = []
                    for raw_scope in _objects(
                        raw_scopes, "provider evidence account_scopes"
                    ):
                        scope = dict(raw_scope)
                        start = _date(
                            scope.get("observed_start"),
                            "provider evidence scope.observed_start",
                        )
                        end = _date(
                            scope.get("observed_end"),
                            "provider evidence scope.observed_end",
                        )
                        if end < start:
                            raise ReportValidationError(
                                "provider evidence scope dates are reversed"
                            )
                        if start > cutoff:
                            continue
                        scope["observed_start"] = start.isoformat()
                        scope["observed_end"] = min(end, cutoff).isoformat()
                        normalized_scopes.append(scope)
            provider_id = _text(account.get("provider_id"), "account.provider_id")
            if provider_id not in canonical["provider_ids"]:
                raise ReportValidationError(
                    f"{strategy_id}.provider_id differs from canonical return series"
                )
            row["account"] = {
                **account,
                **(
                    {"account_scopes": normalized_scopes}
                    if normalized_scopes is not None
                    else {}
                ),
                "cumulative_return": canonical["cumulative_return"],
                "current_drawdown": canonical["current_drawdown"],
                "max_drawdown": canonical["maximum_drawdown"],
                "maximum_drawdown": canonical["maximum_drawdown"],
                "cumulative_return_label": canonical["cumulative_return_label"],
                "path_dependent_metrics_status": canonical[
                    "path_dependent_metrics_status"
                ],
                "path_dependent_metrics_reason": canonical[
                    "path_dependent_metrics_reason"
                ],
                "observed_end": canonical["observed_end"],
                "observed_start": canonical["observed_start"],
                "return_observations": canonical["return_observations"],
                "return_series_schema": canonical["return_series_schema"],
                "return_series_sha256": canonical["return_series_sha256"],
            }
        rows.append(row)
    return rows


def _accepted_pre_live_observations(
    history: Mapping[str, Any], strategy_version: str
) -> int:
    """Count admitted prehistory in an already authenticated coverage row."""
    segments = _objects(history.get("segments"), "history segments")
    live_starts = [
        segment["observed_start"]
        for segment in segments
        if segment.get("evidence_layer") == "ACCOUNT_LIVE"
        and segment.get("mode_admission") == "INCLUDE_SELECTED_LIVE"
        and segment.get("strategy_version") == strategy_version
    ]
    return sum(
        segment["return_observations"]
        for segment in segments
        if live_starts
        and segment.get("evidence_layer") == "BACKTEST"
        and segment.get("mode_admission") == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        and segment.get("strategy_version") == strategy_version
        and segment["observed_end"] < min(live_starts)
    )


def _history_contract(
    path: Path,
    *,
    portfolio_as_of: str,
    strategy_names: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _load_json(path, "strategy history compatibility contract")
    if payload.get("builder") == "portfolio-current-dependence-source":
        module_path = (
            Path(__file__).resolve().with_name("portfolio_current_dependence.py")
        )
        module_name = "portfolio_current_dependence_for_portfolio_report"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ReportValidationError("current dependence verifier is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            verification = module.verify_packaged_source(
                path.parent,
                contract_path=_PORTABLE_HWM_PRODUCT_CONTRACT,
                policy_path=DEPENDENCE_POLICY,
            )
        except Exception as exc:
            raise ReportValidationError(
                f"current dependence source is invalid: {exc}"
            ) from exc
        finally:
            sys.modules.pop(module_name, None)
        if verification.get("verification") != "VALID":
            raise ReportValidationError("current dependence source is not verified")
        dependence_spec = _load_json(path.parent / "spec.json", "dependence spec")
        required_rows = _objects(
            dependence_spec.get("evidence_mode_series"),
            "dependence evidence_mode_series",
        )
        required_names = {
            _text(row.get("series_id"), "dependence series_id") for row in required_rows
        }
        if not required_names or not required_names < strategy_names:
            raise ReportValidationError("current dependence strategy boundary differs")
        current_by_name = {
            _text(row.get("series_id"), "dependence series_id"): {
                "backtest_observations": 0,
                "history_disposition_reason": (
                    "No version-matched backtest prehistory is admitted; the hybrid "
                    "mode is the exact live-only path."
                ),
                "history_status": "UNAVAILABLE_NO_MATCHED_BACKTEST_PREHISTORY",
                "implementation_reconciliation_status": "ACCEPTED_ACCOUNT_LIVE",
                "series_id": row["series_id"],
                "strategy_version": row["strategy_version"],
            }
            for row in required_rows
        }
        # The dependence projection deliberately uses account-live returns.
        # Its authenticated history coverage can also admit exact-version
        # prehistory used by the separate decomposition consumer. Preserve that
        # history without claiming it contributed to dependence observations.
        coverage = _load_json(path.parent / "history-coverage.json", "history coverage")
        for history in _objects(coverage.get("strategies", []), "history strategies"):
            name = history.get("series_id")
            if name not in current_by_name:
                continue
            target = current_by_name[name]
            observations = _accepted_pre_live_observations(
                history, target["strategy_version"]
            )
            if observations > 0:
                target.update(
                    {
                        "backtest_observations": observations,
                        "compatibility_basis": (
                            "HASH_BOUND_EXACT_VERSION_BACKTEST_STRICTLY_BEFORE_ACCOUNT_LIVE"
                        ),
                        "history_disposition_reason": (
                            "Version-matched prehistory is admitted by verified history "
                            "coverage; dependence retains its account-live projection."
                        ),
                        "history_status": "ACCEPTED_VERSION_COMPATIBLE_PREHISTORY",
                    }
                )
        for name in sorted(strategy_names - required_names):
            current_by_name[name] = {
                "backtest_observations": 0,
                "history_disposition_reason": (
                    "Strategy is outside the owner-approved current dependence perimeter."
                ),
                "history_status": "EXCLUDED_OWNER_APPROVED_CURRENT_DEPENDENCE",
                "implementation_reconciliation_status": "NOT_APPLICABLE",
                "series_id": name,
                "strategy_version": "UNRESOLVED",
            }
        sha256, size = hash_file(path)
        return current_by_name, {
            "age_evidence": _object(
                payload.get("age_evidence"), "dependence age_evidence"
            ),
            "as_of_date": _date(
                payload.get("complete_through"), "dependence complete_through"
            ).isoformat(),
            "bundle_id": _text(payload.get("bundle_id"), "dependence bundle_id"),
            "evidence_mode_policy": {
                "default": "LIVE_ONLY",
                "hybrid": "LIVE_ONLY_WHEN_NO_MATCHED_BACKTEST_PREHISTORY",
                "post_inception_backtest_fill": False,
            },
            "evaluated_on": _date(
                payload.get("evaluated_on"), "dependence evaluated_on"
            ).isoformat(),
            "epoch_hpr_receipts": verification["epoch_hpr_receipts"],
            "schema": "portfolio-current-dependence-source/1",
            "returns_sha256": _text(
                payload.get("returns_sha256"), "dependence returns_sha256"
            ),
            "returns_size_bytes": payload.get("returns_size_bytes"),
            "series_count": len(current_by_name),
            "sha256": sha256,
            "size_bytes": size,
        }
    if payload.get("builder") != "portfolio-target-series/layered-correlation-1":
        raise ReportValidationError("strategy history contract builder differs")
    contract_as_of = _date(payload.get("as_of_date"), "history contract as_of_date")
    if contract_as_of > _date(portfolio_as_of, "portfolio as_of_date"):
        raise ReportValidationError("strategy history contract is future-dated")
    policy = _object(
        payload.get("evidence_mode_policy"),
        "history contract evidence_mode_policy",
    )
    if policy != {
        "default": "LIVE_ONLY",
        "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
        "post_inception_backtest_fill": False,
    }:
        raise ReportValidationError("strategy history evidence policy differs")
    rows = _objects(payload.get("series_mappings"), "history contract series_mappings")
    by_name: dict[str, dict[str, Any]] = {}
    for raw in rows:
        name = _text(raw.get("series_id"), "history contract series_id")
        if name in by_name:
            raise ReportValidationError(
                f"strategy history contract duplicates series: {name}"
            )
        history_status = _text(raw.get("history_status"), f"history status {name}")
        _text(raw.get("strategy_version"), f"history version {name}")
        backtest_observations = raw.get("backtest_observations")
        if not isinstance(backtest_observations, int) or backtest_observations < 0:
            raise ReportValidationError(
                f"strategy history observations are invalid: {name}"
            )
        accepted = history_status.startswith("ACCEPTED_")
        if accepted != (backtest_observations > 0):
            raise ReportValidationError(
                f"strategy history status and observations differ: {name}"
            )
        compatibility_basis = raw.get("compatibility_basis")
        if accepted:
            _text(compatibility_basis, f"history compatibility basis {name}")
            _date(raw.get("backtest_observed_start"), f"history start {name}")
            _date(raw.get("backtest_observed_end"), f"history end {name}")
        elif compatibility_basis is not None:
            raise ReportValidationError(
                f"unavailable strategy has a compatibility basis: {name}"
            )
        _text(
            raw.get("implementation_reconciliation_status"),
            f"implementation reconciliation status {name}",
        )
        by_name[name] = dict(raw)
    if set(by_name) != strategy_names:
        raise ReportValidationError("strategy history contract boundary differs")
    sha256, size = hash_file(path)
    return by_name, {
        "as_of_date": contract_as_of.isoformat(),
        "evidence_mode_policy": dict(policy),
        "series_count": len(by_name),
        "schema": "portfolio-target-series-evidence-proof/1",
        "returns_sha256": _text(payload.get("target_sha256"), "history target_sha256"),
        "returns_size_bytes": payload.get("target_size_bytes"),
        "sha256": sha256,
        "size_bytes": size,
    }


def _validate_implementation_observed_intervals(
    value: object,
    *,
    boundary: date,
    field_path: str,
) -> None:
    if isinstance(value, Mapping):
        start = (
            _date(value["observed_start"], f"{field_path}.observed_start")
            if "observed_start" in value
            else None
        )
        end = (
            _date(value["observed_end"], f"{field_path}.observed_end")
            if "observed_end" in value
            else None
        )
        if (start is not None and start > boundary) or (
            end is not None and end > boundary
        ):
            raise ReportValidationError(
                "implementation reconciliation observed interval is future-dated"
            )
        if start is not None and end is not None and end < start:
            raise ReportValidationError(
                "implementation reconciliation observed interval is reversed"
            )
        for key, child in value.items():
            _validate_implementation_observed_intervals(
                child,
                boundary=boundary,
                field_path=f"{field_path}.{key}",
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_implementation_observed_intervals(
                child,
                boundary=boundary,
                field_path=f"{field_path}[{index}]",
            )


def _selected_implementation_policy_evidence(
    catalog: Mapping[str, Any],
    baseline_root: Path,
    provider_ledger_root: Path | None,
    *,
    accepted_as_of: date,
) -> dict[str, Any] | None:
    import portfolio_implementation_reconciliation as implementation

    identity = _baseline_provider_ledger_identity(catalog)
    root = provider_ledger_root or baseline_root.parent / "provider-ledger"
    candidates = [
        path.parent
        for path in (root / "generations").glob("*/generation.json")
        if not path.is_symlink()
        and hash_file(path)[0] == identity["generation-manifest"]
    ]
    if len(candidates) != 1:
        raise ReportValidationError(
            "implementation policy selected provider generation is ambiguous or absent"
        )
    generation = candidates[0]
    observed = _provider_ledger_identity(root, generation.name)
    if observed["catalog_sha256"] != identity["provider-ledger-catalog"]:
        raise ReportValidationError("implementation policy provider catalog differs")
    try:
        return implementation.p123_provider_return_policy(
            generation, accepted_as_of=accepted_as_of
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError(
            "implementation policy source proof differs"
        ) from exc


def _validate_policy_unavailable_comparison(
    value: Mapping[str, Any],
    *,
    strategy_id: str,
    attribution: bool = False,
    expected_policy_evidence: Mapping[str, Any] | None = None,
) -> None:
    status = "UNAVAILABLE_POLICY_NOT_ADMITTED"
    nullable_fields = (
        {"model_return", "gap_pp"}
        if attribution
        else {"model_cumulative_return", "account_minus_model_cumulative"}
    )
    required = {"status", "policy_evidence", *nullable_fields}
    required.update(
        {"account_return", "categories"} if attribution else {"observations"}
    )
    proof = _object(value.get("policy_evidence"), "implementation policy evidence")
    if (
        strategy_id != "p123-ai-factor"
        or value.get("status") != status
        or set(value) != required
        or any(value[field] is not None for field in nullable_fields)
        or proof.get("provider_id") != "portfolio123"
        or proof.get("data_class") != "PROVIDER_LIVE_RETURN"
        or proof.get("disposition") != "EXCLUDE"
        or type(proof.get("admitted_item_count")) is not int
        or proof.get("admitted_item_count") != 0
        or not proof.get("authority_ref")
    ):
        raise ReportValidationError(
            "implementation policy unavailable comparison differs"
        )
    if attribution:
        _finite_number(
            value.get("account_return"), "implementation actual account return"
        )
        if value.get("categories") != []:
            raise ReportValidationError("unavailable comparison has causal categories")
    elif type(value.get("observations")) is not int or value.get("observations") != 0:
        raise ReportValidationError("unavailable comparison has observations")
    if expected_policy_evidence is not None and proof != dict(expected_policy_evidence):
        raise ReportValidationError(
            "implementation policy differs from selected source"
        )


def _implementation_reconciliation_contract(
    path: Path,
    *,
    portfolio_as_of: str,
    expected_policy_evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _load_json(path, "implementation reconciliation contract")
    if (
        payload.get("schema_version") != 1
        or payload.get("builder") != "portfolio-implementation-reconciliation"
        or payload.get("verification") != "VERIFIED"
    ):
        raise ReportValidationError("implementation reconciliation contract differs")
    contract_as_of = _date(
        payload.get("as_of_date"), "implementation reconciliation as_of_date"
    )
    portfolio_boundary = _date(portfolio_as_of, "portfolio as_of_date")
    if contract_as_of > portfolio_boundary:
        raise ReportValidationError("implementation reconciliation is future-dated")
    observed_boundary = min(contract_as_of, portfolio_boundary)
    rows = _objects(
        payload.get("strategies"), "implementation reconciliation strategies"
    )
    expected = {"grizzlybears-vtma", "p123-ai-factor", "tt-portfolio"}
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        strategy_id = _text(
            row.get("strategy_id"), "implementation reconciliation strategy_id"
        )
        if strategy_id in by_id:
            raise ReportValidationError(
                "implementation reconciliation duplicates a strategy"
            )
        if row.get("verification") != "VERIFIED":
            raise ReportValidationError(
                "implementation reconciliation strategy is not verified"
            )
        _validate_implementation_observed_intervals(
            row,
            boundary=observed_boundary,
            field_path=f"implementation reconciliation strategy {strategy_id}",
        )
        metrics = _object(
            row.get("return_reconciliation"),
            f"implementation return reconciliation {strategy_id}",
        )
        if metrics.get("status") == "UNAVAILABLE_POLICY_NOT_ADMITTED":
            if expected_policy_evidence is None:
                raise ReportValidationError(
                    "implementation policy source proof is required"
                )
            _validate_policy_unavailable_comparison(
                metrics,
                strategy_id=strategy_id,
                expected_policy_evidence=expected_policy_evidence,
            )
            actual = _object(
                row.get("account_live_return_summary"), "actual account returns"
            )
            if (
                actual.get("status") != "AVAILABLE"
                or type(actual.get("observations")) is not int
                or actual["observations"] < 2
            ):
                raise ReportValidationError(
                    "implementation actual account returns are invalid"
                )
            _date(actual.get("observed_start"), "actual account observed_start")
            _date(actual.get("observed_end"), "actual account observed_end")
            _finite_number(
                actual.get("cumulative_return"), "actual account cumulative return"
            )
            window = _object(
                row.get("return_window_comparison"), "implementation return window"
            )
            if window != {
                "status": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                "expanded_exact_common_gap": None,
                "expanded_minus_declared_gap": None,
                "prior_declared_overlap": None,
                "policy_evidence": dict(expected_policy_evidence),
            }:
                raise ReportValidationError(
                    "unavailable implementation return window differs"
                )
        else:
            observations = metrics.get("observations")
            if not isinstance(observations, int) or observations < 2:
                raise ReportValidationError(
                    "implementation reconciliation observations are invalid"
                )
            _date(metrics.get("observed_start"), "implementation observed_start")
            _date(metrics.get("observed_end"), "implementation observed_end")
        causes = _objects(row.get("root_causes"), "implementation root causes")
        if not causes:
            raise ReportValidationError(
                "implementation reconciliation lacks root-cause dispositions"
            )
        by_id[strategy_id] = dict(row)
    if set(by_id) != expected:
        raise ReportValidationError(
            "implementation reconciliation strategy boundary differs"
        )
    sha256, size = hash_file(path)
    return by_id, {
        "as_of_date": contract_as_of.isoformat(),
        "reconciliation_id": _text(
            payload.get("reconciliation_id"),
            "implementation reconciliation identity",
        ),
        "schema_version": payload["schema_version"],
        "sha256": sha256,
        "size_bytes": size,
        "strategy_count": len(by_id),
    }


def _implementation_return_attribution_contract(
    path: Path,
    *,
    portfolio_as_of: str,
    expected_policy_evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _load_json(path, "implementation return attribution contract")
    if (
        payload.get("schema_version") != 1
        or payload.get("builder") != "portfolio-implementation-return-attribution"
        or payload.get("verification")
        != "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES"
    ):
        raise ReportValidationError(
            "implementation return attribution contract differs"
        )
    contract_as_of = _date(
        payload.get("as_of_date"), "implementation return attribution as_of_date"
    )
    portfolio_boundary = _date(portfolio_as_of, "portfolio as_of_date")
    rows = _objects(
        payload.get("strategies"), "implementation return attribution strategies"
    )
    expected = {"grizzlybears-vtma", "p123-ai-factor", "tt-portfolio"}
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        strategy_id = _text(
            row.get("strategy_id"), "implementation return attribution strategy_id"
        )
        if strategy_id in by_id:
            raise ReportValidationError(
                "implementation return attribution duplicates a strategy"
            )
        verification = _text(
            row.get("verification"),
            f"implementation return attribution verification {strategy_id}",
        )
        if verification not in {
            "PARTIAL_EXACT_ACTUAL_PNL_PARTIAL_COUNTERFACTUAL_GAP",
            "VERIFIED_WITH_DECLARED_PROXY",
            "VERIFIED_WITH_DECLARED_PUBLIC_PRICE_PROXY",
        }:
            raise ReportValidationError(
                "implementation return attribution strategy is not accepted"
            )
        observed_start = _date(
            row.get("observed_start"),
            f"implementation return attribution observed_start {strategy_id}",
        )
        observed_end = _date(
            row.get("observed_end"),
            f"implementation return attribution observed_end {strategy_id}",
        )
        if observed_end < observed_start or observed_end > portfolio_boundary:
            raise ReportValidationError(
                "implementation return attribution observation boundary differs"
            )
        if contract_as_of < observed_end:
            raise ReportValidationError(
                "implementation return attribution predates its evidence"
            )
        bridge = _object(
            row.get("bridge"), f"implementation return attribution bridge {strategy_id}"
        )
        if bridge.get("status") == "UNAVAILABLE_POLICY_NOT_ADMITTED":
            if expected_policy_evidence is None:
                raise ReportValidationError(
                    "implementation policy source proof is required"
                )
            _validate_policy_unavailable_comparison(
                bridge,
                strategy_id=strategy_id,
                attribution=True,
                expected_policy_evidence=expected_policy_evidence,
            )
            for field in ("execution_shortfall", "effective_account_model_mix"):
                if row.get(field) != {
                    "status": "UNAVAILABLE_POLICY_NOT_ADMITTED",
                    "policy_evidence": dict(expected_policy_evidence),
                }:
                    raise ReportValidationError(
                        "unavailable model counterfactual differs"
                    )
            actual = _object(
                row.get("actual_return_contribution_by_trade_provenance"),
                "implementation actual return attribution",
            )
            observations = actual.get("daily_observations")
            contributions = _object(
                actual.get("contributions_pp"), "actual return contributions"
            )
            if type(observations) is not int or observations < 2 or not contributions:
                raise ReportValidationError(
                    "implementation actual attribution is absent"
                )
            actual_total = sum(
                _finite_number(value, "actual return contribution")
                for value in contributions.values()
            )
            if not math.isclose(
                actual_total,
                _finite_number(bridge.get("account_return"), "actual account return")
                * 100.0,
                abs_tol=1e-8,
            ):
                raise ReportValidationError(
                    "implementation actual attribution does not reconcile"
                )
            by_id[strategy_id] = dict(row)
            continue
        model_return = _finite_number(
            bridge.get("model_return"),
            f"implementation return attribution model return {strategy_id}",
        )
        account_return = _finite_number(
            bridge.get("account_return"),
            f"implementation return attribution account return {strategy_id}",
        )
        gap_pp = _finite_number(
            bridge.get("gap_pp"),
            f"implementation return attribution gap {strategy_id}",
        )
        categories = _objects(
            bridge.get("categories"),
            f"implementation return attribution categories {strategy_id}",
        )
        if not categories:
            raise ReportValidationError(
                "implementation return attribution categories are empty"
            )
        category_total = sum(
            _finite_number(
                category.get("effect_pp"),
                f"implementation return attribution effect {strategy_id}",
            )
            for category in categories
        )
        if not math.isclose(
            gap_pp, (account_return - model_return) * 100.0, abs_tol=1e-10
        ) or not math.isclose(gap_pp, category_total, abs_tol=1e-8):
            raise ReportValidationError(
                "implementation return attribution bridge does not reconcile"
            )
        by_id[strategy_id] = dict(row)
    if set(by_id) != expected:
        raise ReportValidationError(
            "implementation return attribution strategy boundary differs"
        )
    sha256, size = hash_file(path)
    return by_id, {
        "as_of_date": contract_as_of.isoformat(),
        "attribution_id": _text(
            payload.get("attribution_id"),
            "implementation return attribution identity",
        ),
        "schema_version": payload["schema_version"],
        "sha256": sha256,
        "size_bytes": size,
        "strategy_count": len(by_id),
    }


def _strategies(
    catalog: Mapping[str, Any],
    performance: Sequence[Mapping[str, Any]],
    history_contract: Mapping[str, Mapping[str, Any]] | None = None,
    implementation_contract: Mapping[str, Mapping[str, Any]] | None = None,
    implementation_return_attribution_contract: Mapping[str, Mapping[str, Any]]
    | None = None,
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
    strategy_provider_evidence: Sequence[Mapping[str, Any]] | None = None,
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
        str(item["strategy_id"]): item
        for item in (
            strategy_provider_evidence
            if strategy_provider_evidence is not None
            else _strategy_provider_evidence(catalog, strategy_product_controls)
        )
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
        product_control = (
            strategy_product_controls.get(strategy_id)
            if strategy_product_controls is not None
            else None
        )
        product_required = (
            product_control is None
            or product_control.get("product_requirement") == "REQUIRED"
        )
        historical_only = bool(
            product_control
            and product_control.get("lifecycle_status") == "DEPRECATED_HISTORY_ONLY"
        )
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
            product_required
            and provider_row
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
        historical_account = account_path if historical_only and account_path else {}
        legacy_account = metrics if provider_row is None and live_row else {}
        account_authority = (
            "CANONICAL_STRATEGY_RETURNS"
            if canonical_account
            else "HISTORICAL_ACCOUNT_LIVE_EVIDENCE"
            if historical_account
            else "AUDIT_STRATEGY_LIVE_METRICS"
            if legacy_account
            else None
        )
        account_metrics = canonical_account or historical_account or legacy_account
        strategy_path = canonical_account or historical_account or provider_path
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
        compatibility_status = version.get("compatibility_status", "UNRESOLVED")
        compatibility_reason = version.get(
            "compatibility_reason",
            "Compatibility is unavailable without an effective-dated registry row.",
        )
        history = (
            dict(history_contract.get(str(strategy.get("name")), {}))
            if history_contract is not None
            else {}
        )
        implementation = (
            dict(implementation_contract.get(strategy_id, {}))
            if implementation_contract is not None
            else {}
        )
        implementation_return_attribution = (
            dict(implementation_return_attribution_contract.get(strategy_id, {}))
            if implementation_return_attribution_contract is not None
            else {}
        )
        history_observations = history.get("backtest_observations")
        history_accepted = bool(
            history
            and str(history.get("history_status", "")).startswith("ACCEPTED_")
            and isinstance(history_observations, int)
            and history_observations > 0
        )
        if history_contract is not None:
            history_compatibility_status = (
                "ACCEPTED_FOR_ECONOMIC_INFERENCE"
                if history_accepted
                else "UNAVAILABLE_NO_ACCEPTED_HISTORY"
            )
            history_compatibility_reason = (
                _text(
                    history.get("compatibility_basis"),
                    f"history compatibility basis {strategy_id}",
                )
                if history_accepted
                else str(
                    history.get("history_disposition_reason")
                    or history.get("status")
                    or history.get("history_status")
                )
            )
            live_plus_backtest_status = (
                "ELIGIBLE_ACCEPTED_HISTORY"
                if history_accepted
                else "BLOCKED_NO_ACCEPTED_HISTORY"
            )
            live_plus_backtest_reason = (
                "History is accepted for economic inference under "
                f"{history_compatibility_reason}; live implementation reconciliation "
                "remains a separate control."
                if history_accepted
                else (
                    "No accepted strategy history is routed: "
                    f"{str(history_compatibility_reason).rstrip('.')}."
                )
            )
        else:
            history_compatibility_status = None
            history_compatibility_reason = None
            live_plus_backtest_status = (
                "ELIGIBLE_PROVEN_COMPATIBLE"
                if compatibility_status == "PROVEN_COMPATIBLE"
                else "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
            )
            live_plus_backtest_reason = (
                "Effective-dated registry marks the current strategy version PROVEN_COMPATIBLE."
                if compatibility_status == "PROVEN_COMPATIBLE"
                else f"{compatibility_status}: {compatibility_reason}"
            )
        available_segments, total_segments = performance_count[strategy_id]
        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": strategy.get("name"),
                "economic_family": strategy.get("economic_family"),
                "lifecycle_status": (
                    product_control.get("lifecycle_status")
                    if product_control is not None
                    else None
                ),
                "product_requirement": (
                    product_control.get("product_requirement")
                    if product_control is not None
                    else None
                ),
                "funded_membership_status": (
                    product_control.get("funded_membership_status")
                    if product_control is not None
                    else None
                ),
                "current_value_usd": (
                    decimal_text(_amount(allocation, "current_value"))
                    if product_required
                    else None
                ),
                "systematic_weight": (
                    allocation.get("systematic_weight") if product_required else None
                ),
                "available_twr_segments": available_segments,
                "total_assignment_segments": total_segments,
                "account_live_status": (
                    "HISTORICAL_ONLY"
                    if historical_account
                    else "AVAILABLE"
                    if account_metrics
                    else "UNAVAILABLE"
                ),
                "account_live_cagr": account_cagr,
                "account_live_cagr_status": (
                    "AVAILABLE_DERIVED_SAME_CANONICAL_PATH"
                    if canonical_account and account_cagr is not None
                    else "AVAILABLE_SOURCE_REPORTED_SAME_AUDIT_VINTAGE"
                    if legacy_account and account_cagr is not None
                    else "UNAVAILABLE_HISTORICAL_ONLY"
                    if historical_account
                    else "UNAVAILABLE"
                ),
                "account_live_cumulative_return": cumulative_return,
                "account_live_cumulative_return_label": account_metrics.get(
                    "cumulative_return_label"
                ),
                "account_live_max_drawdown": account_metrics.get("maximum_drawdown")
                or account_metrics.get("max_drawdown"),
                "account_live_path_metrics_status": account_metrics.get(
                    "path_dependent_metrics_status"
                ),
                "account_live_path_metrics_reason": account_metrics.get(
                    "path_dependent_metrics_reason"
                ),
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
                    account_path.get("generation_id")
                    if canonical_account or historical_account
                    else None
                ),
                "account_live_source_generation_as_of_date": (
                    account_path.get("generation_as_of_date")
                    if canonical_account or historical_account
                    else live_source.get("as_of_date")
                ),
                "account_live_source_provider": (
                    account_path.get("provider_id")
                    if canonical_account or historical_account
                    else None
                ),
                "account_live_source_sha256": (
                    None if canonical_account else live_source.get("sha256")
                ),
                "nav_status": (
                    "HISTORICAL_ONLY"
                    if historical_account
                    else "AVAILABLE"
                    if admitted_account_nav or admitted_provider_nav
                    else coverage_row.get("status", "UNAVAILABLE")
                ),
                "nav_observed_start": strategy_path.get("observed_start"),
                "nav_observed_end": strategy_path.get("observed_end"),
                "nav_return_observations": strategy_path.get("return_observations"),
                "hwm_current_drawdown": strategy_path.get("current_drawdown"),
                "hwm_max_drawdown": strategy_path.get("maximum_drawdown"),
                "hwm_path_metrics_status": strategy_path.get(
                    "path_dependent_metrics_status"
                ),
                "hwm_path_metrics_reason": strategy_path.get(
                    "path_dependent_metrics_reason"
                ),
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
                "version_compatibility_status": compatibility_status,
                "version_compatibility_reason": compatibility_reason,
                "history_compatibility_status": history_compatibility_status,
                "history_compatibility_reason": history_compatibility_reason,
                "history_version_ref": history.get("strategy_version"),
                "history_observations": history_observations,
                "history_observed_start": history.get("backtest_observed_start"),
                "history_observed_end": history.get("backtest_observed_end"),
                "implementation_lineage": history.get("implementation_lineage"),
                "implementation_reconciliation_status": history.get(
                    "implementation_reconciliation_status"
                )
                if not implementation
                else (
                    "UNAVAILABLE_POLICY_NOT_ADMITTED"
                    if implementation_return_attribution.get("bridge", {}).get("status")
                    == "UNAVAILABLE_POLICY_NOT_ADMITTED"
                    else "VERIFIED_CAUSAL_RETURN_ATTRIBUTION"
                    if implementation_return_attribution
                    else "VERIFIED_TRADE_AND_RETURN_DECONSTRUCTION"
                ),
                "implementation_reconciliation": implementation or None,
                "implementation_return_attribution": (
                    implementation_return_attribution or None
                ),
                "live_plus_backtest_status": live_plus_backtest_status,
                "live_plus_backtest_reason": live_plus_backtest_reason,
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


def _transactions_and_flows(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
    current_account_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    securities = {
        str(item["security_id"]): item
        for item in _objects(catalog.get("securities"), "securities")
    }
    rows: list[dict[str, Any]] = []
    transactions = _current_product_fact_rows(
        _objects(catalog.get("transactions"), "transactions"),
        strategy_product_controls,
        current_account_ids,
        "transactions",
    )
    for record in transactions:
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
    flows = _current_product_fact_rows(
        _objects(catalog.get("flows"), "flows"),
        strategy_product_controls,
        current_account_ids,
        "flows",
    )
    for record in flows:
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


def _positions(
    catalog: Mapping[str, Any], current_account_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    rows = _objects(catalog.get("positions"), "positions")
    if current_account_ids is not None:
        rows = [
            row
            for row in rows
            if isinstance(row.get("account_id"), str)
            and row["account_id"] in current_account_ids
        ]
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


def _flow_reconciliation(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
    current_account_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    flows = _current_product_fact_rows(
        _objects(catalog.get("flows"), "flows"),
        strategy_product_controls,
        current_account_ids,
        "flows",
    )
    for flow in flows:
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


def _allocations(
    catalog: Mapping[str, Any],
    strategy_product_controls: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for item in _objects(catalog.get("allocations"), "allocations"):
        strategy_id = item.get("strategy_id")
        if item.get("scope") == "SYSTEMATIC" and strategy_product_controls is not None:
            if (
                not isinstance(strategy_id, str)
                or strategy_id not in strategy_product_controls
            ):
                raise ReportValidationError(
                    "systematic allocation must reference a known product strategy"
                )
            if not _is_current_product_strategy(strategy_id, strategy_product_controls):
                continue
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
    rows = sorted(
        rows,
        key=lambda item: (
            str(item["as_of_date"]),
            str(item["scope"]),
            str(item["strategy_id"] or ""),
        ),
    )
    if strategy_product_controls is None:
        return rows
    systematic_rows = [row for row in rows if row.get("scope") == "SYSTEMATIC"]
    required_ids = {
        strategy_id
        for strategy_id, control in strategy_product_controls.items()
        if control.get("product_requirement") == "REQUIRED"
    }
    if (
        len(systematic_rows) != len(required_ids)
        or {row.get("strategy_id") for row in systematic_rows} != required_ids
    ):
        raise ReportValidationError("current systematic allocation perimeter differs")
    systematic_total = sum(
        (
            _decimal(row["current_value_usd"], "allocation.current_value_usd")
            for row in systematic_rows
        ),
        Decimal("0"),
    )
    if not systematic_rows or systematic_total <= 0:
        raise ReportValidationError(
            "current systematic allocations must have positive value"
        )
    with localcontext() as context:
        context.prec = 34
        assigned = Decimal("0")
        for row in systematic_rows[:-1]:
            weight = (
                _decimal(row["current_value_usd"], "allocation.current_value_usd")
                / systematic_total
            )
            row["systematic_weight"] = decimal_text(weight)
            assigned += weight
        systematic_rows[-1]["systematic_weight"] = decimal_text(Decimal("1") - assigned)
    return rows


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


def _provider_ledger_identity(root: Path, generation_id: str) -> dict[str, object]:
    try:
        manifest = read_generation(root, generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError(
            "freshness provider ledger generation is not accepted"
        ) from exc
    expected = next(
        (
            artifact
            for artifact in manifest.artifacts
            if artifact.artifact_id == "normalized/provider-ledger.json"
        ),
        None,
    )
    if expected is None:
        raise ReportValidationError("freshness provider ledger catalog is absent")
    generation = root / "generations" / manifest.generation_id
    catalog_path = generation / "provider-ledger.json"
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ReportValidationError("freshness provider ledger catalog is unavailable")
    catalog_sha256, catalog_size = hash_file(catalog_path)
    if (catalog_sha256, catalog_size) != (expected.sha256, expected.size_bytes):
        raise ReportValidationError(
            "freshness provider ledger catalog differs from its manifest"
        )
    manifest_sha256, manifest_size = hash_file(generation / "generation.json")
    return {
        "as_of_date": manifest.as_of_date.isoformat(),
        "catalog_sha256": catalog_sha256,
        "catalog_size_bytes": catalog_size,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest_sha256,
        "manifest_size_bytes": manifest_size,
    }


def _baseline_provider_ledger_identity(
    catalog: Mapping[str, Any],
) -> dict[str, str]:
    references = [
        item
        for item in _objects(catalog.get("source_artifacts"), "source_artifacts")
        if item.get("source_key") == "provider-ledger-generation"
        and item.get("schema_name")
        in {"generation-manifest", "provider-ledger-catalog"}
    ]
    by_schema = {
        _text(item.get("schema_name"), "provider-ledger schema_name"): _text(
            item.get("sha256"), "provider-ledger sha256"
        )
        for item in references
    }
    if len(references) != 2 or set(by_schema) != {
        "generation-manifest",
        "provider-ledger-catalog",
    }:
        raise ReportValidationError(
            "baseline provider-ledger identity is missing or ambiguous"
        )
    return by_schema


def _freshness_assessments(
    path: Path,
    portfolio_as_of: date,
    provider_ledger_root: Path | None = None,
    baseline_catalog: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], date, str]:
    payload = _load_json(path, "freshness health")
    schema_version = payload.get("assessment_schema_version")
    expected = {
        "assessment_schema_version",
        "as_of_date",
        "assessments",
        "policy" if schema_version == 2 else "policies",
    }
    accepted = {frozenset(expected)}
    if schema_version == 3:
        accepted.add(frozenset(expected | {"provider_ledger"}))
    if schema_version not in {2, 3} or frozenset(payload) not in accepted:
        raise ReportValidationError("freshness health contract differs")
    as_of = _date(payload.get("as_of_date"), "freshness health as_of_date")
    if as_of < portfolio_as_of:
        raise ReportValidationError("freshness health predates the portfolio")
    if "provider_ledger" in payload:
        ledger = _object(payload.get("provider_ledger"), "freshness provider ledger")
        if set(ledger) != {
            "as_of_date",
            "catalog_sha256",
            "catalog_size_bytes",
            "generation_id",
            "manifest_sha256",
            "manifest_size_bytes",
        }:
            raise ReportValidationError("freshness provider ledger keys differ")
        ledger_as_of = _date(
            ledger.get("as_of_date"), "freshness provider ledger as_of_date"
        )
        if ledger_as_of > as_of:
            raise ReportValidationError(
                "freshness provider ledger postdates the evaluation"
            )
        for field in ("catalog_sha256", "generation_id", "manifest_sha256"):
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                _text(ledger.get(field), f"freshness provider ledger {field}"),
            ):
                raise ReportValidationError(
                    f"freshness provider ledger {field} differs"
                )
        for field in ("catalog_size_bytes", "manifest_size_bytes"):
            value = ledger.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ReportValidationError(
                    f"freshness provider ledger {field} differs"
                )
        if provider_ledger_root is None:
            raise ReportValidationError(
                "freshness provider ledger root is required for identity binding"
            )
        observed_identity = _provider_ledger_identity(
            provider_ledger_root.resolve(), str(ledger["generation_id"])
        )
        if dict(ledger) != observed_identity:
            raise ReportValidationError(
                "freshness provider ledger identity differs from the selected ledger"
            )
        if baseline_catalog is not None:
            baseline_identity = _baseline_provider_ledger_identity(baseline_catalog)
            if (
                ledger["catalog_sha256"] != baseline_identity["provider-ledger-catalog"]
                or ledger["manifest_sha256"] != baseline_identity["generation-manifest"]
            ):
                raise ReportValidationError(
                    "freshness provider ledger identity differs from the baseline"
                )
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


def _hwm_policy(
    evaluation_date: date, policy_path: Path = HWM_POLICY
) -> tuple[dict[str, Any], str]:
    policy = _load_json(policy_path, "HWM policy")
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
    sha256, _size = hash_file(policy_path)
    return policy, sha256


_HWM_NUMERICAL_REASONS: Final = (
    "NO_HWM_INPUTS",
    "HWM_INPUTS_INVALID",
    "INCOMPLETE_PERIODS",
    "NO_AVAILABLE_PERIODS",
    "UNRESOLVED_RETURN_REFERENCE",
    "DUPLICATE_RETURN_REFERENCE",
    "POLICY_EXCLUSION_INVALID",
    "ENDPOINT_NOT_RECONCILED",
    "MISSING_CONSUMPTION_CONTEXT",
    "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
)

# Shared consumption-boundary validator codes → report reason codes.
_HWM_CONTRACT_REASON_MAP: Final = {
    "POLICY_EXCLUSION_INVALID": "POLICY_EXCLUSION_INVALID",
    "ENDPOINT_NOT_RECONCILED": "ENDPOINT_NOT_RECONCILED",
    "ENDPOINT_DELTA_MISMATCH": "ENDPOINT_NOT_RECONCILED",
    "KEYSPACE_DIFFERS": "INCOMPLETE_PERIODS",
    "UNRESOLVED_RETURN_REFERENCE": "UNRESOLVED_RETURN_REFERENCE",
    "DUPLICATE_RETURN_REFERENCE": "DUPLICATE_RETURN_REFERENCE",
}


def _hwm_numerical_from_envelope(
    catalog: Mapping[str, Any],
    policy_path: Path = HWM_POLICY,
) -> dict[str, Any]:
    """Compute the numerical full-systematic HWM from the governed envelope.

    The ``hwm_inputs`` envelope is the only input: period capital, return
    references, and dispositions come from its period rows; daily returns
    resolve through the hash-bound ``strategy_returns`` crosswalk.  A missing
    envelope or any internal defect yields ``UNAVAILABLE`` with a structured
    reason — never an error and never a fallback to snapshots or observation
    grids.

    Crosswalk binding: every AVAILABLE period's ``return_source_record_id``
    must match EXACTLY ONE ``strategy_returns`` row on reference
    (``account_reconciliation_sha256``), ``strategy_id``, ``date``, and
    ``evidence_layer == "ACCOUNT_LIVE"``, and no other row may carry the
    same reference at all — a duplicate or mismatched row fails closed
    instead of overwriting the proven row.

    Composite rule (per the kernel contract): a day carries no period only
    when every perimeter member is ``EXPECTED_NON_TRADING`` (or
    policy-excluded); on any other day every member must be AVAILABLE (a
    single UNAVAILABLE or NOT_ADMITTED member blocks the whole composite —
    no survivors), and the period return is the
    beginning-capital-weighted member return.
    """

    def _unavailable(reason: str) -> dict[str, Any]:
        assert reason in _HWM_NUMERICAL_REASONS, f"unknown reason code: {reason}"
        return {"hwm_numerical_status": "UNAVAILABLE", "hwm_numerical_reason": reason}

    if any(
        receipt["span_return_receipts"]
        for receipt in _epoch_hpr_receipts(
            catalog, calculation_cutoff_date=None
        ).values()
    ):
        return _unavailable("MISSING_DAILY_POINT_CAN_HIDE_PEAK")

    raw = catalog.get("hwm_inputs")
    if raw is None:
        return _unavailable("NO_HWM_INPUTS")
    try:
        envelope = _object(raw, "hwm_inputs")
        periods = _objects(envelope.get("periods"), "hwm_inputs.periods")
    except ReportValidationError:
        return _unavailable("HWM_INPUTS_INVALID")

    # Consumption boundary: re-enforce the semantic admission contract the
    # baseline builder applies at admission — a hash-bound generation can
    # still carry a semantically invalid envelope.  The shared validator in
    # the ledger package is the single authority (same checks as the panel's
    # second trust boundary).
    artifact_roles: dict[str, tuple[str, str]] = {}
    for link in _objects(catalog.get("source_artifacts"), "source_artifacts"):
        artifact_id = _text(link.get("artifact_id"), "source_artifact.artifact_id")
        artifact_roles[artifact_id] = (
            _text(link.get("schema_name"), "source_artifact.schema_name"),
            _text(link.get("sha256"), "source_artifact.sha256"),
        )
    # Consumption-boundary full context: the builder embeds the hash-verified
    # acceptance record and allocations snapshot into the catalog so consumers
    # can enforce the anchor-content cross-check and endpoint recomputation
    # without access to the governed source files.  Missing context is a
    # fail-closed condition — the consumer never falls back to declaration-only
    # mode.  Both contexts are validated before ANY use of catalog allocations
    # (including perimeter derivation below).
    hwm_acceptance_record = catalog.get("hwm_acceptance_record")
    hwm_allocations = catalog.get("allocations")
    if not isinstance(hwm_acceptance_record, dict) or not isinstance(
        hwm_allocations, list
    ):
        return _unavailable("MISSING_CONSUMPTION_CONTEXT")

    perimeter = sorted(
        {
            _text(item["strategy_id"], "allocation.strategy_id")
            for item in _objects(hwm_allocations, "allocations")
            if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
        }
    )

    try:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=_date(catalog.get("as_of_date"), "catalog.as_of_date"),
            perimeter=perimeter,
            crosswalk=_objects(catalog.get("strategy_returns", []), "strategy_returns"),
            artifact_roles=artifact_roles,
            policy_exclusions=hwm_policy_exclusions(
                _load_json(policy_path, "HWM policy")
            ),
            allocations=_objects(hwm_allocations, "allocations"),
            acceptance_record=hwm_acceptance_record,
        )
    except HwmInputsContractError as exc:
        return _unavailable(
            _HWM_CONTRACT_REASON_MAP.get(exc.code, "HWM_INPUTS_INVALID")
        )

    # Hash-bound return crosswalk admitted by the baseline catalog, indexed
    # as a multimap so duplicate references are detected, never overwritten.
    crosswalk: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in _objects(catalog.get("strategy_returns", []), "strategy_returns"):
        reference = row.get("account_reconciliation_sha256")
        if reference is None:
            continue
        crosswalk[str(reference)].append(row)

    by_date: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in periods:
        strategy_id = row.get("strategy_id")
        day = row.get("date")
        disposition = row.get("disposition")
        if (
            not isinstance(strategy_id, str)
            or not strategy_id
            or not isinstance(day, str)
            or not day
            or not isinstance(disposition, str)
        ):
            return _unavailable("HWM_INPUTS_INVALID")
        if strategy_id in by_date[day]:
            return _unavailable("HWM_INPUTS_INVALID")
        by_date[day][strategy_id] = row

    composite_returns: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        for day in sorted(by_date):
            members = [
                row
                for row in by_date[day].values()
                if row["disposition"] != "EXCLUDED_BY_POLICY"
            ]
            # A day is legitimately empty only when every perimeter member is
            # expected not to trade (policy-excluded members are already
            # removed from `members`).  A required day whose members are all
            # UNAVAILABLE or NOT_ADMITTED is a coverage gap, never a
            # skippable period.
            if all(row["disposition"] == "EXPECTED_NON_TRADING" for row in members):
                continue
            if any(row["disposition"] != "AVAILABLE" for row in members):
                return _unavailable("INCOMPLETE_PERIODS")
            weighted_return = Decimal(0)
            total_capital = Decimal(0)
            for member in sorted(members, key=lambda item: str(item["strategy_id"])):
                try:
                    capital = _decimal(
                        member.get("beginning_capital_usd"),
                        "hwm_inputs.beginning_capital_usd",
                    )
                except ReportValidationError:
                    return _unavailable("HWM_INPUTS_INVALID")
                reference = member.get("return_source_record_id")
                if not isinstance(reference, str):
                    return _unavailable("UNRESOLVED_RETURN_REFERENCE")
                candidates = crosswalk.get(reference, [])
                if len(candidates) != 1:
                    # Zero rows: unresolved; more than one row carrying the
                    # reference: a duplicate must never overwrite the proven
                    # row — both fail closed.
                    return _unavailable(
                        "DUPLICATE_RETURN_REFERENCE"
                        if candidates
                        else "UNRESOLVED_RETURN_REFERENCE"
                    )
                match = candidates[0]
                if (
                    str(match.get("strategy_id") or "") != str(member["strategy_id"])
                    or str(match.get("date") or "") != day
                    or match.get("evidence_layer") != "ACCOUNT_LIVE"
                ):
                    return _unavailable("UNRESOLVED_RETURN_REFERENCE")
                weighted_return += capital * _decimal(
                    match.get("return"), "strategy_returns.return"
                )
                total_capital += capital
            if total_capital <= 0:
                return _unavailable("INCOMPLETE_PERIODS")
            composite_returns.append(weighted_return / total_capital)

    if not composite_returns:
        return _unavailable("NO_AVAILABLE_PERIODS")
    result = compute_full_systematic_hwm_index(
        composite_returns,
        full_systematic_coverage=True,
    )
    if result.get("status") != "AVAILABLE":
        return _unavailable("INCOMPLETE_PERIODS")
    return {
        "hwm_index": decimal_text(result["index_series"][-1]),
        "hwm_current_drawdown": decimal_text(result["current_drawdown"]),
        "hwm_maximum_drawdown": decimal_text(result["maximum_drawdown"]),
        "hwm_numerical_status": "AVAILABLE",
    }


def _report_portable_hwm_proof(
    receipt: Mapping[str, Any],
    input_generation_bytes: bytes,
    calendar_bytes: bytes,
) -> dict[str, Any]:
    """Project a verified PORT-HWM-4 portable proof without upgrading authority."""

    try:
        policy_bytes = _PORTABLE_HWM_POLICY.read_bytes()
        product_contract_bytes = _HWM_POLICY_PRODUCT_AUTHORITY_CONTRACT.read_bytes()
        performance_policy_bytes = _PORTABLE_HWM_PERFORMANCE_POLICY.read_bytes()
        legacy_policy_bytes = HWM_POLICY.read_bytes()
        accepted = validate_as_held_hwm_receipt_generation_binding(
            receipt,
            input_generation_bytes,
            policy_bytes=policy_bytes,
            product_contract_bytes=product_contract_bytes,
            performance_policy_bytes=performance_policy_bytes,
            legacy_policy_bytes=legacy_policy_bytes,
            calendar_bytes=calendar_bytes,
        )
    except HwmCoverageError as exc:
        raise ReportValidationError(f"portable HWM proof is invalid: {exc}") from exc
    return {
        "schema_version": 1,
        "status": accepted["status"],
        "financial_acceptance": "NOT_FINANCIAL_ACCEPTANCE",
        "assurance_profile": accepted["assurance_profile"],
        "policy_id": accepted["policy_id"],
        "policy_sha256": accepted["policy_sha256"],
        "policy_semantic_digest": accepted["policy_semantic_digest"],
        "receipt_sha256": accepted["receipt_sha256"],
        "input_authority_class": accepted["input_authority_class"],
        "calendar_authority_status": accepted["calendar_authority_status"],
        "input_generation_sha256": accepted["input_generation_sha256"],
        "input_sha256": accepted["input_sha256"],
        "return_basis": accepted["return_basis"],
        "evaluated_on": accepted["evaluated_on"],
        "complete_through": accepted["complete_through"],
        "calculation_age_days": accepted["calculation_age_days"],
        "maximum_age_days": accepted["maximum_age_days"],
        "age_status": accepted["age_status"],
        "warnings": accepted["warnings"],
        "current_index": accepted["current_index"],
        "current_hwm": accepted["current_hwm"],
        "current_drawdown": accepted["current_drawdown"],
        "maximum_drawdown": accepted["maximum_drawdown"],
        "index_path": accepted["index_path"],
    }


def _report_governed_hwm_proof(
    result_receipt: Mapping[str, Any],
    input_generation_bytes: bytes,
    calendar_bytes: bytes,
    input_receipt_bytes: bytes,
    *,
    product_contract_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Verify a governed PORT-HWM-4 result and project it into report data.

    Mirrors ``_report_portable_hwm_proof`` for the ACCEPTED_GOVERNED
    authority class (#324/#370): verification is delegated to the kernel's
    own ``verify_governed_hwm_result`` — no reimplementation here.
    """

    input_contract = (
        _HWM_POLICY_PRODUCT_AUTHORITY_CONTRACT.read_bytes()
        if product_contract_bytes is None
        else product_contract_bytes
    )
    try:
        input_policy = governed_hwm_input_policy(
            _PORTABLE_HWM_POLICY.read_bytes(),
            input_contract,
            result_receipt.get("policy_sha256", ""),
        )
        accepted = verify_governed_hwm_result(
            result_receipt,
            input_generation_bytes,
            calendar_bytes=calendar_bytes,
            input_receipt_bytes=input_receipt_bytes,
            policy_bytes=input_policy,
            product_contract_bytes=input_contract,
            performance_policy_bytes=_PORTABLE_HWM_PERFORMANCE_POLICY.read_bytes(),
            legacy_policy_bytes=HWM_POLICY.read_bytes(),
        )
    except (HwmCoverageError, CapitalValidationError) as exc:
        raise ReportValidationError(f"governed HWM result is invalid: {exc}") from exc
    return {
        "schema_version": 1,
        "status": accepted["status"],
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "assurance_profile": accepted["assurance_profile"],
        "policy_id": accepted["policy_id"],
        "policy_sha256": accepted["policy_sha256"],
        "calendar_sha256": accepted["calendar_sha256"],
        "input_generation_sha256": accepted["input_generation_sha256"],
        "upstream_receipt_sha256": accepted["upstream_receipt_sha256"],
        "receipt_sha256": accepted["receipt_sha256"],
        "input_authority_class": accepted["input_authority_class"],
        "return_basis": accepted["return_basis"],
        "evaluated_on": accepted["evaluated_on"],
        "complete_through": accepted["complete_through"],
        "calculation_age_days": accepted["calculation_age_days"],
        "maximum_age_days": accepted["maximum_age_days"],
        "age_status": accepted["age_status"],
        "warnings": accepted["warnings"],
        "current_index": decimal_text(accepted["current_index"]),
        "current_hwm": decimal_text(accepted["current_hwm"]),
        "current_drawdown": decimal_text(accepted["current_drawdown"]),
        "maximum_drawdown": decimal_text(accepted["maximum_drawdown"]),
        "index_path": [
            {
                "as_of": row["as_of"],
                "index": decimal_text(row["index"]),
                "hwm": decimal_text(row["hwm"]),
                "drawdown": decimal_text(row["drawdown"]),
            }
            for row in accepted["index_path"]
        ],
    }


def _governed_hwm_summary_rows(proof: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Governed HWM summary rows for the portfolio summary table."""

    return [
        {
            "metric": "PORT-HWM-4 governed status",
            "status": "GOVERNED_REAL_DATA",
            "unit": "status",
            "value": proof["age_status"],
        },
        {
            "metric": "PORT-HWM-4 governed index",
            "status": "GOVERNED_REAL_DATA",
            "unit": "index",
            "value": proof["current_index"],
        },
        {
            "metric": "PORT-HWM-4 governed HWM",
            "status": "GOVERNED_REAL_DATA",
            "unit": "index",
            "value": proof["current_hwm"],
        },
        {
            "metric": "PORT-HWM-4 governed current drawdown",
            "status": "GOVERNED_REAL_DATA",
            "unit": "ratio",
            "value": proof["current_drawdown"],
        },
        {
            "metric": "PORT-HWM-4 governed maximum drawdown",
            "status": "GOVERNED_REAL_DATA",
            "unit": "ratio",
            "value": proof["maximum_drawdown"],
        },
    ]


def _portable_hwm_summary_rows(proof: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "metric": "PORT-HWM-4 proof status",
            "status": "DEVELOPMENT_NOT_FINANCIAL_ACCEPTANCE",
            "unit": "status",
            "value": "DEVELOPMENT_NOT_FINANCIAL_ACCEPTANCE",
        },
        {
            "metric": "PORT-HWM-4 proof index",
            "status": "PORTABLE_SYNTHETIC_PROOF",
            "unit": "index",
            "value": proof["current_index"],
        },
        {
            "metric": "PORT-HWM-4 proof HWM",
            "status": "PORTABLE_SYNTHETIC_PROOF",
            "unit": "index",
            "value": proof["current_hwm"],
        },
        {
            "metric": "PORT-HWM-4 proof current drawdown",
            "status": "PORTABLE_SYNTHETIC_PROOF",
            "unit": "ratio",
            "value": proof["current_drawdown"],
        },
        {
            "metric": "PORT-HWM-4 proof maximum drawdown",
            "status": "PORTABLE_SYNTHETIC_PROOF",
            "unit": "ratio",
            "value": proof["maximum_drawdown"],
        },
        *[
            {
                "metric": f"PORT-HWM-4 proof path {point['as_of']}",
                "status": "PORTABLE_SYNTHETIC_PROOF",
                "unit": "index",
                "value": point["index"],
            }
            for point in proof["index_path"]
        ],
        {
            "metric": "PORT-HWM-4 proof complete through",
            "status": proof["age_status"],
            "unit": "date",
            "value": proof["complete_through"],
        },
    ]


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


def _calculation_catalog_through(
    catalog: Mapping[str, Any], cutoff_text: str
) -> dict[str, Any]:
    """Project date-bearing calculation rows without rewriting retained lineage."""

    cutoff = _date(cutoff_text, "calculation cutoff date")
    projected = dict(catalog)
    if "assignments" in catalog:
        assignments = _objects(catalog.get("assignments"), "assignments")
        for assignment in assignments:
            for field_name in ("effective_start", "effective_end"):
                value = assignment.get(field_name)
                if (
                    value is not None
                    and _date(value, f"assignment.{field_name}") > cutoff
                ):
                    raise ReportValidationError(
                        f"assignment {field_name} exceeds cutoff"
                    )
        projected["assignments"] = [dict(assignment) for assignment in assignments]
    dated_collections = {
        "calendar_coverage": "date",
        "flows": "event_date",
        "nav_observations": "as_of_date",
        "positions": "as_of_date",
        "provider_strategy_returns": "date",
        "strategy_returns": "date",
        "transactions": "event_date",
    }
    for field_name, date_field in dated_collections.items():
        if field_name not in catalog:
            continue
        projected[field_name] = [
            dict(row)
            for row in _objects(catalog.get(field_name), field_name)
            if (field_name == "calendar_coverage" and row.get(date_field) is None)
            or _date(row.get(date_field), f"{field_name}.{date_field}") <= cutoff
        ]
    raw_hwm_inputs = catalog.get("hwm_inputs")
    if raw_hwm_inputs is not None:
        hwm_inputs = dict(_object(raw_hwm_inputs, "hwm_inputs"))
        hwm_inputs["periods"] = [
            dict(row)
            for row in _objects(hwm_inputs.get("periods"), "hwm_inputs.periods")
            if _date(row.get("date"), "hwm_inputs.periods.date") <= cutoff
        ]
        projected["hwm_inputs"] = hwm_inputs

    def reject_post_cutoff_dates(
        value: Any, field_names: frozenset[str], surface: str
    ) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in field_names and child is not None:
                    if _date(child, f"{surface}.{key}") > cutoff:
                        raise ReportValidationError(
                            f"{surface} calculation context exceeds cutoff"
                        )
                reject_post_cutoff_dates(child, field_names, surface)
        elif isinstance(value, list):
            for child in value:
                reject_post_cutoff_dates(child, field_names, surface)

    context_boundaries = {
        "strategy_live_metrics": frozenset(
            {"first_date", "last_date", "observed_start", "observed_end"}
        ),
        "strategy_nav_coverage": frozenset(
            {"first_date", "last_date", "observed_start", "observed_end"}
        ),
        "hwm_acceptance_record": frozenset({"anchor_date"}),
    }
    for surface, field_names in context_boundaries.items():
        if surface in projected:
            reject_post_cutoff_dates(projected[surface], field_names, surface)
    if raw_hwm_inputs is not None:
        reject_post_cutoff_dates(
            _object(projected["hwm_inputs"].get("anchor"), "hwm_inputs.anchor"),
            frozenset({"anchor_date"}),
            "hwm_inputs.anchor",
        )
    return projected


def build_report_data(
    baseline_root: Path,
    freshness_health_path: Path | None = None,
    provider_ledger_root: Path | None = None,
    attribution_root: Path | None = None,
    history_contract_path: Path | None = None,
    implementation_reconciliation_path: Path | None = None,
    implementation_return_attribution_path: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    correlation_root: Path | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    *,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    _hwm_policy_path: Path | None = None,
    epoch_manifest_path: Path | None = None,
    epoch_manifest_source_path: Path | None = None,
    epoch_crosswalk_path: Path | None = None,
    epoch_manifest_generation_root: Path | None = None,
    baseline_generation_id: str | None = None,
    correlation_generation_dir: Path | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
) -> tuple[dict[str, Any], bytes, Path]:
    """Build canonical report data from the currently accepted baseline."""

    catalog, baseline_ref, baseline_catalog_path = _baseline(
        baseline_root, baseline_generation_id
    )
    strategy_product_controls, product_contract_provenance = _report_product_controls(
        catalog
    )
    current_product_catalog = _current_product_baseline_projection(
        catalog, strategy_product_controls
    )
    # Legacy reports retain their historical account rows.  The explicit
    # crosswalk is the only source that narrows the account perimeter for a
    # governed current-epoch report.
    current_account_ids: set[str] | None = None
    baseline_current_account_ids = _current_product_account_ids(
        catalog, strategy_product_controls
    )
    current_epoch_tuples: set[tuple[str, str, str, str | None]] | None = None
    current_epoch_details: (
        dict[tuple[str, str, str, str | None], dict[str, Any]] | None
    ) = None
    current_epoch_account_details: dict[str, dict[str, Any]] | None = None
    calculation_cutoff_date: str | None = None
    epoch_provenance: list[dict[str, Any]] = []
    epoch_supplied = _require_epoch_manifest_triplet(
        epoch_manifest_path, epoch_manifest_source_path, epoch_crosswalk_path
    )
    _require_epoch_generation_root(epoch_supplied, epoch_manifest_generation_root)
    if (
        _is_governed_financial_mvp2_path(catalog, strategy_product_controls)
        and not epoch_supplied
    ):
        raise ReportValidationError(
            "financial MVP-2 report requires manifest, source receipt, and current epoch crosswalk"
        )
    if epoch_supplied:
        (
            _manifest,
            _source,
            _manifest_bytes,
            _source_bytes,
            _crosswalk_bytes,
            verified_epoch_details,
        ) = _validate_epoch_manifest_input(
            epoch_manifest_path,
            epoch_manifest_source_path,
            strategy_product_controls,
            current_account_ids=baseline_current_account_ids,
            crosswalk_path=epoch_crosswalk_path,
            baseline_catalog=catalog,
            baseline_ref=baseline_ref,
            baseline_catalog_path=baseline_catalog_path,
            epoch_manifest_generation_root=epoch_manifest_generation_root,
        )
        source_receipt_sha256 = hashlib.sha256(_source_bytes).hexdigest()
        epoch_provenance = [
            {
                "artifact": EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
                "generation_id": _source["generation_id"],
                "path": EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
                "schema": "epoch-manifest-source/1",
                "sha256": source_receipt_sha256,
                "size_bytes": len(_source_bytes),
            },
            {
                "artifact": "account-strategy-current-epoch-crosswalk",
                "generation_id": _source["generation_id"],
                "path": EPOCH_CROSSWALK_WORKBOOK_NAME,
                "schema": "account-strategy-current-epoch-crosswalk/1",
                "sha256": hashlib.sha256(_crosswalk_bytes).hexdigest(),
                "size_bytes": len(_crosswalk_bytes),
            },
        ]
        crosswalk_payload = json.loads(_crosswalk_bytes.decode("utf-8"))
        calculation_cutoff_date = crosswalk_payload["calculation_cutoff_date"]
        current_epoch_tuples = {
            (
                row["account_id"],
                row["strategy_id"],
                row["effective_start"],
                row["effective_end"],
            )
            for row in crosswalk_payload["rows"]
            if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
        }
        current_epoch_details = {
            (
                row["account_id"],
                row["strategy_id"],
                row["effective_start"],
                row["effective_end"],
            ): {
                "account_label": row["account_label"],
                "last_four": row["last_four"],
                "epoch_admission_status": verified_epoch_details[
                    (
                        row["account_id"],
                        row["strategy_id"],
                        row["effective_start"],
                        row["effective_end"],
                    )
                ]["epoch_admission_status"],
                "epoch_output_status": verified_epoch_details[
                    (
                        row["account_id"],
                        row["strategy_id"],
                        row["effective_start"],
                        row["effective_end"],
                    )
                ]["epoch_output_status"],
                "epoch_blocking_reason": verified_epoch_details[
                    (
                        row["account_id"],
                        row["strategy_id"],
                        row["effective_start"],
                        row["effective_end"],
                    )
                ]["epoch_blocking_reason"],
            }
            for row in crosswalk_payload["rows"]
            if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
        }
        current_epoch_account_details = {}
        for row in crosswalk_payload["rows"]:
            if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH":
                current_epoch_account_details[row["account_id"]] = {
                    "account_label": row["account_label"],
                    "last_four": row["last_four"],
                }
        current_account_ids = {row[0] for row in current_epoch_tuples}
        catalog = _calculation_catalog_through(catalog, calculation_cutoff_date)
        current_product_catalog = _current_product_baseline_projection(
            catalog, strategy_product_controls
        )
    performance = _account_strategy_performance(
        catalog,
        strategy_product_controls,
        current_epoch_tuples,
        current_epoch_details,
        calculation_cutoff_date=calculation_cutoff_date,
    )
    strategy_provider_evidence = _strategy_provider_evidence(
        catalog,
        strategy_product_controls,
        calculation_cutoff_date=calculation_cutoff_date,
    )
    strategy_names = {
        _text(item.get("name"), "strategy.name")
        for item in _objects(catalog.get("strategies"), "strategies")
    }
    history_contract: dict[str, dict[str, Any]] | None = None
    history_contract_ref: dict[str, Any] | None = None
    if history_contract_path is not None:
        history_contract, history_contract_ref = _history_contract(
            history_contract_path,
            portfolio_as_of=_text(catalog.get("as_of_date"), "portfolio as_of_date"),
            strategy_names=strategy_names,
        )
        if (
            calculation_cutoff_date is not None
            and history_contract_ref.get("as_of_date") != calculation_cutoff_date
        ):
            raise ReportValidationError("strategy history calculation cutoff differs")
        epoch_hpr_receipts = history_contract_ref.pop("epoch_hpr_receipts", [])
        if epoch_hpr_receipts:
            catalog = {**catalog, "epoch_hpr_receipts": epoch_hpr_receipts}
            current_product_catalog = _current_product_baseline_projection(
                catalog, strategy_product_controls
            )
            strategy_provider_evidence = _strategy_provider_evidence(
                catalog,
                strategy_product_controls,
                calculation_cutoff_date=calculation_cutoff_date,
            )
    implementation_contract: dict[str, dict[str, Any]] | None = None
    implementation_contract_ref: dict[str, Any] | None = None
    implementation_policy_evidence = None
    for appendix_path, comparison_key in (
        (implementation_reconciliation_path, "return_reconciliation"),
        (implementation_return_attribution_path, "bridge"),
    ):
        if appendix_path is not None and any(
            _object(row.get(comparison_key), "implementation comparison").get("status")
            == "UNAVAILABLE_POLICY_NOT_ADMITTED"
            for row in _objects(
                _load_json(appendix_path, "implementation appendix").get("strategies"),
                "implementation strategies",
            )
        ):
            implementation_policy_evidence = _selected_implementation_policy_evidence(
                catalog,
                baseline_root,
                provider_ledger_root,
                accepted_as_of=_date(catalog.get("as_of_date"), "portfolio as_of_date"),
            )
            break
    if implementation_reconciliation_path is not None:
        implementation_contract, implementation_contract_ref = (
            _implementation_reconciliation_contract(
                implementation_reconciliation_path,
                portfolio_as_of=_text(
                    catalog.get("as_of_date"), "portfolio as_of_date"
                ),
                expected_policy_evidence=implementation_policy_evidence,
            )
        )
    implementation_return_attribution_contract: dict[str, dict[str, Any]] | None = None
    implementation_return_attribution_ref: dict[str, Any] | None = None
    if implementation_return_attribution_path is not None:
        (
            implementation_return_attribution_contract,
            implementation_return_attribution_ref,
        ) = _implementation_return_attribution_contract(
            implementation_return_attribution_path,
            portfolio_as_of=_text(catalog.get("as_of_date"), "portfolio as_of_date"),
            expected_policy_evidence=implementation_policy_evidence,
        )
    strategy_summary = _strategies(
        catalog,
        performance,
        history_contract,
        implementation_contract,
        implementation_return_attribution_contract,
        strategy_product_controls,
        strategy_provider_evidence,
    )
    if (
        hybrid_decomposition_plan_bundle is not None
        and decomposition_plan_bundle is None
    ):
        raise ReportValidationError("hybrid decomposition requires a live plan bundle")
    if (decomposition_lenses_root is None) != (decomposition_plan_bundle is None):
        raise ReportValidationError(
            "decomposition lens root and plan bundle must be supplied together"
        )
    decomposition_lenses: dict[str, Any] | None = None
    if decomposition_lenses_root is not None and decomposition_plan_bundle is not None:
        from portfolio_control_panel import _decomposition_lenses

        strategy_name_map = {
            _text(row.get("strategy_id"), "strategy_summary.strategy_id"): _text(
                row.get("strategy_name"), "strategy_summary.strategy_name"
            )
            for row in strategy_summary
        }
        decomposition_lenses, _decomposition_bindings = _decomposition_lenses(
            decomposition_lenses_root,
            decomposition_plan_bundle,
            strategy_name_map,
            hybrid_plan_bundle=hybrid_decomposition_plan_bundle,
        )
    attribution_catalog: dict[str, Any] | None = None
    attribution_ref: dict[str, str] | None = None
    if attribution_root is not None:
        attribution_catalog, attribution_ref, _ = _attribution(
            attribution_root, baseline_ref
        )
        strategy_summary = _merge_attribution(strategy_summary, attribution_catalog)
    allocation_history = _allocations(catalog, strategy_product_controls)
    current_weights = {
        _text(row.get("strategy_id"), "allocation_history.strategy_id"): row.get(
            "systematic_weight"
        )
        for row in allocation_history
        if row.get("scope") == "SYSTEMATIC"
    }
    strategy_summary = [
        {
            **row,
            "systematic_weight": current_weights[
                _text(row.get("strategy_id"), "strategy_summary.strategy_id")
            ],
        }
        if row.get("strategy_id") in current_weights
        else row
        for row in strategy_summary
    ]
    coverage = _coverage(catalog)
    dependence_analysis: dict[str, Any] = {
        "default_evidence_mode": "LIVE_ONLY",
        "mode_count": 0,
        "pair_count": 0,
        "pair_mode_count": 0,
        "schema_version": 1,
        "series_ids": [],
        "status": "UNAVAILABLE_NOT_CONFIGURED",
    }
    dependence_rows: list[dict[str, Any]] = []
    dependence_identity: dict[str, str] | None = None
    if correlation_root is not None or correlation_generation_dir is not None:
        if history_contract_ref is None:
            raise ReportValidationError(
                "current dependence requires a verified history contract"
            )
        (
            dependence_analysis,
            dependence_rows,
            dependence_identity,
            _dependence_path,
        ) = _dependence_report(
            correlation_root,
            generation_dir=correlation_generation_dir,
            calculation_cutoff_date=calculation_cutoff_date,
        )
        dependence_source = _object(
            dependence_analysis.get("source"), "dependence source identity"
        )
        if dependence_source.get("sha256") != history_contract_ref.get(
            "returns_sha256"
        ) or dependence_source.get("size_bytes") != history_contract_ref.get(
            "returns_size_bytes"
        ):
            raise ReportValidationError(
                "dependence source differs from verified history contract"
            )
    # The numerical composite consumes only the governed hwm_inputs envelope
    # and is computed independently of the freshness argument: the perimeter
    # is governed by PORT-HWM-3 regardless of whether a freshness assessment
    # is supplied, so the NO_HWM_INPUTS/envelope-derived status always
    # publishes.  Absence or any internal defect yields UNAVAILABLE with a
    # structured reason, never an error or a fallback.
    portable_bundle_presence = {
        hwm_portable_receipt is not None,
        hwm_portable_generation_bytes is not None,
        hwm_portable_calendar_bytes is not None,
    }
    if len(portable_bundle_presence) != 1:
        raise ReportValidationError(
            "portable HWM receipt, input generation, and calendar bytes must be supplied together"
        )
    portable_hwm_proof = (
        _report_portable_hwm_proof(
            hwm_portable_receipt,
            hwm_portable_generation_bytes,
            hwm_portable_calendar_bytes,
        )
        if hwm_portable_receipt is not None
        and hwm_portable_generation_bytes is not None
        and hwm_portable_calendar_bytes is not None
        else None
    )
    portable_hwm_receipt_schema_version = (
        int(hwm_portable_receipt["schema_version"])
        if hwm_portable_receipt is not None
        else None
    )
    # Governed HWM lane (#324/#370): the four governed artifacts form one
    # all-or-nothing bundle, exactly like the portable lane above.
    governed_bundle_presence = {
        hwm_governed_result is not None,
        hwm_governed_generation_bytes is not None,
        hwm_governed_calendar_bytes is not None,
        hwm_governed_receipt_bytes is not None,
    }
    if len(governed_bundle_presence) != 1:
        raise ReportValidationError(
            "governed HWM result, generation, calendar, and receipt bytes must be supplied together"
        )
    financial_input_contract_path = _PORTABLE_HWM_PRODUCT_CONTRACT
    financial_input_contract_bytes = None
    if invested_capital_input_generation_path is not None:
        financial_input_contract_path, financial_input_contract_bytes = (
            resolve_financial_input_authority(
                invested_capital_input_generation_path,
                _PORTABLE_HWM_PRODUCT_CONTRACT,
                catalog,
            )
        )
    governed_hwm_proof = (
        _report_governed_hwm_proof(
            hwm_governed_result,
            hwm_governed_generation_bytes or b"",
            hwm_governed_calendar_bytes or b"",
            hwm_governed_receipt_bytes or b"",
            product_contract_bytes=financial_input_contract_bytes,
        )
        if hwm_governed_result is not None
        else None
    )
    if (invested_capital_result is None) != (
        invested_capital_input_generation_path is None
    ):
        raise ReportValidationError(
            "invested-capital result and input generation must be supplied together"
        )
    invested_capital = (
        dict(
            verify_governed_capital_result(
                invested_capital_result,
                invested_capital_input_generation_path,
                financial_input_contract_path,
            )
        )
        if invested_capital_result is not None
        and invested_capital_input_generation_path is not None
        else None
    )
    hwm_policy_path = HWM_POLICY if _hwm_policy_path is None else _hwm_policy_path
    current_strategy_summary = _current_product_fact_rows(
        strategy_summary,
        strategy_product_controls,
        None,
        "strategy_summary",
    )
    hwm_coverage_inputs: (
        tuple[list[dict[str, Any]], dict[str, Any], date, str] | None
    ) = None
    if freshness_health_path is not None:
        portfolio_as_of = _date(catalog.get("as_of_date"), "portfolio as_of_date")
        freshness, evaluation_date, freshness_sha256 = _freshness_assessments(
            freshness_health_path,
            portfolio_as_of,
            provider_ledger_root,
            catalog,
        )
        hwm_policy, hwm_policy_sha256 = _hwm_policy(evaluation_date, hwm_policy_path)
        current_strategy_ids = {
            _text(row.get("strategy_id"), "strategy_summary.strategy_id")
            for row in current_strategy_summary
        }
        policy_excluded_strategy_ids = set(hwm_policy["excluded_strategy_ids"])
        required_policy_exclusions = policy_excluded_strategy_ids & current_strategy_ids
        if required_policy_exclusions:
            raise ReportValidationError(
                "HWM policy excludes a required current-product strategy"
            )
        effective_hwm_policy = dict(hwm_policy)
        effective_hwm_policy["excluded_strategy_ids"] = sorted(
            policy_excluded_strategy_ids & current_strategy_ids
        )
        hwm_coverage_inputs = (
            freshness,
            effective_hwm_policy,
            evaluation_date,
            hwm_policy_sha256,
        )
    hwm_numerical = _hwm_numerical_from_envelope(
        current_product_catalog, hwm_policy_path
    )
    hwm_summary: dict[str, Any]
    hwm_provenance: list[dict[str, Any]] = []
    if hwm_coverage_inputs is not None:
        freshness, effective_hwm_policy, evaluation_date, hwm_policy_sha256 = (
            hwm_coverage_inputs
        )
        try:
            strategy_summary, coverage_summary = governed_hwm_coverage(
                current_strategy_summary,
                effective_hwm_policy,
                freshness,
                require_freshness=True,
                calculation_boundary=calculation_cutoff_date,
            )
        except HwmCoverageError as exc:
            raise ReportValidationError(str(exc)) from exc
        hwm_summary = {**coverage_summary, **hwm_numerical}
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
    else:
        # No freshness assessment: the coverage perimeter is not evaluated,
        # but the governed numerical state still publishes on its own.
        hwm_summary = hwm_numerical
        strategy_summary = current_strategy_summary
    strategy_provider_evidence = _current_product_fact_rows(
        strategy_provider_evidence,
        strategy_product_controls,
        None,
        "strategy_provider_evidence",
    )
    performance = _current_product_fact_rows(
        performance,
        strategy_product_controls,
        current_account_ids,
        "twr_mwr",
    )
    strategy_returns = _current_product_fact_rows(
        _objects(
            current_product_catalog.get("strategy_returns", []),
            "strategy_returns",
        ),
        strategy_product_controls,
        None,
        "strategy_returns",
    )
    provider_strategy_returns = _current_product_fact_rows(
        _objects(
            catalog.get("provider_strategy_returns", []),
            "provider_strategy_returns",
        ),
        strategy_product_controls,
        None,
        "provider_strategy_returns",
    )
    provider_admissions = _current_product_fact_rows(
        _objects(catalog.get("provider_admissions", []), "provider_admissions"),
        strategy_product_controls,
        current_account_ids,
        "provider_admissions",
    )
    report: dict[str, Any] = {
        "account_summary": _accounts(
            catalog, strategy_product_controls, current_epoch_account_details
        ),
        "allocation_history": allocation_history,
        "as_of_date": catalog.get("as_of_date"),
        "baseline": baseline_ref,
        "corrections": _corrections(catalog),
        "coverage_data_quality": coverage,
        "dependence_analysis": dependence_analysis,
        "dependence_rows": dependence_rows,
        "fact_coverage_matrix": catalog.get("fact_coverage_matrix", []),
        "flow_reconciliation": _flow_reconciliation(
            catalog,
            strategy_product_controls,
            current_account_ids,
        ),
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
            {
                "item": "Dependence",
                "value": "Both LIVE_ONLY and LIVE_PLUS_BACKTEST modes retain exact pairwise observations, horizons, rolling/downside/drawdown/tail evidence, conservative bounds, and provider-lane-only implementation concentration; instrument and venue dimensions remain explicitly unassessed.",
            },
        ],
        "portfolio_summary": [
            *_portfolio_summary(
                catalog,
                hwm_summary,
                dependence_analysis,
                strategy_product_controls,
            ),
            *(
                _portable_hwm_summary_rows(portable_hwm_proof)
                if portable_hwm_proof is not None
                else []
            ),
            *(
                _governed_hwm_summary_rows(governed_hwm_proof)
                if governed_hwm_proof is not None
                else []
            ),
        ],
        "observation_windows": catalog.get("observation_windows", []),
        "positions": _positions(
            catalog,
            current_account_ids,
        ),
        "provider_admissions": provider_admissions,
        "provider_coverage": catalog.get("provider_coverage", []),
        "provider_strategy_returns": provider_strategy_returns,
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
            *(
                [product_contract_provenance]
                if product_contract_provenance is not None
                else []
            ),
            *epoch_provenance,
            *hwm_provenance,
            *(
                [
                    {
                        "artifact": "portfolio-dependence-analysis",
                        "as_of_date": dependence_analysis["as_of_date"],
                        "schema": "portfolio-correlation-analysis/2",
                        "sha256": dependence_identity["correlation_sha256"],
                    }
                ]
                if dependence_identity is not None
                else []
            ),
            *(
                [
                    {
                        "artifact": "portfolio-hwm-4-portable-proof",
                        "as_of_date": portable_hwm_proof["complete_through"],
                        "schema": (
                            "portfolio-hwm-calculation-receipt/"
                            f"{portable_hwm_receipt_schema_version}"
                        ),
                        "sha256": portable_hwm_proof["receipt_sha256"],
                    }
                ]
                if portable_hwm_proof is not None
                else []
            ),
            *(
                [
                    {
                        "artifact": "strategy-history-compatibility-contract",
                        "as_of_date": history_contract_ref["as_of_date"],
                        "schema": history_contract_ref["schema"],
                        "sha256": history_contract_ref["sha256"],
                    }
                ]
                if history_contract_ref is not None
                else []
            ),
            *(
                [
                    {
                        "artifact": "strategy-implementation-return-attribution",
                        "as_of_date": implementation_return_attribution_ref[
                            "as_of_date"
                        ],
                        "schema": "portfolio-implementation-return-attribution/1",
                        "sha256": implementation_return_attribution_ref["sha256"],
                    }
                ]
                if implementation_return_attribution_ref is not None
                else []
            ),
            *(
                [
                    {
                        "artifact": "strategy-implementation-reconciliation",
                        "as_of_date": implementation_contract_ref["as_of_date"],
                        "schema": "portfolio-implementation-reconciliation/1",
                        "sha256": implementation_contract_ref["sha256"],
                    }
                ]
                if implementation_contract_ref is not None
                else []
            ),
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
        "strategy_returns": strategy_returns,
        "transactions_and_flows": _transactions_and_flows(
            catalog,
            strategy_product_controls,
            current_account_ids,
        ),
        "twr_mwr": performance,
    }
    if calculation_cutoff_date is not None:
        report["calculation_cutoff_date"] = calculation_cutoff_date
    if history_contract_ref is not None:
        report["strategy_history_contract"] = history_contract_ref
    if implementation_contract_ref is not None:
        report["implementation_reconciliation_contract"] = implementation_contract_ref
    if implementation_return_attribution_ref is not None:
        report["implementation_return_attribution_contract"] = (
            implementation_return_attribution_ref
        )
    report["hwm_summary"] = hwm_summary
    if invested_capital is not None:
        report["invested_capital"] = invested_capital
    if decomposition_lenses is not None:
        report["decomposition_lenses"] = decomposition_lenses
    if portable_hwm_proof is not None:
        report["hwm_portable_proof"] = portable_hwm_proof
    if governed_hwm_proof is not None:
        report["hwm_governed_proof"] = governed_hwm_proof
    if current_product_catalog.get("hwm_inputs") is not None:
        report["hwm_inputs"] = current_product_catalog["hwm_inputs"]
    if attribution_catalog is not None and attribution_ref is not None:
        report["strategy_attribution"] = {
            "benchmark_assignments": _current_product_fact_rows(
                attribution_catalog["benchmark_assignments"],
                strategy_product_controls,
                None,
                "strategy_attribution.benchmark_assignments",
            ),
            "benchmark_comparisons": attribution_catalog.get(
                "benchmark_comparisons", []
            ),
            "comparisons": _current_product_fact_rows(
                attribution_catalog["comparisons"],
                strategy_product_controls,
                None,
                "strategy_attribution.comparisons",
            ),
            "costs": _current_product_fact_rows(
                attribution_catalog["costs"],
                strategy_product_controls,
                None,
                "strategy_attribution.costs",
            ),
            "generation_id": attribution_ref["generation_id"],
        }
    report = _object(_public_report_value(report), "public report")
    return report, canonical_json_bytes(report), baseline_catalog_path


def _excel_value(value: object, *, header: str | None = None) -> object:
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if header in {"account_label", "last_four"}:
            return value
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


def _portfolio_summary_excel_value(row: Mapping[str, Any], *, header: str) -> object:
    value = row.get(header)
    if header == "value" and row.get("unit") in {"USD", "ratio"}:
        return float(Decimal(str(value)))
    return _excel_value(value, header=header)


def _headers(rows: Sequence[Mapping[str, Any]], fallback: Sequence[str]) -> list[str]:
    if not rows:
        return list(fallback)
    return sorted(rows[0])


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


def _decomposition_workbook_rows(
    value: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project the canonical panel lenses into finite, source-qualified rows."""

    cells: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    modes = _object(value.get("modes"), "decomposition_lenses.modes")
    for evidence_mode in sorted(modes):
        lenses = _objects(modes[evidence_mode], "decomposition lens mode")
        for lens in sorted(lenses, key=lambda row: str(row.get("lens_id", ""))):
            lens_id = _text(lens.get("lens_id"), "decomposition lens_id")
            surfaces = _object(lens.get("surfaces"), "decomposition surfaces")
            for surface in sorted(surfaces):
                analysis = _object(surfaces[surface], "decomposition surface")
                source_specs = {
                    _text(item.get("source_key"), "decomposition source_key"): item
                    for item in _objects(
                        analysis.get("sources"), "decomposition sources"
                    )
                }
                for row in sorted(
                    _objects(analysis.get("series"), "decomposition series"),
                    key=lambda item: (
                        str(item.get("source_key", "")),
                        str(item.get("series_id", "")),
                    ),
                ):
                    source_key = _text(
                        row.get("source_key"), "decomposition row source_key"
                    )
                    series_id = _text(
                        row.get("series_id"), "decomposition row series_id"
                    )
                    source = _object(
                        source_specs.get(source_key), "decomposition source identity"
                    )
                    plan_spec = source.get("plan_spec")
                    plan_spec = plan_spec if isinstance(plan_spec, Mapping) else {}
                    cell = {
                        "evidence_mode": evidence_mode,
                        "generation_id": source.get("generation_id"),
                        "lens_id": lens_id,
                        "model_id": row.get("model_id"),
                        "plan_spec_id": plan_spec.get("spec_id"),
                        "plan_spec_sha256": plan_spec.get("sha256"),
                        "series_id": series_id,
                        "source_key": source_key,
                        "status": row.get("status"),
                        "surface": surface,
                        "analytics_hash": source.get("analytics_hash"),
                        "source_sha256": source.get("source_sha256"),
                        "as_of_date": source.get("as_of_date"),
                        "estimator": row.get("estimator"),
                        "evidence_layer": row.get("evidence_layer"),
                        "selection_status": row.get("selection_status"),
                        "cost_coverage": row.get("cost_coverage"),
                        "use_boundary": row.get("use_boundary"),
                    }
                    contribution = row.get("contribution_summary")
                    diagnostics = row.get("diagnostics")
                    diagnostics = (
                        diagnostics if isinstance(diagnostics, Mapping) else {}
                    )
                    # These existing scalar result blocks supply the workbook's
                    # financial values and interpretation, not just model IDs.
                    for prefix, fields in (
                        ("alpha", row.get("alpha")),
                        ("contribution", contribution),
                        ("coverage", row.get("coverage")),
                        ("train", diagnostics.get("train")),
                        ("holdout", diagnostics.get("holdout")),
                        ("model_quality", row.get("model_quality")),
                        ("history", row.get("series_composition")),
                    ):
                        if isinstance(fields, Mapping):
                            cell.update(
                                {
                                    f"{prefix}_{name}": field
                                    for name, field in fields.items()
                                    if not isinstance(field, (Mapping, list, tuple))
                                }
                            )
                    cells.append(cell)
                    means = (
                        contribution.get("factor_means", {})
                        if isinstance(contribution, Mapping)
                        else {}
                    )
                    for exposure in _objects(
                        row.get("exposures", []), "decomposition exposures"
                    ):
                        factor_id = _text(
                            exposure.get("factor_id"), "decomposition factor_id"
                        )
                        factors.append(
                            {
                                "beta": exposure.get("beta", exposure.get("exposure")),
                                "evidence_mode": evidence_mode,
                                "factor_id": factor_id,
                                "holdout_mean_contribution": means.get(factor_id),
                                "status": row.get("status"),
                                "model_id": row.get("model_id"),
                                "generation_id": source.get("generation_id"),
                                "source_sha256": source.get("source_sha256"),
                                "standard_error": exposure.get("standard_error"),
                                "confidence_status": exposure.get("confidence_status"),
                                "lens_id": lens_id,
                                "series_id": series_id,
                                "source_key": source_key,
                                "surface": surface,
                            }
                        )
    # Some valid diagnostics apply to only one surface. Keep their columns
    # even when the first result has no value for that diagnostic.
    columns = sorted({key for cell in cells for key in cell})
    cells = [{key: cell.get(key) for key in columns} for cell in cells]
    return cells, factors


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
        WORKBOOK_TOTAL_FORMULA,
        formats["formula"],
        "PASS",
    )
    summary_rows = list(report["portfolio_summary"])
    headers = _headers(summary_rows, WORKBOOK_TABLE_SPECS[0][3])
    data = [
        [_portfolio_summary_excel_value(row, header=header) for header in headers]
        for row in summary_rows
    ]
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
                row_index,
                3,
                _portfolio_summary_excel_value(row, header="value"),
                formats["currency"],
            )
        elif row["unit"] == "ratio":
            summary.write_number(
                row_index,
                3,
                _portfolio_summary_excel_value(row, header="value"),
                formats["percent"],
            )
    # Keep navigation links below the summary table.  PORT-HWM-4 adds
    # development-proof rows, so a fixed row would overwrite accepted table
    # values in the workbook even though the canonical JSON remained intact.
    navigation_start_row = max(16, 8 + len(summary_rows))
    capital = report.get("invested_capital")
    capital_sheet_names = CAPITAL_SHEET_NAMES if isinstance(capital, Mapping) else ()
    decomposition = report.get("decomposition_lenses")
    decomposition_sheet_names = (
        DECOMPOSITION_SHEET_NAMES if isinstance(decomposition, Mapping) else ()
    )
    for index, sheet_name in enumerate(
        (*SHEET_NAMES[1:], *capital_sheet_names, *decomposition_sheet_names),
        start=navigation_start_row,
    ):
        summary.write_url(index, 0, f"internal:'{sheet_name}'!A1", string=sheet_name)

    for sheet_name, key, table_name, fallback in WORKBOOK_TABLE_SPECS[1:]:
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

    if isinstance(capital, Mapping):
        daily_rows = [
            *list(capital["daily_by_strategy"]),
            *[{**row, "strategy_id": "TOTAL"} for row in capital["daily_aggregate"]],
        ]
        current_rows = [
            *list(capital["current_by_strategy"]),
            {
                "strategy_id": "TOTAL",
                "invested_capital_usd": capital["current_aggregate_usd"],
            },
        ]
        for sheet_name, rows, table_name in (
            (CAPITAL_SHEET_NAMES[0], daily_rows, CAPITAL_TABLE_NAMES[0]),
            (CAPITAL_SHEET_NAMES[1], current_rows, CAPITAL_TABLE_NAMES[1]),
        ):
            worksheet = workbook.add_worksheet(sheet_name)
            _write_table(
                workbook,
                worksheet,
                title=sheet_name,
                subtitle=(
                    "Verified owner-contributed capital; gains, borrowing, and "
                    "internal transfers do not create aggregate contributions."
                ),
                rows=rows,
                table_name=table_name,
                fallback_headers=("strategy_id", "invested_capital_usd"),
                formats=formats,
            )
            worksheet.write_url(
                "H3", "internal:'Portfolio Summary'!A1", string="Back to summary"
            )
        current_sheet = workbook.get_worksheet_by_name(CAPITAL_SHEET_NAMES[1])
        current_sheet.write("J3", "Daily/current reconciliation", formats["kpi_label"])
        current_sheet.write_formula(
            "J4",
            CAPITAL_RECONCILIATION_FORMULA,
            formats["formula"],
            "PASS",
        )

    if isinstance(decomposition, Mapping):
        cell_rows, factor_rows = _decomposition_workbook_rows(decomposition)
        for sheet_name, rows, table_name, fallback in (
            (
                DECOMPOSITION_SHEET_NAMES[0],
                cell_rows,
                DECOMPOSITION_TABLE_NAMES[0],
                ("evidence_mode", "lens_id", "source_key", "surface", "series_id"),
            ),
            (
                DECOMPOSITION_SHEET_NAMES[1],
                factor_rows,
                DECOMPOSITION_TABLE_NAMES[1],
                (
                    "evidence_mode",
                    "lens_id",
                    "source_key",
                    "surface",
                    "series_id",
                    "factor_id",
                ),
            ),
        ):
            worksheet = workbook.add_worksheet(sheet_name)
            _write_table(
                workbook,
                worksheet,
                title=sheet_name,
                subtitle=(
                    "Plan-bound source, estimator surface, and evidence-mode "
                    "decomposition; equal strategy names do not merge sources."
                ),
                rows=rows,
                table_name=table_name,
                fallback_headers=fallback,
                formats=formats,
            )
            worksheet.write_url(
                "H3", "internal:'Portfolio Summary'!A1", string="Back to summary"
            )

    strategy_sheet = workbook.get_worksheet_by_name("Strategy Summary")
    strategy_rows = list(report["strategy_summary"])
    strategy_headers = _headers(strategy_rows, ("strategy_id",))
    strategy_name_column = strategy_headers.index("strategy_name")
    strategy_value_column = strategy_headers.index("current_value_usd")
    chart = workbook.add_chart({"type": "bar"})
    chart.add_series(
        {
            "name": "Current value (USD)",
            "categories": [
                "Strategy Summary",
                5,
                strategy_name_column,
                4 + len(strategy_rows),
                strategy_name_column,
            ],
            "values": [
                "Strategy Summary",
                5,
                strategy_value_column,
                4 + len(strategy_rows),
                strategy_value_column,
            ],
            "fill": {"color": "#5B9BD5"},
        }
    )
    chart.set_title({"name": "Current systematic allocation"})
    chart.set_legend({"none": True})
    chart.set_style(10)
    strategy_sheet.insert_chart("A17", chart, {"x_scale": 1.25, "y_scale": 1.15})
    workbook.close()
    return {
        "formula_count_expected_min": 2 if capital_sheet_names else 1,
        "sheet_count": (
            len(SHEET_NAMES) + len(capital_sheet_names) + len(decomposition_sheet_names)
        ),
    }


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


def _replace_disposable_json(path: Path, content: bytes) -> None:
    """Atomically reset a writable, non-authority JSON sidecar."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise GenerationConflictError(f"disposable sidecar is linked: {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".engine-run-", suffix=".json", dir=path.parent
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


def _replace_disposable(path: Path, content: bytes) -> None:
    """Atomically reset a writable, non-authority staging file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise GenerationConflictError(
            f"disposable workbook input is linked: {path.name}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".excel-input-", suffix=".xlsx", dir=path.parent
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


def _build_workbook_exact(report: Mapping[str, Any], path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".report-", suffix=".xlsx", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        evidence = _workbook(report, temporary)
        canonicalize_workbook(temporary)
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
    history_contract_path: Path | None = None,
    implementation_reconciliation_path: Path | None = None,
    implementation_return_attribution_path: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    correlation_root: Path | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    epoch_manifest_path: Path | None = None,
    epoch_manifest_source_path: Path | None = None,
    epoch_crosswalk_path: Path | None = None,
    epoch_manifest_generation_root: Path | None = None,
    baseline_generation_id: str | None = None,
    correlation_generation_dir: Path | None = None,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    invested_capital_result_bytes: bytes | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
) -> dict[str, Any]:
    report, report_bytes, _ = build_report_data(
        baseline_root,
        freshness_health_path,
        provider_ledger_root,
        attribution_root,
        history_contract_path,
        implementation_reconciliation_path,
        implementation_return_attribution_path,
        hwm_portable_receipt,
        correlation_root,
        hwm_portable_generation_bytes,
        hwm_portable_calendar_bytes,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        invested_capital_result=invested_capital_result,
        invested_capital_input_generation_path=(invested_capital_input_generation_path),
        epoch_manifest_path=epoch_manifest_path,
        epoch_manifest_source_path=epoch_manifest_source_path,
        epoch_crosswalk_path=epoch_crosswalk_path,
        epoch_manifest_generation_root=epoch_manifest_generation_root,
        baseline_generation_id=baseline_generation_id,
        correlation_generation_dir=correlation_generation_dir,
        decomposition_lenses_root=decomposition_lenses_root,
        decomposition_plan_bundle=decomposition_plan_bundle,
        hybrid_decomposition_plan_bundle=hybrid_decomposition_plan_bundle,
    )
    report_id = hashlib.sha256(report_bytes).hexdigest()
    candidate = output_root / "candidates" / report_id
    data_path = candidate / REPORT_DATA_NAME
    workbook_path = candidate / WORKBOOK_NAME
    excel_input_path = candidate / "excel-input" / WORKBOOK_NAME
    excel_path = candidate / "excel" / WORKBOOK_NAME
    _write_exact(data_path, report_bytes)
    if invested_capital_result is not None:
        assert invested_capital_input_generation_path is not None
        capital_result_bytes = (
            invested_capital_result_bytes
            if invested_capital_result_bytes is not None
            else canonical_json_bytes(dict(invested_capital_result))
        )
        try:
            parsed_capital = json.loads(capital_result_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportValidationError(
                "invested-capital result bytes are invalid"
            ) from exc
        if parsed_capital != invested_capital_result:
            raise ReportValidationError("invested-capital result bytes differ")
        _write_exact(candidate / INVESTED_CAPITAL_RESULT_NAME, capital_result_bytes)
        _write_exact(
            candidate / INVESTED_CAPITAL_INPUT_NAME,
            _regular_file_bytes(
                invested_capital_input_generation_path,
                "invested-capital input generation",
            ),
        )
    workbook_evidence = _build_workbook_exact(report, workbook_path)
    # Excel is allowed to rewrite the file it opens. Keep the canonical initial
    # workbook immutable and reset only this explicit non-authority copy.
    _replace_disposable(excel_input_path, workbook_path.read_bytes())
    contract = {
        "min_row_counts": {
            "tblAccounts": 1,
            "tblAllocations": 8,
            "tblCoverage": 1,
            "tblDependence": 1 if report["dependence_rows"] else 0,
            "tblMethodology": 7,
            "tblPortfolioSummary": 9,
            "tblPositions": 1,
            "tblProvenance": 2,
            "tblStrategies": 7,
            "tblTwrMwr": 1,
            **(
                {
                    "tblInvestedCapitalDaily": len(
                        report["invested_capital"]["daily_by_strategy"]
                    )
                    + len(report["invested_capital"]["daily_aggregate"]),
                    "tblInvestedCapitalCurrent": len(
                        report["invested_capital"]["current_by_strategy"]
                    )
                    + 1,
                }
                if "invested_capital" in report
                else {}
            ),
            **(
                {
                    "tblDecompositionCells": len(
                        _decomposition_workbook_rows(report["decomposition_lenses"])[0]
                    ),
                    "tblDecompositionFactors": len(
                        _decomposition_workbook_rows(report["decomposition_lenses"])[1]
                    ),
                }
                if "decomposition_lenses" in report
                else {}
            ),
        },
        "prohibit_visible_errors": True,
        "required_sheets": [
            *SHEET_NAMES,
            *(CAPITAL_SHEET_NAMES if "invested_capital" in report else ()),
            *(DECOMPOSITION_SHEET_NAMES if "decomposition_lenses" in report else ()),
        ],
        "required_tables": [
            *TABLE_NAMES,
            *(CAPITAL_TABLE_NAMES if "invested_capital" in report else ()),
            *(DECOMPOSITION_TABLE_NAMES if "decomposition_lenses" in report else ()),
        ],
        "sentinel_cells": [
            {
                "cell": "B4",
                "expected": next(
                    row["value"]
                    for row in report["portfolio_summary"]
                    if row["metric"] == "Advisory readiness"
                ),
                "sheet": "Portfolio Summary",
            },
            {"cell": "E4", "expected": "PASS", "sheet": "Portfolio Summary"},
            {"cell": "A6", "expected": "Policy", "sheet": "Methodology"},
            {"cell": "B6", "expected": POLICY_VERSION, "sheet": "Methodology"},
            *(
                [
                    {
                        "cell": "J4",
                        "expected": "PASS",
                        "sheet": CAPITAL_SHEET_NAMES[1],
                    }
                ]
                if "invested_capital" in report
                else []
            ),
        ],
    }
    # Candidate-relative, not absolute. These two fields are the only thing
    # that differed between two builds of identical content into different
    # output roots, and the job is bound into the report generation identity,
    # so an absolute path made the identity depend on where the build ran.
    # The runner receives an absolute job derived from this one.
    job = {
        "idempotency_key": f"portfolio-report-{report_id[:32]}",
        "schema_version": "2.0",
        "steps": [
            {
                "type": "open",
                "read_only": False,
                "update_links": False,
                "workbook_path": excel_input_path.relative_to(candidate).as_posix(),
            },
            {"type": "recalc", "mode": "full_rebuild", "timeout_seconds": 120},
            {
                "type": "save_as",
                "output_path": excel_path.relative_to(candidate).as_posix(),
                "overwrite": True,
            },
        ],
    }
    _write_exact(candidate / "workbook-contract.json", canonical_json_bytes(contract))
    job_bytes = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    _write_exact(candidate / "xlsx-job.json", job_bytes)
    if epoch_manifest_path is not None or epoch_manifest_source_path is not None:
        _manifest, _source, manifest_bytes, source_bytes = _epoch_manifest_payloads(
            epoch_manifest_path, epoch_manifest_source_path
        )
        if epoch_crosswalk_path is None:
            raise ReportValidationError(
                "epoch manifest requires an explicit current epoch crosswalk"
            )
        crosswalk_bytes = _regular_file_bytes(
            epoch_crosswalk_path, "current epoch crosswalk"
        )
        _write_exact(candidate / EPOCH_MANIFEST_WORKBOOK_NAME, manifest_bytes)
        _write_exact(candidate / EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME, source_bytes)
        _write_exact(candidate / EPOCH_CROSSWALK_WORKBOOK_NAME, crosswalk_bytes)
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
            optional_sheet_sets = (
                (),
                CAPITAL_SHEET_NAMES,
                DECOMPOSITION_SHEET_NAMES,
                (*CAPITAL_SHEET_NAMES, *DECOMPOSITION_SHEET_NAMES),
            )
            if sheets not in [
                list((*SHEET_NAMES, *optional_sheets))
                for optional_sheets in optional_sheet_sets
            ]:
                raise ReportValidationError("workbook sheets differ")
            has_capital = all(name in sheets for name in CAPITAL_SHEET_NAMES)
            has_decomposition = all(
                name in sheets for name in DECOMPOSITION_SHEET_NAMES
            )
            formulas = 0
            for name in names:
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                    sheet_root = ET.fromstring(archive.read(name))
                    formulas += sum(
                        1 for item in sheet_root.iter() if _local_name(item.tag) == "f"
                    )
            if formulas < (2 if has_capital else 1):
                raise ReportValidationError("workbook has no reconciliation formula")
            table_count = sum(
                1
                for name in names
                if name.startswith("xl/tables/table") and name.endswith(".xml")
            )
            expected_table_count = (
                len(TABLE_NAMES)
                + (len(CAPITAL_TABLE_NAMES) if has_capital else 0)
                + (len(DECOMPOSITION_TABLE_NAMES) if has_decomposition else 0)
            )
            if table_count != expected_table_count:
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


def _parity_cell_value(value: object, *, header: str) -> object:
    converted = _excel_value(value, header=header)
    return None if converted == "" else converted


def _report_parity_cell_value(
    row: Mapping[str, Any], *, report_key: str, header: str
) -> object:
    if report_key != "portfolio_summary":
        return _parity_cell_value(row.get(header), header=header)
    converted = _portfolio_summary_excel_value(row, header=header)
    return None if converted == "" else converted


def _parity_values_equal(actual: object, expected: object) -> bool:
    """Compare numerics at Excel's 15-significant-digit storage boundary."""

    numeric_types = (int, float)
    if (
        isinstance(actual, numeric_types)
        and not isinstance(actual, bool)
        and isinstance(expected, numeric_types)
        and not isinstance(expected, bool)
    ):
        # Agreement to 15 significant digits, as a relative difference on the
        # exact binary values.  Rounding both operands to 15 digits and
        # requiring equality cannot express this: two doubles one ULP apart --
        # all an Excel round-trip guarantees, since Excel stores 15 significant
        # digits -- straddle a rounding boundary whenever one rounds down and
        # its neighbour rounds up, and then compare unequal at exactly the
        # boundary this is meant to absorb.  Observed on both
        # tblStrategies.systematic_weight and tblInvestedCapitalDaily.
        exact_actual = Decimal(actual)
        exact_expected = Decimal(expected)
        if exact_actual == exact_expected:
            return True
        scale = max(abs(exact_actual), abs(exact_expected))
        if not scale:
            return False
        with localcontext() as context:
            context.prec = 34
            return abs(exact_actual - exact_expected) <= scale * Decimal(10) ** -15
    return actual == expected


def _workbook_parity_receipt(
    report_bytes: bytes, workbook_bytes: bytes
) -> dict[str, Any]:
    """Verify every governed workbook table cell against canonical report JSON."""

    try:
        report = json.loads(report_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("workbook parity report JSON is invalid") from exc
    if not isinstance(report, dict) or report.get("report_schema_version") != 1:
        raise ReportValidationError("workbook parity report contract differs")
    try:
        formula_workbook = load_workbook(
            io.BytesIO(workbook_bytes),
            read_only=False,
            data_only=False,
            keep_links=False,
        )
        cached_workbook = load_workbook(
            io.BytesIO(workbook_bytes),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    except Exception as exc:
        raise ReportValidationError("workbook parity workbook is unreadable") from exc
    try:
        capital = report.get("invested_capital")
        has_capital = isinstance(capital, dict)
        decomposition = report.get("decomposition_lenses")
        has_decomposition = isinstance(decomposition, dict)
        expected_sheets = list(
            (
                *SHEET_NAMES,
                *(CAPITAL_SHEET_NAMES if has_capital else ()),
                *(DECOMPOSITION_SHEET_NAMES if has_decomposition else ()),
            )
        )
        if formula_workbook.sheetnames != expected_sheets:
            raise ReportValidationError("workbook parity sheet order differs")
        formula_cells = [
            (worksheet.title, cell.coordinate, cell.value)
            for worksheet in formula_workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.data_type == "f"
            or (isinstance(cell.value, str) and cell.value.startswith("="))
        ]
        expected_formulas = [("Portfolio Summary", "E4", WORKBOOK_TOTAL_FORMULA)]
        if has_capital:
            expected_formulas.append(
                (
                    CAPITAL_SHEET_NAMES[1],
                    "J4",
                    CAPITAL_RECONCILIATION_FORMULA,
                )
            )
        if formula_cells != expected_formulas:
            raise ReportValidationError("workbook parity formula inventory differs")
        if cached_workbook["Portfolio Summary"]["E4"].value != "PASS":
            raise ReportValidationError("workbook parity cached E4 value differs")
        if (
            has_capital
            and cached_workbook[CAPITAL_SHEET_NAMES[1]]["J4"].value != "PASS"
        ):
            raise ReportValidationError("workbook parity cached capital value differs")
        readiness_rows = [
            row
            for row in report.get("portfolio_summary", [])
            if isinstance(row, dict) and row.get("metric") == "Advisory readiness"
        ]
        if len(readiness_rows) != 1 or not isinstance(
            readiness_rows[0].get("value"), str
        ):
            raise ReportValidationError("workbook parity readiness source differs")
        if (
            formula_workbook["Portfolio Summary"]["B4"].value
            != readiness_rows[0]["value"]
        ):
            raise ReportValidationError("workbook parity readiness differs")
        if (
            formula_workbook["Methodology"]["A6"].value != "Policy"
            or formula_workbook["Methodology"]["B6"].value != POLICY_VERSION
        ):
            raise ReportValidationError("workbook parity methodology sentinel differs")

        observed_table_names: list[str] = []
        table_receipts: list[dict[str, Any]] = []
        report_row_count = 0
        workbook_row_count = 0
        header_cell_count = 0
        data_cell_count = 0
        canonical_data_cell_count = 0
        table_specs = [
            (sheet_name, report_key, table_name, fallback, report.get(report_key))
            for sheet_name, report_key, table_name, fallback in WORKBOOK_TABLE_SPECS
        ]
        if has_capital:
            assert isinstance(capital, dict)
            daily_rows = [
                *capital["daily_by_strategy"],
                *[
                    {**row, "strategy_id": "TOTAL"}
                    for row in capital["daily_aggregate"]
                ],
            ]
            current_rows = [
                *capital["current_by_strategy"],
                {
                    "strategy_id": "TOTAL",
                    "invested_capital_usd": capital["current_aggregate_usd"],
                },
            ]
            table_specs.extend(
                [
                    (
                        CAPITAL_SHEET_NAMES[0],
                        "invested_capital.daily",
                        CAPITAL_TABLE_NAMES[0],
                        ("date", "strategy_id", "invested_capital_usd"),
                        daily_rows,
                    ),
                    (
                        CAPITAL_SHEET_NAMES[1],
                        "invested_capital.current",
                        CAPITAL_TABLE_NAMES[1],
                        ("strategy_id", "invested_capital_usd"),
                        current_rows,
                    ),
                ]
            )
        if has_decomposition:
            assert isinstance(decomposition, dict)
            cell_rows, factor_rows = _decomposition_workbook_rows(decomposition)
            table_specs.extend(
                [
                    (
                        DECOMPOSITION_SHEET_NAMES[0],
                        "decomposition_lenses.cells",
                        DECOMPOSITION_TABLE_NAMES[0],
                        (
                            "evidence_mode",
                            "lens_id",
                            "source_key",
                            "surface",
                            "series_id",
                        ),
                        cell_rows,
                    ),
                    (
                        DECOMPOSITION_SHEET_NAMES[1],
                        "decomposition_lenses.factors",
                        DECOMPOSITION_TABLE_NAMES[1],
                        (
                            "evidence_mode",
                            "lens_id",
                            "source_key",
                            "surface",
                            "series_id",
                            "factor_id",
                        ),
                        factor_rows,
                    ),
                ]
            )
        for sheet_name, report_key, table_name, fallback, raw_rows in table_specs:
            worksheet = formula_workbook[sheet_name]
            if table_name not in worksheet.tables or len(worksheet.tables) != 1:
                raise ReportValidationError(
                    f"workbook parity table boundary differs: {sheet_name}"
                )
            table = worksheet.tables[table_name]
            observed_table_names.append(table.displayName)
            min_column, min_row, max_column, max_row = range_boundaries(table.ref)
            if not isinstance(raw_rows, list) or not all(
                isinstance(row, dict) for row in raw_rows
            ):
                raise ReportValidationError(
                    f"workbook parity report rows differ: {report_key}"
                )
            rows = [dict(row) for row in raw_rows]
            headers = _headers(rows, fallback)
            displayed_headers = [header.replace("_", " ").title() for header in headers]
            if max_column - min_column + 1 != len(headers):
                raise ReportValidationError(
                    f"workbook parity column count differs: {table_name}"
                )
            physical_row_count = max(1, len(rows))
            if max_row - min_row != physical_row_count:
                raise ReportValidationError(
                    f"workbook parity row count differs: {table_name}"
                )
            actual_headers = [
                worksheet.cell(row=min_row, column=column).value
                for column in range(min_column, max_column + 1)
            ]
            if actual_headers != displayed_headers:
                raise ReportValidationError(
                    f"workbook parity headers differ: {table_name}"
                )
            expected_rows = [
                [
                    _report_parity_cell_value(row, report_key=report_key, header=header)
                    for header in headers
                ]
                for row in rows
            ]
            if not expected_rows:
                expected_rows = [[None] * len(headers)]
            for row_offset, expected_row in enumerate(expected_rows, start=1):
                actual_row = [
                    worksheet.cell(row=min_row + row_offset, column=column).value
                    for column in range(min_column, max_column + 1)
                ]
                if not all(
                    _parity_values_equal(actual, expected)
                    for actual, expected in zip(actual_row, expected_row, strict=True)
                ):
                    mismatch = next(
                        index
                        for index, (actual, expected) in enumerate(
                            zip(actual_row, expected_row, strict=True)
                        )
                        if not _parity_values_equal(actual, expected)
                    )
                    raise ReportValidationError(
                        "workbook parity values differ: "
                        f"{table_name} row {row_offset} field {headers[mismatch]}"
                    )
            report_row_count += len(rows)
            workbook_row_count += physical_row_count
            header_cell_count += len(headers)
            data_cell_count += physical_row_count * len(headers)
            canonical_data_cell_count += len(rows) * len(headers)
            table_receipts.append(
                {
                    "canonical_row_count": len(rows),
                    "data_cell_count": physical_row_count * len(headers),
                    "header_count": len(headers),
                    "physical_row_count": physical_row_count,
                    "report_key": report_key,
                    "sheet": sheet_name,
                    "table": table_name,
                }
            )
            expected_table_names = list(
                (
                    *TABLE_NAMES,
                    *(CAPITAL_TABLE_NAMES if has_capital else ()),
                    *(DECOMPOSITION_TABLE_NAMES if has_decomposition else ()),
                )
            )
        if observed_table_names != expected_table_names:
            raise ReportValidationError("workbook parity table order differs")
    finally:
        formula_workbook.close()
        cached_workbook.close()
    return {
        "cached_formula_value_count": 2 if has_capital else 1,
        "canonical_data_cell_count": canonical_data_cell_count,
        "data_cell_count": data_cell_count,
        "formula_cell_count": len(formula_cells),
        "formula_count": len(formula_cells),
        "header_cell_count": header_cell_count,
        "non_table_cell_count": 4,
        "parity_schema_version": WORKBOOK_PARITY_VERSION,
        "report_row_count": report_row_count,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "sheet_count": len(expected_sheets),
        "table_count": len(expected_table_names),
        "tables": table_receipts,
        "workbook_row_count": workbook_row_count,
        "workbook_sha256": hashlib.sha256(workbook_bytes).hexdigest(),
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
    """Normalize the volatile xlsx-win messages and the engine's run ID.

    The engine mints `run_id` at random, so binding it made the report
    generation identity differ between two builds of identical content --
    the mismatch #293 was opened for. Every other `run_id` in this repository
    is a digest of its own payload (`portfolio_current_decomposition_plan`,
    `portfolio_public_decomposition_sources`), and this one now is too.

    This reverses the earlier decision to canonicalize "without changing the
    real run ID". The engine's value is not discarded: it is preserved beside
    the result in `xlsx-engine-run.json`, which nothing binds, so the run
    remains traceable to the supervisor that produced it.
    """
    payload = _load_json(path, "xlsx-win result")
    if payload.get("schema_version") != "2.0":
        raise ReportValidationError("xlsx-win result schema differs")
    _text(payload.get("idempotency_key"), "xlsx-win result idempotency_key")
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
    engine_run_id = run_id
    identity_payload = {key: value for key, value in payload.items() if key != "run_id"}
    payload["run_id"] = hashlib.sha256(
        canonical_json_bytes(identity_payload)
    ).hexdigest()[:32]
    _replace_disposable_json(
        path.with_name(f"{path.stem}.engine-run.json"),
        canonical_json_bytes(
            {
                "engine_run_id": engine_run_id,
                "idempotency_key": payload["idempotency_key"],
                "schema_version": "1",
            }
        ),
    )
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


def _regular_file_bytes(path: Path, field: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReportValidationError(f"{field} is missing or linked")
    return path.read_bytes()


def _successful_workbook_evidence(
    run: Mapping[str, Any], validation: Mapping[str, Any], job: Mapping[str, Any]
) -> dict[str, Any]:
    steps = run.get("steps")
    job_steps = job.get("steps")
    expected_types = ["open", "recalc", "save_as"]
    if (
        run.get("schema_version") != "2.0"
        or run.get("final_state") != "SUCCEEDED"
        or run.get("ok") is not True
        or not isinstance(steps, list)
        or len(steps) != 3
        or not all(isinstance(step, dict) for step in steps)
        or not isinstance(job_steps, list)
        or len(job_steps) != 3
        or not all(isinstance(step, dict) for step in job_steps)
        or [step.get("type") for step in steps] != expected_types
        or [step.get("type") for step in job_steps] != expected_types
        or [step.get("step_index") for step in steps] != [0, 1, 2]
        or [step.get("status") for step in steps]
        != ["succeeded", "succeeded", "succeeded"]
    ):
        raise ReportValidationError("xlsx-win calculation evidence is not successful")
    invariants = validation.get("invariants")
    if (
        validation.get("all_passed") is not True
        or not isinstance(invariants, list)
        or not invariants
        or not all(
            isinstance(item, dict) and item.get("passed") is True for item in invariants
        )
    ):
        raise ReportValidationError("xlsx-win workbook invariants did not all pass")
    return {
        "final_state": "SUCCEEDED",
        "ok": True,
        "recalc_step_status": "succeeded",
        "recalc_step_type": "recalc",
    }


def _workbook_evidence_binding_from_snapshots(
    snapshots: Mapping[str, bytes],
    *,
    candidate_dir: Path,
    final_workbook: Path,
) -> dict[str, Any]:
    """Associate repository sidecars for non-authority workbook delivery."""
    try:
        job = json.loads(snapshots["xlsx_job"])
        parity = json.loads(snapshots["workbook_parity"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("workbook evidence is not valid JSON") from exc
    if not isinstance(job, dict) or not isinstance(parity, dict):
        raise ReportValidationError("workbook evidence root differs")
    report_sha256 = hashlib.sha256(snapshots["report_data"]).hexdigest()
    if candidate_dir.name != report_sha256:
        raise ReportValidationError("candidate report identity differs")
    expected_parity = _repo_bound_workbook_parity_receipt(
        snapshots["report_data"], snapshots["workbook"]
    )
    if parity != expected_parity:
        raise ReportValidationError("workbook parity receipt differs")
    if (
        job.get("schema_version") != "2.0"
        or not isinstance(job.get("steps"), list)
        or len(job["steps"]) != 3
        or not all(isinstance(item, dict) for item in job["steps"])
        or [item.get("type") for item in job["steps"] if isinstance(item, dict)]
        != ["open", "recalc", "save_as"]
        or job["steps"][0].get("workbook_path")
        != (candidate_dir / "excel-input" / WORKBOOK_NAME)
        .relative_to(candidate_dir)
        .as_posix()
        or job["steps"][2].get("output_path")
        != final_workbook.resolve().relative_to(candidate_dir.resolve()).as_posix()
    ):
        raise ReportValidationError("xlsx-win job binding differs")
    binding: dict[str, Any] = {
        "binding_schema_version": WORKBOOK_EVIDENCE_BINDING_VERSION,
        "publication_disposition": WORKBOOK_NON_AUTHORITY_DELIVERY,
        "workbook_evidence_status": WORKBOOK_DELIVERY_EVIDENCE_STATUS,
    }
    for name, content in snapshots.items():
        binding[f"{name}_sha256"] = hashlib.sha256(content).hexdigest()
        binding[f"{name}_size_bytes"] = len(content)
    return binding


def _epoch_manifest_workbook_snapshots(
    candidate_dir: Path,
    epoch_manifest_path: Path | None,
    epoch_manifest_source_path: Path | None,
    epoch_crosswalk_path: Path | None = None,
) -> dict[str, tuple[Path, bytes]]:
    if (epoch_manifest_path is None) != (epoch_manifest_source_path is None) or (
        epoch_manifest_path is None
    ) != (epoch_crosswalk_path is None):
        raise ReportValidationError(
            "epoch manifest, source receipt, and current epoch crosswalk are required together"
        )
    if epoch_manifest_path is None:
        return {}
    assert epoch_manifest_source_path is not None and epoch_crosswalk_path is not None
    expected_manifest = candidate_dir / EPOCH_MANIFEST_WORKBOOK_NAME
    expected_source = candidate_dir / EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME
    expected_crosswalk = candidate_dir / EPOCH_CROSSWALK_WORKBOOK_NAME
    if (
        epoch_manifest_path.resolve() != expected_manifest.resolve()
        or epoch_manifest_source_path.resolve() != expected_source.resolve()
        or epoch_crosswalk_path.resolve() != expected_crosswalk.resolve()
    ):
        raise ReportValidationError("epoch manifest workbook evidence path differs")
    manifest, source, manifest_bytes, source_bytes = _epoch_manifest_payloads(
        epoch_manifest_path, epoch_manifest_source_path
    )
    crosswalk_bytes = _regular_file_bytes(
        epoch_crosswalk_path, "current epoch crosswalk"
    )
    return {
        "epoch_manifest": (epoch_manifest_path, manifest_bytes),
        "epoch_manifest_source": (epoch_manifest_source_path, source_bytes),
        "epoch_crosswalk": (epoch_crosswalk_path, crosswalk_bytes),
    }


def _epoch_manifest_payloads(
    epoch_manifest_path: Path | None,
    epoch_manifest_source_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Read and byte-validate an epoch manifest and its source receipt."""

    if (epoch_manifest_path is None) != (epoch_manifest_source_path is None):
        raise ReportValidationError(
            "epoch manifest and source evidence must be supplied together"
        )
    if epoch_manifest_path is None or epoch_manifest_source_path is None:
        raise ReportValidationError("epoch manifest evidence is required")
    manifest_bytes = _regular_file_bytes(epoch_manifest_path, "epoch manifest evidence")
    source_bytes = _regular_file_bytes(
        epoch_manifest_source_path, "epoch manifest source evidence"
    )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        source = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError(
            "epoch manifest workbook evidence is invalid JSON"
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(source, dict):
        raise ReportValidationError("epoch manifest workbook evidence root differs")
    if (
        canonical_json_bytes(manifest) != manifest_bytes
        or canonical_json_bytes(source) != source_bytes
    ):
        raise ReportValidationError("epoch manifest workbook evidence is not canonical")
    if set(source) != {
        "generation_id",
        "generation_manifest_sha256",
        "generation_receipt_sha256",
        "full_directory_inventory_sha256",
        "artifact",
        "crosswalk",
    }:
        raise ReportValidationError("epoch manifest source workbook evidence differs")
    for key in (
        "generation_id",
        "generation_manifest_sha256",
        "generation_receipt_sha256",
        "full_directory_inventory_sha256",
    ):
        value = source[key]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ReportValidationError(
                f"epoch manifest source workbook evidence {key} differs"
            )
    artifact = source.get("artifact")
    expected_schema_version = str(manifest.get("schema_version"))
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "sha256", "size_bytes", "schema"}
        or artifact.get("path") != "outputs/account-strategy-manifest.json"
        or artifact.get("sha256") != hashlib.sha256(manifest_bytes).hexdigest()
        or artifact.get("size_bytes") != len(manifest_bytes)
        or artifact.get("schema")
        != {"name": "account-strategy-manifest", "version": expected_schema_version}
    ):
        raise ReportValidationError(
            "epoch manifest source artifact differs from workbook evidence"
        )
    crosswalk_artifact = source.get("crosswalk")
    if (
        not isinstance(crosswalk_artifact, dict)
        or set(crosswalk_artifact) != {"path", "sha256", "size_bytes", "schema"}
        or crosswalk_artifact.get("path")
        != "authority/account-assignment-crosswalk.json"
        or not isinstance(crosswalk_artifact.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", crosswalk_artifact["sha256"]) is None
        or isinstance(crosswalk_artifact.get("size_bytes"), bool)
        or not isinstance(crosswalk_artifact.get("size_bytes"), int)
        or crosswalk_artifact["size_bytes"] < 1
        or crosswalk_artifact.get("schema")
        != {"name": "account-strategy-current-epoch-crosswalk", "version": "1"}
    ):
        raise ReportValidationError(
            "epoch manifest source crosswalk artifact differs from workbook evidence"
        )
    return manifest, source, manifest_bytes, source_bytes


def _validate_epoch_manifest_input(
    epoch_manifest_path: Path | None,
    epoch_manifest_source_path: Path | None,
    strategy_product_controls: Mapping[str, Mapping[str, Any]],
    *,
    current_account_ids: set[str] | None = None,
    crosswalk_path: Path | None = None,
    baseline_catalog: Mapping[str, Any] | None = None,
    baseline_ref: Mapping[str, Any] | None = None,
    baseline_catalog_path: Path | None = None,
    epoch_manifest_generation_root: Path | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    bytes,
    bytes,
    bytes,
    dict[tuple[str, str, str, str | None], dict[str, Any]],
]:
    """Validate semantic epoch identity against the explicit join authority."""

    _require_epoch_manifest_triplet(
        epoch_manifest_path, epoch_manifest_source_path, crosswalk_path
    )
    if epoch_manifest_generation_root is None:
        raise ReportValidationError(
            "epoch manifest requires an explicit verified generation root"
        )

    manifest, source, manifest_bytes, source_bytes = _epoch_manifest_payloads(
        epoch_manifest_path, epoch_manifest_source_path
    )
    if crosswalk_path is None:
        raise ReportValidationError(
            "epoch manifest requires an explicit current epoch crosswalk"
        )
    authority_path = crosswalk_path
    crosswalk = _load_json(authority_path, "current epoch crosswalk")
    crosswalk_bytes = _regular_file_bytes(authority_path, "current epoch crosswalk")
    _validate_current_epoch_crosswalk(
        crosswalk,
        strategy_product_controls=strategy_product_controls,
        baseline_catalog=baseline_catalog,
        baseline_ref=baseline_ref,
        baseline_catalog_path=baseline_catalog_path,
    )
    _validate_report_account_manifest(
        manifest,
        strategy_product_controls,
        current_account_ids=current_account_ids,
        crosswalk=crosswalk,
    )
    if manifest.get("schema_version") == 9 and manifest.get(
        "calculation_cutoff_date"
    ) != crosswalk.get("calculation_cutoff_date"):
        raise ReportValidationError(
            "epoch manifest cutoff differs from selected crosswalk"
        )
    source_crosswalk = source["crosswalk"]
    if source_crosswalk["sha256"] != hashlib.sha256(
        crosswalk_bytes
    ).hexdigest() or source_crosswalk["size_bytes"] != len(crosswalk_bytes):
        raise ReportValidationError("current epoch crosswalk source receipt differs")
    epoch_details = _validate_epoch_generation_authority(
        epoch_manifest_generation_root,
        source,
        manifest_bytes,
        crosswalk_bytes,
    )
    return (
        manifest,
        source,
        manifest_bytes,
        source_bytes,
        crosswalk_bytes,
        epoch_details,
    )


def _verified_epoch_authority_details(
    manifest: Mapping[str, Any],
    verification: Mapping[str, Any],
    calculation_inputs: Mapping[str, Any],
    subject_payloads: Mapping[str, bytes],
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    """Validate the exact schema-9/verification-3 admitted epoch partition."""

    try:
        manifest_schema = json.loads(EPOCH_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(manifest_schema, format_checker=FormatChecker()).validate(
            manifest
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        JsonSchemaValidationError,
    ) as exc:
        raise ReportValidationError(
            "epoch manifest schema-9 authority differs"
        ) from exc
    cutoff = cast(str, manifest.get("calculation_cutoff_date"))
    if manifest.get("schema_version") != 9:
        raise ReportValidationError(
            "epoch generation requires schema-9 cutoff authority"
        )
    try:
        verification_schema = json.loads(
            EPOCH_VERIFICATION_SCHEMA.read_text(encoding="utf-8")
        )
        Draft202012Validator(
            verification_schema, format_checker=FormatChecker()
        ).validate(verification)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        JsonSchemaValidationError,
    ) as exc:
        raise ReportValidationError(
            "epoch verification schema-3 authority differs"
        ) from exc
    required_verification_keys = {
        "schema_version",
        "artifact_id",
        "as_of_date",
        "status",
        "verifier",
        "selected_source_identities",
        "subjects",
        "subjects_semantic_sha256",
        "counts",
        "blocker_chains",
        "numeric_verification",
        "invariants",
    }
    if (
        set(verification) != required_verification_keys
        or verification.get("schema_version") != 3
        or verification.get("artifact_id")
        != "verification/independent-verification.json"
        or verification.get("status") != "VERIFIED"
    ):
        raise ReportValidationError("epoch verification schema-3 authority differs")
    expected_invariants = {
        "exact_subject_set": True,
        "canonical_subject_bytes": True,
        "selected_source_binding": True,
        "fact_disposition_complete": True,
        "unassigned_numeric_exclusion": True,
        "classified_flow_reconciliation": True,
        "mapped_row_bijection": True,
        "blocker_propagation": True,
        "blocked_numeric_exclusion": True,
        "privacy_safe_projection": True,
    }
    if verification.get("invariants") != expected_invariants:
        raise ReportValidationError("epoch verification invariants differ")
    raw_subjects = verification.get("subjects")
    if not isinstance(raw_subjects, list):
        raise ReportValidationError("epoch verification subjects differ")
    observed_subjects: list[dict[str, Any]] = []
    subject_paths: set[str] = set()
    for raw_subject in raw_subjects:
        if not isinstance(raw_subject, dict):
            raise ReportValidationError("epoch verification subjects differ")
        path = raw_subject.get("path")
        if not isinstance(path, str) or path in subject_paths:
            raise ReportValidationError("epoch verification subjects differ")
        payload = subject_payloads.get(path)
        if type(payload) is not bytes:
            raise ReportValidationError("epoch verification subject payload differs")
        observed = {
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        if raw_subject != observed:
            raise ReportValidationError("epoch verification subject identity differs")
        observed_subjects.append(observed)
        subject_paths.add(path)
    if subject_paths != set(subject_payloads) - {
        "verification/independent-verification.json"
    }:
        raise ReportValidationError("epoch verification subject set differs")
    expected_subject_digest = hashlib.sha256(
        canonical_json_bytes({"subjects": observed_subjects})
    ).hexdigest()
    if verification.get("subjects_semantic_sha256") != expected_subject_digest:
        raise ReportValidationError(
            "epoch verification subjects semantic digest differs"
        )

    def subject_object(path: str) -> dict[str, Any]:
        payload = subject_payloads.get(path)
        if type(payload) is not bytes:
            raise ReportValidationError("epoch verification subject payload differs")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportValidationError(
                "epoch verification subject payload differs"
            ) from exc
        if not isinstance(decoded, dict) or canonical_json_bytes(decoded) != payload:
            raise ReportValidationError("epoch verification subject payload differs")
        return decoded

    crosswalk_authority = subject_object("authority/account-assignment-crosswalk.json")
    if crosswalk_authority.get("calculation_cutoff_date") != cutoff:
        raise ReportValidationError(
            "epoch manifest cutoff differs from selected crosswalk"
        )

    selected_identities = subject_object("authority/selected-input-identities.json")
    if verification.get("selected_source_identities") != selected_identities:
        raise ReportValidationError(
            "epoch verification selected source identity differs"
        )
    selected_verifier = selected_identities.get("code_sources", {}).get(
        "independent_verifier"
    )
    verification_verifier = verification.get("verifier")
    if (
        not isinstance(selected_verifier, Mapping)
        or not isinstance(verification_verifier, Mapping)
        or {
            "artifact_id": verification_verifier.get("artifact_id"),
            "sha256": verification_verifier.get("sha256"),
            "size_bytes": verification_verifier.get("size_bytes"),
        }
        != dict(selected_verifier)
        or verification_verifier.get("as_of_date")
        != selected_identities.get("as_of_date")
        or verification_verifier.get("schema")
        != {"name": "independent-account-manifest-verifier", "version": "1"}
    ):
        raise ReportValidationError(
            "epoch verification selected verifier identity differs"
        )
    crosswalk_subject = next(
        subject
        for subject in observed_subjects
        if subject["path"] == "authority/account-assignment-crosswalk.json"
    )
    if selected_identities.get("current_epoch_crosswalk") != {
        "sha256": crosswalk_subject["sha256"],
        "size_bytes": crosswalk_subject["size_bytes"],
    }:
        raise ReportValidationError(
            "epoch verification selected source identity differs"
        )

    def subject_identity(path: str) -> dict[str, Any]:
        payload = subject_payloads.get(path)
        if type(payload) is not bytes:
            raise ReportValidationError(
                "epoch manifest selected authority graph differs"
            )
        return {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }

    def artifact_binds_subject(artifact: object, path: str) -> bool:
        return (
            isinstance(artifact, Mapping)
            and artifact.get("artifact_id") == path
            and {
                "sha256": artifact.get("sha256"),
                "size_bytes": artifact.get("size_bytes"),
            }
            == subject_identity(path)
        )

    def validate_receipt_authority() -> None:
        failure = "epoch receipt authority semantic graph differs"
        try:
            authority = subject_object(
                "authority/account-interval-retrieval-receipt-authority.json"
            )
            entries = authority.get("receipts")
            if not isinstance(entries, list):
                raise TypeError("epoch receipt authority receipts must be a list")
            selected_receipts = selected_identities["retrieval_receipts"]
            selected_by_id = {
                receipt["receipt_id"]: receipt for receipt in selected_receipts
            }
            authority_ids = [
                entry["artifact_id"] for entry in entries if isinstance(entry, Mapping)
            ]
            receipt_schema = json.loads(
                EPOCH_RETRIEVAL_RECEIPT_SCHEMA.read_text(encoding="utf-8")
            )
            provider_as_of = date.fromisoformat(
                selected_identities["provider_ledger"]["artifact"]["as_of_date"]
            )
            authority_as_of = date.fromisoformat(authority["as_of_date"])
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ReportValidationError(failure) from exc
        if (
            set(authority)
            != {"schema_version", "artifact_id", "as_of_date", "receipts"}
            or authority.get("schema_version") != 1
            or authority.get("artifact_id")
            != "authority/account-interval-retrieval-receipt-authority.json"
            or authority_as_of.isoformat() != authority.get("as_of_date")
            or authority_as_of > provider_as_of
            or not isinstance(entries, list)
            or not 1 <= len(entries) <= 15
            or len(selected_by_id) != len(selected_receipts)
            or len({receipt_id.casefold() for receipt_id in selected_by_id})
            != len(selected_by_id)
            or any(
                not receipt_id.startswith("receipt/") for receipt_id in selected_by_id
            )
            or authority_ids != sorted(authority_ids, key=str.casefold)
            or len({receipt_id.casefold() for receipt_id in authority_ids})
            != len(authority_ids)
            or set(authority_ids) != set(selected_by_id)
        ):
            raise ReportValidationError(failure)
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "artifact_id",
                "sha256",
                "size_bytes",
                "contract",
            }:
                raise ReportValidationError(failure)
            receipt_id = entry["artifact_id"]
            selected_identity = selected_by_id[receipt_id]
            expected_identity = subject_identity(receipt_id)
            if {
                "sha256": entry.get("sha256"),
                "size_bytes": entry.get("size_bytes"),
            } != expected_identity or {
                "sha256": selected_identity.get("sha256"),
                "size_bytes": selected_identity.get("size_bytes"),
            } != expected_identity:
                raise ReportValidationError(failure)
            receipt = subject_object(receipt_id)
            try:
                Draft202012Validator(
                    receipt_schema, format_checker=FormatChecker()
                ).validate(receipt)
            except JsonSchemaValidationError as exc:
                raise ReportValidationError(failure) from exc
            if receipt.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
                try:
                    staged_inputs = receipt["staged_inputs"]
                    crosswalk = subject_object(
                        "authority/account-assignment-crosswalk.json"
                    )
                    calculation_rows = subject_object(
                        "authority/calculation-inputs.json"
                    )["rows"]
                    assignment = (
                        receipt["account_id"],
                        receipt["strategy_id"],
                        receipt["requested_start"],
                    )
                    matching_crosswalk = [
                        row
                        for row in crosswalk["rows"]
                        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
                        and (
                            row["account_id"],
                            row["strategy_id"],
                            row["effective_start"],
                        )
                        == assignment
                        and row["provider"] == receipt["provider"]
                        and (
                            row["effective_end"] is None
                            or row["effective_end"] == receipt["requested_end"]
                        )
                    ]
                    matching_calculation = [
                        row
                        for row in calculation_rows
                        if (
                            row["epoch"]["account_id"],
                            row["epoch"]["strategy_id"],
                            row["epoch"]["effective_start"],
                            row["epoch"]["effective_end"],
                        )
                        == (
                            receipt["account_id"],
                            receipt["strategy_id"],
                            receipt["requested_start"],
                            matching_crosswalk[0]["effective_end"],
                        )
                    ]
                    expected_end = matching_crosswalk[0]["effective_end"] or cutoff
                    issuer = receipt["issuer"]

                    def observed_facts(field: str) -> dict[str, Any]:
                        facts = matching_calculation[0][field]
                        dates = sorted(
                            item.get(
                                "as_of_date",
                                item.get(
                                    "nav_recognition_date", item.get("event_date")
                                ),
                            )
                            for item in facts
                        )
                        return {
                            "row_count": len(facts),
                            "first_date": dates[0] if dates else None,
                            "last_date": dates[-1] if dates else None,
                        }

                    expected_staged_inputs = {
                        "provider_ledger_generation_id": selected_identities[
                            "provider_ledger"
                        ]["generation_id"],
                        "provider_ledger_artifact": selected_identities[
                            "provider_ledger"
                        ]["artifact"],
                        "baseline_sha256": selected_identities["baseline"]["artifact"][
                            "sha256"
                        ],
                        "baseline_size_bytes": selected_identities["baseline"][
                            "artifact"
                        ]["size_bytes"],
                        "crosswalk_sha256": subject_identity(
                            "authority/account-assignment-crosswalk.json"
                        )["sha256"],
                        "crosswalk_size_bytes": subject_identity(
                            "authority/account-assignment-crosswalk.json"
                        )["size_bytes"],
                    }
                    expected_contract = {
                        "coverage_mode": "STATIC_SNAPSHOT_COVERAGE_V1",
                        "account_id": receipt["account_id"],
                        "strategy_id": receipt["strategy_id"],
                        "requested_start": receipt["requested_start"],
                        "requested_end": receipt["requested_end"],
                        "provider_generation": receipt["provider_generation"],
                        "staged_inputs": staged_inputs,
                    }
                except (IndexError, KeyError, TypeError) as exc:
                    raise ReportValidationError(failure) from exc
                if (
                    len(matching_crosswalk) != 1
                    or len(matching_calculation) != 1
                    or receipt.get("requested_end") != expected_end
                    or receipt.get("calculation_cutoff_date") != cutoff
                    or issuer.get("adapter_name") != "static-snapshot-coverage"
                    or issuer.get("adapter_version") != "1"
                    or cast(str, receipt.get("coverage_as_of_date")) < cutoff
                    or staged_inputs != expected_staged_inputs
                    or receipt.get("observed_facts")
                    != {
                        "nav": observed_facts("nav_observations"),
                        "flows": observed_facts("classified_flows"),
                        "capital_anchors": observed_facts("capital_anchors"),
                    }
                    or entry.get("contract") != expected_contract
                ):
                    raise ReportValidationError(failure)
                continue
            issuer = receipt["issuer"]
            required_categories = receipt["required_categories"]
            raw_artifacts = receipt["raw_evidence_artifacts"]
            expected_contract = {
                "adapter_name": issuer["adapter_name"],
                "adapter_version": issuer["adapter_version"],
                "code_sha256": issuer["code_sha256"],
                "provider": receipt["provider"],
                "account_id": receipt["account_id"],
                "source_account_identity_sha256": receipt[
                    "source_account_identity_sha256"
                ],
                "provider_generation": receipt["provider_generation"],
                "provider_generation_manifest": receipt["provider_generation_manifest"],
                "raw_evidence_artifacts": raw_artifacts,
                "required_categories": required_categories,
                "pagination": receipt["pagination"],
            }
            requested_start = receipt.get("requested_start")
            requested_end = receipt.get("requested_end")
            if (
                not isinstance(requested_start, str)
                or not isinstance(requested_end, str)
                or receipt.get("artifact_id") != receipt_id
                or receipt.get("covered_start") != receipt.get("requested_start")
                or receipt.get("covered_end") != receipt.get("requested_end")
                or requested_start > requested_end
                or required_categories != sorted(set(required_categories))
                or receipt.get("completed_categories") != required_categories
                or receipt.get("unavailable_categories") != []
                or len({item["artifact_id"] for item in raw_artifacts})
                != len(raw_artifacts)
                or entry.get("contract") != expected_contract
            ):
                raise ReportValidationError(failure)

    validate_receipt_authority()

    try:
        manifest_sources = manifest["source_identities"]
        selected_provider = selected_identities["provider_ledger"]
        selected_baseline = selected_identities["baseline"]
        selected_code_sources = selected_identities["code_sources"]
        manifest_ledger = manifest_sources["ledger"]
        expected_ledger = {
            "generation_id": selected_provider["generation_id"],
            "artifact": selected_provider["artifact"],
            "identity_artifact": selected_provider["generation_manifest"],
        }
        expected_baseline = {
            "generation_id": selected_baseline["generation_id"],
            "artifact": selected_baseline["artifact"],
        }
        classified_source = manifest_sources["classified_flows"]
        policy_source = manifest_sources["flow_classification_policy"]
        coverage_source = manifest_sources["flow_coverage"]
        admission_source = manifest_sources["admission_policy"]

        flow_policy = subject_object("authority/flow-classification-policy.json")
        admission_policy = subject_object("authority/mapped-epoch-admission.json")
        code_identities = subject_object("authority/code-identities.json")
        selected_policy_identity = selected_identities["flow_classification_policy"]
        raw_policy_bytes = FLOW_CLASSIFICATION_POLICY_CONTRACT.read_bytes()
        raw_policy = json.loads(raw_policy_bytes)
        expected_policy_identity = subject_identity(
            "authority/flow-classification-policy.json"
        )
        expected_receipt_authority = subject_identity(
            "authority/account-interval-retrieval-receipt-authority.json"
        )
        selected_receipt_ids = {
            receipt["receipt_id"]
            for receipt in selected_identities["retrieval_receipts"]
        }
        receipt_subject_ids = {
            path for path in subject_paths if path.startswith("receipt/")
        }
        manifest_schema_bytes = EPOCH_MANIFEST_SCHEMA.read_bytes()

        graph_is_consistent = (
            manifest_ledger == expected_ledger
            and manifest_sources["baseline"] == expected_baseline
            and classified_source["ledger_identity"] == expected_ledger
            and coverage_source["ledger_identity"] == expected_ledger
            and manifest_sources["calculation_code"]
            == selected_code_sources["epoch_producer"]
            and code_identities
            == {
                "schema_version": 1,
                "artifact_id": "authority/code-identities.json",
                "as_of_date": selected_identities["as_of_date"],
                "code_sources": selected_code_sources,
            }
            and selected_policy_identity
            in (
                {
                    "sha256": hashlib.sha256(raw_policy_bytes).hexdigest(),
                    "size_bytes": len(raw_policy_bytes),
                },
                expected_policy_identity,
            )
            and raw_policy == flow_policy
            and classified_source["classification_policy"]
            == {
                "policy_id": flow_policy["policy_id"],
                "policy_version": flow_policy["policy_version"],
                "policy_as_of_date": flow_policy["as_of_date"],
                "policy_sha256": expected_policy_identity["sha256"],
            }
            and {
                "policy_id": policy_source["policy_id"],
                "policy_version": policy_source["policy_version"],
                "event_dispositions": policy_source["event_dispositions"],
            }
            == {
                "policy_id": flow_policy["policy_id"],
                "policy_version": flow_policy["policy_version"],
                "event_dispositions": flow_policy["event_dispositions"],
            }
            and admission_source["policy_id"] == admission_policy["policy_id"]
            and admission_source["policy_version"] == admission_policy["policy_version"]
            and admission_source["assignment"] == manifest_sources["assignment"]
            and coverage_source["classified_flows_artifact"]
            == classified_source["artifact"]
            and selected_identities["receipt_authority"] == expected_receipt_authority
            and selected_receipt_ids == receipt_subject_ids
            and selected_identities["manifest_schema"]
            == {
                "sha256": hashlib.sha256(manifest_schema_bytes).hexdigest(),
                "size_bytes": len(manifest_schema_bytes),
            }
            and artifact_binds_subject(
                classified_source["artifact"],
                "authority/classified-flows.json",
            )
            and artifact_binds_subject(
                policy_source["artifact"],
                "authority/flow-classification-policy.json",
            )
            and artifact_binds_subject(
                coverage_source["artifact"],
                "authority/account-flow-coverage.json",
            )
            and artifact_binds_subject(
                coverage_source["receipt_authority"],
                "authority/account-interval-retrieval-receipt-authority.json",
            )
            and artifact_binds_subject(
                admission_source["artifact"],
                "authority/mapped-epoch-admission.json",
            )
            and artifact_binds_subject(
                manifest_sources["selected_inputs"],
                "authority/selected-input-identities.json",
            )
            and artifact_binds_subject(
                manifest_sources["account_assignment_perimeter"],
                "authority/account-assignment-perimeter.json",
            )
            and artifact_binds_subject(
                manifest_sources["calculation_inputs"],
                "authority/calculation-inputs.json",
            )
            and artifact_binds_subject(
                manifest_sources["code_identities"],
                "authority/code-identities.json",
            )
            and all(
                {
                    "sha256": receipt["sha256"],
                    "size_bytes": receipt["size_bytes"],
                }
                == subject_identity(receipt["receipt_id"])
                for receipt in selected_identities["retrieval_receipts"]
            )
        )
    except (KeyError, TypeError, OSError):
        graph_is_consistent = False
    if not graph_is_consistent:
        raise ReportValidationError("epoch manifest selected authority graph differs")
    counts = verification.get("counts")
    required_counts = {
        "selected_baseline_account_count",
        "account_count",
        "assignment_count",
        "mapped_assignment_count",
        "historical_assignment_count",
        "reserve_assignment_count",
        "current_product_epoch_count",
        "observed_nav_fact_count",
        "observed_raw_flow_count",
        "classified_event_count",
        "classified_event_classified_count",
        "classified_event_blocked_count",
        "unmatched_nav_fact_count",
        "unmatched_raw_flow_fact_count",
        "unmatched_fact_count",
        "unmatched_projection_count",
        "calculation_input_row_count",
        "coverage_row_count",
        "admission_row_count",
        "output_row_count",
        "complete_coverage_row_count",
        "funded_admission_row_count",
        "ok_output_row_count",
        "blocked_coverage_row_count",
        "blocked_admission_row_count",
        "blocked_output_row_count",
    }
    if (
        not isinstance(counts, dict)
        or set(counts) != required_counts
        or any(type(value) is not int or value < 0 for value in counts.values())
        or counts["mapped_assignment_count"] != 15
        or counts["current_product_epoch_count"] != 13
        or any(
            counts[field] != 13
            for field in (
                "calculation_input_row_count",
                "coverage_row_count",
                "admission_row_count",
                "output_row_count",
            )
        )
        or counts["coverage_row_count"]
        != counts["complete_coverage_row_count"] + counts["blocked_coverage_row_count"]
        or counts["admission_row_count"]
        != counts["funded_admission_row_count"] + counts["blocked_admission_row_count"]
        or counts["output_row_count"]
        != counts["ok_output_row_count"] + counts["blocked_output_row_count"]
        or counts["assignment_count"]
        != counts["mapped_assignment_count"]
        + counts["historical_assignment_count"]
        + counts["reserve_assignment_count"]
        or counts["classified_event_count"]
        != counts["classified_event_classified_count"]
        + counts["classified_event_blocked_count"]
        or counts["unmatched_fact_count"]
        != counts["unmatched_nav_fact_count"] + counts["unmatched_raw_flow_fact_count"]
    ):
        raise ReportValidationError("epoch verification semantic counts differ")
    numeric = verification.get("numeric_verification")
    if (
        not isinstance(numeric, dict)
        or numeric.get("candidate_count") != counts["ok_output_row_count"]
        or numeric.get("comparison_count") != counts["ok_output_row_count"]
        or numeric.get("discrepancy_count") != 0
        or numeric.get("disposition") != "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES"
    ):
        raise ReportValidationError("epoch verification numeric partition differs")
    calculation_rows = _objects(
        calculation_inputs.get("rows"), "epoch calculation input rows"
    )
    coverage_payload = subject_object("authority/account-flow-coverage.json")
    coverage_rows = _objects(
        coverage_payload.get("rows"), "epoch verification coverage rows"
    )
    admission_payload = subject_object("authority/mapped-epoch-admission.json")
    admission_rows = _objects(
        admission_payload.get("admissions"), "epoch verification admission rows"
    )
    manifest_rows = _objects(manifest.get("rows"), "epoch manifest rows")
    calculation_ledger = calculation_inputs.get("calculation_ledger")
    if not isinstance(calculation_ledger, dict):
        raise ReportValidationError("epoch calculation input cutoff differs")
    _objects(
        calculation_ledger.get("nav_observations"),
        "epoch calculation ledger nav observations",
    )
    _objects(calculation_ledger.get("flows"), "epoch calculation ledger flows")
    classified_payload = subject_object("authority/classified-flows.json")
    classified_events = _objects(
        classified_payload.get("events"), "epoch classified events"
    )
    cardinalities = {
        "current_product_epoch_count": len(manifest_rows),
        "classified_event_count": len(classified_events),
        "calculation_input_row_count": len(calculation_rows),
        "coverage_row_count": len(coverage_rows),
        "admission_row_count": len(admission_rows),
        "output_row_count": len(manifest_rows),
        "complete_coverage_row_count": sum(
            row.get("status") == "COMPLETE" for row in coverage_rows
        ),
        "funded_admission_row_count": sum(
            row.get("status") == "FUNDED" for row in admission_rows
        ),
        "ok_output_row_count": sum(row.get("status") == "OK" for row in manifest_rows),
        "blocked_coverage_row_count": sum(
            row.get("status") == "BLOCKED" for row in coverage_rows
        ),
        "blocked_admission_row_count": sum(
            row.get("status") == "BLOCKED" for row in admission_rows
        ),
        "blocked_output_row_count": sum(
            row.get("status") != "OK" for row in manifest_rows
        ),
    }
    if any(counts.get(field) != value for field, value in cardinalities.items()):
        raise ReportValidationError("epoch verification semantic counts differ")
    if (
        calculation_inputs.get("schema_version") != 4
        or calculation_inputs.get("artifact_id") != "authority/calculation-inputs.json"
        or not isinstance(calculation_ledger, dict)
        or calculation_ledger.get("calculation_cutoff_date") != cutoff
    ):
        raise ReportValidationError("epoch calculation input cutoff differs")

    def assert_calculation_dates(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {
                    "date",
                    "event_date",
                    "nav_recognition_date",
                    "as_of_date",
                }:
                    if _date(child, f"calculation input {key}").isoformat() > cutoff:
                        raise ReportValidationError(
                            "epoch calculation input exceeds cutoff"
                        )
                assert_calculation_dates(child)
        elif isinstance(value, list):
            for child in value:
                assert_calculation_dates(child)

    assert_calculation_dates(calculation_ledger)
    assert_calculation_dates(calculation_inputs.get("rows"))
    chains = _objects(verification.get("blocker_chains"), "epoch blocker chains")
    if len(manifest_rows) != 13 or len(chains) != 13:
        raise ReportValidationError("epoch verification semantic partition differs")
    tuple_fields = ("account_id", "strategy_id", "effective_start", "effective_end")
    by_key: dict[tuple[str, str, str, str | None], Mapping[str, Any]] = {}
    for row in manifest_rows:
        raw_key = tuple(row.get(field) for field in tuple_fields)
        if (
            not isinstance(raw_key[0], str)
            or not isinstance(raw_key[1], str)
            or not isinstance(raw_key[2], str)
            or (raw_key[3] is not None and not isinstance(raw_key[3], str))
            or raw_key in by_key
        ):
            raise ReportValidationError("epoch manifest tuple authority differs")
        key = cast(tuple[str, str, str, str | None], raw_key)
        by_key[key] = row

    def index_authority_rows(
        rows: Sequence[Mapping[str, Any]], assignment_field: str | None, label: str
    ) -> dict[tuple[str, str, str, str | None], Mapping[str, Any]]:
        indexed: dict[tuple[str, str, str, str | None], Mapping[str, Any]] = {}
        for authority_row in rows:
            assignment_value: object
            if assignment_field is None:
                if not set(tuple_fields).issubset(authority_row):
                    raise ReportValidationError(f"epoch {label} tuple differs")
                assignment_value = {
                    field: authority_row[field] for field in tuple_fields
                }
            else:
                assignment_value = authority_row.get(assignment_field)
            if not isinstance(assignment_value, dict) or set(assignment_value) != set(
                tuple_fields
            ):
                raise ReportValidationError(f"epoch {label} tuple differs")
            key = cast(
                tuple[str, str, str, str | None],
                tuple(assignment_value[field] for field in tuple_fields),
            )
            if key in indexed:
                raise ReportValidationError(f"epoch {label} tuple differs")
            indexed[key] = authority_row
        if set(indexed) != set(by_key):
            raise ReportValidationError(f"epoch {label} tuple differs")
        return indexed

    calculation_by_key = index_authority_rows(
        calculation_rows, "epoch", "calculation input"
    )
    coverage_by_key = index_authority_rows(coverage_rows, "assignment", "coverage")
    admission_by_key = index_authority_rows(admission_rows, None, "admission")
    assert calculation_by_key
    details: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    numeric_candidate_count = 0
    non_numeric_candidate_count = 0
    for chain in chains:
        if set(chain) != {
            "assignment",
            "coverage",
            "admission",
            "output",
            "numeric_candidate",
        }:
            raise ReportValidationError("epoch blocker chain semantic shape differs")
        assignment = chain.get("assignment")
        if not isinstance(assignment, dict) or set(assignment) != set(tuple_fields):
            raise ReportValidationError("epoch blocker chain tuple differs")
        key = cast(
            tuple[str, str, str, str | None],
            tuple(assignment[field] for field in tuple_fields),
        )
        manifest_row = by_key.get(key)
        admission = chain.get("admission")
        output = chain.get("output")
        coverage = chain.get("coverage")
        coverage_authority = coverage_by_key.get(key)
        admission_authority = admission_by_key.get(key)
        if (
            manifest_row is None
            or not isinstance(coverage, dict)
            or not isinstance(admission, dict)
            or not isinstance(output, dict)
            or coverage_authority is None
            or admission_authority is None
            or coverage
            != {
                "status": coverage_authority.get("status"),
                "blocker_code": coverage_authority.get("blocker_code"),
            }
            or admission
            != {
                "status": admission_authority.get("status"),
                "blocker_code": admission_authority.get("reason"),
            }
            or admission.get("status") != manifest_row.get("admission_status")
            or output.get("status") != manifest_row.get("status")
            or output.get("blocking_reason") != manifest_row.get("blocking_reason")
        ):
            raise ReportValidationError("epoch blocker chain semantic equality differs")
        if chain.get("numeric_candidate") is True:
            if admission != {"status": "FUNDED", "blocker_code": None} or output != {
                "status": "OK",
                "blocking_reason": None,
            }:
                raise ReportValidationError("epoch funded semantic chain differs")
            numeric_candidate_count += 1
        elif chain.get("numeric_candidate") is False:
            if (
                admission.get("status") != "BLOCKED"
                or not isinstance(admission.get("blocker_code"), str)
                or output
                != {
                    "status": "BLOCKED_ADMISSION",
                    "blocking_reason": admission["blocker_code"],
                }
            ):
                raise ReportValidationError("epoch blocked semantic chain differs")
            non_numeric_candidate_count += 1
        else:
            raise ReportValidationError("epoch numeric candidate authority differs")
        details[key] = {
            "account_label": manifest_row.get("account_label"),
            "last_four": manifest_row.get("last_four"),
            "epoch_admission_status": admission["status"],
            "epoch_output_status": output["status"],
            "epoch_blocking_reason": output["blocking_reason"],
        }
    if (
        set(details) != set(by_key)
        or numeric_candidate_count != numeric.get("candidate_count")
        or numeric_candidate_count + non_numeric_candidate_count != len(chains)
    ):
        raise ReportValidationError("epoch verification semantic partition differs")
    return details


def _validate_epoch_generation_authority(
    generation_root: Path,
    source: Mapping[str, Any],
    manifest_bytes: bytes,
    crosswalk_bytes: bytes,
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    """Require the triplet to be an exact payload of an immutable generation."""

    generation_id = source.get("generation_id")
    if not isinstance(generation_id, str):
        raise ReportValidationError("epoch generation root is not verified")
    try:
        bundle = read_generation_bundle(generation_root, generation_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ReportValidationError("epoch generation root is not verified") from exc
    generation_manifest_bytes = bundle.manifest.to_json_bytes()
    if (
        hashlib.sha256(generation_manifest_bytes).hexdigest()
        != source.get("generation_manifest_sha256")
        or hashlib.sha256(bundle.receipt_bytes).hexdigest()
        != source.get("generation_receipt_sha256")
        or bundle.full_directory_inventory_sha256
        != source.get("full_directory_inventory_sha256")
    ):
        raise ReportValidationError("epoch generation root identity differs")
    manifest_payload = json.loads(manifest_bytes)
    if not isinstance(manifest_payload, dict):
        raise ReportValidationError("epoch generation manifest payload differs")
    artifact_specs = {
        "outputs/account-strategy-manifest.json": (
            manifest_bytes,
            "account-strategy-manifest",
            str(manifest_payload["schema_version"]),
        ),
        "authority/account-assignment-crosswalk.json": (
            crosswalk_bytes,
            "account-strategy-current-epoch-crosswalk",
            "1",
        ),
    }
    for artifact_id, (
        expected_bytes,
        schema_name,
        schema_version,
    ) in artifact_specs.items():
        payload = bundle.payloads.get(artifact_id)
        artifact = next(
            (
                item
                for item in bundle.manifest.artifacts
                if item.artifact_id == artifact_id
            ),
            None,
        )
        if (
            type(payload) is not bytes
            or payload != expected_bytes
            or artifact is None
            or artifact.sha256 != hashlib.sha256(expected_bytes).hexdigest()
            or artifact.size_bytes != len(expected_bytes)
            or artifact.schema_name != schema_name
            or artifact.schema_version != schema_version
        ):
            raise ReportValidationError(
                f"epoch generation {artifact_id} authority differs"
            )
    verification_artifact = next(
        (
            item
            for item in bundle.manifest.artifacts
            if item.artifact_id == "verification/independent-verification.json"
        ),
        None,
    )
    calculation_artifact = next(
        (
            item
            for item in bundle.manifest.artifacts
            if item.artifact_id == "authority/calculation-inputs.json"
        ),
        None,
    )
    verification_bytes = bundle.payloads.get(
        "verification/independent-verification.json"
    )
    calculation_bytes = bundle.payloads.get("authority/calculation-inputs.json")
    if (
        verification_artifact is None
        or calculation_artifact is None
        or type(verification_bytes) is not bytes
        or type(calculation_bytes) is not bytes
        or verification_artifact.schema_name
        != "account-manifest-independent-verification"
        or verification_artifact.schema_version != "3"
        or calculation_artifact.schema_name != "account-manifest-calculation-inputs"
        or calculation_artifact.schema_version != "4"
        or verification_artifact.sha256
        != hashlib.sha256(verification_bytes).hexdigest()
        or verification_artifact.size_bytes != len(verification_bytes)
        or calculation_artifact.sha256 != hashlib.sha256(calculation_bytes).hexdigest()
        or calculation_artifact.size_bytes != len(calculation_bytes)
    ):
        raise ReportValidationError("epoch generation verification authority differs")
    try:
        verification_payload = json.loads(verification_bytes)
        calculation_payload = json.loads(calculation_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("epoch generation authority JSON differs") from exc
    if not isinstance(verification_payload, dict) or not isinstance(
        calculation_payload, dict
    ):
        raise ReportValidationError("epoch generation authority payload differs")
    return _verified_epoch_authority_details(
        manifest_payload,
        verification_payload,
        calculation_payload,
        bundle.payloads,
    )


def _require_epoch_manifest_triplet(
    epoch_manifest_path: Path | None,
    epoch_manifest_source_path: Path | None,
    epoch_crosswalk_path: Path | None,
) -> bool:
    """Require all epoch identity inputs together; never silently drop one."""

    supplied = (
        epoch_manifest_path is not None
        or epoch_manifest_source_path is not None
        or epoch_crosswalk_path is not None
    )
    if supplied and not all(
        value is not None
        for value in (
            epoch_manifest_path,
            epoch_manifest_source_path,
            epoch_crosswalk_path,
        )
    ):
        raise ReportValidationError(
            "epoch manifest, source receipt, and current epoch crosswalk are required together"
        )
    return supplied


def _require_epoch_generation_root(
    epoch_supplied: bool, epoch_manifest_generation_root: Path | None
) -> None:
    """Keep verified epoch generation authority inseparable from its triplet."""

    if epoch_supplied and epoch_manifest_generation_root is None:
        raise ReportValidationError(
            "epoch manifest requires an explicit verified generation root"
        )
    if not epoch_supplied and epoch_manifest_generation_root is not None:
        raise ReportValidationError(
            "epoch generation root requires manifest, source receipt, and crosswalk"
        )


def _validate_current_epoch_crosswalk(
    crosswalk: Mapping[str, Any],
    *,
    strategy_product_controls: Mapping[str, Mapping[str, Any]] | None = None,
    baseline_catalog: Mapping[str, Any] | None = None,
    baseline_ref: Mapping[str, Any] | None = None,
    baseline_catalog_path: Path | None = None,
) -> None:
    """Validate the complete crosswalk contract and selected baseline binding."""

    expected_root = {
        "artifact_id",
        "as_of_date",
        "calculation_cutoff_date",
        "disposition_counts",
        "rows",
        "schema_version",
        "selected_baseline",
    }
    if (
        set(crosswalk) != expected_root
        or crosswalk.get("artifact_id") != ("account-strategy-current-epoch-crosswalk")
        or crosswalk.get("schema_version") != 1
    ):
        raise ReportValidationError("current epoch crosswalk schema differs")
    as_of = _date(crosswalk.get("as_of_date"), "current epoch crosswalk.as_of_date")
    cutoff = _date(
        crosswalk.get("calculation_cutoff_date"),
        "current epoch crosswalk.calculation_cutoff_date",
    )
    if cutoff > as_of:
        raise ReportValidationError(
            "current epoch crosswalk calculation cutoff differs"
        )
    rows = _objects(crosswalk.get("rows"), "current epoch crosswalk.rows")
    if len(rows) != 15:
        raise ReportValidationError("current epoch crosswalk must contain 15 rows")
    counts = crosswalk.get("disposition_counts")
    if not isinstance(counts, dict) or counts != {
        "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
        "EXCLUDED_IMMATERIAL": 1,
        "INCLUDED_PRODUCT_EPOCH": 13,
    }:
        raise ReportValidationError("current epoch crosswalk disposition counts differ")
    row_keys = {
        "assignment_id",
        "account_id",
        "provider",
        "strategy_id",
        "effective_start",
        "effective_end",
        "account_label",
        "last_four",
        "product_epoch_disposition",
        "owner_reason",
    }
    assignment_ids: set[str] = set()
    row_identities: set[tuple[Any, ...]] = set()
    for index, row in enumerate(rows):
        if set(row) != row_keys:
            raise ReportValidationError(
                f"current epoch crosswalk row {index} schema differs"
            )
        for key in (
            "assignment_id",
            "account_id",
            "provider",
            "strategy_id",
            "account_label",
        ):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ReportValidationError(
                    f"current epoch crosswalk row {index} is invalid"
                )
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row["assignment_id"]):
            raise ReportValidationError(
                "current epoch crosswalk assignment identity differs"
            )
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row["account_id"]):
            raise ReportValidationError(
                "current epoch crosswalk account identity differs"
            )
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row["strategy_id"]):
            raise ReportValidationError(
                "current epoch crosswalk strategy identity differs"
            )
        if row["assignment_id"] in assignment_ids:
            raise ReportValidationError(
                "current epoch crosswalk assignments are duplicated"
            )
        assignment_ids.add(row["assignment_id"])
        identity = (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        if identity in row_identities:
            raise ReportValidationError("current epoch crosswalk rows are duplicated")
        row_identities.add(identity)
        _date(
            row["effective_start"],
            f"current epoch crosswalk row {index}.effective_start",
        )
        if row["effective_end"] is not None:
            _date(
                row["effective_end"],
                f"current epoch crosswalk row {index}.effective_end",
            )
        if row["last_four"] is not None and not re.fullmatch(
            r"[0-9]{4}", row["last_four"]
        ):
            raise ReportValidationError("current epoch crosswalk last_four differs")
        disposition = row["product_epoch_disposition"]
        if disposition not in {
            "INCLUDED_PRODUCT_EPOCH",
            "EXCLUDED_DEPRECATED_HISTORY_ONLY",
            "EXCLUDED_IMMATERIAL",
        } or not re.fullmatch(r"[A-Z][A-Z0-9_]*", row["owner_reason"]):
            raise ReportValidationError("current epoch crosswalk disposition differs")
        expected_owner_reason = {
            "INCLUDED_PRODUCT_EPOCH": "CURRENT_PRODUCT_EPOCH",
            "EXCLUDED_DEPRECATED_HISTORY_ONLY": (
                "ECHOTRADE_DEPRECATED_EFFECTIVE_2025_11_25"
            ),
            "EXCLUDED_IMMATERIAL": "COMPOSER_OWNER_EXCLUDED_IMMATERIAL",
        }[disposition]
        if row["owner_reason"] != expected_owner_reason:
            raise ReportValidationError(
                "current epoch crosswalk owner authority differs"
            )
        if strategy_product_controls is not None:
            control = strategy_product_controls.get(row["strategy_id"])
            if control is None:
                raise ReportValidationError(
                    "current epoch crosswalk strategy authority differs"
                )
            expected_disposition = (
                "INCLUDED_PRODUCT_EPOCH"
                if control["product_requirement"] == "REQUIRED"
                else (
                    "EXCLUDED_DEPRECATED_HISTORY_ONLY"
                    if control["lifecycle_status"] == "DEPRECATED_HISTORY_ONLY"
                    else "EXCLUDED_IMMATERIAL"
                )
            )
            if disposition != expected_disposition:
                raise ReportValidationError(
                    "current epoch crosswalk disposition authority differs"
                )
    observed_counts = {
        disposition: sum(
            1 for row in rows if row["product_epoch_disposition"] == disposition
        )
        for disposition in counts
    }
    if observed_counts != counts:
        raise ReportValidationError(
            "current epoch crosswalk row disposition counts differ"
        )
    selected = crosswalk.get("selected_baseline")
    if not isinstance(selected, dict) or set(selected) != {
        "artifact_id",
        "as_of_date",
        "generation_id",
        "schema_name",
        "schema_version",
        "sha256",
        "size_bytes",
    }:
        raise ReportValidationError("current epoch crosswalk selected baseline differs")
    if (
        selected["artifact_id"] != "normalized/portfolio-baseline.json"
        or not isinstance(selected["as_of_date"], str)
        or not isinstance(selected["generation_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected["generation_id"])
        or selected["schema_name"] != "portfolio-baseline-catalog"
        or selected["schema_version"] != "1"
        or not isinstance(selected["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected["sha256"])
        or isinstance(selected["size_bytes"], bool)
        or not isinstance(selected["size_bytes"], int)
        or selected["size_bytes"] <= 0
    ):
        raise ReportValidationError(
            "current epoch crosswalk selected baseline fields differ"
        )
    selected_as_of = _date(
        selected["as_of_date"], "current epoch crosswalk selected_baseline.as_of_date"
    )
    if cutoff > selected_as_of or selected_as_of > as_of:
        raise ReportValidationError(
            "current epoch crosswalk selected baseline cutoff differs"
        )
    if (
        baseline_catalog is not None
        and baseline_ref is not None
        and baseline_catalog_path is not None
    ):
        expected_selected = {
            "artifact_id": "normalized/portfolio-baseline.json",
            "as_of_date": _text(
                baseline_catalog.get("as_of_date"), "baseline as_of_date"
            ),
            "generation_id": baseline_ref["generation_id"],
            "schema_name": "portfolio-baseline-catalog",
            "schema_version": "1",
            "sha256": baseline_ref["catalog_sha256"],
            "size_bytes": baseline_catalog_path.stat().st_size,
        }
        if selected != expected_selected:
            raise ReportValidationError(
                "current epoch crosswalk selected baseline binding differs"
            )
        assignments = {
            row.get("assignment_id"): row
            for row in _objects(
                baseline_catalog.get("assignments"), "baseline.assignments"
            )
        }
        accounts = {
            row.get("account_id"): row
            for row in _objects(baseline_catalog.get("accounts"), "baseline.accounts")
        }
        for row in rows:
            assignment = assignments.get(row["assignment_id"])
            account = accounts.get(row["account_id"])
            if (
                assignment is None
                or account is None
                or any(
                    assignment.get(key) != row[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                )
                or account.get("provider") != row["provider"]
            ):
                raise ReportValidationError(
                    "current epoch crosswalk is not bound to baseline authority"
                )


def _repo_bound_workbook_parity_receipt(
    report_bytes: bytes, workbook_bytes: bytes
) -> dict[str, Any]:
    """Record only the byte association of a workbook and report sidecar."""
    return {
        "parity_schema_version": WORKBOOK_PARITY_VERSION,
        "parity_status": "REPO_BOUND_POSTRUN_ONLY",
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "workbook_sha256": hashlib.sha256(workbook_bytes).hexdigest(),
    }


def bind_workbook_evidence(
    candidate_dir: Path,
    workbook: Path,
    xlsx_result: Path,
    validation_result: Path,
    evidence_path: Path | None = None,
    epoch_manifest_path: Path | None = None,
    epoch_manifest_source_path: Path | None = None,
    epoch_crosswalk_path: Path | None = None,
    baseline_root: Path | None = None,
    epoch_manifest_generation_root: Path | None = None,
    baseline_generation_id: str | None = None,
) -> dict[str, Any]:
    """Bind validated workbook QA evidence for non-authority XLSX delivery."""
    if (baseline_root is None) != (baseline_generation_id is None):
        raise ReportValidationError(
            "baseline root and generation identity are required together"
        )
    epoch_supplied = _require_epoch_manifest_triplet(
        epoch_manifest_path, epoch_manifest_source_path, epoch_crosswalk_path
    )
    _require_epoch_generation_root(epoch_supplied, epoch_manifest_generation_root)
    catalog: Mapping[str, Any] | None = None
    baseline_ref: Mapping[str, Any] | None = None
    baseline_catalog_path: Path | None = None
    controls: Mapping[str, Mapping[str, Any]] | None = None
    if baseline_root is not None:
        catalog, baseline_ref, baseline_catalog_path = _baseline(
            baseline_root, baseline_generation_id
        )
        controls, _ = _report_product_controls(catalog)
        if _is_governed_financial_mvp2_path(catalog, controls) and not epoch_supplied:
            raise ReportValidationError(
                "financial MVP-2 workbook binding requires manifest, source receipt, crosswalk, and verified generation root"
            )
    candidate_report = _load_json(
        candidate_dir / REPORT_DATA_NAME, "candidate report data"
    )
    if _is_governed_report_data(candidate_report) and not epoch_supplied:
        raise ReportValidationError(
            "financial MVP-2 workbook binding requires manifest, source receipt, crosswalk, and verified generation root"
        )
    if epoch_supplied:
        if baseline_root is None:
            raise ReportValidationError(
                "epoch manifest binding requires the selected baseline root"
            )
        assert baseline_root is not None
        assert catalog is not None and controls is not None
        _validate_epoch_manifest_input(
            epoch_manifest_path,
            epoch_manifest_source_path,
            controls,
            current_account_ids=_current_product_account_ids(catalog, controls),
            crosswalk_path=epoch_crosswalk_path,
            baseline_catalog=catalog,
            baseline_ref=baseline_ref,
            baseline_catalog_path=baseline_catalog_path,
            epoch_manifest_generation_root=epoch_manifest_generation_root,
        )
    paths = {
        "initial_workbook": candidate_dir / WORKBOOK_NAME,
        "workbook": workbook,
        "report_data": candidate_dir / REPORT_DATA_NAME,
        "xlsx_result": xlsx_result,
        "validation_result": validation_result,
        "workbook_contract": candidate_dir / "workbook-contract.json",
        "xlsx_job": candidate_dir / "xlsx-job.json",
        "workbook_canonicalization": candidate_dir / "workbook-canonicalization.json",
        "xlsx_result_canonicalization": candidate_dir
        / "xlsx-result-canonicalization.json",
    }
    capital_paths = {
        "invested_capital_result": candidate_dir / INVESTED_CAPITAL_RESULT_NAME,
        "invested_capital_input_generation": candidate_dir
        / INVESTED_CAPITAL_INPUT_NAME,
    }
    capital_exists = [path.is_file() for path in capital_paths.values()]
    if any(capital_exists) and not all(capital_exists):
        raise ReportValidationError("candidate invested-capital artifacts differ")
    if all(capital_exists):
        paths.update(capital_paths)
    snapshots = {
        name: _regular_file_bytes(path, name.replace("_", " "))
        for name, path in paths.items()
    }
    snapshots.update(
        {
            name: content
            for name, (_path, content) in _epoch_manifest_workbook_snapshots(
                candidate_dir,
                epoch_manifest_path,
                epoch_manifest_source_path,
                epoch_crosswalk_path,
            ).items()
        }
    )
    parity = _repo_bound_workbook_parity_receipt(
        snapshots["report_data"], snapshots["workbook"]
    )
    parity_content = canonical_json_bytes(parity)
    parity_path = candidate_dir / "workbook-parity.json"
    _write_exact(parity_path, parity_content)
    snapshots["workbook_parity"] = parity_content
    binding = _workbook_evidence_binding_from_snapshots(
        snapshots,
        candidate_dir=candidate_dir,
        final_workbook=workbook,
    )
    binding["workbook_delivery"] = _workbook_delivery_evidence(snapshots)
    if evidence_path is not None:
        _write_exact(evidence_path, canonical_json_bytes(binding))
    return binding


def _is_governed_report_data(report: Mapping[str, Any]) -> bool:
    """Recognize a report already carrying the real current product perimeter."""

    rows = report.get("strategy_summary")
    return (
        isinstance(rows, list)
        and len(rows) == len(CURRENT_REQUIRED_STRATEGY_IDS)
        and {row.get("strategy_id") for row in rows if isinstance(row, Mapping)}
        == CURRENT_REQUIRED_STRATEGY_IDS
        and isinstance(report.get("baseline"), Mapping)
    )


def _workbook_delivery_evidence(snapshots: Mapping[str, bytes]) -> dict[str, Any]:
    """Derive closed, non-authority delivery evidence from supplied QA artifacts."""

    try:
        run = json.loads(snapshots["xlsx_result"])
        validation = json.loads(snapshots["validation_result"])
        job = json.loads(snapshots["xlsx_job"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("workbook delivery evidence is invalid") from exc
    if not all(isinstance(value, dict) for value in (run, validation, job)):
        raise ReportValidationError("workbook delivery evidence differs")
    _successful_workbook_evidence(run, validation, job)
    run_id = run.get("run_id")
    result_schema_version = run.get("schema_version")
    idempotency_key = run.get("idempotency_key")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(result_schema_version, str)
        or not result_schema_version
        or not isinstance(idempotency_key, str)
        or not idempotency_key
        or idempotency_key != job.get("idempotency_key")
    ):
        raise ReportValidationError("workbook delivery run identity differs")
    invariants = validation["invariants"]
    assert isinstance(invariants, list)
    steps = run["steps"]
    assert isinstance(steps, list)
    return {
        "delivery_evidence_schema_version": WORKBOOK_DELIVERY_EVIDENCE_VERSION,
        "engine": {
            "idempotency_key": idempotency_key,
            "result_schema_version": result_schema_version,
            "run_id": run_id,
        },
        "final_workbook": {
            "sha256": hashlib.sha256(snapshots["workbook"]).hexdigest(),
            "size_bytes": len(snapshots["workbook"]),
        },
        "steps": [
            {
                "status": step["status"],
                "step_index": step["step_index"],
                "type": step["type"],
            }
            for step in steps
            if isinstance(step, dict)
        ],
        "validation": {
            "all_passed": True,
            "invariant_count": len(invariants),
            "passed_invariant_count": len(invariants),
        },
        "validation_result": {
            "sha256": hashlib.sha256(snapshots["validation_result"]).hexdigest(),
            "size_bytes": len(snapshots["validation_result"]),
        },
        "xlsx_result": {
            "sha256": hashlib.sha256(snapshots["xlsx_result"]).hexdigest(),
            "size_bytes": len(snapshots["xlsx_result"]),
        },
    }


def _validated_workbook_evidence_snapshots(
    candidate_dir: Path,
    workbook: Path,
    xlsx_result: Path,
    validation_result: Path,
    binding_path: Path | None,
    epoch_manifest_path: Path | None = None,
    epoch_manifest_source_path: Path | None = None,
    epoch_crosswalk_path: Path | None = None,
) -> dict[str, tuple[Path, bytes]]:
    if binding_path is None:
        raise ReportValidationError("workbook evidence binding is required")
    paths = {
        "initial_workbook": candidate_dir / WORKBOOK_NAME,
        "workbook": workbook,
        "report_data": candidate_dir / REPORT_DATA_NAME,
        "xlsx_result": xlsx_result,
        "validation_result": validation_result,
        "workbook_contract": candidate_dir / "workbook-contract.json",
        "xlsx_job": candidate_dir / "xlsx-job.json",
        "workbook_canonicalization": candidate_dir / "workbook-canonicalization.json",
        "xlsx_result_canonicalization": candidate_dir
        / "xlsx-result-canonicalization.json",
        "workbook_parity": candidate_dir / "workbook-parity.json",
    }
    capital_paths = {
        "invested_capital_result": candidate_dir / INVESTED_CAPITAL_RESULT_NAME,
        "invested_capital_input_generation": candidate_dir
        / INVESTED_CAPITAL_INPUT_NAME,
    }
    capital_exists = [path.is_file() for path in capital_paths.values()]
    if any(capital_exists) and not all(capital_exists):
        raise ReportValidationError("candidate invested-capital artifacts differ")
    if all(capital_exists):
        paths.update(capital_paths)
    snapshots = {
        name: (path, _regular_file_bytes(path, name.replace("_", " ")))
        for name, path in paths.items()
    }
    snapshots.update(
        _epoch_manifest_workbook_snapshots(
            candidate_dir,
            epoch_manifest_path,
            epoch_manifest_source_path,
            epoch_crosswalk_path,
        )
    )
    binding_content = _regular_file_bytes(binding_path, "workbook evidence binding")
    try:
        binding = json.loads(binding_content)
        run = json.loads(snapshots["xlsx_result"][1])
        validation = json.loads(snapshots["validation_result"][1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("workbook evidence binding is invalid") from exc
    if not all(isinstance(value, dict) for value in (binding, run, validation)):
        raise ReportValidationError("workbook evidence binding differs")
    try:
        expected = _workbook_evidence_binding_from_snapshots(
            {name: content for name, (_path, content) in snapshots.items()},
            candidate_dir=candidate_dir,
            final_workbook=workbook,
        )
    except ReportValidationError as exc:
        raise ReportValidationError("workbook evidence binding differs") from exc
    expected["workbook_delivery"] = _workbook_delivery_evidence(
        {name: content for name, (_path, content) in snapshots.items()}
    )
    if binding != expected:
        raise ReportValidationError("workbook evidence binding differs")
    snapshots["workbook_evidence_binding"] = (binding_path, binding_content)
    return snapshots


def _verification_from_snapshots(
    report: Mapping[str, Any],
    report_bytes: bytes,
    candidate_dir: Path,
    candidate_report_bytes: bytes,
    workbook_path: Path,
    workbook_bytes: bytes,
) -> dict[str, Any]:
    report_id = hashlib.sha256(report_bytes).hexdigest()
    if candidate_dir.name != report_id:
        raise ReportValidationError("candidate directory identity differs")
    if candidate_report_bytes != report_bytes:
        raise ReportValidationError(
            "candidate report data differs from current baseline"
        )
    if _regular_file_bytes(workbook_path, "Excel workbook") != workbook_bytes:
        raise ReportValidationError("Excel workbook snapshot differs")
    workbook_evidence = verify_workbook(workbook_path)
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


def verify_candidate(
    baseline_root: Path,
    candidate_dir: Path,
    excel_workbook: Path,
    freshness_health_path: Path | None = None,
    provider_ledger_root: Path | None = None,
    attribution_root: Path | None = None,
    history_contract_path: Path | None = None,
    implementation_reconciliation_path: Path | None = None,
    implementation_return_attribution_path: Path | None = None,
    hwm_portable_receipt: Mapping[str, Any] | None = None,
    correlation_root: Path | None = None,
    hwm_portable_generation_bytes: bytes | None = None,
    hwm_portable_calendar_bytes: bytes | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    epoch_manifest_path: Path | None = None,
    epoch_manifest_source_path: Path | None = None,
    epoch_crosswalk_path: Path | None = None,
    epoch_manifest_generation_root: Path | None = None,
    baseline_generation_id: str | None = None,
    correlation_generation_dir: Path | None = None,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    invested_capital_result_bytes: bytes | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
) -> dict[str, Any]:
    epoch_snapshots = _epoch_manifest_workbook_snapshots(
        candidate_dir,
        epoch_manifest_path,
        epoch_manifest_source_path,
        epoch_crosswalk_path,
    )
    _require_epoch_generation_root(
        bool(epoch_snapshots), epoch_manifest_generation_root
    )
    # Once supplied epoch evidence is bound into the candidate, rebuild from
    # those exact candidate bytes. External paths are inputs to the binding
    # check, not an alternate source of report identity.
    effective_epoch_manifest_path = (
        candidate_dir / EPOCH_MANIFEST_WORKBOOK_NAME
        if epoch_snapshots
        else epoch_manifest_path
    )
    effective_epoch_manifest_source_path = (
        candidate_dir / EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME
        if epoch_snapshots
        else epoch_manifest_source_path
    )
    effective_epoch_crosswalk_path = (
        candidate_dir / EPOCH_CROSSWALK_WORKBOOK_NAME
        if epoch_snapshots
        else epoch_crosswalk_path
    )
    report, report_bytes, _ = build_report_data(
        baseline_root,
        freshness_health_path,
        provider_ledger_root,
        attribution_root,
        history_contract_path,
        implementation_reconciliation_path,
        implementation_return_attribution_path,
        hwm_portable_receipt,
        correlation_root,
        hwm_portable_generation_bytes,
        hwm_portable_calendar_bytes,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        invested_capital_result=invested_capital_result,
        invested_capital_input_generation_path=(invested_capital_input_generation_path),
        epoch_manifest_path=effective_epoch_manifest_path,
        epoch_manifest_source_path=effective_epoch_manifest_source_path,
        epoch_crosswalk_path=effective_epoch_crosswalk_path,
        epoch_manifest_generation_root=epoch_manifest_generation_root,
        baseline_generation_id=baseline_generation_id,
        correlation_generation_dir=correlation_generation_dir,
        decomposition_lenses_root=decomposition_lenses_root,
        decomposition_plan_bundle=decomposition_plan_bundle,
        hybrid_decomposition_plan_bundle=hybrid_decomposition_plan_bundle,
    )
    data_path = candidate_dir / REPORT_DATA_NAME
    candidate_report_bytes = _regular_file_bytes(data_path, "candidate report data")
    if invested_capital_result is not None:
        assert invested_capital_input_generation_path is not None
        expected_result_bytes = (
            invested_capital_result_bytes
            if invested_capital_result_bytes is not None
            else canonical_json_bytes(dict(invested_capital_result))
        )
        if (
            _regular_file_bytes(
                candidate_dir / INVESTED_CAPITAL_RESULT_NAME,
                "candidate invested-capital result",
            )
            != expected_result_bytes
        ):
            raise ReportValidationError("candidate invested-capital result differs")
        if _regular_file_bytes(
            candidate_dir / INVESTED_CAPITAL_INPUT_NAME,
            "candidate invested-capital input generation",
        ) != _regular_file_bytes(
            invested_capital_input_generation_path,
            "invested-capital input generation",
        ):
            raise ReportValidationError(
                "candidate invested-capital input generation differs"
            )
    workbook_bytes = _regular_file_bytes(excel_workbook, "Excel workbook")
    verification = _verification_from_snapshots(
        report,
        report_bytes,
        candidate_dir,
        candidate_report_bytes,
        excel_workbook,
        workbook_bytes,
    )
    if epoch_snapshots:
        verification.update(
            {
                "epoch_manifest_sha256": hashlib.sha256(
                    epoch_snapshots["epoch_manifest"][1]
                ).hexdigest(),
                "epoch_manifest_size_bytes": len(epoch_snapshots["epoch_manifest"][1]),
                "epoch_manifest_source_sha256": hashlib.sha256(
                    epoch_snapshots["epoch_manifest_source"][1]
                ).hexdigest(),
                "epoch_crosswalk_sha256": hashlib.sha256(
                    epoch_snapshots["epoch_crosswalk"][1]
                ).hexdigest(),
                "epoch_crosswalk_size_bytes": len(
                    epoch_snapshots["epoch_crosswalk"][1]
                ),
            }
        )
    return verification


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


def _report_delivery_receipt(
    *,
    generation_id: str,
    generation_manifest_bytes: bytes,
    generation_inventory_sha256_value: str,
    report_data_bytes: bytes,
    workbook_bytes: bytes,
    epoch_crosswalk_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Bind a mutable human workbook delivery to immutable report JSON authority."""

    receipt: dict[str, Any] = {
        "authority": {
            "generation_id": generation_id,
            "generation_inventory_sha256": generation_inventory_sha256_value,
            "generation_manifest_sha256": hashlib.sha256(
                generation_manifest_bytes
            ).hexdigest(),
            "generation_manifest_size_bytes": len(generation_manifest_bytes),
            "payload": {
                "relative_path": REPORT_DATA_NAME,
                "sha256": hashlib.sha256(report_data_bytes).hexdigest(),
                "size_bytes": len(report_data_bytes),
            },
        },
        "delivery_classification": NON_AUTHORITY_HUMAN_DELIVERY,
        "schema_version": REPORT_DELIVERY_SCHEMA_VERSION,
        "workbook": {
            "relative_path": WORKBOOK_NAME,
            "sha256": hashlib.sha256(workbook_bytes).hexdigest(),
            "size_bytes": len(workbook_bytes),
        },
    }
    if epoch_crosswalk_bytes is not None:
        receipt["epoch_crosswalk"] = {
            "artifact": "account-strategy-current-epoch-crosswalk",
            "generation_id": generation_id,
            "relative_path": PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH,
            "sha256": hashlib.sha256(epoch_crosswalk_bytes).hexdigest(),
            "size_bytes": len(epoch_crosswalk_bytes),
        }
    return receipt


def inspect_report_delivery(
    authority_root: Path, generation_id: str, delivery_dir: Path
) -> dict[str, Any]:
    """Report delivery-workbook drift without treating the workbook as authority."""

    receipt_bytes = _regular_file_bytes(
        delivery_dir / REPORT_DELIVERY_NAME, "report delivery receipt"
    )
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("report delivery receipt is invalid JSON") from exc
    if not isinstance(receipt, dict) or canonical_json_bytes(receipt) != receipt_bytes:
        raise ReportValidationError("report delivery receipt root differs")
    if set(receipt) not in (
        {"authority", "delivery_classification", "schema_version", "workbook"},
        {
            "authority",
            "delivery_classification",
            "schema_version",
            "workbook",
            "epoch_crosswalk",
        },
    ):
        raise ReportValidationError("report delivery receipt structure differs")
    authority = receipt.get("authority")
    workbook = receipt.get("workbook")
    if (
        receipt.get("schema_version") != REPORT_DELIVERY_SCHEMA_VERSION
        or receipt.get("delivery_classification") != NON_AUTHORITY_HUMAN_DELIVERY
        or not isinstance(authority, dict)
        or not isinstance(workbook, dict)
        or set(workbook) != {"relative_path", "sha256", "size_bytes"}
        or workbook.get("relative_path") != WORKBOOK_NAME
        or not isinstance(workbook.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", workbook["sha256"]) is None
        or isinstance(workbook.get("size_bytes"), bool)
        or not isinstance(workbook.get("size_bytes"), int)
        or workbook["size_bytes"] < 0
    ):
        raise ReportValidationError("report delivery receipt structure differs")
    manifest = read_generation(authority_root, generation_id)
    manifest_path = authority_root / "generations" / generation_id / "generation.json"
    manifest_bytes = _regular_file_bytes(manifest_path, "report generation manifest")
    if manifest.to_json_bytes() != manifest_bytes:
        raise ReportValidationError("report generation manifest differs")
    report_data_bytes = _regular_file_bytes(
        authority_root / "generations" / generation_id / REPORT_DATA_NAME,
        "report authority payload",
    )
    expected_payload = {
        "relative_path": REPORT_DATA_NAME,
        "sha256": hashlib.sha256(report_data_bytes).hexdigest(),
        "size_bytes": len(report_data_bytes),
    }
    expected_authority = {
        "generation_id": generation_id,
        "generation_inventory_sha256": generation_inventory_sha256(
            authority_root, generation_id
        ),
        "generation_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "generation_manifest_size_bytes": len(manifest_bytes),
        "payload": expected_payload,
    }
    if authority != expected_authority:
        raise ReportValidationError("report delivery receipt authority binding differs")
    epoch_crosswalk = receipt.get("epoch_crosswalk")
    if epoch_crosswalk is not None:
        if (
            not isinstance(epoch_crosswalk, dict)
            or set(epoch_crosswalk)
            != {"artifact", "generation_id", "relative_path", "sha256", "size_bytes"}
            or epoch_crosswalk.get("artifact")
            != "account-strategy-current-epoch-crosswalk"
            or epoch_crosswalk.get("generation_id") != generation_id
            or epoch_crosswalk.get("relative_path")
            != PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH
            or not isinstance(epoch_crosswalk.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", epoch_crosswalk["sha256"]) is None
            or isinstance(epoch_crosswalk.get("size_bytes"), bool)
            or not isinstance(epoch_crosswalk.get("size_bytes"), int)
            or epoch_crosswalk["size_bytes"] < 1
        ):
            raise ReportValidationError("report delivery crosswalk binding differs")
        generation_crosswalk = _regular_file_bytes(
            authority_root
            / "generations"
            / generation_id
            / PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH,
            "report generation epoch crosswalk",
        )
        if (
            hashlib.sha256(generation_crosswalk).hexdigest()
            != epoch_crosswalk["sha256"]
            or len(generation_crosswalk) != epoch_crosswalk["size_bytes"]
        ):
            raise ReportValidationError("report delivery crosswalk binding differs")
    actual_workbook = _regular_file_bytes(
        delivery_dir / WORKBOOK_NAME, "report delivery workbook"
    )
    actual_sha256 = hashlib.sha256(actual_workbook).hexdigest()
    result = {
        "actual_workbook_sha256": actual_sha256,
        "actual_workbook_size_bytes": len(actual_workbook),
        "delivery_classification": NON_AUTHORITY_HUMAN_DELIVERY,
        "expected_workbook_sha256": workbook["sha256"],
        "expected_workbook_size_bytes": workbook["size_bytes"],
        "generation_inventory_sha256": expected_authority[
            "generation_inventory_sha256"
        ],
        "generation_manifest_sha256": expected_authority["generation_manifest_sha256"],
        "generation_manifest_size_bytes": expected_authority[
            "generation_manifest_size_bytes"
        ],
        "generation_id": generation_id,
        "report_data_sha256": expected_payload["sha256"],
        "report_data_size_bytes": expected_payload["size_bytes"],
        "workbook_status": (
            "EXACT_NON_AUTHORITY"
            if (
                actual_sha256 == workbook["sha256"]
                and len(actual_workbook) == workbook["size_bytes"]
            )
            else "DRIFTED_NON_AUTHORITY"
        ),
    }
    if epoch_crosswalk is not None:
        result.update(
            {
                "epoch_crosswalk_sha256": epoch_crosswalk["sha256"],
                "epoch_crosswalk_size_bytes": epoch_crosswalk["size_bytes"],
            }
        )
    return result


def _preflight_report_delivery(
    delivery_dir: Path, workbook_bytes: bytes, receipt_bytes: bytes
) -> None:
    """Reject known delivery conflicts before creating either delivery file."""

    states: list[str] = []
    for path, expected in (
        (delivery_dir / WORKBOOK_NAME, workbook_bytes),
        (delivery_dir / REPORT_DELIVERY_NAME, receipt_bytes),
    ):
        if not path.exists():
            states.append("absent")
        elif path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            states.append("different")
        else:
            states.append("exact")
    if "different" in states:
        raise GenerationConflictError("report delivery target differs")


def _tree_file_names(root: Path) -> tuple[str, ...]:
    if root.is_symlink() or not root.is_dir():
        raise ReportValidationError("publication source tree is missing or linked")
    names: list[str] = []
    for directory_name, directory_names, file_names in os.walk(root, followlinks=False):
        directory = Path(directory_name)
        for name in directory_names:
            if (directory / name).is_symlink():
                raise ReportValidationError(
                    "publication source tree contains a linked directory"
                )
        for name in file_names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise ReportValidationError(
                    "publication source tree contains a linked or irregular file"
                )
            names.append(path.relative_to(root).as_posix())
    return tuple(sorted(names))


def _remember_snapshot(
    originals: dict[Path, bytes], source: Path, content: bytes
) -> None:
    resolved = source.resolve()
    prior = originals.get(resolved)
    if prior is not None and prior != content:
        raise ReportValidationError("publication source changed while snapshotting")
    originals[resolved] = content


def _snapshot_file(
    source: Path,
    destination: Path,
    originals: dict[Path, bytes],
    field: str,
) -> Path:
    content = _regular_file_bytes(source, field)
    _remember_snapshot(originals, source, content)
    _write_exact(destination, content)
    return destination


def _snapshot_tree(
    source: Path,
    destination: Path,
    originals: dict[Path, bytes],
    inventories: dict[Path, tuple[str, ...]],
) -> Path:
    names = _tree_file_names(source)
    inventories[source.resolve()] = names
    for relative in names:
        _snapshot_file(
            source / Path(relative),
            destination / Path(relative),
            originals,
            "publication source file",
        )
    return destination


def _snapshot_tree_preserving_root_identity(
    source: Path,
    destination_parent: Path,
    originals: dict[Path, bytes],
    inventories: dict[Path, tuple[str, ...]],
) -> Path:
    """Copy a bundle without changing its identity-bearing root directory name."""

    return _snapshot_tree(
        source,
        destination_parent / source.name,
        originals,
        inventories,
    )


def _snapshot_current_root(
    source: Path,
    destination: Path,
    originals: dict[Path, bytes],
    inventories: dict[Path, tuple[str, ...]],
) -> Path:
    pointer_content = _regular_file_bytes(source / "CURRENT.json", "current pointer")
    try:
        pointer = json.loads(pointer_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("current pointer is not readable JSON") from exc
    generation_id = pointer.get("generation_id") if isinstance(pointer, dict) else None
    if (
        not isinstance(generation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", generation_id) is None
    ):
        raise ReportValidationError("current pointer generation identity differs")
    _remember_snapshot(originals, source / "CURRENT.json", pointer_content)
    _write_exact(destination / "CURRENT.json", pointer_content)
    _snapshot_tree(
        source / "generations" / generation_id,
        destination / "generations" / generation_id,
        originals,
        inventories,
    )
    try:
        read_current(destination)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError(
            "snapshotted current source is not accepted"
        ) from exc
    return destination


def _snapshot_selected_generation_root(
    source_generation_dir: Path,
    destination: Path,
    originals: dict[Path, bytes],
    inventories: dict[Path, tuple[str, ...]],
) -> tuple[Path, Path]:
    """Snapshot one immutable generation without consulting or creating CURRENT."""

    generation_id = source_generation_dir.name
    if (
        source_generation_dir.parent.name != "generations"
        or re.fullmatch(r"[0-9a-f]{64}", generation_id) is None
    ):
        raise ReportValidationError("selected generation layout differs")
    destination_generation = destination / "generations" / generation_id
    _snapshot_tree(
        source_generation_dir,
        destination_generation,
        originals,
        inventories,
    )
    try:
        read_generation(destination, generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReportValidationError(
            "snapshotted selected generation is not accepted"
        ) from exc
    return destination, destination_generation


def _epoch_source_generation_id(source_bytes: bytes) -> str:
    try:
        source = json.loads(source_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("epoch manifest source history is invalid") from exc
    if not isinstance(source, dict):
        raise ReportValidationError("epoch manifest source history differs")
    generation_id = source.get("generation_id")
    if (
        not isinstance(generation_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", generation_id) is None
    ):
        raise ReportValidationError("epoch manifest source history differs")
    return generation_id


def _epoch_history_snapshot_evidence(
    epoch_root: Path,
    source_bytes: bytes,
    expected_manifest_bytes: bytes,
    expected_crosswalk_bytes: bytes,
) -> tuple[Path, bytes, str, str]:
    """Read the exact independent verifier from an explicit epoch bundle root."""

    source = json.loads(source_bytes)
    assert isinstance(source, dict)
    generation_id = _epoch_source_generation_id(source_bytes)
    try:
        bundle = read_generation_bundle(epoch_root, generation_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ReportValidationError("epoch history bundle is not verified") from exc
    manifest_bytes = bundle.manifest.to_json_bytes()
    if (
        bundle.payloads.get("outputs/account-strategy-manifest.json")
        != expected_manifest_bytes
        or hashlib.sha256(manifest_bytes).hexdigest()
        != source.get("generation_manifest_sha256")
        or hashlib.sha256(bundle.receipt_bytes).hexdigest()
        != source.get("generation_receipt_sha256")
        or bundle.full_directory_inventory_sha256
        != source.get("full_directory_inventory_sha256")
    ):
        raise ReportValidationError("epoch history bundle authority differs")
    crosswalk_artifact = source.get("crosswalk")
    if not isinstance(crosswalk_artifact, dict):
        raise ReportValidationError("epoch history crosswalk authority differs")
    crosswalk_payload = bundle.payloads.get(str(crosswalk_artifact.get("path")))
    if (
        type(crosswalk_payload) is not bytes
        or crosswalk_payload != expected_crosswalk_bytes
        or hashlib.sha256(crosswalk_payload).hexdigest()
        != crosswalk_artifact.get("sha256")
        or len(crosswalk_payload) != crosswalk_artifact.get("size_bytes")
    ):
        raise ReportValidationError("epoch history crosswalk authority differs")
    crosswalk_manifest_artifact = next(
        (
            item
            for item in bundle.manifest.artifacts
            if item.artifact_id
            in {
                "account-strategy-current-epoch-crosswalk",
                str(crosswalk_artifact.get("path")),
            }
        ),
        None,
    )
    if (
        crosswalk_manifest_artifact is None
        or crosswalk_manifest_artifact.sha256 != crosswalk_artifact.get("sha256")
        or crosswalk_manifest_artifact.size_bytes
        != crosswalk_artifact.get("size_bytes")
        or crosswalk_manifest_artifact.schema_name
        != "account-strategy-current-epoch-crosswalk"
        or crosswalk_manifest_artifact.schema_version != "1"
    ):
        raise ReportValidationError("epoch history crosswalk artifact differs")
    try:
        manifest_subject = json.loads(expected_manifest_bytes)
        crosswalk_subject = json.loads(expected_crosswalk_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError(
            "epoch history subject authority is invalid"
        ) from exc
    if not isinstance(manifest_subject, dict) or not isinstance(
        crosswalk_subject, dict
    ):
        raise ReportValidationError("epoch history subject authority differs")
    if set(crosswalk_subject) != {
        "artifact_id",
        "as_of_date",
        "calculation_cutoff_date",
        "disposition_counts",
        "rows",
        "schema_version",
        "selected_baseline",
    }:
        raise ReportValidationError("epoch history crosswalk shape differs")
    crosswalk_rows = crosswalk_subject.get("rows")
    manifest_rows = manifest_subject.get("rows")
    if (
        crosswalk_subject.get("artifact_id")
        != "account-strategy-current-epoch-crosswalk"
        or crosswalk_subject.get("schema_version") != 1
        or not isinstance(crosswalk_rows, list)
        or len(crosswalk_rows) != 15
        or not isinstance(manifest_rows, list)
        or len(manifest_rows) != 13
    ):
        raise ReportValidationError("epoch history subject cardinality differs")
    expected_row_keys = {
        "account_id",
        "account_label",
        "assignment_id",
        "effective_end",
        "effective_start",
        "last_four",
        "owner_reason",
        "product_epoch_disposition",
        "provider",
        "strategy_id",
    }
    if any(
        not isinstance(row, dict) or not expected_row_keys <= set(row)
        for row in crosswalk_rows
    ):
        raise ReportValidationError("epoch history crosswalk rows differ")
    tuple_fields = ("account_id", "strategy_id", "effective_start", "effective_end")
    included = {
        tuple(row[field] for field in tuple_fields)
        for row in crosswalk_rows
        if row.get("product_epoch_disposition") == "INCLUDED_PRODUCT_EPOCH"
    }
    manifest_tuples = {
        tuple(row.get(field) for field in tuple_fields)
        for row in manifest_rows
        if isinstance(row, dict)
    }
    if len(included) != 13 or manifest_tuples != included:
        raise ReportValidationError("epoch history subject tuples differ")
    artifact = next(
        (
            item
            for item in bundle.manifest.artifacts
            if item.artifact_id == "verification/independent-verification.json"
        ),
        None,
    )
    payload = bundle.payloads.get("verification/independent-verification.json")
    if (
        artifact is None
        or type(payload) is not bytes
        or artifact.schema_name != "account-manifest-independent-verification"
        or artifact.schema_version != "3"
        or hashlib.sha256(payload).hexdigest() != artifact.sha256
        or len(payload) != artifact.size_bytes
    ):
        raise ReportValidationError("epoch history verification artifact differs")
    calculation_artifact = next(
        (
            item
            for item in bundle.manifest.artifacts
            if item.artifact_id == "authority/calculation-inputs.json"
        ),
        None,
    )
    calculation_payload_bytes = bundle.payloads.get("authority/calculation-inputs.json")
    if (
        calculation_artifact is None
        or type(calculation_payload_bytes) is not bytes
        or calculation_artifact.schema_name != "account-manifest-calculation-inputs"
        or calculation_artifact.schema_version != "4"
        or calculation_artifact.sha256
        != hashlib.sha256(calculation_payload_bytes).hexdigest()
        or calculation_artifact.size_bytes != len(calculation_payload_bytes)
    ):
        raise ReportValidationError("epoch history calculation authority differs")
    try:
        verification_payload = json.loads(payload)
        calculation_payload = json.loads(calculation_payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError("epoch history semantic authority differs") from exc
    if not isinstance(verification_payload, dict) or not isinstance(
        calculation_payload, dict
    ):
        raise ReportValidationError("epoch history semantic authority differs")
    _verified_epoch_authority_details(
        manifest_subject,
        verification_payload,
        calculation_payload,
        bundle.payloads,
    )
    snapshot_path = (
        epoch_root
        / "generations"
        / generation_id
        / "verification"
        / "independent-verification.json"
    )
    return snapshot_path, payload, artifact.schema_name, artifact.schema_version


def _recheck_snapshot_identities(
    originals: Mapping[Path, bytes],
    inventories: Mapping[Path, tuple[str, ...]],
) -> None:
    for source, expected_names in inventories.items():
        try:
            observed_names = _tree_file_names(source)
        except ReportValidationError as exc:
            raise ReportValidationError(
                "source identity drift before publication"
            ) from exc
        if observed_names != expected_names:
            raise ReportValidationError("source identity drift before publication")
    for source, expected_content in originals.items():
        try:
            observed_content = _regular_file_bytes(source, "publication source")
        except ReportValidationError as exc:
            raise ReportValidationError(
                "source identity drift before publication"
            ) from exc
        if observed_content != expected_content:
            raise ReportValidationError("source identity drift before publication")


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
    history_contract_path: Path | None = None,
    implementation_reconciliation_path: Path | None = None,
    implementation_return_attribution_path: Path | None = None,
    correlation_root: Path | None = None,
    workbook_evidence_binding: Path | None = None,
    epoch_manifest_path: Path | None = None,
    epoch_manifest_source_path: Path | None = None,
    epoch_manifest_generation_root: Path | None = None,
    epoch_crosswalk_path: Path | None = None,
    baseline_generation_id: str | None = None,
    correlation_generation_dir: Path | None = None,
    hwm_governed_result: Mapping[str, Any] | None = None,
    hwm_governed_generation_bytes: bytes | None = None,
    hwm_governed_calendar_bytes: bytes | None = None,
    hwm_governed_receipt_bytes: bytes | None = None,
    invested_capital_result: Mapping[str, Any] | None = None,
    invested_capital_input_generation_path: Path | None = None,
    invested_capital_result_bytes: bytes | None = None,
    decomposition_lenses_root: Path | None = None,
    decomposition_plan_bundle: Path | None = None,
    private_stage_root: Path | None = None,
    portfolio_root: Path | None = None,
    hybrid_decomposition_plan_bundle: Path | None = None,
) -> dict[str, Any]:
    governed_hwm_supplied = any(
        value is not None
        for value in (
            hwm_governed_result,
            hwm_governed_generation_bytes,
            hwm_governed_calendar_bytes,
            hwm_governed_receipt_bytes,
        )
    )
    financial_outcome_supplied = (
        governed_hwm_supplied
        or invested_capital_result is not None
        or decomposition_lenses_root is not None
        or decomposition_plan_bundle is not None
        or hybrid_decomposition_plan_bundle is not None
    )
    if financial_outcome_supplied:
        if private_stage_root is None or portfolio_root is None:
            raise ReportValidationError(
                "governed financial publication requires private stage context"
            )
        validate_private_stage_target(output_root, private_stage_root, portfolio_root)
    if invested_capital_result is not None:
        assert invested_capital_input_generation_path is not None
        expected_capital_result_bytes = (
            invested_capital_result_bytes
            if invested_capital_result_bytes is not None
            else canonical_json_bytes(dict(invested_capital_result))
        )
        if (
            _regular_file_bytes(
                candidate_dir / INVESTED_CAPITAL_RESULT_NAME,
                "candidate invested-capital result",
            )
            != expected_capital_result_bytes
        ):
            raise ReportValidationError("candidate invested-capital result differs")
        if _regular_file_bytes(
            candidate_dir / INVESTED_CAPITAL_INPUT_NAME,
            "candidate invested-capital input generation",
        ) != _regular_file_bytes(
            invested_capital_input_generation_path,
            "invested-capital input generation",
        ):
            raise ReportValidationError(
                "candidate invested-capital input generation differs"
            )
    epoch_supplied = _require_epoch_manifest_triplet(
        epoch_manifest_path, epoch_manifest_source_path, epoch_crosswalk_path
    )
    _require_epoch_generation_root(epoch_supplied, epoch_manifest_generation_root)
    catalog, baseline_ref, baseline_catalog_path = _baseline(
        baseline_root, baseline_generation_id
    )
    controls, _ = _report_product_controls(catalog)
    if _is_governed_financial_mvp2_path(catalog, controls):
        if not financial_outcome_supplied:
            if private_stage_root is None or portfolio_root is None:
                raise ReportValidationError(
                    "governed financial publication requires private stage context"
                )
            validate_private_stage_target(
                output_root, private_stage_root, portfolio_root
            )
        if not epoch_supplied:
            raise ReportValidationError(
                "financial MVP-2 publication requires manifest, source receipt, and current epoch crosswalk"
            )
    if epoch_supplied:
        if epoch_crosswalk_path is None:
            raise ReportValidationError(
                "epoch manifest requires an explicit current epoch crosswalk"
            )
        _validate_epoch_manifest_input(
            epoch_manifest_path,
            epoch_manifest_source_path,
            controls,
            current_account_ids=_current_product_account_ids(catalog, controls),
            crosswalk_path=epoch_crosswalk_path,
            baseline_catalog=catalog,
            baseline_ref=baseline_ref,
            baseline_catalog_path=baseline_catalog_path,
            epoch_manifest_generation_root=epoch_manifest_generation_root,
        )
    evidence_snapshots = _validated_workbook_evidence_snapshots(
        candidate_dir,
        excel_workbook,
        xlsx_result,
        validation_result,
        workbook_evidence_binding,
        epoch_manifest_path,
        epoch_manifest_source_path,
        epoch_crosswalk_path,
    )
    effective_epoch_manifest_path = (
        candidate_dir / EPOCH_MANIFEST_WORKBOOK_NAME
        if "epoch_manifest" in evidence_snapshots
        else epoch_manifest_path
    )
    effective_epoch_manifest_source_path = (
        candidate_dir / EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME
        if "epoch_manifest" in evidence_snapshots
        else epoch_manifest_source_path
    )
    effective_epoch_crosswalk_path = (
        candidate_dir / EPOCH_CROSSWALK_WORKBOOK_NAME
        if "epoch_manifest" in evidence_snapshots
        else epoch_crosswalk_path
    )
    # Use the platform's deliberately short temporary root. Nesting this copy
    # below an operator-selected output path can exceed legacy Windows MAX_PATH
    # even though every source and final authority path is valid. These files
    # are disposable read-validation snapshots, never publication authority.
    snapshot_context = tempfile.TemporaryDirectory(prefix=".p-")
    snapshot_root = Path(snapshot_context.name)
    originals: dict[Path, bytes] = {}
    inventories: dict[Path, tuple[str, ...]] = {}
    for path, content in evidence_snapshots.values():
        _remember_snapshot(originals, path, content)
    if baseline_generation_id is None:
        snapshot_baseline_root = _snapshot_current_root(
            baseline_root,
            snapshot_root / "baseline",
            originals,
            inventories,
        )
        snapshot_baseline_generation_id = None
    else:
        snapshot_baseline_root, _snapshot_baseline_generation_dir = (
            _snapshot_selected_generation_root(
                baseline_root / "generations" / baseline_generation_id,
                snapshot_root / "baseline",
                originals,
                inventories,
            )
        )
        snapshot_baseline_generation_id = baseline_generation_id
    snapshot_provider_ledger_root = (
        _snapshot_tree(
            provider_ledger_root,
            snapshot_root / "provider-ledger",
            originals,
            inventories,
        )
        if provider_ledger_root is not None
        else None
    )
    snapshot_attribution_root = (
        _snapshot_current_root(
            attribution_root,
            snapshot_root / "attribution",
            originals,
            inventories,
        )
        if attribution_root is not None
        else None
    )
    snapshot_correlation_generation_dir: Path | None = None
    if correlation_generation_dir is not None:
        (
            snapshot_correlation_root,
            snapshot_correlation_generation_dir,
        ) = _snapshot_selected_generation_root(
            correlation_generation_dir,
            snapshot_root / "correlation",
            originals,
            inventories,
        )
    elif correlation_root is not None:
        snapshot_correlation_root = _snapshot_current_root(
            correlation_root,
            snapshot_root / "correlation",
            originals,
            inventories,
        )
    else:
        snapshot_correlation_root = None
    snapshot_freshness_path = (
        _snapshot_file(
            freshness_health_path,
            snapshot_root / "freshness-health.json",
            originals,
            "freshness health",
        )
        if freshness_health_path is not None
        else None
    )
    snapshot_history_path: Path | None = None
    if history_contract_path is not None:
        history_snapshot_root = _snapshot_tree_preserving_root_identity(
            history_contract_path.parent,
            snapshot_root / "history",
            originals,
            inventories,
        )
        snapshot_history_path = history_snapshot_root / history_contract_path.name
    snapshot_decomposition_lenses_root: Path | None = None
    snapshot_decomposition_plan_bundle: Path | None = None
    snapshot_hybrid_decomposition_plan_bundle: Path | None = None
    if (
        hybrid_decomposition_plan_bundle is not None
        and decomposition_plan_bundle is None
    ):
        raise ReportValidationError("hybrid decomposition requires a live plan bundle")
    if (decomposition_lenses_root is None) != (decomposition_plan_bundle is None):
        raise ReportValidationError(
            "decomposition lens root and plan bundle must be supplied together"
        )
    if decomposition_lenses_root is not None and decomposition_plan_bundle is not None:
        snapshot_decomposition_lenses_root = _snapshot_tree(
            decomposition_lenses_root,
            snapshot_root / "decomposition-lenses",
            originals,
            inventories,
        )
        plan_snapshot_parent = _snapshot_tree_preserving_root_identity(
            decomposition_plan_bundle,
            snapshot_root / "decomposition-plan",
            originals,
            inventories,
        )
        snapshot_decomposition_plan_bundle = plan_snapshot_parent
        if hybrid_decomposition_plan_bundle is not None:
            snapshot_hybrid_decomposition_plan_bundle = (
                _snapshot_tree_preserving_root_identity(
                    hybrid_decomposition_plan_bundle,
                    snapshot_root / "hybrid-decomposition-plan",
                    originals,
                    inventories,
                )
            )
    epoch_history_verification: tuple[Path, bytes, str, str] | None = None
    if "epoch_manifest" in evidence_snapshots:
        if epoch_manifest_generation_root is None:
            raise ReportValidationError("epoch manifest generation root is required")
        epoch_history_verification = _epoch_history_snapshot_evidence(
            epoch_manifest_generation_root,
            evidence_snapshots["epoch_manifest_source"][1],
            evidence_snapshots["epoch_manifest"][1],
            evidence_snapshots["epoch_crosswalk"][1],
        )
        epoch_verification_snapshot = (
            snapshot_root / "epoch-independent-verification.json"
        )
        _write_exact(epoch_verification_snapshot, epoch_history_verification[1])
        epoch_history_verification = (
            epoch_verification_snapshot,
            epoch_history_verification[1],
            epoch_history_verification[2],
            epoch_history_verification[3],
        )
    elif epoch_manifest_generation_root is not None:
        raise ReportValidationError("epoch manifest generation root is unexpected")
    snapshot_implementation_path = (
        _snapshot_file(
            implementation_reconciliation_path,
            snapshot_root / "implementation-reconciliation.json",
            originals,
            "implementation reconciliation",
        )
        if implementation_reconciliation_path is not None
        else None
    )
    snapshot_return_attribution_path = (
        _snapshot_file(
            implementation_return_attribution_path,
            snapshot_root / "implementation-return-attribution.json",
            originals,
            "implementation return attribution",
        )
        if implementation_return_attribution_path is not None
        else None
    )
    snapshot_hwm_policy = _snapshot_file(
        HWM_POLICY,
        snapshot_root / HWM_POLICY.name,
        originals,
        "HWM policy",
    )
    report, report_bytes, baseline_catalog_path = build_report_data(
        snapshot_baseline_root,
        snapshot_freshness_path,
        snapshot_provider_ledger_root,
        snapshot_attribution_root,
        snapshot_history_path,
        snapshot_implementation_path,
        snapshot_return_attribution_path,
        None,
        snapshot_correlation_root,
        None,
        None,
        hwm_governed_result,
        hwm_governed_generation_bytes,
        hwm_governed_calendar_bytes,
        hwm_governed_receipt_bytes,
        invested_capital_result=invested_capital_result,
        invested_capital_input_generation_path=(invested_capital_input_generation_path),
        _hwm_policy_path=snapshot_hwm_policy,
        epoch_manifest_path=effective_epoch_manifest_path,
        epoch_manifest_source_path=effective_epoch_manifest_source_path,
        epoch_crosswalk_path=effective_epoch_crosswalk_path,
        epoch_manifest_generation_root=epoch_manifest_generation_root,
        baseline_generation_id=snapshot_baseline_generation_id,
        correlation_generation_dir=snapshot_correlation_generation_dir,
        decomposition_lenses_root=snapshot_decomposition_lenses_root,
        decomposition_plan_bundle=snapshot_decomposition_plan_bundle,
        hybrid_decomposition_plan_bundle=snapshot_hybrid_decomposition_plan_bundle,
    )
    snapshot_workbook = snapshot_root / WORKBOOK_NAME
    _write_exact(snapshot_workbook, evidence_snapshots["workbook"][1])
    verification = _verification_from_snapshots(
        report,
        report_bytes,
        candidate_dir,
        evidence_snapshots["report_data"][1],
        snapshot_workbook,
        evidence_snapshots["workbook"][1],
    )
    if "epoch_manifest" in evidence_snapshots:
        for name in ("epoch_manifest", "epoch_manifest_source", "epoch_crosswalk"):
            verification[f"{name}_sha256"] = hashlib.sha256(
                evidence_snapshots[name][1]
            ).hexdigest()
            verification[f"{name}_size_bytes"] = len(evidence_snapshots[name][1])
    as_of = _date(report.get("as_of_date"), "report.as_of_date")
    verification_bytes = canonical_json_bytes(verification)
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
            evidence_snapshots["xlsx_result"][0],
            "evidence/xlsx-win-result.json",
            "xlsx-win-result",
            "2.0",
        ),
        (
            evidence_snapshots["validation_result"][0],
            "evidence/workbook-validation.json",
            "xlsx-win-validation",
            "1",
        ),
        (
            evidence_snapshots["workbook_canonicalization"][0],
            "evidence/workbook-canonicalization.json",
            "portfolio-report-workbook-canonicalization",
            WORKBOOK_CANONICALIZATION_VERSION,
        ),
        (
            evidence_snapshots["xlsx_result_canonicalization"][0],
            "evidence/xlsx-result-canonicalization.json",
            "xlsx-win-result-canonicalization",
            XLSX_RESULT_CANONICALIZATION_VERSION,
        ),
        (
            evidence_snapshots["workbook_contract"][0],
            "evidence/workbook-contract.json",
            "xlsx-win-validation-contract",
            "1",
        ),
        (
            evidence_snapshots["xlsx_job"][0],
            "evidence/xlsx-job.json",
            "xlsx-win-job",
            "2.0",
        ),
        (
            evidence_snapshots["workbook_evidence_binding"][0],
            "evidence/workbook-evidence-binding.json",
            "portfolio-report-workbook-evidence-binding",
            str(WORKBOOK_EVIDENCE_BINDING_VERSION),
        ),
        (
            evidence_snapshots["workbook_parity"][0],
            "evidence/workbook-parity.json",
            "portfolio-report-workbook-parity",
            str(WORKBOOK_PARITY_VERSION),
        ),
    ]
    if "invested_capital_result" in evidence_snapshots:
        artifact_specs.extend(
            [
                (
                    evidence_snapshots["invested_capital_result"][0],
                    INVESTED_CAPITAL_RESULT_NAME,
                    "portfolio-invested-capital-result",
                    "1",
                ),
                (
                    evidence_snapshots["invested_capital_input_generation"][0],
                    INVESTED_CAPITAL_INPUT_NAME,
                    "portfolio-invested-capital-input-generation",
                    "1",
                ),
            ]
        )
    if "epoch_manifest" in evidence_snapshots:
        epoch_payload = json.loads(evidence_snapshots["epoch_manifest"][1])
        artifact_specs.extend(
            [
                (
                    evidence_snapshots["epoch_manifest"][0],
                    "input/account-strategy-manifest.json",
                    "account-strategy-manifest",
                    str(epoch_payload["schema_version"]),
                ),
                (
                    evidence_snapshots["epoch_manifest_source"][0],
                    "input/epoch-manifest-source.json",
                    "epoch-manifest-source",
                    "1",
                ),
                (
                    evidence_snapshots["epoch_crosswalk"][0],
                    PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH,
                    "account-strategy-current-epoch-crosswalk",
                    "1",
                ),
            ]
        )
        assert epoch_history_verification is not None
        artifact_specs.append(
            (
                epoch_history_verification[0],
                "input/epoch-independent-verification.json",
                epoch_history_verification[2],
                epoch_history_verification[3],
            )
        )
    if snapshot_attribution_root is not None:
        _attribution_catalog, _attribution_ref, attribution_catalog_path = _attribution(
            snapshot_attribution_root, report["baseline"]
        )
        artifact_specs.append(
            (
                attribution_catalog_path,
                "input/strategy-attribution.json",
                "strategy-attribution",
                "1",
            )
        )
    if snapshot_correlation_root is not None:
        _projection, _rows, _identity, correlation_path = _dependence_report(
            snapshot_correlation_root,
            generation_dir=snapshot_correlation_generation_dir,
            calculation_cutoff_date=report.get("calculation_cutoff_date"),
        )
        artifact_specs.append(
            (
                correlation_path,
                "input/portfolio-correlation.json",
                "portfolio-correlation-analysis",
                "2",
            )
        )
    if snapshot_history_path is not None:
        history_payload = _load_json(
            snapshot_history_path, "strategy history compatibility contract"
        )
        history_schema_name = (
            "portfolio-current-dependence-source"
            if history_payload.get("builder") == "portfolio-current-dependence-source"
            else "portfolio-target-series-evidence-proof"
        )
        artifact_specs.append(
            (
                snapshot_history_path,
                "input/strategy-history-compatibility-contract.json",
                history_schema_name,
                "1",
            )
        )
    if snapshot_implementation_path is not None:
        artifact_specs.append(
            (
                snapshot_implementation_path,
                "input/strategy-implementation-reconciliation.json",
                "portfolio-implementation-reconciliation",
                "1",
            )
        )
    if snapshot_return_attribution_path is not None:
        artifact_specs.append(
            (
                snapshot_return_attribution_path,
                "input/strategy-implementation-return-attribution.json",
                "portfolio-implementation-return-attribution",
                "1",
            )
        )
    if snapshot_freshness_path is not None:
        artifact_specs.extend(
            [
                (
                    snapshot_freshness_path,
                    "input/freshness-health.json",
                    "freshness-assessment",
                    "3",
                ),
                (
                    snapshot_hwm_policy,
                    "input/portfolio-hwm-policy.json",
                    "portfolio-hwm-policy",
                    "2",
                ),
            ]
        )
    snapshots_by_path = {path: content for path, content in evidence_snapshots.values()}
    for path, artifact_id, schema_name, schema_version in artifact_specs:
        snapshot = snapshots_by_path.get(path)
        if snapshot is None:
            artifacts.append(
                SourceArtifact.from_path(
                    path,
                    artifact_id=artifact_id,
                    as_of_date=as_of,
                    schema_name=schema_name,
                    schema_version=schema_version,
                )
            )
        else:
            artifacts.append(
                SourceArtifact(
                    artifact_id=artifact_id,
                    sha256=hashlib.sha256(snapshot).hexdigest(),
                    size_bytes=len(snapshot),
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
    _write_exact(
        generation_dir / REPORT_DATA_NAME,
        evidence_snapshots["report_data"][1],
    )
    _write_exact(
        generation_dir / "xlsx-win-result.json",
        evidence_snapshots["xlsx_result"][1],
    )
    _write_exact(
        generation_dir / "workbook-validation.json",
        evidence_snapshots["validation_result"][1],
    )
    _write_exact(
        generation_dir / "workbook-canonicalization.json",
        evidence_snapshots["workbook_canonicalization"][1],
    )
    _write_exact(
        generation_dir / "xlsx-result-canonicalization.json",
        evidence_snapshots["xlsx_result_canonicalization"][1],
    )
    _write_exact(
        generation_dir / "workbook-contract.json",
        evidence_snapshots["workbook_contract"][1],
    )
    _write_exact(
        generation_dir / "xlsx-job.json",
        evidence_snapshots["xlsx_job"][1],
    )
    _write_exact(
        generation_dir / "workbook-evidence-binding.json",
        evidence_snapshots["workbook_evidence_binding"][1],
    )
    _write_exact(
        generation_dir / "workbook-parity.json",
        evidence_snapshots["workbook_parity"][1],
    )
    if "invested_capital_result" in evidence_snapshots:
        _write_exact(
            generation_dir / INVESTED_CAPITAL_RESULT_NAME,
            evidence_snapshots["invested_capital_result"][1],
        )
        _write_exact(
            generation_dir / INVESTED_CAPITAL_INPUT_NAME,
            evidence_snapshots["invested_capital_input_generation"][1],
        )
    if "epoch_manifest" in evidence_snapshots:
        _write_exact(
            generation_dir / EPOCH_MANIFEST_WORKBOOK_NAME,
            evidence_snapshots["epoch_manifest"][1],
        )
        _write_exact(
            generation_dir / EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
            evidence_snapshots["epoch_manifest_source"][1],
        )
        _write_exact(
            generation_dir / PUBLISHED_EPOCH_CROSSWALK_RELATIVE_PATH,
            evidence_snapshots["epoch_crosswalk"][1],
        )
        assert epoch_history_verification is not None
        _write_exact(
            generation_dir / "epoch-independent-verification.json",
            epoch_history_verification[1],
        )
    _write_exact(generation_dir / "report-verification.json", verification_bytes)
    _recheck_snapshot_identities(originals, inventories)
    generation_manifest_bytes = manifest.to_json_bytes()
    delivery_dir = output_root / "deliveries" / manifest.generation_id
    delivery_receipt = _report_delivery_receipt(
        generation_id=manifest.generation_id,
        generation_manifest_bytes=generation_manifest_bytes,
        generation_inventory_sha256_value=generation_inventory_sha256(
            output_root, manifest.generation_id
        ),
        report_data_bytes=evidence_snapshots["report_data"][1],
        workbook_bytes=evidence_snapshots["workbook"][1],
        epoch_crosswalk_bytes=(
            evidence_snapshots["epoch_crosswalk"][1]
            if "epoch_crosswalk" in evidence_snapshots
            else None
        ),
    )
    delivery_receipt_bytes = canonical_json_bytes(delivery_receipt)
    _preflight_report_delivery(
        delivery_dir, evidence_snapshots["workbook"][1], delivery_receipt_bytes
    )
    _write_exact(delivery_dir / WORKBOOK_NAME, evidence_snapshots["workbook"][1])
    _write_exact(delivery_dir / REPORT_DELIVERY_NAME, delivery_receipt_bytes)
    pointer = publish_generation(output_root, manifest.generation_id)
    snapshot_context.cleanup()
    return {
        "generation_id": manifest.generation_id,
        "publication_state": pointer.state.value,
        "report_data_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "strategy_count": verification["strategy_count"],
        "twr_available_count": verification["twr_available_count"],
        "twr_row_count": verification["twr_row_count"],
        "delivery_dir": str(delivery_dir),
        "delivery_receipt": str(delivery_dir / REPORT_DELIVERY_NAME),
        "workbook_sha256": verification["workbook_sha256"],
        **(
            {
                "epoch_manifest_sha256": verification["epoch_manifest_sha256"],
                "epoch_manifest_size_bytes": verification["epoch_manifest_size_bytes"],
                "epoch_manifest_source_sha256": verification[
                    "epoch_manifest_source_sha256"
                ],
                "epoch_manifest_source_size_bytes": verification[
                    "epoch_manifest_source_size_bytes"
                ],
                "epoch_crosswalk_sha256": verification["epoch_crosswalk_sha256"],
                "epoch_crosswalk_size_bytes": verification[
                    "epoch_crosswalk_size_bytes"
                ],
            }
            if "epoch_crosswalk_sha256" in verification
            else {}
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--baseline-root", type=Path, required=True)
    build.add_argument("--baseline-generation-id")
    build.add_argument("--freshness-health", type=Path)
    build.add_argument("--provider-ledger-root", type=Path)
    build.add_argument("--attribution-root", type=Path)
    build.add_argument("--history-contract", type=Path)
    build.add_argument("--implementation-reconciliation", type=Path)
    build.add_argument("--implementation-return-attribution", type=Path)
    build.add_argument("--correlation-root", type=Path)
    build.add_argument("--correlation-generation-dir", type=Path)
    build.add_argument("--epoch-manifest", type=Path)
    build.add_argument("--epoch-manifest-source", type=Path)
    build.add_argument("--epoch-crosswalk", type=Path)
    build.add_argument("--epoch-manifest-generation-root", type=Path)
    build.add_argument("--output-root", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--baseline-root", type=Path, required=True)
    verify.add_argument("--baseline-generation-id")
    verify.add_argument("--candidate-dir", type=Path, required=True)
    verify.add_argument("--excel-workbook", type=Path, required=True)
    verify.add_argument("--freshness-health", type=Path)
    verify.add_argument("--provider-ledger-root", type=Path)
    verify.add_argument("--attribution-root", type=Path)
    verify.add_argument("--history-contract", type=Path)
    verify.add_argument("--implementation-reconciliation", type=Path)
    verify.add_argument("--implementation-return-attribution", type=Path)
    verify.add_argument("--correlation-root", type=Path)
    verify.add_argument("--correlation-generation-dir", type=Path)
    verify.add_argument("--epoch-manifest", type=Path)
    verify.add_argument("--epoch-manifest-source", type=Path)
    verify.add_argument("--epoch-crosswalk", type=Path)
    verify.add_argument("--epoch-manifest-generation-root", type=Path)
    for command in (build, verify):
        command.add_argument("--hwm-governed-result", type=Path)
        command.add_argument("--hwm-governed-input-generation", type=Path)
        command.add_argument("--hwm-governed-calendar-authority", type=Path)
        command.add_argument("--hwm-governed-admission-receipt", type=Path)
        command.add_argument("--invested-capital-result", type=Path)
        command.add_argument("--invested-capital-input-generation", type=Path)
        command.add_argument("--decomposition-lenses-root", type=Path)
        command.add_argument("--decomposition-plan-bundle", type=Path)
        command.add_argument("--hybrid-decomposition-plan-bundle", type=Path)
    canonicalize = commands.add_parser("canonicalize")
    canonicalize.add_argument("--workbook", type=Path, required=True)
    canonicalize.add_argument("--evidence", type=Path, required=True)
    canonicalize_result = commands.add_parser("canonicalize-xlsx-result")
    canonicalize_result.add_argument("--result", type=Path, required=True)
    canonicalize_result.add_argument("--evidence", type=Path, required=True)
    bind_evidence = commands.add_parser("bind-workbook-evidence")
    bind_evidence.add_argument("--candidate-dir", type=Path, required=True)
    bind_evidence.add_argument("--excel-workbook", type=Path, required=True)
    bind_evidence.add_argument("--xlsx-result", type=Path, required=True)
    bind_evidence.add_argument("--validation-result", type=Path, required=True)
    bind_evidence.add_argument("--evidence", type=Path, required=True)
    bind_evidence.add_argument("--epoch-manifest", type=Path)
    bind_evidence.add_argument("--epoch-manifest-source", type=Path)
    bind_evidence.add_argument("--epoch-crosswalk", type=Path)
    bind_evidence.add_argument("--epoch-manifest-generation-root", type=Path)
    bind_evidence.add_argument("--baseline-root", type=Path)
    bind_evidence.add_argument("--baseline-generation-id")
    publish = commands.add_parser("publish")
    publish.add_argument("--baseline-root", type=Path, required=True)
    publish.add_argument("--baseline-generation-id")
    publish.add_argument("--candidate-dir", type=Path, required=True)
    publish.add_argument("--excel-workbook", type=Path, required=True)
    publish.add_argument("--xlsx-result", type=Path, required=True)
    publish.add_argument("--validation-result", type=Path, required=True)
    publish.add_argument("--workbook-evidence-binding", type=Path, required=True)
    publish.add_argument("--output-root", type=Path, required=True)
    publish.add_argument("--freshness-health", type=Path)
    publish.add_argument("--provider-ledger-root", type=Path)
    publish.add_argument("--attribution-root", type=Path)
    publish.add_argument("--history-contract", type=Path)
    publish.add_argument("--implementation-reconciliation", type=Path)
    publish.add_argument("--implementation-return-attribution", type=Path)
    publish.add_argument("--correlation-root", type=Path)
    publish.add_argument("--correlation-generation-dir", type=Path)
    publish.add_argument("--epoch-manifest", type=Path)
    publish.add_argument("--epoch-manifest-source", type=Path)
    publish.add_argument("--epoch-manifest-generation-root", type=Path)
    publish.add_argument("--epoch-crosswalk", type=Path)
    publish.add_argument("--hwm-governed-result", type=Path)
    publish.add_argument("--hwm-governed-input-generation", type=Path)
    publish.add_argument("--hwm-governed-calendar-authority", type=Path)
    publish.add_argument("--hwm-governed-admission-receipt", type=Path)
    publish.add_argument("--invested-capital-result", type=Path)
    publish.add_argument("--invested-capital-input-generation", type=Path)
    publish.add_argument("--decomposition-lenses-root", type=Path)
    publish.add_argument("--decomposition-plan-bundle", type=Path)
    publish.add_argument("--hybrid-decomposition-plan-bundle", type=Path)
    publish.add_argument("--private-stage-root", type=Path)
    publish.add_argument("--portfolio-root", type=Path)
    return parser


def _governed_hwm_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    paths = (
        args.hwm_governed_result,
        args.hwm_governed_input_generation,
        args.hwm_governed_calendar_authority,
        args.hwm_governed_admission_receipt,
    )
    if all(path is None for path in paths):
        return {}
    if any(path is None for path in paths):
        raise ReportValidationError("governed HWM paths must be supplied together")
    return {
        "hwm_governed_result": _load_json(paths[0], "governed HWM result"),
        "hwm_governed_generation_bytes": paths[1].read_bytes(),
        "hwm_governed_calendar_bytes": paths[2].read_bytes(),
        "hwm_governed_receipt_bytes": paths[3].read_bytes(),
    }


def _invested_capital_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    result_path = args.invested_capital_result
    input_path = args.invested_capital_input_generation
    if result_path is None and input_path is None:
        return {}
    if result_path is None or input_path is None:
        raise ReportValidationError("invested-capital paths must be supplied together")
    return {
        "invested_capital_result": _load_json(
            result_path, "governed invested-capital result"
        ),
        "invested_capital_result_bytes": _regular_file_bytes(
            result_path, "governed invested-capital result"
        ),
        "invested_capital_input_generation_path": input_path,
    }


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
                args.history_contract,
                args.implementation_reconciliation,
                args.implementation_return_attribution,
                None,
                args.correlation_root,
                epoch_manifest_path=args.epoch_manifest,
                epoch_manifest_source_path=args.epoch_manifest_source,
                epoch_crosswalk_path=args.epoch_crosswalk,
                epoch_manifest_generation_root=args.epoch_manifest_generation_root,
                baseline_generation_id=args.baseline_generation_id,
                correlation_generation_dir=args.correlation_generation_dir,
                decomposition_lenses_root=args.decomposition_lenses_root,
                decomposition_plan_bundle=args.decomposition_plan_bundle,
                hybrid_decomposition_plan_bundle=args.hybrid_decomposition_plan_bundle,
                **_governed_hwm_cli_values(args),
                **_invested_capital_cli_values(args),
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.baseline_root,
                args.candidate_dir,
                args.excel_workbook,
                args.freshness_health,
                args.provider_ledger_root,
                args.attribution_root,
                args.history_contract,
                args.implementation_reconciliation,
                args.implementation_return_attribution,
                None,
                args.correlation_root,
                epoch_manifest_path=args.epoch_manifest,
                epoch_manifest_source_path=args.epoch_manifest_source,
                epoch_crosswalk_path=args.epoch_crosswalk,
                epoch_manifest_generation_root=args.epoch_manifest_generation_root,
                baseline_generation_id=args.baseline_generation_id,
                correlation_generation_dir=args.correlation_generation_dir,
                decomposition_lenses_root=args.decomposition_lenses_root,
                decomposition_plan_bundle=args.decomposition_plan_bundle,
                hybrid_decomposition_plan_bundle=args.hybrid_decomposition_plan_bundle,
                **_governed_hwm_cli_values(args),
                **_invested_capital_cli_values(args),
            )
        elif args.command == "canonicalize":
            result = canonicalize_workbook(args.workbook, args.evidence)
        elif args.command == "canonicalize-xlsx-result":
            result = canonicalize_xlsx_result(args.result, args.evidence)
        elif args.command == "bind-workbook-evidence":
            result = bind_workbook_evidence(
                args.candidate_dir,
                args.excel_workbook,
                args.xlsx_result,
                args.validation_result,
                args.evidence,
                args.epoch_manifest,
                args.epoch_manifest_source,
                args.epoch_crosswalk,
                args.baseline_root,
                args.epoch_manifest_generation_root,
                args.baseline_generation_id,
            )
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
                args.history_contract,
                args.implementation_reconciliation,
                args.implementation_return_attribution,
                args.correlation_root,
                args.workbook_evidence_binding,
                args.epoch_manifest,
                args.epoch_manifest_source,
                args.epoch_manifest_generation_root,
                args.epoch_crosswalk,
                args.baseline_generation_id,
                args.correlation_generation_dir,
                decomposition_lenses_root=args.decomposition_lenses_root,
                decomposition_plan_bundle=args.decomposition_plan_bundle,
                hybrid_decomposition_plan_bundle=args.hybrid_decomposition_plan_bundle,
                **_governed_hwm_cli_values(args),
                **_invested_capital_cli_values(args),
                **(
                    {
                        "private_stage_root": args.private_stage_root,
                        "portfolio_root": args.portfolio_root,
                    }
                    if args.private_stage_root is not None
                    or args.portfolio_root is not None
                    else {}
                ),
            )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"portfolio-report: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
