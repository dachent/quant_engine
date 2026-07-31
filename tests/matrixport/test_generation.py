from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.matrixport import (
    MatrixportGenerationError,
    build_generation,
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
            "run_end_ms": 1,
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
    assert first_manifest.verification.value == "VERIFIED"
    assert first["provider_id"] == "matrixport"
    assert first["aliases"] == ["BIT", "BIT.com"]

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
