from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

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
RECOMPUTE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_recompute.py"
CYCLE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_rollforward_cycle.py"
SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "portfolio" / "control-recomputation.schema.json"


def _load_tool(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recompute = _load_tool("portfolio_control_recompute_for_cycle", RECOMPUTE_PATH)
cycle = _load_tool("portfolio_rollforward_cycle", CYCLE_PATH)

CURRENT = date(2026, 8, 6)
PREVIOUS = date(2026, 8, 3)


def _publish(root: Path, filename: str, payload: dict[str, object], name: str) -> str:
    source = canonical_json_bytes({"source": name})
    manifest = GenerationManifest.build(
        as_of_date=CURRENT,
        adapter=AdapterIdentity(name, "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic control input",
            fact_types=("correlation-one-stress",),
            observed_start=CURRENT,
            observed_end=CURRENT,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"input/{name}.json",
                sha256=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source),
                as_of_date=CURRENT,
                schema_name="synthetic-input",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    (staged.parent / filename).write_bytes(canonical_json_bytes(payload))
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def _ledger(root: Path, marker: str) -> tuple[str, dict[str, object]]:
    source = canonical_json_bytes({"catalog": marker})
    manifest = GenerationManifest.build(
        as_of_date=PREVIOUS,
        adapter=AdapterIdentity(f"synthetic-ledger-{marker}", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
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
                schema_name="synthetic-provider-ledger",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    catalog_path = staged.parent / "provider-ledger.json"
    catalog_path.write_bytes(source)
    publish_generation(root, manifest.generation_id)
    manifest_path = staged.parent / "generation.json"
    return manifest.generation_id, {
        "as_of_date": PREVIOUS.isoformat(),
        "catalog_sha256": hashlib.sha256(source).hexdigest(),
        "catalog_size_bytes": len(source),
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "manifest_size_bytes": manifest_path.stat().st_size,
    }


def _freshness(
    ledger_identity: dict[str, object], *, stale: bool = False
) -> dict[str, object]:
    return {
        "assessment_schema_version": 3,
        "as_of_date": CURRENT.isoformat(),
        "assessments": [
            {
                "acquired_at": None,
                "blocks_current_consumers": stale,
                "coverage_status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                "escalation_required": stale,
                "evaluated_on": CURRENT.isoformat(),
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "last_accepted_date": PREVIOUS.isoformat() if stale else CURRENT.isoformat(),
                "next_expected_date": CURRENT.isoformat(),
                "policy_id": "synthetic-daily",
                "provider_id": "portfolio123" if stale else "synthetic",
                "reason": "stale" if stale else "fresh",
                "required_for_current": True,
                "revision_state": "ORIGINAL",
                "state": "STALE" if stale else "FRESH",
                "subject_id": "p123-ai-factor" if stale else "synthetic",
                "subject_type": "DATASET",
            }
        ],
        "policies": [
            {
                "calendar": "CALENDAR_DAY",
                "expected_interval_days": 1,
                "grace_days": 0,
                "mode": "RECURRING",
                "policy_id": "synthetic-daily",
                "stale_after_days": 1,
            }
        ],
        "provider_ledger": ledger_identity,
    }


def _panel_id(artifact_sha256: dict[str, str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(dict(sorted(artifact_sha256.items())))
    ).hexdigest()


def _candidate(
    root: Path,
    correlation_generation: str,
    report_generation: str,
    freshness: dict[str, object],
) -> tuple[Path, Path]:
    dashboard = {
        "advisory": {"correlation_one_stress": "0.3"},
        "meta": {
            "correlation_generation_id": correlation_generation,
            "freshness_evaluated_on": CURRENT.isoformat(),
            "report_generation_id": report_generation,
        },
    }
    files = {
        "data/dashboard_data.json": canonical_json_bytes(dashboard),
        "data/freshness-health.json": canonical_json_bytes(freshness),
    }
    artifact_sha256 = {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }
    panel_id = _panel_id(artifact_sha256)
    candidate = root / "candidates" / panel_id
    for name, content in files.items():
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (candidate / "candidate.json").write_bytes(
        canonical_json_bytes(
            {
                "artifact_sha256": artifact_sha256,
                "panel_id": panel_id,
                "report_generation_id": report_generation,
            }
        )
    )
    return candidate, candidate / "data" / "freshness-health.json"


def _cycle_inputs(
    tmp_path: Path, *, marker: str = "one", stale: bool = False
) -> tuple[Path, str, Path, dict[str, object]]:
    correlation_root = tmp_path / f"correlation-{marker}"
    correlation_generation = _publish(
        correlation_root,
        "correlation.json",
        {
            "dependence": {
                "correlation_one": {
                    "components": [
                        {
                            "allocation_weight": "0.4",
                            "conservative_loss_fraction": "0.25",
                            "loss_contribution": "0.1",
                        },
                        {
                            "adverse_loss_fraction": "0.5",
                            "allocation_weight": "0.4",
                            "loss_contribution": "0.2",
                        },
                    ],
                    "loss": "0.3",
                }
            }
        },
        f"correlation-{marker}",
    )
    report_root = tmp_path / f"report-{marker}"
    report_generation = _publish(
        report_root,
        "portfolio-report-data.json",
        {"portfolio_summary": [{"metric": "Correlation-one stress", "value": "0.3"}]},
        f"report-{marker}",
    )
    ledger_root = tmp_path / f"ledger-{marker}"
    ledger_generation, ledger_identity = _ledger(ledger_root, marker)
    candidate, freshness = _candidate(
        tmp_path / f"panel-{marker}",
        correlation_generation,
        report_generation,
        _freshness(ledger_identity, stale=stale),
    )
    recomputation = recompute.build_receipt(
        correlation_root,
        report_root,
        candidate,
        freshness,
        ledger_root,
        ledger_generation,
        CURRENT,
    )
    return ledger_root, ledger_generation, freshness, recomputation


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(canonical_json_bytes(value))


def test_cycle_accepts_real_v2_recomputation_and_retains_escalation(
    tmp_path: Path,
) -> None:
    ledger_root, generation_id, freshness, recomputation = _cycle_inputs(
        tmp_path, stale=True
    )
    recomputation_path = tmp_path / "real-control-recomputation.json"
    _write(recomputation_path, recomputation)

    receipt = cycle.build_receipt(
        ledger_root,
        generation_id,
        freshness,
        recomputation_path,
        CURRENT,
        PREVIOUS,
    )

    expected_request_identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "evaluation_date": CURRENT.isoformat(),
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "provider_id": "portfolio123",
                "subject_id": "p123-ai-factor",
            }
        )
    ).hexdigest()
    assert receipt["status"] == "PASS_WITH_RETAINED_ESCALATIONS"
    assert receipt["provider_read_plan"] == [
        {
            "acquisition_path": "HAR_FIRST_API_ONLY_FOR_DECLARED_HAR_GAP_WITH_APPROVAL",
            "effect": "READ_ONLY_EXTERNAL",
            "fact_type": "strategy-returns",
            "lane": "ACCOUNT",
            "provider_id": "portfolio123",
            "request_identity": expected_request_identity,
            "retry_limit": 0,
            "status": "NOT_AUTHORIZED_NOT_EXECUTED",
            "subject_id": "p123-ai-factor",
        }
    ]
    assert receipt["pointer_effects"]["advanced"] is False
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(recomputation)
    missing = deepcopy(recomputation)
    del missing["inputs"]["panel"]["candidate_record"]
    with pytest.raises(ValidationError):
        validator.validate(missing)
    extra = deepcopy(recomputation)
    extra["inputs"]["freshness"]["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(extra)


def test_cycle_rejects_real_recomputation_cross_paired_to_other_valid_freshness_and_ledger(
    tmp_path: Path,
) -> None:
    ledger_one, generation_one, freshness_one, recomputation_one = _cycle_inputs(
        tmp_path, marker="one"
    )
    _ledger_two, _generation_two, _freshness_two, recomputation_two = _cycle_inputs(
        tmp_path, marker="two"
    )
    cross_paired = deepcopy(recomputation_one)
    cross_paired["inputs"]["freshness"] = recomputation_two["inputs"]["freshness"]
    cross_paired["inputs"]["provider_ledger"] = recomputation_two["inputs"][
        "provider_ledger"
    ]
    cross_paired["inputs"]["panel"] = recomputation_two["inputs"]["panel"]
    assert all(cross_paired["generation_checks"].values())
    recomputation_path = tmp_path / "cross-paired-recomputation.json"
    _write(recomputation_path, cross_paired)

    with pytest.raises(
        cycle.RollforwardCycleError, match="freshness identity differs"
    ):
        cycle.build_receipt(
            ledger_one,
            generation_one,
            freshness_one,
            recomputation_path,
            CURRENT,
            PREVIOUS,
        )


def test_cycle_rejects_replayed_freshness_date_before_recomputation_validation(
    tmp_path: Path,
) -> None:
    ledger_root, generation_id, freshness, recomputation = _cycle_inputs(tmp_path)
    freshness_payload = json.loads(freshness.read_text(encoding="utf-8"))
    freshness_payload["as_of_date"] = PREVIOUS.isoformat()
    freshness_payload["assessments"][0]["evaluated_on"] = PREVIOUS.isoformat()
    freshness.write_bytes(canonical_json_bytes(freshness_payload))
    recomputation_path = tmp_path / "real-recomputation.json"
    _write(recomputation_path, recomputation)

    with pytest.raises(cycle.RollforwardCycleError, match="evaluation date differs"):
        cycle.build_receipt(
            ledger_root,
            generation_id,
            freshness,
            recomputation_path,
            CURRENT,
            PREVIOUS,
        )
