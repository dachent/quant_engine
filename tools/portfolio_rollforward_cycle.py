"""Record one bounded manual portfolio rollforward monitoring cycle."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from quant_engine.core import hash_file, read_current, read_generation
from quant_engine.core.contracts import canonical_json_bytes


SCHEMA_VERSION = 1


class RollforwardCycleError(ValueError):
    """A monitoring-cycle input or declared boundary differs."""


def _load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RollforwardCycleError(f"{label} is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RollforwardCycleError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise RollforwardCycleError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RollforwardCycleError(f"{label} must be non-empty text")
    return value.strip()


def _objects(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise RollforwardCycleError(f"{label} must contain objects")
    return list(value)


def _identity(path: Path) -> dict[str, object]:
    digest, size = hash_file(path)
    return {"path": str(path.resolve()), "sha256": digest, "size_bytes": size}


def _strict_object(
    value: object, label: str, expected_keys: set[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise RollforwardCycleError(f"{label} fields differ")
    return value


def _hash_text(value: object, label: str) -> str:
    accepted = _text(value, label)
    if len(accepted) != 64 or any(
        character not in "0123456789abcdef" for character in accepted
    ):
        raise RollforwardCycleError(f"{label} must be a lowercase SHA-256")
    return accepted


def _positive_size(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RollforwardCycleError(f"{label} must be a positive integer")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise RollforwardCycleError(f"{label} must be decimal text")
    try:
        accepted = Decimal(value)
    except InvalidOperation as exc:
        raise RollforwardCycleError(f"{label} must be decimal text") from exc
    if not accepted.is_finite():
        raise RollforwardCycleError(f"{label} must be finite")
    return accepted


def _validate_input_identity(value: object, label: str, *, panel: bool = False) -> None:
    identity_key = "panel_id" if panel else "generation_id"
    expected = {"path", "sha256", "size_bytes", identity_key}
    if not panel:
        expected.add("as_of_date")
    identity = _strict_object(value, label, expected)
    _hash_text(identity.get(identity_key), f"{label}.{identity_key}")
    _hash_text(identity.get("sha256"), f"{label}.sha256")
    expected_size = _positive_size(identity.get("size_bytes"), f"{label}.size_bytes")
    path = Path(_text(identity.get("path"), f"{label}.path"))
    if path.is_symlink() or not path.is_file():
        raise RollforwardCycleError(f"{label} input is missing or linked")
    observed_hash, observed_size = hash_file(path)
    if (observed_hash, observed_size) != (identity["sha256"], expected_size):
        raise RollforwardCycleError(f"{label} input identity differs")
    if not panel:
        try:
            date.fromisoformat(_text(identity.get("as_of_date"), f"{label}.as_of_date"))
        except ValueError as exc:
            raise RollforwardCycleError(f"{label}.as_of_date must be a date") from exc


def _validate_recomputation(
    recomputation: Mapping[str, Any], evaluation_date: date
) -> None:
    expected_top_level = {
        "as_of_date",
        "component_count",
        "declared_values",
        "differences",
        "generation_checks",
        "inputs",
        "method",
        "recomputed_correlation_one_stress",
        "receipt_schema_version",
        "status",
        "tolerance",
    }
    if set(recomputation) != expected_top_level:
        raise RollforwardCycleError("control recomputation fields differ")
    if recomputation.get("receipt_schema_version") != 1:
        raise RollforwardCycleError("control recomputation schema differs")
    if recomputation.get("status") != "PASS":
        raise RollforwardCycleError("control recomputation did not pass")
    if recomputation.get("as_of_date") != evaluation_date.isoformat():
        raise RollforwardCycleError("control recomputation date differs")
    if recomputation.get("method") != (
        "DECIMAL_SUM_ALLOCATION_WEIGHT_TIMES_ADVERSE_LOSS_FRACTION"
    ):
        raise RollforwardCycleError("control recomputation method differs")
    component_count = recomputation.get("component_count")
    if (
        isinstance(component_count, bool)
        or not isinstance(component_count, int)
        or component_count < 1
    ):
        raise RollforwardCycleError("control recomputation component count differs")

    declared = _strict_object(
        recomputation.get("declared_values"),
        "control recomputation declared values",
        {"correlation_stress", "panel_headline", "report_headline"},
    )
    for key, value in declared.items():
        _decimal(value, f"control recomputation declared_values.{key}")
    differences = _strict_object(
        recomputation.get("differences"),
        "control recomputation differences",
        {
            "correlation_declared",
            "declared_contribution_sum",
            "maximum_component",
            "panel_headline",
            "report_headline",
        },
    )
    tolerance = _decimal(
        recomputation.get("tolerance"), "control recomputation tolerance"
    )
    if tolerance != Decimal("0.000000000001"):
        raise RollforwardCycleError("control recomputation tolerance differs")
    if any(
        value < 0 or value > tolerance
        for value in (
            _decimal(raw, f"control recomputation differences.{key}")
            for key, raw in differences.items()
        )
    ):
        raise RollforwardCycleError(
            "control recomputation differences exceed tolerance"
        )
    _decimal(
        recomputation.get("recomputed_correlation_one_stress"),
        "control recomputation headline",
    )

    checks = _strict_object(
        recomputation.get("generation_checks"),
        "control recomputation generation checks",
        {
            "panel_correlation_matches_current",
            "panel_freshness_matches_evaluation_date",
            "panel_report_matches_current",
        },
    )
    if not all(value is True for value in checks.values()):
        raise RollforwardCycleError("control recomputation generation check failed")
    inputs = _strict_object(
        recomputation.get("inputs"),
        "control recomputation inputs",
        {"correlation", "panel", "report"},
    )
    _validate_input_identity(inputs["correlation"], "correlation")
    _validate_input_identity(inputs["panel"], "panel", panel=True)
    _validate_input_identity(inputs["report"], "report")


def _request_identity(row: Mapping[str, Any], evaluation_date: date) -> str:
    identity = {
        "evaluation_date": evaluation_date.isoformat(),
        "fact_type": row.get("fact_type"),
        "lane": row.get("lane"),
        "provider_id": row.get("provider_id"),
        "subject_id": row.get("subject_id"),
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def build_receipt(
    provider_ledger_root: Path,
    provider_ledger_generation_id: str,
    freshness_path: Path,
    recomputation_path: Path,
    evaluation_date: date,
    previous_cycle_date: date,
) -> dict[str, object]:
    """Build a deterministic no-provider-call cycle receipt."""

    if previous_cycle_date >= evaluation_date:
        raise RollforwardCycleError("previous cycle must precede this cycle")
    pointer = read_current(provider_ledger_root.resolve())
    if pointer.generation_id != provider_ledger_generation_id:
        raise RollforwardCycleError("provider ledger generation is not current")
    manifest = read_generation(provider_ledger_root.resolve(), pointer.generation_id)

    freshness = _load(freshness_path.resolve(), "freshness health")
    if freshness.get("assessment_schema_version") != 3:
        raise RollforwardCycleError("freshness health schema must be 3")
    if freshness.get("as_of_date") != evaluation_date.isoformat():
        raise RollforwardCycleError("freshness evaluation date differs")
    ledger_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/provider-ledger.json"
        ),
        None,
    )
    if ledger_artifact is None:
        raise RollforwardCycleError("provider ledger catalog artifact is absent")
    freshness_ledger = _strict_object(
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
    if (
        freshness_ledger.get("as_of_date") != manifest.as_of_date.isoformat()
        or freshness_ledger.get("generation_id") != pointer.generation_id
        or freshness_ledger.get("manifest_sha256") != pointer.manifest_sha256
        or freshness_ledger.get("catalog_sha256") != ledger_artifact.sha256
        or freshness_ledger.get("catalog_size_bytes") != ledger_artifact.size_bytes
    ):
        raise RollforwardCycleError("freshness provider ledger identity differs")
    manifest_path = (
        provider_ledger_root.resolve()
        / "generations"
        / pointer.generation_id
        / "generation.json"
    )
    _, manifest_size = hash_file(manifest_path)
    if freshness_ledger.get("manifest_size_bytes") != manifest_size:
        raise RollforwardCycleError("freshness provider ledger manifest size differs")
    assessments = _objects(freshness.get("assessments"), "freshness assessments")
    for row in assessments:
        if row.get("evaluated_on") != evaluation_date.isoformat():
            raise RollforwardCycleError("freshness assessment date differs")
        if not isinstance(row.get("required_for_current"), bool) or not isinstance(
            row.get("escalation_required"), bool
        ):
            raise RollforwardCycleError("freshness requirement fields must be boolean")

    escalations = [
        row
        for row in assessments
        if row.get("required_for_current") is True
        and row.get("escalation_required") is True
    ]
    read_plan = []
    for row in escalations:
        provider_id = _text(row.get("provider_id"), "provider_id")
        read_plan.append(
            {
                "acquisition_path": (
                    "HAR_FIRST_API_ONLY_FOR_DECLARED_HAR_GAP_WITH_APPROVAL"
                    if provider_id == "portfolio123"
                    else "DECLARED_PROVIDER_READ_REQUIRES_EXPLICIT_APPROVAL"
                ),
                "effect": "READ_ONLY_EXTERNAL",
                "fact_type": _text(row.get("fact_type"), "fact_type"),
                "lane": _text(row.get("lane"), "lane"),
                "provider_id": provider_id,
                "request_identity": _request_identity(row, evaluation_date),
                "retry_limit": 0,
                "status": "NOT_AUTHORIZED_NOT_EXECUTED",
                "subject_id": _text(row.get("subject_id"), "subject_id"),
            }
        )
    read_plan.sort(
        key=lambda item: (
            str(item["provider_id"]),
            str(item["subject_id"]),
            str(item["lane"]),
            str(item["fact_type"]),
        )
    )

    recomputation = _load(recomputation_path.resolve(), "control recomputation")
    _validate_recomputation(recomputation, evaluation_date)

    states = Counter(_text(row.get("state"), "freshness state") for row in assessments)
    providers = Counter(str(row["provider_id"]) for row in escalations)
    identity = {
        "evaluation_date": evaluation_date.isoformat(),
        "freshness_sha256": _identity(freshness_path)["sha256"],
        "previous_cycle_date": previous_cycle_date.isoformat(),
        "provider_ledger_generation_id": provider_ledger_generation_id,
        "recomputation_sha256": _identity(recomputation_path)["sha256"],
    }
    cycle_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return {
        "attempt_policy": {
            "external_read_retry_limit": 0,
            "local_retry_limit": 2,
            "unknown_external_outcome_retry": "PROHIBITED",
        },
        "cycle_id": cycle_id,
        "cycle_schema_version": SCHEMA_VERSION,
        "dated_cycle_evidence": {
            "current_cycle_date": evaluation_date.isoformat(),
            "previous_cycle_date": previous_cycle_date.isoformat(),
            "status": "SECOND_DATED_MANUAL_CYCLE_RECORDED",
        },
        "evaluation_date": evaluation_date.isoformat(),
        "freshness": {
            **_identity(freshness_path),
            "assessment_count": len(assessments),
            "escalation_count": len(escalations),
            "provider_escalation_counts": dict(sorted(providers.items())),
            "state_counts": dict(sorted(states.items())),
        },
        "notification_receipt": {
            "channel": "LOCAL_CONTROL_PANEL_OPERATOR_QUEUE",
            "external_notification_sent": False,
            "recorded_escalation_count": len(escalations),
            "status": "RECORDED_FOR_NEXT_PANEL_GENERATION",
        },
        "pointer_effects": {
            "advanced": False,
            "reason": "AUTHORIZED_PROVIDER_REFRESH_AND_PREREQUISITE_RELEASE_NOT_COMPLETE",
        },
        "provider_ledger": {
            "as_of_date": manifest.as_of_date.isoformat(),
            "generation_id": pointer.generation_id,
            "manifest_sha256": pointer.manifest_sha256,
        },
        "provider_read_plan": read_plan,
        "recomputation": _identity(recomputation_path),
        "status": "PASS_WITH_RETAINED_ESCALATIONS",
    }


def write_receipt(path: Path, receipt: Mapping[str, object]) -> dict[str, object]:
    content = canonical_json_bytes(dict(receipt))
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise RollforwardCycleError("create-only cycle receipt already differs")
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
    return {"output": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-ledger-root", type=Path, required=True)
    parser.add_argument("--provider-ledger-generation-id", required=True)
    parser.add_argument("--freshness", type=Path, required=True)
    parser.add_argument("--recomputation", type=Path, required=True)
    parser.add_argument("--evaluation-date", type=date.fromisoformat, required=True)
    parser.add_argument("--previous-cycle-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = build_receipt(
            args.provider_ledger_root,
            args.provider_ledger_generation_id,
            args.freshness,
            args.recomputation,
            args.evaluation_date,
            args.previous_cycle_date,
        )
        result = write_receipt(args.output, receipt)
    except (OSError, RollforwardCycleError, RuntimeError, ValueError) as exc:
        print(f"portfolio-rollforward-cycle: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**result, "status": receipt["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
