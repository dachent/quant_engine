from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_replication.py"
SPEC = importlib.util.spec_from_file_location("portfolio_replication", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio replication builder")
replication = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replication
SPEC.loader.exec_module(replication)


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
    generation_id = "a" * 64
    _write_json(
        generation,
        {
            "generation_id": generation_id,
            "manifest_schema_version": 1,
        },
    )
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


def test_diagnostic_source_model_cannot_be_promoted(tmp_path: Path) -> None:
    spec, source_root = _fixture(tmp_path)
    decomposition_path = source_root / "decomposition.json"
    value = json.loads(decomposition_path.read_text(encoding="utf-8"))
    value["model_results"][0]["model_quality"]["decision_status"] = "DIAGNOSTIC_ONLY"
    _write_json(decomposition_path, value)
    spec_value = json.loads(spec.read_text(encoding="utf-8"))
    spec_value["sources"]["decomposition"] = _source(decomposition_path)
    _write_json(spec, spec_value)

    payload, _manifest, _content = replication.build_replication(spec, source_root)

    available = next(
        row for row in payload["replication_results"] if row["model_id"] is not None
    )
    assert available["availability_status"] == "DIAGNOSTIC_ONLY_SOURCE_MODEL"


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
