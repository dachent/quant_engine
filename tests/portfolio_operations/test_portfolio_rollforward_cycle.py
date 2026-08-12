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
SPEC = importlib.util.spec_from_file_location("portfolio_rollforward_cycle", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load rollforward cycle tool")
cycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cycle
SPEC.loader.exec_module(cycle)

CURRENT = date(2026, 8, 6)
PREVIOUS = date(2026, 8, 3)


def _ledger(root: Path) -> str:
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
                artifact_id="input/ledger.json",
                sha256=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source),
                as_of_date=PREVIOUS,
                schema_name="synthetic-ledger",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation(root, manifest)
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(canonical_json_bytes(value))


def test_cycle_records_unauthorized_reads_and_local_notification(
    tmp_path: Path,
) -> None:
    ledger_root = tmp_path / "ledger"
    generation_id = _ledger(ledger_root)
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
        },
    )
    recomputation = tmp_path / "recomputation.json"
    _write(recomputation, {"as_of_date": CURRENT.isoformat(), "status": "PASS"})

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
    generation_id = _ledger(ledger_root)
    freshness = tmp_path / "freshness.json"
    _write(
        freshness,
        {
            "assessment_schema_version": 3,
            "as_of_date": PREVIOUS.isoformat(),
            "assessments": [],
            "policies": [],
        },
    )
    recomputation = tmp_path / "recomputation.json"
    _write(recomputation, {"as_of_date": CURRENT.isoformat(), "status": "PASS"})

    with pytest.raises(cycle.RollforwardCycleError, match="evaluation date differs"):
        cycle.build_receipt(
            ledger_root,
            generation_id,
            freshness,
            recomputation,
            CURRENT,
            PREVIOUS,
        )
