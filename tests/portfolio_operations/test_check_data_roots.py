"""The data-root gate reproduces each audited pointer defect and passes a good root."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import check_data_roots as gate  # noqa: E402

AS_OF = date(2026, 7, 30)


def _publish(root: Path) -> str:
    content = b"synthetic-content"
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-baseline", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic accounts on the stated as-of date",
            fact_types=("positions", "transactions"),
            observed_start=date(2026, 1, 1),
            observed_end=AS_OF,
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="inputs/tracker.json",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic-portfolio-input",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation(root, manifest)
    publish_generation(root, manifest.generation_id)
    return manifest.generation_id


def test_good_root_passes(tmp_path: Path) -> None:
    root = tmp_path / "dataSchwab"
    generation = _publish(root)
    ok, message = gate.check_root(root)
    assert ok, message
    assert generation[:12] in message
    assert gate.main(["--workspace", str(tmp_path)]) == 0


def test_dangling_pointer_fails(tmp_path: Path) -> None:
    root = tmp_path / "dataSchwab"
    generation = _publish(root)
    shutil.rmtree(root / "generations" / generation)
    ok, message = gate.check_root(root)
    assert not ok and "missing" in message
    assert gate.main(["--workspace", str(tmp_path)]) == 1


def test_legacy_schema_v1_pointer_fails(tmp_path: Path) -> None:
    root = tmp_path / "dataFidelity"
    _publish(root)
    pointer = json.loads((root / "CURRENT.json").read_text(encoding="utf-8"))
    legacy = {
        "generation_id": pointer["generation_id"],
        "manifest_sha256": pointer["manifest_sha256"],
        "pointer_schema_version": 1,
        "state": "PUBLISHED",
    }
    (root / "CURRENT.json").write_text(json.dumps(legacy), encoding="utf-8")
    ok, message = gate.check_root(root)
    assert not ok and "legacy pointer schema" in message


def test_generations_without_a_pointer_fail(tmp_path: Path) -> None:
    root = tmp_path / "dataEpochManifest"
    _publish(root)
    (root / "CURRENT.json").unlink()
    ok, message = gate.check_root(root)
    assert not ok and "CURRENT.json is missing" in message


def test_empty_or_absent_roots_do_not_fail(tmp_path: Path) -> None:
    (tmp_path / "dataRobuxio").mkdir()
    assert gate.check_root(tmp_path / "dataRobuxio")[0]
    assert gate.main(["--workspace", str(tmp_path)]) == 0


def test_usage_errors_exit_two(tmp_path: Path) -> None:
    assert gate.main(["--workspace", str(tmp_path / "nope")]) == 2
    assert gate.main(["--workspace", str(tmp_path), "--root", "dataBogus"]) == 2


def test_every_published_data_root_is_a_boundary_forbidden_root() -> None:
    policy = (REPOSITORY_ROOT / ".policy" / "data-boundary.toml").read_text("utf-8")
    missing = [name for name in gate.DATA_ROOTS if f'"{name}"' not in policy]
    assert missing == [], f"add to forbidden_root_names: {missing}"
    example = (REPOSITORY_ROOT / "config.example.toml").read_text("utf-8")
    assert "../dataRobuxio" in example and "../dataEpochManifest" in example
