"""Build, verify, QA, and publish the private read-only control panel."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Final, Mapping, Sequence

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


PANEL_SCHEMA_VERSION: Final = 4
BUILDER_VERSION: Final = "1.4.0"
DEPENDENCE_HORIZONS: Final = ("5", "21", "22", "63", "126", "130", "252", "260")
DEPENDENCE_DRAWDOWN_REGIMES: Final = (
    "either_strategy_underwater",
    "both_strategies_underwater",
)
SOURCE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-control-panel-source-v1.json"
)
HWM_POLICY = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v2.json"
)
DEFAULT_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1] / "apps" / "portfolio-control-panel"
)
PANEL_FILES: Final = (
    "index.html",
    "styles.css",
    "analytics.css",
    "app.js",
    "data/dashboard_data.json",
    "data/dashboard_data.js",
)
VIEWS: Final = (
    "command",
    "capital",
    "risk",
    "signals",
    "attribution",
    "dependence",
    "candidates",
    "data",
)
_QA_VIEWPORTS: Final = ("desktop", "tablet", "mobile")
CAPTURES: Final = tuple(
    f"{viewport}-{view}" for viewport in _QA_VIEWPORTS for view in VIEWS
) + ("direct-file",)
LINT_CAPTURES: Final = tuple(
    f"{viewport}-{view}" for viewport in _QA_VIEWPORTS for view in VIEWS
)


class ControlPanelValidationError(ValueError):
    """An input or generated control-panel artifact violates its contract."""


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ControlPanelValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPanelValidationError(f"{field_name} must be non-empty text")
    return value.strip()


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is not readable JSON") from exc
    return _object(decoded, field_name)


def _hash(path: Path) -> str:
    return hash_file(path)[0]


def _template_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"template is missing or linked: {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ControlPanelValidationError(
            f"template is not readable UTF-8: {path.name}"
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _current_report(root: Path) -> tuple[dict[str, Any], dict[str, str], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError("report pointer is not accepted") from exc
    path = root / "generations" / pointer.generation_id / "portfolio-report-data.json"
    report = _load_json(path, "portfolio report data")
    if report.get("report_schema_version") != 1:
        raise ControlPanelValidationError("portfolio report schema is unsupported")
    return report, {"generation_id": pointer.generation_id, "sha256": _hash(path)}, path


def _current_analytics(
    root: Path,
    *,
    filename: str,
    field_name: str,
    expected_schema_version: int,
) -> tuple[dict[str, Any], dict[str, str], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError(
            f"{field_name} pointer is not accepted"
        ) from exc
    path = root / "generations" / pointer.generation_id / filename
    value = _load_json(path, field_name)
    if value.get("analytics_schema_version") != expected_schema_version:
        raise ControlPanelValidationError(f"{field_name} schema is unsupported")
    return value, {"generation_id": pointer.generation_id, "sha256": _hash(path)}, path


def _analytics_roots(
    report_root: Path,
    decomposition_root: Path | None,
    correlation_root: Path | None,
) -> tuple[Path, Path]:
    analytics = report_root.parent / "analytics"
    return (
        decomposition_root or analytics / "decomposition",
        correlation_root or analytics / "correlation",
    )


def _source_inputs(legacy_panel: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = _load_json(SOURCE_CONTRACT, "control-panel source contract")
    if contract.get("contract_version") != 1:
        raise ControlPanelValidationError(
            "control-panel source contract is unsupported"
        )
    source_files = _object(contract.get("source_files"), "source_files")
    observed: dict[str, str] = {}
    for relative, expected in source_files.items():
        path = legacy_panel / relative
        actual = _hash(path)
        if actual != _text(expected, f"source_files.{relative}"):
            raise ControlPanelValidationError(f"legacy source hash differs: {relative}")
        observed[relative] = actual
    appendix = _object(contract.get("frozen_appendix"), "frozen_appendix")
    data_relative = _text(appendix.get("data_file"), "frozen_appendix.data_file")
    data_path = legacy_panel / data_relative
    if _hash(data_path) != appendix.get("data_sha256"):
        raise ControlPanelValidationError("frozen appendix data hash differs")
    legacy = _load_json(data_path, "frozen candidate appendix")
    providers = _objects(legacy.get("providers"), "legacy.providers")
    if len(providers) != appendix.get("candidate_count"):
        raise ControlPanelValidationError("frozen candidate count differs")
    return legacy, {"contract": contract, "observed_hashes": observed}


def _summary(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = _objects(report.get("portfolio_summary"), "portfolio_summary")
    return {_text(row.get("metric"), "summary.metric"): row for row in rows}


def _summary_value(summary: Mapping[str, Mapping[str, Any]], metric: str) -> str:
    row = summary.get(metric)
    if row is None:
        raise ControlPanelValidationError(f"portfolio summary lacks {metric}")
    return _text(row.get("value"), f"portfolio_summary.{metric}")


def _fact_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _objects(report.get("coverage_data_quality"), "coverage_data_quality")


def _finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ControlPanelValidationError(f"{field_name} must be numeric")
    try:
        accepted = float(value)
    except ValueError as exc:
        raise ControlPanelValidationError(f"{field_name} must be numeric") from exc
    if not math.isfinite(accepted):
        raise ControlPanelValidationError(f"{field_name} must be finite")
    return accepted


def _number_text(value: float) -> str:
    if abs(value) < 5e-16:
        value = 0.0
    return format(value, ".12g")


def _hwm_policy() -> tuple[dict[str, Any], str]:
    policy = _object(_load_json(HWM_POLICY, "HWM policy"), "HWM policy")
    if policy.get("schema_version") != 2 or policy.get("policy_id") != "PORT-HWM-2":
        raise ControlPanelValidationError("HWM policy identity is unsupported")
    perimeter = _object(policy.get("material_perimeter"), "material_perimeter")
    exclusions = _objects(perimeter.get("exclusions"), "material_perimeter.exclusions")
    excluded_ids: set[str] = set()
    for item in exclusions:
        strategy_id = _text(item.get("strategy_id"), "HWM exclusion strategy_id")
        if strategy_id in excluded_ids:
            raise ControlPanelValidationError("HWM exclusion is duplicated")
        if item.get("classification") != "EXCLUDED_DE_MINIMIS":
            raise ControlPanelValidationError("HWM exclusion classification differs")
        excluded_ids.add(strategy_id)
    policy["excluded_strategy_ids"] = sorted(excluded_ids)
    policy_hash, _size = hash_file(HWM_POLICY)
    return policy, policy_hash


def _decimal_value(value: object, field_name: str) -> Decimal:
    try:
        accepted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ControlPanelValidationError(f"{field_name} must be decimal") from exc
    if not accepted.is_finite():
        raise ControlPanelValidationError(f"{field_name} must be finite")
    return accepted


def _material_hwm_coverage(
    strategies: Sequence[Mapping[str, Any]],
    factor_analysis: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    excluded = set(policy.get("excluded_strategy_ids", []))
    factors = {
        _text(row.get("series_id"), "factor series_id"): row
        for row in _objects(factor_analysis.get("series"), "factor series")
    }
    accepted: list[dict[str, Any]] = []
    covered = Decimal(0)
    material = Decimal(0)
    excluded_value = Decimal(0)
    excluded_names: list[str] = []
    uncovered: list[str] = []
    for original in strategies:
        row = dict(original)
        strategy_id = _text(row.get("strategy_id"), "strategy.strategy_id")
        strategy_name = _text(row.get("strategy_name"), "strategy.strategy_name")
        value = _decimal_value(row.get("current_value_usd"), "strategy.current_value")
        if strategy_id in excluded:
            row.update(
                {
                    "hwm_scope": "EXCLUDED_DE_MINIMIS",
                    "hwm_source": "PORT-HWM-2",
                    "nav_status": "EXCLUDED_DE_MINIMIS",
                }
            )
            excluded_value += value
            excluded_names.append(strategy_name)
            accepted.append(row)
            continue

        material += value
        row["hwm_scope"] = "INCLUDED"
        row["hwm_source"] = "PORTFOLIO_REPORT"
        factor = factors.get(strategy_name)
        if (
            row.get("nav_status") != "AVAILABLE"
            and factor is not None
            and factor.get("status") == "AVAILABLE"
            and factor.get("evidence_layer") == "ACCOUNT_LIVE"
        ):
            factor_coverage = _object(factor.get("coverage"), "factor.coverage")
            row.update(
                {
                    "hwm_source": "ACCOUNT_LIVE_PROVIDER_OVERLAY",
                    "nav_status": "AVAILABLE",
                    "nav_observed_start": factor_coverage.get("target_observed_start"),
                    "nav_observed_end": factor_coverage.get("target_observed_end"),
                    "nav_return_observations": factor_coverage.get(
                        "target_observations"
                    ),
                    "hwm_current_drawdown": factor_coverage.get(
                        "target_current_drawdown"
                    ),
                    "hwm_max_drawdown": factor_coverage.get("target_max_drawdown"),
                }
            )
        if row.get("nav_status") == "AVAILABLE":
            covered += value
        else:
            uncovered.append(strategy_name)
        accepted.append(row)

    if material <= 0:
        raise ControlPanelValidationError("material HWM perimeter is empty")
    ratio = covered / material
    status = (
        "AVAILABLE_MATERIAL_PERIMETER"
        if not uncovered
        else "NOT_AUTOMATABLE_MISSING_MATERIAL_DAILY_NAV"
    )
    return accepted, {
        "covered_value_usd": format(covered, "f"),
        "excluded_value_usd": format(excluded_value, "f"),
        "excluded_strategies": sorted(excluded_names),
        "material_value_usd": format(material, "f"),
        "nav_coverage": format(ratio, "f"),
        "status": status,
        "uncovered_strategies": sorted(uncovered),
    }


def _provider_account_nav_performance(
    strategies: Sequence[Mapping[str, Any]],
    performance: Sequence[Mapping[str, Any]],
    factor_analysis: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Promote a verified provider-custodied account NAV path into TWR evidence."""

    factor_by_name = {
        _text(row.get("series_id"), "factor series_id"): row
        for row in _objects(factor_analysis.get("series"), "factor series")
    }
    accepted_strategies = [dict(row) for row in strategies]
    accepted_performance = [dict(row) for row in performance]
    for strategy in accepted_strategies:
        if strategy.get("hwm_source") != "ACCOUNT_LIVE_PROVIDER_OVERLAY":
            continue
        strategy_id = _text(strategy.get("strategy_id"), "strategy.strategy_id")
        strategy_name = _text(strategy.get("strategy_name"), "strategy.strategy_name")
        factor = factor_by_name.get(strategy_name)
        if (
            factor is None
            or factor.get("status") != "AVAILABLE"
            or factor.get("evidence_layer") != "ACCOUNT_LIVE"
        ):
            raise ControlPanelValidationError(
                f"provider account NAV overlay is unavailable: {strategy_name}"
            )
        coverage = _object(factor.get("coverage"), "factor.coverage")
        cumulative = coverage.get("target_cumulative_return")
        maximum_drawdown = coverage.get("target_max_drawdown")
        if cumulative is None or maximum_drawdown is None:
            raise ControlPanelValidationError(
                f"provider account NAV overlay lacks return evidence: {strategy_name}"
            )
        matches = [
            row for row in accepted_performance if row.get("strategy_id") == strategy_id
        ]
        if len(matches) != 1:
            raise ControlPanelValidationError(
                f"provider account NAV overlay must match one account assignment: {strategy_name}"
            )
        performance_row = matches[0]
        performance_row.update(
            {
                "nav_observations": coverage.get("target_observations"),
                "observed_end": coverage.get("target_observed_end"),
                "observed_start": coverage.get("target_observed_start"),
                "twr": cumulative,
                "twr_evidence": "ACCOUNT_LIVE_PROVIDER_CUSTODIED_NAV",
                "twr_status": "AVAILABLE_PROVIDER_CUSTODIED_ACCOUNT_NAV",
            }
        )
        strategy.update(
            {
                "account_live_cumulative_return": cumulative,
                "account_live_max_drawdown": format(
                    -_decimal_value(maximum_drawdown, "target_max_drawdown"), "f"
                ),
                "account_live_status": "AVAILABLE_PROVIDER_CUSTODIED_ACCOUNT_NAV",
                "available_twr_segments": 1,
            }
        )
    return accepted_strategies, accepted_performance


def _factor_analysis(
    decomposition: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
) -> dict[str, Any]:
    method_label = _text(decomposition.get("method_label"), "factor.method_label")
    methodology_version = _text(
        decomposition.get("methodology_version"), "factor.methodology_version"
    )
    selections = _objects(decomposition.get("model_selections"), "model_selections")
    if {
        _text(row.get("series_id"), "selection.series_id") for row in selections
    } != strategy_names:
        raise ControlPanelValidationError(
            "factor selections differ from the current strategy boundary"
        )
    models = _objects(decomposition.get("model_results"), "model_results")
    model_index = {
        (
            _text(row.get("series_id"), "model.series_id"),
            _text(row.get("model_id"), "model.model_id"),
        ): row
        for row in models
    }
    attempts: dict[str, list[dict[str, Any]]] = {}
    for row in models:
        attempts.setdefault(_text(row.get("series_id"), "model.series_id"), []).append(
            row
        )

    rows: list[dict[str, Any]] = []
    available_count = 0
    dynamic_available_count = 0
    for selection in sorted(selections, key=lambda row: str(row.get("series_id"))):
        series_id = _text(selection.get("series_id"), "selection.series_id")
        selected_model = selection.get("model_id")
        if selected_model is None:
            series_attempts = attempts.get(series_id, [])
            reasons = sorted(
                {_text(item.get("status"), "model.status") for item in series_attempts}
            )
            best_coverage: dict[str, Any] | None = None
            if series_attempts:
                best = max(
                    series_attempts,
                    key=lambda item: int(
                        _object(item.get("coverage"), "model.coverage").get(
                            "aligned_observations", 0
                        )
                    ),
                )
                best_coverage = _object(best.get("coverage"), "model.coverage")
            rows.append(
                {
                    "alpha": None,
                    "contribution_summary": None,
                    "cost_coverage": "INCOMPLETE",
                    "coverage": best_coverage,
                    "diagnostics": None,
                    "estimator": _text(
                        best.get("estimator", "STATIC_RBSA"), "model.estimator"
                    )
                    if series_attempts
                    else None,
                    "evidence_layer": None,
                    "exposures": [],
                    "model_id": None,
                    "selection_status": _text(
                        selection.get("status"), "selection.status"
                    ),
                    "series_id": series_id,
                    "status": "UNAVAILABLE",
                    "unavailable_reasons": reasons or ["UNAVAILABLE_NO_MODEL_EVIDENCE"],
                }
            )
            continue

        model_id = _text(selected_model, "selection.model_id")
        model = model_index.get((series_id, model_id))
        if model is None or model.get("status") != "AVAILABLE":
            raise ControlPanelValidationError(
                f"selected factor model is not available: {series_id}"
            )
        contributions = _objects(model.get("contributions"), "model.contributions")
        if not contributions:
            raise ControlPanelValidationError(
                "selected factor model has no contributions"
            )
        factor_ids = tuple(
            _text(item, "model.factor_ids[]") for item in model.get("factor_ids", [])
        )
        if not factor_ids:
            raise ControlPanelValidationError("selected factor model has no factors")
        maximum_error = 0.0
        test_rows: list[dict[str, Any]] = []
        for contribution in contributions:
            factor_values = _object(
                contribution.get("factor_contributions"),
                "contribution.factor_contributions",
            )
            if set(factor_values) != set(factor_ids):
                raise ControlPanelValidationError(
                    "factor contribution columns differ from selected factors"
                )
            factor_total = sum(
                _finite_float(value, f"contribution.{factor_id}")
                for factor_id, value in factor_values.items()
            )
            alpha_value = _finite_float(
                contribution.get("alpha_contribution"),
                "contribution.alpha_contribution",
            )
            fitted = _finite_float(
                contribution.get("fitted_excess_return"),
                "contribution.fitted_excess_return",
            )
            actual = _finite_float(
                contribution.get("actual_excess_return"),
                "contribution.actual_excess_return",
            )
            residual = _finite_float(
                contribution.get("residual"), "contribution.residual"
            )
            maximum_error = max(
                maximum_error,
                abs(fitted - alpha_value - factor_total),
                abs(actual - fitted - residual),
            )
            if contribution.get("partition") == "TEST":
                test_rows.append(contribution)
        if maximum_error > 1e-9 or not test_rows:
            raise ControlPanelValidationError(
                "factor contributions do not reconcile or lack holdout evidence"
            )

        def mean(field: str) -> str:
            return _number_text(
                sum(
                    _finite_float(row.get(field), f"contribution.{field}")
                    for row in test_rows
                )
                / len(test_rows)
            )

        factor_means = {
            factor_id: _number_text(
                sum(
                    _finite_float(
                        _object(
                            row.get("factor_contributions"),
                            "contribution.factor_contributions",
                        ).get(factor_id),
                        f"contribution.{factor_id}",
                    )
                    for row in test_rows
                )
                / len(test_rows)
            )
            for factor_id in factor_ids
        }
        rows.append(
            {
                "alpha": _object(model.get("alpha"), "model.alpha"),
                "contribution_summary": {
                    "alpha_mean": mean("alpha_contribution"),
                    "factor_means": factor_means,
                    "fitted_mean": mean("fitted_excess_return"),
                    "maximum_reconciliation_error": _number_text(maximum_error),
                    "observations": len(test_rows),
                    "realized_mean": mean("actual_excess_return"),
                    "residual_mean": mean("residual"),
                },
                "cost_coverage": _text(
                    model.get("cost_coverage"), "model.cost_coverage"
                ),
                "coverage": _object(model.get("coverage"), "model.coverage"),
                "diagnostics": _object(model.get("diagnostics"), "model.diagnostics"),
                "estimator": _text(
                    model.get("estimator", "STATIC_RBSA"), "model.estimator"
                ),
                "evidence_layer": _text(
                    model.get("evidence_layer"), "model.evidence_layer"
                ),
                "exposures": _objects(model.get("exposures"), "model.exposures"),
                "model_id": model_id,
                "selection_status": _text(selection.get("status"), "selection.status"),
                "series_id": series_id,
                "status": "AVAILABLE",
                "unavailable_reasons": [],
            }
        )
        available_count += 1
        if model.get("estimator", "STATIC_RBSA") == "DYNAMIC_FLS":
            dynamic_available_count += 1
    return {
        "analytics_hash": _text(
            decomposition.get("analytics_hash"), "factor.analytics_hash"
        ),
        "as_of_date": _text(decomposition.get("as_of_date"), "factor.as_of_date"),
        "available_count": available_count,
        "dynamic_available_count": dynamic_available_count,
        "generation_id": identity["generation_id"],
        "method_label": method_label,
        "methodology_manifest_sha256": _text(
            decomposition.get("methodology_manifest_sha256"),
            "factor.methodology_manifest_sha256",
        ),
        "methodology_version": methodology_version,
        "series": rows,
        "source_sha256": identity["sha256"],
        "sources": _object(decomposition.get("sources"), "factor.sources"),
        "unavailable_count": len(rows) - available_count,
    }


def _dependence_analysis(
    correlation: Mapping[str, Any],
    identity: Mapping[str, str],
    strategy_names: set[str],
) -> dict[str, Any]:
    dependence = _object(correlation.get("dependence"), "dependence")
    raw_series = dependence.get("series_ids")
    if not isinstance(raw_series, list):
        raise ControlPanelValidationError("dependence.series_ids must be an array")
    series_ids = [_text(item, "dependence.series_ids[]") for item in raw_series]
    if len(series_ids) != len(strategy_names) or set(series_ids) != strategy_names:
        raise ControlPanelValidationError(
            "correlation series differ from the current strategy boundary"
        )
    matrices = _object(dependence.get("matrices"), "dependence.matrices")
    required_matrices = {
        "conceptual_prior",
        "decision_no_missing_diversification_credit",
        "observed_pearson",
        "observed_spearman",
        "posterior",
        "stress",
    }
    if set(matrices) != required_matrices:
        raise ControlPanelValidationError("dependence matrix set differs")
    for name, matrix_value in matrices.items():
        matrix = _object(matrix_value, f"matrices.{name}")
        values = matrix.get("values")
        if not isinstance(values, list) or len(values) != len(series_ids):
            raise ControlPanelValidationError(f"matrix {name} row count differs")
        if any(
            not isinstance(row, list) or len(row) != len(series_ids) for row in values
        ):
            raise ControlPanelValidationError(f"matrix {name} column count differs")

    horizon_matrices = _object(
        dependence.get("horizon_matrices"), "dependence.horizon_matrices"
    )
    if set(horizon_matrices) != set(DEPENDENCE_HORIZONS):
        raise ControlPanelValidationError("dependence horizon matrix set differs")
    drawdown_matrices = _object(
        dependence.get("drawdown_matrices"), "dependence.drawdown_matrices"
    )
    if set(drawdown_matrices) != set(DEPENDENCE_DRAWDOWN_REGIMES):
        raise ControlPanelValidationError("dependence drawdown matrix set differs")
    for group_name, group in (
        ("horizon", horizon_matrices),
        ("drawdown", drawdown_matrices),
    ):
        for name, matrix_value in group.items():
            matrix = _object(matrix_value, f"{group_name}_matrices.{name}")
            values = matrix.get("values")
            if not isinstance(values, list) or len(values) != len(series_ids):
                raise ControlPanelValidationError(
                    f"{group_name} matrix {name} row count differs"
                )
            if any(
                not isinstance(row, list) or len(row) != len(series_ids)
                for row in values
            ):
                raise ControlPanelValidationError(
                    f"{group_name} matrix {name} column count differs"
                )

    observed = _objects(dependence.get("observed_cells"), "observed_cells")
    blends = _objects(dependence.get("blend_cells"), "blend_cells")
    implementation = _objects(
        dependence.get("implementation_cells"), "implementation_cells"
    )
    expected_cells = len(series_ids) * (len(series_ids) - 1) // 2
    if not all(
        len(items) == expected_cells for items in (observed, blends, implementation)
    ):
        raise ControlPanelValidationError("dependence cell coverage differs")

    def keyed(
        items: list[dict[str, Any]], name: str
    ) -> dict[frozenset[str], dict[str, Any]]:
        result: dict[frozenset[str], dict[str, Any]] = {}
        for item in items:
            key = frozenset(
                (
                    _text(item.get("left"), f"{name}.left"),
                    _text(item.get("right"), f"{name}.right"),
                )
            )
            if len(key) != 2 or key in result:
                raise ControlPanelValidationError(f"{name} pair coverage is invalid")
            result[key] = item
        return result

    observed_index = keyed(observed, "observed")
    blend_index = keyed(blends, "blend")
    implementation_index = keyed(implementation, "implementation")
    if not (set(observed_index) == set(blend_index) == set(implementation_index)):
        raise ControlPanelValidationError("dependence pair sets differ")
    cells = [
        {
            "blend": blend_index[key],
            "implementation": implementation_index[key],
            "left": item["left"],
            "observed": item,
            "right": item["right"],
        }
        for key, item in observed_index.items()
    ]
    cells.sort(
        key=lambda item: (
            series_ids.index(item["left"]),
            series_ids.index(item["right"]),
        )
    )
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    return {
        "analytics_hash": _text(
            correlation.get("analytics_hash"), "correlation.analytics_hash"
        ),
        "as_of_date": _text(correlation.get("as_of_date"), "correlation.as_of_date"),
        "available_pair_count": sum(
            item.get("status") == "AVAILABLE" for item in observed
        ),
        "cells": cells,
        "correlation_one": correlation_one,
        "generation_id": identity["generation_id"],
        "drawdown_matrices": drawdown_matrices,
        "horizon_matrices": horizon_matrices,
        "matrices": matrices,
        "pair_count": expected_cells,
        "series_ids": series_ids,
        "source_sha256": identity["sha256"],
    }


def _panel_data(
    report: Mapping[str, Any],
    report_identity: Mapping[str, str],
    decomposition: Mapping[str, Any],
    decomposition_identity: Mapping[str, str],
    correlation: Mapping[str, Any],
    correlation_identity: Mapping[str, str],
    legacy: Mapping[str, Any],
    source: Mapping[str, Any],
    template_root: Path,
) -> dict[str, Any]:
    summary = _summary(report)
    strategies = _objects(report.get("strategy_summary"), "strategy_summary")
    accounts = _objects(report.get("account_summary"), "account_summary")
    allocations = _objects(report.get("allocation_history"), "allocation_history")
    performance = _objects(report.get("twr_mwr"), "twr_mwr")
    positions_value = report.get("positions", [])
    positions = (
        [] if positions_value is None else _objects(positions_value, "positions")
    )
    coverage = _fact_rows(report)
    reconciliations = _objects(
        report.get("reconciliation_checks"), "reconciliation_checks"
    )
    if len(strategies) != 9 or len(allocations) != 10:
        raise ControlPanelValidationError(
            "current strategy/allocation boundary differs"
        )
    if len(accounts) != 19:
        raise ControlPanelValidationError("current account boundary differs")

    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in strategies
    }
    current_strategy_names = set(strategy_names.values())
    factor_analysis = _factor_analysis(
        decomposition, decomposition_identity, current_strategy_names
    )
    dependence_analysis = _dependence_analysis(
        correlation, correlation_identity, current_strategy_names
    )
    hwm_policy, hwm_policy_hash = _hwm_policy()
    strategies, hwm_summary = _material_hwm_coverage(
        strategies, factor_analysis, hwm_policy
    )
    strategies, performance = _provider_account_nav_performance(
        strategies, performance, factor_analysis
    )
    coverage = [dict(row) for row in coverage]
    for row in coverage:
        if row.get("category") == "FACT_COVERAGE" and row.get("item") == "nav":
            missing = hwm_summary["uncovered_strategies"]
            row["known_gap"] = (
                f"{len(missing)} included current strategy lacks accepted daily "
                f"NAV; {len(hwm_summary['excluded_strategies'])} de minimis "
                "strategy is explicitly excluded from the HWM "
                "perimeter"
            )
            row["status"] = (
                "PARTIAL" if missing else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
            )
    normalized_allocations = []
    for row in allocations:
        strategy_id = row.get("strategy_id")
        normalized_allocations.append(
            {
                **row,
                "strategy_name": (
                    "Zero-risk reserve"
                    if strategy_id is None
                    else strategy_names.get(str(strategy_id), "UNMAPPED")
                ),
            }
        )
    normalized_performance = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in performance
    ]
    normalized_positions = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in positions
    ]

    fact_by_item = {
        str(row.get("item")): row
        for row in coverage
        if row.get("category") == "FACT_COVERAGE"
    }
    gates = []
    for item, label in (
        ("assignments", "Account/strategy mapping"),
        ("nav", "Daily NAV"),
        ("flows", "Cash flows"),
        ("positions", "Positions"),
        ("fx", "FX observations"),
    ):
        fact = fact_by_item.get(item)
        if fact is None:
            raise ControlPanelValidationError(f"coverage lacks {item}")
        gates.append(
            {
                "item": item,
                "label": label,
                "note": fact.get("known_gap")
                or "Accepted within the declared boundary",
                "status": fact.get("status"),
            }
        )
    blocking_items = [
        dict(row)
        for row in coverage
        if row.get("category") == "RECONCILIATION_EXCEPTION"
        and row.get("status") != "PASS"
    ]
    hwm_block = next(
        (
            row
            for row in blocking_items
            if row.get("item") == "full-systematic-hwm-unavailable"
        ),
        None,
    )
    if hwm_block is not None:
        missing_names = hwm_summary["uncovered_strategies"]
        if missing_names:
            hwm_block["known_gap"] = (
                "BLOCKING_FOR_AUTOMATED_HWM: included sleeves without accepted "
                f"daily NAV: {', '.join(missing_names)}"
            )
            hwm_block["status"] = "UNAVAILABLE"
        else:
            blocking_items.remove(hwm_block)
    providers = _objects(legacy.get("providers"), "legacy.providers")
    allowed_candidate_fields = (
        "candidate",
        "ticker",
        "category",
        "tier",
        "portfolio_role",
        "philosophy_module",
        "execution",
        "cadence",
        "principal_gap",
    )
    frozen_providers = [
        {field: row.get(field) for field in allowed_candidate_fields}
        for row in providers
    ]
    tier_counts = Counter(str(row.get("tier")) for row in frozen_providers)
    provider_counts = Counter(
        _text(row.get("provider"), "account.provider") for row in accounts
    )
    twr_available = sum(
        str(row.get("twr_status", "")).startswith("AVAILABLE") for row in performance
    )
    mwr_available = sum(
        str(row.get("mwr_status", "")).startswith("AVAILABLE") for row in performance
    )
    position_count = sum(
        str(row.get("status", "")).startswith("AVAILABLE") for row in positions
    )
    reconciliation_passed = sum(row.get("status") == "PASS" for row in reconciliations)
    source_contract = _object(source.get("contract"), "source.contract")
    appendix = _object(source_contract.get("frozen_appendix"), "frozen_appendix")
    baseline = _object(report.get("baseline"), "baseline")
    template_hashes = {
        name: hashlib.sha256(_template_bytes(template_root / name)).hexdigest()
        for name in ("index.html", "analytics.css", "app.js")
    }
    provenance = _objects(report.get("provenance"), "provenance")
    provenance = [
        {
            "artifact": "portfolio-report-generation",
            "as_of_date": report.get("as_of_date"),
            "schema": "portfolio-report-data/1",
            "sha256": report_identity["sha256"],
        },
        *provenance,
        {
            "artifact": "frozen-candidate-appendix",
            "as_of_date": appendix.get("as_of_date"),
            "schema": "strategy-allocation-audit/frozen",
            "sha256": appendix.get("data_sha256"),
        },
        {
            "artifact": "portfolio-factor-decomposition-generation",
            "as_of_date": factor_analysis["as_of_date"],
            "schema": "portfolio-factor-decomposition/1",
            "sha256": decomposition_identity["sha256"],
        },
        {
            "artifact": "portfolio-correlation-generation",
            "as_of_date": dependence_analysis["as_of_date"],
            "schema": "portfolio-correlation/1",
            "sha256": correlation_identity["sha256"],
        },
        {
            "artifact": "portfolio-hwm-policy",
            "as_of_date": hwm_policy["material_perimeter"]["exclusions"][0][
                "effective_date"
            ],
            "schema": "portfolio-hwm-policy/2",
            "sha256": hwm_policy_hash,
        },
    ]
    report_stress = _finite_float(
        _summary_value(summary, "Correlation-one stress"),
        "portfolio_summary.Correlation-one stress",
    )
    analytics_stress = _finite_float(
        _object(dependence_analysis["correlation_one"], "correlation_one").get("loss"),
        "correlation_one.loss",
    )
    if abs(report_stress - analytics_stress) > 1e-10:
        raise ControlPanelValidationError(
            "correlation-one stress differs from the accepted portfolio report"
        )
    limitations = sorted(
        {
            "Read-only local artifact; no provider, broker, order, or deployment connection exists.",
            "The candidate view is a frozen audit appendix and is not strategy, selection, or allocation authority.",
            (
                "Material systematic HWM remains unavailable only for explicitly "
                f"named included sleeves: {', '.join(hwm_summary['uncovered_strategies'])}."
                if hwm_summary["uncovered_strategies"]
                else "Material systematic HWM has complete accepted NAV coverage."
            ),
            (
                "Positions remain partial and FX remains unavailable; the panel does not infer either."
                if position_count
                else "Positions and FX remain unavailable; the panel does not infer either."
            ),
            "MWR remains unavailable when complete external-flow coverage is absent.",
            "Provider, account-live, predecessor, shadow, and backtest evidence are not concatenated.",
            *(str(row.get("known_gap")) for row in coverage if row.get("known_gap")),
        }
    )
    return {
        "account_summary": accounts,
        "advisory": {
            "correlation_one_stress": _summary_value(summary, "Correlation-one stress"),
            "hwm_limit": "0.15",
            "hwm_status": hwm_summary["status"],
            "nav_coverage": hwm_summary["nav_coverage"],
            "readiness": _summary_value(summary, "Advisory readiness"),
            "reason": (
                "Material HWM coverage now includes the accepted Matrixport "
                "Robuxio account-live series; Composer is reported but excluded "
                "de minimis. Remaining HWM gap: "
                + ", ".join(hwm_summary["uncovered_strategies"])
                + "."
            ),
        },
        "allocations": normalized_allocations,
        "blocking_items": blocking_items,
        "candidate_appendix": {
            "as_of_date": appendix.get("as_of_date"),
            "providers": frozen_providers,
            "source_sha256": appendix.get("data_sha256"),
            "status": appendix.get("status"),
            "tier_counts": dict(sorted(tier_counts.items())),
        },
        "capital": {
            "reserve": _summary_value(summary, "Reserve"),
            "systematic": _summary_value(summary, "Systematic capital"),
            "total_liquid": _summary_value(summary, "Total liquid"),
        },
        "counts": {
            "accounts": len(accounts),
            "mwr_available": mwr_available,
            "positions": position_count,
            "reconciliation_checks": len(reconciliations),
            "reconciliation_passed": reconciliation_passed,
            "strategies": len(strategies),
            "twr_available": twr_available,
            "twr_rows": len(performance),
        },
        "coverage": coverage,
        "dependence_analysis": dependence_analysis,
        "factor_analysis": factor_analysis,
        "fact_status": {
            item: row.get("status") for item, row in sorted(fact_by_item.items())
        },
        "gates": gates,
        "limitations": limitations,
        "meta": {
            "baseline_generation_id": _text(
                baseline.get("generation_id"), "baseline.generation_id"
            ),
            "panel_mode": "LOCAL_DIRECT_FILE_READ_ONLY",
            "portfolio_as_of": _text(report.get("as_of_date"), "report.as_of_date"),
            "correlation_generation_id": correlation_identity["generation_id"],
            "correlation_sha256": correlation_identity["sha256"],
            "decomposition_generation_id": decomposition_identity["generation_id"],
            "decomposition_sha256": decomposition_identity["sha256"],
            "hwm_policy_sha256": hwm_policy_hash,
            "report_generation_id": report_identity["generation_id"],
            "report_sha256": report_identity["sha256"],
            "source_shell_hashes": source.get("observed_hashes"),
            "template_hashes": template_hashes,
        },
        "panel_schema_version": PANEL_SCHEMA_VERSION,
        "performance": normalized_performance,
        "positions": normalized_positions,
        "provenance": provenance,
        "provider_counts": dict(sorted(provider_counts.items())),
        "reconciliation_checks": reconciliations,
        "strategies": strategies,
        "hwm_summary": hwm_summary,
    }


def _rendered_files(
    report_root: Path,
    legacy_panel: Path,
    template_root: Path,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
) -> tuple[dict[str, bytes], dict[str, Any], tuple[Path, Path, Path, Path]]:
    report, report_identity, report_path = _current_report(report_root)
    resolved_decomposition, resolved_correlation = _analytics_roots(
        report_root, decomposition_root, correlation_root
    )
    decomposition, decomposition_identity, decomposition_path = _current_analytics(
        resolved_decomposition,
        filename="decomposition.json",
        field_name="portfolio factor decomposition",
        expected_schema_version=1,
    )
    correlation, correlation_identity, correlation_path = _current_analytics(
        resolved_correlation,
        filename="correlation.json",
        field_name="portfolio correlation",
        expected_schema_version=1,
    )
    legacy, source = _source_inputs(legacy_panel)
    data = _panel_data(
        report,
        report_identity,
        decomposition,
        decomposition_identity,
        correlation,
        correlation_identity,
        legacy,
        source,
        template_root,
    )
    data_bytes = canonical_json_bytes(data)
    files = {
        "index.html": _template_bytes(template_root / "index.html"),
        "styles.css": (legacy_panel / "styles.css").read_bytes(),
        "analytics.css": _template_bytes(template_root / "analytics.css"),
        "app.js": _template_bytes(template_root / "app.js"),
        "data/dashboard_data.json": data_bytes,
        "data/dashboard_data.js": b"window.PORTFOLIO_DATA = " + data_bytes + b";\n",
    }
    legacy_data = legacy_panel / "data" / "dashboard_data.json"
    return (
        files,
        data,
        (
            report_path,
            decomposition_path,
            correlation_path,
            legacy_data,
        ),
    )


def _panel_id(files: Mapping[str, bytes]) -> str:
    identity = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise GenerationConflictError(f"candidate file differs: {path}")
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


def build_candidate(
    report_root: Path,
    legacy_panel: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
) -> dict[str, Any]:
    files, data, _ = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
    )
    panel_id = _panel_id(files)
    candidate = output_root / "candidates" / panel_id
    for relative, content in files.items():
        _write_exact(candidate / relative, content)
    artifact_hashes = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    manifest = {
        "artifact_sha256": artifact_hashes,
        "panel_id": panel_id,
        "panel_schema_version": PANEL_SCHEMA_VERSION,
        "portfolio_as_of": data["meta"]["portfolio_as_of"],
        "correlation_generation_id": data["meta"]["correlation_generation_id"],
        "decomposition_generation_id": data["meta"]["decomposition_generation_id"],
        "report_generation_id": data["meta"]["report_generation_id"],
        "state": "STAGED",
    }
    _write_exact(candidate / "candidate.json", canonical_json_bytes(manifest))
    return {
        "candidate_dir": str(candidate),
        "candidate_rows": len(data["candidate_appendix"]["providers"]),
        "correlation_pair_count": data["dependence_analysis"]["pair_count"],
        "factor_model_count": data["factor_analysis"]["available_count"],
        "panel_id": panel_id,
        "strategy_count": data["counts"]["strategies"],
        "twr_available_count": data["counts"]["twr_available"],
    }


def _check(name: str, condition: bool, evidence: object) -> dict[str, Any]:
    return {
        "check": name,
        "evidence": evidence,
        "status": "PASS" if condition else "FAIL",
    }


def verify_candidate(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
) -> dict[str, Any]:
    expected, data, _ = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
    )
    panel_id = _panel_id(expected)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check("Candidate identity", candidate_dir.name == panel_id, panel_id)
    )
    exact = True
    for relative, content in expected.items():
        path = candidate_dir / relative
        matches = (
            path.is_file() and not path.is_symlink() and path.read_bytes() == content
        )
        exact = exact and matches
    checks.append(_check("Candidate files are deterministic", exact, list(PANEL_FILES)))
    html = expected["index.html"].decode("utf-8")
    javascript = expected["app.js"].decode("utf-8")
    checks.append(
        _check(
            "All eight operator views are present",
            all(f'data-view="{view}"' in html for view in VIEWS),
            list(VIEWS),
        )
    )
    checks.append(
        _check(
            "No external frontend dependency",
            re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html, re.I)
            is None,
            "relative local assets only",
        )
    )
    execution_pattern = re.compile(
        r"\b(fetch\s*\(|XMLHttpRequest|WebSocket|placeOrder|submitOrder|sendBeacon|brokerApi)\b",
        re.I,
    )
    checks.append(
        _check(
            "No network or trade execution code",
            execution_pattern.search(javascript) is None,
            "static embedded renderer",
        )
    )
    checks.append(
        _check(
            "Accepted capital replay",
            data["capital"]
            == {
                "reserve": "100043.62",
                "systematic": "410272.39",
                "total_liquid": "510316.01",
            },
            data["capital"],
        )
    )
    checks.append(
        _check(
            "Current strategy boundary",
            data["counts"]["strategies"] == 9
            and len(data["allocations"]) == 10
            and data["counts"]["accounts"] == 19,
            data["counts"],
        )
    )
    report, _, _ = _current_report(report_root)
    report_position_rows = _objects(report.get("positions", []), "report.positions")
    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in _objects(report.get("strategy_summary"), "strategy_summary")
    }
    expected_position_rows = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in report_position_rows
    ]
    position_rows = _objects(data.get("positions"), "positions")
    checks.append(
        _check(
            "Current positions preserve report attribution",
            position_rows == expected_position_rows
            and data["counts"]["positions"] == len(report_position_rows)
            and all(
                row.get("status") != "AVAILABLE_MAPPED"
                or (
                    isinstance(row.get("strategy_id"), str) and bool(row["strategy_id"])
                )
                for row in position_rows
            ),
            {
                "mapped": sum(
                    row.get("status") == "AVAILABLE_MAPPED" for row in position_rows
                ),
                "rows": len(position_rows),
                "source_rows": len(report_position_rows),
            },
        )
    )
    checks.append(
        _check(
            "Coverage-aware risk replay",
            data["advisory"]["hwm_status"] == data["hwm_summary"]["status"]
            and data["advisory"]["nav_coverage"] == data["hwm_summary"]["nav_coverage"]
            and data["advisory"]["readiness"] == "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            data["advisory"],
        )
    )
    checks.append(
        _check(
            "Frozen candidates remain isolated",
            data["candidate_appendix"]["status"] == "FROZEN_AUDIT_APPENDIX"
            and len(data["candidate_appendix"]["providers"]) == 33
            and not any("candidate" in row for row in data["strategies"]),
            {
                "as_of": data["candidate_appendix"]["as_of_date"],
                "rows": len(data["candidate_appendix"]["providers"]),
            },
        )
    )
    checks.append(
        _check(
            "Incomplete facts remain explicit",
            data["fact_status"].get("positions") in {"PARTIAL", "FAILED"}
            and data["fact_status"].get("fx") == "FAILED",
            {
                "fx": data["fact_status"].get("fx"),
                "positions": data["fact_status"].get("positions"),
            },
        )
    )
    factor_rows = _objects(data["factor_analysis"].get("series"), "factor.series")
    checks.append(
        _check(
            "Accepted factor and alpha evidence is exact",
            data["factor_analysis"]["available_count"]
            + data["factor_analysis"]["unavailable_count"]
            == 9
            and all(
                row.get("contribution_summary") is None
                or _finite_float(
                    _object(
                        row.get("contribution_summary"), "contribution_summary"
                    ).get("maximum_reconciliation_error"),
                    "maximum_reconciliation_error",
                )
                <= 1e-9
                for row in factor_rows
            ),
            {
                "available": data["factor_analysis"]["available_count"],
                "generation": data["meta"]["decomposition_generation_id"],
                "unavailable": data["factor_analysis"]["unavailable_count"],
            },
        )
    )
    checks.append(
        _check(
            "Accepted dependence evidence is exact",
            data["dependence_analysis"]["pair_count"] == 36
            and data["dependence_analysis"]["available_pair_count"] == 10
            and len(data["dependence_analysis"]["matrices"]) == 6
            and len(data["dependence_analysis"]["horizon_matrices"]) == 8
            and len(data["dependence_analysis"]["drawdown_matrices"]) == 2,
            {
                "available_pairs": data["dependence_analysis"]["available_pair_count"],
                "generation": data["meta"]["correlation_generation_id"],
                "pairs": data["dependence_analysis"]["pair_count"],
            },
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": panel_id,
        "report_generation_id": data["meta"]["report_generation_id"],
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("candidate verification failed")
    return result


def _json_array(path: Path, field_name: str) -> list[object]:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is invalid JSON") from exc
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return value


def validate_qa(candidate_dir: Path, qa_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    screenshot_sizes: dict[str, int] = {}
    console_counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    target_matches: dict[str, bool] = {}
    for name in CAPTURES:
        capture = qa_dir / name
        screenshot = capture / "screenshot.png"
        screenshot_sizes[name] = (
            screenshot.stat().st_size if screenshot.is_file() else 0
        )
        console_counts[name] = len(
            _json_array(capture / "console-events.json", f"{name} console evidence")
        )
        request_counts[name] = len(
            _json_array(capture / "request-failures.json", f"{name} request evidence")
        )
        manifest = _load_json(capture / "manifest.json", f"{name} manifest")
        target_matches[name] = candidate_dir.name in str(manifest.get("target", ""))
    checks.extend(
        [
            _check(
                "Required browser screenshots exist",
                all(value > 0 for value in screenshot_sizes.values()),
                screenshot_sizes,
            ),
            _check(
                "Browser captures have no console errors",
                all(value == 0 for value in console_counts.values()),
                console_counts,
            ),
            _check(
                "Browser captures have no request failures",
                all(value == 0 for value in request_counts.values()),
                request_counts,
            ),
            _check(
                "Browser evidence targets this candidate",
                all(target_matches.values()),
                target_matches,
            ),
        ]
    )
    lint_counts: dict[str, int] = {}
    for name in LINT_CAPTURES:
        lint = _load_json(qa_dir / name / "visual-lint.json", f"{name} visual lint")
        count = lint.get("finding_count")
        if not isinstance(count, int):
            raise ControlPanelValidationError(f"{name} visual lint count is invalid")
        lint_counts[name] = count
    checks.append(
        _check(
            "Required desktop and mobile visual lint is clean",
            all(value == 0 for value in lint_counts.values()),
            lint_counts,
        )
    )
    interaction = _load_json(qa_dir / "interaction-results.json", "interaction QA")
    interaction_checks = _objects(interaction.get("checks"), "interaction.checks")
    checks.append(
        _check(
            "Interaction suite passes",
            interaction.get("all_passed") is True
            and len(interaction_checks) >= 6
            and all(row.get("status") == "PASS" for row in interaction_checks),
            {
                "checks": len(interaction_checks),
                "candidate": interaction.get("panel_id"),
            },
        )
    )
    checks.append(
        _check(
            "Direct-file runtime passes",
            console_counts["direct-file"] == 0
            and request_counts["direct-file"] == 0
            and screenshot_sizes["direct-file"] > 0,
            "file:// evidence",
        )
    )
    pdf = qa_dir / "desktop-command" / "page.pdf"
    checks.append(
        _check(
            "Command view PDF export exists",
            pdf.is_file() and pdf.stat().st_size > 0,
            pdf.stat().st_size if pdf.is_file() else 0,
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": candidate_dir.name,
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("browser QA failed")
    _write_exact(qa_dir / "qa-validation.json", canonical_json_bytes(result))
    return result


def _copy_exact(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != content:
            raise GenerationConflictError(f"generation artifact differs: {destination}")
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


def _publish_current_launcher(output_root: Path, generation_id: str) -> str:
    """Atomically update the stable local entry point after pointer publication."""
    if not re.fullmatch(r"[0-9a-f]{64}", generation_id):
        raise ControlPanelValidationError("launcher generation ID is invalid")
    launcher = output_root / "index.html"
    if launcher.is_symlink():
        raise ControlPanelValidationError("stable launcher cannot be a link")
    target = f"generations/{generation_id}/index.html"
    content = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width,initial-scale=1" />'
        '<meta name="robots" content="noindex,nofollow" />'
        '<meta http-equiv="refresh" content="0; url=' + target + '" />'
        "<title>V Capital — Current Portfolio Control</title></head>"
        "<body><main><h1>Opening the current accepted portfolio snapshot</h1>"
        "<p>This stable local launcher currently resolves to immutable generation "
        + generation_id
        + '.</p><p><a href="'
        + target
        + '">Open the accepted snapshot</a></p></main>'
        '<script>location.replace("./' + target + '");</script></body></html>\n'
    ).encode("utf-8")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".index-{generation_id}.tmp"
    try:
        _write_exact(temporary, content)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(content).hexdigest()


def publish_panel(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    qa_dir: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
    decomposition_root: Path | None = None,
    correlation_root: Path | None = None,
) -> dict[str, Any]:
    verification = verify_candidate(
        report_root,
        legacy_panel,
        candidate_dir,
        template_root,
        decomposition_root,
        correlation_root,
    )
    qa = validate_qa(candidate_dir, qa_dir)
    _, data, input_paths = _rendered_files(
        report_root,
        legacy_panel,
        template_root,
        decomposition_root,
        correlation_root,
    )
    report_path, decomposition_path, correlation_path, legacy_data = input_paths
    as_of = date.fromisoformat(_text(data["meta"]["portfolio_as_of"], "as_of"))
    artifacts: list[SourceArtifact] = [
        SourceArtifact.from_path(
            report_path,
            artifact_id="input/portfolio-report-data.json",
            as_of_date=as_of,
            schema_name="portfolio-report-data",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            legacy_data,
            artifact_id="input/frozen-candidate-appendix.json",
            as_of_date=date.fromisoformat(data["candidate_appendix"]["as_of_date"]),
            schema_name="strategy-allocation-audit-candidates",
            schema_version="frozen-2026-07-17",
        ),
        SourceArtifact.from_path(
            decomposition_path,
            artifact_id="input/portfolio-factor-decomposition.json",
            as_of_date=date.fromisoformat(data["factor_analysis"]["as_of_date"]),
            schema_name="portfolio-factor-decomposition",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            correlation_path,
            artifact_id="input/portfolio-correlation.json",
            as_of_date=date.fromisoformat(data["dependence_analysis"]["as_of_date"]),
            schema_name="portfolio-correlation",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            HWM_POLICY,
            artifact_id="input/portfolio-hwm-policy.json",
            as_of_date=as_of,
            schema_name="portfolio-hwm-policy",
            schema_version="2",
        ),
    ]
    for relative in (*PANEL_FILES, "candidate.json"):
        artifacts.append(
            SourceArtifact.from_path(
                candidate_dir / relative,
                artifact_id=f"panel/{relative}",
                as_of_date=as_of,
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            )
        )
    qa_files = sorted(path for path in qa_dir.rglob("*") if path.is_file())
    for path in qa_files:
        relative = path.relative_to(qa_dir).as_posix()
        artifacts.append(
            SourceArtifact.from_path(
                path,
                artifact_id=f"evidence/{relative}",
                as_of_date=as_of,
                schema_name="portfolio-control-panel-qa",
                schema_version="1",
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-control-panel-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="accepted portfolio, factor/alpha, dependence, and material-HWM evidence rendered read-only with explicit gaps and a separate frozen candidate appendix",
            fact_types=(
                "portfolio control panel",
                "factor and alpha attribution",
                "correlation and dependence",
                "material HWM coverage",
                "browser QA",
                "frozen appendix",
            ),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    generation_dir = manifest_path.parent
    for relative in (*PANEL_FILES, "candidate.json"):
        _copy_exact(candidate_dir / relative, generation_dir / relative)
    for path in qa_files:
        _copy_exact(path, generation_dir / "qa" / path.relative_to(qa_dir))
    pointer = publish_generation(output_root, manifest.generation_id)
    launcher_hash = _publish_current_launcher(output_root, manifest.generation_id)
    return {
        "browser_checks": qa["checks_total"],
        "correlation_generation_id": data["meta"]["correlation_generation_id"],
        "decomposition_generation_id": data["meta"]["decomposition_generation_id"],
        "generation_id": manifest.generation_id,
        "launcher_sha256": launcher_hash,
        "panel_id": verification["panel_id"],
        "publication_state": pointer.state.value,
        "report_generation_id": verification["report_generation_id"],
        "stable_launcher": str(output_root / "index.html"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--report-root", type=Path, required=True)
        command.add_argument("--legacy-panel", type=Path, required=True)
        command.add_argument(
            "--template-root", type=Path, default=DEFAULT_TEMPLATE_ROOT
        )
        command.add_argument("--decomposition-root", type=Path)
        command.add_argument("--correlation-root", type=Path)
        command.add_argument("--output-root", type=Path, required=name != "verify")
        if name != "build":
            command.add_argument("--candidate-dir", type=Path, required=True)
        if name == "publish":
            command.add_argument("--qa-dir", type=Path, required=True)
    qa = commands.add_parser("qa")
    qa.add_argument("--candidate-dir", type=Path, required=True)
    qa.add_argument("--qa-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_candidate(
                args.report_root,
                args.legacy_panel,
                args.output_root,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
            )
        elif args.command == "qa":
            result = validate_qa(args.candidate_dir, args.qa_dir)
        else:
            result = publish_panel(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.qa_dir,
                args.output_root,
                args.template_root,
                args.decomposition_root,
                args.correlation_root,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-control-panel: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
