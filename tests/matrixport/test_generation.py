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
    run = tmp_path / "synthetic-run"
    reports = run / "reports"
    csv_root = run / "csv"
    reports.mkdir(parents=True)
    csv_root.mkdir()
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
    source_gap = "/synthetic: one source-limited request"
    valuation_gap = "synthetic valuation coverage is incomplete"
    _write_json(
        reports / "verification.json",
        {
            "checks": {
                "coverage": {
                    "ok": True,
                    "source_limited_reasons": [source_gap],
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
                    "reasons": [valuation_gap],
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
            "known_gaps": [source_gap, valuation_gap],
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
    document["baseline"]["known_gaps"] = ["different gap"]
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
    extra_gap: str | None = None,
) -> Path:
    """Build a spec identical to ``_spec`` but with optional declared_exclusions
    and an optional extra gap not covered by any exclusion."""
    spec_path = _spec(tmp_path)
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    source_gap = document["baseline"]["known_gaps"][0]
    valuation_gap = document["baseline"]["known_gaps"][1]
    if exclusions is not None:
        document["declared_exclusions"] = exclusions
    if extra_gap is not None:
        run = Path(document["run_root"])
        verification = json.loads(
            (run / "reports" / "verification.json").read_text(encoding="utf-8")
        )
        verification["checks"]["coverage"]["source_limited_reasons"].append(extra_gap)
        _write_json(run / "reports" / "verification.json", verification)
        document["baseline"]["known_gaps"] = [source_gap, valuation_gap, extra_gap]
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


def test_declared_exclusions_flip_coverage_to_complete(tmp_path: Path) -> None:
    """Both gap classes covered by declared exclusions -> COMPLETE_WITHIN_DECLARED_BOUNDARY."""
    source_gap = "/synthetic: one source-limited request"
    valuation_gap = "synthetic valuation coverage is incomplete"
    exclusions = [
        {
            "item": source_gap,
            "reason": "accepted zero-balance defunct tokens — API code 18100410",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
        {
            "item": valuation_gap,
            "reason": "provider endpoint missing NAV for two dates",
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


def test_undeclared_gap_keeps_coverage_partial(tmp_path: Path) -> None:
    """A gap not covered by any declared exclusion keeps coverage PARTIAL."""
    source_gap = "/synthetic: one source-limited request"
    exclusions = [
        {
            "item": source_gap,
            "reason": "accepted",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
    ]
    # The valuation gap is NOT declared, so coverage should stay PARTIAL.
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    with pytest.raises(
        MatrixportGenerationError,
        match="declared_exclusions do not cover all gaps",
    ):
        build_generation(spec)


def test_no_exclusions_keeps_partial(tmp_path: Path) -> None:
    """Spec without declared_exclusions continues to produce PARTIAL (backward compat)."""
    spec = _spec(tmp_path)
    catalog, manifest, _ = build_generation(spec)
    assert manifest.coverage.status is CoverageStatus.PARTIAL
    assert manifest.coverage.known_gaps != ()
    assert manifest.coverage.declared_exclusions == ()


def test_exclusion_records_land_verbatim_in_manifest(tmp_path: Path) -> None:
    """Declared exclusion item, reason, and provenance are preserved in the manifest."""
    gap = "/synthetic: one source-limited request"
    valuation_gap = "synthetic valuation coverage is incomplete"
    exclusions = [
        {
            "item": gap,
            "reason": "API rejects 15 defunct legacy currency symbols (code 18100410)",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
        {
            "item": valuation_gap,
            "reason": "NAV missing for 2026-01-27, 2026-07-03 on provider endpoint",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
    ]
    spec = _spec_with_exclusions(tmp_path, exclusions=exclusions)
    _, manifest, _ = build_generation(spec)

    assert len(manifest.coverage.declared_exclusions) == 2
    exclusion_items = {e.item for e in manifest.coverage.declared_exclusions}
    assert gap in exclusion_items
    assert valuation_gap in exclusion_items

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
    """publish_generation accepts a generation that is COMPLETE_WITHIN_DECLARED_BOUNDARY."""
    source_gap = "/synthetic: one source-limited request"
    valuation_gap = "synthetic valuation coverage is incomplete"
    exclusions = [
        {
            "item": source_gap,
            "reason": "accepted",
            "provenance": "owner-accepted 2026-08-14 per issue #287",
        },
        {
            "item": valuation_gap,
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
        item="TEST-TOKEN",
        reason="API code 18100410 Invalid params",
        provenance="issue #287 2026-08-14",
    )
    serialized = original.to_dict()
    assert serialized == {
        "item": "TEST-TOKEN",
        "reason": "API code 18100410 Invalid params",
        "provenance": "issue #287 2026-08-14",
    }
    restored = DeclaredExclusion.from_dict(serialized)
    assert restored == original
    assert restored.item == "TEST-TOKEN"
    assert restored.reason == "API code 18100410 Invalid params"
    assert restored.provenance == "issue #287 2026-08-14"


def test_coverage_claim_accepts_declared_exclusions(tmp_path: Path) -> None:
    """CoverageClaim with COMPLETE + declared_exclusions is valid."""
    from quant_engine.core import CoverageClaim

    claim = CoverageClaim(
        status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
        boundary="synthetic",
        fact_types=("alpha",),
        declared_exclusions=(
            DeclaredExclusion(
                item="GAP-1",
                reason="accepted",
                provenance="issue #287",
            ),
        ),
    )
    assert claim.declared_exclusions[0].item == "GAP-1"
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
                item="GAP-CURRENCY",
                reason="API rejects 15 symbols",
                provenance="issue #287",
            ),
            DeclaredExclusion(
                item="GAP-NAV",
                reason="NAV missing for 2 dates",
                provenance="issue #287",
            ),
        ),
    )
    serialized = claim.to_dict()
    reloaded = CoverageClaim.from_dict(serialized)
    assert reloaded == claim
    assert len(reloaded.declared_exclusions) == 2
