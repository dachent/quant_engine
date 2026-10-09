"""GOOD-B1 (ruling B.3): the release-findings producer's real CLI, end to end.

The producer/consumer seam between ``tools/validate_release_targets.py main()``
and ``commit_release``'s ``_verify_release_validation`` is pinned only from
both sides with handwritten stand-ins elsewhere (the round-1 review finding);
this module drives the real ``main()`` -- real spec parsing, real prepared
release, real per-target document writes, real exit codes -- and feeds its
written documents to the real commit gate. ``validate_generation.py`` itself
stays out: it is judged on the real panel in run A-verify, not in CI.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from quant_engine.core import (  # noqa: E402
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GateEvidence,
    GenerationManifest,
    ReleaseState,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    stage_generation,
)
from validate_release_targets import main as validate_release_targets_main  # noqa: E402

AS_OF = date(2026, 8, 2)
COMMIT = "c" * 40

REPORT_DATA_BYTES = b'{"report-data": "good-b1"}\n'


def _gate() -> tuple[GateEvidence, ...]:
    return (GateEvidence("bounded-tests", hashlib.sha256(b"pass\n").hexdigest()),)


REPORT_DATA_BYTES = b'{"report-data": "good-b1"}\n'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True).encode("utf-8"))


def _generation(
    label: str,
    root: Path,
    *,
    extra_artifacts: tuple[tuple[str, bytes], ...] = (),
) -> GenerationManifest:
    """A real, staged generation whose artifacts include ``extra_artifacts``."""

    artifacts = [
        SourceArtifact(
            artifact_id=artifact_id,
            sha256=_sha(content),
            size_bytes=len(content),
            as_of_date=AS_OF,
            schema_name="synthetic-release",
            schema_version="1",
        )
        for artifact_id, content in extra_artifacts
    ]
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-release", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {label}",
            fact_types=("release-fixture",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation(root, manifest)
    return manifest


def _panel_generation(
    panel_root: Path, report_generation_id: str, report_data_sha: str
) -> GenerationManifest:
    """The panel generation; its payload binds the report generation by id and
    content digest, so the report generation must exist first."""

    payload_bytes = json.dumps(
        {
            "meta": {
                "report_generation_id": report_generation_id,
                "report_sha256": report_data_sha,
            }
        },
        sort_keys=True,
    ).encode("utf-8")
    manifest = _generation(
        "panel",
        panel_root,
        extra_artifacts=(("data/dashboard_data.json", payload_bytes),),
    )
    payload_path = (
        panel_root
        / "generations"
        / manifest.generation_id
        / "data"
        / "dashboard_data.json"
    )
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(payload_bytes)
    return manifest


def _report_generation(report_root: Path) -> GenerationManifest:
    manifest = _generation(
        "report",
        report_root,
        extra_artifacts=(("portfolio-report-data.json", REPORT_DATA_BYTES),),
    )
    data = (
        report_root
        / "generations"
        / manifest.generation_id
        / "portfolio-report-data.json"
    )
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(REPORT_DATA_BYTES)
    return manifest


def _write_panel_findings(panel_root: Path, generation_id: str) -> Path:
    path = panel_root / "validation" / generation_id / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            {
                "schema": "qe-validate-generation/1",
                "generation_id": generation_id,
                "generation_dir": str(panel_root / "generations" / generation_id),
                "validator_commit": COMMIT,
                "expected_baseline_sha256": "d" * 64,
                "summary": {
                    "BLOCKER_INVARIANT": 0,
                    "BLOCKER_AUTHORITY": 0,
                    "BLOCKER_REGRESSION": 0,
                    "NOT_READY": 0,
                    "WARNING": 0,
                },
                "findings": [],
            }
        ).encode("utf-8")
    )
    return path


def _unclean_panel_findings(panel_root: Path, generation_id: str) -> Path:
    path = panel_root / "validation" / generation_id / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            {
                "schema": "qe-validate-generation/1",
                "generation_id": generation_id,
                "generation_dir": str(panel_root / "generations" / generation_id),
                "validator_commit": COMMIT,
                "expected_baseline_sha256": "d" * 64,
                "summary": {
                    "BLOCKER_INVARIANT": 0,
                    "BLOCKER_AUTHORITY": 1,
                    "BLOCKER_REGRESSION": 0,
                    "NOT_READY": 0,
                    "WARNING": 0,
                },
                "findings": [
                    {
                        "check_id": "test.blocker",
                        "class": "BLOCKER_AUTHORITY",
                        "entity": "x",
                        "observed": "bad",
                        "expected": "good",
                        "pointer": "$",
                        "defect_ids": [],
                    }
                ],
            }
        ).encode("utf-8")
    )
    return path


def _build_world(tmp_path: Path, *, report_digest_override: str | None = None):
    """A synthetic two-target release: panel + portfolio-report, bound."""

    panel_root = tmp_path / "control-panel"
    report_root = tmp_path / "reporting"
    report_manifest = _report_generation(report_root)
    report_digest = report_digest_override or _sha(REPORT_DATA_BYTES)
    panel_manifest = _panel_generation(
        panel_root, report_manifest.generation_id, report_digest
    )
    coordination = tmp_path / "coordination"
    release = prepare_release(
        coordination_root=coordination,
        roots={"portfolio-panel": panel_root, "portfolio-report": report_root},
        proposed_generations={
            "portfolio-panel": panel_manifest.generation_id,
            "portfolio-report": report_manifest.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    spec = {
        "coordination_root": str(coordination),
        "roots": {
            "portfolio-panel": str(panel_root),
            "portfolio-report": str(report_root),
        },
    }
    spec_path = tmp_path / "production-spec.json"
    _write_json(spec_path, spec)
    return release, spec_path, panel_root, report_root


def test_release_validation_producer_main_feeds_the_real_commit_gate(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """GOOD-B1: real main() derives clean documents; the real gate commits."""
    import quant_engine.core.release as release_module

    monkeypatch.setattr(
        release_module, "_verify_release_validation", real_verify_release_validation
    )
    release, spec_path, panel_root, report_root = _build_world(tmp_path)
    panel_findings_path = _write_panel_findings(
        panel_root,
        release.proposed_generations["portfolio-panel"]
        if hasattr(release, "proposed_generations")
        else release.targets[0].proposed_generation_id,
    )

    exit_code = validate_release_targets_main(
        ["--spec", str(spec_path), "--release-id", release.release_id]
    )
    assert exit_code == 0

    report_generation_id = release.targets[-1].proposed_generation_id
    derived = report_root / "validation" / report_generation_id / "findings.json"
    document = json.loads(derived.read_bytes())
    assert document["schema"] == "qe-release-findings/1"
    assert document["expected_baseline_sha256"] == "d" * 64
    assert document["panel_validation"]["findings_sha256"] == _sha(
        panel_findings_path.read_bytes()
    )

    journal = commit_release(
        coordination_root=tmp_path / "coordination",
        release_id=release.release_id,
        roots={"portfolio-panel": panel_root, "portfolio-report": report_root},
    )
    assert journal.state is ReleaseState.COMMITTED


def test_producer_main_refuses_an_unclean_panel_with_exit_2_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """BAD-B4 producer half: an unclean panel findings document refuses before
    any per-target document is written (exit 2, nothing derived)."""
    release, spec_path, panel_root, report_root = _build_world(tmp_path)
    _unclean_panel_findings(panel_root, release.targets[0].proposed_generation_id)

    exit_code = validate_release_targets_main(
        ["--spec", str(spec_path), "--release-id", release.release_id]
    )

    assert exit_code == 2
    assert not (report_root / "validation" / "findings.json").exists()
    for target in release.targets:
        assert (
            not (
                report_root.parent
                / report_root.name
                / "validation"
                / target.proposed_generation_id
                / "findings.json"
            ).exists()
            or target.root_id == "portfolio-panel"
        )


def test_producer_main_exits_1_when_a_binding_blocker_fires(tmp_path: Path) -> None:
    """Exit 1: the producer runs, but a per-target binding blocker fires (here:
    the panel's report digest disagrees with the report generation's bytes)."""
    release, spec_path, panel_root, report_root = _build_world(
        tmp_path, report_digest_override=_sha(b"other-bytes\n")
    )
    _write_panel_findings(panel_root, release.targets[0].proposed_generation_id)

    exit_code = validate_release_targets_main(
        ["--spec", str(spec_path), "--release-id", release.release_id]
    )

    assert exit_code == 1
    report_generation_id = release.targets[-1].proposed_generation_id
    derived = report_root / "validation" / report_generation_id / "findings.json"
    document = json.loads(derived.read_bytes())
    assert document["summary"]["BLOCKER_AUTHORITY"] >= 1
