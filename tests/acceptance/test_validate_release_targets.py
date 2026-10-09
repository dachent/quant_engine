"""Paired bad/good fixtures for `acceptance/validate/release_targets.py` (ruling B).

``acceptance/validate/release_targets.py`` binds every non-panel release target to
the panel's own pointers; it reads only the panel's published JSON payload and each
target's own ``generation.json``, never a production builder. This module builds the
smallest synthetic fixtures that exercise exactly those reads: a panel payload
(dict), a per-target ``generation.json`` manifest on disk, and a release target
record -- not the full ``test_validate_generation.py`` generation bundle (report,
shell, candidate), which that module's panel fixture does not shape with the
binding-table pointers this module checks (no ``dependence_modes`` key, no lens
``surfaces.STATIC.sources[]``, no ``account_manifest.source_identities``).

Fixture IDs follow ruling B.3 verbatim (rulings-t0a-epochs-and-release-findings.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from acceptance.validate import release_targets as rt
from acceptance.validate.checks import BLOCKER_AUTHORITY, BLOCKER_INVARIANT
from acceptance.validate.generation import GENERATION_MANIFEST

GOOD_GEN = "a" * 64
PREVIOUS_GEN = "b" * 64
PANEL_GEN = "c" * 64

LENS_ARTIFACT_SHA = "d" * 64


def _dump(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_manifest(
    root_dir: Path,
    generation_id: str,
    *,
    verification: str = "VERIFIED",
    artifacts: list[dict[str, Any]] | None = None,
) -> bytes:
    """Write a minimal ``generation.json`` and return its raw bytes."""

    manifest = {
        "generation_id": generation_id,
        "verification": verification,
        "artifacts": artifacts if artifacts is not None else [],
    }
    content = _dump(manifest)
    path = root_dir / "generations" / generation_id / GENERATION_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def make_target(root_id: str, root_dir: Path, generation_id: str) -> dict[str, Any]:
    """A release target record, with ``proposed_manifest_sha256`` computed from
    whatever bytes are currently on disk for ``generation_id`` under ``root_dir``."""

    manifest_path = root_dir / "generations" / generation_id / GENERATION_MANIFEST
    return {
        "root_id": root_id,
        "proposed_generation_id": generation_id,
        "proposed_manifest_sha256": _sha(manifest_path.read_bytes()),
    }


def base_panel() -> dict[str, Any]:
    """A panel payload carrying one declared pointer per binding-table root,
    each naming ``GOOD_GEN`` -- the good fixture every BAD-* test perturbs."""

    lens_source = {
        "source_key": "us-economic",
        "generation_id": GOOD_GEN,
        "source_sha256": LENS_ARTIFACT_SHA,
    }
    hybrid_source = {
        "source_key": "us-economic",
        "generation_id": GOOD_GEN,
        "source_sha256": None,
    }
    return {
        "meta": {
            "correlation_generation_id": GOOD_GEN,
            "decomposition_generation_id": GOOD_GEN,
            "weekly_returns_generation_id": GOOD_GEN,
            "report_generation_id": GOOD_GEN,
            "baseline_generation_id": GOOD_GEN,
        },
        "dependence_analysis": {"generation_id": GOOD_GEN},
        "dependence_modes": {
            "LIVE_ONLY": {"generation_id": GOOD_GEN},
            "LIVE_PLUS_BACKTEST": {"generation_id": GOOD_GEN},
        },
        "factor_analysis": {"generation_id": GOOD_GEN},
        "weekly_returns": {"generation_id": GOOD_GEN},
        "account_manifest": {
            "source_identities": {
                "baseline": {"generation_id": GOOD_GEN},
                "ledger": {"generation_id": GOOD_GEN},
                "classified_flows": {"ledger_identity": {"generation_id": GOOD_GEN}},
                "flow_coverage": {"ledger_identity": {"generation_id": GOOD_GEN}},
            }
        },
        "decomposition_lenses": {
            "modes": {
                "LIVE_ONLY": [{"surfaces": {"STATIC": {"sources": [lens_source]}}}],
                "LIVE_PLUS_BACKTEST": [
                    {"surfaces": {"STATIC": {"sources": [hybrid_source]}}}
                ],
            }
        },
    }


# ------------------------------------------------------------- target_integrity


def test_target_integrity_clean_when_manifest_matches_and_verified(
    tmp_path: Path,
) -> None:
    write_manifest(tmp_path, GOOD_GEN)
    target = make_target("correlation", tmp_path, GOOD_GEN)
    assert list(rt.target_integrity(target, tmp_path)) == []


def test_target_integrity_blocks_a_missing_manifest(tmp_path: Path) -> None:
    target = {
        "root_id": "correlation",
        "proposed_generation_id": GOOD_GEN,
        "proposed_manifest_sha256": "0" * 64,
    }
    findings = list(rt.target_integrity(target, tmp_path))
    assert [f.check_id for f in findings] == ["release.target_integrity"]
    assert findings[0].klass == BLOCKER_INVARIANT


def test_target_integrity_blocks_a_sha256_mismatch(tmp_path: Path) -> None:
    write_manifest(tmp_path, GOOD_GEN)
    target = {
        "root_id": "correlation",
        "proposed_generation_id": GOOD_GEN,
        "proposed_manifest_sha256": "0" * 64,
    }
    findings = list(rt.target_integrity(target, tmp_path))
    assert [f.check_id for f in findings] == ["release.target_integrity"]
    assert findings[0].klass == BLOCKER_INVARIANT
    assert "sha256" in findings[0].message


def test_target_integrity_blocks_an_unverified_manifest(tmp_path: Path) -> None:
    write_manifest(tmp_path, GOOD_GEN, verification="PENDING")
    target = make_target("correlation", tmp_path, GOOD_GEN)
    findings = list(rt.target_integrity(target, tmp_path))
    assert [f.check_id for f in findings] == ["release.target_integrity"]
    assert "VERIFIED" in findings[0].message


# ------------------------------------------------------------- BAD-B5, BAD-B6, BAD-B7


def test_bad_b5_target_id_not_at_its_declared_panel_pointer(tmp_path: Path) -> None:
    """BAD-B5 (producer): a target whose generation id differs from every
    declared panel pointer for its root -> release.target_binding, blocking."""
    panel = base_panel()
    write_manifest(tmp_path, PREVIOUS_GEN)
    target = make_target("correlation", tmp_path, PREVIOUS_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] and all(
        f.check_id == "release.target_binding" for f in findings
    )
    assert all(f.klass == BLOCKER_AUTHORITY for f in findings)


def test_bad_b6_root_absent_from_the_binding_table(tmp_path: Path) -> None:
    """BAD-B6 (producer): a release root id not in the declared binding table
    -> release.target_unbound, blocking, fails closed for any root added later."""
    panel = base_panel()
    write_manifest(tmp_path, GOOD_GEN)
    target = make_target("some-future-root", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_unbound"]
    assert findings[0].klass == BLOCKER_AUTHORITY


def test_bad_b7_hybrid_dynamic_style_id_appears_unconsumed_in_the_panel(
    tmp_path: Path,
) -> None:
    """BAD-B7 (producer): hybrid-dynamic-style's generation id inserted into
    the panel with no declared binding -> release.target_unconsumed, blocking."""
    panel = base_panel()
    panel["rogue_pointer"] = {"generation_id": GOOD_GEN}
    target = {
        "root_id": rt.UNCONSUMED_ROOT_ID,
        "proposed_generation_id": GOOD_GEN,
    }

    findings = list(rt.target_unconsumed(target, panel))

    assert [f.check_id for f in findings] == ["release.target_unconsumed"]
    assert findings[0].klass == BLOCKER_AUTHORITY


def test_hybrid_dynamic_style_clean_when_truly_unconsumed(tmp_path: Path) -> None:
    panel = base_panel()
    target = {
        "root_id": rt.UNCONSUMED_ROOT_ID,
        "proposed_generation_id": "9" * 64,
    }
    assert list(rt.target_unconsumed(target, panel)) == []


# ------------------------------------------------------------- lens content sha256


def test_lens_source_sha256_mismatch_against_the_targets_own_artifact_blocks(
    tmp_path: Path,
) -> None:
    """The binding table's content-digest cross-check (ruling B.2.2): the
    panel's lens source_sha256 must equal the target's own analytics/
    decomposition.json artifact sha256, not just name the right generation."""
    panel = base_panel()
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "analytics/decomposition.json",
                "sha256": "e" * 64,  # differs from panel's LENS_ARTIFACT_SHA
                "size_bytes": 1,
            }
        ],
    )
    target = make_target("us-economic", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert "source_sha256" in findings[0].json_path


def test_lens_source_sha256_match_is_clean(tmp_path: Path) -> None:
    panel = base_panel()
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "analytics/decomposition.json",
                "sha256": LENS_ARTIFACT_SHA,
                "size_bytes": 1,
            }
        ],
    )
    target = make_target("us-economic", tmp_path, GOOD_GEN)

    assert list(rt.target_binding(target, panel, tmp_path)) == []


# ------------------------------------------------------------- run_target_checks / document


def test_run_target_checks_on_a_fully_clean_target_is_empty(tmp_path: Path) -> None:
    panel = base_panel()
    write_manifest(tmp_path, GOOD_GEN)
    target = make_target("correlation", tmp_path, GOOD_GEN)
    assert rt.run_target_checks(target, tmp_path, panel) == []


def test_release_findings_document_shape() -> None:
    document = rt.release_findings_document(
        generation_id=GOOD_GEN,
        generation_dir="/some/dir",
        validator_commit="c" * 40,
        expected_baseline_sha256="b" * 64,
        findings=[],
        panel_generation_id=PANEL_GEN,
        panel_findings_sha256="f" * 64,
    )
    assert document["schema"] == "qe-release-findings/1"
    assert document["panel_validation"] == {
        "generation_id": PANEL_GEN,
        "findings_sha256": "f" * 64,
    }
    assert set(document) == {
        "schema",
        "generation_id",
        "generation_dir",
        "validator_commit",
        "expected_baseline_sha256",
        "summary",
        "findings",
        "panel_validation",
    }


# ------------------------------------------------------------- BAD-B8 (re-verified here)


def test_bad_b8_null_expected_baseline_is_carried_through_unchanged() -> None:
    """BAD-B8: a null expected_baseline_sha256 must be preserved in the
    produced document (the refusal itself is enforced at the gate, already
    covered in tests/core/test_publication.py and test_release.py)."""
    document = rt.release_findings_document(
        generation_id=GOOD_GEN,
        generation_dir="/some/dir",
        validator_commit="c" * 40,
        expected_baseline_sha256=None,
        findings=[],
        panel_generation_id=PANEL_GEN,
        panel_findings_sha256="f" * 64,
    )
    assert document["expected_baseline_sha256"] is None


# ------------------------------------------------------------- Seam test


def test_known_roots_equals_production_roots_minus_the_panel() -> None:
    """Seam test (ruling B.3): the binding table's root set equals
    PRODUCTION_ROOTS minus portfolio-panel."""
    import portfolio_pipeline_materialize as materializer

    assert rt.KNOWN_ROOTS == frozenset(materializer.PRODUCTION_ROOTS) - {
        rt.PANEL_ROOT_ID
    }
