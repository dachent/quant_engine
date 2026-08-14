from __future__ import annotations

import csv
from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.core import (
    CoverageStatus,
    DeclaredExclusion,
    ManifestValidationError,
    PublicationRejectedError,
    publish_generation,
)
from quant_engine.matrixport import (
    MatrixportGenerationError,
    build_generation,
    draft_generation_spec,
    stage_generation_candidate,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _artifact(path: Path, root: Path) -> dict[str, object]:
    payload = path.read_bytes()
    relative = path.relative_to(root).as_posix()
    suffix = "jsonl" if relative.endswith(".jsonl") else path.suffix.lstrip(".")
    return {
        "artifact_id": relative,
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "relative_path": relative,
        "schema_name": f"synthetic-{suffix}",
        "schema_version": "1",
    }


def _spec(tmp_path: Path) -> Path:
    """Build a synthetic PARTIAL spec with atomic gap items."""
    run = tmp_path / "synthetic-run"
    reports = run / "reports"
    csv_root = run / "csv"
    reports.mkdir(parents=True, exist_ok=True)
    csv_root.mkdir(exist_ok=True)
    dataset_rows = {"alpha": 1, "beta": 2}
    dataset_digests = {"alpha": "a" * 64, "beta": "b" * 64}
    for name, count in dataset_rows.items():
        path = csv_root / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["value"])
            writer.writerows([[index] for index in range(count)])

    _write_json(
        reports / "release_status.json",
        {
            "export_status": "COMPLETED",
            "extraction_status": "COMPLETED",
            "status": "PARTIAL",
            "verification_ok": True,
            "xlsx_native_status": "PASS",
            "xlsx_visual_status": "PASS",
        },
    )
    _write_json(
        reports / "extraction_summary.json",
        {
            "dataset_counts": dataset_rows,
            "mode": "full",
            "request_count": 2,
            "run_end_ms": int(
                datetime(2026, 7, 26, 23, 59, tzinfo=UTC).timestamp() * 1000
            ),
            "run_id": run.name,
            "status": "COMPLETED",
        },
    )
    _write_json(
        reports / "export_status.json",
        {"dataset_count": 2, "status": "COMPLETED"},
    )
    coverage = [
        {
            "endpoint": "GET https://example.invalid/alpha",
            "status": "complete_to_observed_boundary",
        },
        {"endpoint": "GET https://example.invalid/beta", "status": "partial"},
    ]
    _write_json(reports / "coverage.json", coverage)
    with (reports / "dataset_manifest.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=["dataset", "row_count", "sha256"])
        writer.writeheader()
        for name in sorted(dataset_rows):
            writer.writerow(
                {
                    "dataset": name,
                    "row_count": dataset_rows[name],
                    "sha256": dataset_digests[name],
                }
            )
    (reports / "request_manifest.jsonl").write_text(
        '{"request":1}\n{"request":2}\n', encoding="utf-8"
    )
    # --- Atomic gap items ---
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom = "nav_missing_date:2026-01-27"
    _write_json(
        reports / "verification.json",
        {
            "checks": {
                "coverage": {
                    "ok": True,
                    "source_limited_reasons": [source_atom],
                    "status": "PARTIAL",
                    "status_counts": {
                        "complete_to_observed_boundary": 1,
                        "partial": 1,
                    },
                },
                "csv": {
                    "ok": True,
                    "row_counts": {"alpha.csv": 1, "beta.csv": 2},
                },
                "parity": {
                    "dataset_count": 2,
                    "dataset_digests": dataset_digests,
                    "ok": True,
                },
                "raw": {"checked": 2, "ok": True},
                "requests": {"checked": 2, "ok": True},
                "sqlite": {"dataset_counts": dataset_rows, "ok": True},
                "valuation": {
                    "ok": True,
                    "reasons": [nav_atom],
                    "status": "PARTIAL",
                },
            },
            "ok": True,
            "run_id": run.name,
            "status": "PARTIAL",
        },
    )
    paths = sorted(
        [*reports.iterdir(), *csv_root.iterdir()], key=lambda item: item.as_posix()
    )
    spec = {
        "artifacts": [_artifact(path, run) for path in paths],
        "as_of_date": "2026-07-26",
        "baseline": {
            "artifact_count": len(paths),
            "coverage_status_counts": {
                "complete_to_observed_boundary": 1,
                "partial": 1,
            },
            "dataset_count": 2,
            "known_gaps": [source_atom, nav_atom],
            "request_count": 2,
        },
        "run_id": run.name,
        "run_root": str(run.resolve()),
        "schema_version": 1,
    }
    path = tmp_path / "spec.json"
    _write_json(path, spec)
    return path


def test_partial_generation_is_deterministic_and_never_published(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    first, first_manifest, first_bytes = build_generation(spec)
    second, second_manifest, second_bytes = build_generation(spec)
    assert first == second
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first_manifest.coverage.status.value == "PARTIAL"
    assert first_manifest.coverage.observed_start == date(2026, 7, 26)
    assert first_manifest.coverage.observed_end == date(2026, 7, 26)
    assert first_manifest.verification.value == "VERIFIED"
    assert first["provider_id"] == "matrixport"
    assert first["aliases"] == ["BIT", "BIT.com"]
    assert first["catalog_schema_version"] == 2
    assert first["observed_boundary_date"] == "2026-07-26"

    root = tmp_path / "dataMatrixport"
    staged = stage_generation_candidate(spec, root)
    assert stage_generation_candidate(spec, root) == staged
    assert staged["publication_eligible"] is False
    assert staged["published"] is False
    assert not (root / "CURRENT.json").exists()


def test_hash_coverage_and_gap_drift_fail_closed(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    run = Path(document["run_root"])
    (run / "csv" / "alpha.csv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(MatrixportGenerationError, match="source hash differs"):
        build_generation(spec)

    spec = _spec(tmp_path / "coverage")
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["baseline"]["known_gaps"] = ["different atom"]
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(MatrixportGenerationError, match="known gaps differ"):
        build_generation(spec)


def test_verified_export_can_draft_its_hash_bound_spec(tmp_path: Path) -> None:
    original = _spec(tmp_path)
    run_root = Path(json.loads(original.read_text(encoding="utf-8"))["run_root"])
    drafted = tmp_path / "drafted.json"
    drafted.write_bytes(draft_generation_spec(run_root, date(2026, 7, 26)))

    catalog, manifest, _ = build_generation(drafted)

    assert catalog["request_count"] == 2
    assert len(catalog["datasets"]) == 2
    assert manifest.as_of_date == date(2026, 7, 26)


def test_as_of_date_must_match_extraction_boundary(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["as_of_date"] = "2026-07-27"
    spec.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        MatrixportGenerationError,
        match="as_of_date must equal the extraction run_end_ms UTC date",
    ):
        build_generation(spec)


# ---------------------------------------------------------------------------
# Declared-exclusion helpers and tests
# ---------------------------------------------------------------------------


def _spec_with_exclusions(
    tmp_path: Path,
    *,
    exclusions: list[dict[str, str]] | None = None,
    extra_gaps: list[str] | None = None,
    remove_gaps: list[str] | None = None,
) -> Path:
    """Build a spec with optional declared_exclusions and gap mutations.

    ``extra_gaps`` are added to both verification.json and known_gaps.
    ``remove_gaps`` are removed from both.
    """
    spec_path = _spec(tmp_path)
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    run = Path(document["run_root"])

    if exclusions is not None:
        document["declared_exclusions"] = exclusions

    if extra_gaps or remove_gaps:
        verification = json.loads(
            (run / "reports" / "verification.json").read_text(encoding="utf-8")
        )
        remove_set = set(remove_gaps or [])
        source_gaps = [
            g
            for g in verification["checks"]["coverage"]["source_limited_reasons"]
            if g not in remove_set
        ]
        val_gaps = [
            g
            for g in verification["checks"]["valuation"]["reasons"]
            if g not in remove_set
        ]
        for gap in extra_gaps or []:
            if gap.startswith("nav_missing_date:"):
                val_gaps.append(gap)
            else:
                source_gaps.append(gap)
        verification["checks"]["coverage"]["source_limited_reasons"] = source_gaps
        verification["checks"]["valuation"]["reasons"] = val_gaps
        _write_json(run / "reports" / "verification.json", verification)

        current_gaps = set(document["baseline"]["known_gaps"])
        current_gaps -= remove_set
        current_gaps |= set(extra_gaps or [])
        document["baseline"]["known_gaps"] = sorted(current_gaps)

        # Re-hash the changed verification report
        for artifact in document["artifacts"]:
            if artifact["artifact_id"] == "reports/verification.json":
                vpath = run / "reports" / "verification.json"
                payload = vpath.read_bytes()
                artifact["expected_sha256"] = hashlib.sha256(payload).hexdigest()
                artifact["expected_size_bytes"] = len(payload)
                break

    spec_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return spec_path


# --- Test (d): Full owner-accepted atomic declaration → COMPLETE ---


def test_owner_accepted_full_atomic_declaration_is_complete(
    tmp_path: Path,
) -> None:
    """All gaps covered by exact atomic declared exclusions → COMPLETE."""
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom = "nav_missing_date:2026-01-27"
    exclusions = [
        {
            "item": source_atom,
            "reason": "accepted — API code 18100410",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
        {
            "item": nav_atom,
            "reason": "provider missing NAV for this date",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
    ]
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    catalog, manifest, _ = build_generation(spec)

    assert manifest.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    assert catalog["coverage_status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert catalog["known_gaps"] == []
    assert len(manifest.coverage.declared_exclusions) == 2
    assert manifest.coverage.known_gaps == ()

    # Verify each atom is recorded verbatim in the manifest
    items = {e.item for e in manifest.coverage.declared_exclusions}
    assert source_atom in items
    assert nav_atom in items


# --- Test (a): New undeclared NAV date → PARTIAL + publish rejected ---


def test_new_undeclared_nav_date_keeps_partial(
    tmp_path: Path,
) -> None:
    """A second missing NAV date not covered by any exclusion → PARTIAL manifest
    naming the exact undeclared atom, publish rejected via real path."""
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom_1 = "nav_missing_date:2026-01-27"
    nav_atom_2 = "nav_missing_date:2026-08-14"  # NEW, not declared
    exclusions = [
        {"item": source_atom, "reason": "accepted", "provenance": "issue #287"},
        {"item": nav_atom_1, "reason": "accepted", "provenance": "issue #287"},
    ]
    spec = _spec_with_exclusions(
        tmp_path,
        exclusions=exclusions,
        extra_gaps=[nav_atom_2],
    )
    catalog, manifest, _ = build_generation(spec)

    # Evidence-bearing PARTIAL manifest
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    assert catalog["coverage_status"] == "PARTIAL"
    assert nav_atom_2 in manifest.coverage.undeclared_gaps
    assert nav_atom_2 in manifest.coverage.known_gaps
    assert manifest.coverage.stale_declarations == ()

    # Publish is rejected via the real publish path
    root = tmp_path / "dataMatrixport"
    staged = stage_generation_candidate(spec, root)
    assert staged["publication_eligible"] is False
    with pytest.raises(
        PublicationRejectedError, match="generation coverage is not complete"
    ):
        publish_generation(root, staged["generation_id"])


# --- Test (b): Substituted wallet currency same count → PARTIAL ---


def test_substituted_wallet_currency_same_count_keeps_partial(
    tmp_path: Path,
) -> None:
    """A different rejected currency with the same count → PARTIAL manifest
    naming the exact undeclared atom."""
    source_atom_original = "wallet_deposit_currency:synthetic-rejected-A"
    source_atom_substituted = "wallet_deposit_currency:synthetic-rejected-B"
    nav_atom = "nav_missing_date:2026-01-27"
    exclusions = [
        {
            "item": source_atom_original,
            "reason": "accepted",
            "provenance": "issue #287",
        },
        {"item": nav_atom, "reason": "accepted", "provenance": "issue #287"},
    ]
    spec = _spec_with_exclusions(
        tmp_path,
        exclusions=exclusions,
        extra_gaps=[source_atom_substituted],
        remove_gaps=[source_atom_original],
    )
    catalog, manifest, _ = build_generation(spec)

    assert manifest.coverage.status is CoverageStatus.PARTIAL
    assert source_atom_substituted in manifest.coverage.undeclared_gaps
    assert source_atom_substituted in manifest.coverage.known_gaps
    # The original declared atom is stale
    assert source_atom_original in manifest.coverage.stale_declarations


# --- Test (c): Stale declaration → PARTIAL naming the stale atom ---


def test_stale_declaration_produces_partial_manifest(tmp_path: Path) -> None:
    """A declared exclusion not matching any current gap → PARTIAL manifest
    naming the exact stale atom."""
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom = "nav_missing_date:2026-01-27"
    stale_atom = "wallet_deposit_currency:synthetic-LEGACY-TOKEN"
    exclusions = [
        {"item": source_atom, "reason": "accepted", "provenance": "issue #287"},
        {"item": nav_atom, "reason": "accepted", "provenance": "issue #287"},
        {
            "item": stale_atom,
            "reason": "stale — upstream fixed this",
            "provenance": "issue #287",
        },
    ]
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    catalog, manifest, _ = build_generation(spec)

    assert manifest.coverage.status is CoverageStatus.PARTIAL
    assert stale_atom in manifest.coverage.stale_declarations
    assert manifest.coverage.undeclared_gaps == ()
    # The declared stale item is still recorded in declared_exclusions
    declared_items = {e.item for e in manifest.coverage.declared_exclusions}
    assert stale_atom in declared_items


# --- Test (e): Aggregate-string matching is gone ---


def test_aggregate_string_matching_is_gone(tmp_path: Path) -> None:
    """Prove different symbols → PARTIAL manifest with undeclared + stale diff."""
    source_atom_a = "wallet_deposit_currency:synthetic-rejected-A"
    source_atom_b = "wallet_deposit_currency:synthetic-rejected-B"
    nav_atom = "nav_missing_date:2026-01-27"

    exclusions = [
        {"item": source_atom_a, "reason": "only symbol A", "provenance": "issue #287"},
        {"item": nav_atom, "reason": "accepted", "provenance": "issue #287"},
    ]
    spec = _spec_with_exclusions(
        tmp_path,
        exclusions=exclusions,
        extra_gaps=[source_atom_b],
        remove_gaps=[source_atom_a],
    )
    catalog, manifest, _ = build_generation(spec)

    assert manifest.coverage.status is CoverageStatus.PARTIAL
    assert source_atom_b in manifest.coverage.undeclared_gaps
    assert source_atom_a in manifest.coverage.stale_declarations


# --- Duplicate exclusion items rejected ---


def test_duplicate_exclusion_items_rejected(tmp_path: Path) -> None:
    """Duplicate declared_exclusion items are rejected at build time."""
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom = "nav_missing_date:2026-01-27"
    exclusions = [
        {
            "item": source_atom,
            "reason": "first copy",
            "provenance": "issue #287",
        },
        {
            "item": source_atom,  # DUPLICATE
            "reason": "second copy",
            "provenance": "issue #287",
        },
        {
            "item": nav_atom,
            "reason": "accepted",
            "provenance": "issue #287",
        },
    ]
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    with pytest.raises(
        MatrixportGenerationError,
        match="declared_exclusions contain duplicate items",
    ):
        build_generation(spec)


# --- Manifest verbatim recording ---


def test_exclusion_records_land_verbatim_in_manifest(tmp_path: Path) -> None:
    """Declared exclusion item, reason, and provenance preserved in manifest."""
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom = "nav_missing_date:2026-01-27"
    exclusions = [
        {
            "item": source_atom,
            "reason": "API rejects 15 defunct legacy currency symbols (code 18100410)",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
        {
            "item": nav_atom,
            "reason": "NAV missing for 2026-01-27 on provider endpoint",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
    ]
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    _, manifest, _ = build_generation(spec)

    assert len(manifest.coverage.declared_exclusions) == 2
    exclusion_items = {e.item for e in manifest.coverage.declared_exclusions}
    assert source_atom in exclusion_items
    assert nav_atom in exclusion_items

    for exclusion in manifest.coverage.declared_exclusions:
        assert "18100410" in exclusion.reason or "NAV missing" in exclusion.reason
        assert "issue #287" in exclusion.provenance

    # Round-trip through serialization
    manifest_json = manifest.to_json_bytes()
    reloaded = type(manifest).from_json_bytes(manifest_json)
    assert (
        reloaded.coverage.declared_exclusions == manifest.coverage.declared_exclusions
    )
    assert reloaded.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY


# --- Backward compat ---


def test_no_exclusions_keeps_partial(tmp_path: Path) -> None:
    """Spec without declared_exclusions continues to produce PARTIAL."""
    spec = _spec(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    assert manifest.coverage.known_gaps != ()
    assert manifest.coverage.declared_exclusions == ()


def test_publish_rejected_for_partial_coverage(tmp_path: Path) -> None:
    """publish_generation rejects a generation with PARTIAL coverage."""
    spec = _spec(tmp_path)
    root = tmp_path / "dataMatrixport"
    staged = stage_generation_candidate(spec, root)
    assert staged["publication_eligible"] is False
    with pytest.raises(
        PublicationRejectedError, match="generation coverage is not complete"
    ):
        publish_generation(root, staged["generation_id"])


def test_publish_succeeds_for_complete_within_declared_boundary(
    tmp_path: Path,
) -> None:
    """publish_generation accepts COMPLETE_WITHIN_DECLARED_BOUNDARY generation."""
    source_atom = "wallet_deposit_currency:synthetic-rejected-A"
    nav_atom = "nav_missing_date:2026-01-27"
    exclusions = [
        {
            "item": source_atom,
            "reason": "accepted",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
        {
            "item": nav_atom,
            "reason": "accepted",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
    ]
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    root = tmp_path / "dataMatrixport"
    staged = stage_generation_candidate(spec, root)
    assert staged["publication_eligible"] is True

    pointer = publish_generation(root, staged["generation_id"])
    assert pointer.generation_id == staged["generation_id"]

    current_path = root / "CURRENT.json"
    assert current_path.exists()
    assert json.loads(current_path.read_text(encoding="utf-8"))["generation_id"] == str(
        staged["generation_id"]
    )


# ---------------------------------------------------------------------------
# DeclaredExclusion contract tests
# ---------------------------------------------------------------------------


def test_declared_exclusion_validation() -> None:
    """DeclaredExclusion enforces non-empty fields."""
    with pytest.raises(ManifestValidationError, match="must not be empty"):
        DeclaredExclusion(item="", reason="r", provenance="p")
    with pytest.raises(ManifestValidationError, match="must not be empty"):
        DeclaredExclusion(item="x", reason="", provenance="p")
    with pytest.raises(ManifestValidationError, match="must not be empty"):
        DeclaredExclusion(item="x", reason="r", provenance="")


def test_declared_exclusion_round_trip() -> None:
    """DeclaredExclusion to_dict/from_dict preserves all fields."""
    original = DeclaredExclusion(
        item="wallet_deposit_currency:FTX-BTC-1",
        reason="API code 18100410 Invalid params",
        provenance="issue #287 2026-08-14",
    )
    serialized = original.to_dict()
    assert serialized == {
        "item": "wallet_deposit_currency:FTX-BTC-1",
        "reason": "API code 18100410 Invalid params",
        "provenance": "issue #287 2026-08-14",
    }
    restored = DeclaredExclusion.from_dict(serialized)
    assert restored == original


def test_coverage_claim_accepts_declared_exclusions() -> None:
    """CoverageClaim with COMPLETE + declared_exclusions is valid."""
    from quant_engine.core import CoverageClaim

    claim = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="synthetic",
        fact_types=("alpha",),
        declared_exclusions=(
            DeclaredExclusion(
                item="wallet_deposit_currency:TOKEN",
                reason="accepted",
                provenance="issue #287",
            ),
        ),
    )
    assert claim.declared_exclusions[0].item == "wallet_deposit_currency:TOKEN"
    assert claim.known_gaps == ()


def test_coverage_claim_round_trip_with_exclusions() -> None:
    """CoverageClaim serialization preserves declared_exclusions."""
    from quant_engine.core import CoverageClaim

    claim = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="synthetic",
        fact_types=("alpha", "beta"),
        observed_start=date(2026, 7, 26),
        observed_end=date(2026, 7, 26),
        declared_exclusions=(
            DeclaredExclusion(
                item="wallet_deposit_currency:FTX-BTC-1",
                reason="API rejects 15 symbols",
                provenance="issue #287",
            ),
            DeclaredExclusion(
                item="nav_missing_date:2026-01-27",
                reason="NAV missing",
                provenance="issue #287",
            ),
        ),
    )
    serialized = claim.to_dict()
    reloaded = CoverageClaim.from_dict(serialized)
    assert reloaded == claim
    assert len(reloaded.declared_exclusions) == 2
