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
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_recompute.py"
SPEC = importlib.util.spec_from_file_location("portfolio_control_recompute", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load control recomputation tool")
recompute = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recompute
SPEC.loader.exec_module(recompute)

AS_OF = date(2026, 8, 6)


def _publish(root: Path, filename: str, payload: dict[str, object], name: str) -> str:
    source = canonical_json_bytes({"source": name})
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity(name, "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic independent control recomputation",
            fact_types=("correlation-one-stress",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"input/{name}.json",
                sha256=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source),
                as_of_date=AS_OF,
                schema_name="synthetic-input",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / filename).write_bytes(canonical_json_bytes(payload))
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    correlation_root = tmp_path / "correlation"
    correlation_generation = _publish(
        correlation_root,
        "correlation.json",
        {
            "dependence": {
                "correlation_one": {
                    "components": [
                        {
                            "adverse_loss_fraction": "0.25",
                            "allocation_weight": "0.4",
                            "loss_contribution": "0.1",
                            "series_id": "one",
                        },
                        {
                            "adverse_loss_fraction": "0.5",
                            "allocation_weight": "0.4",
                            "loss_contribution": "0.2",
                            "series_id": "two",
                        },
                    ],
                    "loss": "0.3",
                }
            }
        },
        "synthetic-correlation",
    )
    report_root = tmp_path / "report"
    report_generation = _publish(
        report_root,
        "portfolio-report-data.json",
        {
            "portfolio_summary": [
                {"metric": "Correlation-one stress", "value": "0.3"}
            ]
        },
        "synthetic-report",
    )
    candidate = tmp_path / "candidate"
    dashboard = {
        "advisory": {"correlation_one_stress": "0.3"},
        "meta": {
            "correlation_generation_id": correlation_generation,
            "report_generation_id": report_generation,
        },
    }
    dashboard_bytes = canonical_json_bytes(dashboard)
    (candidate / "data").mkdir(parents=True)
    (candidate / "data" / "dashboard_data.json").write_bytes(dashboard_bytes)
    (candidate / "candidate.json").write_bytes(
        canonical_json_bytes(
            {
                "artifact_sha256": {
                    "data/dashboard_data.json": hashlib.sha256(
                        dashboard_bytes
                    ).hexdigest()
                },
                "panel_id": "a" * 64,
                "report_generation_id": report_generation,
            }
        )
    )
    return correlation_root, report_root, candidate


def test_independently_recomputes_and_reconciles_all_three_surfaces(
    tmp_path: Path,
) -> None:
    correlation_root, report_root, candidate = _inputs(tmp_path)

    receipt = recompute.build_receipt(
        correlation_root, report_root, candidate, AS_OF
    )

    assert receipt["status"] == "PASS"
    assert receipt["component_count"] == 2
    assert receipt["recomputed_correlation_one_stress"] == "0.3"
    assert set(receipt["differences"].values()) == {"0"}
    assert all(receipt["generation_checks"].values())


def test_mismatched_panel_headline_fails_closed(tmp_path: Path) -> None:
    correlation_root, report_root, candidate = _inputs(tmp_path)
    dashboard_path = candidate / "data" / "dashboard_data.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    dashboard["advisory"]["correlation_one_stress"] = "0.2"
    content = canonical_json_bytes(dashboard)
    dashboard_path.write_bytes(content)
    manifest_path = candidate / "candidate.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"]["data/dashboard_data.json"] = hashlib.sha256(
        content
    ).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(
        recompute.ControlRecomputeError,
        match="independent headline recomputation differs",
    ):
        recompute.build_receipt(correlation_root, report_root, candidate, AS_OF)


def test_receipt_is_create_only(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    receipt = {"status": "PASS"}

    first = recompute.write_receipt(output, receipt)
    second = recompute.write_receipt(output, receipt)

    assert first == second
    output.write_text("different", encoding="utf-8")
    with pytest.raises(recompute.ControlRecomputeError, match="exists and differs"):
        recompute.write_receipt(output, receipt)
