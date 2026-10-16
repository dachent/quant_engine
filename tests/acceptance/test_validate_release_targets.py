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

# The lens content artifact's actual bytes on disk, and the sha256 every
# GOOD fixture level agrees on: the panel's source_sha256, the generation
# manifest's declared artifact sha256, and the artifact file's real bytes.
LENS_ARTIFACT_BYTES = b'{"decomposition": "fixture-bytes"}\n'
LENS_ARTIFACT_SHA = hashlib.sha256(LENS_ARTIFACT_BYTES).hexdigest()


def write_lens_artifact(
    root_dir: Path, generation_id: str, *, nested: bool = False
) -> None:
    """Write the lens content artifact whose sha256 is ``LENS_ARTIFACT_SHA``.

    Lens generations stage the artifact FLAT (decomposition.json) while the
    manifest records the full id — the layout the basename fallback resolves.
    ``nested=True`` stages the full-id path (the full-id-first control).
    """

    relative = "analytics/decomposition.json" if nested else "decomposition.json"
    artifact = root_dir / "generations" / generation_id / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(LENS_ARTIFACT_BYTES)


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


def test_bad_b6_unknown_hybrid_root_is_not_resolved_by_prefix_alone(
    tmp_path: Path,
) -> None:
    """Astra :185: ``_declared_pointers`` must not resolve ``hybrid-<x>`` for an
    ``<x>`` outside the declared lens root set -- an unknown hybrid root must
    fall through to ``release.target_unbound``, not silently bind to whatever
    panel pointer happens to share its suffix."""
    panel = base_panel()
    write_manifest(tmp_path, GOOD_GEN)
    target = make_target("hybrid-future-root", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_unbound"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert "hybrid-future-root" not in rt.KNOWN_ROOTS


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

    findings = list(rt.target_unconsumed(target, panel, {}))

    assert [f.check_id for f in findings] == ["release.target_unconsumed"]
    assert findings[0].klass == BLOCKER_AUTHORITY


def test_hybrid_dynamic_style_clean_when_truly_unconsumed(tmp_path: Path) -> None:
    panel = base_panel()
    target = {
        "root_id": rt.UNCONSUMED_ROOT_ID,
        "proposed_generation_id": "9" * 64,
    }
    assert list(rt.target_unconsumed(target, panel, {})) == []


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
    # The manifest's declared sha256 must also match the artifact's actual
    # bytes (the byte-level half of the B.2.2 binding).
    write_lens_artifact(tmp_path, GOOD_GEN)
    target = make_target("us-economic", tmp_path, GOOD_GEN)

    assert list(rt.target_binding(target, panel, tmp_path)) == []


# ------------------------------------------------------------- run_target_checks / document


def test_run_target_checks_on_a_fully_clean_target_is_empty(tmp_path: Path) -> None:
    panel = base_panel()
    write_manifest(tmp_path, GOOD_GEN)
    target = make_target("correlation", tmp_path, GOOD_GEN)
    assert rt.run_target_checks(target, tmp_path, panel, {}) == []


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


# ------------------------------------------------- content-digest bindings (B.2.2)


def _panel_with_digests(**overrides: str) -> dict[str, Any]:
    """base_panel plus the content digests the panel carries for the simple
    roots' files, each naming the digest of the fixture artifact bytes."""

    panel = base_panel()
    panel["meta"].update(
        {
            "report_sha256": overrides.get("report", _sha(REPORT_ARTIFACT_BYTES)),
            "correlation_sha256": overrides.get(
                "correlation", _sha(CORRELATION_ARTIFACT_BYTES)
            ),
            "decomposition_sha256": overrides.get(
                "decomposition", _sha(DECOMPOSITION_ARTIFACT_BYTES)
            ),
            "weekly_returns_sha256": overrides.get(
                "weekly", _sha(WEEKLY_ARTIFACT_BYTES)
            ),
        }
    )
    if "baseline_catalog" in overrides:
        for row in panel.setdefault("provenance", []):
            if row.get("artifact") == "baseline-catalog":
                row["sha256"] = overrides["baseline_catalog"]
    else:
        panel["provenance"] = [
            {
                "artifact": "baseline-generation",
                "sha256": GOOD_GEN,
            },
            {
                "artifact": "baseline-catalog",
                "sha256": overrides.get(
                    "baseline_catalog", _sha(BASELINE_CATALOG_BYTES)
                ),
            },
        ]
    return panel


REPORT_ARTIFACT_BYTES = b'{"report": "fixture"}\n'
CORRELATION_ARTIFACT_BYTES = b'{"correlation": "fixture"}\n'
DECOMPOSITION_ARTIFACT_BYTES = LENS_ARTIFACT_BYTES
WEEKLY_ARTIFACT_BYTES = b'{"weekly": "fixture"}\n'
BASELINE_CATALOG_BYTES = b'{"baseline-catalog": "fixture"}\n'


def _write_content_artifacts(root_dir: Path, generation_id: str) -> None:
    for artifact_id, content in (
        ("portfolio-report-data.json", REPORT_ARTIFACT_BYTES),
        ("correlation.json", CORRELATION_ARTIFACT_BYTES),
        ("analytics/decomposition.json", DECOMPOSITION_ARTIFACT_BYTES),
        ("weekly-returns.json", WEEKLY_ARTIFACT_BYTES),
        ("portfolio-baseline.json", BASELINE_CATALOG_BYTES),
    ):
        path = root_dir / "generations" / generation_id / artifact_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_content_digests_of_the_real_artifact_bytes_bind_for_every_simple_root(
    tmp_path: Path,
) -> None:
    """B.2.2: report/correlation/decomposition/weekly content digests must
    match the target generation's actual artifact bytes, not only the ids."""
    panel = _panel_with_digests()
    write_manifest(tmp_path, GOOD_GEN)
    _write_content_artifacts(tmp_path, GOOD_GEN)
    write_lens_artifact(tmp_path, GOOD_GEN)
    for root_id in (
        "portfolio-report",
        "correlation",
        "factor-decomposition",
        "weekly-returns",
    ):
        target = make_target(root_id, tmp_path, GOOD_GEN)
        assert list(rt.target_binding(target, panel, tmp_path)) == [], root_id


def test_a_report_content_digest_mismatch_is_a_binding_failure(tmp_path: Path) -> None:
    panel = _panel_with_digests(report=_sha(b"different-bytes\n"))
    write_manifest(tmp_path, GOOD_GEN)
    _write_content_artifacts(tmp_path, GOOD_GEN)
    target = make_target("portfolio-report", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert "meta.report_sha256" in findings[0].json_path


def test_a_missing_content_artifact_is_a_binding_failure_not_a_skip(
    tmp_path: Path,
) -> None:
    """The Astra probe: a declared digest with no artifact file behind it must
    refuse, not emit a clean bind."""
    panel = _panel_with_digests()
    write_manifest(tmp_path, GOOD_GEN)
    target = make_target("portfolio-report", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert findings[0].observed is None


def test_a_lens_declared_digest_without_the_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    """The Astra probe: a lens target whose intact manifest carries no
    matching analytics/decomposition.json artifact must refuse when the panel
    declares a source_sha256, instead of silently passing."""
    panel = base_panel()
    write_manifest(tmp_path, GOOD_GEN, artifacts=[])  # no artifact declared
    target = make_target("us-economic", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert findings[0].observed is None


def test_lens_artifact_bytes_disagreeing_with_the_manifest_block(
    tmp_path: Path,
) -> None:
    """The manifest's declared sha256 must match the artifact's actual bytes,
    not only the panel's declaration."""
    panel = base_panel()
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "analytics/decomposition.json",
                "sha256": LENS_ARTIFACT_SHA,
            }
        ],
    )
    (tmp_path / "generations" / GOOD_GEN / "analytics").mkdir(
        parents=True, exist_ok=True
    )
    (
        tmp_path / "generations" / GOOD_GEN / "analytics" / "decomposition.json"
    ).write_bytes(b"tampered\n")
    target = make_target("us-economic", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert "bytes do not match" in findings[0].message


def test_baseline_provenance_rows_bind_to_the_target_bytes(tmp_path: Path) -> None:
    """B.2.2's baseline example: provenance rows for baseline-generation and
    baseline-catalog must match the target's manifest and catalog bytes."""
    panel = _panel_with_digests()
    write_manifest(tmp_path, GOOD_GEN)
    _write_content_artifacts(tmp_path, GOOD_GEN)
    target = make_target("portfolio-baseline", tmp_path, GOOD_GEN)

    assert list(rt.target_binding(target, panel, tmp_path)) == []

    panel["provenance"][1]["sha256"] = _sha(b"other-catalog\n")
    findings = list(rt.target_binding(target, panel, tmp_path))
    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].entity == "portfolio-baseline"


# ------------------------------------------------- unconsumed vs panel AND report


def test_unconsumed_generation_id_in_the_report_alone_blocks(tmp_path: Path) -> None:
    """B.2.3: the unconsumed claim is verified against the panel AND the
    report; a report-only consumer is a refusal (the Astra probe)."""
    unconsumed_gen = "9" * 64  # absent from the panel by construction
    write_manifest(tmp_path, unconsumed_gen)
    target = make_target("hybrid-dynamic-style", tmp_path, unconsumed_gen)
    report = {"some": {"nested": unconsumed_gen}}

    findings = list(rt.target_unconsumed(target, base_panel(), report))

    assert [f.check_id for f in findings] == ["release.target_unconsumed"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert "report" in findings[0].message


def test_unconsumed_clean_when_absent_from_both_payloads(tmp_path: Path) -> None:
    target = {
        "root_id": "hybrid-dynamic-style",
        "proposed_generation_id": "9" * 64,
    }
    assert list(rt.target_unconsumed(target, base_panel(), {"other": 1})) == []


def test_surface_source_sha256_locations_bind_for_every_root(tmp_path: Path) -> None:
    """B.2.2 EVERY-digest requirement (round-2 probe): dependence_analysis,
    dependence_modes, factor_analysis and weekly_returns source_sha256
    locations must bind to the target's bytes; corrupting any one refuses."""
    panel = _panel_with_digests()
    for surface in (
        "dependence_analysis",
        "factor_analysis",
        "weekly_returns",
    ):
        panel[surface]["source_sha256"] = _sha(
            {
                "dependence_analysis": CORRELATION_ARTIFACT_BYTES,
                "factor_analysis": DECOMPOSITION_ARTIFACT_BYTES,
                "weekly_returns": WEEKLY_ARTIFACT_BYTES,
            }[surface]
        )
    panel["dependence_modes"]["LIVE_ONLY"]["source_sha256"] = _sha(
        CORRELATION_ARTIFACT_BYTES
    )
    panel["dependence_modes"]["LIVE_PLUS_BACKTEST"]["source_sha256"] = _sha(
        CORRELATION_ARTIFACT_BYTES
    )
    write_manifest(tmp_path, GOOD_GEN)
    _write_content_artifacts(tmp_path, GOOD_GEN)
    write_lens_artifact(tmp_path, GOOD_GEN)
    for root_id, surface_pointer in (
        ("correlation", "dependence_analysis"),
        ("factor-decomposition", "factor_analysis"),
        ("weekly-returns", "weekly_returns"),
    ):
        target = make_target(root_id, tmp_path, GOOD_GEN)
        assert list(rt.target_binding(target, panel, tmp_path)) == [], root_id
        panel[surface_pointer]["source_sha256"] = _sha(b"corrupted\n")
        findings = list(rt.target_binding(target, panel, tmp_path))
        assert [f.check_id for f in findings] == ["release.target_binding"], root_id
        assert findings[0].klass == BLOCKER_AUTHORITY
        assert surface_pointer in findings[0].json_path
        panel[surface_pointer]["source_sha256"] = {
            "dependence_analysis": _sha(CORRELATION_ARTIFACT_BYTES),
            "factor_analysis": _sha(DECOMPOSITION_ARTIFACT_BYTES),
            "weekly_returns": _sha(WEEKLY_ARTIFACT_BYTES),
        }[surface_pointer]


def test_a_provenance_row_for_a_non_baseline_root_binds(tmp_path: Path) -> None:
    """The Astra probe: provenance[artifact=portfolio-correlation-generation]
    must bind to the correlation target's bytes."""
    panel = _panel_with_digests()
    panel["provenance"].append(
        {
            "artifact": "portfolio-correlation-generation",
            "sha256": _sha(CORRELATION_ARTIFACT_BYTES),
        }
    )
    write_manifest(tmp_path, GOOD_GEN)
    _write_content_artifacts(tmp_path, GOOD_GEN)
    write_lens_artifact(tmp_path, GOOD_GEN)
    target = make_target("correlation", tmp_path, GOOD_GEN)
    assert list(rt.target_binding(target, panel, tmp_path)) == []
    panel["provenance"][-1]["sha256"] = _sha(b"wrong\n")
    findings = list(rt.target_binding(target, panel, tmp_path))
    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert "portfolio-correlation-generation" in findings[0].message


def test_lens_provenance_rows_bind_to_their_owning_roots(tmp_path: Path) -> None:
    """The Astra delta-2 probe: provenance[artifact=portfolio-decomposition-
    lens-<key>] carries the lens content digest and must bind to the owning
    LIVE_ONLY lens root's actual artifact bytes."""
    panel = _panel_with_digests()
    for source_key in ("us-economic", "us-equity-style"):
        panel["provenance"].append(
            {
                "artifact": f"portfolio-decomposition-lens-{source_key}",
                "sha256": LENS_ARTIFACT_SHA,
            }
        )
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "analytics/decomposition.json",
                "sha256": LENS_ARTIFACT_SHA,
            }
        ],
    )
    _write_content_artifacts(tmp_path, GOOD_GEN)
    write_lens_artifact(tmp_path, GOOD_GEN)
    target = make_target("us-economic", tmp_path, GOOD_GEN)
    assert list(rt.target_binding(target, panel, tmp_path)) == []
    for row in panel["provenance"]:
        if row["artifact"] == "portfolio-decomposition-lens-us-economic":
            row["sha256"] = _sha(b"wrong-lens-bytes\n")
    findings = list(rt.target_binding(target, panel, tmp_path))
    assert [f.check_id for f in findings] == ["release.target_binding"]
    assert findings[0].klass == BLOCKER_AUTHORITY
    assert "portfolio-decomposition-lens-us-economic" in findings[0].message


def _decomposition_panel(flat_sha: str) -> dict[str, Any]:
    """A panel carrying every digest location that binds the factor-decomposition
    root's content artifact bytes: the meta content digest, the surface
    ``factor_analysis.source_sha256``, and the provenance row
    ``portfolio-factor-decomposition-generation`` -- all naming ``flat_sha``."""

    panel = base_panel()
    panel["meta"]["decomposition_sha256"] = flat_sha
    panel["factor_analysis"]["source_sha256"] = flat_sha
    panel["provenance"] = [
        {
            "artifact": "portfolio-factor-decomposition-generation",
            "sha256": flat_sha,
        }
    ]
    return panel


def test_the_factor_decomposition_generation_staged_flat_binds_clean(
    tmp_path: Path,
) -> None:
    """The flat-layout defect: the decomposition root's own generation stages
    its content artifact FLAT as ``<gen>/decomposition.json`` (the layout the
    real 09-23 release carries), while the manifest records the full id. With
    every panel digest location naming the flat bytes' sha256, the
    factor-decomposition target must bind clean -- no findings."""
    write_manifest(tmp_path, GOOD_GEN)
    flat = tmp_path / "generations" / GOOD_GEN / "decomposition.json"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(DECOMPOSITION_ARTIFACT_BYTES)
    flat_sha = _sha(DECOMPOSITION_ARTIFACT_BYTES)
    panel = _decomposition_panel(flat_sha)
    target = make_target("factor-decomposition", tmp_path, GOOD_GEN)

    assert list(rt.target_binding(target, panel, tmp_path)) == []


def test_a_flat_factor_decomposition_digest_mismatch_still_blocks_threefold(
    tmp_path: Path,
) -> None:
    """Fail-closed preserved: flat-staged bytes that do NOT match the panel
    digests must still fire at all three binding locations (content, surface,
    provenance) -- the flat resolution widens the layout, never the binding."""
    write_manifest(tmp_path, GOOD_GEN)
    flat = tmp_path / "generations" / GOOD_GEN / "decomposition.json"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"mismatched-flat-bytes\n")
    actual_sha = _sha(b"mismatched-flat-bytes\n")
    panel = _decomposition_panel(_sha(DECOMPOSITION_ARTIFACT_BYTES))
    target = make_target("factor-decomposition", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert [f.check_id for f in findings] == [
        "release.target_binding",
        "release.target_binding",
        "release.target_binding",
    ]
    assert all(f.klass == BLOCKER_AUTHORITY for f in findings)
    by_path = {f.json_path: f for f in findings}
    # The content and surface findings report the target's actual bytes as
    # observed; the provenance finding reports the panel row as observed
    # (the module's existing convention for provenance rows).
    assert by_path["meta.decomposition_sha256"].observed == actual_sha
    assert by_path["$.factor_analysis.source_sha256"].observed == actual_sha
    assert by_path["$.provenance[0]"].observed == _sha(DECOMPOSITION_ARTIFACT_BYTES)
    assert {f.json_path for f in findings} == {
        "meta.decomposition_sha256",
        "$.factor_analysis.source_sha256",
        "$.provenance[0]",
    }


def test_a_non_flat_root_resolves_its_declared_artifact_id_only(
    tmp_path: Path,
) -> None:
    """The scoping's discriminating fixture (Kimi round-2 on PR 521, retargeted
    by PR 530 once factor-decomposition joined the flat set): a root OUTSIDE
    _FLAT_CONTENT_ROOTS (weekly-returns) resolves its declared artifact id
    only. Its matching bytes staged anywhere else -- the flat-layout candidate
    basename, a nested alternative, a same-basename decoy in another
    generation -- must still block at all three binding locations."""
    assert "weekly-returns" not in rt._FLAT_CONTENT_ROOTS
    weekly_sha = _sha(WEEKLY_ARTIFACT_BYTES)
    panel = base_panel()
    panel["meta"]["weekly_returns_sha256"] = weekly_sha
    panel["weekly_returns"]["source_sha256"] = weekly_sha
    panel["provenance"] = [
        {"artifact": "portfolio-weekly-return-generation", "sha256": weekly_sha}
    ]
    write_manifest(tmp_path, GOOD_GEN)
    generation = tmp_path / "generations" / GOOD_GEN
    for decoy in (
        generation / "decomposition.json",
        generation / "analytics" / "decomposition.json",
        generation / "analytics" / "weekly-returns.json",
        tmp_path / "generations" / PREVIOUS_GEN / "weekly-returns.json",
    ):
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_bytes(WEEKLY_ARTIFACT_BYTES)
    target = make_target("weekly-returns", tmp_path, GOOD_GEN)

    findings = list(rt.target_binding(target, panel, tmp_path))

    assert all(f.check_id == "release.target_binding" for f in findings)
    assert all(f.klass == BLOCKER_AUTHORITY for f in findings)
    by_path = {f.json_path: f for f in findings}
    assert set(by_path) == {
        "meta.weekly_returns_sha256",
        "$.weekly_returns.source_sha256",
        "$.provenance[0]",
    }
    # Nothing was resolved: the content and surface findings observe no bytes,
    # the provenance finding expects none.
    assert by_path["meta.weekly_returns_sha256"].observed is None
    assert by_path["$.weekly_returns.source_sha256"].observed is None
    assert by_path["$.provenance[0]"].expected is None

    # Control: the same panel binds clean once the declared id is staged.
    (generation / "weekly-returns.json").write_bytes(WEEKLY_ARTIFACT_BYTES)
    assert list(rt.target_binding(target, panel, tmp_path)) == []


# ------------------------------------------- provider and epoch roots (REL-PROVIDER-ROOTS-001)


def test_provider_bound_roots_equal_the_materializer_provider_roots() -> None:
    """Seam test: the roots judged by provider_binding are exactly the roots the
    production spec adds beside dataPortfolio, and none is in the panel table."""
    import portfolio_pipeline_materialize as materializer

    assert rt.PROVIDER_BOUND_ROOTS == frozenset(materializer.PROVIDER_ROOTS)
    assert rt.PROVIDER_BOUND_ROOTS.isdisjoint(rt.KNOWN_ROOTS)
    assert rt.PROVIDER_LEDGER_ROOT_ID in rt.KNOWN_ROOTS
    assert rt.PROVIDER_ROOT_PROVIDERS == {
        root_id: provider
        for provider, root_id in materializer.LEDGER_PROVIDER_ROOTS.items()
    }


PROVIDER_CATALOG_BYTES = b'{"catalog": "provider bytes"}\n'


def _provider_target(
    tmp_path: Path, root_id: str = "provider-schwab"
) -> dict[str, Any]:
    write_manifest(tmp_path, GOOD_GEN)
    (tmp_path / "generations" / GOOD_GEN / "schwab-generation.json").write_bytes(
        PROVIDER_CATALOG_BYTES
    )
    return make_target(root_id, tmp_path, GOOD_GEN)


def _ledger(target: dict[str, Any], **changes: Any) -> dict[str, Any]:
    row = {
        "provider_id": "schwab",
        "generation_id": GOOD_GEN,
        "manifest_sha256": target["proposed_manifest_sha256"],
        "catalog_sha256": _sha(PROVIDER_CATALOG_BYTES),
        "qualification": "STAGED_COMPLETE",
    }
    row.update(changes)
    return {"provider_generations": [row]}


def test_a_provider_target_bound_by_the_ledger_has_no_findings(tmp_path: Path) -> None:
    target = _provider_target(tmp_path)
    ledger = _ledger(target)
    assert list(rt.provider_binding(target, tmp_path, base_panel(), ledger)) == []
    assert rt.run_target_checks(target, tmp_path, base_panel(), {}, ledger) == []


def test_a_provider_target_the_ledger_does_not_bind_is_a_blocker(
    tmp_path: Path,
) -> None:
    target = _provider_target(tmp_path)
    for changes, fragment in (
        ({"generation_id": PREVIOUS_GEN}, "different generation"),
        ({"manifest_sha256": "0" * 64}, "different manifest hash"),
        ({"catalog_sha256": "1" * 64}, "no file in the target generation"),
    ):
        findings = list(
            rt.provider_binding(
                target, tmp_path, base_panel(), _ledger(target, **changes)
            )
        )
        assert [f.check_id for f in findings] == ["release.provider_binding"]
        assert findings[0].klass == BLOCKER_AUTHORITY
        assert fragment in findings[0].message


def test_a_provider_target_without_a_ledger_catalog_fails_closed(
    tmp_path: Path,
) -> None:
    target = _provider_target(tmp_path)
    findings = list(rt.provider_binding(target, tmp_path, base_panel(), None))
    assert [f.klass for f in findings] == [BLOCKER_AUTHORITY]
    twice = {"provider_generations": [*_ledger(target)["provider_generations"]] * 2}
    assert (
        "not bound once"
        in list(rt.provider_binding(target, tmp_path, base_panel(), twice))[0].message
    )


def test_a_provider_root_is_not_judged_against_the_panel_table(tmp_path: Path) -> None:
    target = _provider_target(tmp_path)
    assert list(rt.target_binding(target, base_panel(), tmp_path)) == []


EPOCH_ARTIFACT_BYTES = b'{"authority": "epoch bytes"}\n'


def _epoch_target(
    tmp_path: Path, *, on_disk: bytes = EPOCH_ARTIFACT_BYTES
) -> dict[str, Any]:
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "authority/calculation-inputs.json",
                "sha256": _sha(EPOCH_ARTIFACT_BYTES),
            }
        ],
    )
    artifact = (
        tmp_path / "generations" / GOOD_GEN / "authority" / "calculation-inputs.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(on_disk)
    return make_target("epoch-manifest", tmp_path, GOOD_GEN)


def _panel_pinning(sha: str) -> dict[str, Any]:
    panel = base_panel()
    panel["account_manifest"]["calculation_inputs"] = {
        "artifact_id": "authority/calculation-inputs.json",
        "sha256": sha,
    }
    return panel


def test_an_epoch_manifest_target_matching_the_panel_pins_has_no_findings(
    tmp_path: Path,
) -> None:
    target = _epoch_target(tmp_path)
    panel = _panel_pinning(_sha(EPOCH_ARTIFACT_BYTES))
    assert list(rt.provider_binding(target, tmp_path, panel, None)) == []


def test_an_epoch_manifest_target_the_panel_does_not_pin_is_a_blocker(
    tmp_path: Path,
) -> None:
    target = _epoch_target(tmp_path)
    different = list(
        rt.provider_binding(target, tmp_path, _panel_pinning("2" * 64), None)
    )
    assert [f.check_id for f in different] == ["release.provider_binding"]
    unpinned = list(rt.provider_binding(target, tmp_path, base_panel(), None))
    assert "pins no artifact" in unpinned[0].message
    other = tmp_path / "other"
    tampered = _epoch_target(other, on_disk=b"tampered\n")
    findings = list(
        rt.provider_binding(
            tampered, other, _panel_pinning(_sha(EPOCH_ARTIFACT_BYTES)), None
        )
    )
    assert findings and findings[0].observed["bytes"] == _sha(b"tampered\n")


def test_an_epoch_manifest_target_missing_a_panel_pinned_authority_artifact_blocks(
    tmp_path: Path,
) -> None:
    target = _epoch_target(tmp_path)
    panel = _panel_pinning(_sha(EPOCH_ARTIFACT_BYTES))
    panel["account_manifest"]["classified_flows"] = {
        "artifact_id": "authority/classified-flows.json",
        "sha256": "3" * 64,
    }
    findings = list(rt.provider_binding(target, tmp_path, panel, None))
    assert [f.check_id for f in findings] == ["release.provider_binding"]
    assert "does not declare it" in findings[0].message
    assert "authority/classified-flows.json" in findings[0].json_path


def test_a_provider_catalog_the_manifest_declares_binds_without_the_file(
    tmp_path: Path,
) -> None:
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "normalized/portfolio123-generation.json",
                "sha256": _sha(PROVIDER_CATALOG_BYTES),
            }
        ],
    )
    target = make_target("provider-portfolio123", tmp_path, GOOD_GEN)
    ledger = {
        "provider_generations": [
            {
                "provider_id": "portfolio123",
                "generation_id": GOOD_GEN,
                "manifest_sha256": target["proposed_manifest_sha256"],
                "catalog_sha256": _sha(PROVIDER_CATALOG_BYTES),
            }
        ]
    }
    assert list(rt.provider_binding(target, tmp_path, base_panel(), ledger)) == []


def test_a_declared_catalog_whose_present_bytes_differ_is_a_blocker(
    tmp_path: Path,
) -> None:
    write_manifest(
        tmp_path,
        GOOD_GEN,
        artifacts=[
            {
                "artifact_id": "normalized/schwab-generation.json",
                "sha256": _sha(PROVIDER_CATALOG_BYTES),
            }
        ],
    )
    (tmp_path / "generations" / GOOD_GEN / "schwab-generation.json").write_bytes(
        b"swapped catalog\n"
    )
    target = make_target("provider-schwab", tmp_path, GOOD_GEN)
    findings = list(
        rt.provider_binding(target, tmp_path, base_panel(), _ledger(target))
    )
    assert [f.check_id for f in findings] == ["release.provider_binding"]
    assert "contradict" in findings[0].message
