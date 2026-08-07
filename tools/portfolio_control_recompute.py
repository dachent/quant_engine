"""Independently recompute and reconcile the headline correlation-one control."""

from __future__ import annotations

import argparse
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


def _panel_payload(candidate: Path) -> tuple[dict[str, Any], dict[str, object]]:
    accepted = candidate.resolve()
    manifest_path = accepted / "candidate.json"
    manifest = _load(manifest_path, "panel candidate manifest")
    dashboard_path = accepted / "data" / "dashboard_data.json"
    dashboard = _load(dashboard_path, "panel dashboard data")
    dashboard_hash, dashboard_size = hash_file(dashboard_path)
    artifact_hashes = _object(
        manifest.get("artifact_sha256"), "candidate artifact_sha256"
    )
    if artifact_hashes.get("data/dashboard_data.json") != dashboard_hash:
        raise ControlRecomputeError(
            "panel dashboard hash differs from candidate manifest"
        )
    panel_id = _text(manifest.get("panel_id"), "panel_id")
    if dashboard.get("meta", {}).get("report_generation_id") != manifest.get(
        "report_generation_id"
    ):
        raise ControlRecomputeError(
            "panel report identity differs from candidate manifest"
        )
    return dashboard, {
        "panel_id": panel_id,
        "path": str(dashboard_path),
        "sha256": dashboard_hash,
        "size_bytes": dashboard_size,
    }


def build_receipt(
    correlation_root: Path,
    report_root: Path,
    panel_candidate: Path,
    evaluation_date: date,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> dict[str, object]:
    """Recompute the stress from primitive weights and adverse losses."""

    if tolerance <= 0:
        raise ControlRecomputeError("tolerance must be positive")
    correlation, correlation_identity = _current_payload(
        correlation_root, "correlation.json", "correlation analytics"
    )
    report, report_identity = _current_payload(
        report_root, "portfolio-report-data.json", "portfolio report"
    )
    panel, panel_identity = _panel_payload(panel_candidate)

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
        adverse_loss = _decimal(
            component.get("adverse_loss_fraction"), f"component {index} adverse loss"
        )
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
        "panel_freshness_matches_evaluation_date": panel_meta.get(
            "freshness_evaluated_on"
        )
        == evaluation_date.isoformat(),
        "panel_report_matches_current": panel_meta.get("report_generation_id")
        == report_identity["generation_id"],
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
            "panel": panel_identity,
            "report": report_identity,
        },
        "method": "DECIMAL_SUM_ALLOCATION_WEIGHT_TIMES_ADVERSE_LOSS_FRACTION",
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
