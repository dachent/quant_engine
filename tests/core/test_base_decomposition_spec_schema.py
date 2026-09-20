"""The base decomposition spec schema must reject the drift that stalled a delivery run.

A template produced before source-period interval policy existed carried `series_policies`
entries with empty `source_segments` and empty `declared_unavailable_labels`. Both sides were
internally consistent, nothing validated the artifact across the seam, and the decomposition
producer could not reach its `SOURCE_SEGMENT_GAP` tolerance path -- so the run stopped on the
first declared provider NAV gap.

These tests pin the two properties that matter: a real specification validates, and each way
the artifact can silently lose its interval policy is refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "portfolio" / "base-decomposition-spec.schema.json"
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _specification() -> dict[str, Any]:
    policy = {
        "finality": "FINALIZED_THROUGH_DECLARED_LABEL",
        "finalized_through": "2026-09-17",
        "gap_disposition": "REJECT_BUCKET",
        "native_calendar": "SESSION_LABELLED",
        "native_label_convention": "PUBLISHED_DAILY_LABEL",
        "return_basis": "NET",
        "source_vintage": "SELECTED_SNAPSHOT",
        "declared_unavailable_labels": [
            {
                "date": "2026-01-27",
                "reason": "provider NAV endpoint returned no data for this date",
                "source_reference": {
                    "source_id": "matrixport",
                    "generation_id": "d" * 64,
                    "manifest_sha256": "e" * 64,
                    "declared_exclusion_item": "nav_missing_date:2026-01-27",
                },
            }
        ],
        "source_segments": [
            {
                "observed_start": "2024-10-16",
                "observed_end": "2026-09-17",
                "segment_id": "qsi:account-live",
                "source_vintage": "f" * 64,
            }
        ],
    }
    return {
        "schema_version": 1,
        "analysis_lens": "HISTORICAL_MINIMUM_PROOF",
        "as_of_date": "2026-09-19",
        "evidence_mode": "LIVE_ONLY",
        "maximum_factor_staleness_days": 10,
        "series_composition": {"QSI": {"layers": ["ACCOUNT_LIVE"]}},
        "target_source": {
            "artifact_id": "frozen/targets.csv",
            "relative_path": "targets.csv",
            "schema_name": "portfolio-target-returns",
            "schema_version": "1",
            "expected_sha256": "a" * 64,
            "expected_size_bytes": 1024,
            "interval_policy": {
                "currency": "USD",
                "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
                "series_policies": {"QSI": policy},
            },
        },
        "factor_source": {
            "artifact_id": "frozen/factors.csv",
            "relative_path": "factors.csv",
            "schema_name": "portfolio-factor-returns",
            "schema_version": "1",
            "date_column": "date",
            "risk_free_column": "RF",
            "expected_sha256": "b" * 64,
            "expected_size_bytes": 2048,
            "factors": [
                {"column": "F1", "factor_id": "factor-one", "return_type": "EXCESS"}
            ],
        },
        "provenance_sources": [
            {
                "artifact_id": "frozen/provenance.json",
                "relative_path": "provenance.json",
                "schema_name": "provenance",
                "schema_version": "1",
                "expected_sha256": "c" * 64,
                "expected_size_bytes": 16,
            }
        ],
        "models": [
            {
                "model_id": "qsi-01",
                "series_id": "QSI",
                "constraint": "UNCONSTRAINED",
                "cost_coverage": "INCOMPLETE",
                "cv_folds": 3,
                "factor_ids": ["factor-one"],
                "minimum_train_observations": 40,
                "minimum_test_observations": 20,
                "parsimony_tolerance": "0.000001",
                "periods_per_year": 252,
                "ridge_candidates": ["0", "0.0001"],
                "train_end": "2026-06-30",
                "test_start": "2026-07-01",
            }
        ],
    }


def test_a_real_specification_validates(schema: dict[str, Any]) -> None:
    jsonschema.validate(_specification(), schema)


def test_series_policy_without_source_segments_is_rejected(
    schema: dict[str, Any],
) -> None:
    """The exact shape the v12 template carried: the key simply absent."""
    specification = _specification()
    del specification["target_source"]["interval_policy"]["series_policies"]["QSI"][
        "source_segments"
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_series_policy_without_declared_unavailable_labels_is_rejected(
    schema: dict[str, Any],
) -> None:
    specification = _specification()
    del specification["target_source"]["interval_policy"]["series_policies"]["QSI"][
        "declared_unavailable_labels"
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_empty_declarations_are_accepted(schema: dict[str, Any]) -> None:
    """An empty list is a declared absence and must remain expressible."""
    specification = _specification()
    policy = specification["target_source"]["interval_policy"]["series_policies"]["QSI"]
    policy["source_segments"] = []
    policy["declared_unavailable_labels"] = []
    jsonschema.validate(specification, schema)


def test_interval_policy_without_series_policies_is_rejected(
    schema: dict[str, Any],
) -> None:
    specification = _specification()
    del specification["target_source"]["interval_policy"]["series_policies"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_empty_series_policies_is_rejected(schema: dict[str, Any]) -> None:
    specification = _specification()
    specification["target_source"]["interval_policy"]["series_policies"] = {}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_unknown_evidence_mode_is_rejected(schema: dict[str, Any]) -> None:
    specification = _specification()
    specification["evidence_mode"] = "LIVE_PLUS_SOMETHING"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_malformed_expected_digest_is_rejected(schema: dict[str, Any]) -> None:
    specification = _specification()
    specification["target_source"]["expected_sha256"] = "not-a-digest"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_model_without_factor_ids_is_rejected(schema: dict[str, Any]) -> None:
    specification = _specification()
    specification["models"][0]["factor_ids"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_unexplained_unavailable_label_is_rejected(schema: dict[str, Any]) -> None:
    """A declared gap without a reason and a source is not admissible."""
    specification = _specification()
    policy = specification["target_source"]["interval_policy"]["series_policies"]["QSI"]
    policy["declared_unavailable_labels"] = [{"date": "2026-01-27"}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)


def test_source_segment_without_vintage_is_rejected(schema: dict[str, Any]) -> None:
    """A segment must say which source generation observed it."""
    specification = _specification()
    policy = specification["target_source"]["interval_policy"]["series_policies"]["QSI"]
    del policy["source_segments"][0]["source_vintage"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(specification, schema)
