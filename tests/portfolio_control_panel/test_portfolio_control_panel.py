from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

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
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
SPEC = importlib.util.spec_from_file_location("portfolio_control_panel", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio control-panel builder")
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)

STRATEGY_NAMES = (
    "CAOS current strategy",
    "Strategy 1",
    "Strategy 2",
    "Strategy 3",
    "Strategy 4",
    "Strategy 5",
    "Strategy 6",
    "Strategy 7",
    "Strategy 8",
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report() -> dict[str, object]:
    strategies = [
        {
            "account_live_cumulative_return": "0.01" if index < 5 else None,
            "account_live_max_drawdown": "-0.02" if index < 5 else None,
            "account_live_status": "AVAILABLE" if index < 5 else "UNAVAILABLE",
            "cost_status": "known_fixed" if index < 4 else "unknown",
            "current_value_usd": str(1000 + index),
            "economic_family": f"Synthetic family {index}",
            "implementation_gap_status": "tested" if index < 4 else "UNAVAILABLE",
            "nav_status": "AVAILABLE" if index < 6 else "UNAVAILABLE",
            "strategy_id": f"strategy-{index}",
            "strategy_name": STRATEGY_NAMES[index],
            "systematic_weight": str(1 / 9),
            "version_ref": None,
        }
        for index in range(9)
    ]
    accounts = [
        {
            "account_id": f"account-{index:02d}",
            "provider": ("SCHWAB", "IBKR", "ALPACA")[index % 3],
        }
        for index in range(19)
    ]
    allocations = [
        {
            "as_of_date": "2026-07-30",
            "current_value_usd": "100043.62",
            "scope": "RESERVE",
            "source_key": "synthetic-summary",
            "strategy_id": None,
            "systematic_weight": None,
        },
        *[
            {
                "as_of_date": "2026-07-30",
                "current_value_usd": str(1000 + index),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic-allocations",
                "strategy_id": f"strategy-{index}",
                "systematic_weight": str(1 / 9),
            }
            for index in range(9)
        ],
    ]
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "assignments",
            "known_gap": "one synthetic gap",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": "three synthetic strategies missing",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "flows",
            "known_gap": "synthetic partial",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "positions",
            "known_gap": "synthetic unavailable",
            "status": "FAILED",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "fx",
            "known_gap": "synthetic unavailable",
            "status": "FAILED",
            "value": None,
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "positions-unavailable",
            "known_gap": "BLOCKING",
            "status": "UNAVAILABLE",
            "value": None,
        },
    ]
    return {
        "account_summary": accounts,
        "allocation_history": allocations,
        "as_of_date": "2026-07-30",
        "baseline": {"generation_id": "b" * 64},
        "coverage_data_quality": coverage,
        "portfolio_summary": [
            {"metric": "Total liquid", "value": "510316.01"},
            {"metric": "Systematic capital", "value": "410272.39"},
            {"metric": "Reserve", "value": "100043.62"},
            {"metric": "NAV coverage", "value": "0.7175692227302938908465178464"},
            {"metric": "Correlation-one stress", "value": "0.37249726899852387"},
            {
                "metric": "Full-systematic HWM",
                "value": "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV",
            },
            {"metric": "Advisory readiness", "value": "DO_NOT_EXPAND_SYSTEMATIC_RISK"},
        ],
        "positions": [
            {
                "account_id": "account-00",
                "as_of_date": "2026-07-30",
                "evidence_class": "WCH_CURRENT",
                "market_value_usd": "30",
                "quantity": "3",
                "security_id": "security-synthetic",
                "status": "AVAILABLE_MAPPED",
                "strategy_id": "strategy-0",
                "symbol": "SYNTH",
            }
        ],
        "provenance": [
            {
                "artifact": "synthetic-baseline",
                "as_of_date": "2026-07-30",
                "schema": "synthetic/1",
                "sha256": "c" * 64,
            }
        ],
        "reconciliation_checks": [
            {"check_id": "synthetic-total", "status": "PASS", "value_usd": "510316.01"}
        ],
        "report_schema_version": 1,
        "strategy_summary": strategies,
        "twr_mwr": [
            {
                "account_id": "account-00",
                "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
                "mwr_xirr": None,
                "nav_observations": 20,
                "observed_end": "2026-07-30",
                "observed_start": "2026-07-01",
                "strategy_id": "strategy-0",
                "twr": "0.01",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            },
            {
                "account_id": "account-01",
                "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
                "mwr_xirr": None,
                "nav_observations": 0,
                "strategy_id": None,
                "twr": None,
                "twr_status": "UNMAPPED",
            },
        ],
    }


def _decomposition() -> dict[str, object]:
    selections = []
    models = []
    for index, series_id in enumerate(STRATEGY_NAMES):
        model_id = f"model-{index}" if index < 5 else None
        selections.append(
            {
                "model_id": model_id,
                "series_id": series_id,
                "status": (
                    "SELECTED_PARSIMONIOUS_HOLDOUT_MODEL"
                    if model_id
                    else "UNAVAILABLE_NO_VALIDATED_MODEL"
                ),
            }
        )
        coverage = {
            "aligned_observations": 80 if model_id else 0,
            "alignment_ratio": "1" if model_id else "0",
            "factor_missing_target_dates": 0,
            "factor_missing_weekdays": 0,
            "factor_missing_weekends": 0,
            "observed_end": "2026-07-30" if model_id else None,
            "observed_start": "2026-01-02" if model_id else None,
            "target_observations": 80 if model_id else 0,
            "test_observations": 2 if model_id else 0,
            "train_observations": 78 if model_id else 0,
        }
        if model_id:
            models.append(
                {
                    "alpha": {
                        "annualized_standard_error": "0.01",
                        "confidence_status": "AVAILABLE_CLASSICAL_OLS",
                        "gross_annualized": None,
                        "gross_status": "UNAVAILABLE_INCOMPLETE_COST_COVERAGE",
                        "net_annualized": "0.05",
                        "net_status": "AVAILABLE_OBSERVED_BASIS",
                        "periodic_intercept": "0.001",
                    },
                    "contributions": [
                        {
                            "actual_excess_return": "0.004",
                            "alpha_contribution": "0.001",
                            "date": "2026-07-29",
                            "factor_contributions": {"SPY_XS": "0.002"},
                            "fitted_excess_return": "0.003",
                            "partition": "TEST",
                            "residual": "0.001",
                        },
                        {
                            "actual_excess_return": "0.005",
                            "alpha_contribution": "0.001",
                            "date": "2026-07-30",
                            "factor_contributions": {"SPY_XS": "0.003"},
                            "fitted_excess_return": "0.004",
                            "partition": "TEST",
                            "residual": "0.001",
                        },
                    ],
                    "cost_coverage": "INCOMPLETE",
                    "coverage": coverage,
                    "diagnostics": {
                        "holdout": {
                            "adjusted_r_squared": "0.39",
                            "observation_count": 2,
                            "predicted_r_squared": "0.4",
                            "r_squared": "0.4",
                            "rmse": "0.01",
                            "tracking_error_annualized": "0.15",
                        },
                        "rank": 2,
                        "rank_status": "FULL_RANK",
                        "train": {
                            "adjusted_r_squared": "0.38",
                            "observation_count": 78,
                            "r_squared": "0.39",
                            "rmse": "0.011",
                            "tracking_error_annualized": "0.16",
                        },
                    },
                    "evidence_layer": "ACCOUNT_LIVE",
                    "exposures": [
                        {
                            "confidence_status": "AVAILABLE_CLASSICAL_OLS",
                            "exposure": "0.8",
                            "factor_id": "SPY_XS",
                            "holdout_mean_period_contribution": "0.0025",
                            "standard_error": "0.1",
                        }
                    ],
                    "factor_ids": ["SPY_XS"],
                    "model_id": model_id,
                    "series_id": series_id,
                    "status": "AVAILABLE",
                }
            )
        else:
            models.append(
                {
                    "cost_coverage": "INCOMPLETE",
                    "coverage": coverage,
                    "model_id": f"attempt-{index}",
                    "series_id": series_id,
                    "status": "UNAVAILABLE_UNKNOWN_TARGET_SERIES",
                }
            )
    return {
        "analytics_hash": "d" * 64,
        "analytics_schema_version": 1,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic",
        "method_label": "MPI_INFORMED_NON_EQUIVALENT",
        "methodology_manifest_sha256": "e" * 64,
        "methodology_version": "ANL-DECOMP-STATIC-1",
        "model_results": models,
        "model_selections": selections,
        "sources": {
            "factor": {"artifact_id": "synthetic-factor", "sha256": "f" * 64},
            "target": {"artifact_id": "synthetic-target", "sha256": "a" * 64},
        },
    }


def _correlation() -> dict[str, object]:
    size = len(STRATEGY_NAMES)
    observed_pairs = {
        (left, right)
        for index, left in enumerate(STRATEGY_NAMES[:5])
        for right in STRATEGY_NAMES[index + 1 : 5]
    }

    def matrix(off_diagonal: str | None) -> list[list[str | None]]:
        return [
            ["1" if row == column else off_diagonal for column in range(size)]
            for row in range(size)
        ]

    observed = matrix(None)
    spearman = matrix(None)
    cells = []
    blends = []
    implementation = []
    for left_index, left in enumerate(STRATEGY_NAMES):
        for right_index in range(left_index + 1, size):
            right = STRATEGY_NAMES[right_index]
            available = (left, right) in observed_pairs
            if available:
                observed[left_index][right_index] = "0.5"
                observed[right_index][left_index] = "0.5"
                spearman[left_index][right_index] = "0.45"
                spearman[right_index][left_index] = "0.45"
            cells.append(
                {
                    "coverage": {
                        "common_observations": 80 if available else 0,
                        "evidence_layer": "ACCOUNT_LIVE" if available else None,
                        "frequency": "DAILY_EXACT_DATE",
                        "left_observations": 80 if available else 0,
                        "missing_from_left": 0,
                        "missing_from_right": 0,
                        "observed_end": "2026-07-30" if available else None,
                        "observed_start": "2026-01-02" if available else None,
                        "right_observations": 80 if available else 0,
                    },
                    "downside": {
                        "correlation": "0.4" if available else None,
                        "observations": 30 if available else 0,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                    },
                    "joint_tail": {
                        "joint_days": 4 if available else 0,
                        "left_threshold": "-0.02" if available else None,
                        "multiple_vs_independence": "5" if available else None,
                        "right_threshold": "-0.02" if available else None,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                    },
                    "left": left,
                    "pearson": "0.5" if available else None,
                    "right": right,
                    "rolling_pearson": {
                        "available_windows": 21 if available else 0,
                        "latest": "0.55" if available else None,
                        "maximum": "0.6" if available else None,
                        "mean": "0.5" if available else None,
                        "minimum": "0.4" if available else None,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                        "window_observations": 60 if available else None,
                    },
                    "spearman": "0.45" if available else None,
                    "status": (
                        "AVAILABLE" if available else "UNAVAILABLE_MISSING_SERIES"
                    ),
                }
            )
            blends.append(
                {
                    "conceptual_prior": "0.25",
                    "conceptual_prior_note": "synthetic conceptual prior",
                    "decision_correlation": "0.3" if available else "1",
                    "decision_status": (
                        "OBSERVED_SUPPORTED_POSTERIOR"
                        if available
                        else "NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION"
                    ),
                    "left": left,
                    "observations": 80 if available else 0,
                    "observed_correlation": "0.5" if available else None,
                    "observed_weight": "0.16" if available else "0",
                    "posterior_pre_psd": "0.3" if available else "0.25",
                    "right": right,
                    "stress_pre_psd": "0.65" if available else "0.4",
                }
            )
            implementation.append(
                {
                    "left": left,
                    "right": right,
                    "shared_instruments": [],
                    "shared_providers": [],
                    "shared_venues": [],
                    "status": "UNAVAILABLE_MAPPING_MISSING",
                }
            )
    return {
        "analytics_hash": "b" * 64,
        "analytics_schema_version": 1,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic",
        "dependence": {
            "blend_cells": blends,
            "correlation_one": {
                "components": [],
                "expected_loss": "0.37249726899852387",
                "loss": "0.37249726899852387",
                "reconciliation_difference": "0",
                "status": "AVAILABLE_RECONCILED_SEPARATE_CONSERVATIVE_BOUND",
            },
            "implementation_cells": implementation,
            "matrices": {
                "conceptual_prior": {
                    "classification": "CONCEPTUAL_NOT_MEASURED",
                    "values": matrix("0.25"),
                },
                "decision_no_missing_diversification_credit": {
                    "classification": "CONSERVATIVE_DECISION_ONLY_NOT_PSD_COVARIANCE_INPUT",
                    "values": matrix("1"),
                },
                "observed_pearson": {
                    "classification": "MEASURED_COMPATIBLE_EXACT_DATE_ONLY",
                    "values": observed,
                },
                "observed_spearman": {
                    "classification": "MEASURED_ROBUST_RANK_CHECK",
                    "values": spearman,
                },
                "posterior": {
                    "classification": "CONCEPTUAL_OBSERVED_BLEND_PSD_PROJECTED",
                    "projection": {
                        "maximum_absolute_adjustment": "0",
                        "minimum_eigenvalue_post_projection": "0.7",
                        "minimum_eigenvalue_pre_projection": "0.7",
                    },
                    "values": matrix("0.3"),
                },
                "stress": {
                    "classification": "STRESS_ASSUMPTION_PSD_PROJECTED",
                    "projection": {
                        "maximum_absolute_adjustment": "0",
                        "minimum_eigenvalue_post_projection": "0.55",
                        "minimum_eigenvalue_pre_projection": "0.55",
                    },
                    "values": matrix("0.45"),
                },
            },
            "observed_cells": cells,
            "series_ids": list(STRATEGY_NAMES),
        },
        "policy": {},
        "source": {},
    }


def _publish_analytics(root: Path, filename: str, value: object, adapter: str) -> None:
    source = root.parent / f"{adapter}.json"
    _write_json(source, value)
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity(adapter, "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {adapter}",
            fact_types=(adapter,),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id=f"synthetic/{filename}",
                as_of_date=date(2026, 7, 30),
                schema_name=adapter,
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / filename).write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    report_root = tmp_path / "reporting"
    source_report = tmp_path / "synthetic-portfolio-report-data.json"
    _write_json(source_report, _report())
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-report-builder", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic control-panel test report",
            fact_types=("synthetic report",),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source_report,
                artifact_id="synthetic/portfolio-report-data.json",
                as_of_date=date(2026, 7, 30),
                schema_name="synthetic-portfolio-report",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(report_root, manifest)
    (manifest_path.parent / "portfolio-report-data.json").write_bytes(
        source_report.read_bytes()
    )
    publish_generation(report_root, manifest.generation_id)
    decomposition_root = tmp_path / "analytics" / "decomposition"
    correlation_root = tmp_path / "analytics" / "correlation"
    _publish_analytics(
        decomposition_root,
        "decomposition.json",
        _decomposition(),
        "synthetic-decomposition",
    )
    _publish_analytics(
        correlation_root,
        "correlation.json",
        _correlation(),
        "synthetic-correlation",
    )
    legacy = tmp_path / "legacy"
    source_content = {
        "index.html": b"synthetic legacy index",
        "styles.css": b":root { --accent: #5cc8c0; }",
        "app.js": b"synthetic legacy app",
        "control_panel_validation.json": b'{"status":"PASS"}',
    }
    for relative, content in source_content.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    providers = [
        {
            "candidate": "CAOS" if index == 0 else f"Candidate {index}",
            "cadence": "daily",
            "category": "synthetic",
            "execution": "fund",
            "philosophy_module": "synthetic",
            "portfolio_role": "synthetic research",
            "principal_gap": "synthetic gap",
            "ticker": "CAOS" if index == 0 else f"S{index}",
            "tier": "Tier 1" if index < 5 else f"Tier {(index % 3) + 2}",
        }
        for index in range(33)
    ]
    _write_json(legacy / "data" / "dashboard_data.json", {"providers": providers})
    contract = {
        "contract_version": 1,
        "frozen_appendix": {
            "as_of_date": "2026-07-17",
            "candidate_count": 33,
            "data_file": "data/dashboard_data.json",
            "data_sha256": _sha(legacy / "data" / "dashboard_data.json"),
            "status": "FROZEN_AUDIT_APPENDIX",
        },
        "source_files": {
            relative: _sha(legacy / relative) for relative in source_content
        },
    }
    contract_path = tmp_path / "source-contract.json"
    contract_path.write_bytes(canonical_json_bytes(contract))
    monkeypatch.setattr(panel, "SOURCE_CONTRACT", contract_path)
    return {
        "correlation": correlation_root,
        "decomposition": decomposition_root,
        "legacy": legacy,
        "output": tmp_path / "control-panel",
        "report": report_root,
    }


def _qa(candidate: Path, root: Path) -> Path:
    qa = root / "qa"
    for name in panel.CAPTURES:
        folder = qa / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "screenshot.png").write_bytes(b"synthetic-png-evidence")
        _write_json(folder / "console-events.json", [])
        _write_json(folder / "request-failures.json", [])
        _write_json(
            folder / "manifest.json",
            {"target": f"file:///{candidate.name}/index.html#{name}"},
        )
    for name in panel.LINT_CAPTURES:
        _write_json(
            qa / name / "visual-lint.json", {"finding_count": 0, "findings": []}
        )
    (qa / "desktop-command" / "page.pdf").write_bytes(b"synthetic-pdf-evidence")
    checks = [
        {"check": f"synthetic interaction {index}", "status": "PASS"}
        for index in range(6)
    ]
    _write_json(
        qa / "interaction-results.json",
        {"all_passed": True, "checks": checks, "panel_id": candidate.name},
    )
    return qa


def test_build_is_deterministic_and_verifies(inputs: dict[str, Path]) -> None:
    first = panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])
    second = panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])
    assert first["panel_id"] == second["panel_id"]
    result = panel.verify_candidate(
        inputs["report"], inputs["legacy"], Path(first["candidate_dir"])
    )
    assert result["all_passed"] is True
    assert result["checks_passed"] == result["checks_total"]
    data = json.loads(
        (Path(first["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["panel_schema_version"] == 3
    assert data["positions"] == [
        {
            "account_id": "account-00",
            "as_of_date": "2026-07-30",
            "evidence_class": "WCH_CURRENT",
            "market_value_usd": "30",
            "quantity": "3",
            "security_id": "security-synthetic",
            "status": "AVAILABLE_MAPPED",
            "strategy_id": "strategy-0",
            "strategy_name": STRATEGY_NAMES[0],
            "symbol": "SYNTH",
        }
    ]
    assert data["factor_analysis"]["available_count"] == 5
    assert data["factor_analysis"]["unavailable_count"] == 4
    assert data["dependence_analysis"]["available_pair_count"] == 10
    assert data["dependence_analysis"]["pair_count"] == 36


def test_template_line_endings_do_not_change_identity(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    lf_template = tmp_path / "template-lf"
    crlf_template = tmp_path / "template-crlf"
    for name in ("index.html", "analytics.css", "app.js"):
        normalized = (
            (panel.DEFAULT_TEMPLATE_ROOT / name)
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        lf_template.mkdir(parents=True, exist_ok=True)
        crlf_template.mkdir(parents=True, exist_ok=True)
        (lf_template / name).write_bytes(normalized.encode("utf-8"))
        (crlf_template / name).write_bytes(
            normalized.replace("\n", "\r\n").encode("utf-8")
        )
    lf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "lf-output",
        lf_template,
    )
    crlf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "crlf-output",
        crlf_template,
    )
    assert lf["panel_id"] == crlf["panel_id"]


def test_source_drift_fails_closed(inputs: dict[str, Path]) -> None:
    (inputs["legacy"] / "styles.css").write_text("changed", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="source hash differs"):
        panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])


def test_candidate_tamper_fails_closed(inputs: dict[str, Path]) -> None:
    result = panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])
    candidate = Path(result["candidate_dir"])
    (candidate / "app.js").write_text("tampered", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="verification failed"):
        panel.verify_candidate(inputs["report"], inputs["legacy"], candidate)


def test_browser_qa_and_publication(inputs: dict[str, Path], tmp_path: Path) -> None:
    built = panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    qa_result = panel.validate_qa(candidate, qa)
    assert qa_result["all_passed"] is True
    published = panel.publish_panel(
        inputs["report"], inputs["legacy"], candidate, qa, inputs["output"]
    )
    pointer = read_current(inputs["output"])
    assert published["publication_state"] == "PUBLISHED"
    assert pointer.generation_id == published["generation_id"]
    generation = inputs["output"] / "generations" / pointer.generation_id
    assert (generation / "index.html").is_file()
    assert (generation / "qa" / "qa-validation.json").is_file()


def test_browser_qa_rejects_console_error(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    _write_json(qa / "desktop-risk" / "console-events.json", [{"text": "boom"}])
    with pytest.raises(panel.ControlPanelValidationError, match="browser QA failed"):
        panel.validate_qa(candidate, qa)
