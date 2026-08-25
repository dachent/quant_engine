"""Build one private portfolio release vertically from immutable local inputs."""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
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
ACCOUNT_STRATEGY_CLASSIFICATION_POLICY = (
    REPOSITORY_ROOT
    / "contracts"
    / "account-strategy-flow-classification-policy-v1.json"
)
ACCOUNT_STRATEGY_PANEL_TEMPLATE_ROOT = (
    REPOSITORY_ROOT / "apps" / "portfolio-control-panel"
)
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


def _portable_filename(value: object, label: str) -> str:
    filename = _text(value, label)
    stem = filename.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        filename in {".", ".."}
        or any(character in filename for character in ("/", "\\", ":"))
        or filename.endswith((" ", "."))
        or any(ord(character) < 32 for character in filename)
        or stem in reserved
    ):
        raise VerticalBuildError(f"{label} must be a portable filename")
    return filename


def _generation_id(value: object, label: str) -> str:
    generation_id = _text(value, label)
    if len(generation_id) != 64 or any(
        character not in "0123456789abcdef" for character in generation_id
    ):
        raise VerticalBuildError(f"{label} is not a canonical generation id")
    return generation_id


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


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise VerticalBuildError(f"source artifact is unavailable: {path}") from exc
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse_point)


def _directory_inventory(path: Path) -> dict[str, Any]:
    if _is_link_or_reparse_point(path) or not path.is_dir():
        raise VerticalBuildError(f"source directory is unavailable: {path}")
    rows = []
    for item in sorted(path.rglob("*")):
        if _is_link_or_reparse_point(item):
            raise VerticalBuildError(f"source directory contains a link: {item}")
        if not item.is_file():
            continue
        sha256, size = _sha(item)
        rows.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": sha256,
                "size_bytes": size,
            }
        )
    content = (
        json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "inventory_sha256": hashlib.sha256(content).hexdigest(),
    }


def _panel_publication_receipt(
    recipe_path: Path, recipe: Mapping[str, Any], output_root: Path, candidate: Path, qa_dir: Path
) -> tuple[Path, dict[str, Any]]:
    recipe_sha256, recipe_size_bytes = _sha(recipe_path)
    release_commit = _text(recipe.get("release_commit"), "release_commit")
    release_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "pipeline_id": _text(recipe.get("pipeline_id"), "pipeline_id"),
                "recipe": {"sha256": recipe_sha256, "size_bytes": recipe_size_bytes},
                "release_commit": release_commit,
            }
        )
    ).hexdigest()
    receipt = {
        "schema_version": 1,
        "operation": "PUBLISH_PANEL_CURRENT",
        "state": "AUTHORIZED",
        "coordinator": "portfolio-release-builder",
        "release_id": release_id,
        "release_commit": release_commit,
        "output_root": str(output_root.resolve()),
        "candidate": {"path": str(candidate.resolve()), **_directory_inventory(candidate)},
        "qa": {"path": str(qa_dir.resolve()), **_directory_inventory(qa_dir)},
    }
    path = output_root / "stage-results" / "panel-publication-authorization.json"
    _write_exact(path, canonical_json_bytes(receipt))
    return path, receipt


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
    if set(files) != {
        "admit-or-declare-receipt.json",
        "generation.json",
        "provider-ledger.json",
    }:
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
        or manifest.adapter.version != "2"
    ):
        raise VerticalBuildError("reusable provider-ledger adapter differs")
    required_artifacts = {
        "normalized/admit-or-declare-receipt.json": (
            "admit-or-declare-receipt.json",
            "provider-admission-receipt",
            "1",
        ),
        "normalized/provider-ledger.json": (
            "provider-ledger.json",
            "provider-ledger-catalog",
            "2",
        ),
    }
    artifact_by_id = {item.artifact_id: item for item in manifest.artifacts}
    contents: dict[str, bytes] = {}
    for artifact_id, (
        relative_path,
        schema_name,
        schema_version,
    ) in required_artifacts.items():
        artifact = artifact_by_id.get(artifact_id)
        if artifact is None:
            raise VerticalBuildError(
                f"reusable provider-ledger artifact is absent: {artifact_id}"
            )
        try:
            content = files[relative_path].read_bytes()
        except OSError as exc:
            raise VerticalBuildError(
                "reusable provider-ledger artifact is unavailable"
            ) from exc
        if (
            artifact.sha256 != hashlib.sha256(content).hexdigest()
            or artifact.size_bytes != len(content)
            or artifact.schema_name != schema_name
            or artifact.schema_version != schema_version
        ):
            raise VerticalBuildError(
                f"reusable provider-ledger artifact identity differs: {artifact_id}"
            )
        contents[relative_path] = content
    content = contents["provider-ledger.json"]
    # Parse the SAME bytes that were just hashed — never reopen after hashing.
    try:
        catalog = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalBuildError(
            "reusable provider-ledger is not readable JSON"
        ) from exc
    if not isinstance(catalog, dict):
        raise VerticalBuildError("reusable provider-ledger root must be an object")
    if (
        catalog.get("as_of_date") != manifest.as_of_date.isoformat()
        or catalog.get("catalog_schema_version") != 2
    ):
        raise VerticalBuildError("reusable provider-ledger catalog contract differs")
    try:
        receipt = json.loads(contents["admit-or-declare-receipt.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerticalBuildError(
            "reusable provider-ledger admission receipt is not readable JSON"
        ) from exc
    if not isinstance(receipt, dict):
        raise VerticalBuildError(
            "reusable provider-ledger admission receipt root must be an object"
        )
    if (
        receipt.get("receipt_type") != "PROVIDER_ADMIT_OR_DECLARE_V1"
        or receipt.get("schema_version") != 1
        or receipt.get("admission_gate_status") != "PASS"
        or receipt.get("product_readiness")
        not in {"READY_FOR_COMPOSITION", "REQUIRED_NOT_READY"}
    ):
        raise VerticalBuildError(
            "reusable provider-ledger admission receipt contract differs"
        )
    return manifest.generation_id, files["provider-ledger.json"]


def _history_contract_path(recipe: Mapping[str, Any]) -> Path:
    correlation_root = _path(
        recipe["correlation_source_root"]["path"], "correlation source root"
    )
    current_receipt = correlation_root / "dependence-source.json"
    if current_receipt.exists():
        module_path = TOOLS_ROOT / "portfolio_current_dependence.py"
        module_name = "portfolio_current_dependence_for_release_builder"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise VerticalBuildError("current dependence verifier is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            result = module.verify_packaged_source(
                correlation_root,
                contract_path=REPOSITORY_ROOT
                / "contracts"
                / "portfolio-financial-mvp-source-v1.json",
                policy_path=REPOSITORY_ROOT
                / "contracts"
                / "portfolio-dependence-policy-v1.json",
            )
        except Exception as exc:
            raise VerticalBuildError(
                f"current dependence source is invalid: {exc}"
            ) from exc
        finally:
            sys.modules.pop(module_name, None)
        if result.get("verification") != "VALID":
            raise VerticalBuildError("current dependence source is not verified")
        return current_receipt
    if recipe.get("schema_version") != 6:
        raise VerticalBuildError(
            "current vertical build requires the verified dependence source"
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
    if schema_version not in {6, 7, 8}:
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
    if schema_version == 8:
        required.update(
            {
                "account_strategy_ledger",
                "account_strategy_ledger_identity",
                "account_strategy_classified_flows",
                "account_strategy_admission_policy",
            }
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
    if schema_version in {7, 8}:
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
    if schema_version == 8:
        for key in (
            "account_strategy_ledger",
            "account_strategy_ledger_identity",
            "account_strategy_classified_flows",
            "account_strategy_admission_policy",
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
        for path, label in (
            (ACCOUNT_STRATEGY_CLASSIFICATION_POLICY, "classification policy"),
            (ACCOUNT_STRATEGY_PANEL_TEMPLATE_ROOT, "panel templates"),
        ):
            if path.is_symlink() or not path.exists():
                raise VerticalBuildError(f"repository-governed {label} is unavailable")
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
    account_strategy_ledger: Path | None = None,
    account_strategy_ledger_identity: Path | None = None,
    account_strategy_classified_flows: Path | None = None,
    account_strategy_admission_policy: Path | None = None,
) -> dict[str, Any]:
    date.fromisoformat(as_of_date)
    if len(release_commit) != 40:
        raise VerticalBuildError("release commit differs")
    if (provider_ledger_spec is None) == (provider_ledger_generation is None):
        raise VerticalBuildError("exactly one provider-ledger source must be supplied")
    financial_sources = {
        "account_strategy_ledger": account_strategy_ledger,
        "account_strategy_ledger_identity": account_strategy_ledger_identity,
        "account_strategy_classified_flows": account_strategy_classified_flows,
        "account_strategy_admission_policy": account_strategy_admission_policy,
    }
    supplied_financial_sources = [
        value for value in financial_sources.values() if value is not None
    ]
    if supplied_financial_sources and len(supplied_financial_sources) != len(
        financial_sources
    ):
        raise VerticalBuildError(
            "all four account-strategy financial sources must be supplied together"
        )
    if supplied_financial_sources and provider_ledger_generation is None:
        raise VerticalBuildError(
            "financial epoch-manifest recipes require a reusable provider-ledger generation"
        )
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
    if supplied_financial_sources:
        payload.update(
            {
                key: _file_definition(value.resolve())
                for key, value in financial_sources.items()
                if value is not None
            }
        )
        payload["schema_version"] = 8
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


def _accepted_workbook_validation(path: Path, candidate: Path) -> dict[str, Any] | None:
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
    binding_path = candidate / "workbook-evidence-binding.json"
    if binding_path.is_symlink() or not binding_path.is_file():
        return None
    binding = _load(binding_path, "workbook evidence binding")
    if (
        binding.get("binding_schema_version") != 1
        or binding.get("validation_all_passed") is not True
    ):
        return None
    evidence_paths = {
        "workbook": candidate / "excel" / "portfolio-report.xlsx",
        "xlsx_result": candidate / "xlsx-result.json",
        "validation_result": path,
        "workbook_contract": candidate / "workbook-contract.json",
        "xlsx_job": candidate / "xlsx-job.json",
        "workbook_canonicalization": candidate / "workbook-canonicalization.json",
        "xlsx_result_canonicalization": candidate / "xlsx-result-canonicalization.json",
    }
    for name, evidence_path in evidence_paths.items():
        if evidence_path.is_symlink() or not evidence_path.is_file():
            return None
        sha256, size = _sha(evidence_path)
        if (
            binding.get(f"{name}_sha256") != sha256
            or binding.get(f"{name}_size_bytes") != size
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


def _legacy_release_plan_specs(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Admit only the pre-surface plan shape to the legacy release composer.

    Current static and dynamic surfaces are emitted as separate, hash-bound
    plan specifications.  The legacy release path keys outputs only by
    ``source_key`` and composes history through the superseded correlation
    adapter, so consuming a surface-qualified plan here would overwrite one
    surface and falsely promote historical dynamic evidence.
    """

    if (
        plan.get("builder") != "portfolio-current-decomposition-plan"
        or plan.get("builder_version") not in {"0.15.0", "0.16.0"}
        or plan.get("schema_version") != 1
    ):
        raise VerticalBuildError(
            "legacy release composer cannot admit a surface-qualified plan"
        )
    bundle_id = _text(plan.get("bundle_id"), "attribution plan bundle_id")
    identity = dict(plan)
    identity.pop("bundle_id")
    if hashlib.sha256(canonical_json_bytes(identity)).hexdigest() != bundle_id:
        raise VerticalBuildError("attribution plan bundle identity differs")
    specs = plan.get("specs")
    if not isinstance(specs, list) or not specs:
        raise VerticalBuildError("attribution plan has no lens specifications")
    rows: list[dict[str, Any]] = []
    source_keys: list[str] = []
    for row in specs:
        if not isinstance(row, dict):
            raise VerticalBuildError("attribution lens definition is invalid")
        if "surface" in row or "spec_id" in row:
            raise VerticalBuildError(
                "legacy release composer cannot admit a surface-qualified plan"
            )
        source_keys.append(_text(row.get("source_key"), "lens source_key"))
        rows.append(row)
    if len(source_keys) != len(set(source_keys)):
        raise VerticalBuildError("legacy attribution plan duplicates a source_key")
    return rows


_CURRENT_PLAN_SPEC_KEYS = {
    "analysis_lens",
    "as_of_date",
    "data_boundary",
    "model_calculation_boundaries",
    "model_count",
    "path",
    "sha256",
    "size_bytes",
    "source_key",
    "spec_id",
    "surface",
}
_CURRENT_PLAN_SURFACES = {"DYNAMIC_FLS", "STATIC"}
_WINDOWS_RESERVED_SEGMENTS = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_SURFACE_MODEL_ONLY_KEYS = {
    "constraint",
    "estimator",
    "ridge_candidates",
    "smoothness_candidates",
}


def _portable_bundle_file(bundle: Path, value: object, label: str) -> tuple[str, Path]:
    relative = _text(value, label)
    parts = relative.split("/")
    if (
        "\\" in relative
        or relative.startswith("/")
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise VerticalBuildError(f"{label} is not a portable relative path")
    if bundle.is_symlink() or not bundle.is_dir():
        raise VerticalBuildError("attribution plan bundle is not a regular directory")
    root = bundle.resolve()
    artifact = root.joinpath(*parts)
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise VerticalBuildError(f"{label} resolves through a link")
    try:
        resolved = artifact.resolve(strict=True)
    except OSError as exc:
        raise VerticalBuildError(f"{label} is unavailable") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise VerticalBuildError(f"{label} is not a regular bundle file")
    return relative, resolved


def _manifest_artifacts(
    bundle: Path, value: object, label: str
) -> dict[str, tuple[str, int]]:
    if not isinstance(value, list) or not value:
        raise VerticalBuildError(f"{label} are invalid")
    artifacts: dict[str, tuple[str, int]] = {}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "size_bytes"}:
            raise VerticalBuildError(f"{label} row is invalid")
        relative, artifact = _portable_bundle_file(
            bundle, raw.get("path"), f"{label} path"
        )
        sha256 = _text(raw.get("sha256"), f"{label} sha256")
        size = raw.get("size_bytes")
        if (
            len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise VerticalBuildError(f"{label} hash/size binding is invalid")
        if relative in artifacts:
            raise VerticalBuildError(f"{label} path is duplicated")
        if _sha(artifact) != (sha256, size):
            raise VerticalBuildError(f"{label} artifact differs: {relative}")
        artifacts[relative] = (sha256, size)
    return artifacts


def _portable_source_key(value: object) -> str:
    source_key = _text(value, "lens source_key")
    if (
        value != source_key
        or len(source_key) > 64
        or not all(
            character.isascii()
            and (character.isalnum() or character in {"-", "_", "."})
            for character in source_key
        )
        or source_key.endswith((".", " "))
        or source_key.split(".", 1)[0].upper() in _WINDOWS_RESERVED_SEGMENTS
    ):
        raise VerticalBuildError("lens source_key is not a portable path segment")
    return source_key


def _candidate_column(candidate: str, policy: str) -> str:
    if policy == "CANDIDATE_ID":
        return candidate
    if policy == "LOWER_CANDIDATE_ID":
        return candidate.lower()
    if policy == "REMOVE_EU_PREFIX_THEN_LOWER" and candidate.startswith("EU_"):
        return candidate.removeprefix("EU_").lower()
    raise VerticalBuildError("governed source column policy differs")


def _proxy_rationale(candidate_id: str, candidate: Mapping[str, Any], lens: str) -> str:
    family = str(candidate.get("economic_family") or "UNCLASSIFIED").strip()
    source_id = str(candidate.get("source_id") or "UNAVAILABLE_NOT_DECLARED").strip()
    return (
        f"{candidate_id} represents the declared {family} family for the {lens} "
        f"candidate universe using {source_id}; raw orthogonality is not assumed, "
        "and final inclusion is determined only by training-period validation, "
        "collinearity diagnostics, and the declared parsimony rule."
    )


def _csv_rows(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise VerticalBuildError(f"{label} is not readable CSV") from exc
    if not headers or not rows or len(headers) != len(set(headers)):
        raise VerticalBuildError(f"{label} boundary differs")
    return headers, rows


def _public_factor_output_alias(
    candidates: Sequence[tuple[str, Mapping[str, Any]]],
    frozen_path: Path,
    input_key: str,
    eligible_month_end: date,
    capture_evaluation_date: date,
) -> str:
    headers, rows = _csv_rows(frozen_path, f"public factor {input_key}")
    try:
        dates = [
            date.fromisoformat(_text(row.get("date"), "public factor date"))
            for row in rows
        ]
    except ValueError as exc:
        raise VerticalBuildError("public factor date differs") from exc
    first_date = dates[0].isoformat()
    last_date = dates[-1].isoformat()
    missing_cells = sum(
        not (row.get(column) or "").strip()
        for row in rows
        for column in headers
        if column != "date"
    )
    expected_coverage = (
        "PARTIAL"
        if input_key == "us_industries"
        else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    )
    publication_deadline = eligible_month_end + timedelta(days=45)
    matches: list[str] = []
    for alias_name, output in candidates:
        freshness = output.get("freshness")
        row_count = output.get("row_count")
        declared_missing_cells = output.get("missing_cells")
        if (
            not isinstance(freshness, dict)
            or output.get("coverage") != expected_coverage
            or output.get("declared_boundary") != "EXACT_DATE_INTERSECTION"
            or output.get("first_date") != first_date
            or output.get("last_date") != last_date
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count != len(rows)
            or isinstance(declared_missing_cells, bool)
            or not isinstance(declared_missing_cells, int)
            or declared_missing_cells != missing_cells
            or freshness.get("state") != "CURRENT"
            or freshness.get("accepted_observation_end") != last_date
            or freshness.get("eligible_month_end") != eligible_month_end.isoformat()
            or freshness.get("eligible_month") != eligible_month_end.strftime("%Y-%m")
            or (dates[-1].year, dates[-1].month)
            != (eligible_month_end.year, eligible_month_end.month)
            or freshness.get("publication_deadline") != publication_deadline.isoformat()
            or publication_deadline > capture_evaluation_date
        ):
            continue
        matches.append(alias_name)
    if not matches:
        raise VerticalBuildError("public factor manifest output binding differs")
    return min(matches)


def _csv_bytes(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def _complete_factor_dates(
    rows: Sequence[Mapping[str, str]],
    required_columns: Sequence[str],
    input_key: str,
) -> list[date]:
    risk_free = required_columns[0]
    result: list[date] = []
    seen: set[date] = set()
    previous: date | None = None
    completeness_started = False
    for row in rows:
        raw_date = _text(row.get("date"), "factor date")
        try:
            observed = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise VerticalBuildError("factor date differs") from exc
        if (
            observed.isoformat() != raw_date
            or observed in seen
            or (previous is not None and observed <= previous)
        ):
            raise VerticalBuildError("factor date differs")
        seen.add(observed)
        previous = observed
        missing_columns = {
            column
            for column in required_columns
            if not str(row.get(column) or "").strip()
        }
        if missing_columns:
            allowed_missing_after_start = (
                set(required_columns[1:]) if input_key == "us_industries" else set()
            )
            always_required = {risk_free} if input_key == "us_industries" else set()
            if missing_columns & always_required or (
                completeness_started
                and not missing_columns.issubset(allowed_missing_after_start)
            ):
                raise VerticalBuildError("factor source completeness boundary differs")
            continue
        completeness_started = True
        result.append(observed)
    if not result:
        raise VerticalBuildError("attribution plan model calendar differs")
    return result


def _expected_model_timing(
    attribution_bundle: Path,
    inputs: Mapping[str, tuple[str, int]],
    specification: Mapping[str, Any],
    source_key: str,
    expected_models: Mapping[str, Mapping[str, Any]],
    source_contract: Mapping[str, Any],
    *,
    evaluation_date: str,
    holdout_fraction: str,
) -> dict[str, dict[str, Any]]:
    factor_source = specification.get("factor_source")
    target_source = specification.get("target_source")
    if not isinstance(factor_source, dict) or not isinstance(target_source, dict):
        raise VerticalBuildError("attribution plan model sources differ")
    factor_relative = f"inputs/lenses/{source_key}-factors.csv"
    target_relative = f"inputs/lenses/{source_key}-targets.csv"
    if (
        set(factor_source)
        != {
            "artifact_id",
            "date_column",
            "expected_sha256",
            "expected_size_bytes",
            "factors",
            "relative_path",
            "risk_free_column",
            "schema_name",
            "schema_version",
        }
        or set(target_source)
        != {
            "artifact_id",
            "expected_sha256",
            "expected_size_bytes",
            "relative_path",
            "schema_name",
            "schema_version",
        }
        or factor_source.get("artifact_id") != f"market/{source_key}.csv"
        or factor_source.get("schema_name") != f"portfolio-{source_key}-returns"
        or factor_source.get("schema_version") != "1"
        or target_source.get("artifact_id") != f"target/{source_key}.csv"
        or target_source.get("schema_name") != "portfolio-decomposition-target-series"
        or target_source.get("schema_version") != "1"
        or factor_source.get("relative_path") != factor_relative
        or target_source.get("relative_path") != target_relative
        or factor_source.get("risk_free_column")
        != source_contract.get("risk_free_column")
    ):
        raise VerticalBuildError("attribution plan model sources differ")
    _, factor_path = _portable_bundle_file(
        attribution_bundle, factor_relative, "factor source path"
    )
    _, target_path = _portable_bundle_file(
        attribution_bundle, target_relative, "target source path"
    )
    if (
        inputs.get(factor_relative) != _sha(factor_path)
        or inputs.get(target_relative) != _sha(target_path)
        or factor_source.get("expected_sha256") != inputs[factor_relative][0]
        or factor_source.get("expected_size_bytes") != inputs[factor_relative][1]
        or target_source.get("expected_sha256") != inputs[target_relative][0]
        or target_source.get("expected_size_bytes") != inputs[target_relative][1]
    ):
        raise VerticalBuildError("attribution plan model source binding differs")
    input_key = _text(source_contract.get("input_key"), "source input_key")
    raw_factor_relative = f"inputs/market/{input_key}.csv"
    raw_target_relative = "inputs/history/selected-targets.csv"
    _, raw_factor_path = _portable_bundle_file(
        attribution_bundle, raw_factor_relative, "raw factor source path"
    )
    _, raw_target_path = _portable_bundle_file(
        attribution_bundle, raw_target_relative, "raw target source path"
    )
    if inputs.get(raw_factor_relative) != _sha(raw_factor_path) or inputs.get(
        raw_target_relative
    ) != _sha(raw_target_path):
        raise VerticalBuildError("attribution plan raw model source binding differs")
    candidates = source_contract.get("candidates")
    column_policy = source_contract.get("column_policy")
    factor_definitions = factor_source.get("factors")
    if (
        not isinstance(candidates, list)
        or not isinstance(column_policy, str)
        or not isinstance(factor_definitions, list)
    ):
        raise VerticalBuildError("attribution plan factor definitions differ")
    columns_by_factor: dict[str, str] = {}
    for definition in factor_definitions:
        if not isinstance(definition, dict):
            raise VerticalBuildError("attribution plan factor definitions differ")
        factor_id = _text(definition.get("factor_id"), "factor definition factor_id")
        column = _text(definition.get("column"), "factor definition column")
        if factor_id in columns_by_factor:
            raise VerticalBuildError("attribution plan factor definitions differ")
        columns_by_factor[factor_id] = column
    if columns_by_factor != {
        candidate: _candidate_column(candidate, column_policy)
        for candidate in candidates
    }:
        raise VerticalBuildError("attribution plan factor definitions differ")

    projected_factor_headers, _projected_factor_rows = _csv_rows(
        factor_path, "projected factor source"
    )
    projected_target_headers, _projected_target_rows = _csv_rows(
        target_path, "projected target source"
    )
    factor_headers, factor_rows = _csv_rows(raw_factor_path, "raw factor source")
    target_headers, target_rows = _csv_rows(raw_target_path, "raw target source")
    risk_free = _text(source_contract.get("risk_free_column"), "risk-free column")
    if (
        factor_source.get("date_column") != "date"
        or "date" not in factor_headers
        or "date" not in projected_factor_headers
        or risk_free not in factor_headers
        or risk_free not in projected_factor_headers
        or not {"series_id", "date"}.issubset(target_headers)
        or not {"series_id", "date"}.issubset(projected_target_headers)
    ):
        raise VerticalBuildError("attribution plan model source columns differ")
    target_dates: dict[str, set[date]] = {}
    for row in target_rows:
        series_id = _text(row.get("series_id"), "target series_id")
        raw_date = _text(row.get("date"), "target date")
        try:
            observed = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise VerticalBuildError("target date differs") from exc
        if observed.isoformat() != raw_date or observed in target_dates.setdefault(
            series_id, set()
        ):
            raise VerticalBuildError("target date differs")
        target_dates[series_id].add(observed)
    try:
        evaluated_on = date.fromisoformat(evaluation_date)
        fraction = Decimal(holdout_fraction)
    except (ValueError, InvalidOperation) as exc:
        raise VerticalBuildError("attribution plan holdout policy differs") from exc
    result: dict[str, dict[str, Any]] = {}
    factor_dates_by_model: dict[str, list[date]] = {}
    for base_model_id, expected in expected_models.items():
        series_id = _text(expected.get("series_id"), "governed model series_id")
        factor_ids = expected.get("factor_ids")
        if not isinstance(factor_ids, list) or series_id not in target_dates:
            raise VerticalBuildError("attribution plan governed model identity differs")
        required_columns = [
            risk_free,
            *[
                columns_by_factor[_text(item, "governed factor_id")]
                for item in factor_ids
            ],
        ]
        if not set(required_columns).issubset(factor_headers):
            raise VerticalBuildError("attribution plan model source columns differ")
        model_dates = _complete_factor_dates(factor_rows, required_columns, input_key)
        factor_dates_by_model[base_model_id] = model_dates
        common = sorted(target_dates[series_id].intersection(model_dates))
        common = [observed for observed in common if observed <= evaluated_on]
        if not common:
            raise VerticalBuildError("attribution plan model calendar differs")
        model_as_of = common[-1]
        test_count = max(
            int(
                (Decimal(len(common)) * fraction).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
            int(expected["minimum_test_observations"]),
        )
        if len(common) - test_count < 3:
            raise VerticalBuildError("attribution plan model calendar differs")
        limiting_inputs: list[str] = []
        if max(target_dates[series_id]) == model_as_of:
            limiting_inputs.append(f"target:{series_id}")
        if max(model_dates) == model_as_of:
            limiting_inputs.append(
                f"factor:{input_key}:{','.join(factor_ids)}:{risk_free}"
            )
        if not limiting_inputs:
            limiting_inputs = [
                f"target:{series_id}",
                f"factor:{input_key}:{','.join(factor_ids)}:{risk_free}",
            ]
        result[base_model_id] = {
            "as_of_date": model_as_of.isoformat(),
            "limiting_inputs": sorted(limiting_inputs),
            "test_start": common[-test_count].isoformat(),
            "train_end": common[-test_count - 1].isoformat(),
        }
    required_factor_columns = [
        risk_free,
        *[columns_by_factor[candidate] for candidate in candidates],
    ]
    target_columns = (
        "series_id",
        "date",
        "return",
        "evidence_layer",
        "return_basis",
        "return_type",
        "cost",
    )
    if (
        not set(("date", *required_factor_columns)).issubset(factor_headers)
        or not set(target_columns).issubset(target_headers)
        or projected_factor_headers != ["date", *required_factor_columns]
        or projected_target_headers != list(target_columns)
    ):
        raise VerticalBuildError("attribution plan projection columns differ")
    factor_input_start = min(
        min(model_dates) for model_dates in factor_dates_by_model.values()
    )
    analysis_as_of = max(
        date.fromisoformat(timing["as_of_date"]) for timing in result.values()
    )
    candidate_records = source_contract.get("candidate_records")
    lens = _text(source_contract.get("lens"), "governed source lens")
    return_type = _text(
        source_contract.get("return_type"), "governed source return_type"
    )
    if not isinstance(candidate_records, dict):
        raise VerticalBuildError("attribution plan factor definitions differ")
    expected_factor_definitions: list[dict[str, Any]] = []
    for candidate_id in candidates:
        candidate = candidate_records.get(candidate_id)
        if not isinstance(candidate, dict):
            raise VerticalBuildError("attribution plan factor definitions differ")
        column = columns_by_factor[candidate_id]
        effective_dates = [
            date.fromisoformat(row["date"])
            for row in factor_rows
            if str(row.get(column) or "").strip()
            and str(row.get(risk_free) or "").strip()
            and factor_input_start <= date.fromisoformat(row["date"]) <= analysis_as_of
        ]
        raw_orthogonal = candidate.get("raw_orthogonal", False)
        if not effective_dates or not isinstance(raw_orthogonal, bool):
            raise VerticalBuildError("attribution plan factor definitions differ")
        source_id = str(
            candidate.get("source_id") or "UNAVAILABLE_NOT_DECLARED"
        ).strip()
        expected_factor_definitions.append(
            {
                "column": column,
                "economic_family": str(
                    candidate.get("economic_family") or "UNCLASSIFIED"
                ).strip(),
                "effective_end": effective_dates[-1].isoformat(),
                "effective_start": effective_dates[0].isoformat(),
                "factor_id": candidate_id,
                "raw_orthogonal": raw_orthogonal,
                "redundancy_policy": (
                    "TRAIN_ONLY_NEAR_DUPLICATE_VIF_CONDITION_NUMBER_AND_PARSIMONY_GATES"
                ),
                "return_source_id": str(
                    candidate.get("return_source_id") or source_id
                ).strip(),
                "return_type": return_type,
                "selection_rationale": _proxy_rationale(candidate_id, candidate, lens),
                "source_id": source_id,
            }
        )
    if factor_definitions != expected_factor_definitions:
        raise VerticalBuildError("attribution plan factor definitions differ")
    expected_factor_content = _csv_bytes(
        ("date", *required_factor_columns),
        [
            [row["date"], *(row[column] for column in required_factor_columns)]
            for row in factor_rows
            if factor_input_start <= date.fromisoformat(row["date"]) <= analysis_as_of
            and row[risk_free].strip()
        ],
    )
    assigned_series = {
        _text(expected.get("series_id"), "governed model series_id")
        for expected in expected_models.values()
    }
    expected_target_content = _csv_bytes(
        target_columns,
        [
            [row[column] for column in target_columns]
            for row in target_rows
            if row["series_id"] in assigned_series
            and date.fromisoformat(row["date"]) <= analysis_as_of
        ],
    )
    if (
        factor_path.read_bytes() != expected_factor_content
        or target_path.read_bytes() != expected_target_content
    ):
        raise VerticalBuildError("attribution plan model projection differs")
    return result


def _surface_population(
    row: Mapping[str, Any],
    specification: Mapping[str, Any],
    surface: str,
    expected_models: Mapping[str, Mapping[str, Any]],
    expected_timing: Mapping[str, Mapping[str, Any]],
    model_policy: Mapping[str, Any],
    evaluation_date: str,
) -> dict[str, Any]:
    if specification.get("maximum_factor_staleness_days") != 0:
        raise VerticalBuildError("attribution plan factor staleness differs")
    models = specification.get("models")
    if not isinstance(models, list):
        raise VerticalBuildError("attribution plan surface models are invalid")
    suffix = "-static" if surface == "STATIC" else "-dynamic-fls"
    normalized_models: dict[str, dict[str, Any]] = {}
    model_ids: set[str] = set()
    model_as_of_dates: dict[str, str] = {}
    timing_by_model_id: dict[str, Mapping[str, Any]] = {}
    for raw_model in models:
        if not isinstance(raw_model, dict):
            raise VerticalBuildError("attribution plan surface model is invalid")
        model_id = _text(raw_model.get("model_id"), "surface model_id")
        if not model_id.endswith(suffix) or len(model_id) == len(suffix):
            raise VerticalBuildError("attribution plan surface model_id differs")
        base_model_id = model_id[: -len(suffix)]
        expected = expected_models.get(base_model_id)
        timing = expected_timing.get(base_model_id)
        if expected is None or timing is None or base_model_id in normalized_models:
            raise VerticalBuildError("attribution plan governed model identity differs")
        common_keys = {
            "as_of_date",
            "constraint",
            "cost_coverage",
            "cv_folds",
            "estimator",
            "factor_ids",
            "minimum_test_observations",
            "minimum_train_observations",
            "model_id",
            "parsimony_tolerance",
            "periods_per_year",
            "ridge_candidates",
            "series_id",
            "test_start",
            "train_end",
        }
        expected_keys = (
            common_keys | {"smoothness_candidates"}
            if surface == "DYNAMIC_FLS"
            else common_keys
        )
        if set(raw_model) != expected_keys or any(
            raw_model.get(key) != value
            for key, value in expected.items()
            if key != "static_constraint"
        ):
            raise VerticalBuildError("attribution plan governed model identity differs")
        if surface == "STATIC":
            expected_constraint = expected.get("static_constraint")
            expected_ridge = model_policy.get("static_ridge_candidates")
        else:
            expected_constraint = model_policy.get("dynamic_constraint")
            expected_ridge = [model_policy.get("dynamic_state_ridge")]
        if (
            raw_model.get("constraint") != expected_constraint
            or raw_model.get("ridge_candidates") != expected_ridge
            or (
                surface == "DYNAMIC_FLS"
                and raw_model.get("smoothness_candidates")
                != model_policy.get("dynamic_smoothness_candidates")
            )
        ):
            raise VerticalBuildError("attribution plan dynamic model policy differs")
        as_of_date = _text(raw_model.get("as_of_date"), "surface model as_of_date")
        train_end = _text(raw_model.get("train_end"), "surface model train_end")
        test_start = _text(raw_model.get("test_start"), "surface model test_start")
        try:
            parsed_as_of = date.fromisoformat(as_of_date)
            parsed_train_end = date.fromisoformat(train_end)
            parsed_test_start = date.fromisoformat(test_start)
            parsed_evaluation = date.fromisoformat(evaluation_date)
        except ValueError as exc:
            raise VerticalBuildError("attribution plan model date differs") from exc
        if (
            parsed_as_of.isoformat() != as_of_date
            or parsed_train_end.isoformat() != train_end
            or parsed_test_start.isoformat() != test_start
            or parsed_evaluation.isoformat() != evaluation_date
            or not (parsed_train_end < parsed_test_start <= parsed_as_of)
            or parsed_as_of > parsed_evaluation
        ):
            raise VerticalBuildError("attribution plan model date differs")
        if any(
            raw_model.get(key) != timing[key]
            for key in ("as_of_date", "test_start", "train_end")
        ):
            raise VerticalBuildError("attribution plan holdout split differs")
        model_as_of_dates[model_id] = as_of_date
        timing_by_model_id[model_id] = timing
        if base_model_id in normalized_models:
            raise VerticalBuildError("attribution plan surface model is duplicated")
        normalized = {
            key: value
            for key, value in raw_model.items()
            if key not in _SURFACE_MODEL_ONLY_KEYS and key != "model_id"
        }
        normalized["model_id"] = base_model_id
        normalized_models[base_model_id] = normalized
        model_ids.add(model_id)
    boundaries = row.get("model_calculation_boundaries")
    if not isinstance(boundaries, dict) or set(boundaries) != model_ids:
        raise VerticalBuildError("attribution plan model calculation boundaries differ")
    normalized_boundaries: dict[str, Any] = {}
    for model_id, value in boundaries.items():
        _validate_age_boundary(
            value,
            evaluation_date=evaluation_date,
            expected_complete_through=model_as_of_dates[model_id],
            expected_limiting_inputs=timing_by_model_id[model_id]["limiting_inputs"],
            expected_warning_after_days=0,
            label="attribution plan model data age boundary",
        )
        normalized_boundaries[model_id[: -len(suffix)]] = value
    if set(normalized_models) != set(expected_models):
        raise VerticalBuildError("attribution plan governed model identity differs")
    data_complete_through = min(model_as_of_dates.values())
    expected_limiting_inputs = sorted(
        f"model:{model_id[: -len(suffix)]}"
        for model_id, model_as_of in model_as_of_dates.items()
        if model_as_of == data_complete_through
    )
    _validate_age_boundary(
        row.get("data_boundary"),
        evaluation_date=evaluation_date,
        expected_complete_through=data_complete_through,
        expected_limiting_inputs=expected_limiting_inputs,
        expected_warning_after_days=0,
        label="attribution plan data age boundary",
    )
    if specification.get("as_of_date") != max(model_as_of_dates.values()):
        raise VerticalBuildError("attribution plan model date differs")
    return {
        "data_boundary": row.get("data_boundary"),
        "model_calculation_boundaries": normalized_boundaries,
        "models": normalized_models,
        "specification": {
            key: value for key, value in specification.items() if key != "models"
        },
    }


def _validate_age_boundary(
    value: object,
    *,
    evaluation_date: str,
    expected_complete_through: str,
    label: str,
    expected_limiting_inputs: Sequence[str] | None = None,
    expected_warning_after_days: int | None = None,
) -> None:
    expected_keys = {
        "age_days",
        "complete_through",
        "evaluated_on",
        "limiting_inputs",
        "status",
        "warning_after_days",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise VerticalBuildError(f"{label} differs")
    age_days = value.get("age_days")
    warning_after_days = value.get("warning_after_days")
    limiting_inputs = value.get("limiting_inputs")
    if (
        not isinstance(age_days, int)
        or isinstance(age_days, bool)
        or age_days < 0
        or not isinstance(warning_after_days, int)
        or isinstance(warning_after_days, bool)
        or warning_after_days < 0
        or not isinstance(limiting_inputs, list)
        or not limiting_inputs
        or any(
            not isinstance(item, str) or not item.strip() for item in limiting_inputs
        )
        or limiting_inputs != sorted(set(limiting_inputs))
    ):
        raise VerticalBuildError(f"{label} differs")
    complete_through = _text(value.get("complete_through"), f"{label} complete")
    evaluated_on = _text(value.get("evaluated_on"), f"{label} evaluated")
    try:
        complete_date = date.fromisoformat(complete_through)
        evaluated_date = date.fromisoformat(evaluated_on)
    except ValueError as exc:
        raise VerticalBuildError(f"{label} differs") from exc
    expected_status = (
        "WARN_COMPLETE_DATA_AGE"
        if age_days > warning_after_days
        else "CURRENT_WITHIN_AGE_THRESHOLD"
    )
    if (
        complete_date.isoformat() != complete_through
        or evaluated_date.isoformat() != evaluated_on
        or evaluated_on != evaluation_date
        or complete_through != expected_complete_through
        or age_days != (evaluated_date - complete_date).days
        or value.get("status") != expected_status
        or (
            expected_warning_after_days is not None
            and warning_after_days != expected_warning_after_days
        )
        or (
            expected_limiting_inputs is not None
            and limiting_inputs != list(expected_limiting_inputs)
        )
    ):
        raise VerticalBuildError(f"{label} differs")


def _age_boundary_value(
    *,
    complete_through: date,
    evaluated_on: date,
    warning_after_days: int,
    limiting_inputs: Sequence[str],
) -> dict[str, Any]:
    age_days = (evaluated_on - complete_through).days
    if age_days < 0:
        raise VerticalBuildError("attribution plan data age differs")
    return {
        "age_days": age_days,
        "complete_through": complete_through.isoformat(),
        "evaluated_on": evaluated_on.isoformat(),
        "limiting_inputs": sorted(set(limiting_inputs)),
        "status": (
            "WARN_COMPLETE_DATA_AGE"
            if age_days > warning_after_days
            else "CURRENT_WITHIN_AGE_THRESHOLD"
        ),
        "warning_after_days": warning_after_days,
    }


def _expected_non_spec_data_age(
    attribution_bundle: Path,
    inputs: Mapping[str, tuple[str, int]],
    governed_sources: Mapping[str, str],
    model_policy: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    evaluation_date: str,
    assurance_profile: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evaluated_on = date.fromisoformat(evaluation_date)
    public_manifest_relative = "inputs/governance/public-factor-source-manifest.json"
    _, public_manifest_path = _portable_bundle_file(
        attribution_bundle, public_manifest_relative, "public factor manifest path"
    )
    if inputs.get(public_manifest_relative) != _sha(public_manifest_path):
        raise VerticalBuildError("public factor manifest binding differs")
    public_manifest = _load(public_manifest_path, "public factor manifest")
    acquisition_run = public_manifest.get("acquisition_run")
    if (
        public_manifest.get("schema_version") != 2
        or public_manifest.get("builder") != "portfolio-public-decomposition-sources"
        or public_manifest.get("builder_version") != "0.3.0"
        or not isinstance(acquisition_run, dict)
    ):
        raise VerticalBuildError("public factor manifest contract differs")
    capture_evaluation_text = _text(
        acquisition_run.get("evaluation_date"), "public factor evaluation_date"
    )
    try:
        capture_evaluation_date = date.fromisoformat(capture_evaluation_text)
    except ValueError as exc:
        raise VerticalBuildError("public factor evaluation date differs") from exc
    if (
        capture_evaluation_date.isoformat() != capture_evaluation_text
        or capture_evaluation_date > evaluated_on
        or (
            assurance_profile == "RELEASE_QC"
            and capture_evaluation_date != evaluated_on
        )
    ):
        raise VerticalBuildError("public factor evaluation date differs")
    public_factor_capture = _age_boundary_value(
        complete_through=capture_evaluation_date,
        evaluated_on=evaluated_on,
        warning_after_days=0,
        limiting_inputs=("public-factor-acquisition-run",),
    )

    source_contracts = model_policy.get("source_contracts")
    if not isinstance(source_contracts, dict):
        raise VerticalBuildError("governed source runtime contract differs")
    source_segments = contract.get("source_segments")
    if not isinstance(source_segments, list):
        raise VerticalBuildError("financial MVP source contract differs")
    contract_sources: dict[str, Mapping[str, Any]] = {}
    for raw_source in source_segments:
        if not isinstance(raw_source, dict):
            raise VerticalBuildError("financial MVP source contract differs")
        source_id = _text(raw_source.get("source_id"), "financial MVP source_id")
        if source_id in contract_sources:
            raise VerticalBuildError("financial MVP source contract differs")
        contract_sources[source_id] = raw_source
    market_contract_source_ids = {
        "style_returns": "public-etf-style-sector-and-market-prices",
        "sector_returns": "public-etf-style-sector-and-market-prices",
    }
    expected_market_sources: dict[str, dict[str, Any]] = {}
    for source_key in governed_sources:
        source_contract = source_contracts.get(source_key)
        if not isinstance(source_contract, dict):
            raise VerticalBuildError("governed source runtime contract differs")
        input_key = _text(source_contract.get("input_key"), "source input_key")
        contract_source_id = market_contract_source_ids.get(input_key)
        if contract_source_id is None:
            continue
        raw_relative = f"inputs/market/{input_key}.csv"
        _, raw_path = _portable_bundle_file(
            attribution_bundle, raw_relative, "market data-age source path"
        )
        if inputs.get(raw_relative) != _sha(raw_path):
            raise VerticalBuildError("market data-age source binding differs")
        headers, rows = _csv_rows(raw_path, "market data-age source")
        candidates = source_contract.get("candidates")
        column_policy = source_contract.get("column_policy")
        risk_free = _text(
            source_contract.get("risk_free_column"), "source risk-free column"
        )
        if not isinstance(candidates, list) or not isinstance(column_policy, str):
            raise VerticalBuildError("market data-age source contract differs")
        required_columns = [
            risk_free,
            *[
                _candidate_column(_text(candidate, "candidate_id"), column_policy)
                for candidate in candidates
            ],
        ]
        if not set(("date", *required_columns)).issubset(headers):
            raise VerticalBuildError("market data-age source columns differ")
        complete_dates = _complete_factor_dates(rows, required_columns, input_key)
        contract_source = contract_sources.get(contract_source_id)
        if not isinstance(contract_source, dict):
            raise VerticalBuildError("financial MVP market freshness differs")
        freshness = contract_source.get("freshness")
        if not isinstance(freshness, dict):
            raise VerticalBuildError("financial MVP market freshness differs")
        maximum_age = freshness.get("max_age_days")
        if (
            not isinstance(maximum_age, int)
            or isinstance(maximum_age, bool)
            or maximum_age < 0
        ):
            raise VerticalBuildError("financial MVP market freshness differs")
        expected = {
            **_age_boundary_value(
                complete_through=complete_dates[-1],
                evaluated_on=evaluated_on,
                warning_after_days=maximum_age,
                limiting_inputs=(f"factor:{input_key}",),
            ),
            "input_key": input_key,
            "source_id": contract_source_id,
        }
        prior = expected_market_sources.setdefault(input_key, expected)
        if prior != expected:
            raise VerticalBuildError("market data age differs across plans")
        if assurance_profile == "RELEASE_QC" and (
            expected["status"] == "WARN_COMPLETE_DATA_AGE"
            or contract_source.get("readiness") != "READY"
        ):
            raise VerticalBuildError("release QC market data age differs")
    return (
        [expected_market_sources[key] for key in sorted(expected_market_sources)],
        public_factor_capture,
    )


def _provenance_definition(
    artifact_id: str,
    relative_path: str,
    content: bytes,
    schema_name: str,
    schema_version: str = "1",
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_size_bytes": len(content),
        "relative_path": relative_path,
        "schema_name": schema_name,
        "schema_version": schema_version,
    }


def _verify_current_plan_upstream_evidence(
    attribution_bundle: Path,
    inputs: Mapping[str, tuple[str, int]],
    plan: Mapping[str, Any],
    *,
    evaluation_date: str,
    evidence_mode: str,
    assurance_profile: str,
) -> dict[str, Any]:
    module_name = "portfolio_current_decomposition_plan_for_release_builder"
    module_path = TOOLS_ROOT / "portfolio_current_decomposition_plan.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise VerticalBuildError(
            "current decomposition planner verifier is unavailable"
        )
    planner = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = planner
    try:
        spec.loader.exec_module(planner)
        evaluated_on = date.fromisoformat(evaluation_date)
        catalog_relative = "inputs/governance/decomposition-source-catalog.json"
        catalog_path = attribution_bundle / catalog_relative
        catalog_content = catalog_path.read_bytes()
        frozen_market_paths = {
            key: attribution_bundle / f"inputs/market/{key}.csv"
            for key in planner.INPUT_KEYS
        }
        public_manifest_path = (
            attribution_bundle / "inputs/governance/public-factor-source-manifest.json"
        )
        style_manifest_path = (
            attribution_bundle / "inputs/governance/style-source-manifest.json"
        )
        sector_manifest_path = (
            attribution_bundle / "inputs/governance/sector-source-manifest.json"
        )
        catalog_sha256 = hashlib.sha256(catalog_content).hexdigest()
        public_manifest = _load(public_manifest_path, "public factor manifest")
        public_outputs = public_manifest.get("outputs")
        style_manifest = _load(style_manifest_path, "style source manifest")
        sector_manifest = _load(sector_manifest_path, "sector source manifest")
        if not isinstance(public_outputs, list):
            raise VerticalBuildError("public factor manifest outputs differ")
        with tempfile.TemporaryDirectory(prefix="quant-engine-plan-verifier-") as raw:
            alias_root = Path(raw)
            public_output_index: dict[
                tuple[str, int], list[tuple[str, Mapping[str, Any]]]
            ] = {}
            for output in public_outputs:
                if not isinstance(output, dict):
                    continue
                output_sha256 = output.get("sha256")
                output_size = output.get("bytes")
                if (
                    not isinstance(output_sha256, str)
                    or isinstance(output_size, bool)
                    or not isinstance(output_size, int)
                ):
                    continue
                try:
                    alias_name = _portable_filename(
                        output.get("path"), "public factor output path"
                    )
                except VerticalBuildError:
                    continue
                public_output_index.setdefault((output_sha256, output_size), []).append(
                    (alias_name, output)
                )
            acquisition_run = public_manifest.get("acquisition_run")
            if not isinstance(acquisition_run, dict):
                raise VerticalBuildError("public factor acquisition run differs")
            try:
                capture_evaluation_date = date.fromisoformat(
                    _text(
                        acquisition_run.get("evaluation_date"),
                        "public factor evaluation date",
                    )
                )
            except ValueError as exc:
                raise VerticalBuildError(
                    "public factor evaluation date differs"
                ) from exc
            eligible_month_end = planner._required_observation_month_end(
                capture_evaluation_date
            )
            market_paths: dict[str, Path] = {}
            for key in planner.PUBLIC_FACTOR_INPUT_KEYS:
                frozen_path = frozen_market_paths[key]
                frozen_sha256, frozen_size = _sha(frozen_path)
                candidates = public_output_index.get((frozen_sha256, frozen_size), [])
                alias_name = _public_factor_output_alias(
                    candidates,
                    frozen_path,
                    key,
                    eligible_month_end,
                    capture_evaluation_date,
                )
                alias_path = alias_root / "public" / key / alias_name
                alias_path.parent.mkdir(parents=True)
                shutil.copyfile(frozen_path, alias_path)
                market_paths[key] = alias_path
            _public_boundaries, public_capture = planner._verify_public_factor_manifest(
                public_manifest_path,
                market_paths,
                catalog_sha256,
                evaluated_on,
                assurance_profile,
            )
            for key, manifest in (
                ("style_returns", style_manifest),
                ("sector_returns", sector_manifest),
            ):
                frozen_path = frozen_market_paths[key]
                frozen_sha256, frozen_size = _sha(frozen_path)
                raw_outputs = manifest.get("outputs")
                alias_name = f"{key}.csv"
                if isinstance(raw_outputs, list):
                    safe_matches: list[tuple[str, dict[str, Any]]] = []
                    for candidate in raw_outputs:
                        if (
                            not isinstance(candidate, dict)
                            or candidate.get("sha256") != frozen_sha256
                            or candidate.get("bytes") != frozen_size
                        ):
                            continue
                        try:
                            candidate_name = _portable_filename(
                                candidate.get("path"), "market source output path"
                            )
                        except VerticalBuildError:
                            continue
                        safe_matches.append((candidate_name, candidate))
                    safe_matches.sort(key=lambda item: item[0])
                    if not safe_matches:
                        raise VerticalBuildError(
                            "market source manifest output differs"
                        )
                    alias_name = safe_matches[0][0]
                else:
                    singular_output = manifest.get("output")
                    if (
                        not isinstance(singular_output, dict)
                        or singular_output.get("sha256") != frozen_sha256
                        or singular_output.get("size_bytes") != frozen_size
                    ):
                        raise VerticalBuildError(
                            "market source manifest output differs"
                        )
                alias_path = alias_root / key / alias_name
                alias_path.parent.mkdir()
                shutil.copyfile(frozen_path, alias_path)
                market_paths[key] = alias_path
            planner._verify_market_manifest(
                market_paths["style_returns"], style_manifest_path, catalog_sha256
            )
            planner._verify_market_manifest(
                market_paths["sector_returns"], sector_manifest_path, catalog_sha256
            )
        governed_plan_relative = (
            "inputs/governance/current-strategy-decomposition-plan.json"
        )
        governed_plan_path = attribution_bundle / governed_plan_relative
        governed_plan = _load(governed_plan_path, "governed current strategy plan")
        strategies = planner._resolved_strategies(governed_plan)
        history_root = attribution_bundle / "inputs/history"
        contract_path = history_root / "financial-mvp-contract.json"
        generation = _load(history_root / "generation.json", "layered generation")
        generation_outputs = generation.get("outputs")
        if not isinstance(generation_outputs, dict):
            raise VerticalBuildError("layered generation outputs differ")
        generation_id = _generation_id(
            generation.get("generation_id"), "layered generation_id"
        )
        with tempfile.TemporaryDirectory(
            prefix="quant-engine-history-verifier-"
        ) as raw:
            verification_root = Path(raw) / generation_id
            verification_root.mkdir()
            shutil.copyfile(
                history_root / "generation.json",
                verification_root / "generation.json",
            )
            for name, output in generation_outputs.items():
                portable_name = PurePosixPath(
                    _text(name, "layered generation output name")
                )
                if len(portable_name.parts) != 1 or not isinstance(output, dict):
                    raise VerticalBuildError("layered generation output differs")
                output_path = history_root / name
                output_sha256, output_size = _sha(output_path)
                if (
                    inputs.get(f"inputs/history/{name}") != (output_sha256, output_size)
                    or output.get("sha256") != output_sha256
                    or output.get("size_bytes") != output_size
                ):
                    raise VerticalBuildError(
                        f"layered generation output binding differs: {name}"
                    )
                shutil.copyfile(output_path, verification_root / name)
            layered = planner._layered_history_targets(
                generation_root=verification_root,
                contract_path=contract_path,
                strategies=strategies,
                evidence_mode=evidence_mode,
                evaluated_on=evaluated_on,
                assurance_profile=assurance_profile,
            )
    except Exception as exc:
        raise VerticalBuildError(
            f"current decomposition upstream evidence differs: {exc}"
        ) from exc
    finally:
        sys.modules.pop(module_name, None)

    history_contents = layered.get("contents")
    history_identity = layered.get("identity")
    history_composition = layered.get("composition")
    history_schema_definitions = layered.get("schema_definitions")
    plan_data_age = plan.get("data_age")
    if (
        not isinstance(history_contents, dict)
        or not isinstance(history_identity, dict)
        or not isinstance(history_composition, dict)
        or not isinstance(history_schema_definitions, dict)
        or plan.get("layered_history") != history_identity
        or not isinstance(plan_data_age, dict)
        or plan_data_age.get("public_factor_capture") != public_capture
    ):
        raise VerticalBuildError("current decomposition layered history differs")
    for name, content in history_contents.items():
        relative = f"inputs/history/{name}"
        path = attribution_bundle / relative
        if (
            not isinstance(name, str)
            or not isinstance(content, bytes)
            or inputs.get(relative) != _sha(path)
            or path.read_bytes() != content
        ):
            raise VerticalBuildError(
                "current decomposition layered history artifacts differ"
            )

    base_spec_relative = "inputs/governance/base-decomposition-spec.json"
    base_spec_path = attribution_bundle / base_spec_relative
    base_spec = _load(base_spec_path, "base decomposition specification")
    base_provenance = base_spec.get("provenance_sources")
    if not isinstance(base_provenance, list):
        raise VerticalBuildError("base decomposition provenance differs")
    expected_provenance: list[dict[str, Any]] = []
    for index, definition in enumerate(base_provenance):
        if not isinstance(definition, dict):
            raise VerticalBuildError("base decomposition provenance differs")
        original_relative = _text(
            definition.get("relative_path"), "base provenance relative_path"
        )
        copied_relative = (
            f"inputs/base/provenance/{index:02d}-"
            f"{PurePosixPath(original_relative).name}"
        )
        copied_path = attribution_bundle / copied_relative
        if inputs.get(copied_relative) != _sha(copied_path):
            raise VerticalBuildError("base decomposition provenance binding differs")
        expected_provenance.append(
            {
                "artifact_id": _text(
                    definition.get("artifact_id"), "base provenance artifact_id"
                ),
                "expected_sha256": _text(
                    definition.get("expected_sha256"), "base provenance sha256"
                ),
                "expected_size_bytes": definition.get("expected_size_bytes"),
                "relative_path": copied_relative,
                "schema_name": _text(
                    definition.get("schema_name"), "base provenance schema_name"
                ),
                "schema_version": _text(
                    definition.get("schema_version"), "base provenance schema_version"
                ),
            }
        )
    governed_plan_content = governed_plan_path.read_bytes()
    public_manifest_content = public_manifest_path.read_bytes()
    style_manifest_content = style_manifest_path.read_bytes()
    sector_manifest_content = sector_manifest_path.read_bytes()
    expected_provenance.extend(
        (
            _provenance_definition(
                "plan/current-strategy-decomposition-plan.json",
                governed_plan_relative,
                governed_plan_content,
                "current-strategy-decomposition-plan",
                str(governed_plan["schema_version"]),
            ),
            _provenance_definition(
                "catalog/decomposition-source-catalog.json",
                catalog_relative,
                catalog_content,
                "decomposition-source-catalog",
            ),
            _provenance_definition(
                "market/public-factor-source-manifest.json",
                "inputs/governance/public-factor-source-manifest.json",
                public_manifest_content,
                "portfolio-public-decomposition-source-manifest",
            ),
            _provenance_definition(
                "market/style-source-manifest.json",
                "inputs/governance/style-source-manifest.json",
                style_manifest_content,
                "portfolio-public-style-source-manifest",
            ),
            _provenance_definition(
                "market/sector-source-manifest.json",
                "inputs/governance/sector-source-manifest.json",
                sector_manifest_content,
                "portfolio-public-sector-source-manifest",
            ),
        )
    )
    for name, content in sorted(history_contents.items()):
        schema = history_schema_definitions.get(name)
        if not isinstance(schema, dict):
            raise VerticalBuildError("layered history schema definition differs")
        expected_provenance.append(
            _provenance_definition(
                f"history/{name}",
                f"inputs/history/{name}",
                content,
                _text(schema.get("schema_name"), "history schema_name"),
                _text(schema.get("schema_version"), "history schema_version"),
            )
        )
    return {
        "layered_composition": history_composition,
        "provenance_sources": expected_provenance,
    }


def _governed_plan_sources(
    attribution_bundle: Path,
    inputs: Mapping[str, tuple[str, int]],
    outputs: Mapping[str, tuple[str, int]],
) -> tuple[
    dict[str, str],
    tuple[str, int],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, Any],
]:
    plan_relative = "inputs/governance/current-strategy-decomposition-plan.json"
    contract_relative = "inputs/history/financial-mvp-contract.json"
    catalog_relative = "inputs/governance/decomposition-source-catalog.json"
    plan_binding = inputs.get(plan_relative)
    if (
        plan_binding is None
        or contract_relative not in inputs
        or catalog_relative not in inputs
        or "candidate-dispositions.json" not in outputs
    ):
        raise VerticalBuildError("governed current strategy plan is unavailable")
    plan = _load(attribution_bundle / plan_relative, "governed current strategy plan")
    if plan.get("schema_version") != 3:
        raise VerticalBuildError("governed current strategy plan schema differs")
    sources = plan.get("sources")
    strategies = plan.get("strategies")
    split_policy = plan.get("split_policy")
    if (
        not isinstance(sources, dict)
        or not sources
        or not isinstance(strategies, list)
        or not isinstance(split_policy, dict)
    ):
        raise VerticalBuildError("governed current strategy plan boundary differs")
    dynamic_policy = split_policy.get("dynamic")
    static_ridge_candidates = split_policy.get("ridge_candidates")
    dynamic_smoothness_candidates = (
        dynamic_policy.get("smoothness_candidates")
        if isinstance(dynamic_policy, dict)
        else None
    )
    cv_folds = split_policy.get("cv_folds")
    holdout_fraction = split_policy.get("holdout_fraction")
    minimum_holdout = split_policy.get("minimum_holdout_observations")
    minimum_per_factor = split_policy.get("minimum_training_observations_per_factor")
    parsimony_tolerance = split_policy.get("parsimony_tolerance")
    if (
        not isinstance(static_ridge_candidates, list)
        or not static_ridge_candidates
        or not isinstance(dynamic_policy, dict)
        or not isinstance(dynamic_smoothness_candidates, list)
        or not dynamic_smoothness_candidates
    ):
        raise VerticalBuildError("governed current strategy model policy differs")
    try:
        parsed_holdout_fraction = Decimal(str(holdout_fraction))
        parsed_static_ridge = tuple(
            Decimal(str(candidate)) for candidate in static_ridge_candidates
        )
        parsed_dynamic_state_ridge = Decimal(str(dynamic_policy.get("state_ridge")))
        parsed_dynamic_smoothness = tuple(
            Decimal(candidate) for candidate in dynamic_smoothness_candidates
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VerticalBuildError(
            "governed current strategy model policy differs"
        ) from exc
    if (
        not isinstance(cv_folds, int)
        or isinstance(cv_folds, bool)
        or cv_folds < 2
        or not isinstance(minimum_holdout, int)
        or isinstance(minimum_holdout, bool)
        or minimum_holdout < 1
        or not isinstance(minimum_per_factor, int)
        or isinstance(minimum_per_factor, bool)
        or minimum_per_factor < 1
        or not isinstance(parsimony_tolerance, str)
        or not parsimony_tolerance
        or isinstance(holdout_fraction, bool)
        or not parsed_holdout_fraction.is_finite()
        or parsed_holdout_fraction < Decimal("0.01")
        or parsed_holdout_fraction >= Decimal("1")
        or not isinstance(static_ridge_candidates, list)
        or not static_ridge_candidates
        or any(isinstance(item, bool) for item in static_ridge_candidates)
        or any(not item.is_finite() or item < 0 for item in parsed_static_ridge)
        or len(parsed_static_ridge) != len(set(parsed_static_ridge))
        or not isinstance(dynamic_policy, dict)
        or set(dynamic_policy) != {"constraint", "smoothness_candidates", "state_ridge"}
        or dynamic_policy.get("constraint") != "UNCONSTRAINED"
        or not isinstance(dynamic_policy.get("state_ridge"), str)
        or not parsed_dynamic_state_ridge.is_finite()
        or parsed_dynamic_state_ridge < 0
        or not isinstance(dynamic_smoothness_candidates, list)
        or not dynamic_smoothness_candidates
        or any(not isinstance(item, str) for item in dynamic_smoothness_candidates)
        or any(
            not item.is_finite() or item < Decimal("0.000000000001")
            for item in parsed_dynamic_smoothness
        )
        or len(parsed_dynamic_smoothness) != len(set(parsed_dynamic_smoothness))
    ):
        raise VerticalBuildError("governed current strategy model policy differs")
    model_policy: dict[str, Any] = {
        "dynamic_constraint": dynamic_policy["constraint"],
        "dynamic_smoothness_candidates": dynamic_smoothness_candidates,
        "dynamic_state_ridge": dynamic_policy["state_ridge"],
        "holdout_fraction": str(parsed_holdout_fraction),
        "minimum_training_observations_per_factor": minimum_per_factor,
        "static_ridge_candidates": static_ridge_candidates,
    }

    catalog = _load(
        attribution_bundle / catalog_relative, "decomposition source catalog"
    )
    universes = catalog.get("universes")
    if not isinstance(universes, list):
        raise VerticalBuildError("governed decomposition catalog differs")
    catalog_candidates: dict[str, list[str]] = {}
    catalog_candidate_records: dict[str, dict[str, dict[str, Any]]] = {}
    for raw_universe in universes:
        if not isinstance(raw_universe, dict):
            raise VerticalBuildError("governed decomposition catalog differs")
        universe_id = _text(raw_universe.get("universe_id"), "catalog universe_id")
        raw_candidates = raw_universe.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise VerticalBuildError("governed decomposition catalog differs")
        candidates = [
            _text(candidate.get("candidate_id"), "catalog candidate_id")
            for candidate in raw_candidates
            if isinstance(candidate, dict)
        ]
        if (
            len(candidates) != len(raw_candidates)
            or len(candidates) != len(set(candidates))
            or universe_id in catalog_candidates
        ):
            raise VerticalBuildError("governed decomposition catalog differs")
        catalog_candidates[universe_id] = candidates
        catalog_candidate_records[universe_id] = {
            candidate_id: dict(candidate)
            for candidate_id, candidate in zip(candidates, raw_candidates, strict=True)
        }
    source_lenses: dict[str, str] = {}
    source_candidates: dict[str, list[str]] = {}
    source_runtime_contracts: dict[str, dict[str, Any]] = {}
    for raw_source_key, raw_source in sources.items():
        source_key = _portable_source_key(raw_source_key)
        if not isinstance(raw_source, dict):
            raise VerticalBuildError("governed plan source is invalid")
        source_lenses[source_key] = _text(
            raw_source.get("lens"), "governed plan source lens"
        )
        if source_lenses[source_key] not in {
            "ECONOMIC_FACTOR",
            "ETF_STYLE",
            "RETURN_INDUSTRY",
            "RETURN_SECTOR",
        }:
            raise VerticalBuildError("governed plan source lens differs")
        universe_id = _text(
            raw_source.get("universe_id"), "governed plan source universe_id"
        )
        if universe_id not in catalog_candidates:
            raise VerticalBuildError("governed plan source universe differs")
        source_candidates[source_key] = catalog_candidates[universe_id]
        input_key = _text(raw_source.get("input_key"), "governed source input_key")
        if input_key not in {
            "us_factors",
            "europe_factors",
            "style_returns",
            "sector_returns",
            "us_industries",
        }:
            raise VerticalBuildError("governed source input key differs")
        if input_key in {"us_factors", "europe_factors", "us_industries"} and (
            raw_source.get("freshness_policy_id")
            != "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1"
        ):
            raise VerticalBuildError("governed source freshness policy differs")
        risk_free_column = _text(
            raw_source.get("risk_free_column"), "governed source risk-free column"
        )
        column_policy = _text(
            raw_source.get("column_policy"), "governed source column policy"
        )
        return_type = _text(
            raw_source.get("return_type"), "governed source return_type"
        )
        source_runtime_contracts[source_key] = {
            "candidate_records": catalog_candidate_records[universe_id],
            "candidates": catalog_candidates[universe_id],
            "column_policy": column_policy,
            "input_key": input_key,
            "lens": source_lenses[source_key],
            "return_type": return_type,
            "risk_free_column": risk_free_column,
        }
    model_policy["source_contracts"] = source_runtime_contracts

    by_strategy_id: dict[str, Mapping[str, Any]] = {}
    for raw_strategy in strategies:
        if not isinstance(raw_strategy, dict):
            raise VerticalBuildError("governed plan strategy is invalid")
        strategy_id = _text(raw_strategy.get("strategy_id"), "governed strategy_id")
        if strategy_id in by_strategy_id:
            raise VerticalBuildError("governed plan strategy is duplicated")
        by_strategy_id[strategy_id] = raw_strategy
    active_sources: set[str] = set()
    expected_models: dict[str, dict[str, dict[str, Any]]] = {
        source_key: {} for source_key in source_lenses
    }
    expected_dispositions: list[dict[str, Any]] = []
    assignment_keys_by_strategy: dict[str, set[str]] = {}
    disposition_cells_by_strategy: dict[str, dict[str, Mapping[str, Any]]] = {}
    for strategy_id, strategy in by_strategy_id.items():
        series_id = _text(strategy.get("series_id"), "governed strategy series_id")
        inherited = strategy.get("inherit_assignments_from")
        assignment_owner = strategy
        if inherited is not None:
            parent_id = _text(inherited, "governed assignment inheritance")
            parent = by_strategy_id.get(parent_id)
            if parent is None or parent.get("inherit_assignments_from") is not None:
                raise VerticalBuildError("governed assignment inheritance differs")
            assignment_owner = parent
        assignments = assignment_owner.get("assignments", [])
        if not isinstance(assignments, list):
            raise VerticalBuildError("governed plan assignments are invalid")
        strategy_assignments: dict[str, Mapping[str, Any]] = {}
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise VerticalBuildError("governed plan assignment is invalid")
            source_key = _portable_source_key(assignment.get("source_key"))
            if source_key not in source_lenses or source_key in strategy_assignments:
                raise VerticalBuildError("governed plan assignment source differs")
            strategy_assignments[source_key] = assignment
        active_sources.update(strategy_assignments)
        assignment_keys_by_strategy[strategy_id] = set(strategy_assignments)

        source_dispositions = strategy.get("source_dispositions")
        if not isinstance(source_dispositions, dict) or set(source_dispositions) != set(
            source_lenses
        ):
            raise VerticalBuildError("governed plan source dispositions differ")
        disposition_cells_by_strategy[strategy_id] = {}
        for source_key, candidates in source_candidates.items():
            cell = source_dispositions.get(source_key)
            if not isinstance(cell, dict) or set(cell) != {
                "authority",
                "readiness",
                "reason",
                "requirement",
            }:
                raise VerticalBuildError("governed plan source disposition differs")
            disposition_cells_by_strategy[strategy_id][source_key] = cell
            assignment = strategy_assignments.get(source_key)
            factor_sets: list[list[str]] = []
            screened: set[str] = set()
            if assignment is not None:
                if assignment.get("model_policy") == "EACH_CANDIDATE_ONE_FACTOR":
                    factor_sets = [[candidate] for candidate in candidates]
                else:
                    raw_factor_sets = assignment.get("factor_sets")
                    if not isinstance(raw_factor_sets, list):
                        raise VerticalBuildError("governed plan factor sets differ")
                    factor_sets = [
                        list(factor_set)
                        for factor_set in raw_factor_sets
                        if isinstance(factor_set, list)
                        and all(isinstance(item, str) for item in factor_set)
                    ]
                    if len(factor_sets) != len(raw_factor_sets):
                        raise VerticalBuildError("governed plan factor sets differ")
                raw_screened = assignment.get("screened_out_candidates", [])
                if not isinstance(raw_screened, list) or any(
                    not isinstance(item, str) for item in raw_screened
                ):
                    raise VerticalBuildError("governed plan factor sets differ")
                screened = set(raw_screened)
                modeled = {item for factor_set in factor_sets for item in factor_set}
                if (
                    not factor_sets
                    or any(
                        not factor_set or len(factor_set) != len(set(factor_set))
                        for factor_set in factor_sets
                    )
                    or modeled & screened
                    or modeled | screened != set(candidates)
                ):
                    raise VerticalBuildError("governed plan factor sets differ")
                static_constraint = _text(
                    assignment.get("constraint"), "governed static constraint"
                )
                for index, factor_set in enumerate(factor_sets, 1):
                    base_model_id = f"{strategy_id}-{source_key}-{index:02d}"
                    if base_model_id in expected_models[source_key]:
                        raise VerticalBuildError(
                            "governed model identity is duplicated"
                        )
                    expected_models[source_key][base_model_id] = {
                        "cost_coverage": "INCOMPLETE",
                        "cv_folds": cv_folds,
                        "factor_ids": factor_set,
                        "minimum_test_observations": minimum_holdout,
                        "minimum_train_observations": max(
                            3, minimum_per_factor * len(factor_set)
                        ),
                        "parsimony_tolerance": parsimony_tolerance,
                        "periods_per_year": 252,
                        "series_id": series_id,
                        "static_constraint": static_constraint,
                    }
            for candidate in candidates:
                disposition = (
                    "SCREENED_OUT_PREDECLARED_DIMENSION_CONTROL"
                    if assignment is not None and candidate in screened
                    else (
                        "PLANNED_MODEL_CANDIDATE"
                        if assignment is not None
                        else cell["readiness"]
                    )
                )
                for surface in _CURRENT_PLAN_SURFACES:
                    expected_dispositions.append(
                        {
                            "authority": cell["authority"],
                            "candidate_id": candidate,
                            "disposition": disposition,
                            "lens": source_lenses[source_key],
                            "readiness": cell["readiness"],
                            "reason": cell["reason"],
                            "requirement": cell["requirement"],
                            "source_key": source_key,
                            "strategy_id": strategy_id,
                            "surface": surface,
                        }
                    )
    if not active_sources:
        raise VerticalBuildError("governed plan has no active decomposition sources")
    dispositions = _load(
        attribution_bundle / "candidate-dispositions.json",
        "candidate dispositions",
    )
    disposition_rows = dispositions.get("candidate_dispositions")
    if (
        dispositions.get("schema_version") != 2
        or dispositions.get("policy")
        != "EVERY_CURRENT_STRATEGY_BY_EVERY_PLANNED_CANDIDATE_UNIVERSE"
        or not isinstance(disposition_rows, list)
        or sorted(
            disposition_rows,
            key=lambda item: (
                str(item.get("strategy_id")) if isinstance(item, dict) else "",
                str(item.get("source_key")) if isinstance(item, dict) else "",
                str(item.get("surface")) if isinstance(item, dict) else "",
                str(item.get("candidate_id")) if isinstance(item, dict) else "",
            ),
        )
        != sorted(
            expected_dispositions,
            key=lambda item: (
                item["strategy_id"],
                item["source_key"],
                item["surface"],
                item["candidate_id"],
            ),
        )
    ):
        raise VerticalBuildError("candidate dispositions differ from governed plan")

    contract = _load(
        attribution_bundle / contract_relative, "financial MVP source contract"
    )
    contract_strategies = contract.get("strategies")
    if not isinstance(contract_strategies, list):
        raise VerticalBuildError("financial MVP strategy perimeter differs")
    contract_by_strategy_id: dict[str, Mapping[str, Any]] = {}
    for contract_strategy in contract_strategies:
        if not isinstance(contract_strategy, dict):
            raise VerticalBuildError("financial MVP strategy perimeter differs")
        strategy_id = _text(
            contract_strategy.get("strategy_id"), "financial MVP strategy_id"
        )
        if strategy_id in contract_by_strategy_id:
            raise VerticalBuildError("financial MVP strategy perimeter differs")
        contract_by_strategy_id[strategy_id] = contract_strategy
    if set(contract_by_strategy_id) != set(by_strategy_id):
        raise VerticalBuildError("financial MVP strategy perimeter differs")
    for strategy_id, contract_strategy in contract_by_strategy_id.items():
        plan_strategy = by_strategy_id[strategy_id]
        contract_series_id = contract_strategy.get("history_series_id")
        if (
            contract_series_id is not None
            and plan_strategy.get("series_id") != contract_series_id
        ):
            raise VerticalBuildError("financial MVP strategy series differs")
        outcome_dispositions = contract_strategy.get("outcome_dispositions")
        cells = disposition_cells_by_strategy[strategy_id]
        assignments = assignment_keys_by_strategy[strategy_id]
        if not isinstance(outcome_dispositions, dict):
            raise VerticalBuildError("financial MVP strategy disposition differs")
        product_requirement = contract_strategy.get("product_requirement")
        decomposition_disposition = outcome_dispositions.get("DECOMPOSITION")
        if (
            product_requirement == "REQUIRED"
            and decomposition_disposition == "REQUIRED"
        ):
            ready_sources: set[str] = set()
            for source_key, cell in cells.items():
                readiness = cell.get("readiness")
                reason = cell.get("reason")
                if cell.get("requirement") != "REQUIRED":
                    raise VerticalBuildError(
                        "strategy assignments differ from source disposition authority"
                    )
                if readiness == "READY_TO_MODEL":
                    if not isinstance(reason, str) or not reason.startswith(
                        "APPLICABLE_"
                    ):
                        raise VerticalBuildError(
                            "strategy assignments differ from source disposition authority"
                        )
                    ready_sources.add(source_key)
                elif readiness == "REQUIRED_NOT_READY":
                    if (
                        not isinstance(reason, str)
                        or not reason.startswith("REQUIRED_NOT_READY_")
                        or any(
                            token in reason
                            for token in ("DROPPED", "OUT_OF_SCOPE", "NOT_APPLICABLE")
                        )
                    ):
                        raise VerticalBuildError(
                            "strategy assignments differ from source disposition authority"
                        )
                else:
                    raise VerticalBuildError(
                        "strategy assignments differ from source disposition authority"
                    )
            if assignments != ready_sources:
                raise VerticalBuildError(
                    "strategy assignments differ from source disposition authority"
                )
        elif (
            product_requirement == "EXCLUDED"
            and decomposition_disposition == "EXCLUDED_BY_ACCEPTED_POLICY"
        ):
            if assignments or any(
                cell.get("requirement") != "NOT_APPLICABLE_OWNER_APPROVED"
                or cell.get("readiness") != "NOT_APPLICABLE_OWNER_APPROVED"
                or cell.get("reason") != "NOT_APPLICABLE_OWNER_APPROVED"
                for cell in cells.values()
            ):
                raise VerticalBuildError(
                    "strategy assignments differ from source disposition authority"
                )
        else:
            raise VerticalBuildError("financial MVP strategy disposition differs")
    source_segments = contract.get("source_segments")
    if not isinstance(source_segments, list):
        raise VerticalBuildError("governed plan authority is unavailable")
    authorities = [
        row
        for row in source_segments
        if isinstance(row, dict) and row.get("source_id") == "current-strategy-registry"
    ]
    if len(authorities) != 1:
        raise VerticalBuildError("governed plan authority is unavailable")
    authority = authorities[0]
    generation = authority.get("current_generation")
    schema = authority.get("schema")
    admission = authority.get("admission")
    if (
        authority.get("source_version") != "decomposition-plan-v3"
        or authority.get("endpoint_or_artifact_selector")
        != "docs/analytics/current-strategy-decomposition-plan.json"
        or authority.get("existence_status") != "EXISTS"
        or authority.get("capture_status") != "CAPTURED_GOVERNED"
        or authority.get("product_requirement") != "REQUIRED"
        or not isinstance(authority.get("consumers"), list)
        or "DECOMPOSITION" not in authority["consumers"]
        or not isinstance(authority.get("data_classes"), list)
        or "DECOMPOSITION_APPLICABILITY" not in authority["data_classes"]
        or not isinstance(schema, dict)
        or schema.get("name") != "current-strategy-decomposition-plan"
        or schema.get("version") != "3"
        or not isinstance(admission, dict)
        or admission.get("transform") != "portfolio-current-product-strategy-census"
        or admission.get("version") != "3"
        or not isinstance(admission.get("facts"), list)
        or "DECOMPOSITION_APPLICABILITY" not in admission["facts"]
        or not isinstance(generation, dict)
        or generation.get("generation_id") != plan_binding[0]
        or generation.get("sha256") != plan_binding[0]
    ):
        raise VerticalBuildError("governed plan authority differs")
    return (
        {source_key: source_lenses[source_key] for source_key in active_sources},
        plan_binding,
        {source_key: expected_models[source_key] for source_key in active_sources},
        model_policy,
    )


def _current_release_plan_specs(
    attribution_bundle: Path, plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if set(plan) != {
        "assurance_profile",
        "builder",
        "builder_version",
        "bundle_id",
        "data_age",
        "evaluation_date",
        "inputs",
        "layered_history",
        "outputs",
        "policies",
        "release_qc_eligibility",
        "schema_version",
        "specs",
    }:
        raise VerticalBuildError("current decomposition plan fields differ")
    if (
        plan.get("builder") != "portfolio-current-decomposition-plan"
        or plan.get("builder_version") != "0.17.0"
        or plan.get("schema_version") != 2
    ):
        raise VerticalBuildError("current decomposition plan contract differs")
    bundle_id = _text(plan.get("bundle_id"), "attribution plan bundle_id")
    if len(bundle_id) != 64 or any(
        character not in "0123456789abcdef" for character in bundle_id
    ):
        raise VerticalBuildError("attribution plan bundle identity is invalid")
    identity = dict(plan)
    identity.pop("bundle_id")
    if hashlib.sha256(canonical_json_bytes(identity)).hexdigest() != bundle_id:
        raise VerticalBuildError("attribution plan bundle identity differs")
    if attribution_bundle.name != bundle_id:
        raise VerticalBuildError("attribution plan bundle directory differs")

    layered_history = plan.get("layered_history")
    if not isinstance(layered_history, dict):
        raise VerticalBuildError("attribution plan layered history is invalid")
    evidence_mode = _text(layered_history.get("mode"), "attribution plan evidence mode")
    if evidence_mode not in {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
        raise VerticalBuildError("attribution plan evidence mode is unsupported")
    assurance_profile = _text(
        plan.get("assurance_profile"), "attribution plan assurance_profile"
    )
    if assurance_profile not in {"DEVELOPMENT", "WORKSTATION_QC", "RELEASE_QC"}:
        raise VerticalBuildError("attribution plan assurance profile differs")
    expected_release_eligibility = (
        "REQUIRES_DOWNSTREAM_RELEASE_QC"
        if assurance_profile == "RELEASE_QC"
        else "NOT_ELIGIBLE_NON_RELEASE_PROFILE"
    )
    if plan.get("release_qc_eligibility") != expected_release_eligibility:
        raise VerticalBuildError("attribution plan release eligibility differs")
    policies = plan.get("policies")
    if not isinstance(policies, dict) or set(policies) != {
        "calendar",
        "data_age",
        "holdout",
        "model_dimension",
        "publication",
        "strategy_evidence_mode",
    }:
        raise VerticalBuildError("current decomposition plan policy fields differ")
    if policies.get("strategy_evidence_mode") != evidence_mode:
        raise VerticalBuildError("attribution plan evidence-mode policy differs")
    model_dimension = policies.get("model_dimension")
    if not isinstance(model_dimension, dict) or set(model_dimension) != {
        "absolute_minimum_training_observations",
        "dynamic_constraint",
        "dynamic_smoothness_candidates",
        "dynamic_state_ridge",
        "minimum_training_observations_per_factor",
        "required_surfaces",
    }:
        raise VerticalBuildError(
            "current decomposition plan model-dimension fields differ"
        )
    required_surfaces = model_dimension.get("required_surfaces")
    if (
        not isinstance(required_surfaces, list)
        or any(not isinstance(item, str) for item in required_surfaces)
        or len(required_surfaces) != len(set(required_surfaces))
        or set(required_surfaces) != _CURRENT_PLAN_SURFACES
    ):
        raise VerticalBuildError("attribution plan required surfaces differ")

    inputs = _manifest_artifacts(
        attribution_bundle, plan.get("inputs"), "attribution plan inputs"
    )
    outputs = _manifest_artifacts(
        attribution_bundle, plan.get("outputs"), "attribution plan outputs"
    )
    if set(inputs) & set(outputs):
        raise VerticalBuildError("attribution plan artifact inventory overlaps")
    required_inputs = {
        "inputs/governance/base-decomposition-spec.json",
        "inputs/governance/current-strategy-decomposition-plan.json",
        "inputs/governance/decomposition-source-catalog.json",
        "inputs/governance/public-factor-source-manifest.json",
        "inputs/governance/sector-source-manifest.json",
        "inputs/governance/style-source-manifest.json",
        "inputs/history/financial-mvp-contract.json",
        *{
            f"inputs/market/{key}.csv"
            for key in (
                "us_factors",
                "europe_factors",
                "style_returns",
                "sector_returns",
                "us_industries",
            )
        },
    }
    required_outputs = {"candidate-dispositions.json"}
    declared = set(inputs) | set(outputs)
    root = attribution_bundle.resolve()
    paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise VerticalBuildError("attribution plan artifact inventory contains a link")
    actual = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
    if (
        not required_inputs.issubset(inputs)
        or not required_outputs.issubset(outputs)
        or actual != declared | {"plan-manifest.json"}
    ):
        raise VerticalBuildError("attribution plan artifact inventory differs")
    evaluation_date = _text(plan.get("evaluation_date"), "attribution evaluation_date")
    try:
        if date.fromisoformat(evaluation_date).isoformat() != evaluation_date:
            raise ValueError
    except ValueError as exc:
        raise VerticalBuildError("attribution evaluation_date differs") from exc
    upstream_evidence = _verify_current_plan_upstream_evidence(
        attribution_bundle,
        inputs,
        plan,
        evaluation_date=evaluation_date,
        evidence_mode=evidence_mode,
        assurance_profile=assurance_profile,
    )
    (
        governed_sources,
        _governed_plan_binding,
        governed_models,
        governed_model_policy,
    ) = _governed_plan_sources(
        attribution_bundle,
        inputs,
        outputs,
    )
    financial_contract = _load(
        attribution_bundle / "inputs/history/financial-mvp-contract.json",
        "financial MVP source contract",
    )
    product = financial_contract.get("product")
    as_of_policy = product.get("as_of_policy") if isinstance(product, dict) else None
    execution_profiles = (
        as_of_policy.get("execution_profiles")
        if isinstance(as_of_policy, dict)
        else None
    )
    profile_policy = (
        execution_profiles.get(assurance_profile)
        if isinstance(execution_profiles, dict)
        else None
    )
    if (
        not isinstance(profile_policy, str)
        or not profile_policy.strip()
        or layered_history.get("assurance_profile") != assurance_profile
        or layered_history.get("assurance_profile_policy") != profile_policy.strip()
    ):
        raise VerticalBuildError("attribution plan assurance policy differs")
    if (
        policies.get("calendar") != "EXACT_DATE_COMPLETE_CANDIDATE_UNIVERSE_NO_FILL"
        or policies.get("publication") != "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE"
        or policies.get("data_age")
        != "WARN_IN_DEVELOPMENT_AND_WORKSTATION; FAIL_REQUIRED_STALE_SOURCE_IN_RELEASE_QC"
        or policies.get("holdout")
        != {
            "fraction": governed_model_policy["holdout_fraction"],
            "minimum_observations": min(
                model["minimum_test_observations"]
                for source_models in governed_models.values()
                for model in source_models.values()
            ),
            "selection": "TRAILING_SEALED_HOLDOUT",
        }
        or model_dimension.get("absolute_minimum_training_observations") != 3
        or model_dimension.get("minimum_training_observations_per_factor")
        != governed_model_policy["minimum_training_observations_per_factor"]
        or any(
            model_dimension.get(key) != governed_model_policy[key]
            for key in (
                "dynamic_constraint",
                "dynamic_smoothness_candidates",
                "dynamic_state_ridge",
            )
        )
    ):
        raise VerticalBuildError("attribution plan execution policy differs")
    specs = plan.get("specs")
    if not isinstance(specs, list) or not specs:
        raise VerticalBuildError("attribution plan has no lens specifications")
    rows: list[dict[str, Any]] = []
    spec_ids: set[str] = set()
    spec_paths: set[str] = set()
    source_surfaces: set[tuple[str, str]] = set()
    surfaces_by_source: dict[str, set[str]] = {}
    source_keys_by_windows_identity: dict[str, str] = {}
    populations_by_source: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in specs:
        if not isinstance(raw, dict) or set(raw) != _CURRENT_PLAN_SPEC_KEYS:
            raise VerticalBuildError("current attribution lens definition is invalid")
        source_key = _portable_source_key(raw.get("source_key"))
        windows_identity = source_key.casefold()
        existing_source = source_keys_by_windows_identity.setdefault(
            windows_identity, source_key
        )
        if existing_source != source_key:
            raise VerticalBuildError("lens source_key has a Windows path collision")
        surface = _text(raw.get("surface"), "lens surface")
        if surface not in _CURRENT_PLAN_SURFACES:
            raise VerticalBuildError("attribution plan surface is unsupported")
        spec_id = _text(raw.get("spec_id"), "lens spec_id")
        if spec_id != f"{source_key}:{surface}":
            raise VerticalBuildError("attribution plan spec_id differs")
        expected_relative = (
            f"specs/{source_key}.json"
            if surface == "STATIC"
            else f"specs/{source_key}-dynamic-fls.json"
        )
        relative, artifact = _portable_bundle_file(
            attribution_bundle, raw.get("path"), "lens spec path"
        )
        if (
            governed_sources.get(source_key) != raw.get("analysis_lens")
            or relative != expected_relative
        ):
            raise VerticalBuildError("attribution plan differs from governed plan")
        sha256 = _text(raw.get("sha256"), "lens spec sha256")
        size = raw.get("size_bytes")
        if (
            spec_id in spec_ids
            or relative in spec_paths
            or (source_key, surface) in source_surfaces
        ):
            raise VerticalBuildError("attribution plan lens identity is duplicated")
        if outputs.get(relative) != (sha256, size) or _sha(artifact) != (sha256, size):
            raise VerticalBuildError(f"attribution plan artifact differs: {relative}")
        specification = _load(artifact, f"lens specification {spec_id}")
        model_count = raw.get("model_count")
        models = specification.get("models")
        expected_estimator = "STATIC_RBSA" if surface == "STATIC" else "DYNAMIC_FLS"
        if (
            specification.get("schema_version") != 1
            or specification.get("analysis_lens") != raw.get("analysis_lens")
            or specification.get("as_of_date") != raw.get("as_of_date")
            or specification.get("evidence_mode") != evidence_mode
            or not isinstance(model_count, int)
            or isinstance(model_count, bool)
            or model_count < 1
            or not isinstance(models, list)
            or len(models) != model_count
            or any(
                not isinstance(model, dict)
                or model.get("estimator") != expected_estimator
                for model in models
            )
        ):
            raise VerticalBuildError(
                f"attribution plan lens specification differs: {spec_id}"
            )
        if (
            specification.get("provenance_sources")
            != upstream_evidence["provenance_sources"]
        ):
            raise VerticalBuildError(
                f"attribution plan provenance differs from governed plan: {spec_id}"
            )
        expected_series = {
            _text(model.get("series_id"), "governed model series_id")
            for model in governed_models[source_key].values()
        }
        layered_composition = upstream_evidence.get("layered_composition")
        if (
            not isinstance(layered_composition, dict)
            or specification.get("series_composition")
            != {
                series_id: layered_composition[series_id]
                for series_id in sorted(expected_series)
                if series_id in layered_composition
            }
            or not expected_series.issubset(layered_composition)
        ):
            raise VerticalBuildError(
                f"attribution plan series composition differs: {spec_id}"
            )
        source_contracts = governed_model_policy.get("source_contracts")
        source_contract = (
            source_contracts.get(source_key)
            if isinstance(source_contracts, dict)
            else None
        )
        if not isinstance(source_contract, dict):
            raise VerticalBuildError("governed source runtime contract differs")
        expected_timing = _expected_model_timing(
            attribution_bundle,
            inputs,
            specification,
            source_key,
            governed_models[source_key],
            source_contract,
            evaluation_date=evaluation_date,
            holdout_fraction=governed_model_policy["holdout_fraction"],
        )
        population = _surface_population(
            raw,
            specification,
            surface,
            governed_models[source_key],
            expected_timing,
            governed_model_policy,
            evaluation_date,
        )
        spec_ids.add(spec_id)
        spec_paths.add(relative)
        source_surfaces.add((source_key, surface))
        surfaces_by_source.setdefault(source_key, set()).add(surface)
        populations_by_source.setdefault(source_key, {})[surface] = population
        rows.append({**raw, "evidence_mode": evidence_mode})
    if any(
        surfaces != _CURRENT_PLAN_SURFACES for surfaces in surfaces_by_source.values()
    ):
        raise VerticalBuildError(
            "current attribution plan requires exact STATIC/DYNAMIC_FLS surfaces"
        )
    if set(surfaces_by_source) != set(governed_sources):
        raise VerticalBuildError("attribution plan sources differ from governed plan")
    if any(
        populations["STATIC"] != populations["DYNAMIC_FLS"]
        for populations in populations_by_source.values()
    ):
        raise VerticalBuildError("attribution plan surface populations differ")
    expected_market_sources, expected_public_factor_capture = (
        _expected_non_spec_data_age(
            attribution_bundle,
            inputs,
            governed_sources,
            governed_model_policy,
            financial_contract,
            evaluation_date=evaluation_date,
            assurance_profile=assurance_profile,
        )
    )
    data_age = plan.get("data_age")
    if (
        not isinstance(data_age, dict)
        or set(data_age)
        != {
            "market_sources",
            "public_factor_capture",
            "spec_calculation_boundaries",
        }
        or not isinstance(data_age.get("market_sources"), list)
        or not isinstance(data_age.get("public_factor_capture"), dict)
        or data_age.get("market_sources") != expected_market_sources
        or data_age.get("public_factor_capture") != expected_public_factor_capture
        or data_age.get("spec_calculation_boundaries")
        != {str(row["spec_id"]): row["data_boundary"] for row in rows}
    ):
        raise VerticalBuildError("attribution plan data-age manifest differs")
    return sorted(rows, key=lambda row: str(row["spec_id"]))


def build_analytics(
    recipe_path: Path,
    attribution_bundle: Path,
    output_root: Path,
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    plan = _load(attribution_bundle / "plan-manifest.json", "attribution plan")
    recipe_bundle = _path(
        recipe["decomposition_plan_bundle"]["path"],
        "decomposition plan bundle",
    )
    if attribution_bundle.resolve() != recipe_bundle:
        raise VerticalBuildError("attribution plan bundle differs from recipe")
    recipe_as_of = _text(recipe.get("as_of_date"), "recipe as_of_date")
    correlation_source = _path(
        recipe["correlation_source_root"]["path"], "correlation source root"
    )
    current_dependence_receipt = correlation_source / "dependence-source.json"
    if current_dependence_receipt.is_file():
        correlation_identity = _load(
            current_dependence_receipt, "current dependence receipt"
        )
        correlation_as_of = _text(
            correlation_identity.get("evaluated_on"), "correlation evaluated_on"
        )
    else:
        correlation_identity = _load(
            correlation_source / "evidence-proof.json",
            "strategy history compatibility contract",
        )
        correlation_as_of = _text(
            correlation_identity.get("as_of_date"), "correlation as_of_date"
        )
    current_plan = (
        plan.get("builder") == "portfolio-current-decomposition-plan"
        and plan.get("builder_version") == "0.17.0"
        and plan.get("schema_version") == 2
    )
    if current_plan:
        if (
            plan.get("evaluation_date") != recipe_as_of
            or correlation_as_of != recipe_as_of
        ):
            raise VerticalBuildError("current analytics as-of date differs")
        specs = _current_release_plan_specs(attribution_bundle, plan)
    else:
        specs = _legacy_release_plan_specs(plan)
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
    decomposition_generation_id = _generation_id(
        decomposition.get("generation_id"), "decomposition generation_id"
    )
    lenses_root = output_root / "decomposition-lenses"
    lenses: dict[str, str] = {}
    lens_bindings: list[dict[str, Any]] = []
    hybrid_lenses: dict[str, str] = {}
    hybrid_bundles: dict[str, str] = {}
    hybrid_dynamic: dict[str, Any] = {}
    if not current_plan:
        hybrid_root = lenses_root / "LIVE_PLUS_BACKTEST"
        # Keep this private staging name short. The content-addressed bundle adds a
        # 64-character directory plus nested provenance names, and the authoritative
        # Windows runtime may still enforce the legacy 260-character path ceiling.
        evidence_mode_root = output_root / "hybrid-inputs"
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
    for row in specs:
        source_key = _text(row.get("source_key"), "lens source_key")
        if current_plan:
            surface = _text(row.get("surface"), "lens surface")
            evidence_mode = _text(row.get("evidence_mode"), "lens evidence_mode")
            spec_id = _text(row.get("spec_id"), "lens spec_id")
            lens_root = lenses_root / evidence_mode / surface / source_key
        else:
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
        generation_id = _generation_id(
            result.get("generation_id"), "lens generation_id"
        )
        if current_plan:
            lenses[spec_id] = generation_id
            lens_bindings.append(
                {
                    "evidence_mode": evidence_mode,
                    "generation_id": generation_id,
                    "path": _text(row.get("path"), "lens spec path"),
                    "root": str(lens_root),
                    "sha256": _text(row.get("sha256"), "lens spec sha256"),
                    "size_bytes": row.get("size_bytes"),
                    "source_key": source_key,
                    "spec_id": spec_id,
                    "surface": surface,
                }
            )
        else:
            lenses[source_key] = generation_id
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
    correlation_generation_id = _generation_id(
        correlation.get("generation_id"), "correlation generation_id"
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
    weekly_generation_id = _generation_id(
        weekly.get("generation_id"), "weekly generation_id"
    )
    result = {
        "correlation_generation_id": correlation_generation_id,
        "correlation_root": str(correlation_root),
        "decomposition_generation_id": decomposition_generation_id,
        "hybrid_decomposition_bundle_ids": hybrid_bundles,
        "hybrid_dynamic_generation_id": hybrid_dynamic.get("generation_id"),
        "hybrid_lens_generations": hybrid_lenses,
        "decomposition_lenses_root": str(lenses_root),
        "decomposition_root": str(decomposition_root),
        "lens_generations": lenses,
        "weekly_generation_id": weekly_generation_id,
        "weekly_root": str(weekly_root),
    }
    if current_plan:
        result.update(
            {
                "evidence_mode": _text(
                    specs[0].get("evidence_mode"), "lens evidence_mode"
                ),
                "lens_generation_bindings": sorted(
                    lens_bindings, key=lambda row: str(row["spec_id"])
                ),
                "plan_bundle_id": _text(
                    plan.get("bundle_id"), "attribution plan bundle_id"
                ),
                "schema_version": 2,
            }
        )
    return result


def build_report_candidate(
    baseline_root: Path,
    provider_ledger_root: Path,
    freshness_health: Path,
    history_contract: Path,
    implementation_reconciliation: Path,
    implementation_return_attribution: Path,
    attribution_root: Path,
    output_root: Path,
    correlation_root: Path | None = None,
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
        *(
            ("--correlation-root", str(correlation_root))
            if correlation_root is not None
            else ()
        ),
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
    correlation_root: Path | None = None,
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
    validation = _accepted_workbook_validation(validation_path, candidate)
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
    binding_path = candidate / "workbook-evidence-binding.json"
    workbook_evidence_binding = _tool(
        "portfolio_report.py",
        "bind-workbook-evidence",
        "--candidate-dir",
        str(candidate),
        "--excel-workbook",
        str(excel_workbook),
        "--xlsx-result",
        str(result_path),
        "--validation-result",
        str(validation_path),
        "--evidence",
        str(binding_path),
    )
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
        *(
            ("--correlation-root", str(correlation_root))
            if correlation_root is not None
            else ()
        ),
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
        "--workbook-evidence-binding",
        str(binding_path),
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
        *(
            ("--correlation-root", str(correlation_root))
            if correlation_root is not None
            else ()
        ),
        timeout=7200,
    )
    return {
        **published,
        "candidate_dir": str(candidate),
        "root": str(output_root),
        "workbook_canonicalization": canonicalization,
        "workbook_evidence_binding": workbook_evidence_binding,
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
    correlation_root: Path | None = None,
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
        correlation_root,
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
        correlation_root,
    )


def _financial_epoch_inputs(recipe: Mapping[str, Any]) -> dict[str, Path] | None:
    """Return the four independently bound inputs for a schema-8 release.

    Classification policy and UI templates deliberately do not appear here:
    they are repository-owned authorities, never recipe-controlled paths.
    """
    if recipe["schema_version"] != 8:
        return None
    return {
        "ledger": _path(
            recipe["account_strategy_ledger"]["path"],
            "account strategy ledger",
        ),
        "ledger_identity": _path(
            recipe["account_strategy_ledger_identity"]["path"],
            "account strategy ledger identity",
        ),
        "classified_flows": _path(
            recipe["account_strategy_classified_flows"]["path"],
            "account strategy classified flows",
        ),
        "admission_policy": _path(
            recipe["account_strategy_admission_policy"]["path"],
            "account strategy admission policy",
        ),
    }


def _report_baseline_generation_id(report_root: Path) -> str:
    """Derive the exact baseline generation bound by the published report."""
    _report_id, report_generation = _published_generation(report_root)
    report = _load(
        report_generation / "portfolio-report-data.json",
        "published portfolio report data",
    )
    baseline = report.get("baseline")
    if not isinstance(baseline, dict):
        raise VerticalBuildError("published portfolio report baseline is invalid")
    return _generation_id(
        baseline.get("generation_id"),
        "published portfolio report baseline generation_id",
    )


def _stage_account_strategy_manifest(
    recipe: Mapping[str, Any],
    report_root: Path,
    baseline_root: Path,
    output_root: Path,
) -> Path | None:
    """Build one manifest from the exact report-bound baseline generation."""
    financial_inputs = _financial_epoch_inputs(recipe)
    if financial_inputs is None:
        return None
    report_baseline_id = _report_baseline_generation_id(report_root)
    baseline_generation_id, _baseline_generation = _published_generation(baseline_root)
    if baseline_generation_id != report_baseline_id:
        raise VerticalBuildError(
            "report baseline generation differs from release baseline authority"
        )
    manifest = _tool(
        "account_strategy_manifest.py",
        "--ledger-json",
        str(financial_inputs["ledger"]),
        "--ledger-identity-json",
        str(financial_inputs["ledger_identity"]),
        "--baseline-root",
        str(baseline_root),
        "--baseline-generation-id",
        report_baseline_id,
        "--flow-classes-json",
        str(financial_inputs["classified_flows"]),
        "--classification-policy-json",
        str(ACCOUNT_STRATEGY_CLASSIFICATION_POLICY),
        "--admission-policy-json",
        str(financial_inputs["admission_policy"]),
        "--format",
        "json",
    )
    if manifest.get("schema_version") != 4:
        raise VerticalBuildError("account-strategy manifest schema differs")
    staged = output_root / "account-strategy-manifest.json"
    _write_exact(staged, canonical_json_bytes(manifest))
    return staged


def _panel_financial_arguments(
    recipe: Mapping[str, Any],
    baseline_root: Path,
    account_strategy_manifest: Path | None,
) -> tuple[str, ...]:
    financial_inputs = _financial_epoch_inputs(recipe)
    if financial_inputs is None:
        return ()
    if account_strategy_manifest is None:
        raise VerticalBuildError("financial panel requires a staged account manifest")
    return (
        "--template-root",
        str(ACCOUNT_STRATEGY_PANEL_TEMPLATE_ROOT),
        "--financial-mode",
        "--account-manifest",
        str(account_strategy_manifest),
        "--manifest-ledger",
        str(financial_inputs["ledger"]),
        "--manifest-ledger-identity",
        str(financial_inputs["ledger_identity"]),
        "--manifest-baseline-root",
        str(baseline_root),
        "--manifest-classified-flows",
        str(financial_inputs["classified_flows"]),
        "--manifest-classification-policy",
        str(ACCOUNT_STRATEGY_CLASSIFICATION_POLICY),
        "--manifest-admission-policy",
        str(financial_inputs["admission_policy"]),
    )


def build_panel_candidate(
    recipe_path: Path,
    report_root: Path,
    baseline_root: Path,
    attribution_bundle: Path,
    analytics: Mapping[str, Any],
    freshness_health: Path,
    output_root: Path,
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    legacy_panel = _path(recipe["legacy_panel"]["path"], "legacy panel")
    account_strategy_manifest = _stage_account_strategy_manifest(
        recipe,
        report_root,
        baseline_root,
        output_root,
    )
    financial_arguments = _panel_financial_arguments(
        recipe,
        baseline_root,
        account_strategy_manifest,
    )
    result = _tool(
        "portfolio_control_panel.py",
        "build",
        "--report-root",
        str(report_root),
        "--baseline-root",
        str(baseline_root),
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
        *financial_arguments,
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
        "--baseline-root",
        str(baseline_root),
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
        *financial_arguments,
        "--freshness-health",
        str(freshness_health),
        "--candidate-dir",
        str(candidate),
        timeout=7200,
    )
    account_manifest_identity = (
        _file_definition(account_strategy_manifest)
        if account_strategy_manifest is not None
        else None
    )
    return {
        **result,
        **(
            {
                "account_strategy_manifest": str(account_strategy_manifest),
                "account_strategy_manifest_sha256": account_manifest_identity["sha256"],
                "account_strategy_manifest_size_bytes": account_manifest_identity[
                    "size_bytes"
                ],
            }
            if account_manifest_identity is not None
            else {}
        ),
        "candidate_dir": str(candidate),
        "root": str(output_root),
    }


def publish_panel(
    recipe_path: Path,
    report_root: Path,
    baseline_root: Path,
    attribution_bundle: Path,
    analytics: Mapping[str, Any],
    freshness_health: Path,
    candidate: Path,
    qa_dir: Path,
    output_root: Path,
    *,
    account_strategy_manifest: Path | None = None,
    expected_account_strategy_manifest_sha256: str | None = None,
    expected_account_strategy_manifest_size_bytes: int | None = None,
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    legacy_panel = _path(recipe["legacy_panel"]["path"], "legacy panel")
    if recipe["schema_version"] == 8 and account_strategy_manifest is None:
        account_strategy_manifest = _stage_account_strategy_manifest(
            recipe,
            report_root,
            baseline_root,
            output_root,
        )
    financial_arguments = _panel_financial_arguments(
        recipe,
        baseline_root,
        account_strategy_manifest,
    )
    if account_strategy_manifest is not None:
        actual_sha256, actual_size_bytes = _sha(account_strategy_manifest)
        if (
            expected_account_strategy_manifest_sha256 is None
            or expected_account_strategy_manifest_size_bytes is None
            or actual_sha256 != expected_account_strategy_manifest_sha256
            or actual_size_bytes != expected_account_strategy_manifest_size_bytes
        ):
            raise VerticalBuildError(
                "staged account-strategy manifest identity differs"
            )
    semantic_validation = _tool(
        "portfolio_control_panel.py",
        "verify",
        "--report-root", str(report_root), "--baseline-root", str(baseline_root),
        "--legacy-panel", str(legacy_panel),
        "--decomposition-root", _text(analytics.get("decomposition_root"), "decomposition_root"),
        "--decomposition-lenses-root", _text(analytics.get("decomposition_lenses_root"), "decomposition_lenses_root"),
        "--decomposition-plan-bundle", str(attribution_bundle),
        "--correlation-root", _text(analytics.get("correlation_root"), "correlation_root"),
        "--weekly-returns-root", _text(analytics.get("weekly_root"), "weekly_root"),
        *financial_arguments,
        "--freshness-health", str(freshness_health),
        "--candidate-dir", str(candidate),
        timeout=7200,
    )
    _write_exact(
        qa_dir / "semantic-validation.json", canonical_json_bytes(semantic_validation)
    )
    _tool(
        "portfolio_control_panel.py",
        "qa",
        "--candidate-dir",
        str(candidate),
        "--qa-dir",
        str(qa_dir),
        timeout=7200,
    )
    receipt_path, receipt = _panel_publication_receipt(
        recipe_path, recipe, output_root, candidate, qa_dir
    )
    result = _tool(
        "portfolio_control_panel.py",
        "publish",
        "--report-root",
        str(report_root),
        "--baseline-root",
        str(baseline_root),
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
        *financial_arguments,
        "--freshness-health",
        str(freshness_health),
        "--candidate-dir",
        str(candidate),
        "--qa-dir",
        str(qa_dir),
        "--coordinated-release-receipt",
        str(receipt_path),
        "--output-root",
        str(output_root),
        timeout=7200,
    )
    return {
        **result,
        **(
            {
                "account_strategy_manifest": str(account_strategy_manifest),
                "account_strategy_manifest_sha256": actual_sha256,
                "account_strategy_manifest_size_bytes": actual_size_bytes,
            }
            if account_strategy_manifest is not None
            else {}
        ),
        "candidate_dir": str(candidate),
        "coordinated_release_receipt": str(receipt_path),
        "coordinated_release_receipt_sha256": hashlib.sha256(
            canonical_json_bytes(receipt)
        ).hexdigest(),
        "release_id": receipt["release_id"],
        "root": str(output_root),
    }


def _stage_path(output_root: Path, role: str) -> Path:
    return output_root / "stage-results" / f"{role}.json"


def _stage_result(output_root: Path, role: str) -> dict[str, Any]:
    return _load(_stage_path(output_root, role), f"{role} stage result")


def _record_stage(output_root: Path, role: str, result: Mapping[str, Any]) -> None:
    if role == "panel-candidate":
        raise VerticalBuildError("panel candidate checkpoint requires provenance envelope")
    _write_exact(_stage_path(output_root, role), canonical_json_bytes(dict(result)))


_PANEL_CANDIDATE_PREDECESSORS = (
    "ledger",
    "baseline",
    "attribution",
    "analytics",
    "report",
    "workbook",
)


def _panel_candidate_checkpoint_error(detail: str) -> VerticalBuildError:
    return VerticalBuildError(f"panel candidate checkpoint {detail}")


def _panel_candidate_predecessors(output_root: Path) -> dict[str, dict[str, Any]]:
    predecessors: dict[str, dict[str, Any]] = {}
    for role in _PANEL_CANDIDATE_PREDECESSORS:
        sha256, size_bytes = _sha(_stage_path(output_root, role))
        predecessors[role] = {"sha256": sha256, "size_bytes": size_bytes}
    return predecessors


def _panel_candidate_checkpoint(
    recipe_path: Path,
    recipe: Mapping[str, Any],
    output_root: Path,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = _path(result.get("candidate_dir"), "panel candidate")
    recipe_sha256, recipe_size_bytes = _sha(recipe_path)
    return {
        "schema_version": 1,
        "role": "panel-candidate",
        "recipe": {
            "sha256": recipe_sha256,
            "size_bytes": recipe_size_bytes,
        },
        "release_commit": _text(recipe.get("release_commit"), "release_commit"),
        "predecessors": _panel_candidate_predecessors(output_root),
        "candidate": {
            "path": str(candidate),
            **_directory_inventory(candidate),
        },
        "result": dict(result),
    }


def _record_panel_candidate_checkpoint(
    recipe_path: Path,
    recipe: Mapping[str, Any],
    output_root: Path,
    result: Mapping[str, Any],
) -> None:
    checkpoint_path = _stage_path(output_root, "panel-candidate")
    if checkpoint_path.exists():
        raise _panel_candidate_checkpoint_error("already exists")
    envelope = _panel_candidate_checkpoint(recipe_path, recipe, output_root, result)
    try:
        _write_exact(checkpoint_path, canonical_json_bytes(envelope))
    except VerticalBuildError as exc:
        raise _panel_candidate_checkpoint_error("cannot be created") from exc


def _load_strict_panel_candidate_checkpoint(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise _panel_candidate_checkpoint_error("is not a regular file")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _panel_candidate_checkpoint_error("contains duplicate JSON keys")
            value[key] = item
        return value

    try:
        content = path.read_bytes()
        value = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VerticalBuildError,
    ) as exc:
        raise _panel_candidate_checkpoint_error("is not readable strict JSON") from exc
    if type(value) is not dict:
        raise _panel_candidate_checkpoint_error("root must be an object")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise _panel_candidate_checkpoint_error("is not canonical JSON") from exc
    if content != canonical:
        raise _panel_candidate_checkpoint_error("bytes are not canonical")
    return value


def _validate_panel_candidate_checkpoint_shape(envelope: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "role",
        "recipe",
        "release_commit",
        "predecessors",
        "candidate",
        "result",
    }
    if set(envelope) != required:
        raise _panel_candidate_checkpoint_error("keys differ")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise _panel_candidate_checkpoint_error("schema differs")
    if type(envelope["role"]) is not str or envelope["role"] != "panel-candidate":
        raise _panel_candidate_checkpoint_error("role differs")
    if type(envelope["release_commit"]) is not str:
        raise _panel_candidate_checkpoint_error("release_commit differs")
    for label, definition, keys in (
        ("recipe", envelope["recipe"], {"sha256", "size_bytes"}),
        ("candidate", envelope["candidate"], {"path", "file_count", "inventory_sha256"}),
    ):
        if type(definition) is not dict or set(definition) != keys:
            raise _panel_candidate_checkpoint_error(f"{label} shape differs")
    recipe = envelope["recipe"]
    candidate = envelope["candidate"]
    assert type(recipe) is dict and type(candidate) is dict
    if type(recipe["sha256"]) is not str or type(recipe["size_bytes"]) is not int:
        raise _panel_candidate_checkpoint_error("recipe scalar types differ")
    if (
        type(candidate["path"]) is not str
        or type(candidate["file_count"]) is not int
        or type(candidate["inventory_sha256"]) is not str
    ):
        raise _panel_candidate_checkpoint_error("candidate scalar types differ")
    predecessors = envelope["predecessors"]
    if type(predecessors) is not dict or set(predecessors) != set(
        _PANEL_CANDIDATE_PREDECESSORS
    ):
        raise _panel_candidate_checkpoint_error("predecessors shape differs")
    for role in _PANEL_CANDIDATE_PREDECESSORS:
        definition = predecessors[role]
        if (
            type(definition) is not dict
            or set(definition) != {"sha256", "size_bytes"}
            or type(definition["sha256"]) is not str
            or type(definition["size_bytes"]) is not int
        ):
            raise _panel_candidate_checkpoint_error("predecessor scalar types differ")
    if type(envelope["result"]) is not dict:
        raise _panel_candidate_checkpoint_error("result differs")


def _load_panel_candidate_checkpoint(
    recipe_path: Path, recipe: Mapping[str, Any], output_root: Path
) -> dict[str, Any]:
    try:
        envelope = _load_strict_panel_candidate_checkpoint(
            _stage_path(output_root, "panel-candidate")
        )
    except VerticalBuildError as exc:
        raise _panel_candidate_checkpoint_error("is unreadable") from exc
    _validate_panel_candidate_checkpoint_shape(envelope)
    result = envelope.get("result")
    assert type(result) is dict
    try:
        expected = _panel_candidate_checkpoint(recipe_path, recipe, output_root, result)
    except VerticalBuildError as exc:
        raise _panel_candidate_checkpoint_error("nested result differs") from exc
    if envelope.get("recipe") != expected["recipe"]:
        raise _panel_candidate_checkpoint_error("recipe differs")
    if envelope.get("release_commit") != expected["release_commit"]:
        raise _panel_candidate_checkpoint_error("release_commit differs")
    if envelope.get("predecessors") != expected["predecessors"]:
        raise _panel_candidate_checkpoint_error("predecessors differ")
    if envelope.get("candidate") != expected["candidate"]:
        raise _panel_candidate_checkpoint_error("candidate differs")
    try:
        nested_candidate = _path(result.get("candidate_dir"), "panel candidate")
    except VerticalBuildError as exc:
        raise _panel_candidate_checkpoint_error("nested result differs") from exc
    if nested_candidate != Path(str(expected["candidate"]["path"])):
        raise _panel_candidate_checkpoint_error("nested candidate differs")
    return result


def _validate_stage_financial_arguments(
    recipe: Mapping[str, Any],
    *,
    manifest_ledger: Path | None,
    manifest_ledger_identity: Path | None,
    manifest_classified_flows: Path | None,
    manifest_admission_policy: Path | None,
) -> None:
    supplied = {
        "ledger": manifest_ledger,
        "ledger_identity": manifest_ledger_identity,
        "classified_flows": manifest_classified_flows,
        "admission_policy": manifest_admission_policy,
    }
    expected = _financial_epoch_inputs(recipe)
    if expected is None:
        if any(path is not None for path in supplied.values()):
            raise VerticalBuildError(
                "non-financial stage cannot receive epoch-manifest inputs"
            )
        return
    if any(path is None for path in supplied.values()):
        raise VerticalBuildError(
            "financial stage requires all four epoch-manifest inputs"
        )
    for key, expected_path in expected.items():
        observed = supplied[key]
        assert observed is not None
        if observed.resolve(strict=True) != expected_path.resolve(strict=True):
            raise VerticalBuildError(f"financial stage {key} authority differs")


def build_stage(
    recipe_path: Path,
    output_root: Path,
    role: str,
    *,
    qa_dir: Path | None = None,
    manifest_ledger: Path | None = None,
    manifest_ledger_identity: Path | None = None,
    manifest_classified_flows: Path | None = None,
    manifest_admission_policy: Path | None = None,
) -> dict[str, Any]:
    """Build exactly one source-to-panel stage from its predecessor outputs."""

    stages = output_root / "stages"
    recipe = _recipe(recipe_path)
    _validate_stage_financial_arguments(
        recipe,
        manifest_ledger=manifest_ledger,
        manifest_ledger_identity=manifest_ledger_identity,
        manifest_classified_flows=manifest_classified_flows,
        manifest_admission_policy=manifest_admission_policy,
    )
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
        analytics = _stage_result(output_root, "analytics")
        result = build_report_candidate(
            stages / "baseline",
            stages / "ledger",
            _path(ledger.get("freshness_health"), "freshness health"),
            history_contract,
            implementation_reconciliation,
            implementation_return_attribution,
            strategy_attribution_root,
            stages / "report-candidate",
            _path(analytics.get("correlation_root"), "correlation root"),
        )
    elif role == "workbook":
        ledger = _stage_result(output_root, "ledger")
        report = _stage_result(output_root, "report")
        analytics = _stage_result(output_root, "analytics")
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
            _path(analytics.get("correlation_root"), "correlation root"),
        )
    elif role in {"panel-candidate", "panel"}:
        ledger = _stage_result(output_root, "ledger")
        attribution = _stage_result(output_root, "attribution")
        analytics = _stage_result(output_root, "analytics")
        _stage_result(output_root, "workbook")
        candidate_result_path = _stage_path(output_root, "panel-candidate")
        panel = (
            _load_panel_candidate_checkpoint(recipe_path, recipe, output_root)
            if candidate_result_path.is_file()
            else build_panel_candidate(
                recipe_path,
                stages / "report",
                stages / "baseline",
                _path(attribution.get("destination"), "attribution destination"),
                analytics,
                _path(ledger.get("freshness_health"), "freshness health"),
                stages / "panel",
            )
        )
        if not candidate_result_path.is_file():
            _record_panel_candidate_checkpoint(recipe_path, recipe, output_root, panel)
        if role == "panel-candidate":
            result = panel
        elif qa_dir is not None:
            final_recipe = _recipe(recipe_path)
            _validate_stage_financial_arguments(
                final_recipe,
                manifest_ledger=manifest_ledger,
                manifest_ledger_identity=manifest_ledger_identity,
                manifest_classified_flows=manifest_classified_flows,
                manifest_admission_policy=manifest_admission_policy,
            )
            panel = _load_panel_candidate_checkpoint(
                recipe_path, final_recipe, output_root
            )
            staged_manifest = panel.get("account_strategy_manifest")
            result = publish_panel(
                recipe_path,
                stages / "report",
                stages / "baseline",
                _path(attribution.get("destination"), "attribution destination"),
                analytics,
                _path(ledger.get("freshness_health"), "freshness health"),
                _path(panel.get("candidate_dir"), "panel candidate"),
                qa_dir,
                stages / "panel",
                account_strategy_manifest=(
                    _path(staged_manifest, "staged account strategy manifest")
                    if staged_manifest is not None
                    else None
                ),
                expected_account_strategy_manifest_sha256=(
                    _text(
                        panel.get("account_strategy_manifest_sha256"),
                        "staged account strategy manifest sha256",
                    )
                    if staged_manifest is not None
                    else None
                ),
                expected_account_strategy_manifest_size_bytes=(
                    panel.get("account_strategy_manifest_size_bytes")
                    if staged_manifest is not None
                    and isinstance(
                        panel.get("account_strategy_manifest_size_bytes"), int
                    )
                    else None
                ),
            )
        else:
            raise VerticalBuildError("panel publication requires browser QA")
    else:
        raise VerticalBuildError(f"unsupported vertical stage: {role}")
    if role != "panel-candidate":
        _record_stage(output_root, role, result)
    return result


def build_all(recipe_path: Path, output_root: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    inputs = _financial_epoch_inputs(recipe)
    financial_arguments: dict[str, Path] = (
        {
            "manifest_ledger": inputs["ledger"],
            "manifest_ledger_identity": inputs["ledger_identity"],
            "manifest_classified_flows": inputs["classified_flows"],
            "manifest_admission_policy": inputs["admission_policy"],
        }
        if inputs is not None
        else {}
    )
    ledger = build_stage(recipe_path, output_root, "ledger", **financial_arguments)
    baseline = build_stage(recipe_path, output_root, "baseline", **financial_arguments)
    attribution = build_stage(
        recipe_path, output_root, "attribution", **financial_arguments
    )
    analytics = build_stage(
        recipe_path, output_root, "analytics", **financial_arguments
    )
    report_candidate = build_stage(
        recipe_path, output_root, "report", **financial_arguments
    )
    report = build_stage(recipe_path, output_root, "workbook", **financial_arguments)
    panel = build_stage(
        recipe_path, output_root, "panel-candidate", **financial_arguments
    )
    result = {
        "analytics": analytics,
        "attribution": attribution,
        "baseline": baseline,
        "ledger": ledger,
        "panel": panel,
        "release_commit": recipe["release_commit"],
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
    recipe.add_argument("--account-strategy-ledger", type=Path)
    recipe.add_argument("--account-strategy-ledger-identity", type=Path)
    recipe.add_argument("--account-strategy-classified-flows", type=Path)
    recipe.add_argument("--account-strategy-admission-policy", type=Path)
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
    stage.add_argument("--manifest-ledger", type=Path)
    stage.add_argument("--manifest-ledger-identity", type=Path)
    stage.add_argument("--manifest-classified-flows", type=Path)
    stage.add_argument("--manifest-admission-policy", type=Path)
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
                account_strategy_ledger=args.account_strategy_ledger,
                account_strategy_ledger_identity=args.account_strategy_ledger_identity,
                account_strategy_classified_flows=args.account_strategy_classified_flows,
                account_strategy_admission_policy=args.account_strategy_admission_policy,
            )
        else:
            result = build_stage(
                args.recipe,
                args.output_root,
                args.role,
                qa_dir=args.qa_dir,
                manifest_ledger=args.manifest_ledger,
                manifest_ledger_identity=args.manifest_ledger_identity,
                manifest_classified_flows=args.manifest_classified_flows,
                manifest_admission_policy=args.manifest_admission_policy,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"portfolio-release-builder: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
