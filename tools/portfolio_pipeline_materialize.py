"""Build one governed source-to-panel pipeline from immutable local inputs."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from quant_engine.core import (
    CoverageStatus,
    GenerationManifest,
    VerificationStatus,
    inventory_generation,
    runtime_environment_sha256,
)
from quant_engine.core.contracts import canonical_json_bytes


class MaterializationError(ValueError):
    """A private recipe, rebuilt stage, or semantic bridge differs."""


STAGES = (
    "ledger",
    "baseline",
    "attribution",
    "analytics",
    "report",
    "workbook",
    "panel",
)
RELEASE_ROLES = ("ledger", "baseline", "report", "panel")
ROOT_IDS = {
    "ledger": "provider-ledger",
    "baseline": "baseline",
    "report": "reporting",
    "panel": "control-panel",
}
GENERATION_STAGE = {
    "ledger": "ledger",
    "baseline": "baseline",
    "report": "workbook",
    "panel": "panel",
}
GENERATION_ROOT = {
    "ledger": "ledger",
    "baseline": "baseline",
    "report": "report",
    "panel": "panel",
}
WINDOWS_SAFE_FILE_PATH = 240
WEIGHT_SUM_TOLERANCE = Decimal("0.000000000001")


def _load(path: Path, label: str = "JSON file") -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MaterializationError(f"{label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise MaterializationError(f"{label} root must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaterializationError(f"{label} must be non-empty text")
    return value.strip()


def _absolute(value: object, label: str) -> Path:
    path = Path(_text(value, label)).expanduser().resolve()
    if not path.is_absolute():
        raise MaterializationError(f"{label} must be absolute")
    return path


def _sha(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise MaterializationError(f"regular file is missing: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise MaterializationError(f"create-only output differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _directory_rows(path: Path) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_dir():
        raise MaterializationError(f"source directory is missing: {path}")
    rows: list[dict[str, object]] = []
    for item in sorted(
        candidate for candidate in path.rglob("*") if candidate.is_file()
    ):
        if item.is_symlink():
            raise MaterializationError(f"source directory contains a link: {item}")
        sha256, size = _sha(item)
        rows.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": sha256,
                "size_bytes": size,
            }
        )
    return rows


def _directory_identity(path: Path) -> dict[str, object]:
    rows = _directory_rows(path)
    encoded = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "inventory_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _file_definition(path: Path) -> dict[str, object]:
    sha256, size = _sha(path)
    return {"path": str(path.resolve()), "sha256": sha256, "size_bytes": size}


def _directory_definition(path: Path) -> dict[str, object]:
    return {"path": str(path.resolve()), **_directory_identity(path)}


def _validate_file_definition(value: object, label: str) -> Path:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size_bytes"}:
        raise MaterializationError(f"{label} definition differs")
    path = _absolute(value["path"], f"{label}.path")
    sha256, size = _sha(path)
    if sha256 != value["sha256"] or size != value["size_bytes"]:
        raise MaterializationError(f"{label} identity differs")
    return path


def _validate_directory_definition(value: object, label: str) -> Path:
    if not isinstance(value, dict) or set(value) != {
        "file_count",
        "inventory_sha256",
        "path",
    }:
        raise MaterializationError(f"{label} definition differs")
    path = _absolute(value["path"], f"{label}.path")
    actual = _directory_identity(path)
    expected = {
        "file_count": value["file_count"],
        "inventory_sha256": value["inventory_sha256"],
    }
    if actual != expected:
        raise MaterializationError(f"{label} inventory differs")
    return path


def _generation_expectation(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "files",
        "generation_id",
        "manifest_sha256",
    }:
        raise MaterializationError(f"{role} generation expectation differs")
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise MaterializationError(f"{role} generation inventory is empty")
    for row in files:
        if not isinstance(row, dict) or set(row) != {
            "relative_path",
            "sha256",
            "size_bytes",
        }:
            raise MaterializationError(f"{role} generation inventory differs")
    generation_id = _text(value["generation_id"], f"{role} generation_id")
    manifest_sha256 = _text(value["manifest_sha256"], f"{role} manifest_sha256")
    if len(generation_id) != 64 or len(manifest_sha256) != 64:
        raise MaterializationError(f"{role} generation hashes differ")
    return dict(value)


def _recipe(path: Path) -> dict[str, Any]:
    recipe = _load(path, "pipeline recipe")
    required = {
        "as_of_date",
        "expected_generations",
        "panel_launcher_sha256",
        "pipeline_id",
        "qa_dir",
        "release_commit",
        "schema_version",
        "vertical_recipe",
    }
    if recipe.get("schema_version") != 2 or set(recipe) != required:
        raise MaterializationError("portfolio pipeline recipe differs")
    _text(recipe["as_of_date"], "as_of_date")
    _text(recipe["pipeline_id"], "pipeline_id")
    if len(_text(recipe["release_commit"], "release_commit")) != 40:
        raise MaterializationError("release_commit differs")
    if len(_text(recipe["panel_launcher_sha256"], "panel_launcher_sha256")) != 64:
        raise MaterializationError("panel launcher hash differs")
    vertical_path = _validate_file_definition(
        recipe["vertical_recipe"], "vertical recipe"
    )
    vertical = _load(vertical_path, "vertical recipe")
    if (
        vertical.get("schema_version") != 3
        or vertical.get("release_commit") != recipe["release_commit"]
        or vertical.get("as_of_date") != recipe["as_of_date"]
    ):
        raise MaterializationError("vertical recipe release identity differs")
    _validate_directory_definition(recipe["qa_dir"], "panel QA")
    expected = recipe["expected_generations"]
    if not isinstance(expected, dict) or set(expected) != set(RELEASE_ROLES):
        raise MaterializationError("expected release generations differ")
    for role in RELEASE_ROLES:
        _generation_expectation(expected[role], role)
    return recipe


def _vertical_source_paths(vertical_path: Path) -> list[Path]:
    vertical = _load(vertical_path, "vertical recipe")
    paths = [vertical_path]
    for key in (
        "provider_ledger_spec",
        "baseline_spec",
        "base_factor_file",
        "legacy_panel",
    ):
        value = vertical.get(key)
        if not isinstance(value, dict):
            raise MaterializationError(f"vertical recipe {key} differs")
        paths.append(_absolute(value.get("path"), f"vertical recipe {key}.path"))
    for key in ("decomposition_plan_bundle", "correlation_source_root"):
        value = vertical.get(key)
        if not isinstance(value, dict):
            raise MaterializationError(f"vertical recipe {key} differs")
        paths.append(_absolute(value.get("path"), f"vertical recipe {key}.path"))
    return paths


def _generation_dir(output_root: Path, recipe: Mapping[str, Any], role: str) -> Path:
    generation_id = str(recipe["expected_generations"][role]["generation_id"])
    return (
        output_root / "stages" / GENERATION_ROOT[role] / "generations" / generation_id
    )


def create_recipe(
    vertical_recipe: Path,
    proof_root: Path,
    qa_dir: Path,
    pipeline_id: str,
    output: Path,
) -> dict[str, Any]:
    vertical = _load(vertical_recipe.resolve(), "vertical recipe")
    if vertical.get("schema_version") != 3:
        raise MaterializationError("vertical recipe schema differs")
    expected: dict[str, object] = {}
    for role in RELEASE_ROLES:
        stage_result = _load(
            proof_root / "stage-results" / f"{GENERATION_STAGE[role]}.json",
            f"proof {role} stage result",
        )
        generation_id = _text(
            stage_result.get("generation_id"), f"proof {role} generation"
        )
        generation = (
            proof_root
            / "stages"
            / GENERATION_ROOT[role]
            / "generations"
            / generation_id
        )
        inventory = inventory_generation(generation)
        expected[role] = {
            "files": inventory["files"],
            "generation_id": inventory["generation_id"],
            "manifest_sha256": inventory["manifest_sha256"],
        }
    launcher = proof_root / "stages" / "panel" / "index.html"
    payload = {
        "as_of_date": vertical["as_of_date"],
        "expected_generations": expected,
        "panel_launcher_sha256": _sha(launcher)[0],
        "pipeline_id": pipeline_id,
        "qa_dir": _directory_definition(qa_dir.resolve()),
        "release_commit": vertical["release_commit"],
        "schema_version": 2,
        "vertical_recipe": _file_definition(vertical_recipe.resolve()),
    }
    _write_exact(output, canonical_json_bytes(payload))
    _recipe(output)
    return {"pipeline_id": pipeline_id, "recipe": str(output)}


def _source_payload(recipe: Mapping[str, Any]) -> dict[str, Any]:
    vertical_path = _absolute(recipe["vertical_recipe"]["path"], "vertical recipe")
    artifacts: list[dict[str, object]] = []
    for path in _vertical_source_paths(vertical_path):
        if path.is_dir():
            artifacts.append(
                {"path": str(path), "type": "DIRECTORY", **_directory_identity(path)}
            )
        else:
            artifacts.append(
                {"path": str(path), "type": "FILE", **_file_definition(path)}
            )
    qa_path = _absolute(recipe["qa_dir"]["path"], "panel QA")
    artifacts.append(
        {"path": str(qa_path), "type": "DIRECTORY", **_directory_identity(qa_path)}
    )
    return {
        "as_of_date": recipe["as_of_date"],
        "artifacts": sorted(artifacts, key=lambda row: str(row["path"])),
        "release_commit": recipe["release_commit"],
        "schema_version": 2,
    }


def acquire(recipe_path: Path, output_dir: Path) -> dict[str, Any]:
    payload = _source_payload(_recipe(recipe_path))
    _write_exact(output_dir / "source-run.json", canonical_json_bytes(payload))
    return {"source_artifacts": len(payload["artifacts"]), "state": "CAPTURED_LOCAL"}


def authority(recipe_path: Path, source_run: Path, output_dir: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    source = _load(source_run, "source run")
    expected = _source_payload(recipe)
    if canonical_json_bytes(source) != canonical_json_bytes(expected):
        raise MaterializationError("source run differs from the accepted recipe")
    payload = {
        "as_of_date": recipe["as_of_date"],
        "authority_policy": "IMMUTABLE_VERTICAL_RECIPE",
        "release_commit": recipe["release_commit"],
        "schema_version": 2,
        "source_run_sha256": hashlib.sha256(canonical_json_bytes(source)).hexdigest(),
        "vertical_recipe_sha256": recipe["vertical_recipe"]["sha256"],
    }
    _write_exact(output_dir / "authority-snapshot.json", canonical_json_bytes(payload))
    return {"state": "AUTHORITY_SNAPSHOT_CREATED"}


def _decimal(value: object, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MaterializationError(f"{label} is not decimal") from exc


def _current_generation(root: Path) -> tuple[str, Path]:
    pointer = _load(root / "CURRENT.json", "CURRENT pointer")
    generation_id = _text(pointer.get("generation_id"), "CURRENT generation_id")
    return generation_id, root / "generations" / generation_id


def _result(
    checks: dict[str, dict[str, Any]], name: str, passed: bool, **evidence: Any
) -> None:
    checks[name] = {"evidence": evidence, "status": "PASS" if passed else "FAIL"}


def _semantic_gate_payload(recipe: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "as_of_date": recipe["as_of_date"],
        "checks": {
            "analytics_report_panel_equality": "PASS",
            "financial_identities": "PASS",
            "freshness": "PASS",
            "generation_invariants": "PASS",
            "ledger_bridges": "PASS",
            "mappings": "PASS",
        },
        "release_commit": recipe["release_commit"],
        "release_generations": {
            ROOT_IDS[role]: recipe["expected_generations"][role]["generation_id"]
            for role in RELEASE_ROLES
        },
        "schema_version": 1,
        "unwaived_blocking_exceptions": 0,
        "verdict": "PASS",
    }


def semantic(recipe_path: Path, output_path: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    output_root = output_path.parent.parent
    checks: dict[str, dict[str, Any]] = {}

    generation_rows: dict[str, dict[str, Any]] = {}
    generation_ok = True
    for role in RELEASE_ROLES:
        generation = _generation_dir(output_root, recipe, role)
        expected = recipe["expected_generations"][role]
        try:
            inventory = inventory_generation(generation)
            inventory_files = inventory["files"]
            if not isinstance(inventory_files, list):
                raise MaterializationError(f"{role} generation inventory differs")
            manifest = GenerationManifest.from_json_bytes(
                (generation / "generation.json").read_bytes()
            )
            actual = {
                "files": inventory_files,
                "generation_id": inventory["generation_id"],
                "manifest_sha256": inventory["manifest_sha256"],
            }
            passed = (
                actual == expected
                and manifest.verification is VerificationStatus.VERIFIED
                and manifest.coverage.status
                is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
                and manifest.as_of_date.isoformat() == recipe["as_of_date"]
            )
            generation_rows[role] = {
                "actual_generation_id": actual["generation_id"],
                "expected_generation_id": expected["generation_id"],
                "file_count": len(inventory_files),
                "status": "PASS" if passed else "FAIL",
            }
        except (OSError, ValueError) as exc:
            passed = False
            generation_rows[role] = {"error": str(exc), "status": "FAIL"}
        generation_ok = generation_ok and passed
    _result(checks, "generation_invariants", generation_ok, generations=generation_rows)

    ledger_generation = _generation_dir(output_root, recipe, "ledger")
    baseline_generation = _generation_dir(output_root, recipe, "baseline")
    report_generation = _generation_dir(output_root, recipe, "report")
    panel_generation = _generation_dir(output_root, recipe, "panel")
    baseline = _load(
        baseline_generation / "portfolio-baseline.json", "portfolio baseline"
    )
    report = _load(report_generation / "portfolio-report-data.json", "portfolio report")
    panel = _load(panel_generation / "data" / "dashboard_data.json", "panel data")

    replay = baseline.get("replay")
    allocations = baseline.get("allocations")
    if not isinstance(replay, dict) or not isinstance(allocations, list):
        raise MaterializationError("baseline financial facts differ")
    systematic = _decimal(replay.get("systematic_total_usd"), "systematic total")
    reserve = _decimal(replay.get("reserve_total_usd"), "reserve total")
    total = _decimal(replay.get("total_liquid_usd"), "total liquid")
    systematic_allocations = sum(
        (
            _decimal(row["current_value"]["value"], "allocation value")
            for row in allocations
            if isinstance(row, dict)
            and row.get("pool_id") == "systematic-capital"
            and isinstance(row.get("current_value"), dict)
        ),
        Decimal(0),
    )
    weights = sum(
        (
            _decimal(row.get("systematic_weight"), "systematic weight")
            for row in allocations
            if isinstance(row, dict) and row.get("pool_id") == "systematic-capital"
        ),
        Decimal(0),
    )
    financial_ok = (
        total == systematic + reserve
        and systematic_allocations == systematic
        and abs(weights - Decimal(1)) <= WEIGHT_SUM_TOLERANCE
    )
    _result(
        checks,
        "financial_identities",
        financial_ok,
        reserve_total_usd=str(reserve),
        systematic_allocation_sum_usd=str(systematic_allocations),
        systematic_total_usd=str(systematic),
        systematic_weight_sum=str(weights),
        total_liquid_usd=str(total),
    )

    assignments = baseline.get("assignments")
    if not isinstance(assignments, list):
        raise MaterializationError("baseline assignments differ")
    open_systematic_unmapped = [
        row
        for row in assignments
        if isinstance(row, dict)
        and row.get("effective_end") is None
        and row.get("pool_id") == "systematic-capital"
        and row.get("strategy_id") is None
    ]
    invalid_reserve = [
        row
        for row in assignments
        if isinstance(row, dict)
        and row.get("effective_end") is None
        and row.get("pool_id") == "reserve-capital"
        and (
            row.get("strategy_id") is not None
            or row.get("strategy_mapping_disposition") != "RESERVE_NOT_APPLICABLE"
        )
    ]
    _result(
        checks,
        "mappings",
        not open_systematic_unmapped and not invalid_reserve,
        invalid_open_reserve_count=len(invalid_reserve),
        open_systematic_unmapped_count=len(open_systematic_unmapped),
    )

    ledger_sha = _sha(ledger_generation / "provider-ledger.json")[0]
    baseline_sha = _sha(baseline_generation / "portfolio-baseline.json")[0]
    source_artifacts = baseline.get("source_artifacts")
    report_baseline = report.get("baseline")
    provider_refs = (
        [
            row
            for row in source_artifacts
            if isinstance(row, dict)
            and row.get("source_key") == "provider-ledger-generation"
        ]
        if isinstance(source_artifacts, list)
        else []
    )
    manifest_refs = [
        row for row in provider_refs if row.get("schema_name") == "generation-manifest"
    ]
    catalog_refs = [
        row
        for row in provider_refs
        if row.get("schema_name") == "provider-ledger-catalog"
    ]
    ledger_manifest_sha = _sha(ledger_generation / "generation.json")[0]
    ledger_bridge_ok = (
        len(manifest_refs) == 1
        and manifest_refs[0].get("sha256") == ledger_manifest_sha
        and len(catalog_refs) == 1
        and catalog_refs[0].get("sha256") == ledger_sha
        and isinstance(report_baseline, dict)
        and report_baseline.get("generation_id")
        == recipe["expected_generations"]["baseline"]["generation_id"]
        and report_baseline.get("catalog_sha256") == baseline_sha
    )
    _result(
        checks,
        "ledger_bridges",
        ledger_bridge_ok,
        baseline_catalog_sha256=baseline_sha,
        baseline_provider_catalog_references=catalog_refs,
        baseline_provider_manifest_references=manifest_refs,
        ledger_catalog_sha256=ledger_sha,
        ledger_manifest_sha256=ledger_manifest_sha,
        report_baseline=report_baseline,
    )

    analytics_ids: dict[str, str] = {}
    for name, root_name in (
        ("decomposition_generation_id", "decomposition"),
        ("correlation_generation_id", "correlation"),
        ("weekly_returns_generation_id", "weekly-returns"),
    ):
        generation_id, _ = _current_generation(
            output_root / "stages" / "analytics" / root_name
        )
        analytics_ids[name] = generation_id
    panel_meta = panel.get("meta")
    report_hwm = report.get("hwm_summary")
    panel_hwm = panel.get("hwm_summary")
    equality_ok = (
        isinstance(panel_meta, dict)
        and panel_meta.get("baseline_generation_id")
        == recipe["expected_generations"]["baseline"]["generation_id"]
        and panel_meta.get("report_generation_id")
        == recipe["expected_generations"]["report"]["generation_id"]
        and all(panel_meta.get(key) == value for key, value in analytics_ids.items())
        and isinstance(report_hwm, dict)
        and report_hwm == panel_hwm
        and len(report.get("strategy_summary", []))
        == len(panel.get("strategies", []))
        == 9
    )
    _result(
        checks,
        "analytics_report_panel_equality",
        equality_ok,
        analytics_generation_ids=analytics_ids,
        panel_baseline_generation_id=(panel_meta or {}).get("baseline_generation_id")
        if isinstance(panel_meta, dict)
        else None,
        panel_report_generation_id=(panel_meta or {}).get("report_generation_id")
        if isinstance(panel_meta, dict)
        else None,
        report_hwm=report_hwm,
        panel_hwm=panel_hwm,
        report_strategy_count=len(report.get("strategy_summary", [])),
        panel_strategy_count=len(panel.get("strategies", [])),
    )

    freshness_path = output_root / "stages" / "ledger" / "freshness-health.json"
    freshness = _load(freshness_path, "freshness evidence")
    assessments = freshness.get("assessments")
    blocking = (
        [
            row
            for row in assessments
            if isinstance(row, dict) and row.get("blocks_current_consumers") is True
        ]
        if isinstance(assessments, list)
        else ["INVALID_ASSESSMENTS"]
    )
    freshness_ok = freshness.get("as_of_date") == recipe["as_of_date"] and not blocking
    _result(
        checks,
        "freshness",
        freshness_ok,
        as_of_date=freshness.get("as_of_date"),
        blocking_assessment_count=len(blocking),
        sha256=_sha(freshness_path)[0],
    )

    panel_qa = _load(panel_generation / "qa" / "qa-validation.json", "panel QA")
    interaction_qa = _load(
        panel_generation / "qa" / "interaction-results.json", "interaction QA"
    )
    workbook_validation = _load(
        report_generation / "workbook-validation.json", "workbook validation"
    )
    xlsx_result = _load(
        report_generation / "xlsx-win-result.json", "native Excel result"
    )
    evidence_ok = (
        panel_qa.get("checks_passed") == panel_qa.get("checks_total")
        and interaction_qa.get("all_passed") is True
        and workbook_validation.get("all_passed") is True
        and xlsx_result.get("final_state") == "SUCCEEDED"
        and xlsx_result.get("ok") is True
        and _sha(output_root / "stages" / "panel" / "index.html")[0]
        == recipe["panel_launcher_sha256"]
    )
    generation_check = checks["generation_invariants"]
    generation_check["evidence"]["release_evidence"] = {
        "native_excel_ok": xlsx_result.get("ok"),
        "panel_browser_checks": panel_qa.get("checks_total"),
        "panel_interaction_ok": interaction_qa.get("all_passed"),
        "status": "PASS" if evidence_ok else "FAIL",
    }
    if not evidence_ok:
        generation_check["status"] = "FAIL"

    verdict = (
        "PASS" if all(row["status"] == "PASS" for row in checks.values()) else "FAIL"
    )
    details = {
        "as_of_date": recipe["as_of_date"],
        "checks": checks,
        "release_commit": recipe["release_commit"],
        "schema_version": 1,
        "verdict": verdict,
    }
    details_path = output_path.with_name("semantic-details.json")
    _write_exact(details_path, canonical_json_bytes(details))
    if verdict != "PASS":
        failed = sorted(name for name, row in checks.items() if row["status"] != "PASS")
        raise MaterializationError(
            f"semantic reconciliation failed: {', '.join(failed)}"
        )
    _write_exact(output_path, canonical_json_bytes(_semantic_gate_payload(recipe)))
    return {"details": str(details_path), "state": "PASS"}


def _command(
    *,
    node_id: str,
    runtime: Path,
    entrypoint: Path,
    lock: Path,
    recipe: Path,
    depends_on: Sequence[str],
    args: Sequence[str],
    reads: Sequence[Path],
    writes: Sequence[Path],
    retry_limit: int = 0,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    return {
        "argv": [str(runtime), str(entrypoint), *args],
        "cwd": str(entrypoint.parent.parent),
        "declared_reads": [
            str(lock),
            str(entrypoint),
            str(recipe),
            *(str(path) for path in reads),
        ],
        "declared_writes": [str(path) for path in writes],
        "depends_on": list(depends_on),
        "effect": "LOCAL_ONLY",
        "kind": "COMMAND",
        "node_id": node_id,
        "request_identity": None,
        "retry_limit": retry_limit,
        "timeout_seconds": timeout_seconds,
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _validate_output_path_budget(output_root: Path, recipe: Mapping[str, Any]) -> None:
    longest = 0
    for role in RELEASE_ROLES:
        generation_id = str(recipe["expected_generations"][role]["generation_id"])
        for row in recipe["expected_generations"][role]["files"]:
            candidate = (
                output_root
                / "release-roots"
                / ROOT_IDS[role]
                / "generations"
                / generation_id
                / row["relative_path"]
            )
            longest = max(longest, len(str(candidate)))
    if longest > WINDOWS_SAFE_FILE_PATH:
        raise MaterializationError(
            f"private output root exceeds the portable Windows path budget: {longest} characters"
        )


def build_spec(recipe_path: Path, output_root: Path, spec_path: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    _validate_output_path_budget(output_root.resolve(), recipe)
    repo = Path(__file__).resolve().parents[1]
    release_commit = _git(repo, "rev-parse", "HEAD")
    if release_commit != recipe["release_commit"] or _git(
        repo, "status", "--porcelain=v1", "--untracked-files=all"
    ):
        raise MaterializationError("repository must be clean at the recipe commit")
    runtime = Path(os.environ.get("QE_PIPELINE_PYTHON", sys.executable)).resolve()
    lock = repo / "uv.lock"
    entrypoint = Path(__file__).resolve()
    builder = repo / "tools" / "portfolio_release_builder.py"
    run_id = hashlib.sha256(canonical_json_bytes(recipe)).hexdigest()
    run_dir = output_root / "runs" / run_id
    source_run = run_dir / "source-run.json"
    authority_dir = output_root / "stages" / "authority"
    authority_path = authority_dir / "authority-snapshot.json"
    semantic_dir = output_root / "evidence"
    semantic_path = semantic_dir / "post-build-semantic.json"
    vertical_path = _absolute(recipe["vertical_recipe"]["path"], "vertical recipe")
    qa_dir = _absolute(recipe["qa_dir"]["path"], "panel QA")
    source_paths = _vertical_source_paths(vertical_path)

    nodes: list[dict[str, Any]] = []
    nodes.append(
        _command(
            node_id="acquire-local-sources",
            runtime=runtime,
            entrypoint=entrypoint,
            lock=lock,
            recipe=recipe_path,
            depends_on=(),
            args=(
                "acquire",
                "--recipe",
                str(recipe_path),
                "--output-dir",
                str(run_dir),
            ),
            reads=[*source_paths, qa_dir],
            writes=[run_dir],
        )
    )
    nodes.append(
        _command(
            node_id="build-authority-snapshot",
            runtime=runtime,
            entrypoint=entrypoint,
            lock=lock,
            recipe=recipe_path,
            depends_on=("acquire-local-sources",),
            args=(
                "authority",
                "--recipe",
                str(recipe_path),
                "--source-run",
                str(source_run),
                "--output-dir",
                str(authority_dir),
            ),
            reads=[run_dir],
            writes=[authority_dir],
        )
    )
    source_payload = _source_payload(recipe)
    authority_bytes = canonical_json_bytes(
        {
            "as_of_date": recipe["as_of_date"],
            "authority_policy": "IMMUTABLE_VERTICAL_RECIPE",
            "release_commit": recipe["release_commit"],
            "schema_version": 2,
            "source_run_sha256": hashlib.sha256(
                canonical_json_bytes(source_payload)
            ).hexdigest(),
            "vertical_recipe_sha256": recipe["vertical_recipe"]["sha256"],
        }
    )
    nodes.append(
        {
            "depends_on": ["build-authority-snapshot"],
            "kind": "ARTIFACT",
            "node_id": "authority-snapshot",
            "path": str(authority_path),
            "sha256": hashlib.sha256(authority_bytes).hexdigest(),
            "size_bytes": len(authority_bytes),
        }
    )

    stage_writes = {
        "ledger": [
            output_root / "stages" / "ledger",
            output_root / "stage-results" / "ledger.json",
        ],
        "baseline": [
            output_root / "stages" / "baseline",
            output_root / "stage-results" / "baseline.json",
        ],
        "attribution": [
            output_root / "stages" / "attribution",
            output_root / "stage-results" / "attribution.json",
        ],
        "analytics": [
            output_root / "stages" / "analytics",
            output_root / "stage-results" / "analytics.json",
        ],
        "report": [
            output_root / "stages" / "report-candidate",
            output_root / "stage-results" / "report.json",
        ],
        "workbook": [
            output_root / "stages" / "report-candidate",
            output_root / "stages" / "report",
            output_root / "stage-results" / "workbook.json",
        ],
        "panel": [
            output_root / "stages" / "panel",
            output_root / "stage-results" / "panel.json",
        ],
    }
    previous_node = "authority-snapshot"
    previous_outputs: list[Path] = [authority_path]
    generation_nodes: dict[str, str] = {}
    stage_read_dependencies = {
        "baseline": ("ledger",),
        "attribution": ("baseline", "ledger"),
        "analytics": ("attribution",),
        "report": ("baseline", "ledger", "analytics"),
        "workbook": ("baseline", "ledger", "report"),
        "panel": ("ledger", "attribution", "analytics", "workbook"),
    }
    for role in STAGES:
        command_id = f"build-{role}"
        args = [
            "stage",
            "--recipe",
            str(vertical_path),
            "--output-root",
            str(output_root),
            "--role",
            role,
        ]
        if role == "panel":
            args.extend(("--qa-dir", str(qa_dir)))
        reads = [*previous_outputs, *source_paths]
        for dependency in stage_read_dependencies.get(role, ()):
            reads.extend(stage_writes[dependency])
        if role == "panel":
            reads.append(qa_dir)
        reads = list(dict.fromkeys(reads))
        nodes.append(
            _command(
                node_id=command_id,
                runtime=runtime,
                entrypoint=builder,
                lock=lock,
                recipe=recipe_path,
                depends_on=(previous_node,),
                args=args,
                reads=reads,
                writes=stage_writes[role],
                retry_limit=1 if role == "workbook" else 0,
            )
        )
        release_role = next(
            (item for item, stage in GENERATION_STAGE.items() if stage == role), None
        )
        if release_role is not None:
            expected = recipe["expected_generations"][release_role]
            generation_node = f"{release_role}-generation"
            generation_source = _generation_dir(output_root, recipe, release_role)
            nodes.append(
                {
                    "depends_on": [command_id],
                    "files": expected["files"],
                    "generation_id": expected["generation_id"],
                    "kind": "GENERATION",
                    "manifest_sha256": expected["manifest_sha256"],
                    "node_id": generation_node,
                    "release_target": True,
                    "root_id": ROOT_IDS[release_role],
                    "source_dir": str(generation_source),
                }
            )
            generation_nodes[release_role] = generation_node
            previous_outputs = [generation_source]
        else:
            previous_outputs = stage_writes[role]
        previous_node = command_id

    generation_sources = [
        _generation_dir(output_root, recipe, role) for role in RELEASE_ROLES
    ]
    nodes.append(
        _command(
            node_id="post-build-semantic-reconciliation",
            runtime=runtime,
            entrypoint=entrypoint,
            lock=lock,
            recipe=recipe_path,
            depends_on=(previous_node, *generation_nodes.values()),
            args=(
                "semantic",
                "--recipe",
                str(recipe_path),
                "--output",
                str(semantic_path),
            ),
            reads=[
                authority_path,
                *generation_sources,
                output_root / "stages" / "analytics",
                *stage_writes["panel"],
            ],
            writes=[semantic_path, semantic_dir / "semantic-details.json"],
        )
    )

    command_sources = []
    for node in nodes:
        if node["kind"] != "COMMAND":
            continue
        executable = Path(node["argv"][1])
        command_sources.append(
            {
                "entrypoint_path": str(executable),
                "entrypoint_sha256": _sha(executable)[0],
                "node_id": node["node_id"],
            }
        )
    contract = {
        "authority_snapshot": {
            "artifact_node": "authority-snapshot",
            "command_node": "build-authority-snapshot",
        },
        "command_sources": command_sources,
        "profile": "PORTFOLIO_SOURCE_TO_PANEL_V1",
        "release_commit": release_commit,
        "runtime": {
            "environment_sha256": runtime_environment_sha256(runtime),
            "executable_path": str(runtime),
            "executable_sha256": _sha(runtime)[0],
            "lock_path": str(lock),
            "lock_sha256": _sha(lock)[0],
            "repo_root": str(repo),
        },
        "schema_version": 1,
        "semantic_reconciliation_node": "post-build-semantic-reconciliation",
        "source_acquisitions": [
            {"adapter_commit": release_commit, "node_id": "acquire-local-sources"}
        ],
        "stages": {
            role: {
                "command_node": f"build-{role}",
                "generation_nodes": [generation_nodes[release_role]]
                if (
                    release_role := next(
                        (
                            item
                            for item, stage in GENERATION_STAGE.items()
                            if stage == role
                        ),
                        None,
                    )
                )
                is not None
                else [],
            }
            for role in STAGES
        },
    }
    contract_path = spec_path.with_name("portfolio-orchestration-contract.json")
    contract_bytes = canonical_json_bytes(contract)
    _write_exact(contract_path, contract_bytes)
    semantic_bytes = canonical_json_bytes(_semantic_gate_payload(recipe))
    launcher = output_root / "stages" / "panel" / "index.html"
    spec = {
        "as_of_date": recipe["as_of_date"],
        "code_commit": release_commit,
        "companions": [
            {
                "companion_id": "stable-control-panel-launcher",
                "relative_path": "index.html",
                "root_id": "control-panel",
                "sha256": recipe["panel_launcher_sha256"],
                "source_path": str(launcher),
            }
        ],
        "coordination_root": str(output_root / "coordination"),
        "created_at": datetime.now(UTC).isoformat(),
        "gates": [
            {
                "gate_id": "portfolio-orchestration-contract",
                "path": str(contract_path),
                "sha256": hashlib.sha256(contract_bytes).hexdigest(),
            },
            {
                "gate_id": "post-build-semantic",
                "path": str(semantic_path),
                "sha256": hashlib.sha256(semantic_bytes).hexdigest(),
            },
        ],
        "nodes": nodes,
        "pipeline_id": recipe["pipeline_id"],
        "roots": {
            ROOT_IDS[role]: str(output_root / "release-roots" / ROOT_IDS[role])
            for role in RELEASE_ROLES
        },
        "schema_version": 1,
        "state_root": str(output_root / "state"),
    }
    _write_exact(spec_path, canonical_json_bytes(spec))
    return {"contract": str(contract_path), "nodes": len(nodes), "spec": str(spec_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    acquire_cmd = commands.add_parser("acquire")
    acquire_cmd.add_argument("--recipe", type=Path, required=True)
    acquire_cmd.add_argument("--output-dir", type=Path, required=True)
    authority_cmd = commands.add_parser("authority")
    authority_cmd.add_argument("--recipe", type=Path, required=True)
    authority_cmd.add_argument("--source-run", type=Path, required=True)
    authority_cmd.add_argument("--output-dir", type=Path, required=True)
    semantic_cmd = commands.add_parser("semantic")
    semantic_cmd.add_argument("--recipe", type=Path, required=True)
    semantic_cmd.add_argument("--output", type=Path, required=True)
    recipe_cmd = commands.add_parser("recipe")
    recipe_cmd.add_argument("--vertical-recipe", type=Path, required=True)
    recipe_cmd.add_argument("--proof-root", type=Path, required=True)
    recipe_cmd.add_argument("--qa-dir", type=Path, required=True)
    recipe_cmd.add_argument("--pipeline-id", required=True)
    recipe_cmd.add_argument("--output", type=Path, required=True)
    spec_cmd = commands.add_parser("spec")
    spec_cmd.add_argument("--recipe", type=Path, required=True)
    spec_cmd.add_argument("--output-root", type=Path, required=True)
    spec_cmd.add_argument("--spec", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "acquire":
            result = acquire(args.recipe, args.output_dir)
        elif args.command == "authority":
            result = authority(args.recipe, args.source_run, args.output_dir)
        elif args.command == "semantic":
            result = semantic(args.recipe, args.output)
        elif args.command == "recipe":
            result = create_recipe(
                args.vertical_recipe,
                args.proof_root,
                args.qa_dir,
                args.pipeline_id,
                args.output,
            )
        else:
            result = build_spec(args.recipe, args.output_root, args.spec)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"portfolio-pipeline: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
