from __future__ import annotations

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
    read_generation_bundle,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report_epoch_binding", ROOT / "tools" / "portfolio_report.py"
)
assert SPEC is not None and SPEC.loader is not None
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _fixture(candidate: Path) -> tuple[Path, Path, bytes, dict[str, object]]:
    manifest = {
        "accounting_currency": "USD",
        "artifact": "account-strategy-manifest",
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "rows": [],
        "schema_version": 8,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    source = {
        "generation_id": "1" * 64,
        "generation_manifest_sha256": "2" * 64,
        "generation_receipt_sha256": "3" * 64,
        "full_directory_inventory_sha256": "4" * 64,
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
            "schema": {"name": "account-strategy-manifest", "version": "8"},
        },
    }
    manifest_path = candidate / "account-strategy-manifest.json"
    source_path = candidate / "epoch-manifest-source.json"
    candidate.mkdir()
    manifest_path.write_bytes(manifest_bytes)
    source_path.write_bytes(canonical_json_bytes(source))
    return manifest_path, source_path, manifest_bytes, source


def test_epoch_manifest_workbook_snapshot_binds_exact_bytes(tmp_path: Path) -> None:
    manifest_path, source_path, manifest_bytes, source = _fixture(
        tmp_path / "candidate"
    )

    snapshots = reporter._epoch_manifest_workbook_snapshots(
        manifest_path.parent, manifest_path, source_path
    )

    assert snapshots["epoch_manifest"] == (manifest_path, manifest_bytes)
    assert json.loads(snapshots["epoch_manifest_source"][1]) == source


@pytest.mark.parametrize("mutation", ("payload", "source_hash", "half_pair"))
def test_epoch_manifest_workbook_snapshot_rejects_incomplete_or_changed_binding(
    tmp_path: Path, mutation: str
) -> None:
    manifest_path, source_path, _manifest_bytes, source = _fixture(
        tmp_path / "candidate"
    )
    supplied_source: Path | None = source_path
    if mutation == "payload":
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    elif mutation == "source_hash":
        source["artifact"]["sha256"] = "0" * 64
        source_path.write_bytes(canonical_json_bytes(source))
    else:
        supplied_source = None

    with pytest.raises(reporter.ReportValidationError):
        reporter._epoch_manifest_workbook_snapshots(
            manifest_path.parent, manifest_path, supplied_source
        )


def test_epoch_history_verification_uses_explicit_independent_generation_root(
    tmp_path: Path,
) -> None:
    epoch_root = tmp_path
    for index in range(1):
        epoch_root /= f"independent-epoch-component-{index:02d}-abcdefghijk"
    manifest_bytes = canonical_json_bytes({"rows": [], "schema_version": 8})
    verification_bytes = canonical_json_bytes({"status": "VERIFIED"})
    manifest = GenerationManifest.build(
        as_of_date=reporter.date(2026, 1, 3),
        adapter=AdapterIdentity("synthetic-epoch", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="standalone epoch generation",
            fact_types=("account-strategy-manifest",),
            observed_start=reporter.date(2026, 1, 3),
            observed_end=reporter.date(2026, 1, 3),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="outputs/account-strategy-manifest.json",
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                size_bytes=len(manifest_bytes),
                as_of_date=reporter.date(2026, 1, 3),
                schema_name="account-strategy-manifest",
                schema_version="8",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(verification_bytes).hexdigest(),
                size_bytes=len(verification_bytes),
                as_of_date=reporter.date(2026, 1, 3),
                schema_name="account-manifest-independent-verification",
                schema_version="3",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        epoch_root,
        manifest,
        {
            "outputs/account-strategy-manifest.json": manifest_bytes,
            "verification/independent-verification.json": verification_bytes,
        },
    )
    bundle = read_generation_bundle(epoch_root, manifest.generation_id)
    verification_path = (
        epoch_root
        / "generations"
        / manifest.generation_id
        / "verification"
        / "independent-verification.json"
    )
    if sys.platform == "win32":
        assert len(str(verification_path.resolve())) > 260
    generation_manifest_bytes = manifest.to_json_bytes()
    source_bytes = canonical_json_bytes(
        {
            "generation_id": manifest.generation_id,
            "generation_manifest_sha256": hashlib.sha256(
                generation_manifest_bytes
            ).hexdigest(),
            "generation_receipt_sha256": hashlib.sha256(
                bundle.receipt_bytes
            ).hexdigest(),
            "full_directory_inventory_sha256": (
                bundle.full_directory_inventory_sha256
            ),
        }
    )

    path, observed, schema_name, schema_version = (
        reporter._epoch_history_snapshot_evidence(
            epoch_root,
            source_bytes,
            manifest_bytes,
        )
    )

    assert path == verification_path
    assert observed == verification_bytes
    assert schema_name == "account-manifest-independent-verification"
    assert schema_version == "3"
