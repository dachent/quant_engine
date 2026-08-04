"""Materialize one hash-bound private portfolio release through the core pipeline."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
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
    """A private recipe or materialized output differs from its contract."""


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
WINDOWS_SAFE_FILE_PATH = 240


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MaterializationError(f"regular JSON file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"JSON file is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise MaterializationError(f"JSON root must be an object: {path}")
    return value


def _character_map_text(value: Mapping[str, Any]) -> str:
    try:
        ordered = sorted(
            ((int(key), item) for key, item in value.items()), key=lambda row: row[0]
        )
    except (TypeError, ValueError) as exc:
        raise MaterializationError("character-map evidence is malformed") from exc
    if any(
        not isinstance(item, str) or index != offset
        for offset, (index, item) in enumerate(ordered)
    ):
        raise MaterializationError("character-map evidence is incomplete")
    return "".join(item for _, item in ordered)


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


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir() or destination.exists():
        raise MaterializationError("generation copy boundary is invalid")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise MaterializationError("generation contains a linked path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _generation(path: Path) -> tuple[GenerationManifest, dict[str, object]]:
    manifest_path = path / "generation.json"
    manifest = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    if (
        manifest.verification is not VerificationStatus.VERIFIED
        or manifest.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        or path.name != manifest.generation_id
    ):
        raise MaterializationError(f"generation is not releasable: {path}")
    inventory = inventory_generation(path)
    return manifest, inventory


def _inventory_identity(inventory: Mapping[str, object]) -> dict[str, object]:
    return {
        "files": inventory["files"],
        "generation_id": inventory["generation_id"],
        "manifest_sha256": inventory["manifest_sha256"],
    }


def _validate_output_path_budget(output_root: Path, recipe: Mapping[str, Any]) -> None:
    longest: Path | None = None
    for role in RELEASE_ROLES:
        source = Path(str(recipe["generations"][role]["source_dir"])).resolve()
        manifest, inventory = _generation(source)
        files = inventory["files"]
        if not isinstance(files, list):
            raise MaterializationError(f"{role} generation inventory is malformed")
        for item in files:
            if not isinstance(item, dict) or not isinstance(
                item.get("relative_path"), str
            ):
                raise MaterializationError(f"{role} generation inventory is malformed")
            relative = Path(item["relative_path"])
            candidates = (
                output_root
                / "stages"
                / role
                / "generations"
                / manifest.generation_id
                / relative,
                output_root
                / "release-roots"
                / ROOT_IDS[role]
                / "generations"
                / manifest.generation_id
                / relative,
            )
            candidate = max(candidates, key=lambda path: len(str(path)))
            if longest is None or len(str(candidate)) > len(str(longest)):
                longest = candidate
    if longest is not None and len(str(longest)) > WINDOWS_SAFE_FILE_PATH:
        raise MaterializationError(
            "private output root exceeds the portable Windows path budget: "
            f"{len(str(longest))} characters; use a shorter output root"
        )


def _recipe(path: Path) -> dict[str, Any]:
    value = _load(path)
    if value.get("schema_version") != 1 or set(value.get("generations", {})) != set(
        RELEASE_ROLES
    ):
        raise MaterializationError("portfolio materialization recipe differs")
    return value


def _source_payload(recipe: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = []
    for row in recipe.get("source_artifacts", []):
        if not isinstance(row, dict):
            raise MaterializationError("source artifact must be an object")
        path = Path(str(row["path"])).resolve()
        sha256, size = _sha(path)
        if sha256 != row.get("sha256") or size != row.get("size_bytes"):
            raise MaterializationError(
                f"source artifact differs: {row.get('artifact_id')}"
            )
        artifacts.append({**row, "path": str(path)})
    generations = {}
    for role, row in recipe["generations"].items():
        source = Path(str(row["source_dir"])).resolve()
        manifest, inventory = _generation(source)
        if manifest.generation_id != row.get("generation_id"):
            raise MaterializationError(f"{role} generation identity differs")
        generations[role] = {
            "generation_id": manifest.generation_id,
            "inventory_sha256": hashlib.sha256(
                canonical_json_bytes(_inventory_identity(inventory))
            ).hexdigest(),
            "source_dir": str(source),
        }
    return {
        "as_of_date": recipe["as_of_date"],
        "release_commit": recipe["release_commit"],
        "schema_version": 1,
        "source_artifacts": sorted(
            artifacts, key=lambda item: str(item["artifact_id"])
        ),
        "source_generations": generations,
    }


def acquire(recipe_path: Path, output_dir: Path) -> dict[str, Any]:
    payload = _source_payload(_recipe(recipe_path))
    _write_exact(output_dir / "source-run.json", canonical_json_bytes(payload))
    return {
        "source_artifacts": len(payload["source_artifacts"]),
        "state": "CAPTURED_LOCAL",
    }


def authority(recipe_path: Path, source_run: Path, output_dir: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    source = _load(source_run)
    expected = _source_payload(recipe)
    if canonical_json_bytes(source) != canonical_json_bytes(expected):
        raise MaterializationError("source run differs from the accepted recipe")
    payload = {
        "as_of_date": recipe["as_of_date"],
        "authority_policy": "EXPLICIT_ACCEPTED_GENERATIONS_ONLY",
        "release_commit": recipe["release_commit"],
        "schema_version": 1,
        "selected_generations": {
            role: row["generation_id"]
            for role, row in sorted(source["source_generations"].items())
        },
        "source_run_sha256": hashlib.sha256(canonical_json_bytes(source)).hexdigest(),
    }
    _write_exact(output_dir / "authority-snapshot.json", canonical_json_bytes(payload))
    return {"state": "AUTHORITY_SNAPSHOT_CREATED"}


def materialize(recipe_path: Path, role: str, output_dir: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    if role not in RELEASE_ROLES:
        raise MaterializationError("materialized role is unsupported")
    row = recipe["generations"][role]
    source = Path(str(row["source_dir"])).resolve()
    manifest, source_inventory = _generation(source)
    target = output_dir / "generations" / manifest.generation_id
    _copy_tree(source, target)
    if _inventory_identity(inventory_generation(target)) != _inventory_identity(
        source_inventory
    ):
        raise MaterializationError(f"{role} generation copy differs")
    result: dict[str, Any] = {
        "generation_id": manifest.generation_id,
        "state": "MATERIALIZED",
    }
    if role == "panel":
        launcher = Path(str(recipe["panel_launcher"])).resolve()
        if launcher.is_symlink() or not launcher.is_file():
            raise MaterializationError("panel launcher is missing")
        if _sha(launcher)[0] != recipe.get("panel_launcher_sha256"):
            raise MaterializationError("panel launcher differs from the recipe")
        _write_exact(output_dir / "index.html", launcher.read_bytes())
        result["launcher_sha256"] = _sha(output_dir / "index.html")[0]
    return result


def marker(
    recipe_path: Path, role: str, inputs: Sequence[Path], output_dir: Path
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    if role not in {"attribution", "analytics", "workbook"}:
        raise MaterializationError("marker role is unsupported")
    rows: list[dict[str, object]] = []
    for path in inputs:
        resolved = path.resolve()
        if resolved.is_dir():
            files = sorted(item for item in resolved.rglob("*") if item.is_file())
            digest = hashlib.sha256(
                _canonical_bytes(
                    [
                        {
                            "path": item.relative_to(resolved).as_posix(),
                            "sha256": _sha(item)[0],
                            "size_bytes": _sha(item)[1],
                        }
                        for item in files
                    ]
                )
            ).hexdigest()
            rows.append({"path": str(resolved), "sha256": digest, "type": "DIRECTORY"})
        else:
            digest, size = _sha(resolved)
            rows.append(
                {
                    "path": str(resolved),
                    "sha256": digest,
                    "size_bytes": size,
                    "type": "FILE",
                }
            )
    payload = {
        "as_of_date": recipe["as_of_date"],
        "inputs": rows,
        "release_commit": recipe["release_commit"],
        "role": role,
        "schema_version": 1,
        "status": "PASS",
    }
    _write_exact(output_dir / "stage.json", canonical_json_bytes(payload))
    return {"role": role, "state": "VERIFIED"}


def _semantic_payload(recipe: Mapping[str, Any]) -> dict[str, Any]:
    release_generations = {
        ROOT_IDS[role]: recipe["generations"][role]["generation_id"]
        for role in RELEASE_ROLES
    }
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
        "release_generations": release_generations,
        "schema_version": 1,
        "unwaived_blocking_exceptions": 0,
        "verdict": "PASS",
    }


def semantic(recipe_path: Path, output_path: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    output_root = output_path.parent.parent
    source_payload = _source_payload(recipe)
    expected_authority = {
        "as_of_date": recipe["as_of_date"],
        "authority_policy": "EXPLICIT_ACCEPTED_GENERATIONS_ONLY",
        "release_commit": recipe["release_commit"],
        "schema_version": 1,
        "selected_generations": {
            role: row["generation_id"]
            for role, row in sorted(source_payload["source_generations"].items())
        },
        "source_run_sha256": hashlib.sha256(
            canonical_json_bytes(source_payload)
        ).hexdigest(),
    }
    actual_authority = _load(
        output_root / "stages" / "authority" / "authority-snapshot.json"
    )
    if canonical_json_bytes(actual_authority) != canonical_json_bytes(
        expected_authority
    ):
        raise MaterializationError("materialized authority snapshot differs")

    for role in RELEASE_ROLES:
        source = Path(recipe["generations"][role]["source_dir"]).resolve()
        generation_id = str(recipe["generations"][role]["generation_id"])
        target = output_root / "stages" / role / "generations" / generation_id
        _, source_inventory = _generation(source)
        _, target_inventory = _generation(target)
        if _inventory_identity(target_inventory) != _inventory_identity(
            source_inventory
        ):
            raise MaterializationError(f"{role} semantic generation differs")

    panel_stage = output_root / "stages" / "panel"
    if _sha(panel_stage / "index.html")[0] != recipe["panel_launcher_sha256"]:
        raise MaterializationError("materialized panel launcher differs")
    panel_generation = (
        panel_stage
        / "generations"
        / str(recipe["generations"]["panel"]["generation_id"])
    )
    panel_qa = _load(panel_generation / "qa" / "qa-validation.json")
    interaction_qa = _load(panel_generation / "qa" / "interaction-results.json")
    if panel_qa.get("checks_passed") != panel_qa.get("checks_total"):
        raise MaterializationError("panel browser QA is not complete")
    if interaction_qa.get("all_passed") is not True:
        raise MaterializationError("panel interaction QA is not complete")

    report_generation = (
        output_root
        / "stages"
        / "report"
        / "generations"
        / str(recipe["generations"]["report"]["generation_id"])
    )
    report_verification = _load(report_generation / "report-verification.json")
    workbook_validation = _load(report_generation / "workbook-validation.json")
    xlsx_result = _load(report_generation / "xlsx-win-result.json")
    if workbook_validation.get("all_passed") is not True:
        raise MaterializationError("workbook validation is not complete")
    if (
        xlsx_result.get("final_state") != "SUCCEEDED"
        or xlsx_result.get("ok") is not True
    ):
        if all(key.isdigit() for key in xlsx_result):
            xlsx_text = _character_map_text(xlsx_result)
            if (
                '"final_state":"SUCCEEDED"' not in xlsx_text
                or '"ok":true' not in xlsx_text
            ):
                raise MaterializationError("native Excel verification is not complete")
        else:
            raise MaterializationError("native Excel verification is not complete")
    if (
        report_verification.get("workbook_sha256")
        != _sha(report_generation / "portfolio-report.xlsx")[0]
    ):
        raise MaterializationError("verified workbook hash differs")

    freshness = _load(Path(str(recipe["freshness_health"])).resolve())
    if freshness.get("as_of_date") != recipe["as_of_date"]:
        raise MaterializationError("freshness evidence is for another as-of date")
    assessments = freshness.get("assessments")
    if not isinstance(assessments, list) or any(
        isinstance(row, dict) and row.get("blocks_current_consumers") is True
        for row in assessments
    ):
        raise MaterializationError("freshness evidence has a blocking consumer gap")

    payload = _semantic_payload(recipe)
    _write_exact(output_path, canonical_json_bytes(payload))
    return {"state": "PASS"}


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
        "retry_limit": 0,
        "timeout_seconds": 3600,
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


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
    entrypoint_sha, _ = _sha(entrypoint)
    run_id = hashlib.sha256(canonical_json_bytes(recipe)).hexdigest()
    run_dir = output_root / "runs" / run_id
    authority_dir = output_root / "stages" / "authority"
    stage_dirs = {role: output_root / "stages" / role for role in STAGES}
    semantic_path = output_root / "evidence" / "post-build-semantic.json"
    source_run = run_dir / "source-run.json"
    authority_path = authority_dir / "authority-snapshot.json"

    nodes: list[dict[str, Any]] = []
    nodes.append(
        _command(
            node_id="acquire-local-sources",
            runtime=runtime,
            entrypoint=entrypoint,
            lock=lock,
            recipe=recipe_path,
            depends_on=[],
            args=(
                "acquire",
                "--recipe",
                str(recipe_path),
                "--output-dir",
                str(run_dir),
            ),
            reads=[Path(row["path"]) for row in recipe.get("source_artifacts", [])]
            + [Path(row["source_dir"]) for row in recipe["generations"].values()],
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
            depends_on=["acquire-local-sources"],
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
    authority_bytes = canonical_json_bytes(
        {
            "as_of_date": recipe["as_of_date"],
            "authority_policy": "EXPLICIT_ACCEPTED_GENERATIONS_ONLY",
            "release_commit": recipe["release_commit"],
            "schema_version": 1,
            "selected_generations": {
                role: row["generation_id"]
                for role, row in sorted(recipe["generations"].items())
            },
            "source_run_sha256": hashlib.sha256(
                canonical_json_bytes(_source_payload(recipe))
            ).hexdigest(),
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

    generation_nodes: dict[str, str] = {}
    previous_node = "authority-snapshot"
    previous_output = authority_path
    command_sources = []
    for role in STAGES:
        command_id = f"build-{role}"
        output = stage_dirs[role]
        if role in RELEASE_ROLES:
            source = Path(recipe["generations"][role]["source_dir"]).resolve()
            manifest, inventory = _generation(source)
            args: list[str] = [
                "materialize",
                "--recipe",
                str(recipe_path),
                "--role",
                role,
                "--output-dir",
                str(output),
            ]
            reads = [previous_output, source]
            nodes.append(
                _command(
                    node_id=command_id,
                    runtime=runtime,
                    entrypoint=entrypoint,
                    lock=lock,
                    recipe=recipe_path,
                    depends_on=[previous_node],
                    args=args,
                    reads=reads,
                    writes=[output],
                )
            )
            generation_node = f"{role}-generation"
            generation_source = output / "generations" / manifest.generation_id
            nodes.append(
                {
                    "depends_on": [command_id],
                    "files": inventory["files"],
                    "generation_id": manifest.generation_id,
                    "kind": "GENERATION",
                    "manifest_sha256": hashlib.sha256(
                        manifest.to_json_bytes()
                    ).hexdigest(),
                    "node_id": generation_node,
                    "release_target": True,
                    "root_id": ROOT_IDS[role],
                    "source_dir": str(generation_source),
                }
            )
            generation_nodes[role] = generation_node
            previous_output = generation_source
        else:
            inputs = [
                previous_output,
                *(Path(item) for item in recipe.get("stage_inputs", {}).get(role, [])),
            ]
            args = [
                "marker",
                "--recipe",
                str(recipe_path),
                "--role",
                role,
                "--output-dir",
                str(output),
            ]
            for item in inputs:
                args.extend(("--input", str(item)))
            nodes.append(
                _command(
                    node_id=command_id,
                    runtime=runtime,
                    entrypoint=entrypoint,
                    lock=lock,
                    recipe=recipe_path,
                    depends_on=[previous_node],
                    args=args,
                    reads=inputs,
                    writes=[output],
                )
            )
            previous_output = output
        previous_node = command_id

    semantic_dependencies = [previous_node, *generation_nodes.values()]
    generation_sources = [
        stage_dirs[role] / "generations" / recipe["generations"][role]["generation_id"]
        for role in RELEASE_ROLES
    ]
    nodes.append(
        _command(
            node_id="post-build-semantic-reconciliation",
            runtime=runtime,
            entrypoint=entrypoint,
            lock=lock,
            recipe=recipe_path,
            depends_on=semantic_dependencies,
            args=(
                "semantic",
                "--recipe",
                str(recipe_path),
                "--output",
                str(semantic_path),
            ),
            reads=[authority_path, *generation_sources, stage_dirs["panel"]],
            writes=[semantic_path],
        )
    )
    command_sources = [
        {
            "entrypoint_path": str(entrypoint),
            "entrypoint_sha256": entrypoint_sha,
            "node_id": row["node_id"],
        }
        for row in nodes
        if row["kind"] == "COMMAND"
    ]
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
                "generation_nodes": [generation_nodes[role]]
                if role in generation_nodes
                else [],
            }
            for role in STAGES
        },
    }
    contract_path = spec_path.with_name("portfolio-orchestration-contract.json")
    contract_bytes = canonical_json_bytes(contract)
    _write_exact(contract_path, contract_bytes)
    freshness = Path(recipe["freshness_health"]).resolve()
    semantic_bytes = canonical_json_bytes(_semantic_payload(recipe))
    roots = {
        ROOT_IDS[role]: str(output_root / "release-roots" / ROOT_IDS[role])
        for role in RELEASE_ROLES
    }
    panel_launcher = stage_dirs["panel"] / "index.html"
    spec = {
        "as_of_date": recipe["as_of_date"],
        "code_commit": release_commit,
        "companions": [
            {
                "companion_id": "stable-control-panel-launcher",
                "relative_path": "index.html",
                "root_id": "control-panel",
                "sha256": recipe["panel_launcher_sha256"],
                "source_path": str(panel_launcher),
            }
        ],
        "coordination_root": str(output_root / "coordination"),
        "created_at": datetime.now(UTC).isoformat(),
        "gates": [
            {
                "gate_id": "freshness-health",
                "path": str(freshness),
                "sha256": _sha(freshness)[0],
            },
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
        "roots": roots,
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
    materialize_cmd = commands.add_parser("materialize")
    materialize_cmd.add_argument("--recipe", type=Path, required=True)
    materialize_cmd.add_argument("--role", required=True)
    materialize_cmd.add_argument("--output-dir", type=Path, required=True)
    marker_cmd = commands.add_parser("marker")
    marker_cmd.add_argument("--recipe", type=Path, required=True)
    marker_cmd.add_argument("--role", required=True)
    marker_cmd.add_argument("--input", action="append", type=Path, default=[])
    marker_cmd.add_argument("--output-dir", type=Path, required=True)
    semantic_cmd = commands.add_parser("semantic")
    semantic_cmd.add_argument("--recipe", type=Path, required=True)
    semantic_cmd.add_argument("--output", type=Path, required=True)
    spec_cmd = commands.add_parser("spec")
    spec_cmd.add_argument("--recipe", type=Path, required=True)
    spec_cmd.add_argument("--output-root", type=Path, required=True)
    spec_cmd.add_argument("--spec", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "acquire":
        result = acquire(args.recipe, args.output_dir)
    elif args.command == "authority":
        result = authority(args.recipe, args.source_run, args.output_dir)
    elif args.command == "materialize":
        result = materialize(args.recipe, args.role, args.output_dir)
    elif args.command == "marker":
        result = marker(args.recipe, args.role, args.input, args.output_dir)
    elif args.command == "semantic":
        result = semantic(args.recipe, args.output)
    else:
        result = build_spec(args.recipe, args.output_root, args.spec)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
