"""Execute the governed add/replace/resize diligence rubric.

The runner consumes an immutable portfolio-dependence artifact and a separate
candidate evidence record.  It does not acquire data, infer missing facts,
choose an allocation, or create execution authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Final, Mapping, Sequence, cast

import numpy as np
import numpy.typing as npt

from quant_engine.core.contracts import canonical_json_bytes


RUBRIC_CONTRACT: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-diligence-rubric-v1.json"
)
ECONOMIC_DRIVER_CLUSTERS: Final = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-economic-driver-clusters-v1.json"
)
EXPECTED_GATES: Final = (
    "record_lineage",
    "subscriber_realism",
    "tail_boundary",
    "portfolio_hwm",
    "net_economics",
    "operational_fit",
    "independence",
    "capacity_liquidity",
)
HASH_LENGTH: Final = 64
FloatArray = npt.NDArray[np.float64]


class DiligenceError(ValueError):
    """The rubric contract or one of its evidence artifacts is invalid."""


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiligenceError(f"{name} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise DiligenceError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DiligenceError(f"{name} must be an object")
    return cast(Mapping[str, Any], value)


def _objects(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DiligenceError(f"{name} must be an array of objects")
    return cast(list[Mapping[str, Any]], value)


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DiligenceError(f"{name} must be an array of strings")
    return cast(list[str], value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiligenceError(f"{name} must be non-empty text")
    return value.strip()


def _number_value(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise DiligenceError(f"{name} must be numeric")
    try:
        accepted = float(cast(float | int | str, value))
    except (TypeError, ValueError) as exc:
        raise DiligenceError(f"{name} must be numeric") from exc
    if not math.isfinite(accepted) or (minimum is not None and accepted < minimum):
        raise DiligenceError(f"{name} is outside its accepted range")
    return accepted


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    accepted = _number_value(value, name, minimum=float(minimum))
    if not accepted.is_integer():
        raise DiligenceError(f"{name} must be an integer")
    return int(accepted)


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise DiligenceError("rubric result is non-finite")
    if abs(value) < 5e-16:
        value = 0.0
    return format(value, ".12g")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == HASH_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _missing_fields(block: Mapping[str, Any], fields: Sequence[str]) -> list[str]:
    return [
        field
        for field in fields
        if field not in block or block[field] is None or block[field] == ""
    ]


def _gate(
    gate_id: str,
    status: str,
    reasons: Sequence[str],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if status not in {"PASS", "FAIL", "UNAVAILABLE"}:
        raise DiligenceError(f"unsupported gate status for {gate_id}")
    return {
        "gate_id": gate_id,
        "status": status,
        "reasons": sorted(set(reasons)),
        "evidence": dict(evidence),
    }


def _required_by_gate(
    contract: Mapping[str, Any],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    gates = _objects(contract.get("gates"), "rubric gates")
    parsed: dict[str, tuple[str, tuple[str, ...]]] = {}
    for gate in gates:
        gate_id = _text(gate.get("gate_id"), "gate_id")
        parsed[gate_id] = (
            _text(gate.get("evidence_block"), f"{gate_id}.evidence_block"),
            tuple(_strings(gate.get("required_fields"), f"{gate_id}.required_fields")),
        )
    if tuple(parsed) != EXPECTED_GATES:
        raise DiligenceError("rubric gate order or identity differs")
    return parsed


def _project_correlation(matrix: FloatArray, floor: float) -> FloatArray:
    symmetric = (matrix + matrix.T) / 2.0
    values, vectors = np.linalg.eigh(symmetric)
    clipped = np.maximum(values, floor)
    positive = vectors @ np.diag(clipped) @ vectors.T
    scale = np.sqrt(np.diag(positive))
    projected = positive / np.outer(scale, scale)
    projected = np.clip(projected, -1.0, 1.0)
    np.fill_diagonal(projected, 1.0)
    return cast(FloatArray, projected.astype(np.float64))


def _central_loss(vector: FloatArray, matrix: FloatArray) -> float:
    squared = float(vector @ matrix @ vector)
    if squared < -1e-12:
        raise DiligenceError("central-case loss is negative")
    return math.sqrt(max(0.0, squared))


def _dependence_parts(
    source: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    dependence_value = source.get("dependence")
    dependence = (
        _object(dependence_value, "dependence")
        if dependence_value is not None
        else source
    )
    policy = _object(source.get("policy"), "dependence policy")
    return dependence, policy


def _with_cluster_budget(
    dependence: Mapping[str, Any],
    cluster_contract: Mapping[str, Any],
    *,
    cluster_contract_sha256: str,
) -> dict[str, Any]:
    if cluster_contract.get("schema_version") != 1:
        raise DiligenceError("economic driver cluster contract is unsupported")
    series_ids = _strings(dependence.get("series_ids"), "dependence series_ids")
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    central = _object(dependence.get("central_case"), "central_case")
    component_index = {
        _text(row.get("series_id"), "correlation-one component series_id"): row
        for row in _objects(
            correlation_one.get("components"), "correlation-one components"
        )
    }
    if set(component_index) != set(series_ids):
        raise DiligenceError("correlation-one components differ from portfolio series")
    definitions = _objects(cluster_contract.get("clusters"), "driver clusters")
    declared_members = [
        member
        for cluster in definitions
        for member in _strings(cluster.get("members"), "cluster members")
    ]
    if len(declared_members) != len(set(declared_members)) or set(
        declared_members
    ) != set(series_ids):
        raise DiligenceError(
            "economic driver clusters differ from the current portfolio"
        )
    limit = _number_value(
        cluster_contract.get("systematic_loss_limit"),
        "systematic loss limit",
        minimum=0,
    )
    clusters: list[dict[str, Any]] = []
    for definition in definitions:
        members = _strings(definition.get("members"), "cluster members")
        contribution = sum(
            _number_value(
                component_index[member].get("loss_contribution"),
                f"loss contribution {member}",
                minimum=0,
            )
            for member in members
        )
        clusters.append(
            {
                "cluster_id": _text(definition.get("cluster_id"), "cluster_id"),
                "label": _text(definition.get("label"), "cluster label"),
                "members": members,
                "loss_contribution": _number(contribution),
                "budget_status": (
                    "BREACHES_SYSTEMATIC_LOSS_LIMIT"
                    if contribution > limit
                    else "WITHIN_SYSTEMATIC_LOSS_LIMIT"
                ),
            }
        )
    accepted = dict(dependence)
    accepted["economic_driver_cluster_budget"] = {
        "budget_rule": _text(cluster_contract.get("budget_rule"), "budget_rule"),
        "clusters": clusters,
        "contract_sha256": cluster_contract_sha256,
        "central_case_loss": central.get("loss"),
        "correlation_one_loss": correlation_one.get("loss"),
        "systematic_loss_limit": _number(limit),
    }
    return accepted


def _leave_one_out(dependence: Mapping[str, Any]) -> list[dict[str, Any]]:
    series_ids = _strings(dependence.get("series_ids"), "dependence series_ids")
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    central = _object(dependence.get("central_case"), "central_case")
    cluster_budget = _object(
        dependence.get("economic_driver_cluster_budget"), "cluster budget"
    )
    conservative_total = _number_value(
        correlation_one.get("loss"), "correlation_one.loss", minimum=0
    )
    central_total = _number_value(central.get("loss"), "central_case.loss", minimum=0)
    conservative_components = {
        _text(row.get("series_id"), "correlation_one component series_id"): row
        for row in _objects(
            correlation_one.get("components"), "correlation_one components"
        )
    }
    central_components = {
        _text(row.get("series_id"), "central component series_id"): row
        for row in _objects(central.get("components"), "central components")
    }
    if set(conservative_components) != set(series_ids) or set(
        central_components
    ) != set(series_ids):
        raise DiligenceError("dependence component identities differ")
    matrix = np.asarray(
        _object(central.get("dependence_matrix"), "central dependence matrix").get(
            "values"
        ),
        dtype=np.float64,
    )
    if matrix.shape != (len(series_ids), len(series_ids)):
        raise DiligenceError("central dependence matrix dimensions differ")
    vector = np.asarray(
        [
            _number_value(
                central_components[series_id].get("loss_scale_contribution"),
                f"central loss scale {series_id}",
                minimum=0,
            )
            for series_id in series_ids
        ],
        dtype=np.float64,
    )
    limit = _number_value(
        cluster_budget.get("systematic_loss_limit"), "systematic loss limit", minimum=0
    )
    clusters = _objects(cluster_budget.get("clusters"), "driver clusters")
    cluster_by_member = {
        member: cluster
        for cluster in clusters
        for member in _strings(cluster.get("members"), "cluster members")
    }
    rows: list[dict[str, Any]] = []
    for index, series_id in enumerate(series_ids):
        kept = [item for item in range(len(series_ids)) if item != index]
        reduced_central = _central_loss(vector[kept], matrix[np.ix_(kept, kept)])
        conservative_contribution = _number_value(
            conservative_components[series_id].get("loss_contribution"),
            f"conservative loss contribution {series_id}",
            minimum=0,
        )
        reduced_conservative = conservative_total - conservative_contribution
        cluster = cluster_by_member.get(series_id)
        cluster_loss = (
            _number_value(
                cluster.get("loss_contribution"),
                f"cluster loss contribution {series_id}",
                minimum=0,
            )
            - conservative_contribution
            if cluster is not None
            else None
        )
        rows.append(
            {
                "series_id": series_id,
                "correlation_one_loss_without": _number(reduced_conservative),
                "correlation_one_loss_reduction": _number(conservative_contribution),
                "central_case_loss_without": _number(reduced_central),
                "central_case_loss_reduction": _number(central_total - reduced_central),
                "economic_driver_cluster_id": (
                    cluster.get("cluster_id") if cluster is not None else None
                ),
                "cluster_loss_without": (
                    _number(cluster_loss) if cluster_loss is not None else None
                ),
                "cluster_budget_status_without": (
                    "BREACHES_SYSTEMATIC_LOSS_LIMIT"
                    if cluster_loss is not None and cluster_loss > limit
                    else "WITHIN_SYSTEMATIC_LOSS_LIMIT"
                ),
            }
        )
    return rows


def _scenario(
    dependence: Mapping[str, Any],
    policy: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    scenario = _object(candidate.get("scenario"), "candidate scenario")
    mode = _text(scenario.get("mode"), "scenario.mode")
    if mode not in {"ADD", "REPLACE", "RESIZE"}:
        raise DiligenceError("scenario.mode must be ADD, REPLACE, or RESIZE")
    funding_source = _text(scenario.get("funding_source"), "scenario.funding_source")
    series_ids = _strings(dependence.get("series_ids"), "dependence series_ids")
    correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
    central = _object(dependence.get("central_case"), "central_case")
    cluster_budget = _object(
        dependence.get("economic_driver_cluster_budget"), "cluster budget"
    )
    conservative_total = _number_value(
        correlation_one.get("loss"), "correlation_one.loss", minimum=0
    )
    central_total = _number_value(central.get("loss"), "central_case.loss", minimum=0)
    conservative_components = {
        _text(row.get("series_id"), "correlation_one component series_id"): row
        for row in _objects(
            correlation_one.get("components"), "correlation_one components"
        )
    }
    central_components = {
        _text(row.get("series_id"), "central component series_id"): row
        for row in _objects(central.get("components"), "central components")
    }
    base_matrix = np.asarray(
        _object(central.get("dependence_matrix"), "central dependence matrix").get(
            "values"
        ),
        dtype=np.float64,
    )
    replacement = scenario.get("replacement_series_id")
    if mode in {"REPLACE", "RESIZE"}:
        replacement_id = _text(replacement, "scenario.replacement_series_id")
        if replacement_id not in series_ids:
            raise DiligenceError("replacement series is not in the current portfolio")
    elif replacement is not None:
        raise DiligenceError("ADD scenario cannot name replacement_series_id")
    else:
        replacement_id = None
    kept_ids = [item for item in series_ids if item != replacement_id]
    kept_indices = [series_ids.index(item) for item in kept_ids]
    kept_matrix = base_matrix[np.ix_(kept_indices, kept_indices)]
    kept_vector = [
        _number_value(
            central_components[item].get("loss_scale_contribution"),
            f"central loss scale {item}",
            minimum=0,
        )
        for item in kept_ids
    ]
    weight = _number_value(
        scenario.get("systematic_weight"), "systematic_weight", minimum=0
    )
    if weight > 1:
        raise DiligenceError("systematic_weight must not exceed one")
    conservative_fraction = _number_value(
        scenario.get("conservative_loss_fraction"),
        "conservative_loss_fraction",
        minimum=0,
    )
    central_fraction = _number_value(
        scenario.get("central_loss_fraction"), "central_loss_fraction", minimum=0
    )
    candidate_conservative = weight * conservative_fraction
    candidate_central_scale = weight * central_fraction
    retained_weight = sum(
        _number_value(
            conservative_components[item].get("allocation_weight"),
            f"allocation weight {item}",
            minimum=0,
        )
        for item in kept_ids
    )
    if retained_weight <= 0 and weight < 1:
        raise DiligenceError("retained portfolio weight is unavailable")
    retained_scale = 0.0 if weight == 1 else (1 - weight) / retained_weight
    kept_vector = [value * retained_scale for value in kept_vector]
    retained_conservative = sum(
        _number_value(
            conservative_components[item].get("loss_contribution"),
            f"loss contribution {item}",
            minimum=0,
        )
        * retained_scale
        for item in kept_ids
    )
    conservative_loss = retained_conservative + candidate_conservative
    correlation_value = candidate.get("correlations")
    correlation_rows = {
        _text(row.get("series_id"), "candidate correlation series_id"): row
        for row in (
            _objects(correlation_value, "candidate correlations")
            if correlation_value is not None
            else []
        )
    }
    minimum_observations = _integer(
        policy.get("minimum_observations"), "minimum_observations", minimum=1
    )
    missing_correlations = sorted(set(kept_ids) - set(correlation_rows))
    ineligible_correlations = sorted(
        series_id
        for series_id in kept_ids
        if series_id in correlation_rows
        and (
            correlation_rows[series_id].get("status") != "AVAILABLE"
            or _integer(
                correlation_rows[series_id].get("overlap_observations", 0),
                f"overlap observations {series_id}",
            )
            < minimum_observations
        )
    )
    if missing_correlations or ineligible_correlations:
        central_status = "UNAVAILABLE"
        central_loss: float | None = None
        projection: dict[str, Any] | None = None
    else:
        candidate_correlations = np.asarray(
            [
                _number_value(
                    correlation_rows[series_id].get("pearson"),
                    f"candidate pearson {series_id}",
                )
                for series_id in kept_ids
            ],
            dtype=np.float64,
        )
        if np.any(candidate_correlations < -1) or np.any(candidate_correlations > 1):
            raise DiligenceError("candidate correlation is outside [-1, 1]")
        augmented = np.eye(len(kept_ids) + 1, dtype=np.float64)
        augmented[:-1, :-1] = kept_matrix
        augmented[-1, :-1] = candidate_correlations
        augmented[:-1, -1] = candidate_correlations
        floor = _number_value(
            scenario.get("projection_eigenvalue_floor"),
            "projection_eigenvalue_floor",
            minimum=0,
        )
        projected = _project_correlation(augmented, floor)
        vector = np.asarray([*kept_vector, candidate_central_scale], dtype=np.float64)
        central_loss = _central_loss(vector, projected)
        central_status = "AVAILABLE"
        projection = {
            "authority": _text(
                scenario.get("projection_authority"), "projection_authority"
            ),
            "eigenvalue_floor": _number(floor),
            "minimum_eigenvalue_pre_projection": _number(
                float(np.min(np.linalg.eigvalsh(augmented)))
            ),
            "minimum_eigenvalue_post_projection": _number(
                float(np.min(np.linalg.eigvalsh(projected)))
            ),
            "maximum_absolute_adjustment": _number(
                float(np.max(np.abs(projected - augmented)))
            ),
        }
    limit = _number_value(
        cluster_budget.get("systematic_loss_limit"), "systematic loss limit", minimum=0
    )
    clusters = {
        _text(row.get("cluster_id"), "cluster_id"): row
        for row in _objects(cluster_budget.get("clusters"), "driver clusters")
    }
    cluster_id = _text(
        scenario.get("economic_driver_cluster_id"), "economic_driver_cluster_id"
    )
    cluster = clusters.get(cluster_id)
    cluster_effects: list[dict[str, Any]] = []
    for current_cluster_id, current_cluster in clusters.items():
        members = _strings(current_cluster.get("members"), "cluster members")
        base_loss = _number_value(
            current_cluster.get("loss_contribution"),
            f"cluster loss {current_cluster_id}",
            minimum=0,
        )
        adjusted_loss = sum(
            _number_value(
                conservative_components[member].get("loss_contribution"),
                f"loss contribution {member}",
                minimum=0,
            )
            * retained_scale
            for member in members
            if member != replacement_id
        )
        if current_cluster_id == cluster_id:
            adjusted_loss += candidate_conservative
        cluster_effects.append(
            {
                "cluster_id": current_cluster_id,
                "classification": "EXISTING_GOVERNED_CLUSTER",
                "baseline_loss": _number(base_loss),
                "scenario_loss": _number(adjusted_loss),
                "status": "PASS" if adjusted_loss <= limit else "FAIL",
            }
        )
    if cluster_id not in clusters:
        cluster_effects.append(
            {
                "cluster_id": cluster_id,
                "classification": "CANDIDATE_DECLARED_NEW_CLUSTER",
                "baseline_loss": "0",
                "scenario_loss": _number(candidate_conservative),
                "status": "PASS" if candidate_conservative <= limit else "FAIL",
            }
        )
    cluster_effects.sort(key=lambda row: cast(str, row["cluster_id"]))
    candidate_cluster_effect = next(
        row for row in cluster_effects if row["cluster_id"] == cluster_id
    )
    return {
        "mode": mode,
        "funding_source": funding_source,
        "weighting_method": "CANDIDATE_POST_SCENARIO_WEIGHT_RETAINED_SLEEVES_PRO_RATA",
        "retained_sleeve_weight_scale": _number(retained_scale),
        "replacement_series_id": replacement_id,
        "baseline": {
            "correlation_one_loss": _number(conservative_total),
            "central_case_loss": _number(central_total),
        },
        "candidate": {
            "systematic_weight": _number(weight),
            "conservative_loss_fraction": _number(conservative_fraction),
            "central_loss_fraction": _number(central_fraction),
            "correlation_one_loss_contribution": _number(candidate_conservative),
            "central_loss_scale_contribution": _number(candidate_central_scale),
        },
        "result": {
            "correlation_one_loss": _number(conservative_loss),
            "central_case_loss": _number(central_loss)
            if central_loss is not None
            else None,
            "central_case_status": central_status,
            "systematic_loss_limit": _number(limit),
            "correlation_one_status": (
                "PASS" if conservative_loss <= limit else "FAIL"
            ),
            "central_case_status_against_limit": (
                "UNAVAILABLE"
                if central_loss is None
                else "PASS"
                if central_loss <= limit
                else "FAIL"
            ),
            "economic_driver_cluster_id": cluster_id,
            "economic_driver_cluster_status": (
                "EXISTING_GOVERNED_CLUSTER"
                if cluster is not None
                else "CANDIDATE_DECLARED_NEW_CLUSTER"
            ),
            "economic_driver_cluster_authority": _text(
                scenario.get("economic_driver_cluster_authority"),
                "economic_driver_cluster_authority",
            ),
            "cluster_loss": candidate_cluster_effect["scenario_loss"],
            "cluster_budget_status": (
                "FAIL"
                if any(row["status"] == "FAIL" for row in cluster_effects)
                else "PASS"
            ),
            "cluster_budget_effects": cluster_effects,
            "missing_correlations": missing_correlations,
            "ineligible_correlations": ineligible_correlations,
            "projection": projection,
        },
    }


def _simple_gate(
    gate_id: str,
    block: Mapping[str, Any],
    required: Sequence[str],
    failures: Sequence[str],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    missing = _missing_fields(block, required)
    if missing:
        return _gate(
            gate_id, "UNAVAILABLE", [f"MISSING:{item}" for item in missing], evidence
        )
    if failures:
        return _gate(gate_id, "FAIL", failures, evidence)
    return _gate(gate_id, "PASS", [], evidence)


def _replacement_comparison(candidate: Mapping[str, Any]) -> dict[str, Any]:
    scenario = _object(candidate.get("scenario"), "candidate scenario")
    if scenario.get("mode") != "REPLACE":
        return {"status": "NOT_APPLICABLE", "reasons": []}
    comparison_value = candidate.get("replacement_comparison")
    if comparison_value is None:
        return {
            "status": "UNAVAILABLE",
            "reasons": ["MISSING:replacement_comparison"],
        }
    comparison = _object(comparison_value, "replacement comparison")
    required = (
        "candidate_observed_start",
        "candidate_observed_end",
        "candidate_evidence_layer",
        "incumbent_observed_start",
        "incumbent_observed_end",
        "incumbent_evidence_layer",
    )
    missing = _missing_fields(comparison, required)
    if missing:
        return {
            "status": "UNAVAILABLE",
            "reasons": [f"MISSING:{item}" for item in missing],
        }
    pairs = (
        ("candidate_observed_start", "incumbent_observed_start", "START_DATE_MISMATCH"),
        ("candidate_observed_end", "incumbent_observed_end", "END_DATE_MISMATCH"),
        (
            "candidate_evidence_layer",
            "incumbent_evidence_layer",
            "EVIDENCE_LAYER_MISMATCH",
        ),
    )
    failures = [
        reason for left, right, reason in pairs if comparison[left] != comparison[right]
    ]
    return {"status": "FAIL" if failures else "PASS", "reasons": failures}


def build_payload(
    dependence_source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    dependence_sha256: str,
    candidate_sha256: str,
    contract: Mapping[str, Any],
    contract_sha256: str,
    cluster_contract: Mapping[str, Any],
    cluster_contract_sha256: str,
) -> dict[str, Any]:
    if (
        contract.get("schema_version") != 1
        or contract.get("rubric_id") != "PORT-RUBRIC-1"
    ):
        raise DiligenceError("rubric contract identity is unsupported")
    if candidate.get("schema_version") != 1:
        raise DiligenceError("candidate evidence schema version is unsupported")
    required = _required_by_gate(contract)
    dependence, policy = _dependence_parts(dependence_source)
    expected_series = _integer(
        contract.get("current_portfolio_series_count"),
        "current_portfolio_series_count",
        minimum=1,
    )
    actual_series = len(_strings(dependence.get("series_ids"), "dependence series_ids"))
    if actual_series != expected_series:
        raise DiligenceError(
            "current portfolio series count differs from the rubric contract"
        )
    dependence = _with_cluster_budget(
        dependence,
        cluster_contract,
        cluster_contract_sha256=cluster_contract_sha256,
    )
    scenario_block = _object(candidate.get("scenario", {}), "candidate scenario")
    scenario_missing = _missing_fields(scenario_block, required["portfolio_hwm"][1])
    if scenario_block.get("mode") in {"REPLACE", "RESIZE"} and not scenario_block.get(
        "replacement_series_id"
    ):
        scenario_missing.append("replacement_series_id")
    if scenario_missing:
        correlation_one = _object(dependence.get("correlation_one"), "correlation_one")
        central = _object(dependence.get("central_case"), "central_case")
        cluster_budget = _object(
            dependence.get("economic_driver_cluster_budget"), "cluster budget"
        )
        scenario: dict[str, Any] = {
            "status": "UNAVAILABLE",
            "reasons": [f"MISSING:{item}" for item in scenario_missing],
            "mode": scenario_block.get("mode"),
            "replacement_series_id": scenario_block.get("replacement_series_id"),
            "baseline": {
                "correlation_one_loss": correlation_one.get("loss"),
                "central_case_loss": central.get("loss"),
            },
            "candidate": {},
            "result": {
                "correlation_one_loss": None,
                "central_case_loss": None,
                "central_case_status": "UNAVAILABLE",
                "systematic_loss_limit": cluster_budget.get("systematic_loss_limit"),
                "correlation_one_status": "UNAVAILABLE",
                "central_case_status_against_limit": "UNAVAILABLE",
                "cluster_budget_status": "UNAVAILABLE",
            },
        }
    else:
        scenario = _scenario(dependence, policy, candidate)
    minimum_observations = _integer(
        policy.get("minimum_observations"), "minimum_observations", minimum=1
    )
    minimum_downside = _integer(
        policy.get("minimum_downside_observations"),
        "minimum_downside_observations",
        minimum=1,
    )
    gates: list[dict[str, Any]] = []

    record = _object(candidate.get(required["record_lineage"][0], {}), "record_lineage")
    record_failures = (
        []
        if record.get("version_matches_return_evidence") is True
        else ["VERSION_DOES_NOT_MATCH_RETURN_EVIDENCE"]
    )
    for field in ("immutable_version_sha256", "signal_archive_sha256"):
        if field in record and not _valid_hash(record[field]):
            record_failures.append(f"INVALID_SHA256:{field}")
    gates.append(
        _simple_gate(
            "record_lineage",
            record,
            required["record_lineage"][1],
            record_failures,
            {
                "ruleset_version": record.get("ruleset_version"),
                "effective_date": record.get("effective_date"),
            },
        )
    )

    subscriber = _object(
        candidate.get(required["subscriber_realism"][0], {}), "subscriber_realism"
    )
    subscriber_missing = _missing_fields(subscriber, required["subscriber_realism"][1])
    subscriber_failures: list[str] = []
    subscriber_reasons: list[str] = []
    if not subscriber_missing:
        if not _valid_hash(subscriber.get("return_series_sha256")):
            subscriber_failures.append("INVALID_SHA256:return_series_sha256")
        if subscriber.get("net_of_all_strategy_costs") is not True:
            subscriber_failures.append("RETURNS_NOT_NET_OF_ALL_STRATEGY_COSTS")
        observations = _integer(
            subscriber.get("return_observations"), "subscriber return observations"
        )
        if observations < minimum_observations:
            subscriber_reasons.append("INSUFFICIENT_RETURN_OBSERVATIONS")
        accepted_layers = _strings(
            subscriber.get("accepted_evidence_layers_for_verdict"),
            "accepted_evidence_layers_for_verdict",
        )
        if subscriber.get("evidence_layer") not in accepted_layers:
            subscriber_reasons.append("EVIDENCE_LAYER_BELOW_DECLARED_THRESHOLD")
    if subscriber_missing or subscriber_reasons:
        gates.append(
            _gate(
                "subscriber_realism",
                "UNAVAILABLE",
                [
                    *(f"MISSING:{item}" for item in subscriber_missing),
                    *subscriber_reasons,
                ],
                {"minimum_observations": minimum_observations},
            )
        )
    else:
        gates.append(
            _gate(
                "subscriber_realism",
                "FAIL" if subscriber_failures else "PASS",
                subscriber_failures,
                {
                    "minimum_observations": minimum_observations,
                    "return_observations": subscriber.get("return_observations"),
                    "evidence_layer": subscriber.get("evidence_layer"),
                    "minimum_overlap_days_for_verdict": subscriber.get(
                        "minimum_overlap_days_for_verdict"
                    ),
                    "evidence_threshold_authority": subscriber.get(
                        "evidence_threshold_authority"
                    ),
                },
            )
        )

    tail = _object(candidate.get(required["tail_boundary"][0], {}), "tail_boundary")
    tail_missing = _missing_fields(tail, required["tail_boundary"][1])
    tail_failures: list[str] = []
    for field in ("position_risk_cube_sha256", "payoff_reproduction_sha256"):
        if field in tail and not _valid_hash(tail[field]):
            tail_failures.append(f"INVALID_SHA256:{field}")
    if not tail_missing and _number_value(
        tail.get("worst_loss_fraction"), "worst_loss_fraction", minimum=0
    ) > _number_value(
        tail.get("stated_loss_limit_fraction"), "stated_loss_limit_fraction", minimum=0
    ):
        tail_failures.append("WORST_LOSS_EXCEEDS_STATED_LIMIT")
    gates.append(
        _simple_gate(
            "tail_boundary",
            tail,
            required["tail_boundary"][1],
            tail_failures,
            {
                "worst_loss_fraction": tail.get("worst_loss_fraction"),
                "stated_loss_limit_fraction": tail.get("stated_loss_limit_fraction"),
            },
        )
    )

    scenario_result = _object(scenario.get("result"), "scenario result")
    hwm_statuses = {
        scenario_result.get("correlation_one_status"),
        scenario_result.get("central_case_status_against_limit"),
        scenario_result.get("cluster_budget_status"),
    }
    hwm_status = (
        "FAIL"
        if "FAIL" in hwm_statuses
        else "UNAVAILABLE"
        if "UNAVAILABLE" in hwm_statuses
        else "PASS"
    )
    hwm_reasons = list(cast(list[str], scenario.get("reasons", [])))
    hwm_reasons.extend(
        f"{name}:{scenario_result.get(name)}"
        for name in (
            "correlation_one_status",
            "central_case_status_against_limit",
            "cluster_budget_status",
        )
        if scenario_result.get(name) != "PASS"
    )
    hwm_reasons.extend(
        f"MISSING_CORRELATION:{series_id}"
        for series_id in cast(
            list[str], scenario_result.get("missing_correlations", [])
        )
    )
    hwm_reasons.extend(
        f"INELIGIBLE_CORRELATION:{series_id}"
        for series_id in cast(
            list[str], scenario_result.get("ineligible_correlations", [])
        )
    )
    gates.append(
        _gate(
            "portfolio_hwm",
            hwm_status,
            hwm_reasons,
            {
                "correlation_one_loss": scenario_result.get("correlation_one_loss"),
                "central_case_loss": scenario_result.get("central_case_loss"),
                "systematic_loss_limit": scenario_result.get("systematic_loss_limit"),
            },
        )
    )

    economics = _object(
        candidate.get(required["net_economics"][0], {}), "net_economics"
    )
    economics_missing = _missing_fields(economics, required["net_economics"][1])
    economics_evidence: dict[str, Any] = {}
    if not economics_missing:
        notional = _number_value(
            economics.get("sleeve_notional"), "sleeve_notional", minimum=0
        )
        annual_cost = (
            _number_value(
                economics.get("subscription_annual"), "subscription_annual", minimum=0
            )
            + notional
            * (
                _number_value(
                    economics.get("expense_ratio"), "expense_ratio", minimum=0
                )
                + _number_value(
                    economics.get("spread_fraction"), "spread_fraction", minimum=0
                )
            )
            + _number_value(
                economics.get("commissions_annual"), "commissions_annual", minimum=0
            )
            + _number_value(
                economics.get("tax_cost_annual"), "tax_cost_annual", minimum=0
            )
        )
        economics_evidence = {
            "annual_cost_dollars": _number(annual_cost),
            "annual_cost_fraction": _number(annual_cost / notional)
            if notional
            else None,
            "tax_character": economics.get("tax_character"),
        }
    gates.append(
        _simple_gate(
            "net_economics",
            economics,
            required["net_economics"][1],
            [],
            economics_evidence,
        )
    )

    operations = _object(
        candidate.get(required["operational_fit"][0], {}), "operational_fit"
    )
    operations_failures = [
        f"FALSE:{field}"
        for field in required["operational_fit"][1]
        if field in operations and operations[field] is not True
    ]
    gates.append(
        _simple_gate(
            "operational_fit",
            operations,
            required["operational_fit"][1],
            operations_failures,
            {field: operations.get(field) for field in required["operational_fit"][1]},
        )
    )

    independence = _object(
        candidate.get(required["independence"][0], {}), "independence"
    )
    independence_missing = _missing_fields(independence, required["independence"][1])
    correlation_value = candidate.get("correlations")
    correlation_rows = (
        _objects(correlation_value, "candidate correlations")
        if correlation_value is not None
        else []
    )
    cluster_correlation_value = candidate.get("cluster_correlations")
    cluster_correlation_rows = (
        _objects(cluster_correlation_value, "candidate cluster correlations")
        if cluster_correlation_value is not None
        else []
    )
    independence_reasons: list[str] = []
    if correlation_value is None:
        independence_reasons.append("MISSING:correlations")
    if cluster_correlation_value is None:
        independence_reasons.append("MISSING:cluster_correlations")
    independence_reasons.extend(
        f"MISSING_SUBSCRIBER_THRESHOLD:{field}" for field in subscriber_missing
    )
    independence_failures: list[str] = []
    if not independence_missing and not subscriber_missing:
        maximum_downside = _number_value(
            independence.get("maximum_downside_correlation_for_pass"),
            "maximum_downside_correlation_for_pass",
        )
        minimum_worst = _number_value(
            independence.get("minimum_worst_decile_return_for_pass"),
            "minimum_worst_decile_return_for_pass",
        )
        accepted_layers = _strings(
            subscriber.get("accepted_evidence_layers_for_verdict"),
            "accepted_evidence_layers_for_verdict",
        )
        minimum_overlap_days = _integer(
            subscriber.get("minimum_overlap_days_for_verdict"),
            "minimum_overlap_days_for_verdict",
            minimum=1,
        )
        current_cluster_ids = {
            _text(row.get("cluster_id"), "cluster_id")
            for row in _objects(
                _object(
                    dependence.get("economic_driver_cluster_budget"),
                    "cluster budget",
                ).get("clusters"),
                "driver clusters",
            )
        }
        supplied_cluster_ids = {
            _text(row.get("cluster_id"), "cluster correlation cluster_id")
            for row in cluster_correlation_rows
        }
        independence_reasons.extend(
            f"MISSING_CLUSTER_CORRELATION:{cluster_id}"
            for cluster_id in sorted(current_cluster_ids - supplied_cluster_ids)
        )
        current_series_ids = set(
            _strings(dependence.get("series_ids"), "dependence series_ids")
        )
        supplied_series_ids = {
            _text(row.get("series_id"), "correlation series_id")
            for row in correlation_rows
        }
        independence_reasons.extend(
            f"MISSING_SERIES_CORRELATION:{series_id}"
            for series_id in sorted(current_series_ids - supplied_series_ids)
        )
        scoped_rows = [
            ("SERIES", _text(row.get("series_id"), "correlation series_id"), row)
            for row in correlation_rows
        ] + [
            (
                "CLUSTER",
                _text(row.get("cluster_id"), "cluster correlation cluster_id"),
                row,
            )
            for row in cluster_correlation_rows
        ]
        for scope, row_id, row in scoped_rows:
            if row.get("status") != "AVAILABLE":
                independence_reasons.append(f"UNAVAILABLE_CORRELATION:{scope}:{row_id}")
                continue
            if (
                _integer(row.get("overlap_observations", 0), "overlap observations")
                < minimum_observations
            ):
                independence_reasons.append(
                    f"INSUFFICIENT_OBSERVATIONS:{scope}:{row_id}"
                )
            if (
                _integer(row.get("downside_observations", 0), "downside observations")
                < minimum_downside
            ):
                independence_reasons.append(f"INSUFFICIENT_DOWNSIDE:{scope}:{row_id}")
            if (
                _integer(row.get("overlap_calendar_days", 0), "overlap calendar days")
                < minimum_overlap_days
            ):
                independence_reasons.append(
                    f"INSUFFICIENT_CALENDAR_OVERLAP:{scope}:{row_id}"
                )
            if row.get("evidence_layer") not in accepted_layers:
                independence_reasons.append(
                    f"EVIDENCE_LAYER_BELOW_THRESHOLD:{scope}:{row_id}"
                )
            if (
                _number_value(row.get("downside_correlation"), "downside_correlation")
                > maximum_downside
            ):
                independence_failures.append(
                    f"DOWNSIDE_THRESHOLD_FAILED:{scope}:{row_id}"
                )
            if (
                _number_value(
                    row.get("worst_decile_average_return"),
                    "worst_decile_average_return",
                )
                < minimum_worst
            ):
                independence_failures.append(
                    f"WORST_DECILE_THRESHOLD_FAILED:{scope}:{row_id}"
                )
    if independence_missing or independence_reasons:
        gates.append(
            _gate(
                "independence",
                "UNAVAILABLE",
                [
                    *(f"MISSING:{item}" for item in independence_missing),
                    *independence_reasons,
                ],
                {
                    "minimum_observations": minimum_observations,
                    "minimum_downside_observations": minimum_downside,
                },
            )
        )
    else:
        gates.append(
            _gate(
                "independence",
                "FAIL" if independence_failures else "PASS",
                independence_failures,
                {
                    "decision_threshold_authority": independence.get(
                        "decision_threshold_authority"
                    ),
                    "evaluated_series_pairs": len(correlation_rows),
                    "evaluated_cluster_pairs": len(cluster_correlation_rows),
                },
            )
        )

    capacity = _object(
        candidate.get(required["capacity_liquidity"][0], {}), "capacity_liquidity"
    )
    capacity_missing = _missing_fields(capacity, required["capacity_liquidity"][1])
    capacity_failures: list[str] = []
    if not capacity_missing:
        if _number_value(
            capacity.get("modeled_position"), "modeled_position", minimum=0
        ) > _number_value(
            capacity.get("maximum_position"), "maximum_position", minimum=0
        ):
            capacity_failures.append("MODELED_POSITION_EXCEEDS_CAPACITY")
        if _number_value(
            capacity.get("liquidation_days"), "liquidation_days", minimum=0
        ) > _number_value(
            capacity.get("maximum_liquidation_days"),
            "maximum_liquidation_days",
            minimum=0,
        ):
            capacity_failures.append("LIQUIDATION_WINDOW_EXCEEDS_LIMIT")
    gates.append(
        _simple_gate(
            "capacity_liquidity",
            capacity,
            required["capacity_liquidity"][1],
            capacity_failures,
            {
                "modeled_position": capacity.get("modeled_position"),
                "maximum_position": capacity.get("maximum_position"),
                "liquidation_days": capacity.get("liquidation_days"),
                "maximum_liquidation_days": capacity.get("maximum_liquidation_days"),
            },
        )
    )
    if tuple(row["gate_id"] for row in gates) != EXPECTED_GATES:
        raise DiligenceError("rubric output gate order differs")
    counts = {
        status: sum(row["status"] == status for row in gates)
        for status in ("PASS", "FAIL", "UNAVAILABLE")
    }
    return {
        "schema_version": 1,
        "rubric_id": "PORT-RUBRIC-1",
        "as_of_date": _text(candidate.get("as_of_date"), "candidate.as_of_date"),
        "candidate_id": _text(candidate.get("candidate_id"), "candidate.candidate_id"),
        "candidate_name": _text(
            candidate.get("candidate_name"), "candidate.candidate_name"
        ),
        "source": {
            "dependence_sha256": dependence_sha256,
            "candidate_evidence_sha256": candidate_sha256,
            "rubric_contract_sha256": contract_sha256,
            "economic_driver_cluster_contract_sha256": cluster_contract_sha256,
        },
        "thresholds": {
            "minimum_observations": minimum_observations,
            "minimum_downside_observations": minimum_downside,
            "authority": "SOURCE_DEPENDENCE_POLICY",
        },
        "authority_boundary": dict(
            _object(contract.get("decision_boundary"), "decision boundary")
        ),
        "gate_counts": counts,
        "gates": gates,
        "scenario": scenario,
        "replacement_comparison": _replacement_comparison(candidate),
        "leave_one_out": _leave_one_out(dependence),
    }


def build(dependence_path: Path, candidate_path: Path, output: Path) -> dict[str, Any]:
    contract = _load_json(RUBRIC_CONTRACT, "rubric contract")
    payload = build_payload(
        _load_json(dependence_path, "dependence artifact"),
        _load_json(candidate_path, "candidate evidence"),
        dependence_sha256=_hash(dependence_path),
        candidate_sha256=_hash(candidate_path),
        contract=contract,
        contract_sha256=_hash(RUBRIC_CONTRACT),
        cluster_contract=_load_json(
            ECONOMIC_DRIVER_CLUSTERS, "economic driver cluster contract"
        ),
        cluster_contract_sha256=_hash(ECONOMIC_DRIVER_CLUSTERS),
    )
    content = canonical_json_bytes(payload)
    if output.exists() and output.read_bytes() != content:
        raise DiligenceError("output exists with different content")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return payload


def verify(
    dependence_path: Path, candidate_path: Path, artifact_path: Path
) -> dict[str, Any]:
    expected = build_payload(
        _load_json(dependence_path, "dependence artifact"),
        _load_json(candidate_path, "candidate evidence"),
        dependence_sha256=_hash(dependence_path),
        candidate_sha256=_hash(candidate_path),
        contract=_load_json(RUBRIC_CONTRACT, "rubric contract"),
        contract_sha256=_hash(RUBRIC_CONTRACT),
        cluster_contract=_load_json(
            ECONOMIC_DRIVER_CLUSTERS, "economic driver cluster contract"
        ),
        cluster_contract_sha256=_hash(ECONOMIC_DRIVER_CLUSTERS),
    )
    actual = _load_json(artifact_path, "rubric artifact")
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise DiligenceError("rubric artifact differs from deterministic rebuild")
    return {
        "ok": True,
        "artifact_sha256": _hash(artifact_path),
        "candidate_id": actual.get("candidate_id"),
        "gate_counts": actual.get("gate_counts"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "verify"):
        selected = subparsers.add_parser(command)
        selected.add_argument("--dependence", type=Path, required=True)
        selected.add_argument("--candidate-evidence", type=Path, required=True)
        if command == "build":
            selected.add_argument("--output", type=Path, required=True)
        else:
            selected.add_argument("--artifact", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build(args.dependence, args.candidate_evidence, args.output)
        else:
            result = verify(args.dependence, args.candidate_evidence, args.artifact)
    except DiligenceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
