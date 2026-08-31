"""Independently recompute and reconcile the headline correlation-one control."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from quant_engine.core import hash_file, read_current, read_generation
from quant_engine.core.authority import FreshnessPolicy, ManifestValidationError
from quant_engine.core.contracts import canonical_json_bytes


SCHEMA_VERSION = 2
DEFAULT_TOLERANCE = Decimal("0.000000000001")


class ControlRecomputeError(ValueError):
    """A control input or independently recomputed result differs."""


def _load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControlRecomputeError(f"{label} is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlRecomputeError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise ControlRecomputeError(f"{label} must be an object")
    return value


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlRecomputeError(f"{label} must be an object")
    return value


def _objects(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise ControlRecomputeError(f"{label} must contain objects")
    return list(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlRecomputeError(f"{label} must be non-empty text")
    return value.strip()


def _hash_text(value: object, label: str) -> str:
    accepted = _text(value, label)
    if not re.fullmatch(r"[0-9a-f]{64}", accepted):
        raise ControlRecomputeError(f"{label} must be a lowercase SHA-256")
    return accepted


def _positive_size(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ControlRecomputeError(f"{label} must be a positive integer")
    return value


def _strict_object(
    value: object, label: str, expected_keys: set[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ControlRecomputeError(f"{label} fields differ")
    return value


def _file_identity(path: Path) -> dict[str, object]:
    digest, size = hash_file(path)
    return {"path": str(path.resolve()), "sha256": digest, "size_bytes": size}


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ControlRecomputeError(f"{label} must be numeric")
    try:
        accepted = Decimal(str(value))
    except InvalidOperation as exc:
        raise ControlRecomputeError(f"{label} must be numeric") from exc
    if not accepted.is_finite():
        raise ControlRecomputeError(f"{label} must be finite")
    return accepted


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f").rstrip("0").rstrip(".")


def _current_payload(
    root: Path, filename: str, label: str
) -> tuple[dict[str, Any], dict[str, object]]:
    accepted_root = root.resolve()
    pointer = read_current(accepted_root)
    manifest = read_generation(accepted_root, pointer.generation_id)
    path = accepted_root / "generations" / pointer.generation_id / filename
    payload = _load(path, label)
    sha256, size_bytes = hash_file(path)
    return payload, {
        "as_of_date": manifest.as_of_date.isoformat(),
        "generation_id": pointer.generation_id,
        "path": str(path),
        "sha256": sha256,
        "size_bytes": size_bytes,
    }


def _provider_ledger_identity(
    root: Path, generation_id: str
) -> dict[str, object]:
    accepted_root = root.resolve()
    try:
        pointer = read_current(accepted_root)
        manifest = read_generation(accepted_root, pointer.generation_id)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ControlRecomputeError("current provider ledger files differ") from exc
    if pointer.generation_id != generation_id:
        raise ControlRecomputeError("provider ledger generation is not current")
    catalog_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/provider-ledger.json"
        ),
        None,
    )
    if catalog_artifact is None:
        raise ControlRecomputeError("provider ledger catalog artifact is absent")
    generation_root = accepted_root / "generations" / generation_id
    catalog_path = generation_root / "provider-ledger.json"
    manifest_path = generation_root / "generation.json"
    catalog = _file_identity(catalog_path)
    manifest_identity = _file_identity(manifest_path)
    if (
        catalog["sha256"] != catalog_artifact.sha256
        or catalog["size_bytes"] != catalog_artifact.size_bytes
        or manifest_identity["sha256"] != pointer.manifest_sha256
    ):
        raise ControlRecomputeError("current provider ledger files differ")
    return {
        "as_of_date": manifest.as_of_date.isoformat(),
        "catalog": catalog,
        "generation_id": generation_id,
        "manifest": manifest_identity,
    }


def _validate_freshness(
    freshness_path: Path,
    evaluation_date: date,
    provider_ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, object]]:
    freshness = _load(freshness_path.resolve(), "freshness health")
    expected_bundle_keys = {
        "assessment_schema_version",
        "as_of_date",
        "assessments",
        "policies",
        "provider_ledger",
    }
    if set(freshness) != expected_bundle_keys:
        raise ControlRecomputeError("freshness health fields differ")
    if freshness.get("assessment_schema_version") != 3:
        raise ControlRecomputeError("freshness health schema must be 3")
    if freshness.get("as_of_date") != evaluation_date.isoformat():
        raise ControlRecomputeError("freshness evaluation date differs")
    ledger = _strict_object(
        freshness.get("provider_ledger"),
        "freshness provider ledger",
        {
            "as_of_date",
            "catalog_sha256",
            "catalog_size_bytes",
            "generation_id",
            "manifest_sha256",
            "manifest_size_bytes",
        },
    )
    catalog = _strict_object(
        provider_ledger.get("catalog"), "selected provider ledger catalog", {"path", "sha256", "size_bytes"}
    )
    manifest = _strict_object(
        provider_ledger.get("manifest"), "selected provider ledger manifest", {"path", "sha256", "size_bytes"}
    )
    if (
        ledger.get("as_of_date") != provider_ledger.get("as_of_date")
        or ledger.get("generation_id") != provider_ledger.get("generation_id")
        or ledger.get("catalog_sha256") != catalog.get("sha256")
        or ledger.get("catalog_size_bytes") != catalog.get("size_bytes")
        or ledger.get("manifest_sha256") != manifest.get("sha256")
        or ledger.get("manifest_size_bytes") != manifest.get("size_bytes")
    ):
        raise ControlRecomputeError("freshness provider ledger identity differs")
    policies: dict[str, FreshnessPolicy] = {}
    for raw_policy in _objects(freshness.get("policies"), "freshness policies"):
        try:
            policy = FreshnessPolicy.from_dict(raw_policy)
        except (ManifestValidationError, ValueError, TypeError) as exc:
            raise ControlRecomputeError("freshness policy differs") from exc
        if policy.policy_id in policies:
            raise ControlRecomputeError("freshness policy identity is duplicated")
        policies[policy.policy_id] = policy
    if not policies:
        raise ControlRecomputeError("freshness policies are unavailable")
    assessment_keys = {
        "acquired_at",
        "blocks_current_consumers",
        "coverage_status",
        "escalation_required",
        "evaluated_on",
        "fact_type",
        "lane",
        "last_accepted_date",
        "next_expected_date",
        "policy_id",
        "provider_id",
        "reason",
        "required_for_current",
        "revision_state",
        "state",
        "subject_id",
        "subject_type",
    }
    for index, assessment in enumerate(
        _objects(freshness.get("assessments"), "freshness assessments")
    ):
        if set(assessment) != assessment_keys:
            raise ControlRecomputeError("freshness assessment fields differ")
        if assessment.get("evaluated_on") != evaluation_date.isoformat():
            raise ControlRecomputeError("freshness assessment date differs")
        for field in (
            "blocks_current_consumers",
            "escalation_required",
            "required_for_current",
        ):
            if not isinstance(assessment.get(field), bool):
                raise ControlRecomputeError(f"freshness assessment {field} differs")
        for field in (
            "coverage_status",
            "fact_type",
            "lane",
            "policy_id",
            "provider_id",
            "reason",
            "revision_state",
            "state",
            "subject_id",
            "subject_type",
        ):
            _text(assessment.get(field), f"freshness assessment {index} {field}")
        if assessment.get("policy_id") not in policies:
            raise ControlRecomputeError("freshness assessment policy differs")
        for field in ("last_accepted_date", "next_expected_date"):
            value = assessment.get(field)
            if value is not None:
                try:
                    date.fromisoformat(_text(value, f"freshness assessment {field}"))
                except ValueError as exc:
                    raise ControlRecomputeError(
                        f"freshness assessment {field} differs"
                    ) from exc
        acquired_at = assessment.get("acquired_at")
        if acquired_at is not None and not isinstance(acquired_at, str):
            raise ControlRecomputeError("freshness assessment acquired_at differs")
    return freshness, {
        **_file_identity(freshness_path),
        "as_of_date": evaluation_date.isoformat(),
    }


def _panel_payload(
    candidate: Path, selected_freshness: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, object]]:
    accepted = candidate.resolve()
    manifest_path = accepted / "candidate.json"
    manifest = _load(manifest_path, "panel candidate manifest")
    dashboard_path = accepted / "data" / "dashboard_data.json"
    dashboard = _load(dashboard_path, "panel dashboard data")
    dashboard_hash, dashboard_size = hash_file(dashboard_path)
    artifact_hashes = _object(
        manifest.get("artifact_sha256"), "candidate artifact_sha256"
    )
    if not artifact_hashes:
        raise ControlRecomputeError("candidate artifact_sha256 is unavailable")
    normalized_hashes: dict[str, str] = {}
    for relative, declared_hash in artifact_hashes.items():
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ControlRecomputeError("candidate artifact path differs")
        normalized_hashes[relative] = _hash_text(
            declared_hash, f"candidate artifact {relative} sha256"
        )
        artifact_path = accepted / relative
        observed = _file_identity(artifact_path)
        if observed["sha256"] != normalized_hashes[relative]:
            if relative == "data/freshness-health.json":
                raise ControlRecomputeError("candidate freshness hash differs")
            raise ControlRecomputeError("candidate artifact hash differs")
    expected_panel_id = hashlib.sha256(
        canonical_json_bytes(dict(sorted(normalized_hashes.items())))
    ).hexdigest()
    if artifact_hashes.get("data/dashboard_data.json") != dashboard_hash:
        raise ControlRecomputeError(
            "panel dashboard hash differs from candidate manifest"
        )
    freshness_path = accepted / "data" / "freshness-health.json"
    candidate_freshness = _file_identity(freshness_path)
    declared_freshness = normalized_hashes.get("data/freshness-health.json")
    if declared_freshness is None or declared_freshness != candidate_freshness["sha256"]:
        raise ControlRecomputeError("candidate freshness hash differs")
    if (
        candidate_freshness["sha256"] != selected_freshness.get("sha256")
        or candidate_freshness["size_bytes"] != selected_freshness.get("size_bytes")
    ):
        raise ControlRecomputeError("panel freshness identity differs")
    panel_id = _text(manifest.get("panel_id"), "panel_id")
    if panel_id != expected_panel_id:
        raise ControlRecomputeError("candidate panel_id differs")
    if accepted.name != expected_panel_id:
        raise ControlRecomputeError("candidate directory differs from panel_id")
    if dashboard.get("meta", {}).get("report_generation_id") != manifest.get(
        "report_generation_id"
    ):
        raise ControlRecomputeError(
            "panel report identity differs from candidate manifest"
        )
    return dashboard, {
        "candidate_dir": str(accepted),
        "candidate_record": _file_identity(manifest_path),
        "dashboard": {
            "path": str(dashboard_path),
            "sha256": dashboard_hash,
            "size_bytes": dashboard_size,
        },
        "declared_freshness_sha256": declared_freshness,
        "freshness": candidate_freshness,
        "panel_id": panel_id,
    }


def build_receipt(
    correlation_root: Path,
    report_root: Path,
    panel_candidate: Path,
    freshness_path: Path,
    provider_ledger_root: Path,
    provider_ledger_generation_id: str,
    evaluation_date: date,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> dict[str, object]:
    """Recompute the bound from primitive weights and conservative losses."""

    if tolerance <= 0:
        raise ControlRecomputeError("tolerance must be positive")
    correlation, correlation_identity = _current_payload(
        correlation_root, "correlation.json", "correlation analytics"
    )
    report, report_identity = _current_payload(
        report_root, "portfolio-report-data.json", "portfolio report"
    )
    provider_ledger_identity = _provider_ledger_identity(
        provider_ledger_root, provider_ledger_generation_id
    )
    _freshness, freshness_identity = _validate_freshness(
        freshness_path, evaluation_date, provider_ledger_identity
    )
    panel, panel_identity = _panel_payload(panel_candidate, freshness_identity)

    dependence = _object(correlation.get("dependence"), "correlation dependence")
    correlation_one = _object(
        dependence.get("correlation_one"), "correlation-one stress"
    )
    components = _objects(correlation_one.get("components"), "stress components")
    if not components:
        raise ControlRecomputeError("stress components are unavailable")
    recomputed = Decimal(0)
    declared_contribution_sum = Decimal(0)
    maximum_component_difference = Decimal(0)
    for index, component in enumerate(components):
        weight = _decimal(
            component.get("allocation_weight"), f"component {index} weight"
        )
        loss_fraction = component.get("conservative_loss_fraction")
        if loss_fraction is None:
            loss_fraction = component.get("adverse_loss_fraction")
        adverse_loss = _decimal(loss_fraction, f"component {index} conservative loss")
        declared_contribution = _decimal(
            component.get("loss_contribution"), f"component {index} contribution"
        )
        independently_computed = weight * adverse_loss
        recomputed += independently_computed
        declared_contribution_sum += declared_contribution
        maximum_component_difference = max(
            maximum_component_difference,
            abs(independently_computed - declared_contribution),
        )

    declared_correlation = _decimal(correlation_one.get("loss"), "correlation loss")
    summary = _objects(report.get("portfolio_summary"), "portfolio summary")
    headline_rows = [
        row for row in summary if row.get("metric") == "Correlation-one stress"
    ]
    if len(headline_rows) != 1:
        raise ControlRecomputeError("report correlation-one headline is not unique")
    report_headline = _decimal(headline_rows[0].get("value"), "report headline")
    advisory = _object(panel.get("advisory"), "panel advisory")
    panel_headline = _decimal(advisory.get("correlation_one_stress"), "panel headline")
    panel_meta = _object(panel.get("meta"), "panel meta")
    generation_checks = {
        "panel_correlation_matches_current": panel_meta.get("correlation_generation_id")
        == correlation_identity["generation_id"],
        "panel_freshness_matches_selected": (
            panel_meta.get("freshness_evaluated_on") == evaluation_date.isoformat()
            and panel_identity["freshness"] == {
                "path": panel_identity["freshness"]["path"],
                "sha256": freshness_identity["sha256"],
                "size_bytes": freshness_identity["size_bytes"],
            }
        ),
        "panel_report_matches_current": panel_meta.get("report_generation_id")
        == report_identity["generation_id"],
        "freshness_matches_current_provider_ledger": True,
    }
    differences = {
        "correlation_declared": abs(recomputed - declared_correlation),
        "declared_contribution_sum": abs(recomputed - declared_contribution_sum),
        "maximum_component": maximum_component_difference,
        "panel_headline": abs(recomputed - panel_headline),
        "report_headline": abs(recomputed - report_headline),
    }
    status = (
        "PASS"
        if all(generation_checks.values())
        and all(value <= tolerance for value in differences.values())
        else "FAIL"
    )
    receipt = {
        "as_of_date": evaluation_date.isoformat(),
        "component_count": len(components),
        "declared_values": {
            "correlation_stress": _decimal_text(declared_correlation),
            "panel_headline": _decimal_text(panel_headline),
            "report_headline": _decimal_text(report_headline),
        },
        "differences": {
            key: _decimal_text(value) for key, value in sorted(differences.items())
        },
        "generation_checks": generation_checks,
        "inputs": {
            "correlation": correlation_identity,
            "freshness": freshness_identity,
            "panel": panel_identity,
            "provider_ledger": provider_ledger_identity,
            "report": report_identity,
        },
        "method": "DECIMAL_SUM_ALLOCATION_WEIGHT_TIMES_CONSERVATIVE_LOSS_FRACTION",
        "recomputed_correlation_one_stress": _decimal_text(recomputed),
        "receipt_schema_version": SCHEMA_VERSION,
        "status": status,
        "tolerance": _decimal_text(tolerance),
    }
    if status != "PASS":
        raise ControlRecomputeError("independent headline recomputation differs")
    return receipt


def write_receipt(path: Path, receipt: Mapping[str, object]) -> dict[str, object]:
    content = canonical_json_bytes(dict(receipt))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ControlRecomputeError(
                "create-only receipt already exists and differs"
            )
    else:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "output": str(path.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--correlation-root", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--panel-candidate", type=Path, required=True)
    parser.add_argument("--freshness", type=Path, required=True)
    parser.add_argument("--provider-ledger-root", type=Path, required=True)
    parser.add_argument("--provider-ledger-generation-id", required=True)
    parser.add_argument("--evaluation-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = build_receipt(
            args.correlation_root,
            args.report_root,
            args.panel_candidate,
            args.freshness,
            args.provider_ledger_root,
            args.provider_ledger_generation_id,
            args.evaluation_date,
        )
        result = write_receipt(args.output.resolve(), receipt)
    except (ControlRecomputeError, OSError, ValueError) as exc:
        print(f"control recomputation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**result, "status": receipt["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
