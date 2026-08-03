from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys

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
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_freshness.py"
SPEC = importlib.util.spec_from_file_location("portfolio_freshness", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio freshness builder")
freshness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = freshness
SPEC.loader.exec_module(freshness)


def _provider_ledger(root: Path) -> str:
    catalog = {
        "as_of_date": "2026-07-31",
        "strategy_evidence": [
            {
                "account": {
                    "observed_end": "2026-07-30",
                    "provider_id": "ibkr",
                },
                "provider": {
                    "observed_end": "2026-07-29",
                    "provider_id": "portfolio123",
                },
                "provider_id": "ibkr",
                "strategy_id": "current-one",
            },
            {
                "provider": {
                    "observed_end": "2026-07-17",
                    "provider_id": "tracker",
                },
                "provider_id": "tracker",
                "strategy_id": "stale-one",
            },
            {
                "provider": {
                    "observed_end": "2026-07-26",
                    "provider_id": "matrixport",
                },
                "provider_id": "matrixport",
                "strategy_id": "late-one",
            },
        ],
    }
    source = root.parent / "provider-ledger.json"
    source.write_bytes(canonical_json_bytes(catalog))
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 31),
        adapter=AdapterIdentity("synthetic-provider-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("strategy_evidence",),
            observed_start=date(2026, 7, 17),
            observed_end=date(2026, 7, 31),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/provider-ledger.json",
                as_of_date=date(2026, 7, 31),
                schema_name="provider-ledger-catalog",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    staged = stage_generation(root, manifest)
    (staged.parent / "provider-ledger.json").write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def test_freshness_prefers_account_lane_and_blocks_stale_strategy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id)

    by_id = {row["subject_id"]: row for row in result["assessments"]}
    assert by_id["current-one"]["provider_id"] == "ibkr"
    assert by_id["current-one"]["state"] == "FRESH"
    assert by_id["current-one"]["blocks_current_consumers"] is False
    assert by_id["stale-one"]["state"] == "STALE"
    assert by_id["stale-one"]["blocks_current_consumers"] is True
    assert by_id["late-one"]["state"] == "LATE"
    assert by_id["late-one"]["blocks_current_consumers"] is False
    assert result["assessment_schema_version"] == 2
    assert result["policy"]["stale_after_days"] == 5


def test_freshness_reages_late_strategy_on_operational_date(tmp_path: Path) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)

    result = freshness.build_health(root, generation_id, date(2026, 8, 2))

    by_id = {row["subject_id"]: row for row in result["assessments"]}
    assert result["as_of_date"] == "2026-08-02"
    assert by_id["late-one"]["state"] == "STALE"
    assert by_id["late-one"]["blocks_current_consumers"] is True


def test_freshness_output_is_create_only_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "provider-ledger"
    generation_id = _provider_ledger(root)
    output = tmp_path / "freshness-health.json"

    first = freshness.write_health(root, generation_id, output)
    second = freshness.write_health(root, generation_id, output)

    assert first == second
    assert json.loads(output.read_text(encoding="utf-8"))["as_of_date"] == (
        "2026-07-31"
    )
