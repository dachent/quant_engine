"""Build one private portfolio release vertically from immutable local inputs."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from quant_engine.core import (
    GenerationManifest,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
)
from quant_engine.core.contracts import canonical_json_bytes


class VerticalBuildError(ValueError):
    """A source, stage, or proof differs from the vertical-build contract."""


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
XLSX_SKILL_ROOT = Path(r"C:\Users\BorisVaisman\.codex\skills\xlsx-win")
XLSX_RUNTIME = Path(r"C:\Tools\xlsx-win\.venv\Scripts\python.exe")
XLSX_CLI = XLSX_SKILL_ROOT / "control_plane" / "cli.py"


def _load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerticalBuildError(f"{label} is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalBuildError(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise VerticalBuildError(f"{label} root must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerticalBuildError(f"{label} must be non-empty text")
    return value.strip()


def _path(value: object, label: str) -> Path:
    path = Path(_text(value, label)).resolve()
    if not path.is_absolute():
        raise VerticalBuildError(f"{label} must be absolute")
    return path


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise VerticalBuildError(f"create-only output differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sha(path: Path) -> tuple[str, int]:
    try:
        return hash_file(path)
    except (OSError, ValueError) as exc:
        raise VerticalBuildError(f"source artifact is unavailable: {path}") from exc


def _directory_inventory(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir():
        raise VerticalBuildError(f"source directory is unavailable: {path}")
    rows = []
    for item in sorted(
        candidate for candidate in path.rglob("*") if candidate.is_file()
    ):
        if item.is_symlink():
            raise VerticalBuildError(f"source directory contains a link: {item}")
        sha256, size = _sha(item)
        rows.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": sha256,
                "size_bytes": size,
            }
        )
    content = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "inventory_sha256": hashlib.sha256(content).hexdigest(),
    }


def _validated_reusable_provider_ledger(path: Path) -> tuple[str, Path]:
    if path.is_symlink() or not path.is_dir():
        raise VerticalBuildError("reusable provider-ledger generation is unavailable")
    if len(path.name) != 64 or any(
        character not in "0123456789abcdef" for character in path.name
    ):
        raise VerticalBuildError("reusable provider-ledger generation id is invalid")
    files = {
        item.relative_to(path).as_posix(): item
        for item in path.rglob("*")
        if item.is_file()
    }
    if set(files) != {"generation.json", "provider-ledger.json"}:
        raise VerticalBuildError("reusable provider-ledger file boundary differs")
    if any(item.is_symlink() for item in files.values()):
        raise VerticalBuildError("reusable provider-ledger contains a link")
    try:
        manifest = GenerationManifest.from_json_bytes(
            files["generation.json"].read_bytes()
        )
    except (OSError, ValueError) as exc:
        raise VerticalBuildError(
            "reusable provider-ledger manifest is invalid"
        ) from exc
    if manifest.generation_id != path.name:
        raise VerticalBuildError("reusable provider-ledger generation id differs")
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise VerticalBuildError("reusable provider-ledger is not verified")
    if (
        manifest.adapter.name != "quant-engine-provider-ledger"
        or manifest.adapter.version != "1"
    ):
        raise VerticalBuildError("reusable provider-ledger adapter differs")
    artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "normalized/provider-ledger.json"
        ),
        None,
    )
    if artifact is None:
        raise VerticalBuildError("reusable provider-ledger artifact is absent")
    catalog_sha256, catalog_size = _sha(files["provider-ledger.json"])
    if artifact.sha256 != catalog_sha256 or artifact.size_bytes != catalog_size:
        raise VerticalBuildError("reusable provider-ledger artifact identity differs")
    catalog = _load(files["provider-ledger.json"], "reusable provider-ledger")
    if (
        catalog.get("as_of_date") != manifest.as_of_date.isoformat()
        or catalog.get("catalog_schema_version") != 1
    ):
        raise VerticalBuildError("reusable provider-ledger catalog contract differs")
    return manifest.generation_id, files["provider-ledger.json"]


def _history_contract_path(recipe: Mapping[str, Any]) -> Path:
    correlation_root = _path(
        recipe["correlation_source_root"]["path"], "correlation source root"
    )
    proof_path = correlation_root / "evidence-proof.json"
    proof = _load(proof_path, "strategy history compatibility contract")
    if proof.get("builder") != "portfolio-target-series/layered-correlation-1":
        raise VerticalBuildError("strategy history contract builder differs")
    if proof.get("evidence_mode_policy") != {
        "default": "LIVE_ONLY",
        "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
        "post_inception_backtest_fill": False,
    }:
        raise VerticalBuildError("strategy history evidence policy differs")
    mappings = proof.get("series_mappings")
    if not isinstance(mappings, list) or len(mappings) != 9:
        raise VerticalBuildError("strategy history contract boundary differs")
    names = []
    for row in mappings:
        if not isinstance(row, dict):
            raise VerticalBuildError("strategy history contract row is invalid")
        names.append(_text(row.get("series_id"), "history series_id"))
        observations = row.get("backtest_observations")
        status = _text(row.get("history_status"), "history status")
        _text(row.get("strategy_version"), "history strategy_version")
        if not isinstance(observations, int) or observations < 0:
            raise VerticalBuildError("strategy history observations are invalid")
        if status.startswith("ACCEPTED_") != (observations > 0):
            raise VerticalBuildError("strategy history status and observations differ")
    if len(set(names)) != len(names):
        raise VerticalBuildError("strategy history contract duplicates a series")
    target_sha, target_size = _sha(correlation_root / "returns.csv")
    if (
        proof.get("target_sha256") != target_sha
        or proof.get("target_size_bytes") != target_size
    ):
        raise VerticalBuildError("strategy history contract target identity differs")
    return proof_path


def _recipe(path: Path) -> dict[str, Any]:
    recipe = _load(path, "vertical-build recipe")
    schema_version = recipe.get("schema_version")
    if schema_version not in {6, 7}:
        raise VerticalBuildError("vertical-build recipe schema is unsupported")
    required = {
        "as_of_date",
        "base_factor_file",
        "baseline_spec",
        "correlation_source_root",
        "decomposition_plan_bundle",
        "implementation_reconciliation",
        "implementation_return_attribution",
        "legacy_panel",
        "pipeline_id",
        "release_commit",
        "schema_version",
        "strategy_attribution_root",
    }
    required.add(
        "provider_ledger_spec" if schema_version == 6 else "provider_ledger_generation"
    )
    if set(recipe) != required:
        raise VerticalBuildError("vertical-build recipe keys differ")
    date.fromisoformat(_text(recipe["as_of_date"], "as_of_date"))
    for key in (
        "baseline_spec",
        "base_factor_file",
        "implementation_reconciliation",
        "implementation_return_attribution",
    ):
        definition = recipe[key]
        if not isinstance(definition, dict) or set(definition) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise VerticalBuildError(f"{key} definition differs")
        artifact = _path(definition["path"], f"{key}.path")
        sha256, size = _sha(artifact)
        if sha256 != definition["sha256"] or size != definition["size_bytes"]:
            raise VerticalBuildError(f"{key} identity differs")
    if schema_version == 6:
        definition = recipe["provider_ledger_spec"]
        if not isinstance(definition, dict) or set(definition) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise VerticalBuildError("provider_ledger_spec definition differs")
        artifact = _path(definition["path"], "provider_ledger_spec.path")
        sha256, size = _sha(artifact)
        if sha256 != definition["sha256"] or size != definition["size_bytes"]:
            raise VerticalBuildError("provider_ledger_spec identity differs")
    for key in (
        "decomposition_plan_bundle",
        "correlation_source_root",
        "legacy_panel",
        "strategy_attribution_root",
    ):
        definition = recipe[key]
        if not isinstance(definition, dict) or set(definition) != {
            "file_count",
            "inventory_sha256",
            "path",
        }:
            raise VerticalBuildError(f"{key} definition differs")
        actual = _directory_inventory(_path(definition["path"], f"{key}.path"))
        if actual != {
            "file_count": definition["file_count"],
            "inventory_sha256": definition["inventory_sha256"],
        }:
            raise VerticalBuildError(f"{key} inventory differs")
    if schema_version == 7:
        definition = recipe["provider_ledger_generation"]
        if not isinstance(definition, dict) or set(definition) != {
            "file_count",
            "inventory_sha256",
            "path",
        }:
            raise VerticalBuildError("provider_ledger_generation definition differs")
        generation = _path(definition["path"], "provider_ledger_generation.path")
        actual = _directory_inventory(generation)
        if actual != {
            "file_count": definition["file_count"],
            "inventory_sha256": definition["inventory_sha256"],
        }:
            raise VerticalBuildError("provider_ledger_generation inventory differs")
        _validated_reusable_provider_ledger(generation)
    _history_contract_path(recipe)
    return recipe


def _file_definition(path: Path) -> dict[str, object]:
    sha256, size = _sha(path)
    return {
        "path": str(path.resolve()),
        "sha256": sha256,
        "size_bytes": size,
    }


def create_recipe(
    *,
    provider_ledger_spec: Path | None,
    provider_ledger_generation: Path | None,
    baseline_spec: Path,
    base_factor_file: Path,
    implementation_reconciliation: Path,
    implementation_return_attribution: Path,
    strategy_attribution_root: Path,
    decomposition_plan_bundle: Path,
    correlation_source_root: Path,
    legacy_panel: Path,
    as_of_date: str,
    pipeline_id: str,
    release_commit: str,
    output: Path,
) -> dict[str, Any]:
    date.fromisoformat(as_of_date)
    if len(release_commit) != 40:
        raise VerticalBuildError("release commit differs")
    if (provider_ledger_spec is None) == (provider_ledger_generation is None):
        raise VerticalBuildError("exactly one provider-ledger source must be supplied")
    payload: dict[str, Any] = {
        "as_of_date": as_of_date,
        "base_factor_file": _file_definition(base_factor_file.resolve()),
        "baseline_spec": _file_definition(baseline_spec.resolve()),
        "implementation_reconciliation": _file_definition(
            implementation_reconciliation.resolve()
        ),
        "implementation_return_attribution": _file_definition(
            implementation_return_attribution.resolve()
        ),
        "correlation_source_root": {
            "path": str(correlation_source_root.resolve()),
            **_directory_inventory(correlation_source_root.resolve()),
        },
        "decomposition_plan_bundle": {
            "path": str(decomposition_plan_bundle.resolve()),
            **_directory_inventory(decomposition_plan_bundle.resolve()),
        },
        "legacy_panel": {
            "path": str(legacy_panel.resolve()),
            **_directory_inventory(legacy_panel.resolve()),
        },
        "pipeline_id": pipeline_id,
        "release_commit": release_commit,
        "strategy_attribution_root": {
            "path": str(strategy_attribution_root.resolve()),
            **_directory_inventory(strategy_attribution_root.resolve()),
        },
    }
    if provider_ledger_spec is not None:
        payload["provider_ledger_spec"] = _file_definition(
            provider_ledger_spec.resolve()
        )
        payload["schema_version"] = 6
    else:
        assert provider_ledger_generation is not None
        generation = provider_ledger_generation.resolve()
        _validated_reusable_provider_ledger(generation)
        payload["provider_ledger_generation"] = {
            "path": str(generation),
            **_directory_inventory(generation),
        }
        payload["schema_version"] = 7
    _write_exact(output, canonical_json_bytes(payload))
    _recipe(output)
    return {"pipeline_id": pipeline_id, "recipe": str(output)}


def _run(
    command: Sequence[str],
    *,
    timeout: int = 3600,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        list(command),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=None if env is None else dict(env),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise VerticalBuildError(
            f"command failed ({completed.returncode}): {' '.join(command)}: {detail}"
        )
    output = completed.stdout.strip()
    if not output:
        return {}
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise VerticalBuildError("command did not return JSON") from exc
    if not isinstance(value, dict):
        raise VerticalBuildError("command JSON root is not an object")
    return value


def _is_xlsx_startup_deadline(result: Mapping[str, Any]) -> bool:
    steps = result.get("steps")
    if result.get("final_state") != "TIMED_OUT" or not isinstance(steps, list):
        return False
    if len(steps) != 1 or not isinstance(steps[0], dict):
        return False
    error = steps[0].get("error")
    if not isinstance(error, dict):
        return False
    details = error.get("details")
    return (
        error.get("code") == "PHASE_DEADLINE_EXCEEDED"
        and isinstance(details, dict)
        and details.get("phase") == "STARTING_EXCEL"
    )


def _xlsx_job_with_startup_extension(job: Mapping[str, Any]) -> dict[str, Any]:
    if set(job) != {"idempotency_key", "schema_version", "steps"}:
        raise VerticalBuildError("xlsx-win base job keys differ")
    if job.get("schema_version") != "2.0" or not isinstance(job.get("steps"), list):
        raise VerticalBuildError("xlsx-win base job contract differs")
    return {
        **job,
        # The installed C# supervisor documents this tolerant extension even
        # though the Python schema has not exposed it yet.  Only Excel COM
        # activation receives a larger budget; all other phase limits remain
        # explicit and bounded.
        "timeouts": {
            "calculation_seconds": 180,
            "close_seconds": 300,
            "open_workbook_seconds": 60,
            "refresh_total_seconds": 300,
            "save_seconds": 60,
            "start_excel_seconds": 120,
        },
    }


def _accepted_xlsx_evidence(candidate: Path) -> bool:
    job_path = candidate / "xlsx-job.json"
    result_path = candidate / "xlsx-result.json"
    workbook_path = candidate / "excel" / "portfolio-report.xlsx"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (job_path, result_path, workbook_path)
    ):
        return False
    job = _load(job_path, "xlsx-win job")
    result = _load(result_path, "xlsx-win result")
    steps = result.get("steps")
    return (
        result.get("schema_version") == "2.0"
        and result.get("idempotency_key") == job.get("idempotency_key")
        and result.get("final_state") == "SUCCEEDED"
        and result.get("ok") is True
        and isinstance(steps, list)
        and len(steps) == len(job.get("steps", []))
        and all(
            isinstance(step, dict) and step.get("status") == "succeeded"
            for step in steps
        )
    )


def _accepted_workbook_validation(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    result = _load(path, "workbook validation")
    invariants = result.get("invariants")
    if (
        result.get("all_passed") is not True
        or not isinstance(invariants, list)
        or not invariants
        or not all(
            isinstance(invariant, dict) and invariant.get("passed") is True
            for invariant in invariants
        )
    ):
        return None
    return result


def _xlsx_executable(project: str) -> Path:
    root = XLSX_SKILL_ROOT / "supervisor" / project / "bin"
    matches = sorted(
        (
            path
            for path in root.rglob(f"{project}.exe")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not matches:
        raise VerticalBuildError(f"xlsx-win {project} executable is unavailable")
    return matches[0]


def _run_xlsx_startup_extension(candidate: Path) -> Path:
    base_job = _load(candidate / "xlsx-job.json", "xlsx-win job")
    extended_job = candidate / "xlsx-job-startup-extension.json"
    _write_exact(
        extended_job,
        canonical_json_bytes(_xlsx_job_with_startup_extension(base_job)),
    )
    events_path = candidate / "xlsx-events-startup-extension.jsonl"
    result_path = candidate / "xlsx-result-startup-extension.json"
    supervisor = _xlsx_executable("XlsxWinSupervisor")
    worker = _xlsx_executable("XlsxWinWorker")
    environment = dict(os.environ)
    environment["XLSXWIN_WORKER_EXE_PATH"] = str(worker)
    _run(
        (
            str(supervisor),
            str(extended_job),
            str(events_path),
            str(result_path),
        ),
        timeout=600,
        env=environment,
    )
    result = _load(result_path, "xlsx-win startup-extension result")
    if result.get("ok") is not True or result.get("final_state") != "SUCCEEDED":
        raise VerticalBuildError("xlsx-win startup extension did not succeed")
    return result_path


def _tool(name: str, *args: str, timeout: int = 3600) -> dict[str, Any]:
    return _run(
        (sys.executable, str(TOOLS_ROOT / name), *args),
        timeout=timeout,
    )


def _published_generation(root: Path) -> tuple[str, Path]:
    pointer = read_current(root)
    generation = root / "generations" / pointer.generation_id
    if generation.is_symlink() or not generation.is_dir():
        raise VerticalBuildError("published generation directory is unavailable")
    return pointer.generation_id, generation


def build_ledger(recipe_path: Path, output_root: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    if recipe["schema_version"] == 6:
        source_spec = _path(recipe["provider_ledger_spec"]["path"], "provider spec")
        spec = _load(source_spec, "provider-ledger specification")
        spec["output_root"] = str(output_root.resolve())
        derived = output_root / "config" / "provider-ledger-spec.json"
        _write_exact(derived, canonical_json_bytes(spec))
        staged = _tool("provider_ledger.py", "stage", "--spec", str(derived))
        generation_id = _text(staged.get("generation_id"), "ledger generation_id")
        _tool(
            "provider_ledger.py",
            "verify",
            "--spec",
            str(derived),
            "--generation-id",
            generation_id,
        )
        source_mode = "REBUILT_FROM_BOUND_SPEC"
    else:
        source = _path(
            recipe["provider_ledger_generation"]["path"],
            "provider-ledger generation",
        )
        generation_id, _ = _validated_reusable_provider_ledger(source)
        destination = output_root / "generations" / generation_id
        for artifact in sorted(source.iterdir(), key=lambda item: item.name):
            _write_exact(destination / artifact.name, artifact.read_bytes())
        _validated_reusable_provider_ledger(destination)
        source_mode = "REUSED_VERIFIED_IMMUTABLE_GENERATION"
    pointer = publish_generation(output_root, generation_id)
    freshness = output_root / "freshness-health.json"
    _tool(
        "portfolio_freshness.py",
        "--provider-ledger-root",
        str(output_root),
        "--provider-ledger-generation-id",
        generation_id,
        "--evaluation-date",
        _text(recipe["as_of_date"], "as_of_date"),
        "--output",
        str(freshness),
    )
    return {
        "freshness_health": str(freshness),
        "generation_id": generation_id,
        "publication_state": pointer.state.value,
        "root": str(output_root),
        "source_mode": source_mode,
    }


def build_baseline(
    recipe_path: Path, ledger_root: Path, output_root: Path
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    ledger_id, ledger_generation = _published_generation(ledger_root)
    ledger_manifest = ledger_generation / "generation.json"
    ledger_catalog = ledger_generation / "provider-ledger.json"
    manifest_sha256, _ = _sha(ledger_manifest)
    catalog_sha256, _ = _sha(ledger_catalog)
    spec = _load(
        _path(recipe["baseline_spec"]["path"], "baseline spec"),
        "baseline specification",
    )
    spec["provider_ledger"] = {
        "catalog_sha256": catalog_sha256,
        "generation_id": ledger_id,
        "manifest_sha256": manifest_sha256,
        "root_id": "provider-ledger",
    }
    roots = spec.get("roots")
    if not isinstance(roots, dict):
        raise VerticalBuildError("baseline roots are invalid")
    roots["provider-ledger"] = str(ledger_root.resolve())
    derived = output_root / "config" / "portfolio-baseline-spec.json"
    _write_exact(derived, canonical_json_bytes(spec))
    staged = _tool(
        "portfolio_baseline.py",
        "stage",
        "--spec",
        str(derived),
        "--output-root",
        str(output_root),
    )
    generation_id = _text(staged.get("generation_id"), "baseline generation_id")
    published = _tool(
        "portfolio_baseline.py",
        "publish",
        "--spec",
        str(derived),
        "--output-root",
        str(output_root),
        "--generation-id",
        generation_id,
    )
    return {
        **published,
        "root": str(output_root),
        "spec": str(derived),
    }


def build_attribution(recipe_path: Path, output_root: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    source = _path(
        recipe["decomposition_plan_bundle"]["path"], "decomposition plan bundle"
    )
    manifest = _load(source / "plan-manifest.json", "attribution plan manifest")
    if manifest.get("bundle_id") != source.name:
        raise VerticalBuildError("attribution plan bundle identity differs")
    rows = []
    for section in ("inputs", "outputs"):
        values = manifest.get(section)
        if not isinstance(values, list) or not values:
            raise VerticalBuildError(f"attribution plan {section} are invalid")
        for row in values:
            if not isinstance(row, dict):
                raise VerticalBuildError(f"attribution plan {section} row is invalid")
            relative = _text(row.get("path"), f"attribution {section} path")
            artifact = source / relative
            sha256, size = _sha(artifact)
            passed = sha256 == row.get("sha256") and size == row.get("size_bytes")
            rows.append(
                {
                    "path": relative,
                    "sha256": sha256,
                    "size_bytes": size,
                    "status": "PASS" if passed else "FAIL",
                }
            )
            if not passed:
                raise VerticalBuildError(
                    f"attribution plan artifact differs: {relative}"
                )
    ledger_id, ledger_generation = _published_generation(output_root.parent / "ledger")
    ledger_manifest = ledger_generation / "generation.json"
    ledger_catalog = ledger_generation / "provider-ledger.json"
    bound_manifest = (
        source / "inputs" / "base" / "provenance" / "00-provider-ledger-generation.json"
    )
    bound_catalog = (
        source / "inputs" / "base" / "provenance" / "01-provider-ledger.json"
    )
    if (
        bound_manifest.read_bytes() != ledger_manifest.read_bytes()
        or bound_catalog.read_bytes() != ledger_catalog.read_bytes()
    ):
        raise VerticalBuildError(
            "attribution plan is not bound to the rebuilt provider ledger"
        )
    verification = {
        "artifact_count": len(rows),
        "bundle_id": source.name,
        "ledger_catalog_sha256": _sha(ledger_catalog)[0],
        "ledger_generation_id": ledger_id,
        "plan_manifest_sha256": _sha(source / "plan-manifest.json")[0],
        "schema_version": 1,
        "status": "PASS",
    }
    verification_path = output_root / "attribution-verification.json"
    _write_exact(verification_path, canonical_json_bytes(verification))
    return {
        "bundle_id": source.name,
        "destination": str(source),
        "verification": str(verification_path),
    }


def _build_hybrid_decomposition(
    *,
    base_spec: Path,
    base_source: Path,
    correlation_source: Path,
    bundle_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Publish one version-bound hybrid decomposition from the shared history contract."""

    bundle = _tool(
        "portfolio_decomposition_evidence_modes.py",
        "--base-spec",
        str(base_spec),
        "--base-source-root",
        str(base_source),
        "--layered-returns",
        str(correlation_source / "returns.csv"),
        "--correlation-spec",
        str(correlation_source / "spec.json"),
        "--output-root",
        str(bundle_root),
        timeout=7200,
    )
    source_root = _path(bundle.get("destination"), "hybrid decomposition bundle")
    hybrid_spec = source_root / "base-spec.json"
    _tool(
        "portfolio_decomposition.py",
        "stage",
        "--spec",
        str(hybrid_spec),
        "--source-root",
        str(source_root),
        "--output-root",
        str(output_root),
        timeout=7200,
    )
    published = _tool(
        "portfolio_decomposition.py",
        "publish",
        "--spec",
        str(hybrid_spec),
        "--source-root",
        str(source_root),
        "--output-root",
        str(output_root),
        timeout=7200,
    )
    return {
        "bundle_id": _text(bundle.get("bundle_id"), "hybrid bundle_id"),
        "bundle_root": str(source_root),
        "generation_id": _text(published.get("generation_id"), "hybrid generation_id"),
        "root": str(output_root),
    }


def build_analytics(
    recipe_path: Path,
    attribution_bundle: Path,
    output_root: Path,
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    base_spec = (
        attribution_bundle / "inputs" / "governance" / "base-decomposition-spec.json"
    )
    base_source = output_root / "base-input"
    frozen_base = attribution_bundle / "inputs" / "base"
    for source in sorted(path for path in frozen_base.rglob("*") if path.is_file()):
        _write_exact(base_source / source.relative_to(frozen_base), source.read_bytes())
    base_factor = _path(recipe["base_factor_file"]["path"], "base factor file")
    _write_exact(base_source / "factors.csv", base_factor.read_bytes())
    base_spec_value = _load(base_spec, "base decomposition specification")
    provenance_sources = base_spec_value.get("provenance_sources")
    if not isinstance(provenance_sources, list) or not provenance_sources:
        raise VerticalBuildError("base decomposition provenance sources are invalid")
    frozen_files = [path for path in frozen_base.rglob("*") if path.is_file()]
    for definition in provenance_sources:
        if not isinstance(definition, dict):
            raise VerticalBuildError("base decomposition provenance source is invalid")
        relative = _text(
            definition.get("relative_path"), "base provenance relative_path"
        )
        expected_sha256 = _text(
            definition.get("expected_sha256"), "base provenance expected_sha256"
        )
        expected_size = definition.get("expected_size_bytes")
        matches = [
            path
            for path in frozen_files
            if _sha(path) == (expected_sha256, expected_size)
        ]
        if len(matches) != 1:
            raise VerticalBuildError(
                f"base provenance source is missing or ambiguous: {relative}"
            )
        _write_exact(base_source / relative, matches[0].read_bytes())
    decomposition_root = output_root / "decomposition"
    _tool(
        "portfolio_decomposition.py",
        "stage",
        "--spec",
        str(base_spec),
        "--source-root",
        str(base_source),
        "--output-root",
        str(decomposition_root),
        timeout=7200,
    )
    decomposition = _tool(
        "portfolio_decomposition.py",
        "publish",
        "--spec",
        str(base_spec),
        "--source-root",
        str(base_source),
        "--output-root",
        str(decomposition_root),
        timeout=7200,
    )
    correlation_source = _path(
        recipe["correlation_source_root"]["path"], "correlation source root"
    )
    lenses_root = output_root / "decomposition-lenses"
    lenses: dict[str, str] = {}
    hybrid_lenses: dict[str, str] = {}
    hybrid_bundles: dict[str, str] = {}
    hybrid_root = lenses_root / "LIVE_PLUS_BACKTEST"
    evidence_mode_root = output_root / "decomposition-evidence-modes"
    hybrid_dynamic = _build_hybrid_decomposition(
        base_spec=base_spec,
        base_source=base_source,
        correlation_source=correlation_source,
        bundle_root=evidence_mode_root / "dynamic-style",
        output_root=hybrid_root / "dynamic-style",
    )
    hybrid_bundles["dynamic-style"] = _text(
        hybrid_dynamic.get("bundle_id"), "hybrid dynamic bundle_id"
    )
    plan = _load(attribution_bundle / "plan-manifest.json", "attribution plan")
    specs = plan.get("specs")
    if not isinstance(specs, list) or not specs:
        raise VerticalBuildError("attribution plan has no lens specifications")
    for row in specs:
        if not isinstance(row, dict):
            raise VerticalBuildError("attribution lens definition is invalid")
        source_key = _text(row.get("source_key"), "lens source_key")
        lens_root = lenses_root / source_key
        lens_spec = attribution_bundle / _text(row.get("path"), "lens spec path")
        _tool(
            "portfolio_decomposition.py",
            "stage",
            "--spec",
            str(lens_spec),
            "--source-root",
            str(attribution_bundle),
            "--output-root",
            str(lens_root),
            timeout=7200,
        )
        result = _tool(
            "portfolio_decomposition.py",
            "publish",
            "--spec",
            str(lens_spec),
            "--source-root",
            str(attribution_bundle),
            "--output-root",
            str(lens_root),
            timeout=7200,
        )
        lenses[source_key] = _text(result.get("generation_id"), "lens generation_id")
        hybrid = _build_hybrid_decomposition(
            base_spec=lens_spec,
            base_source=attribution_bundle,
            correlation_source=correlation_source,
            bundle_root=evidence_mode_root / source_key,
            output_root=hybrid_root / source_key,
        )
        hybrid_lenses[source_key] = _text(
            hybrid.get("generation_id"), "hybrid lens generation_id"
        )
        hybrid_bundles[source_key] = _text(
            hybrid.get("bundle_id"), "hybrid lens bundle_id"
        )
    correlation_root = output_root / "correlation"
    _tool(
        "portfolio_correlation.py",
        "stage",
        "--spec",
        str(correlation_source / "spec.json"),
        "--source-root",
        str(correlation_source),
        "--output-root",
        str(correlation_root),
        timeout=7200,
    )
    correlation = _tool(
        "portfolio_correlation.py",
        "publish",
        "--spec",
        str(correlation_source / "spec.json"),
        "--source-root",
        str(correlation_source),
        "--output-root",
        str(correlation_root),
        timeout=7200,
    )
    weekly_root = output_root / "weekly-returns"
    built = _tool(
        "portfolio_weekly_returns.py",
        "build",
        "--correlation-root",
        str(correlation_root),
        "--strategy-returns",
        str(correlation_source / "returns.csv"),
        "--plan-bundle",
        str(attribution_bundle),
        "--output-root",
        str(weekly_root),
        timeout=7200,
    )
    candidate_dir = _path(built.get("candidate_dir"), "weekly candidate")
    _tool(
        "portfolio_weekly_returns.py",
        "verify",
        "--correlation-root",
        str(correlation_root),
        "--strategy-returns",
        str(correlation_source / "returns.csv"),
        "--plan-bundle",
        str(attribution_bundle),
        "--candidate-dir",
        str(candidate_dir),
        timeout=7200,
    )
    weekly = _tool(
        "portfolio_weekly_returns.py",
        "publish",
        "--correlation-root",
        str(correlation_root),
        "--strategy-returns",
        str(correlation_source / "returns.csv"),
        "--plan-bundle",
        str(attribution_bundle),
        "--output-root",
        str(weekly_root),
        "--candidate-dir",
        str(candidate_dir),
        timeout=7200,
    )
    return {
        "correlation_generation_id": correlation.get("generation_id"),
        "correlation_root": str(correlation_root),
        "decomposition_generation_id": decomposition.get("generation_id"),
        "hybrid_decomposition_bundle_ids": hybrid_bundles,
        "hybrid_dynamic_generation_id": hybrid_dynamic.get("generation_id"),
        "hybrid_lens_generations": hybrid_lenses,
        "decomposition_lenses_root": str(lenses_root),
        "decomposition_root": str(decomposition_root),
        "lens_generations": lenses,
        "weekly_generation_id": weekly.get("generation_id"),
        "weekly_root": str(weekly_root),
    }


def build_report_candidate(
    baseline_root: Path,
    provider_ledger_root: Path,
    freshness_health: Path,
    history_contract: Path,
    implementation_reconciliation: Path,
    implementation_return_attribution: Path,
    attribution_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    built = _tool(
        "portfolio_report.py",
        "build",
        "--baseline-root",
        str(baseline_root),
        "--provider-ledger-root",
        str(provider_ledger_root),
        "--freshness-health",
        str(freshness_health),
        "--history-contract",
        str(history_contract),
        "--implementation-reconciliation",
        str(implementation_reconciliation),
        "--implementation-return-attribution",
        str(implementation_return_attribution),
        "--attribution-root",
        str(attribution_root),
        "--output-root",
        str(output_root),
        timeout=7200,
    )
    candidate = _path(built.get("candidate_dir"), "report candidate")
    return {**built, "candidate_dir": str(candidate), "root": str(output_root)}


def build_workbook(
    baseline_root: Path,
    provider_ledger_root: Path,
    freshness_health: Path,
    history_contract: Path,
    implementation_reconciliation: Path,
    implementation_return_attribution: Path,
    attribution_root: Path,
    candidate: Path,
    output_root: Path,
) -> dict[str, Any]:
    if not XLSX_RUNTIME.is_file() or not XLSX_CLI.is_file():
        raise VerticalBuildError("xlsx-win durable runtime is unavailable")
    result_path = candidate / "xlsx-result.json"
    events_path = candidate / "xlsx-events.jsonl"
    if not _accepted_xlsx_evidence(candidate):
        try:
            _run(
                (
                    str(XLSX_RUNTIME),
                    str(XLSX_CLI),
                    "run",
                    str(candidate / "xlsx-job.json"),
                    "--events",
                    str(events_path),
                    "--result",
                    str(result_path),
                    "--hard-timeout-seconds",
                    "300",
                ),
                timeout=420,
            )
        except VerticalBuildError:
            failed = _load(result_path, "xlsx-win failed result")
            if not _is_xlsx_startup_deadline(failed):
                raise
            result_path = _run_xlsx_startup_extension(candidate)
    result_canonicalization = _tool(
        "portfolio_report.py",
        "canonicalize-xlsx-result",
        "--result",
        str(result_path),
        "--evidence",
        str(candidate / "xlsx-result-canonicalization.json"),
    )
    excel_workbook = candidate / "excel" / "portfolio-report.xlsx"
    canonicalization = _tool(
        "portfolio_report.py",
        "canonicalize",
        "--workbook",
        str(excel_workbook),
        "--evidence",
        str(candidate / "workbook-canonicalization.json"),
    )
    validation_path = candidate / "workbook-validation.json"
    validation = _accepted_workbook_validation(validation_path)
    if validation is None:
        validation = _run(
            (
                str(XLSX_RUNTIME),
                str(XLSX_CLI),
                "validate-contract",
                str(excel_workbook),
                str(candidate / "workbook-contract.json"),
            ),
            timeout=180,
        )
        _write_exact(validation_path, canonical_json_bytes(validation))
    _tool(
        "portfolio_report.py",
        "verify",
        "--baseline-root",
        str(baseline_root),
        "--provider-ledger-root",
        str(provider_ledger_root),
        "--candidate-dir",
        str(candidate),
        "--excel-workbook",
        str(excel_workbook),
        "--freshness-health",
        str(freshness_health),
        "--history-contract",
        str(history_contract),
        "--implementation-reconciliation",
        str(implementation_reconciliation),
        "--implementation-return-attribution",
        str(implementation_return_attribution),
        "--attribution-root",
        str(attribution_root),
    )
    published = _tool(
        "portfolio_report.py",
        "publish",
        "--baseline-root",
        str(baseline_root),
        "--provider-ledger-root",
        str(provider_ledger_root),
        "--candidate-dir",
        str(candidate),
        "--excel-workbook",
        str(excel_workbook),
        "--xlsx-result",
        str(result_path),
        "--validation-result",
        str(validation_path),
        "--output-root",
        str(output_root),
        "--freshness-health",
        str(freshness_health),
        "--history-contract",
        str(history_contract),
        "--implementation-reconciliation",
        str(implementation_reconciliation),
        "--implementation-return-attribution",
        str(implementation_return_attribution),
        "--attribution-root",
        str(attribution_root),
        timeout=7200,
    )
    return {
        **published,
        "candidate_dir": str(candidate),
        "root": str(output_root),
        "workbook_canonicalization": canonicalization,
        "xlsx_result_canonicalization": result_canonicalization,
    }


def build_report(
    baseline_root: Path,
    provider_ledger_root: Path,
    freshness_health: Path,
    history_contract: Path,
    implementation_reconciliation: Path,
    implementation_return_attribution: Path,
    attribution_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    candidate = build_report_candidate(
        baseline_root,
        provider_ledger_root,
        freshness_health,
        history_contract,
        implementation_reconciliation,
        implementation_return_attribution,
        attribution_root,
        output_root,
    )
    return build_workbook(
        baseline_root,
        provider_ledger_root,
        freshness_health,
        history_contract,
        implementation_reconciliation,
        implementation_return_attribution,
        attribution_root,
        Path(_text(candidate.get("candidate_dir"), "report candidate")),
        output_root,
    )


def build_panel_candidate(
    recipe_path: Path,
    report_root: Path,
    attribution_bundle: Path,
    analytics: Mapping[str, Any],
    freshness_health: Path,
    output_root: Path,
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    legacy_panel = _path(recipe["legacy_panel"]["path"], "legacy panel")
    result = _tool(
        "portfolio_control_panel.py",
        "build",
        "--report-root",
        str(report_root),
        "--legacy-panel",
        str(legacy_panel),
        "--decomposition-root",
        _text(analytics.get("decomposition_root"), "decomposition_root"),
        "--decomposition-lenses-root",
        _text(
            analytics.get("decomposition_lenses_root"),
            "decomposition_lenses_root",
        ),
        "--decomposition-plan-bundle",
        str(attribution_bundle),
        "--correlation-root",
        _text(analytics.get("correlation_root"), "correlation_root"),
        "--weekly-returns-root",
        _text(analytics.get("weekly_root"), "weekly_root"),
        "--freshness-health",
        str(freshness_health),
        "--output-root",
        str(output_root),
        timeout=7200,
    )
    candidate = _path(result.get("candidate_dir"), "panel candidate")
    _tool(
        "portfolio_control_panel.py",
        "verify",
        "--report-root",
        str(report_root),
        "--legacy-panel",
        str(legacy_panel),
        "--decomposition-root",
        _text(analytics.get("decomposition_root"), "decomposition_root"),
        "--decomposition-lenses-root",
        _text(
            analytics.get("decomposition_lenses_root"),
            "decomposition_lenses_root",
        ),
        "--decomposition-plan-bundle",
        str(attribution_bundle),
        "--correlation-root",
        _text(analytics.get("correlation_root"), "correlation_root"),
        "--weekly-returns-root",
        _text(analytics.get("weekly_root"), "weekly_root"),
        "--freshness-health",
        str(freshness_health),
        "--candidate-dir",
        str(candidate),
        timeout=7200,
    )
    return {**result, "candidate_dir": str(candidate), "root": str(output_root)}


def publish_panel(
    recipe_path: Path,
    report_root: Path,
    attribution_bundle: Path,
    analytics: Mapping[str, Any],
    freshness_health: Path,
    candidate: Path,
    qa_dir: Path,
    output_root: Path,
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    legacy_panel = _path(recipe["legacy_panel"]["path"], "legacy panel")
    result = _tool(
        "portfolio_control_panel.py",
        "publish",
        "--report-root",
        str(report_root),
        "--legacy-panel",
        str(legacy_panel),
        "--decomposition-root",
        _text(analytics.get("decomposition_root"), "decomposition_root"),
        "--decomposition-lenses-root",
        _text(
            analytics.get("decomposition_lenses_root"),
            "decomposition_lenses_root",
        ),
        "--decomposition-plan-bundle",
        str(attribution_bundle),
        "--correlation-root",
        _text(analytics.get("correlation_root"), "correlation_root"),
        "--weekly-returns-root",
        _text(analytics.get("weekly_root"), "weekly_root"),
        "--freshness-health",
        str(freshness_health),
        "--candidate-dir",
        str(candidate),
        "--qa-dir",
        str(qa_dir),
        "--output-root",
        str(output_root),
        timeout=7200,
    )
    return {**result, "candidate_dir": str(candidate), "root": str(output_root)}


def _stage_path(output_root: Path, role: str) -> Path:
    return output_root / "stage-results" / f"{role}.json"


def _stage_result(output_root: Path, role: str) -> dict[str, Any]:
    return _load(_stage_path(output_root, role), f"{role} stage result")


def _record_stage(output_root: Path, role: str, result: Mapping[str, Any]) -> None:
    _write_exact(_stage_path(output_root, role), canonical_json_bytes(dict(result)))


def build_stage(
    recipe_path: Path,
    output_root: Path,
    role: str,
    *,
    qa_dir: Path | None = None,
) -> dict[str, Any]:
    """Build exactly one source-to-panel stage from its predecessor outputs."""

    stages = output_root / "stages"
    recipe = _recipe(recipe_path)
    history_contract = _history_contract_path(recipe)
    implementation_reconciliation = _path(
        recipe["implementation_reconciliation"]["path"],
        "implementation reconciliation",
    )
    implementation_return_attribution = _path(
        recipe["implementation_return_attribution"]["path"],
        "implementation return attribution",
    )
    strategy_attribution_root = _path(
        recipe["strategy_attribution_root"]["path"],
        "strategy attribution root",
    )
    if role == "ledger":
        result = build_ledger(recipe_path, stages / "ledger")
    elif role == "baseline":
        _stage_result(output_root, "ledger")
        result = build_baseline(recipe_path, stages / "ledger", stages / "baseline")
    elif role == "attribution":
        _stage_result(output_root, "baseline")
        result = build_attribution(recipe_path, stages / "attribution")
    elif role == "analytics":
        attribution = _stage_result(output_root, "attribution")
        result = build_analytics(
            recipe_path,
            _path(attribution.get("destination"), "attribution destination"),
            stages / "analytics",
        )
    elif role == "report":
        ledger = _stage_result(output_root, "ledger")
        _stage_result(output_root, "analytics")
        result = build_report_candidate(
            stages / "baseline",
            stages / "ledger",
            _path(ledger.get("freshness_health"), "freshness health"),
            history_contract,
            implementation_reconciliation,
            implementation_return_attribution,
            strategy_attribution_root,
            stages / "report-candidate",
        )
    elif role == "workbook":
        ledger = _stage_result(output_root, "ledger")
        report = _stage_result(output_root, "report")
        result = build_workbook(
            stages / "baseline",
            stages / "ledger",
            _path(ledger.get("freshness_health"), "freshness health"),
            history_contract,
            implementation_reconciliation,
            implementation_return_attribution,
            strategy_attribution_root,
            _path(report.get("candidate_dir"), "report candidate"),
            stages / "report",
        )
    elif role in {"panel-candidate", "panel"}:
        ledger = _stage_result(output_root, "ledger")
        attribution = _stage_result(output_root, "attribution")
        analytics = _stage_result(output_root, "analytics")
        _stage_result(output_root, "workbook")
        candidate_result_path = _stage_path(output_root, "panel-candidate")
        panel = (
            _load(candidate_result_path, "panel candidate stage result")
            if candidate_result_path.is_file()
            else build_panel_candidate(
                recipe_path,
                stages / "report",
                _path(attribution.get("destination"), "attribution destination"),
                analytics,
                _path(ledger.get("freshness_health"), "freshness health"),
                stages / "panel",
            )
        )
        if role == "panel-candidate":
            result = panel
        elif qa_dir is not None:
            result = publish_panel(
                recipe_path,
                stages / "report",
                _path(attribution.get("destination"), "attribution destination"),
                analytics,
                _path(ledger.get("freshness_health"), "freshness health"),
                _path(panel.get("candidate_dir"), "panel candidate"),
                qa_dir,
                stages / "panel",
            )
        else:
            raise VerticalBuildError("panel publication requires browser QA")
    else:
        raise VerticalBuildError(f"unsupported vertical stage: {role}")
    _record_stage(output_root, role, result)
    return result


def build_all(recipe_path: Path, output_root: Path) -> dict[str, Any]:
    ledger = build_stage(recipe_path, output_root, "ledger")
    baseline = build_stage(recipe_path, output_root, "baseline")
    attribution = build_stage(recipe_path, output_root, "attribution")
    analytics = build_stage(recipe_path, output_root, "analytics")
    report_candidate = build_stage(recipe_path, output_root, "report")
    report = build_stage(recipe_path, output_root, "workbook")
    panel = build_stage(recipe_path, output_root, "panel-candidate")
    result = {
        "analytics": analytics,
        "attribution": attribution,
        "baseline": baseline,
        "ledger": ledger,
        "panel": panel,
        "release_commit": _recipe(recipe_path)["release_commit"],
        "report": report,
        "report_candidate": report_candidate,
        "schema_version": 1,
        "state": "PANEL_CANDIDATE_REQUIRES_BROWSER_QA",
    }
    state_path = output_root / "vertical-build.json"
    _write_exact(state_path, canonical_json_bytes(result))
    return {**result, "state_path": str(state_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--recipe", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--path", type=Path, required=True)
    recipe = commands.add_parser("recipe")
    provider = recipe.add_mutually_exclusive_group(required=True)
    provider.add_argument("--provider-ledger-spec", type=Path)
    provider.add_argument("--provider-ledger-generation", type=Path)
    recipe.add_argument("--baseline-spec", type=Path, required=True)
    recipe.add_argument("--base-factor-file", type=Path, required=True)
    recipe.add_argument("--implementation-reconciliation", type=Path, required=True)
    recipe.add_argument("--implementation-return-attribution", type=Path, required=True)
    recipe.add_argument("--strategy-attribution-root", type=Path, required=True)
    recipe.add_argument("--decomposition-plan-bundle", type=Path, required=True)
    recipe.add_argument("--correlation-source-root", type=Path, required=True)
    recipe.add_argument("--legacy-panel", type=Path, required=True)
    recipe.add_argument("--as-of-date", required=True)
    recipe.add_argument("--pipeline-id", required=True)
    recipe.add_argument("--release-commit", required=True)
    recipe.add_argument("--output", type=Path, required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--recipe", type=Path, required=True)
    stage.add_argument("--output-root", type=Path, required=True)
    stage.add_argument(
        "--role",
        choices=(
            "ledger",
            "baseline",
            "attribution",
            "analytics",
            "report",
            "workbook",
            "panel-candidate",
            "panel",
        ),
        required=True,
    )
    stage.add_argument("--qa-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_all(args.recipe, args.output_root)
        elif args.command == "inventory":
            result = _directory_inventory(args.path.resolve())
        elif args.command == "recipe":
            result = create_recipe(
                provider_ledger_spec=args.provider_ledger_spec,
                provider_ledger_generation=args.provider_ledger_generation,
                baseline_spec=args.baseline_spec,
                base_factor_file=args.base_factor_file,
                implementation_reconciliation=args.implementation_reconciliation,
                implementation_return_attribution=(
                    args.implementation_return_attribution
                ),
                strategy_attribution_root=args.strategy_attribution_root,
                decomposition_plan_bundle=args.decomposition_plan_bundle,
                correlation_source_root=args.correlation_source_root,
                legacy_panel=args.legacy_panel,
                as_of_date=args.as_of_date,
                pipeline_id=args.pipeline_id,
                release_commit=args.release_commit,
                output=args.output,
            )
        else:
            result = build_stage(
                args.recipe,
                args.output_root,
                args.role,
                qa_dir=args.qa_dir,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"portfolio-release-builder: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
