"""Hash-bound acceptance of one frozen Matrixport/BIT extraction run."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import csv
from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    DeclaredExclusion,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


SPEC_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 2
ADAPTER_VERSION = "0.2.1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_REPORTS = {
    "reports/coverage.json",
    "reports/dataset_manifest.csv",
    "reports/export_status.json",
    "reports/extraction_summary.json",
    "reports/release_status.json",
    "reports/request_manifest.jsonl",
    "reports/verification.json",
}


class MatrixportGenerationError(ValueError):
    """The frozen run or its acceptance contract failed closed."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MatrixportGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise MatrixportGenerationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _strict(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise MatrixportGenerationError(
            f"{name} keys differ: missing={sorted(keys - set(value))}, "
            f"unexpected={sorted(set(value) - keys)}"
        )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MatrixportGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MatrixportGenerationError(f"{name} must be a nonnegative integer")
    return value


def _iso_date(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise MatrixportGenerationError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise MatrixportGenerationError(f"{name} must be a canonical ISO date")
    return parsed


def _relative(value: object, name: str) -> str:
    raw = _text(value, name)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise MatrixportGenerationError(f"{name} must be a portable relative path")
    return raw


def _run_root(value: object) -> Path:
    path = Path(_text(value, "run_root"))
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise MatrixportGenerationError("run_root must be a real absolute directory")
    return path.resolve(strict=True)


def _source_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise MatrixportGenerationError("configured source is missing or linked")
    try:
        path.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise MatrixportGenerationError("configured source escapes run_root") from exc
    return path


def _json(path: Path, name: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixportGenerationError(f"{name} is not valid UTF-8 JSON") from exc


def _load_spec(path: Path) -> dict[str, Any]:
    spec = _object(_json(path, "spec"), "spec")
    required_keys = {
        "artifacts",
        "as_of_date",
        "baseline",
        "run_id",
        "run_root",
        "schema_version",
    }
    extra = set(spec) - required_keys - {"declared_exclusions"}
    if extra:
        raise MatrixportGenerationError(
            f"spec contains unexpected keys: {sorted(extra)}"
        )
    missing = required_keys - set(spec)
    if missing:
        raise MatrixportGenerationError(
            f"spec is missing required keys: {sorted(missing)}"
        )
    if spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise MatrixportGenerationError("spec schema version is unsupported")
    _exclusions = spec.get("declared_exclusions")
    if _exclusions is not None:
        if not isinstance(_exclusions, list):
            raise MatrixportGenerationError("declared_exclusions must be an array")
        for idx, entry in enumerate(_exclusions):
            _strict(
                entry, {"item", "reason", "provenance"}, f"declared_exclusions[{idx}]"
            )
            _text(entry.get("item"), f"declared_exclusions[{idx}].item")
            _text(entry.get("reason"), f"declared_exclusions[{idx}].reason")
            _text(entry.get("provenance"), f"declared_exclusions[{idx}].provenance")
    return spec


def _artifacts(
    value: object, *, root: Path, as_of: date, expected_count: int
) -> tuple[dict[str, Path], tuple[SourceArtifact, ...]]:
    paths: dict[str, Path] = {}
    artifacts: list[SourceArtifact] = []
    for record in _objects(value, "artifacts"):
        _strict(
            record,
            {
                "artifact_id",
                "expected_sha256",
                "expected_size_bytes",
                "relative_path",
                "schema_name",
                "schema_version",
            },
            "artifacts[]",
        )
        artifact_id = _relative(record["artifact_id"], "artifact_id")
        if artifact_id in paths:
            raise MatrixportGenerationError("artifact IDs must be unique")
        relative = _relative(record["relative_path"], "relative_path")
        path = _source_path(root, relative)
        actual_hash, actual_size = hash_file(path)
        expected_hash = _text(record["expected_sha256"], "expected_sha256")
        if _SHA256.fullmatch(expected_hash) is None or actual_hash != expected_hash:
            raise MatrixportGenerationError(f"source hash differs: {artifact_id}")
        if actual_size != _integer(
            record["expected_size_bytes"], "expected_size_bytes"
        ):
            raise MatrixportGenerationError(f"source size differs: {artifact_id}")
        paths[artifact_id] = path
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=actual_hash,
                size_bytes=actual_size,
                as_of_date=as_of,
                schema_name=_text(record["schema_name"], "schema_name"),
                schema_version=_text(record["schema_version"], "schema_version"),
            )
        )
    if len(artifacts) != expected_count:
        raise MatrixportGenerationError("artifact count differs")
    if not _REQUIRED_REPORTS <= set(paths):
        raise MatrixportGenerationError("required acceptance reports are missing")
    return paths, tuple(artifacts)


def _dataset_manifest(path: Path) -> list[dict[str, object]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ["dataset", "row_count", "sha256"]:
                raise MatrixportGenerationError("dataset manifest header differs")
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise MatrixportGenerationError("dataset manifest is unreadable") from exc
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for row in rows:
        name = _text(row.get("dataset"), "dataset")
        if name in names or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
            raise MatrixportGenerationError("dataset names must be unique identifiers")
        names.add(name)
        try:
            row_count = int(_text(row.get("row_count"), "row_count"))
        except ValueError as exc:
            raise MatrixportGenerationError("dataset row_count is invalid") from exc
        digest = _text(row.get("sha256"), "dataset.sha256")
        if row_count < 0 or _SHA256.fullmatch(digest) is None:
            raise MatrixportGenerationError("dataset manifest value is invalid")
        result.append({"dataset": name, "row_count": row_count, "sha256": digest})
    return sorted(result, key=lambda item: str(item["dataset"]))


def _coverage_counts(value: object) -> dict[str, int]:
    rows = _objects(value, "coverage")
    counts = Counter(_text(row.get("status"), "coverage.status") for row in rows)
    return dict(sorted(counts.items()))


def _expected_counts(value: object, name: str) -> dict[str, int]:
    raw = _object(value, name)
    return dict(
        sorted((key, _integer(item, f"{name}.{key}")) for key, item in raw.items())
    )


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise MatrixportGenerationError(f"{name} must be an array")
    return sorted(_text(item, f"{name}[]") for item in value)


def _report_contract(
    paths: Mapping[str, Path], *, run_id: str, baseline: Mapping[str, Any]
) -> tuple[list[dict[str, object]], dict[str, int], list[str], date]:
    _strict(
        baseline,
        {
            "artifact_count",
            "coverage_status_counts",
            "dataset_count",
            "known_gaps",
            "request_count",
        },
        "baseline",
    )
    request_count = _integer(baseline["request_count"], "request_count")
    dataset_count = _integer(baseline["dataset_count"], "dataset_count")
    expected_coverage = _expected_counts(
        baseline["coverage_status_counts"], "coverage_status_counts"
    )
    expected_gaps = _strings(baseline["known_gaps"], "known_gaps")

    release = _object(
        _json(paths["reports/release_status.json"], "release_status"), "release_status"
    )
    required_release = {
        "export_status": "COMPLETED",
        "extraction_status": "COMPLETED",
        "status": "PARTIAL",
        "verification_ok": True,
        "xlsx_native_status": "PASS",
        "xlsx_visual_status": "PASS",
    }
    if any(release.get(key) != value for key, value in required_release.items()):
        raise MatrixportGenerationError(
            "release status is not the accepted partial result"
        )

    extraction = _object(
        _json(paths["reports/extraction_summary.json"], "extraction_summary"),
        "extraction_summary",
    )
    if (
        extraction.get("run_id") != run_id
        or extraction.get("status") != "COMPLETED"
        or extraction.get("request_count") != request_count
    ):
        raise MatrixportGenerationError("extraction summary differs")
    run_end_ms = _integer(extraction.get("run_end_ms"), "run_end_ms")
    if run_end_ms == 0:
        raise MatrixportGenerationError("run_end_ms must be positive")
    try:
        observed_boundary = datetime.fromtimestamp(run_end_ms / 1000, tz=UTC).date()
    except (OSError, OverflowError, ValueError) as exc:
        raise MatrixportGenerationError(
            "run_end_ms is outside the supported range"
        ) from exc
    extraction_counts = _expected_counts(
        extraction.get("dataset_counts"), "dataset_counts"
    )
    if len(extraction_counts) != dataset_count:
        raise MatrixportGenerationError("extraction dataset count differs")

    export = _object(
        _json(paths["reports/export_status.json"], "export_status"), "export_status"
    )
    if export != {"dataset_count": dataset_count, "status": "COMPLETED"}:
        raise MatrixportGenerationError("export status differs")

    verification = _object(
        _json(paths["reports/verification.json"], "verification"), "verification"
    )
    if (
        verification.get("ok") is not True
        or verification.get("run_id") != run_id
        or verification.get("status") != "PARTIAL"
    ):
        raise MatrixportGenerationError("verification status differs")
    checks = _object(verification.get("checks"), "verification.checks")
    requests_check = _object(checks.get("requests"), "checks.requests")
    raw_check = _object(checks.get("raw"), "checks.raw")
    if (
        requests_check.get("ok") is not True
        or requests_check.get("checked") != request_count
        or raw_check.get("ok") is not True
        or raw_check.get("checked") != request_count
    ):
        raise MatrixportGenerationError("request/raw verification differs")

    datasets = _dataset_manifest(paths["reports/dataset_manifest.csv"])
    if len(datasets) != dataset_count:
        raise MatrixportGenerationError("dataset manifest count differs")
    row_counts = {
        str(item["dataset"]): _integer(item["row_count"], "dataset.row_count")
        for item in datasets
    }
    digests = {str(item["dataset"]): str(item["sha256"]) for item in datasets}
    if row_counts != extraction_counts:
        raise MatrixportGenerationError("dataset manifest row counts differ")
    parity = _object(checks.get("parity"), "checks.parity")
    if (
        parity.get("ok") is not True
        or parity.get("dataset_count") != dataset_count
        or parity.get("dataset_digests") != digests
    ):
        raise MatrixportGenerationError("dataset parity differs")
    csv_check = _object(checks.get("csv"), "checks.csv")
    expected_csv_counts = {f"{name}.csv": count for name, count in row_counts.items()}
    if (
        csv_check.get("ok") is not True
        or csv_check.get("row_counts") != expected_csv_counts
    ):
        raise MatrixportGenerationError("CSV row-count verification differs")
    sqlite_check = _object(checks.get("sqlite"), "checks.sqlite")
    if (
        sqlite_check.get("ok") is not True
        or sqlite_check.get("dataset_counts") != row_counts
    ):
        raise MatrixportGenerationError("SQLite row-count verification differs")
    for name in row_counts:
        if f"csv/{name}.csv" not in paths:
            raise MatrixportGenerationError(f"CSV artifact is missing: {name}")

    coverage = _coverage_counts(_json(paths["reports/coverage.json"], "coverage"))
    if coverage != expected_coverage:
        raise MatrixportGenerationError("coverage status counts differ")
    coverage_check = _object(checks.get("coverage"), "checks.coverage")
    if (
        coverage_check.get("ok") is not True
        or coverage_check.get("status") not in {"PASS", "PARTIAL"}
        or coverage_check.get("status_counts") != expected_coverage
    ):
        raise MatrixportGenerationError("coverage verification differs")
    valuation = _object(checks.get("valuation"), "checks.valuation")
    if valuation.get("ok") is not True or valuation.get("status") != "PARTIAL":
        raise MatrixportGenerationError("valuation verification differs")
    actual_gaps = _strings(
        coverage_check.get("source_limited_reasons", []),
        "source_limited_reasons",
    ) + _strings(valuation.get("reasons", []), "valuation.reasons")
    actual_gaps = sorted(actual_gaps)
    if actual_gaps != expected_gaps:
        raise MatrixportGenerationError("known gaps differ")

    request_lines = sum(
        1
        for line in paths["reports/request_manifest.jsonl"]
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    )
    if request_lines != request_count:
        raise MatrixportGenerationError("request manifest count differs")
    return datasets, coverage, actual_gaps, observed_boundary


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    """Verify frozen outputs and build deterministic, metadata-only generation zero."""

    spec = _load_spec(spec_path)
    as_of = _iso_date(spec["as_of_date"], "as_of_date")
    root = _run_root(spec["run_root"])
    run_id = _text(spec["run_id"], "run_id")
    if root.name != run_id:
        raise MatrixportGenerationError("run_root name must equal run_id")
    baseline = _object(spec["baseline"], "baseline")
    artifacts, source_artifacts = _artifacts(
        spec["artifacts"],
        root=root,
        as_of=as_of,
        expected_count=_integer(baseline.get("artifact_count"), "artifact_count"),
    )
    datasets, coverage_counts, known_gaps, observed_boundary = _report_contract(
        artifacts, run_id=run_id, baseline=baseline
    )
    if observed_boundary != as_of:
        raise MatrixportGenerationError(
            "as_of_date must equal the extraction run_end_ms UTC date"
        )

    # --- Evaluate declared exclusions with exact set-equality ---
    raw_exclusions: list[dict[str, str]] = spec.get("declared_exclusions", []) or []
    exclusion_items: set[str] = {str(e["item"]) for e in raw_exclusions}
    gap_set: set[str] = set(known_gaps)

    if len(exclusion_items) != len(raw_exclusions):
        raise MatrixportGenerationError(
            "declared_exclusions contain duplicate items; every item must be unique"
        )

    undeclared = gap_set - exclusion_items
    stale = exclusion_items - gap_set

    if raw_exclusions and not undeclared and not stale:
        coverage_status = CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        manifest_known_gaps: tuple[str, ...] = ()
        manifest_undeclared: tuple[str, ...] = ()
        manifest_stale: tuple[str, ...] = ()
    elif raw_exclusions:
        # Drift: produce evidence-bearing PARTIAL manifest with exact diff.
        coverage_status = CoverageStatus.PARTIAL
        manifest_undeclared = tuple(sorted(undeclared))
        manifest_stale = tuple(sorted(stale))
        if known_gaps:
            # Actual gaps present (undeclared and/or mixed) → use them directly.
            manifest_known_gaps = tuple(known_gaps)
        else:
            # Stale-only drift: no current gaps, but stale declarations exist.
            # Encode stale atoms as synthetic gap entries so the PARTIAL
            # contract is satisfied while the stale items remain auditable.
            manifest_known_gaps = tuple(
                f"stale_declaration:{atom}" for atom in sorted(stale)
            )
    else:
        coverage_status = CoverageStatus.PARTIAL
        manifest_known_gaps = tuple(known_gaps)
        manifest_undeclared = ()
        manifest_stale = ()

    declared_exclusions = tuple(
        DeclaredExclusion(
            item=str(e["item"]),
            reason=str(e["reason"]),
            provenance=str(e["provenance"]),
        )
        for e in raw_exclusions
    )

    catalog: dict[str, object] = {
        "aliases": ["BIT", "BIT.com"],
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage_status": coverage_status.value,
        "coverage_status_counts": coverage_counts,
        "datasets": datasets,
        "known_gaps": list(manifest_known_gaps),
        "observed_boundary_date": observed_boundary.isoformat(),
        "provider_id": "matrixport",
        "request_count": baseline["request_count"],
        "source_run_id": run_id,
    }
    if declared_exclusions:
        catalog["declared_exclusions"] = [e.to_dict() for e in declared_exclusions]
    if manifest_undeclared:
        catalog["undeclared_gaps"] = list(manifest_undeclared)
    if manifest_stale:
        catalog["stale_declarations"] = list(manifest_stale)
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_artifact = SourceArtifact(
        artifact_id="normalized/generation-catalog.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=as_of,
        schema_name="matrixport-generation-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(
            name="quant-engine-matrixport", version=ADAPTER_VERSION
        ),
        coverage=CoverageClaim(
            status=coverage_status,
            boundary=(
                "Frozen Matrixport/BIT run through its recorded run_end_ms; "
                "provider lifetime retention is not claimed."
            ),
            fact_types=tuple(str(item["dataset"]) for item in datasets),
            observed_start=observed_boundary,
            observed_end=observed_boundary,
            known_gaps=manifest_known_gaps,
            declared_exclusions=declared_exclusions,
            undeclared_gaps=manifest_undeclared,
            stale_declarations=manifest_stale,
        ),
        artifacts=(*source_artifacts, catalog_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def draft_generation_spec(run_root: Path, as_of_date: date) -> bytes:
    """Build the exact acceptance spec for one completed, verified export."""

    root = run_root.expanduser().resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise MatrixportGenerationError("run_root must be a real directory")
    run_id = root.name
    required = tuple(sorted(_REQUIRED_REPORTS))
    for relative in required:
        _source_path(root, relative)
    extraction = _object(
        _json(root / "reports" / "extraction_summary.json", "extraction_summary"),
        "extraction_summary",
    )
    verification = _object(
        _json(root / "reports" / "verification.json", "verification"),
        "verification",
    )
    checks = _object(verification.get("checks"), "verification.checks")
    coverage_check = _object(checks.get("coverage"), "checks.coverage")
    valuation_check = _object(checks.get("valuation"), "checks.valuation")
    coverage_rows = _json(root / "reports" / "coverage.json", "coverage")
    dataset_rows = _dataset_manifest(root / "reports" / "dataset_manifest.csv")
    artifacts: list[dict[str, object]] = []
    paths = [
        *sorted((root / "csv").glob("*.csv")),
        *(root.joinpath(*PurePosixPath(relative).parts) for relative in required),
    ]
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest, size = hash_file(path)
        if relative.startswith("csv/"):
            schema_name = "matrixport-canonical-csv"
        elif relative.endswith(".csv"):
            schema_name = "matrixport-acceptance-csv"
        elif relative.endswith(".jsonl"):
            schema_name = "matrixport-acceptance-jsonl"
        else:
            schema_name = "matrixport-acceptance-json"
        artifacts.append(
            {
                "artifact_id": relative,
                "expected_sha256": digest,
                "expected_size_bytes": size,
                "relative_path": relative,
                "schema_name": schema_name,
                "schema_version": "1",
            }
        )
    known_gaps = sorted(
        _strings(
            coverage_check.get("source_limited_reasons", []),
            "source_limited_reasons",
        )
        + _strings(valuation_check.get("reasons", []), "valuation.reasons")
    )
    payload = {
        "artifacts": artifacts,
        "as_of_date": as_of_date.isoformat(),
        "baseline": {
            "artifact_count": len(artifacts),
            "coverage_status_counts": _coverage_counts(coverage_rows),
            "dataset_count": len(dataset_rows),
            "known_gaps": known_gaps,
            "request_count": _integer(extraction.get("request_count"), "request_count"),
        },
        "run_id": run_id,
        "run_root": str(root),
        "schema_version": SPEC_SCHEMA_VERSION,
    }
    return canonical_json_bytes(payload)


def stage_generation_candidate(spec_path: Path, output_root: Path) -> dict[str, object]:
    """Create an immutable candidate; never advance CURRENT.json."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "catalog.json"
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise MatrixportGenerationError("existing catalog conflicts with generation")
    if not catalog_path.exists():
        catalog_path.write_bytes(catalog_bytes)
    eligible = (
        manifest.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    )
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "coverage_status": manifest.coverage.status.value,
        "dataset_count": len(catalog["datasets"]),  # type: ignore[arg-type]
        "generation_id": manifest.generation_id,
        "publication_eligible": eligible,
        "published": False,
        "request_count": catalog["request_count"],
    }
