"""Build one governed source-to-panel pipeline from immutable local inputs."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
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
from quant_engine.portfolio_ledger import validate_admit_or_declare_receipt


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
STAGE_DEPENDENCIES = {
    "ledger": (),
    "baseline": ("ledger",),
    "attribution": ("baseline", "ledger"),
    "analytics": ("attribution",),
    "report": ("baseline", "ledger", "analytics"),
    "workbook": ("baseline", "ledger", "report"),
    "panel": ("ledger", "attribution", "analytics", "workbook"),
}
SEMANTIC_FIRST_STAGE = {
    "AS_HELD_COMPOSITE_HWM": "analytics",
    "COMPOSITION": "ledger",
    "PROVIDER_LEDGER": "ledger",
    "LAYERED_HISTORY": "attribution",
    "LAYERED_HISTORY_LIVE_PLUS_BACKTEST": "attribution",
    "DECOMPOSITION": "attribution",
    "HWM": "analytics",
    "INVESTED_CAPITAL": "analytics",
    "DEPENDENCE": "analytics",
    "REPORT": "report",
    "WORKBOOK": "workbook",
    "PANEL": "panel",
    "RELEASE_RECEIPT": "panel",
}
PRODUCT_AUTHORITY_GATE_IDS = {
    "contract": "financial-mvp-contract-authority",
    "source_matrix": "financial-mvp-source-matrix-authority",
    "provider_manifest": "financial-mvp-provider-manifest-authority",
    "provider_catalog": "financial-mvp-provider-catalog-authority",
    "provider_receipt": "financial-mvp-provider-admission-authority",
}
RELEASE_ROLES = ("ledger", "baseline", "report", "panel")
ROOT_IDS = {
    "ledger": "provider-ledger",
    "baseline": "baseline",
    "report": "reporting",
    "panel": "control-panel",
}
PRODUCTION_ROOTS = {
    "correlation": Path("analytics/correlation"),
    "europe-economic": Path("analytics/decomposition-lenses/europe-economic"),
    "factor-decomposition": Path("analytics/decomposition"),
    "hybrid-dynamic-style": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/dynamic-style"
    ),
    "hybrid-europe-economic": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/europe-economic"
    ),
    "hybrid-multi-asset-style": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/multi-asset-style"
    ),
    "hybrid-us-economic": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-economic"
    ),
    "hybrid-us-equity-style": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-equity-style"
    ),
    "hybrid-us-industry": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-industry"
    ),
    "hybrid-us-sector": Path(
        "analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-sector"
    ),
    "multi-asset-style": Path("analytics/decomposition-lenses/multi-asset-style"),
    "portfolio-baseline": Path("baseline"),
    "portfolio-panel": Path("control-panel"),
    "portfolio-report": Path("reporting"),
    "provider-ledger": Path("provider-ledger"),
    "us-economic": Path("analytics/decomposition-lenses/us-economic"),
    "us-equity-style": Path("analytics/decomposition-lenses/us-equity-style"),
    "us-industry": Path("analytics/decomposition-lenses/us-industry"),
    "us-sector": Path("analytics/decomposition-lenses/us-sector"),
    "weekly-returns": Path("analytics/weekly-returns"),
}
PRODUCTION_STAGE_ROOTS = {
    "correlation": Path("stages/analytics/correlation"),
    "europe-economic": Path("stages/analytics/decomposition-lenses/europe-economic"),
    "factor-decomposition": Path("stages/analytics/decomposition"),
    "hybrid-dynamic-style": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/dynamic-style"
    ),
    "hybrid-europe-economic": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/europe-economic"
    ),
    "hybrid-multi-asset-style": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/multi-asset-style"
    ),
    "hybrid-us-economic": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-economic"
    ),
    "hybrid-us-equity-style": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-equity-style"
    ),
    "hybrid-us-industry": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-industry"
    ),
    "hybrid-us-sector": Path(
        "stages/analytics/decomposition-lenses/LIVE_PLUS_BACKTEST/us-sector"
    ),
    "multi-asset-style": Path(
        "stages/analytics/decomposition-lenses/multi-asset-style"
    ),
    "portfolio-baseline": Path("stages/baseline"),
    "portfolio-panel": Path("stages/panel"),
    "portfolio-report": Path("stages/report"),
    "provider-ledger": Path("stages/ledger"),
    "us-economic": Path("stages/analytics/decomposition-lenses/us-economic"),
    "us-equity-style": Path("stages/analytics/decomposition-lenses/us-equity-style"),
    "us-industry": Path("stages/analytics/decomposition-lenses/us-industry"),
    "us-sector": Path("stages/analytics/decomposition-lenses/us-sector"),
    "weekly-returns": Path("stages/analytics/weekly-returns"),
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FINANCIAL_MVP_SCHEMA = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "financial-mvp-source-contract.schema.json"
)
CURRENT_STRATEGY_REGISTRY = (
    REPOSITORY_ROOT / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
)
FINANCIAL_MVP_TOOL = REPOSITORY_ROOT / "tools" / "portfolio_financial_mvp_contract.py"
HYBRID_PANEL_ROOT_IDS = {
    "dynamic-style": "hybrid-dynamic-style",
    "europe-economic": "hybrid-europe-economic",
    "us-economic": "hybrid-us-economic",
    "us-equity-style": "hybrid-us-equity-style",
    "us-industry": "hybrid-us-industry",
    "us-sector": "hybrid-us-sector",
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


def _canonical_clone(value: object) -> Any:
    """Return a deterministic JSON-only copy without retaining mutable aliases."""

    return json.loads(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def _downstream_stages(first_stage: str) -> list[str]:
    if first_stage not in STAGES:
        raise MaterializationError(f"unknown semantic rebuild stage: {first_stage}")
    affected = {first_stage}
    changed = True
    while changed:
        changed = False
        for stage in STAGES:
            if stage in affected:
                continue
            if any(dependency in affected for dependency in STAGE_DEPENDENCIES[stage]):
                affected.add(stage)
                changed = True
    return [stage for stage in STAGES if stage in affected]


def _semantic_output_route(output_role: object) -> dict[str, Any]:
    role = _text(output_role, "semantic output role")
    first_stage = SEMANTIC_FIRST_STAGE.get(role)
    if first_stage is None:
        raise MaterializationError(f"semantic output has no rebuild route: {role}")
    return {
        "affected_stages": _downstream_stages(first_stage),
        "first_stage": first_stage,
        "output_role": role,
    }


def _authority_registry(
    path: Path | None,
    *,
    schema_name: str,
    record_key: str,
) -> dict[str, Any]:
    """Read an optional #322/#326 producer envelope through one strict consumer seam."""

    if path is None:
        return {
            "authority_state": "REQUIRED_NOT_READY_NOT_PROVIDED",
            "file": None,
            "records": [],
            "registry_sha256": None,
            "schema_name": schema_name,
            "schema_version": 1,
        }
    path = path.resolve()
    payload = _load(path, schema_name)
    required = {
        "census_sha256",
        "contract_semantic_sha256",
        record_key,
        "registry_sha256",
        "schema_name",
        "schema_version",
    }
    if set(payload) != required or payload.get("schema_name") != schema_name:
        raise MaterializationError(f"{schema_name} shape differs")
    if payload.get("schema_version") != 1:
        raise MaterializationError(f"{schema_name} version differs")
    records = payload.get(record_key)
    if not isinstance(records, list):
        raise MaterializationError(f"{schema_name} records differ")
    declared_sha256 = _text(payload.get("registry_sha256"), f"{schema_name} digest")
    if len(declared_sha256) != 64:
        raise MaterializationError(f"{schema_name} digest differs")
    digest_payload = dict(payload)
    digest_payload.pop("registry_sha256")
    actual_sha256 = hashlib.sha256(canonical_json_bytes(digest_payload)).hexdigest()
    if declared_sha256 != actual_sha256:
        raise MaterializationError(f"{schema_name} digest differs")
    return {
        "authority_state": "PROVIDED",
        "census_sha256": payload["census_sha256"],
        "contract_semantic_sha256": payload["contract_semantic_sha256"],
        "file": _file_definition(path),
        "records": _canonical_clone(records),
        "registry_sha256": declared_sha256,
        "schema_name": schema_name,
        "schema_version": 1,
    }


def _panel_hybrid_generation_ids(panel_payload: Mapping[str, Any]) -> dict[str, str]:
    lenses = panel_payload.get("decomposition_lenses")
    modes = lenses.get("modes") if isinstance(lenses, dict) else None
    hybrid = modes.get("LIVE_PLUS_BACKTEST") if isinstance(modes, dict) else None
    if not isinstance(hybrid, list):
        raise MaterializationError("prepared panel hybrid analytics authority differs")

    source_ids: dict[str, str] = {}
    for lens in hybrid:
        if not isinstance(lens, dict):
            raise MaterializationError(
                "prepared panel hybrid analytics authority differs"
            )
        if lens.get("lens_id") == "DYNAMIC_STYLE":
            if "dynamic-style" in source_ids:
                raise MaterializationError(
                    "prepared panel hybrid analytics authority differs"
                )
            source_ids["dynamic-style"] = _text(
                lens.get("generation_id"), "hybrid dynamic generation_id"
            )
        sources = lens.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                raise MaterializationError(
                    "prepared panel hybrid analytics authority differs"
                )
            source_key = source.get("source_key")
            if (
                not isinstance(source_key, str)
                or source_key not in HYBRID_PANEL_ROOT_IDS
            ):
                continue
            if source_key in source_ids:
                raise MaterializationError(
                    "prepared panel hybrid analytics authority differs"
                )
            source_ids[source_key] = _text(
                source.get("generation_id"), f"hybrid {source_key} generation_id"
            )
    if set(source_ids) != set(HYBRID_PANEL_ROOT_IDS):
        raise MaterializationError("prepared panel hybrid analytics authority differs")
    return {
        HYBRID_PANEL_ROOT_IDS[source_key]: generation_id
        for source_key, generation_id in source_ids.items()
    }


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


def _financial_mvp_module() -> Any:
    """Load the exact repository tool without trusting a cached module object."""

    module_name = "_portfolio_financial_mvp_contract_for_pipeline"
    spec = importlib.util.spec_from_file_location(module_name, FINANCIAL_MVP_TOOL)
    if spec is None or spec.loader is None:
        raise MaterializationError("financial MVP contract validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise MaterializationError("financial MVP contract validator failed") from exc
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    module_file = getattr(module, "__file__", None)
    if (
        module_file is None
        or Path(module_file).resolve() != FINANCIAL_MVP_TOOL.resolve()
    ):
        raise MaterializationError("financial MVP contract validator origin differs")
    return module


def _manifest_artifact_bytes(
    manifest: GenerationManifest,
    generation: Path,
    artifact_id: str,
    relative_path: str,
    schema_name: str,
    schema_version: str,
) -> bytes:
    matches = [item for item in manifest.artifacts if item.artifact_id == artifact_id]
    if len(matches) != 1:
        raise MaterializationError(f"provider ledger artifact differs: {artifact_id}")
    artifact = matches[0]
    path = generation / relative_path
    if path.is_symlink() or not path.is_file():
        raise MaterializationError(
            f"provider ledger artifact is unavailable: {artifact_id}"
        )
    content = path.read_bytes()
    if (
        artifact.sha256 != hashlib.sha256(content).hexdigest()
        or artifact.size_bytes != len(content)
        or artifact.schema_name != schema_name
        or artifact.schema_version != schema_version
    ):
        raise MaterializationError(
            f"provider ledger artifact identity differs: {artifact_id}"
        )
    return content


def _provider_admission_authority(
    generation: Path,
    contract: Mapping[str, Any],
    *,
    contract_raw_sha256: str,
    contract_semantic_sha256: str,
    census_sha256: str,
) -> dict[str, Any]:
    if generation.is_symlink() or not generation.is_dir():
        raise MaterializationError("provider ledger generation is unavailable")
    if len(generation.name) != 64:
        raise MaterializationError("provider ledger generation identity differs")
    expected_files = {
        "admit-or-declare-receipt.json",
        "generation.json",
        "provider-ledger.json",
    }
    files = {
        item.relative_to(generation).as_posix()
        for item in generation.rglob("*")
        if item.is_file()
    }
    if files != expected_files or any(
        item.is_symlink() for item in generation.iterdir()
    ):
        raise MaterializationError("provider ledger generation file boundary differs")
    try:
        manifest_bytes = (generation / "generation.json").read_bytes()
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except (OSError, ValueError) as exc:
        raise MaterializationError("provider ledger manifest is invalid") from exc
    if (
        manifest.generation_id != generation.name
        or manifest.verification is not VerificationStatus.VERIFIED
        or manifest.adapter.name != "quant-engine-provider-ledger"
        or manifest.adapter.version != "2"
    ):
        raise MaterializationError("provider ledger manifest authority differs")
    receipt_bytes = _manifest_artifact_bytes(
        manifest,
        generation,
        "normalized/admit-or-declare-receipt.json",
        "admit-or-declare-receipt.json",
        "provider-admission-receipt",
        "1",
    )
    catalog_bytes = _manifest_artifact_bytes(
        manifest,
        generation,
        "normalized/provider-ledger.json",
        "provider-ledger.json",
        "provider-ledger-catalog",
        "2",
    )
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        catalog = json.loads(catalog_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError("provider ledger authority is unreadable") from exc
    if not isinstance(receipt, dict) or not isinstance(catalog, dict):
        raise MaterializationError("provider ledger authority root differs")
    if (
        catalog.get("catalog_schema_version") != 2
        or catalog.get("as_of_date") != manifest.as_of_date.isoformat()
    ):
        raise MaterializationError("provider ledger catalog authority differs")
    product = contract.get("product")
    census = contract.get("current_census")
    sources = contract.get("source_segments")
    if (
        not isinstance(product, dict)
        or not isinstance(census, dict)
        or not isinstance(sources, list)
    ):
        raise MaterializationError("financial MVP authority shape differs")
    source_segments = {
        str(row.get("source_id")): row for row in sources if isinstance(row, dict)
    }
    provider_rows = receipt.get("provider_generations")
    if not isinstance(provider_rows, list):
        raise MaterializationError("provider admission generations differ")
    provider_generations = {
        str(row.get("provider_id")): row
        for row in provider_rows
        if isinstance(row, dict)
    }
    try:
        validate_admit_or_declare_receipt(
            receipt,
            product_authority={
                "census_id": census.get("census_id"),
                "census_sha256": census_sha256,
                "contract_raw_sha256": contract_raw_sha256,
                "contract_semantic_sha256": contract_semantic_sha256,
                "product_id": product.get("product_id"),
                "product_version": product.get("product_version"),
            },
            source_segments=source_segments,
            provider_generations=provider_generations,
        )
    except (RuntimeError, ValueError) as exc:
        raise MaterializationError("provider admission receipt is invalid") from exc
    manifest_artifacts = tuple(manifest.artifacts)
    for row in provider_generations.values():
        if not isinstance(row, dict):
            raise MaterializationError("provider admission generation row differs")
        provider_id = str(row.get("provider_id"))
        prefix = f"providers/{provider_id}/"
        hashes = {
            item.sha256
            for item in manifest_artifacts
            if item.artifact_id.startswith(prefix)
        }
        if (
            row.get("manifest_sha256") not in hashes
            or row.get("catalog_sha256") not in hashes
        ):
            raise MaterializationError(
                f"provider admission generation is not manifest-bound: {provider_id}"
            )
    return {
        "admission_gate_status": receipt.get("admission_gate_status"),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "disposition_count": len(receipt.get("dispositions", [])),
        "generation_id": manifest.generation_id,
        "inventory_count": len(receipt.get("inventory", [])),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "product_readiness": receipt.get("product_readiness"),
        "provider_count": len(receipt.get("provider_generations", [])),
        "receipt": _canonical_clone(receipt),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }


def _product_conformance(
    contract: Mapping[str, Any],
    admission: Mapping[str, Any],
    correction_registry: Mapping[str, Any],
    rederive_registry: Mapping[str, Any],
    *,
    contract_raw_sha256: str,
    contract_semantic_sha256: str,
    census_sha256: str,
    source_matrix_sha256: str,
    invested_capital_policy_sha256: str,
) -> dict[str, Any]:
    product = contract["product"]
    strategies = contract["strategies"]
    sources = contract["source_segments"]
    outcomes = contract["financial_outcomes"]
    history = contract["history_corrections"]
    census = contract["current_census"]
    assert isinstance(product, dict)
    assert isinstance(strategies, list)
    assert isinstance(sources, list)
    assert isinstance(outcomes, list)
    assert isinstance(history, dict)
    assert isinstance(census, dict)
    sleeve_values = census.get("account_sleeves")
    if not isinstance(sleeve_values, list):
        raise MaterializationError("financial MVP census sleeve authority differs")
    census_memberships = []
    for sleeve in sleeve_values:
        if not isinstance(sleeve, dict) or not isinstance(
            sleeve.get("assignments"), list
        ):
            raise MaterializationError("financial MVP census membership differs")
        census_memberships.append(
            {
                "assignments": sorted(
                    (
                        {
                            "effective_end": row["effective_end"],
                            "effective_start": row["effective_start"],
                            "status": row["status"],
                            "strategy_id": row["strategy_id"],
                            "transition_group_id": row["transition_group_id"],
                        }
                        for row in sleeve["assignments"]
                        if isinstance(row, dict)
                    ),
                    key=lambda row: (
                        row["effective_start"],
                        row["strategy_id"],
                        str(row["effective_end"]),
                    ),
                ),
                "current_strategy_id": sleeve["current_strategy_id"],
                "provider_id": sleeve["provider_id"],
                "role": sleeve["role"],
                "sleeve_alias": sleeve["sleeve_alias"],
                "source_segment_ids": sorted(sleeve["source_segment_ids"]),
            }
        )
    source_rows = sorted(
        (_canonical_clone(row) for row in sources if isinstance(row, dict)),
        key=lambda row: row["source_id"],
    )
    outcome_rows = sorted(
        (_canonical_clone(row) for row in outcomes if isinstance(row, dict)),
        key=lambda row: row["outcome_id"],
    )
    strategy_rows = sorted(
        (_canonical_clone(row) for row in strategies if isinstance(row, dict)),
        key=lambda row: row["strategy_id"],
    )
    corrections = history.get("corrections")
    if not isinstance(corrections, list):
        raise MaterializationError("financial MVP correction authority differs")
    correction_rows = sorted(
        (_canonical_clone(row) for row in corrections if isinstance(row, dict)),
        key=lambda row: row["correction_id"],
    )

    runtime_corrections = correction_registry.get("records")
    runtime_rederivations = rederive_registry.get("records")
    if not isinstance(runtime_corrections, list) or not isinstance(
        runtime_rederivations, list
    ):
        raise MaterializationError("correction/re-derivation runtime authority differs")
    correction_ids: set[str] = set()
    applicable_open: list[str] = []
    resolved_applicable: dict[str, set[str]] = {}
    for raw in runtime_corrections:
        if not isinstance(raw, dict) or set(raw) != {
            "applicability",
            "correction_id",
            "disposition_sha256",
            "evidence_sha256",
            "invalidated_outputs",
            "rebuild_boundary",
            "status",
            "subject_ids",
        }:
            raise MaterializationError("correction registry record differs")
        correction_id = _text(raw["correction_id"], "correction_id")
        if correction_id in correction_ids:
            raise MaterializationError(f"duplicate correction record: {correction_id}")
        correction_ids.add(correction_id)
        status = _text(raw["status"], f"{correction_id}.status")
        applicability = _text(raw["applicability"], f"{correction_id}.applicability")
        if status not in {"OPEN", "RESOLVED", "DISPOSITIONED"} or applicability not in {
            "APPLICABLE",
            "NOT_APPLICABLE",
        }:
            raise MaterializationError(f"correction state differs: {correction_id}")
        subjects = raw["subject_ids"]
        outputs = raw["invalidated_outputs"]
        if (
            not isinstance(subjects, list)
            or not subjects
            or not all(isinstance(value, str) and value for value in subjects)
            or not isinstance(outputs, list)
            or not outputs
        ):
            raise MaterializationError(f"correction scope differs: {correction_id}")
        for output in outputs:
            _semantic_output_route(output)
        for field in ("evidence_sha256",):
            if len(_text(raw[field], f"{correction_id}.{field}")) != 64:
                raise MaterializationError(
                    f"correction evidence differs: {correction_id}"
                )
        disposition = raw["disposition_sha256"]
        if status == "OPEN":
            if disposition is not None:
                raise MaterializationError(
                    f"open correction has a disposition: {correction_id}"
                )
        elif not isinstance(disposition, str) or len(disposition) != 64:
            raise MaterializationError(
                f"closed correction lacks a disposition: {correction_id}"
            )
        if status == "OPEN" and applicability == "APPLICABLE":
            applicable_open.append(correction_id)
        if status in {"RESOLVED", "DISPOSITIONED"} and applicability == "APPLICABLE":
            resolved_applicable[correction_id] = {str(value) for value in outputs}

    rederived: dict[str, set[str]] = {}
    for raw in runtime_rederivations:
        if not isinstance(raw, dict) or set(raw) != {
            "correction_id",
            "input_generation_id",
            "invalidated_outputs",
            "output_generation_id",
            "receipt_sha256",
            "status",
        }:
            raise MaterializationError("re-derivation registry record differs")
        correction_id = _text(raw["correction_id"], "re-derivation correction_id")
        if correction_id in rederived or raw["status"] != "VERIFIED":
            raise MaterializationError(
                f"re-derivation receipt state differs: {correction_id}"
            )
        for field in ("input_generation_id", "output_generation_id", "receipt_sha256"):
            if len(_text(raw[field], f"{correction_id}.{field}")) != 64:
                raise MaterializationError(
                    f"re-derivation receipt identity differs: {correction_id}"
                )
        outputs = raw["invalidated_outputs"]
        if not isinstance(outputs, list) or not outputs:
            raise MaterializationError(
                f"re-derivation receipt scope differs: {correction_id}"
            )
        for output in outputs:
            _semantic_output_route(output)
        rederived[correction_id] = {str(value) for value in outputs}
    missing_rederivations = sorted(
        correction_id
        for correction_id, outputs in resolved_applicable.items()
        if rederived.get(correction_id) != outputs
    )

    source_routes = []
    for row in source_rows:
        outputs = sorted(str(value) for value in row["invalidated_outputs"])
        rebuild_paths = [_semantic_output_route(output) for output in outputs]
        affected_stages = [
            stage
            for stage in STAGES
            if any(stage in route["affected_stages"] for route in rebuild_paths)
        ]
        source_routes.append(
            {
                "affected_stages": affected_stages,
                "invalidated_outputs": outputs,
                "rebuild_paths": rebuild_paths,
                "source_id": row["source_id"],
            }
        )
    runtime_correction_routes = []
    for row in runtime_corrections:
        assert isinstance(row, dict)
        rebuild_paths = [
            _semantic_output_route(output) for output in row["invalidated_outputs"]
        ]
        runtime_correction_routes.append(
            {
                "affected_stages": [
                    stage
                    for stage in STAGES
                    if any(stage in route["affected_stages"] for route in rebuild_paths)
                ],
                "correction_id": row["correction_id"],
                "invalidated_outputs": sorted(row["invalidated_outputs"]),
                "rebuild_paths": rebuild_paths,
            }
        )

    stage_identities: list[dict[str, Any]] = []
    upstream_identity_by_stage: dict[str, str] = {}
    for stage in STAGES:
        semantic_sources = [
            row
            for row in source_rows
            if stage
            in next(
                route["affected_stages"]
                for route in source_routes
                if route["source_id"] == row["source_id"]
            )
        ]
        semantic_corrections = [
            row
            for row in runtime_corrections
            if stage
            in next(
                route["affected_stages"]
                for route in runtime_correction_routes
                if route["correction_id"] == row["correction_id"]
            )
        ]
        stage_payload = {
            "corrections": semantic_corrections,
            "outcomes": [
                row
                for row in outcome_rows
                if stage in _semantic_output_route(row["outcome_id"])["affected_stages"]
            ],
            "provider_admission_receipt_sha256": admission["receipt_sha256"],
            "sources": semantic_sources,
            "stage": stage,
            "upstream_stage_identities": {
                dependency: upstream_identity_by_stage[dependency]
                for dependency in STAGE_DEPENDENCIES[stage]
            },
        }
        identity = hashlib.sha256(canonical_json_bytes(stage_payload)).hexdigest()
        upstream_identity_by_stage[stage] = identity
        stage_identities.append(
            {
                "affected_by_corrections": sorted(
                    row["correction_id"] for row in semantic_corrections
                ),
                "semantic_input_sha256": identity,
                "source_ids": sorted(row["source_id"] for row in semantic_sources),
                "stage": stage,
                "upstream_stages": list(STAGE_DEPENDENCIES[stage]),
            }
        )
    blockers = sorted(
        {
            *(
                f"SOURCE:{row['source_id']}"
                for row in source_rows
                if row["product_requirement"] == "REQUIRED"
                and row["readiness"] != "READY"
            ),
            *(
                f"OUTCOME:{row['outcome_id']}"
                for row in outcome_rows
                if row["product_requirement"] == "REQUIRED"
                and row["readiness"] != "READY"
            ),
            *(
                ("PROVIDER_ADMISSION",)
                if admission["product_readiness"] != "READY_FOR_COMPOSITION"
                else ()
            ),
            *(
                ("CORRECTION_AUTHORITY_REQUIRED",)
                if correction_registry["authority_state"] != "PROVIDED"
                else ()
            ),
            *(f"OPEN_CORRECTION:{value}" for value in applicable_open),
            *(
                ("REDERIVE_AUTHORITY_REQUIRED",)
                if resolved_applicable
                and rederive_registry["authority_state"] != "PROVIDED"
                else ()
            ),
            *(f"MISSING_REDERIVATION:{value}" for value in missing_rederivations),
        }
    )
    correction_boundary = {
        key: _canonical_clone(value)
        for key, value in correction_registry.items()
        if key != "file"
    }
    correction_boundary["applicable_open_correction_ids"] = sorted(applicable_open)
    rederive_boundary = {
        key: _canonical_clone(value)
        for key, value in rederive_registry.items()
        if key != "file"
    }
    rederive_boundary["missing_correction_ids"] = missing_rederivations
    return {
        "blocking_reasons": blockers,
        "census_memberships": sorted(
            census_memberships, key=lambda row: row["sleeve_alias"]
        ),
        "census_sha256": census_sha256,
        "contract_raw_sha256": contract_raw_sha256,
        "contract_semantic_sha256": contract_semantic_sha256,
        "correction_authority": correction_boundary,
        "correction_invalidation_routes": sorted(
            runtime_correction_routes, key=lambda row: row["correction_id"]
        ),
        "history_corrections": correction_rows,
        "invested_capital_policy_sha256": invested_capital_policy_sha256,
        "outcomes": sorted(outcome_rows, key=lambda row: row["outcome_id"]),
        "product": _canonical_clone(product),
        "product_id": product["product_id"],
        "product_version": product["product_version"],
        "provider_admission": dict(admission),
        "readiness": "READY_FOR_EXECUTION" if not blockers else "REQUIRED_NOT_READY",
        "rederive_authority": rederive_boundary,
        "schema_version": 2,
        "source_invalidation_routes": sorted(
            source_routes, key=lambda row: row["source_id"]
        ),
        "source_matrix_sha256": source_matrix_sha256,
        "sources": source_rows,
        "stage_identities": stage_identities,
        "stage_order": list(STAGES),
        "strategies": strategy_rows,
    }


def _derive_product_authority(
    vertical_path: Path,
    contract_path: Path,
    source_matrix_path: Path,
    correction_registry_path: Path | None = None,
    rederive_registry_path: Path | None = None,
) -> dict[str, Any]:
    vertical = _load(vertical_path, "vertical recipe")
    if vertical.get("schema_version") != 7:
        raise MaterializationError(
            "financial MVP composition requires vertical recipe schema 7"
        )
    contract_path = contract_path.resolve()
    source_matrix_path = source_matrix_path.resolve()
    module = _financial_mvp_module()
    try:
        contract = module.load_contract(
            contract_path, CURRENT_STRATEGY_REGISTRY, FINANCIAL_MVP_SCHEMA
        )
        contract_semantic_sha256 = module.contract_digest(contract)
        census_sha256 = module.census_digest(contract["current_census"])
        invested_sha256 = module.invested_capital_policy_digest(contract)
        expected_matrix = module.render_views(contract).source_matrix_json
    except (OSError, RuntimeError, ValueError) as exc:
        raise MaterializationError(
            "financial MVP product authority is invalid"
        ) from exc
    contract_raw_sha256, _ = _sha(contract_path)
    source_matrix_bytes = source_matrix_path.read_bytes()
    if source_matrix_bytes != expected_matrix:
        raise MaterializationError("financial MVP source matrix differs from contract")
    source_matrix_sha256 = hashlib.sha256(source_matrix_bytes).hexdigest()
    provider_definition = vertical.get("provider_ledger_generation")
    if not isinstance(provider_definition, dict):
        raise MaterializationError("financial MVP provider generation is absent")
    provider_generation = _absolute(
        provider_definition.get("path"), "provider ledger generation"
    )
    admission = _provider_admission_authority(
        provider_generation,
        contract,
        contract_raw_sha256=contract_raw_sha256,
        contract_semantic_sha256=contract_semantic_sha256,
        census_sha256=census_sha256,
    )
    correction_registry = _authority_registry(
        correction_registry_path,
        schema_name="portfolio-correction-registry",
        record_key="corrections",
    )
    rederive_registry = _authority_registry(
        rederive_registry_path,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
    )
    for label, boundary in (
        ("correction registry", correction_registry),
        ("re-derivation registry", rederive_registry),
    ):
        if boundary["authority_state"] == "PROVIDED" and (
            boundary["contract_semantic_sha256"] != contract_semantic_sha256
            or boundary["census_sha256"] != census_sha256
        ):
            raise MaterializationError(f"{label} product authority differs")
    conformance = _product_conformance(
        contract,
        admission,
        correction_registry,
        rederive_registry,
        contract_raw_sha256=contract_raw_sha256,
        contract_semantic_sha256=contract_semantic_sha256,
        census_sha256=census_sha256,
        source_matrix_sha256=source_matrix_sha256,
        invested_capital_policy_sha256=invested_sha256,
    )
    return {
        "contract": _file_definition(contract_path),
        "conformance": conformance,
        "correction_registry": correction_registry,
        "provider_generation": {
            "catalog": _file_definition(provider_generation / "provider-ledger.json"),
            "generation_id": provider_generation.name,
            "manifest": _file_definition(provider_generation / "generation.json"),
            "receipt": _file_definition(
                provider_generation / "admit-or-declare-receipt.json"
            ),
        },
        "rederive_registry": rederive_registry,
        "source_matrix": _file_definition(source_matrix_path),
    }


def _product_gate_payload(authority: Mapping[str, Any]) -> dict[str, Any]:
    provider = authority.get("provider_generation")
    if not isinstance(provider, dict):
        raise MaterializationError("financial MVP provider authority differs")
    return {
        "authority_hashes": {
            "contract_raw_sha256": authority["contract"]["sha256"],
            "provider_catalog_sha256": provider["catalog"]["sha256"],
            "provider_manifest_sha256": provider["manifest"]["sha256"],
            "provider_receipt_sha256": provider["receipt"]["sha256"],
            "source_matrix_sha256": authority["source_matrix"]["sha256"],
        },
        "conformance": _canonical_clone(authority["conformance"]),
        "schema_version": 2,
    }


def _product_gate_rows(
    authority: Mapping[str, Any], product_path: Path
) -> list[dict[str, Any]]:
    payload = _product_gate_payload(authority)
    product_bytes = canonical_json_bytes(payload)
    _write_exact(product_path, product_bytes)
    provider = authority["provider_generation"]
    assert isinstance(provider, dict)
    definitions = {
        "contract": authority["contract"],
        "provider_catalog": provider["catalog"],
        "provider_manifest": provider["manifest"],
        "provider_receipt": provider["receipt"],
        "source_matrix": authority["source_matrix"],
    }
    rows = [
        {
            "gate_id": "financial-mvp-product-conformance",
            "path": str(product_path),
            "sha256": hashlib.sha256(product_bytes).hexdigest(),
        }
    ]
    for key, gate_id in PRODUCT_AUTHORITY_GATE_IDS.items():
        definition = definitions[key]
        rows.append(
            {
                "gate_id": gate_id,
                "path": str(definition["path"]),
                "sha256": definition["sha256"],
            }
        )
    for key in ("correction_registry", "rederive_registry"):
        boundary = authority[key]
        assert isinstance(boundary, dict)
        definition = boundary.get("file")
        if definition is not None:
            rows.append(
                {
                    "gate_id": (
                        "portfolio-correction-registry"
                        if key == "correction_registry"
                        else "portfolio-rederive-receipt-registry"
                    ),
                    "path": str(definition["path"]),
                    "sha256": definition["sha256"],
                }
            )
    return rows


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
    schema_version = recipe.get("schema_version")
    if schema_version == 3:
        required.add("product_authority")
    if schema_version not in {2, 3} or set(recipe) != required:
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
        vertical.get("schema_version") not in {6, 7}
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
    if schema_version == 3:
        authority = recipe["product_authority"]
        if not isinstance(authority, dict) or set(authority) != {
            "contract",
            "conformance",
            "correction_registry",
            "provider_generation",
            "rederive_registry",
            "source_matrix",
        }:
            raise MaterializationError("financial MVP product authority differs")
        contract_path = _validate_file_definition(
            authority["contract"], "financial MVP contract"
        )
        source_matrix_path = _validate_file_definition(
            authority["source_matrix"], "financial MVP source matrix"
        )
        registry_paths: dict[str, Path | None] = {}
        for key in ("correction_registry", "rederive_registry"):
            boundary = authority[key]
            if not isinstance(boundary, dict):
                raise MaterializationError(f"financial MVP {key} differs")
            file_definition = boundary.get("file")
            registry_paths[key] = (
                None
                if file_definition is None
                else _validate_file_definition(file_definition, key)
            )
        provider_generation = authority["provider_generation"]
        if not isinstance(provider_generation, dict) or set(provider_generation) != {
            "catalog",
            "generation_id",
            "manifest",
            "receipt",
        }:
            raise MaterializationError("financial MVP provider generation differs")
        for key in ("catalog", "manifest", "receipt"):
            _validate_file_definition(provider_generation[key], f"provider {key}")
        expected_authority = _derive_product_authority(
            vertical_path,
            contract_path,
            source_matrix_path,
            registry_paths["correction_registry"],
            registry_paths["rederive_registry"],
        )
        if canonical_json_bytes(authority) != canonical_json_bytes(expected_authority):
            raise MaterializationError("financial MVP product authority is stale")
    return recipe


def _vertical_source_paths(vertical_path: Path) -> list[Path]:
    vertical = _load(vertical_path, "vertical recipe")
    schema_version = vertical.get("schema_version")
    if schema_version not in {6, 7}:
        raise MaterializationError("vertical recipe schema differs")
    paths = [vertical_path]
    for key in (
        "baseline_spec",
        "base_factor_file",
        "implementation_reconciliation",
        "implementation_return_attribution",
        "legacy_panel",
    ):
        value = vertical.get(key)
        if not isinstance(value, dict):
            raise MaterializationError(f"vertical recipe {key} differs")
        paths.append(_absolute(value.get("path"), f"vertical recipe {key}.path"))
    provider_key = (
        "provider_ledger_spec" if schema_version == 6 else "provider_ledger_generation"
    )
    provider = vertical.get(provider_key)
    if not isinstance(provider, dict):
        raise MaterializationError(f"vertical recipe {provider_key} differs")
    paths.append(
        _absolute(provider.get("path"), f"vertical recipe {provider_key}.path")
    )
    for key in (
        "strategy_attribution_root",
        "decomposition_plan_bundle",
        "correlation_source_root",
    ):
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
    financial_mvp_contract: Path | None = None,
    financial_mvp_source_matrix: Path | None = None,
    correction_registry: Path | None = None,
    rederive_receipt_registry: Path | None = None,
) -> dict[str, Any]:
    vertical = _load(vertical_recipe.resolve(), "vertical recipe")
    if vertical.get("schema_version") not in {6, 7}:
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
    payload: dict[str, Any] = {
        "as_of_date": vertical["as_of_date"],
        "expected_generations": expected,
        "panel_launcher_sha256": _sha(launcher)[0],
        "pipeline_id": pipeline_id,
        "qa_dir": _directory_definition(qa_dir.resolve()),
        "release_commit": vertical["release_commit"],
        "schema_version": 3,
        "vertical_recipe": _file_definition(vertical_recipe.resolve()),
    }
    if financial_mvp_contract is None or financial_mvp_source_matrix is None:
        raise MaterializationError(
            "current pipeline recipe requires financial MVP contract and source matrix"
        )
    payload["product_authority"] = _derive_product_authority(
        vertical_recipe.resolve(),
        financial_mvp_contract.resolve(),
        financial_mvp_source_matrix.resolve(),
        None if correction_registry is None else correction_registry.resolve(),
        (
            None
            if rederive_receipt_registry is None
            else rederive_receipt_registry.resolve()
        ),
    )
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
    product_authority = recipe.get("product_authority")
    if isinstance(product_authority, dict):
        for key in ("contract", "source_matrix"):
            definition = product_authority[key]
            assert isinstance(definition, dict)
            path = _absolute(definition["path"], f"product authority {key}")
            artifacts.append(
                {"path": str(path), "type": "FILE", **_file_definition(path)}
            )
        provider = product_authority["provider_generation"]
        for key in ("manifest", "catalog", "receipt"):
            definition = provider[key]
            path = _absolute(definition["path"], f"product provider {key}")
            artifacts.append(
                {"path": str(path), "type": "FILE", **_file_definition(path)}
            )
        for key in ("correction_registry", "rederive_registry"):
            definition = product_authority[key].get("file")
            if definition is not None:
                path = _absolute(definition["path"], f"product authority {key}")
                artifacts.append(
                    {"path": str(path), "type": "FILE", **_file_definition(path)}
                )
    return {
        "as_of_date": recipe["as_of_date"],
        "artifacts": sorted(artifacts, key=lambda row: str(row["path"])),
        "release_commit": recipe["release_commit"],
        "schema_version": recipe["schema_version"],
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
        "product_conformance_sha256": (
            hashlib.sha256(
                canonical_json_bytes(recipe["product_authority"]["conformance"])
            ).hexdigest()
            if recipe.get("schema_version") == 3
            else None
        ),
        "schema_version": recipe["schema_version"],
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


def _generation_as_of_allowed(
    role: str, generation_as_of: date, release_as_of: date
) -> bool:
    """Allow a dated source ledger while keeping derived release roles exact-date."""

    return (
        generation_as_of <= release_as_of
        if role == "ledger"
        else generation_as_of == release_as_of
    )


def _freshness_key(row: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    provider = row.get("provider_id")
    subject = row.get("subject_id")
    lane = row.get("lane")
    fact_type = row.get("fact_type")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (provider, subject, lane, fact_type)
    ):
        return None
    assert isinstance(provider, str)
    assert isinstance(subject, str)
    assert isinstance(lane, str)
    assert isinstance(fact_type, str)
    return provider.strip(), subject.strip(), lane.strip(), fact_type.strip()


def _semantic_gate_payload(
    recipe: Mapping[str, Any],
    release_generations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    checks = {
        "analytics_report_panel_equality": "PASS",
        "financial_identities": "PASS",
        "freshness": "PASS",
        "generation_invariants": "PASS",
        "ledger_bridges": "PASS",
        "mappings": "PASS",
    }
    if recipe.get("schema_version") == 3:
        checks["product_conformance"] = "PASS"
    return {
        "as_of_date": recipe["as_of_date"],
        "checks": checks,
        "release_commit": recipe["release_commit"],
        "release_generations": (
            dict(release_generations)
            if release_generations is not None
            else {
                ROOT_IDS[role]: recipe["expected_generations"][role]["generation_id"]
                for role in RELEASE_ROLES
            }
        ),
        "schema_version": 2 if recipe.get("schema_version") == 3 else 1,
        "unwaived_blocking_exceptions": 0,
        "verdict": "PASS",
    }


def semantic(recipe_path: Path, output_path: Path) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    output_root = output_path.parent.parent
    checks: dict[str, dict[str, Any]] = {}
    release_as_of = date.fromisoformat(_text(recipe["as_of_date"], "as_of_date"))
    if recipe.get("schema_version") == 3:
        conformance = recipe["product_authority"]["conformance"]
        _result(
            checks,
            "product_conformance",
            True,
            readiness=conformance["readiness"],
            sha256=hashlib.sha256(canonical_json_bytes(conformance)).hexdigest(),
        )

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
            generation_as_of = manifest.as_of_date
            passed = (
                actual == expected
                and manifest.verification is VerificationStatus.VERIFIED
                and manifest.coverage.status
                is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
                and _generation_as_of_allowed(role, generation_as_of, release_as_of)
            )
            generation_rows[role] = {
                "actual_generation_id": actual["generation_id"],
                "as_of_rule": (
                    "ON_OR_BEFORE_RELEASE" if role == "ledger" else "EXACT_RELEASE_DATE"
                ),
                "expected_generation_id": expected["generation_id"],
                "file_count": len(inventory_files),
                "generation_as_of_date": generation_as_of.isoformat(),
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
    assessment_rows = (
        [row for row in assessments if isinstance(row, dict)]
        if isinstance(assessments, list)
        else []
    )
    assessment_shape_ok = isinstance(assessments, list) and len(assessment_rows) == len(
        assessments
    )
    evaluation_ok = assessment_shape_ok and all(
        row.get("evaluated_on") == recipe["as_of_date"] for row in assessment_rows
    )
    blocking = [
        row for row in assessment_rows if row.get("blocks_current_consumers") is True
    ]
    blocking_disclosures = {
        _freshness_key(row): (row.get("state"), row.get("reason")) for row in blocking
    }
    blockers_disclosed = all(
        _freshness_key(row) is not None
        and row.get("required_for_current") is True
        and row.get("escalation_required") is True
        and row.get("state") in {"STALE", "UNAVAILABLE"}
        and isinstance(row.get("reason"), str)
        and bool(str(row["reason"]).strip())
        for row in blocking
    )
    panel_coverage = panel.get("coverage")
    rendered_blocking = (
        [
            row
            for row in panel_coverage
            if isinstance(row, dict)
            and row.get("category") == "DATA_FRESHNESS"
            and row.get("blocks_current_consumers") is True
        ]
        if isinstance(panel_coverage, list)
        else []
    )
    rendered_disclosures = {
        _freshness_key(row): (row.get("status"), row.get("reason"))
        for row in rendered_blocking
    }
    rendered_disclosures_ok = blocking_disclosures == rendered_disclosures
    freshness_ok = (
        freshness.get("as_of_date") == recipe["as_of_date"]
        and evaluation_ok
        and blockers_disclosed
        and rendered_disclosures_ok
    )
    _result(
        checks,
        "freshness",
        freshness_ok,
        as_of_date=freshness.get("as_of_date"),
        blocking_assessment_count=len(blocking),
        blockers_explicitly_disclosed=blockers_disclosed,
        blockers_rendered_in_panel=rendered_disclosures_ok,
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
        "schema_version": 2 if recipe.get("schema_version") == 3 else 1,
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
    if recipe.get("schema_version") != 3:
        raise MaterializationError(
            "schema-2 recipe is recovery-only and cannot create a current specification"
        )
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
    product_authority = recipe["product_authority"]
    for key in ("contract", "source_matrix"):
        definition = product_authority[key]
        source_paths.append(_absolute(definition["path"], f"product authority {key}"))
    provider_definition = product_authority["provider_generation"]
    for key in ("manifest", "catalog", "receipt"):
        source_paths.append(
            _absolute(provider_definition[key]["path"], f"provider authority {key}")
        )
    for key in ("correction_registry", "rederive_registry"):
        definition = product_authority[key].get("file")
        if definition is not None:
            source_paths.append(
                _absolute(definition["path"], f"product authority {key}")
            )

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
            "product_conformance_sha256": (
                hashlib.sha256(
                    canonical_json_bytes(recipe["product_authority"]["conformance"])
                ).hexdigest()
                if recipe.get("schema_version") == 3
                else None
            ),
            "release_commit": recipe["release_commit"],
            "schema_version": recipe["schema_version"],
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

    report_data_file = next(
        row
        for row in recipe["expected_generations"]["report"]["files"]
        if row["relative_path"] == "portfolio-report-data.json"
    )
    report_candidate = (
        output_root
        / "stages"
        / "report-candidate"
        / "candidates"
        / report_data_file["sha256"]
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
            report_candidate / "portfolio-report-data.json",
            report_candidate / "portfolio-report.xlsx",
            report_candidate / "workbook-contract.json",
            report_candidate / "xlsx-job.json",
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
        for dependency in STAGE_DEPENDENCIES[role]:
            reads.extend(stage_writes[dependency])
        if role == "panel":
            reads.append(qa_dir)
        reads = list(dict.fromkeys(reads))
        command = _command(
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
        nodes.append(command)
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
        "schema_version": 2 if recipe.get("schema_version") == 3 else 1,
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
    contract["product_authority"] = _product_gate_payload(product_authority)
    contract_path = spec_path.with_name("portfolio-orchestration-contract.json")
    contract_bytes = canonical_json_bytes(contract)
    _write_exact(contract_path, contract_bytes)
    product_path = spec_path.with_name("financial-mvp-product-conformance.json")
    product_gates = _product_gate_rows(product_authority, product_path)
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
            *product_gates,
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


def build_production_spec(
    recipe_path: Path,
    prepared_root: Path,
    semantic_path: Path,
    portfolio_root: Path,
    state_root: Path,
    spec_path: Path,
) -> dict[str, Any]:
    """Bind a verified private proof to the stable production root set."""

    recipe = _recipe(recipe_path)
    if recipe.get("schema_version") != 3:
        raise MaterializationError(
            "schema-2 recipe is recovery-only and cannot create a production specification"
        )
    repo = Path(__file__).resolve().parents[1]
    release_commit = _git(repo, "rev-parse", "HEAD")
    if release_commit != recipe["release_commit"] or _git(
        repo, "status", "--porcelain=v1", "--untracked-files=all"
    ):
        raise MaterializationError("repository must be clean at the recipe commit")
    prepared_root = prepared_root.resolve()
    semantic_path = semantic_path.resolve()
    portfolio_root = portfolio_root.resolve()
    state_root = state_root.resolve()
    if portfolio_root == repo or repo in portfolio_root.parents:
        raise MaterializationError("production portfolio root must be outside Git")

    sources: dict[str, Path] = {}
    generation_ids: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    for root_id, relative in PRODUCTION_STAGE_ROOTS.items():
        staged_root = prepared_root / relative
        if root_id in {
            "provider-ledger",
            "portfolio-baseline",
            "portfolio-report",
            "portfolio-panel",
        }:
            role = {
                "provider-ledger": "ledger",
                "portfolio-baseline": "baseline",
                "portfolio-report": "report",
                "portfolio-panel": "panel",
            }[root_id]
            generation_id = _text(
                recipe["expected_generations"][role]["generation_id"],
                f"{root_id} generation_id",
            )
        else:
            generation_id, _ = _current_generation(staged_root)
        source = staged_root / "generations" / generation_id
        inventory = inventory_generation(source)
        files = inventory.get("files")
        if not isinstance(files, list):
            raise MaterializationError(f"{root_id} generation inventory differs")
        expected = (
            recipe["expected_generations"][role]
            if root_id
            in {
                "provider-ledger",
                "portfolio-baseline",
                "portfolio-report",
                "portfolio-panel",
            }
            else None
        )
        actual = {
            "files": files,
            "generation_id": inventory["generation_id"],
            "manifest_sha256": inventory["manifest_sha256"],
        }
        if expected is not None and actual != expected:
            raise MaterializationError(f"{root_id} prepared generation differs")
        sources[root_id] = source
        generation_ids[root_id] = generation_id
        nodes.append(
            {
                "depends_on": [],
                "files": files,
                "generation_id": generation_id,
                "kind": "GENERATION",
                "manifest_sha256": inventory["manifest_sha256"],
                "node_id": f"publish-{root_id}",
                "release_target": True,
                "root_id": root_id,
                "source_dir": str(source),
            }
        )

    panel_generation = sources["portfolio-panel"]
    panel_payload = _load(
        panel_generation / "data" / "dashboard_data.json", "prepared panel data"
    )
    panel_meta = panel_payload.get("meta")
    expected_panel_analytics = {
        "correlation_generation_id": generation_ids["correlation"],
        "decomposition_generation_id": generation_ids["factor-decomposition"],
        "weekly_returns_generation_id": generation_ids["weekly-returns"],
    }
    if not isinstance(panel_meta, dict) or any(
        panel_meta.get(key) != value for key, value in expected_panel_analytics.items()
    ):
        raise MaterializationError("prepared panel analytics authority differs")
    if _panel_hybrid_generation_ids(panel_payload) != {
        root_id: generation_ids[root_id] for root_id in HYBRID_PANEL_ROOT_IDS.values()
    }:
        raise MaterializationError("prepared panel hybrid analytics authority differs")

    expected_semantic = canonical_json_bytes(_semantic_gate_payload(recipe))
    if semantic_path.read_bytes() != expected_semantic:
        raise MaterializationError("prepared semantic gate differs")
    production_semantic = canonical_json_bytes(
        _semantic_gate_payload(recipe, generation_ids)
    )
    production_semantic_path = spec_path.with_name(
        "production-post-build-semantic.json"
    )
    _write_exact(production_semantic_path, production_semantic)
    orchestration_path = prepared_root / "portfolio-orchestration-contract.json"
    orchestration_sha = _sha(orchestration_path)[0]
    orchestration = _load(orchestration_path, "prepared orchestration contract")
    prepared_release_commit = _text(
        orchestration.get("release_commit"), "prepared orchestration release_commit"
    )
    if len(prepared_release_commit) != 40:
        raise MaterializationError("prepared orchestration release_commit differs")
    launcher = prepared_root / "stages" / "panel" / "index.html"
    launcher_sha = _sha(launcher)[0]
    if launcher_sha != recipe["panel_launcher_sha256"]:
        raise MaterializationError("prepared launcher differs")

    contract = {
        "code_commit": release_commit,
        "generation_ids": generation_ids,
        "orchestration_contract_sha256": orchestration_sha,
        "pipeline_id": recipe["pipeline_id"],
        "prepared_release_commit": prepared_release_commit,
        "production_roots": {
            root_id: str(portfolio_root / relative)
            for root_id, relative in PRODUCTION_ROOTS.items()
        },
        "schema_version": 2 if recipe.get("schema_version") == 3 else 1,
        "semantic_gate_sha256": hashlib.sha256(expected_semantic).hexdigest(),
        "production_semantic_gate_sha256": hashlib.sha256(
            production_semantic
        ).hexdigest(),
        "source_generations": {key: str(value) for key, value in sources.items()},
    }
    production_product_authority = _canonical_clone(recipe["product_authority"])
    expected_admission = recipe["product_authority"]["conformance"][
        "provider_admission"
    ]
    prepared_provider = sources["provider-ledger"]
    if (
        prepared_provider.name != expected_admission["generation_id"]
        or _sha(prepared_provider / "generation.json")[0]
        != expected_admission["manifest_sha256"]
        or _sha(prepared_provider / "provider-ledger.json")[0]
        != expected_admission["catalog_sha256"]
        or _sha(prepared_provider / "admit-or-declare-receipt.json")[0]
        != expected_admission["receipt_sha256"]
    ):
        raise MaterializationError("prepared provider admission authority differs")
    production_product_authority["provider_generation"] = {
        "catalog": _file_definition(prepared_provider / "provider-ledger.json"),
        "generation_id": prepared_provider.name,
        "manifest": _file_definition(prepared_provider / "generation.json"),
        "receipt": _file_definition(
            prepared_provider / "admit-or-declare-receipt.json"
        ),
    }
    contract["product_authority_sha256"] = hashlib.sha256(
        canonical_json_bytes(_product_gate_payload(production_product_authority))
    ).hexdigest()
    contract_path = spec_path.with_name("portfolio-production-rebind-contract.json")
    contract_bytes = canonical_json_bytes(contract)
    _write_exact(contract_path, contract_bytes)
    product_path = spec_path.with_name("financial-mvp-product-conformance.json")
    product_gates = _product_gate_rows(production_product_authority, product_path)
    spec = {
        "as_of_date": recipe["as_of_date"],
        "code_commit": release_commit,
        "companions": [
            {
                "companion_id": "portfolio-panel-launcher",
                "relative_path": "index.html",
                "root_id": "portfolio-panel",
                "sha256": launcher_sha,
                "source_path": str(launcher),
            }
        ],
        "coordination_root": str(portfolio_root / "releases"),
        "created_at": datetime.now(UTC).isoformat(),
        "gates": [
            {
                "gate_id": "port-underwrite-production-rebind",
                "path": str(contract_path),
                "sha256": hashlib.sha256(contract_bytes).hexdigest(),
            },
            {
                "gate_id": "post-build-semantic",
                "path": str(production_semantic_path),
                "sha256": hashlib.sha256(production_semantic).hexdigest(),
            },
            *product_gates,
        ],
        "nodes": nodes,
        "pipeline_id": f"{recipe['pipeline_id']}-production",
        "roots": {
            root_id: str(portfolio_root / relative)
            for root_id, relative in PRODUCTION_ROOTS.items()
        },
        "schema_version": 1,
        "state_root": str(state_root),
    }
    _write_exact(spec_path, canonical_json_bytes(spec))
    return {
        "contract": str(contract_path),
        "nodes": len(nodes),
        "root_count": len(PRODUCTION_ROOTS),
        "spec": str(spec_path),
    }


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
    recipe_cmd.add_argument("--financial-mvp-contract", type=Path, required=True)
    recipe_cmd.add_argument("--financial-mvp-source-matrix", type=Path, required=True)
    recipe_cmd.add_argument("--correction-registry", type=Path)
    recipe_cmd.add_argument("--rederive-receipt-registry", type=Path)
    recipe_cmd.add_argument("--output", type=Path, required=True)
    spec_cmd = commands.add_parser("spec")
    spec_cmd.add_argument("--recipe", type=Path, required=True)
    spec_cmd.add_argument("--output-root", type=Path, required=True)
    spec_cmd.add_argument("--spec", type=Path, required=True)
    production_cmd = commands.add_parser("production-spec")
    production_cmd.add_argument("--recipe", type=Path, required=True)
    production_cmd.add_argument("--prepared-root", type=Path, required=True)
    production_cmd.add_argument("--semantic-gate", type=Path, required=True)
    production_cmd.add_argument("--portfolio-root", type=Path, required=True)
    production_cmd.add_argument("--state-root", type=Path, required=True)
    production_cmd.add_argument("--spec", type=Path, required=True)
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
                args.financial_mvp_contract,
                args.financial_mvp_source_matrix,
                args.correction_registry,
                args.rederive_receipt_registry,
            )
        elif args.command == "spec":
            result = build_spec(args.recipe, args.output_root, args.spec)
        else:
            result = build_production_spec(
                args.recipe,
                args.prepared_root,
                args.semantic_gate,
                args.portfolio_root,
                args.state_root,
                args.spec,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"portfolio-pipeline: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
