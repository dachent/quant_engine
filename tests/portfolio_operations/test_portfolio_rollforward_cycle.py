from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
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
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_rollforward_cycle.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_rollforward_cycle", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load rollforward cycle tool")
cycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cycle
SPEC.loader.exec_module(cycle)

CURRENT = date(2026, 8, 6)
PREVIOUS = date(2026, 8, 3)


def _ledger(root: Path) -> tuple[str, dict[str, object]]:
    source = b"synthetic-ledger\n"
    manifest = GenerationManifest.build(
        as_of_date=PREVIOUS,
        adapter=AdapterIdentity("synthetic-ledger", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic ledger",
            fact_types=("strategy_returns",),
            observed_start=PREVIOUS,
            observed_end=PREVIOUS,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source),
                as_of_date=PREVIOUS,
                schema_name="synthetic-ledger",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    (staged.parent / "provider-ledger.json").write_bytes(source)
    publish_generation(root, manifest.generation_id)
    manifest_path = staged.parent / "generation.json"
    return manifest.generation_id, {
        "as_of_date": manifest.as_of_date.isoformat(),
        "catalog_sha256": hashlib.sha256(source).hexdigest(),
        "catalog_size_bytes": len(source),
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "manifest_size_bytes": manifest_path.stat().st_size,
    }


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _input_identity(
    tmp_path: Path, name: str, *, panel: bool = False
) -> dict[str, object]:
    path = tmp_path / "control-inputs" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json_bytes({"source": name})
    path.write_bytes(content)
    identity: dict[str, object] = {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    identity["panel_id" if panel else "generation_id"] = hashlib.sha256(
        name.encode("utf-8")
    ).hexdigest()
    if not panel:
        identity["as_of_date"] = PREVIOUS.isoformat()
    return identity


def _recomputation(tmp_path: Path) -> dict[str, object]:
    return {
        "as_of_date": CURRENT.isoformat(),
        "component_count": 2,
        "declared_values": {
            "correlation_stress": "0.3",
            "panel_headline": "0.3",
            "report_headline": "0.3",
        },
        "differences": {
            "correlation_declared": "0",
            "declared_contribution_sum": "0",
            "maximum_component": "0",
            "panel_headline": "0",
            "report_headline": "0",
        },
        "generation_checks": {
            "panel_correlation_matches_current": True,
            "panel_freshness_matches_evaluation_date": True,
            "panel_report_matches_current": True,
        },
        "inputs": {
            "correlation": _input_identity(tmp_path, "correlation"),
            "panel": _input_identity(tmp_path, "panel", panel=True),
            "report": _input_identity(tmp_path, "report"),
        },
        "method": "DECIMAL_SUM_ALLOCATION_WEIGHT_TIMES_ADVERSE_LOSS_FRACTION",
        "recomputed_correlation_one_stress": "0.3",
        "receipt_schema_version": 1,
        "status": "PASS",
        "tolerance": "0.000000000001",
    }


def test_cycle_records_unauthorized_reads_and_local_notification(
    tmp_path: Path,
) -> None:
    ledger_root = tmp_path / "ledger"
    generation_id, ledger_identity = _ledger(ledger_root)
    freshness = tmp_path / "freshness.json"
    _write(
        freshness,
        {
            "assessment_schema_version": 3,
            "as_of_date": CURRENT.isoformat(),
            "assessments": [
                {
                    "escalation_required": True,
                    "evaluated_on": CURRENT.isoformat(),
                    "fact_type": "strategy-returns",
                    "lane": "ACCOUNT",
                    "provider_id": "portfolio123",
                    "required_for_current": True,
                    "state": "STALE",
                    "subject_id": "p123-ai-factor",
                },
                {
                    "escalation_required": False,
                    "evaluated_on": CURRENT.isoformat(),
                    "fact_type": "strategy-returns",
                    "lane": "PROVIDER",
                    "provider_id": "portfolio123",
                    "required_for_current": False,
                    "state": "STALE",
                    "subject_id": "p123-ai-factor",
                },
            ],
            "policies": [],
            "provider_ledger": ledger_identity,
        },
    )
    recomputation = tmp_path / "recomputation.json"
    _write(recomputation, _recomputation(tmp_path))

    receipt = cycle.build_receipt(
        ledger_root,
        generation_id,
        freshness,
        recomputation,
        CURRENT,
        PREVIOUS,
    )

    assert receipt["status"] == "PASS_WITH_RETAINED_ESCALATIONS"
    assert receipt["dated_cycle_evidence"]["status"] == (
        "SECOND_DATED_MANUAL_CYCLE_RECORDED"
    )
    assert receipt["notification_receipt"] == {
        "channel": "LOCAL_CONTROL_PANEL_OPERATOR_QUEUE",
        "external_notification_sent": False,
        "recorded_escalation_count": 1,
        "status": "RECORDED_FOR_NEXT_PANEL_GENERATION",
    }
    assert receipt["pointer_effects"]["advanced"] is False
    assert receipt["provider_read_plan"] == [
        {
            "acquisition_path": "HAR_FIRST_API_ONLY_FOR_DECLARED_HAR_GAP_WITH_APPROVAL",
            "effect": "READ_ONLY_EXTERNAL",
            "fact_type": "strategy-returns",
            "lane": "ACCOUNT",
            "provider_id": "portfolio123",
            "request_identity": receipt["provider_read_plan"][0]["request_identity"],
            "retry_limit": 0,
            "status": "NOT_AUTHORIZED_NOT_EXECUTED",
            "subject_id": "p123-ai-factor",
        }
    ]


def test_cycle_fails_when_freshness_date_is_replayed(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    generation_id, ledger_identity = _ledger(ledger_root)
    freshness = tmp_path / "freshness.json"
    _write(
        freshness,
        {
            "assessment_schema_version": 3,
            "as_of_date": PREVIOUS.isoformat(),
            "assessments": [],
            "policies": [],
            "provider_ledger": ledger_identity,
        },
    )
    recomputation = tmp_path / "recomputation.json"
    _write(recomputation, _recomputation(tmp_path))

    with pytest.raises(cycle.RollforwardCycleError, match="evaluation date differs"):
        cycle.build_receipt(
            ledger_root,
            generation_id,
            freshness,
            recomputation,
            CURRENT,
            PREVIOUS,
        )


def test_cycle_rejects_freshness_from_another_ledger(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    generation_id, ledger_identity = _ledger(ledger_root)
    ledger_identity["generation_id"] = "b" * 64
    freshness = tmp_path / "freshness.json"
    _write(
        freshness,
        {
            "assessment_schema_version": 3,
            "as_of_date": CURRENT.isoformat(),
            "assessments": [],
            "policies": [],
            "provider_ledger": ledger_identity,
        },
    )
    recomputation = tmp_path / "recomputation.json"
    _write(recomputation, _recomputation(tmp_path))

    with pytest.raises(cycle.RollforwardCycleError, match="ledger identity differs"):
        cycle.build_receipt(
            ledger_root,
            generation_id,
            freshness,
            recomputation,
            CURRENT,
            PREVIOUS,
        )


def test_cycle_rejects_truncated_recomputation_receipt(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    generation_id, ledger_identity = _ledger(ledger_root)
    freshness = tmp_path / "freshness.json"
    _write(
        freshness,
        {
            "assessment_schema_version": 3,
            "as_of_date": CURRENT.isoformat(),
            "assessments": [],
            "policies": [],
            "provider_ledger": ledger_identity,
        },
    )
    recomputation = tmp_path / "recomputation.json"
    _write(recomputation, {"as_of_date": CURRENT.isoformat(), "status": "PASS"})

    with pytest.raises(
        cycle.RollforwardCycleError, match="recomputation fields differ"
    ):
        cycle.build_receipt(
            ledger_root,
            generation_id,
            freshness,
            recomputation,
            CURRENT,
            PREVIOUS,
        )
