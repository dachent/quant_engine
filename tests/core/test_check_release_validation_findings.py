"""tools/check_release_validation_findings.py against the cross-lane contract.

This CLI is a thin wrapper around
`quant_engine.core.publication.evaluate_findings_document_at_path` -- the
single-document check the publication sink's
`evaluate_release_findings_set` also applies per target (ruling B). These
tests exercise the CLI boundary (argv, exit code, stderr/stdout); the
contract's edge cases (malformed documents, class typing, generation-id
mismatch) are exercised directly against `evaluate_release_validation_findings`
and `evaluate_release_findings_set` in `tests/core/test_publication.py`, and
against `commit_release`'s gate in `tests/core/test_release.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_release_validation_findings.py"


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "check_id": "CHECK_EXAMPLE",
        "class": "WARNING",
        "entity": "example-entity",
        "observed": "x",
        "expected": "y",
        "pointer": "generations/abc/report.json",
        "defect_ids": [],
    }
    base.update(overrides)
    return base


def _document(
    generation_id: str,
    *,
    findings: tuple[dict[str, object], ...] = (),
    baseline: str | None = "b" * 64,
) -> dict[str, object]:
    summary = {
        "BLOCKER_INVARIANT": 0,
        "BLOCKER_AUTHORITY": 0,
        "BLOCKER_REGRESSION": 0,
        "NOT_READY": 0,
        "WARNING": 0,
    }
    for entry in findings:
        summary[str(entry["class"])] += 1
    return {
        "schema": "qe-validate-generation/1",
        "generation_id": generation_id,
        "generation_dir": f"/generations/{generation_id}",
        "validator_commit": "c" * 40,
        "expected_baseline_sha256": baseline,
        "summary": summary,
        "findings": list(findings),
    }


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_cli_refuses_when_findings_file_is_absent(tmp_path: Path) -> None:
    result = _run_cli(
        "--generation-id", "gen-1", "--root", str(tmp_path / "nowhere-root")
    )

    assert result.returncode == 1
    assert "REFUSED" in result.stderr


def test_cli_clears_with_a_clean_document_at_the_default_path(tmp_path: Path) -> None:
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_document("gen-1")), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 0
    assert "CLEARED" in result.stdout


def test_cli_refuses_when_expected_baseline_is_null(tmp_path: Path) -> None:
    """T2.8 (PR #487 bot finding 4113723374): no --expected means no
    regression comparison happened; the CLI (a thin wrapper around the same
    gate `commit_release` uses) must refuse rather than CLEAR."""
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_document("gen-1", baseline=None)), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "expected_baseline_sha256" in result.stderr


def test_cli_refuses_when_a_blocker_is_present(tmp_path: Path) -> None:
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            _document("gen-1", findings=(_finding(**{"class": "BLOCKER_AUTHORITY"}),))
        ),
        encoding="utf-8",
    )

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "BLOCKER_AUTHORITY" in result.stderr


def test_cli_refuses_when_generation_id_differs(tmp_path: Path) -> None:
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_document("a-different-generation")), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "generation_id differs" in result.stderr


def test_cli_accepts_an_explicit_findings_path(tmp_path: Path) -> None:
    path = tmp_path / "somewhere" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_document("gen-1")), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--findings", str(path))

    assert result.returncode == 0


def test_cli_refuses_on_a_malformed_document(tmp_path: Path) -> None:
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"not": "a valid document"}), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "REFUSED" in result.stderr


def _derived_document(
    generation_id: str,
    *,
    findings: tuple[dict[str, object], ...] = (),
    panel_generation_id: str = "panel-gen",
    panel_findings_sha256: str = "f" * 64,
) -> dict[str, object]:
    document = _document(generation_id, findings=findings)
    document["schema"] = "qe-release-findings/1"
    document["panel_validation"] = {
        "generation_id": panel_generation_id,
        "findings_sha256": panel_findings_sha256,
    }
    return document


def test_cli_clears_a_clean_derived_qe_release_findings_document(
    tmp_path: Path,
) -> None:
    """Ruling B: the CLI's single-document check accepts the derived
    ``qe-release-findings/1`` schema the same way it accepts the panel's own
    ``qe-validate-generation/1`` -- the cross-target staleness check against
    the release's current panel findings only exists at the publication sink
    (``evaluate_release_findings_set``), not from one path alone."""
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_derived_document("gen-1")), encoding="utf-8")

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 0
    assert "CLEARED" in result.stdout


def test_cli_refuses_a_derived_document_with_a_blocker(tmp_path: Path) -> None:
    path = tmp_path / "validation" / "gen-1" / "findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            _derived_document(
                "gen-1", findings=(_finding(**{"class": "BLOCKER_AUTHORITY"}),)
            )
        ),
        encoding="utf-8",
    )

    result = _run_cli("--generation-id", "gen-1", "--root", str(tmp_path))

    assert result.returncode == 1
    assert "BLOCKER_AUTHORITY" in result.stderr
