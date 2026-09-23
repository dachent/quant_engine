from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
import math
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
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_replication.py"
SPEC = importlib.util.spec_from_file_location("portfolio_replication", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio replication builder")
replication = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replication
SPEC.loader.exec_module(replication)


DECOMPOSITION_MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_decomposition.py"
DECOMPOSITION_SPEC = importlib.util.spec_from_file_location(
    "portfolio_decomposition_for_replication", DECOMPOSITION_MODULE_PATH
)
if DECOMPOSITION_SPEC is None or DECOMPOSITION_SPEC.loader is None:
    raise RuntimeError("unable to load portfolio decomposition builder")
producer = importlib.util.module_from_spec(DECOMPOSITION_SPEC)
sys.modules[DECOMPOSITION_SPEC.name] = producer
DECOMPOSITION_SPEC.loader.exec_module(producer)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _source(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    factors = source_root / "factors.csv"
    with factors.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "RF", "MARKET"])
        writer.writeheader()
        writer.writerows(
            [
                {"date": "2026-07-01", "RF": "0", "MARKET": "0.01"},
                {"date": "2026-07-02", "RF": "0", "MARKET": "-0.02"},
            ]
        )
    decomposition = source_root / "decomposition.json"
    model = {
        "alpha": {"periodic_intercept": "0.001"},
        "diagnostics": {
            "dynamic": {
                "holdout_forecasts": [
                    {
                        "actual_excess_return": "0.005",
                        "date": "2026-07-01",
                        "forecast_exposures": {"MARKET": "0.5"},
                        "predicted_excess_return": "0.006",
                    },
                    {
                        "actual_excess_return": "-0.03",
                        "date": "2026-07-02",
                        "forecast_exposures": {"MARKET": "2"},
                        "predicted_excess_return": "-0.039",
                    },
                ],
                "selection_rule": replication.EXPECTED_SELECTION_RULE,
                "validation": replication.EXPECTED_VALIDATION,
            },
            "holdout": {
                "observation_count": 2,
                "prediction_mode": replication.EXPECTED_PREDICTION_MODE,
            },
        },
        "estimator": replication.EXPECTED_ESTIMATOR,
        "factor_ids": ["MARKET"],
        "method_label": replication.ALLOWED_SOURCE_METHOD_LABEL,
        "model_id": "dynamic-market",
        "model_quality": {"decision_status": "PM_VALID"},
        "selected_for_series": True,
        "selected_ridge": "1e-10",
        "selected_smoothness": "0.001",
        "series_id": "Synthetic Strategy",
    }
    _write_json(
        decomposition,
        {
            "analytics_schema_version": 1,
            "as_of_date": "2026-07-31",
            "method_label": replication.ALLOWED_SOURCE_METHOD_LABEL,
            "model_results": [model],
            "model_selections": [
                {
                    "model_id": "dynamic-market",
                    "series_id": "Synthetic Strategy",
                    "status": "SELECTED_PARSIMONIOUS_TRAIN_CV_MODEL",
                },
                {
                    "model_id": None,
                    "series_id": "No Evidence Strategy",
                    "status": "UNAVAILABLE_NO_VALIDATED_MODEL",
                },
            ],
        },
    )
    generation = source_root / "generation.json"
    source_manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 31),
        adapter=AdapterIdentity("portfolio-decomposition", "synthetic"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic dynamic decomposition fixture",
            fact_types=("dynamic-decomposition",),
            observed_start=date(2026, 7, 1),
            observed_end=date(2026, 7, 31),
        ),
        artifacts=(
            SourceArtifact.from_path(
                decomposition,
                artifact_id="analytics/decomposition.json",
                as_of_date=date(2026, 7, 31),
                schema_name="portfolio-factor-decomposition",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation.write_bytes(source_manifest.to_json_bytes())
    generation_id = source_manifest.generation_id
    spec = tmp_path / "replication-spec.json"
    _write_json(
        spec,
        {
            "as_of_date": "2026-07-31",
            "method_label": replication.METHOD_LABEL,
            "policy": {
                "cost_sensitivities_bps": [0, 10, 20],
                "max_absolute_factor_weight": "1.5",
                "max_gross_factor_exposure": "2",
                "periods_per_year": 252,
                "selected_cost_bps": 10,
            },
            "schema_version": 1,
            "sources": {
                "decomposition": _source(decomposition),
                "decomposition_generation": _source(generation),
                "decomposition_generation_id": generation_id,
                "factors": _source(factors),
            },
        },
    )
    return spec, source_root


def _rebuild_generation(
    spec: Path, source_root: Path, artifacts: tuple[SourceArtifact, ...]
) -> None:
    generation_path = source_root / "generation.json"
    current = GenerationManifest.from_json_bytes(generation_path.read_bytes())
    replacement = GenerationManifest.build(
        as_of_date=current.as_of_date,
        adapter=current.adapter,
        coverage=current.coverage,
        artifacts=artifacts,
        verification=current.verification,
    )
    generation_path.write_bytes(replacement.to_json_bytes())
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition_generation"] = _source(generation_path)
    spec_value["sources"]["decomposition_generation_id"] = replacement.generation_id
    _write_json(spec, spec_value)


def _replace_decomposition_artifact(
    spec: Path,
    source_root: Path,
    **overrides: object,
) -> None:
    generation_path = source_root / "generation.json"
    current = GenerationManifest.from_json_bytes(generation_path.read_bytes())
    original = next(
        artifact
        for artifact in current.artifacts
        if artifact.artifact_id == "analytics/decomposition.json"
    )
    replacement = SourceArtifact(
        artifact_id=original.artifact_id,
        sha256=str(overrides.get("sha256", original.sha256)),
        size_bytes=int(overrides.get("size_bytes", original.size_bytes)),
        as_of_date=overrides.get("as_of_date", original.as_of_date),
        schema_name=str(overrides.get("schema_name", original.schema_name)),
        schema_version=str(overrides.get("schema_version", original.schema_version)),
    )
    _rebuild_generation(
        spec,
        source_root,
        tuple(
            replacement
            if artifact.artifact_id == "analytics/decomposition.json"
            else artifact
            for artifact in current.artifacts
        ),
    )


def _producer_fixture(
    root: Path, *, analysis_lens: str | None = None
) -> tuple[Path, Path]:
    root.mkdir()
    days: list[date] = []
    current = date(2025, 1, 2)
    while len(days) < 150:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    target = root / "targets.csv"
    factors = root / "factors.csv"
    with (
        target.open("w", encoding="utf-8", newline="") as target_handle,
        factors.open("w", encoding="utf-8", newline="") as factor_handle,
    ):
        target_writer = csv.writer(target_handle)
        factor_writer = csv.writer(factor_handle)
        target_writer.writerow(producer.TARGET_HEADERS)
        factor_writer.writerow(["date", "RF", "F1", "F2"])
        for index, observed in enumerate(days):
            factor_one = 0.012 * math.sin(index / 3.7) + 0.004 * math.cos(index / 13)
            factor_two = 0.010 * math.cos(index / 5.1) - 0.003 * math.sin(index / 17)
            beta_one = 0.15 + 0.70 * index / (len(days) - 1)
            beta_two = -0.35 + 0.30 * math.sin(index / 28)
            risk_free = 0.00005
            target_writer.writerow(
                [
                    "synthetic-strategy",
                    observed.isoformat(),
                    format(
                        0.00015
                        + beta_one * factor_one
                        + beta_two * factor_two
                        + risk_free,
                        ".17g",
                    ),
                    "ACCOUNT_LIVE",
                    "NET",
                    "TOTAL",
                    "",
                ]
            )
            factor_writer.writerow(
                [
                    observed.isoformat(),
                    format(risk_free, ".17g"),
                    format(factor_one, ".17g"),
                    format(factor_two, ".17g"),
                ]
            )
    target_hash, target_size = _source(target)["sha256"], _source(target)["size_bytes"]
    factor_hash, factor_size = (
        _source(factors)["sha256"],
        _source(factors)["size_bytes"],
    )
    specification: dict[str, object] = {
        "as_of_date": days[-1].isoformat(),
        "factor_source": {
            "artifact_id": "frozen/factors.csv",
            "date_column": "date",
            "expected_sha256": factor_hash,
            "expected_size_bytes": factor_size,
            "factors": [
                {"column": "F1", "factor_id": "F1", "return_type": "EXCESS"},
                {"column": "F2", "factor_id": "F2", "return_type": "EXCESS"},
            ],
            "relative_path": "factors.csv",
            "risk_free_column": "RF",
            "schema_name": "synthetic-factor-returns",
            "schema_version": "1",
        },
        "maximum_factor_staleness_days": 7,
        "models": [
            {
                "constraint": "UNCONSTRAINED",
                "cost_coverage": "INCOMPLETE",
                "cv_folds": 3,
                "estimator": "DYNAMIC_FLS",
                "factor_ids": ["F1", "F2"],
                "minimum_test_observations": 20,
                "minimum_train_observations": 60,
                "model_id": "dynamic-fls",
                "parsimony_tolerance": "0.000001",
                "periods_per_year": 252,
                "ridge_candidates": ["0.0000000001"],
                "series_id": "synthetic-strategy",
                "smoothness_candidates": [
                    "0.00000001",
                    "0.0000001",
                    "0.000001",
                    "0.00001",
                    "0.0001",
                    "0.001",
                ],
                "test_start": days[105].isoformat(),
                "train_end": days[104].isoformat(),
            }
        ],
        "schema_version": 1,
        "target_source": {
            "artifact_id": "frozen/targets.csv",
            "expected_sha256": target_hash,
            "expected_size_bytes": target_size,
            "relative_path": "targets.csv",
            "schema_name": "synthetic-target-returns",
            "schema_version": "1",
        },
    }
    if analysis_lens is not None:
        specification["analysis_lens"] = analysis_lens
    spec = root / "spec.json"
    _write_json(spec, specification)
    return spec, factors


def test_builds_forward_only_costed_tracker_and_dispositions(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)

    payload, manifest, _content = replication.build_replication(spec, source_root)

    assert payload["method_label"] == replication.METHOD_LABEL
    assert payload["authority_boundary"]["readiness_gate_effect"] == "NONE"
    results = {row["series_id"]: row for row in payload["replication_results"]}
    available = results["Synthetic Strategy"]
    assert available["availability_status"] == "AVAILABLE_RESEARCH_REPLICATION"
    assert available["parameter_selection"]["holdout_recalibration"] == "NONE"
    assert available["tracker_rows"][0]["information_set"].endswith("PRIOR_OBSERVATION")
    assert available["tracker_rows"][0]["one_period_turnover"] == "0.5"
    assert available["tracker_rows"][0]["net_tracker_excess_return"] == "0.0045"
    assert available["tracker_rows"][1]["implementable_factor_weights"] == {
        "MARKET": "1.5"
    }
    assert available["tracker_rows"][1]["constraint_bindings"] == [
        "PER_FACTOR_ABSOLUTE_LIMIT"
    ]
    assert results["No Evidence Strategy"]["availability_status"].startswith(
        "UNAVAILABLE"
    )
    assert manifest.coverage.status.value == "COMPLETE_WITHIN_DECLARED_BOUNDARY"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"sha256": "0" * 64}, "artifact hash differs"),
        ({"size_bytes": 1}, "artifact size differs"),
        ({"as_of_date": date(2026, 7, 30)}, "artifact as_of_date differs"),
        ({"schema_name": "other-schema"}, "artifact schema_name differs"),
        ({"schema_version": "2"}, "artifact schema_version differs"),
    ],
    ids=["hash", "size", "date", "schema-name", "schema-version"],
)
def test_decomposition_manifest_artifact_must_match_source(
    tmp_path: Path, overrides: dict[str, object], error: str
) -> None:
    spec, source_root = _fixture(tmp_path)
    _replace_decomposition_artifact(spec, source_root, **overrides)

    with pytest.raises(replication.ReplicationError, match=error):
        replication.build_replication(spec, source_root)


def test_legacy_generation_without_decomposition_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    spec, source_root = _fixture(tmp_path)
    current = GenerationManifest.from_json_bytes(
        (source_root / "generation.json").read_bytes()
    )
    original = current.artifacts[0]
    _rebuild_generation(
        spec,
        source_root,
        (
            SourceArtifact(
                artifact_id="legacy/decomposition.json",
                sha256=original.sha256,
                size_bytes=original.size_bytes,
                as_of_date=original.as_of_date,
                schema_name=original.schema_name,
                schema_version=original.schema_version,
            ),
        ),
    )

    with pytest.raises(replication.ReplicationError, match="artifact is missing"):
        replication.build_replication(spec, source_root)


def test_decomposition_analytics_schema_version_must_be_an_integer(
    tmp_path: Path,
) -> None:
    spec, source_root = _fixture(tmp_path)
    decomposition_path = source_root / "decomposition.json"
    value = json.loads(decomposition_path.read_text(encoding="utf-8"))
    value["analytics_schema_version"] = "1"
    _write_json(decomposition_path, value)
    source = _source(decomposition_path)
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition"] = source
    _write_json(spec, spec_value)
    _replace_decomposition_artifact(
        spec,
        source_root,
        sha256=source["sha256"],
        size_bytes=source["size_bytes"],
    )

    with pytest.raises(
        replication.ReplicationError,
        match="analytics_schema_version must be an integer",
    ):
        replication.build_replication(spec, source_root)


def test_real_producer_manifest_rejects_cross_paired_decomposition_bytes(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "producer-first"
    first_spec, factors = _producer_fixture(first_root)
    _first_payload, first_manifest, first_bytes = producer.build_decomposition(
        first_spec, first_root
    )
    second_root = tmp_path / "producer-second"
    second_spec, _second_factors = _producer_fixture(
        second_root, analysis_lens="ETF_STYLE"
    )
    _second_payload, _second_manifest, second_bytes = producer.build_decomposition(
        second_spec, second_root
    )
    assert first_bytes != second_bytes

    source_root = tmp_path / "replication-source"
    source_root.mkdir()
    decomposition_path = source_root / "decomposition.json"
    generation_path = source_root / "generation.json"
    factor_path = source_root / "factors.csv"
    decomposition_path.write_bytes(first_bytes)
    generation_path.write_bytes(first_manifest.to_json_bytes())
    factor_path.write_bytes(factors.read_bytes())
    spec = tmp_path / "replication-spec.json"
    _write_json(
        spec,
        {
            "as_of_date": "2025-07-30",
            "method_label": replication.METHOD_LABEL,
            "policy": {
                "cost_sensitivities_bps": [0, 10],
                "max_absolute_factor_weight": "1.5",
                "max_gross_factor_exposure": "2",
                "periods_per_year": 252,
                "selected_cost_bps": 10,
            },
            "schema_version": 1,
            "sources": {
                "decomposition": _source(decomposition_path),
                "decomposition_generation": _source(generation_path),
                "decomposition_generation_id": first_manifest.generation_id,
                "factors": _source(factor_path),
            },
        },
    )

    matched, _manifest, _content = replication.build_replication(spec, source_root)
    assert matched["replication_results"]

    decomposition_path.write_bytes(second_bytes)
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition"] = _source(decomposition_path)
    _write_json(spec, spec_value)
    with pytest.raises(replication.ReplicationError, match="artifact hash differs"):
        replication.build_replication(spec, source_root)


def test_diagnostic_source_model_cannot_be_promoted(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)
    decomposition_path = source_root / "decomposition.json"
    value = json.loads(decomposition_path.read_text(encoding="utf-8"))
    value["model_results"][0]["model_quality"]["decision_status"] = "DIAGNOSTIC_ONLY"
    _write_json(decomposition_path, value)
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition"] = _source(decomposition_path)
    _write_json(spec, spec_value)
    source = _source(decomposition_path)
    _replace_decomposition_artifact(
        spec,
        source_root,
        sha256=source["sha256"],
        size_bytes=source["size_bytes"],
    )

    payload, _manifest, _content = replication.build_replication(spec, source_root)

    available = next(
        row for row in payload["replication_results"] if row["model_id"] is not None
    )
    assert available["availability_status"] == "DIAGNOSTIC_ONLY_SOURCE_MODEL"


def test_source_generation_requires_complete_manifest_envelope(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)
    generation_path = source_root / "generation.json"
    _write_json(
        generation_path,
        {"generation_id": "a" * 64, "manifest_schema_version": 1},
    )
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition_generation"] = _source(generation_path)
    spec_value["sources"]["decomposition_generation_id"] = "a" * 64
    _write_json(spec, spec_value)

    with pytest.raises(
        replication.ReplicationError,
        match="generation manifest is invalid",
    ):
        replication.build_replication(spec, source_root)


def test_source_generation_must_be_verified(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)
    generation_path = source_root / "generation.json"
    accepted = GenerationManifest.from_json_bytes(generation_path.read_bytes())
    pending = GenerationManifest.build(
        as_of_date=accepted.as_of_date,
        adapter=accepted.adapter,
        coverage=accepted.coverage,
        artifacts=accepted.artifacts,
        verification=VerificationStatus.PENDING,
    )
    generation_path.write_bytes(pending.to_json_bytes())
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition_generation"] = _source(generation_path)
    spec_value["sources"]["decomposition_generation_id"] = pending.generation_id
    _write_json(spec, spec_value)

    with pytest.raises(replication.ReplicationError, match="is not VERIFIED"):
        replication.build_replication(spec, source_root)


def test_source_forecast_must_independently_reproduce(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)
    decomposition_path = source_root / "decomposition.json"
    value = json.loads(decomposition_path.read_text(encoding="utf-8"))
    value["model_results"][0]["diagnostics"]["dynamic"]["holdout_forecasts"][0][
        "predicted_excess_return"
    ] = "0.99"
    _write_json(decomposition_path, value)
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition"] = _source(decomposition_path)
    _write_json(spec, spec_value)
    source = _source(decomposition_path)
    _replace_decomposition_artifact(
        spec,
        source_root,
        sha256=source["sha256"],
        size_bytes=source["size_bytes"],
    )

    with pytest.raises(replication.ReplicationError, match="does not reproduce"):
        replication.build_replication(spec, source_root)


def test_candidate_verification_is_byte_exact(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)
    output_root = tmp_path / "output"
    staged = replication.stage_candidate(spec, source_root, output_root)
    candidate = Path(staged["candidate"])

    verified = replication.verify_candidate(spec, source_root, candidate)
    assert verified["verification"] == "VERIFIED_EXACT_REBUILD"

    (candidate / "replication.json").write_text("{}", encoding="utf-8")
    with pytest.raises(replication.ReplicationError, match="exact rebuild"):
        replication.verify_candidate(spec, source_root, candidate)
