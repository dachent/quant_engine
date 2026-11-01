"""Build one governed source-to-panel pipeline from immutable local inputs."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Final, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

from quant_engine.core import (
    CoverageStatus,
    GenerationManifest,
    VerificationStatus,
    inventory_generation,
    PointerIntegrityError,
    read_current,
    read_generation_bundle,
    runtime_environment_sha256,
)
from quant_engine.core import CacheBaseError, run_storage, verify_if_promoted
from quant_engine.core import (
    CODE_IDENTITY_COMMIT_SCHEME as COMMIT_SCHEME,
    CODE_IDENTITY_TREE_SCHEME as TREE_SCHEME,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core import inventory_identity
from quant_engine.portfolio_ledger import (
    HwmCoverageError,
    validate_admit_or_declare_receipt,
)
from quant_engine.portfolio_ledger.hwm_governed_execution import (
    verify_governed_hwm_result,
)
from lifecycle_fields import product_strategy_rows
from portfolio_invested_capital import (
    CapitalValidationError,
    governed_hwm_input_policy,
)


class MaterializationError(ValueError):
    """A private recipe, rebuilt stage, or semantic bridge differs."""


STAGES = (
    "ledger",
    "baseline",
    "attribution",
    "analytics",
    "report",
    "panel",
)
ATTENDED_STAGES: Final = (
    "ledger",
    "baseline",
    "attribution",
    "analytics",
    "report",
    "panel-candidate",
    "panel",
)
STAGE_DEPENDENCIES = {
    "ledger": (),
    "baseline": ("ledger",),
    "attribution": ("baseline", "ledger"),
    "analytics": ("attribution",),
    "report": ("baseline", "ledger", "analytics"),
    "panel": ("ledger", "attribution", "analytics", "report"),
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
EPOCH_MANIFEST_INPUTS = {
    "account_strategy_ledger": "--manifest-ledger",
    "account_strategy_ledger_identity": "--manifest-ledger-identity",
    "account_strategy_classified_flows": "--manifest-classified-flows",
    "account_strategy_flow_coverage": "--manifest-flow-coverage",
    "account_strategy_admission_policy": "--manifest-admission-policy",
}
ATTENDED_HWM_INPUTS = {
    "--hwm-governed-result": "hwm_governed_result",
    "--hwm-governed-input-generation": "hwm_governed_input_generation",
    "--hwm-governed-calendar-authority": "hwm_governed_calendar_authority",
    "--hwm-governed-admission-receipt": "hwm_governed_admission_receipt",
}
ATTENDED_CAPITAL_INPUTS = {
    "--invested-capital-result": "invested_capital_result",
    "--invested-capital-input-generation": "invested_capital_input_generation",
}
SCHEMA_9_VERTICAL_RECIPE_KEYS = frozenset(
    {
        "as_of_date",
        "base_factor_file",
        "baseline_spec",
        "correlation_source_root",
        "decomposition_plan_bundle",
        "implementation_reconciliation",
        "implementation_return_attribution",
        "legacy_panel",
        "pipeline_id",
        "provider_ledger_generation",
        "release_commit",
        "schema_version",
        "strategy_attribution_root",
        *EPOCH_MANIFEST_INPUTS,
    }
)
RELEASE_ROLES = ("ledger", "baseline", "report", "panel")
# Roles whose release-builder path validates the private stage target against
# production and therefore requires --portfolio-root on a financial run.
_PORTFOLIO_ROOT_STAGES: Final = ("report", "panel")
# The roles whose staged output depends on the governed numerical results. A
# run that binds them into one of these and not the others produces two
# different semantic validations of the same release.
_GOVERNED_OUTCOME_STAGES: Final = ("report", "panel")
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
# Governed data roots that sit BESIDE ``dataPortfolio`` and that a release depends
# on (REL-PROVIDER-ROOTS-001): the seven providers the provider ledger binds and the
# epoch-manifest root. They are release targets in their own right so the
# predecessor-graph restore reproduces provider inputs, not only derived outputs.
PROVIDER_ROOTS: Final = {
    "epoch-manifest": "dataEpochManifest",
    "provider-alpaca": "dataAlpaca",
    "provider-fidelity": "dataFidelity",
    "provider-ibkr": "dataIBKR",
    "provider-matrixport": "dataMatrixport",
    "provider-portfolio123": "dataPortfolio123",
    "provider-quantbase": "dataQuantbase",
    "provider-schwab": "dataSchwab",
}
LEDGER_PROVIDER_ROOTS: Final = {
    root_id.removeprefix("provider-"): root_id
    for root_id in PROVIDER_ROOTS
    if root_id.startswith("provider-")
}
# The ledger's own qualification of each bound provider generation decides whether
# that generation may be current consumer authority. A PARTIAL generation is
# immutable evidence (docs/architecture/system-boundaries.md): it is retained in
# its root but is never published as CURRENT.
PROVIDER_CURRENT_QUALIFICATIONS: Final = frozenset(
    {"FINITE_STATIC", "PUBLISHED_COMPLETE", "STAGED_COMPLETE"}
)
PROVIDER_EVIDENCE_QUALIFICATIONS: Final = frozenset({"ACCEPTED_PARTIAL"})
# The only root the data-root gate treats as evidence-only (tools/check_data_roots.py).
# A ledger that newly qualified another provider PARTIAL would otherwise drop it from
# the release without an error.
EVIDENCE_ONLY_PROVIDER_ROOTS: Final = frozenset({"provider-ibkr"})
# The only refusals a root's own pointer may carry and still be admitted by the
# `predecessor-pointer-recovery` gate. Anything else (a generation edited after
# publication, a manifest hash difference) is damage, not a missing or legacy pointer.
RECOVERABLE_POINTER_FAILURES: Final = frozenset(
    {
        "current generation manifest is missing",
        "legacy pointer schema is not accepted as current authority",
    }
)
POINTER_RECOVERY_GATE_ID: Final = "predecessor-pointer-recovery"
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
HWM_GOVERNED_POLICY = REPOSITORY_ROOT / "contracts" / "portfolio-hwm-policy-v4.json"
HWM_PERFORMANCE_POLICY = (
    REPOSITORY_ROOT / "contracts" / "portfolio-performance-policy-v1.json"
)
HWM_LEGACY_POLICY = REPOSITORY_ROOT / "contracts" / "portfolio-hwm-policy-v3.json"
HWM_BINDING_KEYS = frozenset(
    {
        "governed_result",
        "input_generation",
        "calendar_authority",
        "provider_admission_receipt",
    }
)


def _hwm_binding_paths(
    hwm_governed_result_path: Path | None,
    hwm_governed_input_generation_path: Path | None,
    hwm_governed_calendar_authority_path: Path | None,
    hwm_governed_admission_receipt_path: Path | None,
) -> dict[str, Path] | None:
    values = {
        "governed_result": hwm_governed_result_path,
        "input_generation": hwm_governed_input_generation_path,
        "calendar_authority": hwm_governed_calendar_authority_path,
        "provider_admission_receipt": hwm_governed_admission_receipt_path,
    }
    if all(value is None for value in values.values()):
        return None
    if any(value is None for value in values.values()):
        raise MaterializationError("governed HWM bindings must be supplied together")
    return {key: Path(value).resolve() for key, value in values.items() if value}


def _governed_stage_paths(
    hwm_governed_result_path: Path | None,
    hwm_governed_input_generation_path: Path | None,
    hwm_governed_calendar_authority_path: Path | None,
    hwm_governed_admission_receipt_path: Path | None,
    invested_capital_result_path: Path | None,
    invested_capital_input_generation_path: Path | None,
) -> dict[str, Path]:
    """Resolve the governed numerical results the staging roles read.

    All six move together. A run that gave the report an invested-capital
    result but not a governed HWM would publish a report the panel cannot
    verify, so a partial set is refused rather than silently narrowed.
    """
    values = {
        "--hwm-governed-result": hwm_governed_result_path,
        "--hwm-governed-input-generation": hwm_governed_input_generation_path,
        "--hwm-governed-calendar-authority": hwm_governed_calendar_authority_path,
        "--hwm-governed-admission-receipt": hwm_governed_admission_receipt_path,
        "--invested-capital-result": invested_capital_result_path,
        "--invested-capital-input-generation": (invested_capital_input_generation_path),
    }
    if all(value is None for value in values.values()):
        return {}
    if any(value is None for value in values.values()):
        raise MaterializationError(
            "governed numerical results must be supplied together"
        )
    resolved = {}
    for flag, value in values.items():
        assert value is not None
        path = Path(value).resolve()
        if path.is_symlink() or not path.is_file():
            raise MaterializationError(f"governed result is absent or linked: {flag}")
        resolved[flag] = path
    return resolved


def _admission_policy_artifact(artifact_rows: Sequence[Mapping[str, Any]]) -> Path:
    """Resolve the selected admission policy from the production artifact inventory.

    This used to search the epoch-manifest generation for an artifact whose digest equalled
    the `admission_policy` identity the generation declares, and it could never succeed. The
    generation stores a *projection* of the policy -- admissions filtered to the selected
    assignment keys with `assignment_sha256` recomputed, then canonicalised -- so the
    selected input's own bytes are not among its artifacts. Nor could they be: every
    generation artifact must be canonical JSON, checked by the independent verifier and
    again by `quant_engine.core.publication`, while the captured policy is pretty-printed.

    So the input travels the way every other out-of-root producer input travels -- as its
    own node in the production specification, beside the generation it belongs to.
    `_identity_snapshot` downstream still requires these bytes to equal the identity the
    generation declares, so naming the wrong file is refused there.
    """

    rows = [
        row
        for row in artifact_rows
        if row.get("kind") == "ARTIFACT"
        and row.get("node_id") == "financial-account-manifest-admission-policy"
    ]
    if len(rows) != 1:
        raise MaterializationError(
            "selected HWM admission policy binding is absent or ambiguous"
        )
    path = Path(_text(rows[0].get("path"), "HWM admission policy path"))
    definition = _file_definition(path)
    if (
        definition["sha256"] != rows[0].get("sha256")
        or definition["size_bytes"] != rows[0].get("size_bytes")
        or Path(cast(str, definition["path"])).resolve() != path.resolve()
    ):
        raise MaterializationError("selected HWM admission policy binding differs")
    return path


def _production_sleeve_facts_paths(
    artifact_rows: Sequence[Mapping[str, Any]],
) -> tuple[Path, ...]:
    """Resolve the selected sleeve-facts files from the production artifact inventory.

    The account-manifest generation names the facts it selected by identity only.
    Each travels as its own node, ``financial-account-manifest-sleeve-facts-<sha256
    [:16]>``, exactly as the admission policy does.  The node id must name the
    digest of the file it binds, and the bytes are re-read here.
    """

    prefix = "financial-account-manifest-sleeve-facts-"
    paths: list[Path] = []
    for row in sorted(
        (
            item
            for item in artifact_rows
            if item.get("kind") == "ARTIFACT"
            and str(item.get("node_id", "")).startswith(prefix)
        ),
        key=lambda item: str(item["node_id"]),
    ):
        path = Path(_text(row.get("path"), "HWM sleeve facts path"))
        definition = _file_definition(path)
        if (
            definition["sha256"] != row.get("sha256")
            or definition["size_bytes"] != row.get("size_bytes")
            or Path(cast(str, definition["path"])).resolve() != path.resolve()
            or row.get("node_id") != f"{prefix}{str(definition['sha256'])[:16]}"
        ):
            raise MaterializationError("selected HWM sleeve facts binding differs")
        paths.append(path)
    return tuple(paths)


def _production_hwm_source_paths(
    sources: Mapping[str, Path],
    artifact_rows: Sequence[Mapping[str, Any]],
    manifest_inputs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Resolve the selected ledger authorities already present in the production spec."""

    account_rows = [
        row
        for row in artifact_rows
        if row.get("kind") == "ARTIFACT"
        and row.get("node_id") == "financial-account-manifest-generation"
    ]
    if len(account_rows) != 1:
        raise MaterializationError(
            "selected HWM account-manifest generation is absent or ambiguous"
        )
    account_row = account_rows[0]
    account_path = Path(_text(account_row.get("path"), "HWM account generation path"))
    if account_path.name != "generation.json":
        raise MaterializationError("selected HWM account generation binding differs")
    account_definition = _file_definition(account_path)
    if (
        account_definition["sha256"] != account_row.get("sha256")
        or account_definition["size_bytes"] != account_row.get("size_bytes")
        or Path(cast(str, account_definition["path"])).resolve()
        != account_path.resolve()
    ):
        raise MaterializationError("selected HWM account generation binding differs")
    if isinstance(manifest_inputs, Mapping):
        admission_path = _validate_file_definition(
            manifest_inputs.get("account_strategy_admission_policy"),
            "account strategy admission policy",
        )
    else:
        admission_path = _admission_policy_artifact(artifact_rows)
    try:
        provider_path = sources["provider-ledger"] / "generation.json"
        baseline_path = sources["portfolio-baseline"] / "generation.json"
    except KeyError as exc:
        raise MaterializationError("selected HWM generation source is absent") from exc
    resolved: dict[str, Any] = {
        "account_manifest_generation_path": account_path.resolve(),
        "account_manifest_admission_policy_path": admission_path.resolve(),
        "account_manifest_flow_classification_policy_path": (
            ACCOUNT_STRATEGY_CLASSIFICATION_POLICY.resolve()
        ),
        "provider_ledger_generation_path": provider_path.resolve(),
        "baseline_generation_path": baseline_path.resolve(),
    }
    facts = _production_sleeve_facts_paths(artifact_rows)
    if facts:
        resolved["sleeve_facts_paths"] = facts
    return resolved


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
ACCOUNT_STRATEGY_MANIFEST_TOOL = (
    REPOSITORY_ROOT / "tools" / "account_strategy_manifest.py"
)
ACCOUNT_STRATEGY_CLASSIFICATION_POLICY = (
    REPOSITORY_ROOT
    / "contracts"
    / "account-strategy-flow-classification-policy-v1.json"
)
ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS = (
    REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
)
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
    "report": "report",
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
    # The trailing newline is load-bearing. `portfolio_release_builder
    # ._directory_inventory` and `portfolio_control_panel._directory_identity`
    # both hash the canonical rows plus a newline, and this function has to
    # validate what they write. Omitting it made every panel-candidate receipt
    # unverifiable: the rows agree byte for byte and only the framing differed.
    # `test_directory_identity_agrees_across_producers` pins all three together.
    return inventory_identity(_directory_rows(path))


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


def _financial_epoch_manifest_inputs(
    vertical: Mapping[str, Any],
) -> dict[str, dict[str, object]] | None:
    """Return the immutable five-file manifest authority for schema-9 recipes.

    Schema 9 is deliberately an additive, fail-closed extension of schema 7:
    the builder is allowed no caller-selected financial input beyond these five
    file identities.  The returned definitions are freshly derived from the
    files, not copied from a mutable recipe object.
    """

    if vertical.get("schema_version") != 9:
        return None
    if set(vertical) not in (
        SCHEMA_9_VERTICAL_RECIPE_KEYS,
        SCHEMA_9_VERTICAL_RECIPE_KEYS | {"hybrid_decomposition_plan_bundle"},
    ):
        raise MaterializationError("financial epoch manifest schema-9 inputs differ")
    definitions: dict[str, dict[str, object]] = {}
    for key in EPOCH_MANIFEST_INPUTS:
        path = _validate_file_definition(
            vertical.get(key), f"financial epoch manifest {key}"
        )
        definitions[key] = _file_definition(path)
    return definitions


def _epoch_manifest_generation_definition(
    vertical: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Re-read and bind the exact detached epoch-manifest generation for schema 10."""

    if vertical.get("schema_version") != 10:
        return None
    definition = vertical.get("epoch_manifest_generation")
    if not isinstance(definition, dict) or set(definition) != {
        "runtime_root",
        "generation_id",
        "source",
    }:
        raise MaterializationError("epoch-manifest generation definition differs")
    runtime_root = _absolute(
        definition["runtime_root"], "epoch_manifest_generation.runtime_root"
    )
    generation_id = _text(
        definition["generation_id"], "epoch_manifest_generation.generation_id"
    )
    if len(generation_id) != 64:
        raise MaterializationError("epoch-manifest generation_id differs")
    try:
        bundle = read_generation_bundle(runtime_root, generation_id)
        source = _financial_mvp_module().epoch_manifest_source_from_bundle(bundle)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "epoch-manifest generation bundle verification failed"
        ) from exc
    try:
        source_matches = canonical_json_bytes(
            definition["source"]
        ) == canonical_json_bytes(source)
    except (TypeError, ValueError) as exc:
        raise MaterializationError(
            "epoch-manifest generation source identity differs"
        ) from exc
    if not isinstance(source, dict) or not source_matches:
        raise MaterializationError("epoch-manifest generation source identity differs")
    return {
        "runtime_root": str(runtime_root),
        "generation_id": generation_id,
        "source": _canonical_clone(source),
    }


def _financial_epoch_manifest_gate_rows(
    definitions: Mapping[str, Mapping[str, object]] | None,
    proof: Mapping[str, Any] | None,
    gate_path: Path,
) -> list[dict[str, object]]:
    if definitions is None:
        return []
    if proof is None:
        raise MaterializationError("financial epoch manifest proof is required")
    payload = {
        "inputs": {
            key: _canonical_clone(definitions[key]) for key in EPOCH_MANIFEST_INPUTS
        },
        "post_build_proof_sha256": hashlib.sha256(
            canonical_json_bytes(proof)
        ).hexdigest(),
        "schema_version": 2,
    }
    content = canonical_json_bytes(payload)
    _write_exact(gate_path, content)
    return [
        {
            "gate_id": "financial-epoch-manifest-input-authority",
            "path": str(gate_path),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]


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
        relative_path = row["relative_path"]
        if (
            role == "report"
            and isinstance(relative_path, str)
            and (
                relative_path.lower().endswith(".xlsx")
                or any(
                    segment.lower() == "deliveries"
                    for segment in relative_path.replace("\\", "/").split("/")
                )
            )
        ):
            raise MaterializationError(
                "report generation cannot inventory non-authority workbook delivery"
            )
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


def _bind_epoch_manifest_bundle(
    generation_root: Path,
    generation_id: str,
    financial_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the financial contract to one verified epoch-manifest generation.

    This binds and validates; it does not stage the manifest into the run
    root. The panel command writes that file itself from the same verified
    bundle, and `build-panel` declares the directory it writes, so seeding it
    here made the node's own output pre-exist its pipeline receipt and every
    plan refused the run before it started.
    """

    try:
        bundle = read_generation_bundle(generation_root, generation_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "epoch-manifest generation bundle verification failed"
        ) from exc
    financial_module = _financial_mvp_module()
    try:
        source = financial_module.epoch_manifest_source_from_bundle(bundle)
        # The lookup is the check: a bundle that verified but carries no
        # manifest payload raises KeyError here rather than binding a source
        # document to a generation that does not contain it.
        _manifest_payload = bundle.payloads["outputs/account-strategy-manifest.json"]
    except (
        AttributeError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise MaterializationError("epoch-manifest bundle binding failed") from exc
    if not isinstance(source, dict):
        raise MaterializationError("epoch-manifest bundle binding failed")
    try:
        augmented = _canonical_clone(financial_contract)
        source_copy = _canonical_clone(source)
    except (TypeError, ValueError) as exc:
        raise MaterializationError("financial MVP contract clone failed") from exc
    if not isinstance(augmented, dict):
        raise MaterializationError("financial MVP contract must be an object")
    if (
        augmented.get("contract_schema_version") != 6
        or "epoch_manifest_source" in augmented
    ):
        raise MaterializationError(
            "epoch-manifest binding requires an unbound schema-v6 financial contract"
        )
    augmented["contract_schema_version"] = 7
    augmented["epoch_manifest_source"] = source_copy
    try:
        registry = _load(CURRENT_STRATEGY_REGISTRY, "current strategy registry")
        validated = financial_module.validate_contract(
            augmented,
            registry=registry,
            epoch_manifest_bundle=bundle,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "bound financial MVP contract validation failed"
        ) from exc
    if canonical_json_bytes(validated) != canonical_json_bytes(augmented):
        raise MaterializationError("bound financial MVP contract validation differs")

    return augmented


def _account_strategy_manifest_module() -> Any:
    """Load the repository epoch producer used for an independent rebuild."""

    module_name = "_account_strategy_manifest_for_pipeline_proof"
    spec = importlib.util.spec_from_file_location(
        module_name, ACCOUNT_STRATEGY_MANIFEST_TOOL
    )
    if spec is None or spec.loader is None:
        raise MaterializationError("account-strategy manifest producer is unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise MaterializationError("account-strategy manifest producer failed") from exc
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    module_file = getattr(module, "__file__", None)
    if (
        module_file is None
        or Path(module_file).resolve() != ACCOUNT_STRATEGY_MANIFEST_TOOL.resolve()
    ):
        raise MaterializationError("account-strategy manifest producer origin differs")
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


def _schwab_selected_point_positions(
    manifest: GenerationManifest,
    source: Mapping[str, Any],
    requested_as_of: date,
) -> dict[str, dict[str, Any]] | None:
    """Reconstruct point facts from reopened source authority, not ledger flags."""
    metadata = source.get("position_snapshot_as_of")
    if not isinstance(metadata, dict) or "observed_at" not in metadata:
        return None
    try:
        clock = metadata["observed_at"]
        captured = datetime.fromisoformat(clock)
        day = captured.date()
        transport = source["current_transport"]
        artifacts = [
            item
            for item in source["source_artifacts"]
            if item["schema"]["name"] == "schwab-api-current-snapshot"
        ]
        if (
            captured.tzinfo is None
            or captured.utcoffset() is None
            or day != manifest.as_of_date
            or source["as_of_date"] != day.isoformat()
            or source["catalog_schema_version"] not in {2, 3}
            or manifest.adapter.name != "quant-engine-schwab"
            or len(artifacts) != 1
            or transport not in {"WEB_CONTRACT", "DEVELOPER_API"}
        ):
            raise ValueError("point clock/date/transport differs")
        financial_observation_date = captured.astimezone(
            ZoneInfo("America/New_York")
        ).date()
        artifact = artifacts[0]
        if (
            artifact["as_of_date"] != day.isoformat()
            or artifact["schema"]["version"] not in {"1", "2"}
            or (artifact["schema"]["version"] == "1" and transport != "DEVELOPER_API")
            or type(artifact["size_bytes"]) is not int
            or artifact["size_bytes"] < 1
            or [
                a.to_dict()
                for a in manifest.artifacts
                if a.artifact_id == artifact["artifact_id"]
            ]
            != [artifact]
            or len(
                [
                    a
                    for a in source["source_artifacts"]
                    if a["artifact_id"] == artifact["artifact_id"]
                ]
            )
            != 1
        ):
            raise ValueError("point selected artifact differs")
        accounts = {row["account_id"] for row in source["accounts"]}
        current = source["current_accounts"]
        if (
            not accounts
            or len(accounts) != len(source["accounts"])
            or {row["account_id"] for row in current} != accounts
            or len(current) != len(accounts)
            or any(row["observed_at"] != clock for row in current)
        ):
            raise ValueError("point account/clock scope differs")
        expected = {}
        for native in source["positions"]:
            identity = {
                "account_id": native["account_id"],
                "as_of_date": native["as_of_date"],
                "security_id": native["security_id"],
            }
            record_id = native["record_id"]
            if (
                native["account_id"] not in accounts
                or native["as_of_date"] != day.isoformat()
                or native["observed_at"] != clock
                or native["security_id"]
                != "security-schwab-"
                + hashlib.sha256(native["symbol"].encode()).hexdigest()[:24]
                or record_id
                != "position-"
                + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
                or record_id in expected
                or native.get("source_artifact_id", artifact["artifact_id"])
                != artifact["artifact_id"]
                or native["evidence_class"]
                != (
                    "WCH_CURRENT"
                    if transport == "WEB_CONTRACT"
                    else "DEVELOPER_API_CURRENT"
                )
            ):
                raise ValueError("point native row scope differs")
            expected[record_id] = {
                **{
                    key: native[key]
                    for key in (
                        "account_id",
                        "as_of_date",
                        "observed_at",
                        "quantity",
                        "market_value",
                        "average_price",
                        "security_id",
                        "symbol",
                    )
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "schwab",
                "record_id": "schwab:" + record_id,
                "source_record_id": record_id,
                "source_artifact_id": artifact["artifact_id"],
                "generation_id": manifest.generation_id,
            }
        if source["current_position_account_count"] != len(
            {row["account_id"] for row in expected.values()}
        ):
            raise ValueError("point position account count differs")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise MaterializationError("Schwab selected point authority differs") from exc
    return expected if financial_observation_date <= requested_as_of else None


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
        or manifest.adapter.version != "5"
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
    try:
        financial_cutoff = date.fromisoformat(str(catalog["as_of_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MaterializationError("provider ledger catalog authority differs") from exc
    normalized_artifacts = {
        item.artifact_id: item
        for item in manifest.artifacts
        if item.artifact_id
        in {
            "normalized/provider-ledger.json",
            "normalized/admit-or-declare-receipt.json",
        }
    }
    observation = receipt.get("run_observation")
    receipt_cutoff = (
        observation.get("financial_cutoff")
        if isinstance(observation, dict)
        else catalog.get("as_of_date")
    )
    if (
        catalog.get("catalog_schema_version") != 2
        or receipt_cutoff != catalog.get("as_of_date")
        or set(normalized_artifacts)
        != {
            "normalized/provider-ledger.json",
            "normalized/admit-or-declare-receipt.json",
        }
        or any(
            artifact.as_of_date != financial_cutoff
            for artifact in normalized_artifacts.values()
        )
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
    observation = receipt.get("run_observation")
    if observation is not None:
        # Reopen selection and inputs independently. Receipt counts/hashes and
        # candidate dispositions are outputs, never the source inventory oracle.
        module_spec = importlib.util.spec_from_file_location(
            "_admission_source_reader", Path(__file__).with_name("provider_ledger.py")
        )
        if module_spec is None or module_spec.loader is None:
            raise MaterializationError("provider source reader is unavailable")
        reader = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(reader)
        try:
            observation = reader._object(observation, "provider run observation")
            observation_path = reader._absolute_regular_path(
                observation["path"], "provider run observation"
            )
            observed, digest, size = reader._verified_json(
                observation_path,
                observation["sha256"],
                "provider run observation",
            )
            if (
                size != observation["size_bytes"]
                or set(observed)
                != {
                    "schema_version",
                    "policy_contract_sha256",
                    "financial_cutoff",
                    "captured_at_utc",
                    "providers",
                }
                or observed.get("schema_version") != 1
                or observed.get("policy_contract_sha256") != contract_raw_sha256
                or observed.get("financial_cutoff") != catalog["as_of_date"]
                or any(
                    observed.get(key) != observation.get(key)
                    for key in (
                        "captured_at_utc",
                        "financial_cutoff",
                        "policy_contract_sha256",
                    )
                )
            ):
                raise MaterializationError("provider run observation identity differs")
            reader._utc_timestamp(
                observed["captured_at_utc"], "run observation capture"
            )
            selected = reader._object(
                observed["providers"], "run observation providers"
            )
            if set(selected) != set(reader._PROVIDER_CATALOGS):
                raise MaterializationError("provider run observation selection differs")
            identities, manifests, catalogs = [], {}, {}
            facts: dict[str, dict[str, list[dict[str, Any]]]] = {}
            cutoff = date.fromisoformat(observed["financial_cutoff"])
            for provider_id, selection in sorted(selected.items()):
                reference = {
                    key: selection[key]
                    for key in (
                        "root",
                        "generation_id",
                        "manifest_sha256",
                        "catalog_sha256",
                        "qualification",
                    )
                }
                identity, source_manifest, source_catalog, _, _ = reader._provider(
                    provider_id,
                    reference,
                    cutoff,
                    run_observation=observed,
                )
                identities.append(identity)
                manifests[provider_id] = source_manifest
                catalogs[provider_id] = source_catalog
                facts[provider_id] = {}
                effective = min(
                    cutoff, date.fromisoformat(selection["finalized_through"])
                )
                point_positions = (
                    _schwab_selected_point_positions(
                        source_manifest, source_catalog, cutoff
                    )
                    if provider_id == "schwab"
                    else None
                )
                for fact_name in reader._TARGET_ADMISSION_FACT:
                    rows = reader._objects(catalog.get(fact_name, []), fact_name)
                    if any(row.get("provider_id") not in selected for row in rows):
                        raise MaterializationError(
                            "materialized provider fact scope differs"
                        )
                    if fact_name == "provider_strategy_returns":
                        continue
                    rows = [row for row in rows if row["provider_id"] == provider_id]
                    point_bound = (
                        fact_name == "positions" and point_positions is not None
                    )
                    if point_bound:
                        assert point_positions is not None
                        if (
                            len(rows) != len(point_positions)
                            or {row.get("source_record_id") for row in rows}
                            != set(point_positions)
                            or any(
                                {
                                    key: value
                                    for key, value in row.items()
                                    if key not in {"mapping_status", "strategy_id"}
                                }
                                != point_positions.get(row.get("source_record_id"))
                                for row in rows
                            )
                        ):
                            raise MaterializationError(
                                "materialized Schwab point differs from selected native positions"
                            )
                    if (
                        provider_id == "ibkr"
                        and fact_name in reader._NATIVE_LEVERAGE_OBSERVATION_FACTS
                        and not rows
                    ):
                        continue
                    for row in rows:
                        days = [
                            date.fromisoformat(row[field])
                            for field in (
                                "as_of_date",
                                "event_date",
                                "effective_date",
                                "nav_recognition_date",
                                "value_date",
                                "accounting_date",
                                "settlement_date",
                                "period_start",
                                "period_end",
                                "report_date",
                            )
                            if row.get(field) not in {None, ""}
                        ]
                        if point_bound:
                            # Native equality above binds the original UTC date
                            # and clock; EOD selection uses the provider day.
                            days = [
                                datetime.fromisoformat(row["observed_at"])
                                .astimezone(ZoneInfo("America/New_York"))
                                .date()
                            ]
                        if row.get("generation_id") != identity.generation_id or (
                            any(
                                day > (cutoff if point_bound else effective)
                                for day in days
                            )
                        ):
                            raise MaterializationError(
                                "materialized provider fact cutoff or generation differs"
                            )
                    facts[provider_id][fact_name] = rows
                for fact_name in ("strategy_returns", "provider_strategy_returns"):
                    for row in reader._objects(catalog.get(fact_name, []), fact_name):
                        if (
                            row.get("provider_id") == provider_id
                            and (
                                fact_name == "provider_strategy_returns"
                                or row.get("evidence_layer") == "ACCOUNT_LIVE"
                            )
                            and date.fromisoformat(row["date"]) > effective
                        ):
                            raise MaterializationError(
                                "materialized strategy return cutoff differs"
                            )
            authority = {
                "census_id": census["census_id"],
                "census_sha256": census_sha256,
                "contract": contract,
                "sources": source_segments,
                "contract_raw_sha256": contract_raw_sha256,
                "contract_semantic_sha256": contract_semantic_sha256,
                "product_id": product["product_id"],
                "product_version": product["product_version"],
                "run_observation": observed,
                "run_observation_path": str(observation_path),
                "run_observation_sha256": digest,
                "run_observation_size_bytes": size,
            }
            reader._validate_current_census_provider_generations(
                authority, identities, manifests
            )
            expected_receipt, _ = reader._admit_or_declare_receipt(
                authority,
                identities,
                manifests,
                catalogs,
                facts,
                reader._objects(
                    catalog.get("provider_strategy_returns", []), "provider returns"
                ),
            )
            if receipt != expected_receipt:
                raise MaterializationError(
                    "provider admission differs from independent source inventory or materialized facts"
                )
            expected_generations = [
                {
                    **identity.to_dict(),
                    "generation_as_of_date": manifests[
                        identity.provider_id
                    ].as_of_date.isoformat(),
                }
                for identity in identities
            ]
            if catalog.get("provider_generations") != expected_generations:
                raise MaterializationError(
                    "provider ledger selected generations differ"
                )
            expected_generation_as_of = reader._generation_as_of_date(
                cutoff, tuple(manifest.artifacts)
            )
            if manifest.as_of_date != expected_generation_as_of:
                raise MaterializationError("provider ledger generation date differs")
        except MaterializationError:
            raise
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            raise MaterializationError(
                "provider source admission verification failed"
            ) from exc
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


def validate_source_phase(
    *,
    requested_as_of: str,
    provider_ledger_generation_root: Path,
    financial_input_contract_path: Path,
) -> dict[str, Any]:
    """Replay one retained provider admission at the frozen financial boundary.

    This is deliberately narrower than product conformance.  It proves the
    selected provider generation and the exact input contract it was admitted
    under; the release recipe continues to validate the public, history and
    numerical producer inputs through their existing readers.
    """

    try:
        requested_cutoff = date.fromisoformat(requested_as_of).isoformat()
    except (TypeError, ValueError) as exc:
        raise MaterializationError("requested financial cutoff is invalid") from exc
    generation = provider_ledger_generation_root.resolve()
    contract_path = financial_input_contract_path.resolve()
    module = _financial_mvp_module()
    try:
        contract = module.load_contract(
            contract_path, CURRENT_STRATEGY_REGISTRY, FINANCIAL_MVP_SCHEMA
        )
        contract_semantic_sha256 = module.contract_digest(contract)
        census_sha256 = module.census_digest(contract["current_census"])
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise MaterializationError("financial input contract is invalid") from exc
    contract_raw_sha256, _ = _sha(contract_path)
    admission = _provider_admission_authority(
        generation,
        contract,
        contract_raw_sha256=contract_raw_sha256,
        contract_semantic_sha256=contract_semantic_sha256,
        census_sha256=census_sha256,
    )
    try:
        catalog = _load(generation / "provider-ledger.json", "provider ledger catalog")
        receipt = admission["receipt"]
        if not isinstance(receipt, dict):
            raise ValueError("receipt differs")
        observation = receipt.get("run_observation")
        if not isinstance(observation, dict):
            raise ValueError("run observation differs")
        receipt_cutoff = observation.get("financial_cutoff")
        if (
            catalog.get("as_of_date") != requested_cutoff
            or receipt_cutoff != requested_cutoff
            or admission.get("admission_gate_status") != "PASS"
            or admission.get("product_readiness") != "READY_FOR_COMPOSITION"
        ):
            raise ValueError("financial cutoff or admission readiness differs")
    except (OSError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "provider source phase is not READY_FOR_COMPOSITION at requested financial cutoff"
        ) from exc
    return {
        "financial_data_cutoff": requested_cutoff,
        "financial_input_contract": _file_definition(contract_path),
        "provider_ledger_catalog": _file_definition(
            generation / "provider-ledger.json"
        ),
        "provider_ledger_generation_id": admission["generation_id"],
        "provider_ledger_manifest": _file_definition(generation / "generation.json"),
        "provider_ledger_receipt": _file_definition(
            generation / "admit-or-declare-receipt.json"
        ),
        "source_provenance": _canonical_clone(receipt.get("run_observation")),
        "status": "PASS",
    }


def _product_conformance(
    contract: Mapping[str, Any],
    admission: Mapping[str, Any],
    correction_registry: Mapping[str, Any],
    rederive_registry: Mapping[str, Any],
    *,
    candidate_generation_ids: Mapping[str, str],
    contract_raw_sha256: str,
    contract_semantic_sha256: str,
    census_sha256: str,
    source_matrix_sha256: str,
    invested_capital_policy_sha256: str,
) -> dict[str, Any]:
    if set(candidate_generation_ids) != set(RELEASE_ROLES) or any(
        not isinstance(value, str) or len(value) != 64
        for value in candidate_generation_ids.values()
    ):
        raise MaterializationError("financial MVP candidate generations differ")
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
        if (
            not isinstance(outputs, list)
            or not outputs
            or outputs != sorted(set(outputs))
        ):
            raise MaterializationError(
                f"re-derivation receipt scope differs: {correction_id}"
            )
        rederive_affected_stages = {
            stage
            for output in outputs
            for stage in _semantic_output_route(output)["affected_stages"]
        }
        output_role = next(
            (
                role
                for role in RELEASE_ROLES
                if GENERATION_STAGE[role] in rederive_affected_stages
            ),
            None,
        )
        receipt_payload = {
            "correction_id": correction_id,
            "input_generation_id": raw["input_generation_id"],
            "invalidated_outputs": outputs,
            "output_generation_id": raw["output_generation_id"],
            "status": raw["status"],
        }
        if (
            output_role is None
            or raw["input_generation_id"] != candidate_generation_ids["ledger"]
            or raw["output_generation_id"] != candidate_generation_ids[output_role]
            or raw["receipt_sha256"]
            != hashlib.sha256(canonical_json_bytes(receipt_payload)).hexdigest()
        ):
            raise MaterializationError(
                f"re-derivation receipt candidate binding differs: {correction_id}"
            )
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
    candidate_generation_ids: Mapping[str, str],
    correction_registry_path: Path | None = None,
    rederive_registry_path: Path | None = None,
    input_contract_path: Path | None = None,
) -> dict[str, Any]:
    vertical = _load(vertical_path, "vertical recipe")
    if vertical.get("schema_version") not in {7, 9, 10}:
        raise MaterializationError(
            "financial MVP composition requires vertical recipe schema 7, 9, or 10"
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
    # The admit-or-declare receipt records the contract the ledger was ADMITTED
    # under -- the retained account-input contract, which cannot be the product
    # contract because a contract never pins a ledger built from itself.  Compare
    # the receipt against the contract that produced it, while the product
    # authority (lineage, source matrix, conformance) stays the product contract.
    # With no separate input contract supplied this is the product contract,
    # exactly as before.
    admission_contract = contract
    admission_raw_sha256 = contract_raw_sha256
    admission_semantic_sha256 = contract_semantic_sha256
    admission_census_sha256 = census_sha256
    if input_contract_path is not None:
        resolved_input = input_contract_path.resolve()
        try:
            admission_contract = module.load_contract(
                resolved_input, CURRENT_STRATEGY_REGISTRY, FINANCIAL_MVP_SCHEMA
            )
            admission_semantic_sha256 = module.contract_digest(admission_contract)
            admission_census_sha256 = module.census_digest(
                admission_contract["current_census"]
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise MaterializationError(
                "financial MVP admitted input authority is invalid"
            ) from exc
        admission_raw_sha256, _ = _sha(resolved_input)

        # Matching the receipt is not sufficient: an old admission marked READY
        # could otherwise be paired with a current conformance contract that
        # governs different accounts or different financial rules.  This is the
        # predicate `portfolio_invested_capital.resolve_financial_input_authority`
        # already applies to the same current/retained pair -- identical product,
        # census, REQUIRED strategy set and invested-capital policy.
        def _required_strategy_ids(source: Mapping[str, Any]) -> set[str]:
            return {
                str(row["strategy_id"])
                for row in source["strategies"]
                if row.get("product_requirement") == "REQUIRED"
            }

        try:
            compatible = (
                contract["product"]["product_id"]
                == admission_contract["product"]["product_id"]
                and census_sha256 == admission_census_sha256
                and _required_strategy_ids(contract)
                == _required_strategy_ids(admission_contract)
                and module.invested_capital_policy_digest(contract)
                == module.invested_capital_policy_digest(admission_contract)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterializationError(
                "financial MVP admitted input authority is invalid"
            ) from exc
        if not compatible:
            raise MaterializationError(
                "admitted input differs from the current calculation authority"
            )
    admission = _provider_admission_authority(
        provider_generation,
        admission_contract,
        contract_raw_sha256=admission_raw_sha256,
        contract_semantic_sha256=admission_semantic_sha256,
        census_sha256=admission_census_sha256,
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
        candidate_generation_ids=candidate_generation_ids,
        contract_raw_sha256=contract_raw_sha256,
        contract_semantic_sha256=contract_semantic_sha256,
        census_sha256=census_sha256,
        source_matrix_sha256=source_matrix_sha256,
        invested_capital_policy_sha256=invested_sha256,
    )
    derived: dict[str, Any] = {
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
    if input_contract_path is not None:
        # Recorded so _recipe can re-derive the identical block: the admission
        # comparison depends on which contract the ledger was admitted under.
        derived["input_contract"] = _file_definition(input_contract_path.resolve())
    return derived


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
        "schema_version": 3,
    }


def _hwm_input_authority_assessment(
    authority: Mapping[str, Any],
    *,
    hwm_bindings: Mapping[str, Path] | None = None,
    hwm_source_paths: Mapping[str, Path] | None = None,
    hwm_sleeve_facts_paths: Sequence[Path] = (),
    assessment_path: Path | None = None,
    input_contract_path: Path | None = None,
) -> dict[str, Any]:
    """Independently assess the actual governed PORT-HWM-4 result bundle."""

    conformance = authority.get("conformance")
    provider = authority.get("provider_generation")
    if not isinstance(conformance, Mapping) or not isinstance(provider, Mapping):
        raise MaterializationError("financial MVP HWM authority differs")
    sources = conformance.get("sources")
    outcomes = conformance.get("outcomes")
    strategies = conformance.get("strategies")
    if (
        not isinstance(sources, list)
        or not isinstance(outcomes, list)
        or not isinstance(strategies, list)
    ):
        raise MaterializationError("financial MVP HWM authority differs")

    hwm_outcomes = [
        row
        for row in outcomes
        if isinstance(row, Mapping) and row.get("outcome_id") == "AS_HELD_COMPOSITE_HWM"
    ]
    if len(hwm_outcomes) > 1:
        raise MaterializationError("financial MVP HWM outcome is ambiguous")
    unresolved_source_ids = sorted(
        str(row["source_id"])
        for row in sources
        if isinstance(row, Mapping)
        and isinstance(row.get("consumers"), list)
        and "AS_HELD_COMPOSITE_HWM" in row["consumers"]
        and isinstance(row.get("current_generation"), Mapping)
        and (
            row["current_generation"].get("generation_id") is None
            or row["current_generation"].get("sha256") is None
        )
    )
    required_strategy_ids = sorted(
        str(row["strategy_id"])
        for row in strategies
        if isinstance(row, Mapping) and row.get("product_requirement") == "REQUIRED"
    )
    outcome_readiness = (
        str(hwm_outcomes[0].get("readiness")) if hwm_outcomes else "NOT_DECLARED"
    )
    blockers: set[str] = {
        "ACCEPTED_HWM_INPUT_GENERATION_ABSENT",
        "ACCEPTED_MIXED_CALENDAR_AUTHORITY_ABSENT",
        *(f"SOURCE:{source_id}" for source_id in unresolved_source_ids),
    }
    provider_admission = conformance.get("provider_admission")
    if not isinstance(provider_admission, Mapping):
        raise MaterializationError("financial MVP HWM provider authority differs")
    if provider_admission.get("product_readiness") != "READY_FOR_COMPOSITION":
        blockers.add("PROVIDER_ADMISSION")

    base = {
        "assessment_scope": "PENDING_GOVERNED_HWM_RESULT",
        "authority_hashes": {
            "census_sha256": conformance["census_sha256"],
            "contract_raw_sha256": authority["contract"]["sha256"],
            "contract_semantic_sha256": conformance["contract_semantic_sha256"],
            "provider_catalog_sha256": provider["catalog"]["sha256"],
            "provider_manifest_sha256": provider["manifest"]["sha256"],
            "provider_receipt_sha256": provider["receipt"]["sha256"],
            "source_matrix_sha256": authority["source_matrix"]["sha256"],
        },
        "bindings": {},
        "blocking_reasons": sorted(blockers),
        "calendar_authority_sha256": None,
        "input_generation_sha256": None,
        "outcome_id": "AS_HELD_COMPOSITE_HWM",
        "outcome_readiness": outcome_readiness,
        "required_strategy_ids": required_strategy_ids,
        "result_sha256": None,
        "schema_version": 2,
        "state": "REQUIRED_NOT_READY",
        "unresolved_source_ids": unresolved_source_ids,
    }
    if hwm_bindings is None:
        return base
    if hwm_source_paths is None:
        raise MaterializationError("selected HWM source bindings are required")
    if set(hwm_bindings) != HWM_BINDING_KEYS:
        raise MaterializationError("governed HWM bindings must be supplied together")
    paths = {key: Path(value).resolve() for key, value in hwm_bindings.items()}
    if assessment_path is not None and any(
        path == assessment_path.resolve() for path in paths.values()
    ):
        raise MaterializationError("governed HWM assessment self-reference")
    definitions = {key: _file_definition(path) for key, path in paths.items()}
    contract_path = _validate_file_definition(
        authority.get("contract"), "financial MVP contract"
    )
    provider_receipt = provider.get("receipt")
    if (
        not isinstance(provider_receipt, Mapping)
        or definitions["provider_admission_receipt"] != provider_receipt
    ):
        raise MaterializationError("governed HWM admission receipt binding differs")
    try:
        result = _load(paths["governed_result"], "governed HWM result")
        # The governed HWM kernel is bound to the contract the provider ledger was
        # ADMITTED under, which the admission receipt names.  That is the retained
        # account-input contract, not the product contract this spec's authority
        # gate carries: a contract can never pin a ledger built from itself, so the
        # two legitimately differ.  portfolio_report.py:5312 and
        # portfolio_control_panel.py:8219 already reconcile this the same way --
        # retarget the current policy onto the admitted input and require the
        # result to agree.  With no separate input contract supplied this is the
        # product contract, exactly as before.
        input_contract_bytes = (
            contract_path if input_contract_path is None else input_contract_path
        ).read_bytes()
        try:
            governed_policy_bytes = governed_hwm_input_policy(
                HWM_GOVERNED_POLICY.read_bytes(),
                input_contract_bytes,
                _text(result.get("policy_sha256"), "governed HWM policy sha256"),
            )
        except CapitalValidationError as exc:
            raise MaterializationError(
                "governed HWM policy differs from the admitted input authority"
            ) from exc
        accepted = verify_governed_hwm_result(
            result,
            paths["input_generation"].read_bytes(),
            calendar_bytes=paths["calendar_authority"].read_bytes(),
            input_receipt_bytes=paths["provider_admission_receipt"].read_bytes(),
            policy_bytes=governed_policy_bytes,
            product_contract_bytes=input_contract_bytes,
            performance_policy_bytes=HWM_PERFORMANCE_POLICY.read_bytes(),
            legacy_policy_bytes=HWM_LEGACY_POLICY.read_bytes(),
        )
    except (HwmCoverageError, OSError, TypeError, ValueError, KeyError) as exc:
        raise MaterializationError("governed HWM result verification failed") from exc
    memberships = accepted.get("input_evidence", {}).get("memberships")
    verified_strategy_ids = (
        sorted(
            str(row.get("strategy_id"))
            for row in memberships
            if isinstance(row, Mapping) and row.get("strategy_id") is not None
        )
        if isinstance(memberships, list)
        else []
    )
    if (
        accepted.get("status") != "AVAILABLE"
        or verified_strategy_ids != required_strategy_ids
    ):
        raise MaterializationError(
            "governed HWM result does not cover required strategies"
        )
    try:
        from portfolio_financial_outcomes import verify_selected_hwm_generation

        resolved_source_ids = verify_selected_hwm_generation(
            hwm_source_paths,
            contract_path=contract_path,
            bindings=paths,
            input_contract_path=input_contract_path,
            policy_bytes=governed_policy_bytes,
            sleeve_facts_paths=hwm_sleeve_facts_paths,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise MaterializationError(
            "selected HWM source replay verification failed"
        ) from exc
    remaining_source_ids = sorted(set(unresolved_source_ids) - set(resolved_source_ids))
    if remaining_source_ids or (
        provider_admission.get("product_readiness") != "READY_FOR_COMPOSITION"
    ):
        raise MaterializationError("governed HWM required authority is absent")
    return {
        **base,
        "assessment_scope": "VERIFIED_GOVERNED_HWM_RESULT",
        "bindings": definitions,
        "blocking_reasons": [],
        "calendar_authority_sha256": definitions["calendar_authority"]["sha256"],
        "input_generation_sha256": definitions["input_generation"]["sha256"],
        "result_sha256": definitions["governed_result"]["sha256"],
        "state": "AVAILABLE",
        "unresolved_source_ids": remaining_source_ids,
        **(
            {}
            if input_contract_path is None
            else {"input_contract": _file_definition(input_contract_path)}
        ),
    }


def _product_gate_rows(
    authority: Mapping[str, Any],
    product_path: Path,
    *,
    hwm_bindings: Mapping[str, Path] | None = None,
    hwm_source_paths: Mapping[str, Path] | None = None,
    hwm_sleeve_facts_paths: Sequence[Path] = (),
    input_contract_path: Path | None = None,
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
    hwm_path = product_path.with_name("financial-mvp-hwm-input-authority.json")
    hwm_bytes = canonical_json_bytes(
        _hwm_input_authority_assessment(
            authority,
            hwm_bindings=hwm_bindings,
            hwm_source_paths=hwm_source_paths,
            hwm_sleeve_facts_paths=hwm_sleeve_facts_paths,
            assessment_path=hwm_path,
            input_contract_path=input_contract_path,
        )
    )
    _write_exact(hwm_path, hwm_bytes)
    rows.append(
        {
            "gate_id": "financial-mvp-hwm-input-authority",
            "path": str(hwm_path),
            "sha256": hashlib.sha256(hwm_bytes).hexdigest(),
        }
    )
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
    if schema_version not in {2, 3}:
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
        vertical.get("schema_version") not in {6, 7, 9, 10}
        or vertical.get("release_commit") != recipe["release_commit"]
        or vertical.get("as_of_date") != recipe["as_of_date"]
    ):
        raise MaterializationError("vertical recipe release identity differs")
    manifest_inputs = _financial_epoch_manifest_inputs(vertical)
    epoch_generation = _epoch_manifest_generation_definition(vertical)
    if manifest_inputs is None:
        required.discard("financial_epoch_manifest_inputs")
        required.discard("financial_epoch_manifest_proof")
    else:
        required.add("financial_epoch_manifest_inputs")
        required.add("financial_epoch_manifest_proof")
    if epoch_generation is None:
        required.discard("epoch_manifest_generation")
    else:
        required.add("epoch_manifest_generation")
    if set(recipe) != required:
        raise MaterializationError("portfolio pipeline recipe differs")
    if manifest_inputs is not None and canonical_json_bytes(
        recipe["financial_epoch_manifest_inputs"]
    ) != canonical_json_bytes(manifest_inputs):
        raise MaterializationError("financial epoch manifest inputs are stale")
    if manifest_inputs is not None:
        proof = recipe.get("financial_epoch_manifest_proof")
        if (
            not isinstance(proof, dict)
            or proof.get("schema_version") != 1
            or proof.get("status") != "PASS"
            or proof.get("financial_epoch_manifest_inputs_sha256")
            != hashlib.sha256(canonical_json_bytes(manifest_inputs)).hexdigest()
        ):
            raise MaterializationError("financial epoch manifest proof differs")
    if epoch_generation is not None and canonical_json_bytes(
        recipe["epoch_manifest_generation"]
    ) != canonical_json_bytes(epoch_generation):
        raise MaterializationError("epoch-manifest generation is stale")
    _validate_directory_definition(recipe["qa_dir"], "panel QA")
    expected = recipe["expected_generations"]
    if not isinstance(expected, dict) or set(expected) != set(RELEASE_ROLES):
        raise MaterializationError("expected release generations differ")
    for role in RELEASE_ROLES:
        _generation_expectation(expected[role], role)
    if schema_version == 3:
        authority = recipe["product_authority"]
        # input_contract is present only when the ledger was admitted under a
        # contract other than the product contract.
        if not isinstance(authority, dict) or set(authority) - {"input_contract"} != {
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
            {role: str(expected[role]["generation_id"]) for role in RELEASE_ROLES},
            registry_paths["correction_registry"],
            registry_paths["rederive_registry"],
            input_contract_path=(
                None
                if authority.get("input_contract") is None
                else _validate_file_definition(
                    authority["input_contract"], "financial MVP admitted input"
                )
            ),
        )
        if canonical_json_bytes(authority) != canonical_json_bytes(expected_authority):
            raise MaterializationError("financial MVP product authority is stale")
    return recipe


def _vertical_source_paths(vertical_path: Path) -> list[Path]:
    vertical = _load(vertical_path, "vertical recipe")
    schema_version = vertical.get("schema_version")
    if schema_version not in {6, 7, 9, 10}:
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
    manifest_inputs = _financial_epoch_manifest_inputs(vertical)
    if manifest_inputs is not None:
        paths.extend(
            _absolute(manifest_inputs[key]["path"], f"financial epoch manifest {key}")
            for key in EPOCH_MANIFEST_INPUTS
        )
    epoch_generation = _epoch_manifest_generation_definition(vertical)
    if epoch_generation is not None:
        paths.append(Path(epoch_generation["runtime_root"]))
    for key in (
        "strategy_attribution_root",
        "decomposition_plan_bundle",
        "correlation_source_root",
        *(
            ("hybrid_decomposition_plan_bundle",)
            if "hybrid_decomposition_plan_bundle" in vertical
            else ()
        ),
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


def _epoch_generation_artifact(manifest: GenerationManifest, artifact_id: str) -> Any:
    matches = [item for item in manifest.artifacts if item.artifact_id == artifact_id]
    if len(matches) != 1:
        raise MaterializationError(
            f"epoch-manifest panel generation artifact differs: {artifact_id}"
        )
    return matches[0]


def _epoch_json_snapshot(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise MaterializationError(f"{label} is missing or linked")
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise MaterializationError(f"{label} root differs")
    return content, value


def _financial_epoch_manifest_proof(
    recipe: Mapping[str, Any], output_root: Path
) -> dict[str, Any] | None:
    """Independently prove the emitted schema-9 producer -> panel seam.

    Recipe definitions are only selectors.  PASS evidence comes from reopening
    the exact emitted panel generation, checking its GenerationManifest, and
    rebuilding the complete account-strategy manifest from the five selected
    inputs plus the exact emitted baseline generation and repository policy.
    The rebuilt object must equal both the staged producer artifact and the
    manifest embedded in dashboard data, so every row, status, value, and
    source identity participates in the proof digest.
    """

    definitions = recipe.get("financial_epoch_manifest_inputs")
    if definitions is None:
        return None
    if not isinstance(definitions, dict) or set(definitions) != set(
        EPOCH_MANIFEST_INPUTS
    ):
        raise MaterializationError("financial epoch manifest definitions differ")

    expected_panel = _generation_expectation(
        recipe["expected_generations"]["panel"], "panel"
    )
    panel_generation = _generation_dir(output_root, recipe, "panel")
    manifest_path = panel_generation / "generation.json"
    manifest_bytes, _manifest_payload = _epoch_json_snapshot(
        manifest_path, "epoch-manifest panel generation manifest"
    )
    try:
        generation_manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except (ValueError, RuntimeError) as exc:
        raise MaterializationError(
            "epoch-manifest panel generation manifest is invalid"
        ) from exc
    if (
        generation_manifest.generation_id != expected_panel["generation_id"]
        or generation_manifest.verification is not VerificationStatus.VERIFIED
        or generation_manifest.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        or hashlib.sha256(manifest_bytes).hexdigest()
        != expected_panel["manifest_sha256"]
    ):
        raise MaterializationError("epoch-manifest panel generation authority differs")

    staged_path = output_root / "stages" / "panel" / "account-strategy-manifest.json"
    staged_bytes, staged_manifest = _epoch_json_snapshot(
        staged_path, "staged account-strategy manifest"
    )
    dashboard_path = panel_generation / "data" / "dashboard_data.json"
    dashboard_bytes, dashboard = _epoch_json_snapshot(
        dashboard_path, "epoch-manifest panel data"
    )
    candidate_path = panel_generation / "candidate.json"
    candidate_bytes, candidate = _epoch_json_snapshot(
        candidate_path, "epoch-manifest panel candidate"
    )

    account_artifact = _epoch_generation_artifact(
        generation_manifest, "input/account-strategy-manifest.json"
    )
    dashboard_artifact = _epoch_generation_artifact(
        generation_manifest, "panel/data/dashboard_data.json"
    )
    candidate_artifact = _epoch_generation_artifact(
        generation_manifest, "panel/candidate.json"
    )
    if (
        account_artifact.schema_name != "account-strategy-manifest"
        or account_artifact.schema_version != "7"
        or (account_artifact.sha256, account_artifact.size_bytes)
        != (hashlib.sha256(staged_bytes).hexdigest(), len(staged_bytes))
        or (dashboard_artifact.sha256, dashboard_artifact.size_bytes)
        != (hashlib.sha256(dashboard_bytes).hexdigest(), len(dashboard_bytes))
        or (candidate_artifact.sha256, candidate_artifact.size_bytes)
        != (hashlib.sha256(candidate_bytes).hexdigest(), len(candidate_bytes))
    ):
        raise MaterializationError("epoch-manifest panel artifact binding differs")

    bound_artifact_ids = {
        "account_strategy_ledger": "input-bound/manifest_ledger",
        "account_strategy_ledger_identity": "input-bound/manifest_ledger_identity",
        "account_strategy_classified_flows": ("input-bound/manifest_classified_flows"),
        "account_strategy_flow_coverage": ("input-bound/manifest_flow_coverage"),
        "account_strategy_admission_policy": ("input-bound/manifest_admission_policy"),
    }
    bound_inputs: dict[str, dict[str, object]] = {}
    selected_paths: dict[str, Path] = {}
    for key, artifact_id in bound_artifact_ids.items():
        definition = definitions.get(key)
        if not isinstance(definition, dict):
            raise MaterializationError(
                f"financial epoch manifest proof {key} definition differs"
            )
        path = _validate_file_definition(
            definition, f"financial epoch manifest proof {key}"
        )
        selected_paths[key] = path
        artifact = _epoch_generation_artifact(generation_manifest, artifact_id)
        expected_identity = {
            "sha256": definition["sha256"],
            "size_bytes": definition["size_bytes"],
        }
        actual_identity = {
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
        if actual_identity != expected_identity:
            raise MaterializationError(
                f"epoch-manifest panel source binding differs: {key}"
            )
        bound_inputs[key] = actual_identity

    baseline_generation_id = _text(
        recipe["expected_generations"]["baseline"]["generation_id"],
        "epoch-manifest baseline generation_id",
    )
    producer = _account_strategy_manifest_module()
    try:
        rebuilt = producer.build_manifest_candidate(
            ledger_path=selected_paths["account_strategy_ledger"],
            ledger_identity_path=selected_paths["account_strategy_ledger_identity"],
            baseline_root=output_root / "stages" / "baseline",
            baseline_generation_id=baseline_generation_id,
            classified_flows_path=selected_paths["account_strategy_classified_flows"],
            flow_coverage_path=selected_paths["account_strategy_flow_coverage"],
            classification_policy_path=ACCOUNT_STRATEGY_CLASSIFICATION_POLICY,
            admission_policy_path=selected_paths["account_strategy_admission_policy"],
            membership_census_path=ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise MaterializationError(
            "independent post-build epoch-manifest rebuild failed"
        ) from exc
    rebuilt_bytes = canonical_json_bytes(rebuilt)
    if staged_bytes != rebuilt_bytes:
        raise MaterializationError(
            "staged account-strategy manifest differs from independent rebuild"
        )
    panel_manifest = dashboard.get("account_manifest")
    if not isinstance(panel_manifest, dict):
        raise MaterializationError("panel account-strategy manifest shape differs")
    if canonical_json_bytes(panel_manifest) != rebuilt_bytes:
        raise MaterializationError(
            "panel account-strategy manifest differs from independent rebuild"
        )

    meta = dashboard.get("meta")
    if not isinstance(meta, dict):
        raise MaterializationError("epoch-manifest panel metadata differs")
    expected_verification = {
        "method": "EXACT_REBUILD_REPORT_BASELINE_CANONICAL_POLICY_AND_TEMPLATE_BOUND",
        "producer_readiness": rebuilt.get("authority_readiness"),
        "publication_readiness": "READY",
        "status": "PANEL_INDEPENDENT_EXACT_REBUILD_VERIFIED",
    }
    authority_profile = meta.get("account_manifest_authority_profile")
    verification = meta.get("account_manifest_verification")
    if (
        authority_profile != "INDEPENDENT_EXACT_REBUILD"
        or verification != expected_verification
        or meta.get("account_manifest_sha256")
        != hashlib.sha256(staged_bytes).hexdigest()
        or meta.get("account_manifest_size_bytes") != len(staged_bytes)
        or candidate.get("account_manifest_authority_profile") != authority_profile
        or candidate.get("account_manifest_verification") != verification
    ):
        raise MaterializationError("epoch-manifest panel authority profile differs")
    artifact_sha256 = candidate.get("artifact_sha256")
    if not isinstance(artifact_sha256, dict) or (
        artifact_sha256.get("data/dashboard_data.json")
        != hashlib.sha256(dashboard_bytes).hexdigest()
    ):
        raise MaterializationError("epoch-manifest panel candidate binding differs")

    authority_readiness = rebuilt.get("authority_readiness")
    source_identities = rebuilt.get("source_identities")
    rows = rebuilt.get("rows")
    if (
        not isinstance(authority_readiness, dict)
        or not isinstance(source_identities, dict)
        or not isinstance(rows, list)
    ):
        raise MaterializationError("independent epoch-manifest proof shape differs")
    status_counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MaterializationError("independent epoch-manifest row differs")
        status = _text(row.get("status"), "epoch-manifest row status")
        status_counts[status] = status_counts.get(status, 0) + 1

    inputs_sha256 = hashlib.sha256(canonical_json_bytes(definitions)).hexdigest()
    proof = {
        "account_manifest_sha256": hashlib.sha256(staged_bytes).hexdigest(),
        "authority_profile": authority_profile,
        "authority_readiness_sha256": hashlib.sha256(
            canonical_json_bytes(authority_readiness)
        ).hexdigest(),
        "bound_inputs_sha256": hashlib.sha256(
            canonical_json_bytes(bound_inputs)
        ).hexdigest(),
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "dashboard_data_sha256": hashlib.sha256(dashboard_bytes).hexdigest(),
        "epoch_row_count": len(rows),
        "epoch_rows_sha256": hashlib.sha256(
            canonical_json_bytes({"rows": rows})
        ).hexdigest(),
        "epoch_status_counts": dict(sorted(status_counts.items())),
        "financial_epoch_manifest_inputs_sha256": inputs_sha256,
        "panel_generation_id": generation_manifest.generation_id,
        "panel_generation_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "schema_version": 1,
        "source_identities_sha256": hashlib.sha256(
            canonical_json_bytes(source_identities)
        ).hexdigest(),
        "status": "PASS",
    }
    return proof


def _verified_epoch_manifest_materialization_proof(
    recipe: Mapping[str, Any], output_root: Path
) -> dict[str, Any] | None:
    """Prove schema-10 bundle bytes survive unchanged into the published panel."""

    definition = recipe.get("epoch_manifest_generation")
    if definition is None:
        return None
    if not isinstance(definition, dict) or set(definition) != {
        "runtime_root",
        "generation_id",
        "source",
    }:
        raise MaterializationError("epoch-manifest generation definition differs")
    generation_root = _absolute(
        definition["runtime_root"], "epoch_manifest_generation.runtime_root"
    )
    generation_id = _text(
        definition["generation_id"], "epoch_manifest_generation.generation_id"
    )
    try:
        bundle = read_generation_bundle(generation_root, generation_id)
        financial_module = _financial_mvp_module()
        source = financial_module.epoch_manifest_source_from_bundle(bundle)
        source_bytes = bundle.payloads["outputs/account-strategy-manifest.json"]
    except (
        AttributeError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise MaterializationError(
            "epoch-manifest generation bundle verification failed"
        ) from exc
    if canonical_json_bytes(source) != canonical_json_bytes(definition["source"]):
        raise MaterializationError("epoch-manifest generation source identity differs")

    bound_path = output_root / "financial-mvp-bound-contract.json"
    bound_bytes, bound_contract = _epoch_json_snapshot(
        bound_path, "bound financial MVP contract"
    )
    try:
        registry = _load(CURRENT_STRATEGY_REGISTRY, "current strategy registry")
        validated_bound_contract = financial_module.validate_contract(
            bound_contract,
            registry=registry,
            epoch_manifest_bundle=bundle,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "bound financial MVP contract validation failed"
        ) from exc
    if (
        canonical_json_bytes(validated_bound_contract) != bound_bytes
        or bound_contract.get("epoch_manifest_source") != source
    ):
        raise MaterializationError("bound financial MVP contract authority differs")

    staged_path = output_root / "stages" / "panel" / "account-strategy-manifest.json"
    staged_bytes, staged_manifest = _epoch_json_snapshot(
        staged_path, "staged verified account-strategy manifest"
    )
    if staged_bytes != source_bytes:
        raise MaterializationError(
            "staged account-strategy manifest differs from verified bundle"
        )

    expected_panel = _generation_expectation(
        recipe["expected_generations"]["panel"], "panel"
    )
    panel_generation = _generation_dir(output_root, recipe, "panel")
    manifest_bytes, _manifest_payload = _epoch_json_snapshot(
        panel_generation / "generation.json",
        "verified epoch-manifest panel generation manifest",
    )
    try:
        panel_manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except (ValueError, RuntimeError) as exc:
        raise MaterializationError(
            "verified epoch-manifest panel generation manifest is invalid"
        ) from exc
    if (
        panel_manifest.generation_id != expected_panel["generation_id"]
        or panel_manifest.verification is not VerificationStatus.VERIFIED
        or hashlib.sha256(manifest_bytes).hexdigest()
        != expected_panel["manifest_sha256"]
    ):
        raise MaterializationError("verified epoch-manifest panel authority differs")

    dashboard_bytes, dashboard = _epoch_json_snapshot(
        panel_generation / "data" / "dashboard_data.json",
        "verified epoch-manifest panel data",
    )
    candidate_bytes, candidate = _epoch_json_snapshot(
        panel_generation / "candidate.json",
        "verified epoch-manifest panel candidate",
    )
    account_artifact = _epoch_generation_artifact(
        panel_manifest, "input/account-strategy-manifest.json"
    )
    # The panel declares `input/*` as logical snapshot artifacts and does not materialize
    # them (portfolio_control_panel.py:12784) -- no panel generation built by this product
    # has ever contained the directory. Requiring the physical file therefore rejected every
    # real panel, while the bytes it guards are already proved to reach the published panel:
    # the staged copy above is asserted equal to the bundle payload, the declared artifact
    # below equal to the staged copy, and the dashboard's embedded manifest canonicalizes to
    # the same bytes. So verify the physical file where a producer does write it, and do not
    # demand it where one declares it logically.
    physical_input = panel_generation / "input" / "account-strategy-manifest.json"
    if physical_input.is_symlink():
        raise MaterializationError("physical panel input must not be a link")
    if physical_input.exists():
        if _sha(physical_input) != (
            hashlib.sha256(source_bytes).hexdigest(),
            len(source_bytes),
        ):
            raise MaterializationError(
                "physical panel input differs from verified bundle"
            )
    if (
        account_artifact.schema_name != "account-strategy-manifest"
        or account_artifact.schema_version != str(staged_manifest.get("schema_version"))
        or (account_artifact.sha256, account_artifact.size_bytes)
        != (hashlib.sha256(staged_bytes).hexdigest(), len(staged_bytes))
    ):
        raise MaterializationError("verified epoch-manifest panel artifact differs")
    embedded = dashboard.get("account_manifest")
    if not isinstance(embedded, dict) or canonical_json_bytes(embedded) != staged_bytes:
        raise MaterializationError(
            "panel account-strategy manifest differs from verified bundle"
        )
    meta = dashboard.get("meta")
    if not isinstance(meta, dict):
        raise MaterializationError("verified epoch-manifest panel metadata differs")
    verification = meta.get("account_manifest_verification")
    if not isinstance(verification, dict):
        raise MaterializationError("verified epoch-manifest panel verification differs")
    independent_report = verification.get("independent_report")
    if (
        meta.get("account_manifest_authority_profile")
        != "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES"
        or meta.get("account_manifest_sha256")
        != hashlib.sha256(staged_bytes).hexdigest()
        or meta.get("account_manifest_size_bytes") != len(staged_bytes)
        or verification.get("status") != "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
        or verification.get("method")
        != "READ_GENERATION_BUNDLE_EXACT_BYTES_AND_FINANCIAL_MVP_BINDER"
        or verification.get("publication_readiness") != "NOT_READY"
        or verification.get("producer_readiness")
        != staged_manifest.get("authority_readiness")
        or verification.get("epoch_manifest_source") != source
        or not isinstance(independent_report, dict)
        or independent_report.get("status") != "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
        or candidate.get("account_manifest_authority_profile")
        != meta.get("account_manifest_authority_profile")
        or candidate.get("account_manifest_verification") != verification
    ):
        raise MaterializationError("verified epoch-manifest panel lineage differs")
    candidate_artifacts = candidate.get("artifact_sha256")
    if (
        not isinstance(candidate_artifacts, dict)
        or candidate_artifacts.get("data/dashboard_data.json")
        != hashlib.sha256(dashboard_bytes).hexdigest()
    ):
        raise MaterializationError("verified epoch-manifest panel candidate differs")

    return {
        "account_manifest_sha256": hashlib.sha256(staged_bytes).hexdigest(),
        "account_manifest_size_bytes": len(staged_bytes),
        "authority_profile": meta["account_manifest_authority_profile"],
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "dashboard_data_sha256": hashlib.sha256(dashboard_bytes).hexdigest(),
        "epoch_manifest_source_sha256": hashlib.sha256(
            canonical_json_bytes(source)
        ).hexdigest(),
        "financial_mvp_bound_contract_sha256": hashlib.sha256(bound_bytes).hexdigest(),
        "panel_generation_id": panel_manifest.generation_id,
        "panel_generation_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "schema_version": 1,
        "status": "PASS",
    }


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
    financial_input_contract: Path | None = None,
) -> dict[str, Any]:
    vertical = _load(vertical_recipe.resolve(), "vertical recipe")
    if vertical.get("schema_version") not in {6, 7, 9, 10}:
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
    manifest_inputs = _financial_epoch_manifest_inputs(vertical)
    if manifest_inputs is not None:
        payload["financial_epoch_manifest_inputs"] = manifest_inputs
        proof = _financial_epoch_manifest_proof(payload, proof_root)
        if proof is None:  # pragma: no cover - guarded by manifest_inputs
            raise MaterializationError("financial epoch manifest proof is unavailable")
        payload["financial_epoch_manifest_proof"] = proof
    epoch_generation = _epoch_manifest_generation_definition(vertical)
    if epoch_generation is not None:
        payload["epoch_manifest_generation"] = epoch_generation
    if financial_mvp_contract is None or financial_mvp_source_matrix is None:
        raise MaterializationError(
            "current pipeline recipe requires financial MVP contract and source matrix"
        )
    payload["product_authority"] = _derive_product_authority(
        vertical_recipe.resolve(),
        financial_mvp_contract.resolve(),
        financial_mvp_source_matrix.resolve(),
        {
            role: str(_generation_expectation(expected[role], role)["generation_id"])
            for role in RELEASE_ROLES
        },
        None if correction_registry is None else correction_registry.resolve(),
        (
            None
            if rederive_receipt_registry is None
            else rederive_receipt_registry.resolve()
        ),
        input_contract_path=financial_input_contract,
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


def _authority_payload(
    recipe: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "as_of_date": recipe["as_of_date"],
        "authority_policy": "IMMUTABLE_VERTICAL_RECIPE",
        "financial_epoch_manifest_inputs_sha256": (
            hashlib.sha256(
                canonical_json_bytes(recipe["financial_epoch_manifest_inputs"])
            ).hexdigest()
            if recipe.get("financial_epoch_manifest_inputs") is not None
            else None
        ),
        **(
            {
                "financial_epoch_manifest_proof_sha256": hashlib.sha256(
                    canonical_json_bytes(recipe["financial_epoch_manifest_proof"])
                ).hexdigest()
            }
            if recipe.get("financial_epoch_manifest_proof") is not None
            else {}
        ),
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
    payload = _authority_payload(recipe, source)
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


_CAPITAL_SYSTEMATIC: Final = "SYSTEMATIC"
_CAPITAL_RETAINED_EXCLUDED: Final = "RETAINED_EXCLUDED"
_CAPITAL_AVAILABLE: Final = "AVAILABLE"
_CAPITAL_NOT_READY: Final = "NOT_READY_AT_CUTOFF"
_CAPITAL_RESERVE_POOL: Final = "reserve-capital"
# Alphabetical, so every list built in this order is already sorted.
_CAPITAL_COMPONENTS: Final = (
    "reserve",
    "retained_excluded",
    "systematic",
    "total_liquid",
)
_CAPITAL_EXACT_PRECISION: Final = 100
_CAPITAL_WEIGHT_TOLERANCE: Final = Decimal("1e-28")
_CURRENT_CAPITAL_DIFFERS: Final = "baseline current capital differs"


def _capital_day(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(_text(value, "capital date"))
    except ValueError as exc:
        raise MaterializationError(_CURRENT_CAPITAL_DIFFERS) from exc


def _capital_amount(value: object) -> Decimal | None:
    """A published USD amount: ``None`` when UNAVAILABLE, else its exact value."""

    if not isinstance(value, dict) or value.get("currency") != "USD":
        raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
    if value.get("status") == "UNAVAILABLE" and value.get("value") is None:
        return None
    if value.get("status") != _CAPITAL_AVAILABLE:
        raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
    return _decimal(value.get("value"), "current capital amount")


def _capital_sum(values: Sequence[Decimal]) -> Decimal:
    """Add money exactly: the default 28-digit context would round long sums."""

    with localcontext() as context:
        context.prec = _CAPITAL_EXACT_PRECISION
        return sum(values, Decimal(0))


def _capital_total(
    parts: Sequence[tuple[Decimal | None, date | None]],
) -> tuple[Decimal | None, date | None]:
    """Exact sum and oldest date of settled parts; nothing while any part is open."""

    values = [value for value, _ in parts if value is not None]
    if len(values) != len(parts):
        return None, None
    days = [day for _, day in parts if day is not None]
    return _capital_sum(values), (min(days) if days else None)


def _capital_in_effect(
    valuation: date, start: date | None, end: date | None, *, open_start: bool = False
) -> bool:
    """Whether a window contains the valuation date (a missing start is open only
    when ``open_start``: a MAPPED assignment must name its start)."""

    started = start <= valuation if start is not None else open_start
    return started and (end is None or valuation <= end)


def _derived_cash_parts(
    block: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    valuation: date,
    problems: list[str],
) -> dict[str, list[tuple[Decimal, date]]]:
    """Each strategy's derived custody cash lines as ``(value, as_of)`` parts.

    Since OWNER_318 the producer (``portfolio_baseline._build_current_capital``)
    publishes a strategy's value as its member NAVs plus its ``derived_cash`` lines,
    dated by the oldest of them.  The lines are read as claims too: each must be an
    AVAILABLE USD amount dated on or before the cutoff, equal to the exact sum of its
    own signed events, none dated after the line; and lines exist only under the
    block's selected ``custody_authority``, as the producer's validator requires.
    """

    parts: dict[str, list[tuple[Decimal, date]]] = {}
    for row in rows:
        lines = row.get("derived_cash", [])
        if not isinstance(lines, list) or not all(
            isinstance(line, dict) for line in lines
        ):
            raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
        if not lines:
            continue
        strategy = str(row.get("strategy_id"))
        for line in lines:
            value = _capital_amount(line.get("current_value"))
            day = _capital_day(line.get("as_of_date"))
            events = line.get("events")
            if (
                value is None
                or day is None
                or day > valuation
                or line.get("status") != _CAPITAL_AVAILABLE
                or not isinstance(events, list)
                or not events
                or not all(isinstance(event, dict) for event in events)
            ):
                raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
            signed = [
                _decimal(event.get("signed_usd"), "derived cash event")
                for event in events
            ]
            event_days = [_capital_day(event.get("event_date")) for event in events]
            if _capital_sum(signed) != value or any(
                event_day is None or event_day > day for event_day in event_days
            ):
                problems.append(f"derived cash for {strategy} differs from its events")
            parts.setdefault(strategy, []).append((value, day))
    if parts and not block.get("custody_authority"):
        problems.append("derived cash lacks the selected custody authority")
    return parts


def _current_capital_identity(
    baseline: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Verify the baseline's current capital block against its own NAV facts.

    The block is read as a claim.  Its rows must be exactly the strategies the
    baseline's own registry names (``strategies``).  Every strategy value, every
    aggregate and the weights are then recomputed here from ``nav_observations``
    x ``assignments``, plus each strategy's OWNER_318 derived custody cash lines
    (see ``_derived_cash_parts``), and the reserve's AUTHORITATIVE NAV admissions,
    without calling the baseline builder.  A lie fails; a component that is honestly
    NOT_READY is reported as such in the evidence and never counted as PASS.
    """

    block = baseline.get("current_capital")
    nav = baseline.get("nav_observations")
    assignments = baseline.get("assignments")
    admissions = baseline.get("provider_admissions", [])
    registry = baseline.get("strategies")
    rows = block.get("strategies") if isinstance(block, dict) else None
    if not (
        isinstance(block, dict)
        and isinstance(nav, list)
        and isinstance(assignments, list)
        and isinstance(admissions, list)
        and isinstance(registry, list)
        and all(isinstance(item, dict) for item in registry)
        and isinstance(rows, list)
        and all(isinstance(row, dict) for row in rows)
    ):
        raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
    valuation = _capital_day(baseline.get("as_of_date"))
    if valuation is None:
        raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
    problems: list[str] = []
    if block.get("valuation_date") != valuation.isoformat():
        problems.append("valuation_date")

    # The universe of rows is the baseline's own registry, never the rows the block
    # happens to publish: the producer publishes one row for every registered
    # strategy (required or retained-excluded), so the two id sets must be equal.
    # A universe read from the published rows alone would let a corrupted baseline
    # drop a strategy with its assignments, lower the systematic total, recompute
    # the weights and still reconcile.  This is settled before anything is
    # recomputed: a block over the wrong universe has nothing to recompute.
    registered = {
        _text(item.get("strategy_id"), "registry strategy_id") for item in registry
    }
    published = {str(row.get("strategy_id")) for row in rows}
    if registered - published:
        problems.append(
            "missing strategy rows: " + ", ".join(sorted(registered - published))
        )
    if published - registered:
        problems.append(
            "unregistered strategy rows: " + ", ".join(sorted(published - registered))
        )
    if registered != published:
        return False, {
            "component_mismatches": [],
            "derived_cash_usd": {},
            "identity_status": "FAIL",
            "not_ready_components": [],
            "problems": problems,
            "recomputed_strategy_count": 0,
            "strategy_mismatches": [],
            "systematic_total_usd": None,
            "total_liquid_usd": None,
            "valuation_date": block.get("valuation_date"),
            "weight_sum": None,
        }

    # Facts: each account's latest AVAILABLE USD NAV on or before the valuation
    # date, and the same restricted to AUTHORITATIVE account-live admission windows.
    windows_by_account: dict[str, list[tuple[date, date]]] = {}
    for admission in admissions:
        if (
            isinstance(admission, dict)
            and admission.get("fact_type") == "nav_observations"
            and admission.get("disposition") == "AUTHORITATIVE"
            and admission.get("evidence_layer") == "ACCOUNT_LIVE"
            and admission.get("account_id") is not None
            and admission.get("pool_id") is None
            and admission.get("strategy_id") is None
        ):
            start = _capital_day(admission.get("effective_start"))
            end = _capital_day(admission.get("effective_end"))
            if start is None or end is None:
                raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
            windows_by_account.setdefault(str(admission["account_id"]), []).append(
                (start, end)
            )
    latest: dict[str, tuple[date, Decimal]] = {}
    admitted_latest: dict[str, tuple[date, Decimal]] = {}
    for fact in nav:
        amount = fact.get("nav") if isinstance(fact, dict) else None
        if (
            not isinstance(amount, dict)
            or amount.get("status") != _CAPITAL_AVAILABLE
            or amount.get("currency") != "USD"
        ):
            continue
        day = _capital_day(fact.get("as_of_date"))
        account = _text(fact.get("account_id"), "NAV account")
        if day is None or day > valuation:
            continue
        point = (day, _decimal(amount.get("value"), "NAV value"))
        if account not in latest or day > latest[account][0]:
            latest[account] = point
        admitted = any(
            low <= day <= high for low, high in windows_by_account.get(account, ())
        )
        if admitted and (
            account not in admitted_latest or day > admitted_latest[account][0]
        ):
            admitted_latest[account] = point

    # Membership: MAPPED assignments, current ones for required strategies.
    scopes = {str(row.get("strategy_id")): str(row.get("scope")) for row in rows}
    if len(scopes) != len(rows):
        problems.append("duplicate strategy rows")
    windows: list[tuple[str, str, date | None, date | None]] = []
    for item in assignments:
        if not isinstance(item, dict) or item.get("mapping_status") != "MAPPED":
            continue
        windows.append(
            (
                _text(item.get("strategy_id"), "assignment strategy"),
                _text(item.get("account_id"), "assignment account"),
                _capital_day(item.get("effective_start")),
                _capital_day(item.get("effective_end")),
            )
        )
    owner: dict[str, str] = {}
    for strategy, account, start, end in windows:
        if strategy not in scopes:
            problems.append(f"unpublished strategy {strategy}")
        elif _capital_in_effect(valuation, start, end):
            if owner.setdefault(account, strategy) != strategy:
                problems.append(f"account {account} has two current strategies")
    members: dict[str, set[str]] = {strategy: set() for strategy in scopes}
    for strategy, account, start, end in windows:
        scope = scopes.get(strategy)
        if scope == _CAPITAL_SYSTEMATIC and _capital_in_effect(valuation, start, end):
            members[strategy].add(account)
        elif (
            scope == _CAPITAL_RETAINED_EXCLUDED
            and owner.get(account, strategy) == strategy
        ):
            members[strategy].add(account)

    cash = _derived_cash_parts(block, rows, valuation, problems)
    expected: dict[str, tuple[Decimal | None, date | None]] = {}
    for strategy, accounts in members.items():
        if not accounts or any(account not in latest for account in accounts):
            expected[strategy] = (None, None)
        else:
            # NAV + derived custody cash, as the producer publishes it (OWNER_318).
            expected[strategy] = _capital_total(
                [(latest[account][1], latest[account][0]) for account in accounts]
                + cash.get(strategy, [])
            )
    mismatched: list[str] = []
    for row in rows:
        strategy = str(row.get("strategy_id"))
        value = _capital_amount(row.get("current_value"))
        day = _capital_day(row.get("as_of_date"))
        published_members = row.get("members")
        published_accounts = sorted(
            str(member.get("account_id"))
            for member in (
                published_members if isinstance(published_members, list) else []
            )
            if isinstance(member, dict)
        )
        want, want_day = expected[strategy]
        if not (
            row.get("status")
            == (_CAPITAL_AVAILABLE if value is not None else _CAPITAL_NOT_READY)
            and value == want
            and day == want_day
            and published_accounts == sorted(members[strategy])
        ):
            mismatched.append(strategy)

    # Aggregates and the capital identity, recomputed from the settled members.
    required = [
        expected[name] for name in sorted(scopes) if scopes[name] == _CAPITAL_SYSTEMATIC
    ]
    excluded = [
        expected[name]
        for name in sorted(scopes)
        if scopes[name] == _CAPITAL_RETAINED_EXCLUDED
    ]
    systematic = _capital_total(required) if required else (None, None)
    if systematic[0] is not None and systematic[0] <= 0:
        systematic = (None, None)
    reserve_accounts: set[str] = set()
    for item in assignments:
        if isinstance(item, dict) and item.get("pool_id") == _CAPITAL_RESERVE_POOL:
            if _capital_in_effect(
                valuation,
                _capital_day(item.get("effective_start")),
                _capital_day(item.get("effective_end")),
                open_start=True,
            ):
                reserve_accounts.add(_text(item.get("account_id"), "reserve account"))
    reserve: tuple[Decimal | None, date | None] = (None, None)
    if reserve_accounts and all(
        account in admitted_latest for account in reserve_accounts
    ):
        reserve = _capital_total(
            [
                (admitted_latest[account][1], admitted_latest[account][0])
                for account in sorted(reserve_accounts)
            ]
        )
    aggregates = {
        "reserve": reserve,
        "retained_excluded": _capital_total(excluded),
        "systematic": systematic,
    }
    aggregates["total_liquid"] = _capital_total(
        [aggregates[name] for name in ("reserve", "retained_excluded", "systematic")]
    )
    component_mismatches: list[str] = []
    statuses: dict[str, str] = {}
    for name in _CAPITAL_COMPONENTS:
        part = block.get(name)
        if not isinstance(part, dict):
            raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
        value = _capital_amount(part.get("current_value"))
        day = _capital_day(part.get("as_of_date"))
        want, want_day = aggregates[name]
        statuses[name] = str(part.get("status"))
        if not (
            part.get("status")
            == (_CAPITAL_AVAILABLE if value is not None else _CAPITAL_NOT_READY)
            and value == want
            and day == want_day
        ):
            component_mismatches.append(name)

    # Shares of the systematic total, closing to one; none while it is not current.
    weight_sum: Decimal | None = None
    weights_ok = True
    required_rows = [
        row for row in rows if scopes[str(row["strategy_id"])] == _CAPITAL_SYSTEMATIC
    ]
    total = systematic[0]
    if total is not None:
        shares: list[Decimal] = []
        for row in required_rows:
            if row.get("current_weight") is None:
                weights_ok = False
                continue
            share = _decimal(row.get("current_weight"), "current weight")
            shares.append(share)
            wanted = expected[str(row["strategy_id"])][0]
            with localcontext() as context:
                context.prec = _CAPITAL_EXACT_PRECISION
                if wanted is None or abs(share - wanted / total) > (
                    _CAPITAL_WEIGHT_TOLERANCE
                ):
                    weights_ok = False
        weight_sum = _capital_sum(shares)
        if abs(weight_sum - Decimal(1)) > WEIGHT_SUM_TOLERANCE:
            weights_ok = False
    else:
        weights_ok = all(row.get("current_weight") is None for row in required_rows)
    if any(
        row.get("current_weight") is not None
        for row in rows
        if scopes[str(row["strategy_id"])] != _CAPITAL_SYSTEMATIC
    ):
        weights_ok = False

    not_ready = [
        name for name in _CAPITAL_COMPONENTS if statuses[name] != _CAPITAL_AVAILABLE
    ]
    failed = bool(problems or mismatched or component_mismatches or not weights_ok)
    identity_status = "FAIL" if failed else "NOT_READY" if not_ready else "PASS"
    total_liquid = aggregates["total_liquid"][0]
    return identity_status != "FAIL", {
        "component_mismatches": component_mismatches,
        "derived_cash_usd": {
            strategy: str(_capital_sum([value for value, _ in parts]))
            for strategy, parts in sorted(cash.items())
        },
        "identity_status": identity_status,
        "not_ready_components": not_ready,
        "problems": problems,
        "recomputed_strategy_count": len(rows),
        "strategy_mismatches": mismatched,
        "systematic_total_usd": str(total) if total is not None else None,
        "total_liquid_usd": str(total_liquid) if total_liquid is not None else None,
        "valuation_date": block.get("valuation_date"),
        "weight_sum": str(weight_sum) if weight_sum is not None else None,
    }


_DATA_DATED_RELEASE_ROLES: Final = frozenset({"ledger", "baseline", "report"})


def _release_evaluation_date(recipe: Mapping[str, Any]) -> str:
    """Return the date this release is evaluated on, not the financial cutoff it carries.

    The two are different clocks and the owner contract keeps them apart: the cutoff is an
    explicit owner selection, while the evaluation date is when the release is cut.  Only
    the schema-10 vertical records it, so an older single-clock vertical falls back to the
    cutoff -- the same shape `portfolio_release_builder.py:2031` already uses.  Reading it
    from the vertical rather than the pipeline recipe keeps every recipe hash unchanged.

    A recipe with no `vertical_recipe` at all never passed `_recipe`, which requires the key,
    so that shape reaches here only from a unit test exercising something else.  It falls back
    to the cutoff rather than raising, because refusing would make this helper the reason such
    a test fails instead of the behaviour it is actually checking.
    """

    definition = recipe.get("vertical_recipe")
    if definition is None:
        return _text(recipe["as_of_date"], "as_of_date")
    vertical = _load(
        _validate_file_definition(definition, "vertical recipe"), "vertical recipe"
    )
    evaluated_on = vertical.get("evaluated_on", recipe["as_of_date"])
    return _text(evaluated_on, "evaluated_on")


def _generation_as_of_allowed(
    role: str,
    generation_as_of: date,
    release_as_of: date,
    cutoff: date | None = None,
) -> bool:
    """Allow data-dated roles to lag the release; keep the panel exact-date.

    The ledger is dated by the market data it carries, which legitimately lags
    the evaluation date -- the release-QC planning date is pinned to the public
    factor capture date, and the plan records `complete_through` and
    `evaluated_on` as separate fields with an age threshold between them.

    The baseline and the report are computed from that same ledger and inherit
    its data date, so holding them to the exact release date while exempting
    their own source was inconsistent: with the market data three days behind
    the capture date, no release could satisfy both rules at once.  Freshness is
    still asserted -- `_freshness_*` compares every assessment row's
    `evaluated_on` against the release date, and the plan's own age threshold
    still applies -- so the boundary is enforced where the data age is actually
    measured rather than duplicated here.

    The panel manifest has one date field and downstream readers take it as the data
    date, so it is dated at the financial cutoff exactly -- never at the later date the
    release is cut, which the freshness artifact carries as its own ``evaluated_on``.
    Without a cutoff the panel is held to the release date, as before.
    """

    if role in _DATA_DATED_RELEASE_ROLES:
        return generation_as_of <= release_as_of
    return generation_as_of == (release_as_of if cutoff is None else cutoff)


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
    *,
    epoch_manifest_proof: Mapping[str, Any] | None = None,
    financial_outcomes: Mapping[str, Any] | None = None,
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
    if financial_outcomes is not None:
        if financial_outcomes.get("status") != "PASS":
            raise MaterializationError(
                "financial outcomes must pass actual verification"
            )
        checks["financial_outcomes"] = "PASS"
    manifest_inputs = recipe.get("financial_epoch_manifest_inputs")
    if manifest_inputs is not None:
        if epoch_manifest_proof is None:
            raise MaterializationError(
                "post-build epoch-manifest proof is required for semantic PASS"
            )
        accepted_proof = recipe.get("financial_epoch_manifest_proof")
        if not isinstance(accepted_proof, dict):
            raise MaterializationError("accepted epoch-manifest proof differs")
        if canonical_json_bytes(epoch_manifest_proof) != canonical_json_bytes(
            accepted_proof
        ):
            raise MaterializationError("post-build epoch-manifest proof differs")
        checks["epoch_manifest_input_authority"] = "PASS"
    proof_sha256 = (
        hashlib.sha256(canonical_json_bytes(epoch_manifest_proof)).hexdigest()
        if epoch_manifest_proof is not None
        else None
    )
    if financial_outcomes is not None:
        from portfolio_financial_outcomes import financial_outcomes_semantic_identity

    return {
        "as_of_date": recipe["as_of_date"],
        # The generation and freshness checks are decided by this date, so a receipt carrying
        # only the cutoff cannot be audited back to the comparison it made.
        "evaluated_on": _release_evaluation_date(recipe),
        "checks": checks,
        **(
            {
                "financial_outcomes_sha256": financial_outcomes_semantic_identity(
                    financial_outcomes
                )
            }
            if financial_outcomes is not None
            else {}
        ),
        # Both epoch keys appear only with the raw-five inputs they describe.
        # This one used to be emitted unconditionally as null, so a schema-10
        # vertical -- which has no epoch-manifest input gate -- produced a
        # semantic gate carrying a key the reader does not allow, and every
        # run failed its last node with "post-build semantic gate keys differ
        # from the contract".
        **(
            {
                "financial_epoch_manifest_inputs_sha256": hashlib.sha256(
                    canonical_json_bytes(manifest_inputs)
                ).hexdigest(),
                "financial_epoch_manifest_proof_sha256": proof_sha256,
            }
            if manifest_inputs is not None
            else {}
        ),
        "release_commit": recipe["release_commit"],
        "release_generations": (
            dict(release_generations)
            if release_generations is not None
            else {
                ROOT_IDS[role]: recipe["expected_generations"][role]["generation_id"]
                for role in RELEASE_ROLES
            }
        ),
        "schema_version": (
            3
            if manifest_inputs is not None
            else 2
            if recipe.get("schema_version") == 3
            else 1
        ),
        "unwaived_blocking_exceptions": 0,
        "verdict": "PASS",
    }


# The audit snapshot is sealed (see ``portfolio_baseline._snapshot_sum``): the baseline
# replays it, and this check compares with that replay, adding sequentially at 28 digits.
_SNAPSHOT_PRECISION: Final = 28


def _snapshot_sum(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = _SNAPSHOT_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return sum(values, Decimal(0))


def _financial_snapshot_sums(
    allocations: Sequence[object], systematic: Decimal, reserve: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """The audit snapshot's allocation sum, weight sum and systematic plus reserve.

    The snapshot is a sealed, retained artifact: its sums are compared for equality
    with the figures the baseline replayed from it, so they keep the historical
    arithmetic (28 digits, in row order) and never the exact arithmetic that current
    capital uses.
    """

    values = [
        _decimal(row["current_value"]["value"], "allocation value")
        for row in allocations
        if isinstance(row, dict)
        and row.get("pool_id") == "systematic-capital"
        and isinstance(row.get("current_value"), dict)
    ]
    weights = [
        _decimal(row.get("systematic_weight"), "systematic weight")
        for row in allocations
        if isinstance(row, dict) and row.get("pool_id") == "systematic-capital"
    ]
    return (
        _snapshot_sum(values),
        _snapshot_sum(weights),
        _snapshot_sum([systematic, reserve]),
    )


def semantic(
    recipe_path: Path, output_path: Path, financial_spec_path: Path | None = None
) -> dict[str, Any]:
    recipe = _recipe(recipe_path)
    output_root = output_path.parent.parent
    checks: dict[str, dict[str, Any]] = {}
    # `_generation_as_of_allowed` reasons about the date the release is CUT for the
    # data-dated roles, which "legitimately lag the evaluation date": handing them the
    # financial cutoff conflated the two clocks, so a ledger whose provenance is one day
    # past the cutoff (which `provider_ledger._generation_as_of_date` produces by design)
    # failed.  The panel manifest is the one date readers take as the DATA date, so it is
    # held to the financial cutoff exactly while the execution date is carried by the
    # freshness artifact (`evaluated_on`) and this receipt.
    evaluated_on = _release_evaluation_date(recipe)
    release_as_of = date.fromisoformat(evaluated_on)
    financial_cutoff = date.fromisoformat(_text(recipe["as_of_date"], "as_of_date"))
    if recipe.get("schema_version") == 3:
        conformance = recipe["product_authority"]["conformance"]
        _result(
            checks,
            "product_conformance",
            True,
            readiness=conformance["readiness"],
            sha256=hashlib.sha256(canonical_json_bytes(conformance)).hexdigest(),
        )
    manifest_inputs = recipe.get("financial_epoch_manifest_inputs")
    epoch_manifest_proof: dict[str, Any] | None = None
    if manifest_inputs is not None:
        epoch_manifest_proof = _financial_epoch_manifest_proof(recipe, output_root)
        if epoch_manifest_proof is None:  # pragma: no cover - guarded above
            raise MaterializationError("post-build epoch-manifest proof is unavailable")
        if canonical_json_bytes(epoch_manifest_proof) != canonical_json_bytes(
            recipe["financial_epoch_manifest_proof"]
        ):
            raise MaterializationError("post-build epoch-manifest proof differs")
        _result(
            checks,
            "epoch_manifest_input_authority",
            True,
            proof_sha256=hashlib.sha256(
                canonical_json_bytes(epoch_manifest_proof)
            ).hexdigest(),
        )
    verified_epoch_proof = _verified_epoch_manifest_materialization_proof(
        recipe, output_root
    )
    if verified_epoch_proof is not None:
        _result(
            checks,
            "verified_epoch_manifest_bundle_authority",
            True,
            proof_sha256=hashlib.sha256(
                canonical_json_bytes(verified_epoch_proof)
            ).hexdigest(),
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
                and _generation_as_of_allowed(
                    role, generation_as_of, release_as_of, financial_cutoff
                )
            )
            generation_rows[role] = {
                "actual_generation_id": actual["generation_id"],
                "as_of_rule": (
                    "ON_OR_BEFORE_RELEASE"
                    if role in _DATA_DATED_RELEASE_ROLES
                    else "EXACT_FINANCIAL_CUTOFF"
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
    audit_snapshot = baseline.get("audit_snapshot")
    if not isinstance(replay, dict) or not isinstance(allocations, list):
        raise MaterializationError("baseline financial facts differ")
    if not isinstance(audit_snapshot, dict) or not isinstance(
        baseline.get("current_capital"), dict
    ):
        raise MaterializationError(_CURRENT_CAPITAL_DIFFERS)
    systematic = _decimal(replay.get("systematic_total_usd"), "systematic total")
    reserve = _decimal(replay.get("reserve_total_usd"), "reserve total")
    total = _decimal(replay.get("total_liquid_usd"), "total liquid")
    systematic_allocations, weights, expected_total = _financial_snapshot_sums(
        allocations, systematic, reserve
    )
    # The dated audit snapshot keeps its identity (its own table against its own
    # replay) but is only ever a labeled historical reference; the current block
    # is verified separately, from the NAV facts, and never against that table.
    snapshot_ok = (
        total == expected_total
        and systematic_allocations == systematic
        and abs(weights - Decimal(1)) <= WEIGHT_SUM_TOLERANCE
        and audit_snapshot.get("status") == "HISTORICAL_REFERENCE"
        and audit_snapshot.get("replay") == replay
    )
    current_ok, current_evidence = _current_capital_identity(baseline)
    _result(
        checks,
        "financial_identities",
        snapshot_ok and current_ok,
        audit_snapshot={
            "as_of_date": audit_snapshot.get("as_of_date"),
            "reserve_total_usd": str(reserve),
            "status": audit_snapshot.get("status"),
            "systematic_allocation_sum_usd": str(systematic_allocations),
            "systematic_total_usd": str(systematic),
            "systematic_weight_sum": str(weights),
            "total_liquid_usd": str(total),
        },
        current_capital=current_evidence,
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
    # The perimeter is whatever the accepted product requires, not a constant.
    # This was pinned at 9 by 24101f4, when the product had nine REQUIRED
    # strategies; it now has seven, so the literal rejected every correct
    # release.  Derive it from the same conformance block the HWM assessment
    # uses for required_strategy_ids.
    # Schema-2 recovery recipes carry no conformance block; those keep the
    # report-versus-panel equality without an absolute count.
    authority_block = recipe.get("product_authority")
    conformance_block = (
        authority_block.get("conformance")
        if isinstance(authority_block, Mapping)
        else None
    )
    conformance_strategies = (
        conformance_block.get("strategies")
        if isinstance(conformance_block, Mapping)
        else None
    )
    required_strategy_count = (
        sum(
            1
            for row in conformance_strategies
            if isinstance(row, Mapping) and row.get("product_requirement") == "REQUIRED"
        )
        if isinstance(conformance_strategies, list)
        else None
    )
    # A retained history-only strategy is a record the panel carries for history; it is not
    # a product strategy, so the report/panel strategy equality and the required count are
    # taken over the product strategies (one shared rule, lifecycle_fields).
    panel_strategies = product_strategy_rows(panel.get("strategies", []))
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
        and len(report.get("strategy_summary", [])) == len(panel_strategies)
        and (
            required_strategy_count is None
            or len(panel_strategies) == required_strategy_count
        )
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
        panel_strategy_count=len(panel_strategies),
        panel_retained_history_only_count=len(panel.get("strategies", []))
        - len(panel_strategies),
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
    # Schema 4 judges every assessment at the financial cutoff and carries the
    # execution date beside it; earlier schemas judged at the execution date.
    if freshness.get("assessment_schema_version") == 4:
        data_clock_ok = (
            freshness.get("as_of_date") == recipe["as_of_date"]
            and freshness.get("evaluated_on") == evaluated_on
        )
        row_clock = freshness.get("as_of_date")
    else:
        data_clock_ok = freshness.get("as_of_date") == evaluated_on
        row_clock = evaluated_on
    evaluation_ok = assessment_shape_ok and all(
        row.get("evaluated_on") == row_clock for row in assessment_rows
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
        data_clock_ok
        and evaluation_ok
        and blockers_disclosed
        and rendered_disclosures_ok
    )
    _result(
        checks,
        "freshness",
        freshness_ok,
        as_of_date=freshness.get("as_of_date"),
        evaluated_on=freshness.get("evaluated_on"),
        blocking_assessment_count=len(blocking),
        blockers_explicitly_disclosed=blockers_disclosed,
        blockers_rendered_in_panel=rendered_disclosures_ok,
        sha256=_sha(freshness_path)[0],
    )

    panel_qa = _load(panel_generation / "qa" / "qa-validation.json", "panel QA")
    interaction_qa = _load(
        panel_generation / "qa" / "interaction-results.json", "interaction QA"
    )
    # Workbook generation is on permanent owner hold and the runtime was removed in
    # `47120f1`, so the report generation no longer carries `workbook-validation.json` or
    # `xlsx-win-result.json`. Requiring them here made the gate demand an artifact the
    # product is forbidden to produce -- `docs/controller-graph.md:74`: the workbook
    # "cannot become a required edge". Both terms proved workbook properties only, and the
    # value they fed, `release_evidence.native_excel_ok`, had no reader anywhere.
    evidence_ok = (
        panel_qa.get("checks_passed") == panel_qa.get("checks_total")
        and interaction_qa.get("all_passed") is True
        and _sha(output_root / "stages" / "panel" / "index.html")[0]
        == recipe["panel_launcher_sha256"]
    )
    generation_check = checks["generation_invariants"]
    generation_check["evidence"]["release_evidence"] = {
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
        "evaluated_on": evaluated_on,
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
    financial_proof = None
    if financial_spec_path is not None:
        from quant_engine.core.pipeline import PipelineSpec
        from portfolio_financial_outcomes import validate_financial_outcomes

        financial_spec = PipelineSpec.from_json_bytes(financial_spec_path.read_bytes())
        financial_proof = validate_financial_outcomes(
            financial_spec, caller="materialize.semantic"
        )
    _write_exact(
        output_path,
        canonical_json_bytes(
            _semantic_gate_payload(
                recipe,
                epoch_manifest_proof=epoch_manifest_proof,
                financial_outcomes=financial_proof,
            )
        ),
    )
    return {"details": str(details_path), "state": "PASS"}


# `timeout_seconds` bounds a node by time without progress. The analytics stage
# reports once per decomposition lens, and `portfolio_release_builder` allows a
# single lens 7200s, so one lens solve is the longest silence that stage can
# legitimately produce. A smaller budget would kill a lens the layer below it
# still considers healthy.
ANALYTICS_SILENCE_SECONDS = 7200


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


def _release_identity(repo: Path, claimed: object) -> tuple[str, str]:
    """Return (identity value, scheme) when ``claimed`` names the live repository.

    A claimed commit SHA binds under the legacy git-commit-v1 scheme exactly as
    before; a claimed ``HEAD^{tree}`` binds under git-tree-v2 (RELEASE-HASHTIME-001
    cut 5). Anything else is refused, so a different tree can never pass.
    """
    head = _git(repo, "rev-parse", "HEAD")
    if claimed == head:
        return head, COMMIT_SCHEME
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    if claimed == tree and len(tree) == 40:
        return tree, TREE_SCHEME
    raise MaterializationError("repository must be clean at the recipe commit")


def _spec_identity_fields(scheme: str) -> dict[str, str]:
    return {} if scheme == COMMIT_SCHEME else {"code_identity_scheme": scheme}


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


def build_spec(
    recipe_path: Path,
    output_root: Path,
    spec_path: Path,
    hwm_governed_result_path: Path | None = None,
    hwm_governed_input_generation_path: Path | None = None,
    hwm_governed_calendar_authority_path: Path | None = None,
    hwm_governed_admission_receipt_path: Path | None = None,
    financial_input_contract_path: Path | None = None,
    portfolio_root: Path | None = None,
    invested_capital_result_path: Path | None = None,
    invested_capital_input_generation_path: Path | None = None,
    retained_cache_root: Path | None = None,
    workbook_runtime_path: Path | None = None,
) -> dict[str, Any]:
    if portfolio_root is None:
        # Fail here rather than four nodes into the run. `build_workbook` and
        # `build_panel` refuse a financial publication without this root, and
        # every schema-3 vertical is financial, so an omitted root is always a
        # blocked execute with an opaque node failure.
        raise MaterializationError(
            "financial pipeline specification requires a portfolio root"
        )
    recipe = _recipe(recipe_path)
    if recipe.get("schema_version") != 3:
        raise MaterializationError(
            "schema-2 recipe is recovery-only and cannot create a current specification"
        )
    _validate_output_path_budget(output_root.resolve(), recipe)
    retained_cache_root = (retained_cache_root or output_root / "state").resolve()
    repo = Path(__file__).resolve().parents[1]
    release_commit, identity_scheme = _release_identity(repo, recipe["release_commit"])
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
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
    manifest_inputs = recipe.get("financial_epoch_manifest_inputs")
    manifest_input_paths: dict[str, Path] = {}
    if manifest_inputs is not None:
        if not isinstance(manifest_inputs, dict):
            raise MaterializationError("financial epoch manifest inputs differ")
        manifest_input_paths = {
            key: _absolute(
                manifest_inputs[key]["path"], f"financial epoch manifest {key}"
            )
            for key in EPOCH_MANIFEST_INPUTS
        }
    product_authority = recipe["product_authority"]
    epoch_generation = recipe.get("epoch_manifest_generation")
    bound_contract_path: Path | None = None
    if epoch_generation is not None:
        contract_definition = product_authority.get("contract")
        if not isinstance(contract_definition, dict):
            raise MaterializationError("financial MVP contract authority differs")
        financial_contract = _load(
            _absolute(contract_definition["path"], "financial MVP contract"),
            "financial MVP contract",
        )
        bound_financial_contract = _bind_epoch_manifest_bundle(
            Path(epoch_generation["runtime_root"]),
            epoch_generation["generation_id"],
            financial_contract,
        )
        bound_contract_path = output_root / "financial-mvp-bound-contract.json"
        bound_contract_bytes = canonical_json_bytes(bound_financial_contract)
        _write_exact(bound_contract_path, bound_contract_bytes)
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
    authority_bytes = canonical_json_bytes(_authority_payload(recipe, source_payload))
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
            retained_cache_root,
        ],
        "report": [
            output_root / "stages" / "report",
            output_root / "stage-results" / "report.json",
        ],
        "panel": [
            output_root / "stages" / "panel",
            output_root / "stage-results" / "panel.json",
        ],
    }
    governed_stage_paths = _governed_stage_paths(
        hwm_governed_result_path,
        hwm_governed_input_generation_path,
        hwm_governed_calendar_authority_path,
        hwm_governed_admission_receipt_path,
        invested_capital_result_path,
        invested_capital_input_generation_path,
    )
    governed_stage_args = [
        value
        for flag, path in governed_stage_paths.items()
        for value in (flag, str(path))
    ]
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
        for key, flag in EPOCH_MANIFEST_INPUTS.items():
            if key in manifest_input_paths:
                args.extend((flag, str(manifest_input_paths[key])))
        if role == "panel":
            args.extend(("--qa-dir", str(qa_dir)))
        if role == "analytics":
            args.extend(("--retained-cache-root", str(retained_cache_root)))
        # `build_report` and `build_panel` treat any run carrying governed
        # HWM, invested capital or decomposition lenses as a financial
        # publication and refuse it without a portfolio root, which is what
        # proves the stage output root is disjoint from production. Every
        # financial vertical has lenses, so both roles always need it.
        needs_portfolio_root = role in _PORTFOLIO_ROOT_STAGES or (
            bool(governed_stage_paths) and role in _GOVERNED_OUTCOME_STAGES
        )
        if needs_portfolio_root and portfolio_root is not None:
            args.extend(("--portfolio-root", str(portfolio_root)))
        # The governed numerical results are stage inputs, not merely gate
        # evidence: `build_report` and `build_panel` each read
        # them and each refuses to verify a candidate that was built with them
        # when it is re-run without. Omitting them here made the pipeline's own
        # panel node recompute a semantic validation that differed from the one
        # the proof wrote into the shared QA directory.
        if role in _GOVERNED_OUTCOME_STAGES:
            args.extend(governed_stage_args)
        reads = [*previous_outputs, *source_paths]
        if role in _GOVERNED_OUTCOME_STAGES:
            reads.extend(governed_stage_paths.values())
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
            retry_limit=0,
            timeout_seconds=(
                ANALYTICS_SILENCE_SECONDS if role == "analytics" else 3600
            ),
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
                *([bound_contract_path] if bound_contract_path is not None else []),
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
    manifest_inputs = recipe.get("financial_epoch_manifest_inputs")
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
        "schema_version": (
            3
            if manifest_inputs is not None
            # A reused verified epoch-manifest generation is its own
            # profile: it carries one generation identity where schema 3
            # carries the five raw role files, and the consumer has to
            # know which of the two it is reading.
            else 4
            if recipe.get("epoch_manifest_generation") is not None
            else 2
            if recipe.get("schema_version") == 3
            else 1
        ),
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
    epoch_generation = recipe.get("epoch_manifest_generation")
    if epoch_generation is not None:
        contract["epoch_manifest_generation"] = _canonical_clone(epoch_generation)
        contract["epoch_manifest_source_sha256"] = hashlib.sha256(
            canonical_json_bytes(epoch_generation["source"])
        ).hexdigest()
        contract["financial_mvp_bound_contract_sha256"] = hashlib.sha256(
            bound_contract_bytes
        ).hexdigest()
    if manifest_inputs is not None:
        contract["financial_epoch_manifest_inputs"] = _canonical_clone(manifest_inputs)
        contract["financial_epoch_manifest_proof_sha256"] = hashlib.sha256(
            canonical_json_bytes(recipe["financial_epoch_manifest_proof"])
        ).hexdigest()
    contract_path = spec_path.with_name("portfolio-orchestration-contract.json")
    contract_bytes = canonical_json_bytes(contract)
    _write_exact(contract_path, contract_bytes)
    product_path = spec_path.with_name("financial-mvp-product-conformance.json")
    # The private proof run stages the governed results but does not assess
    # them: `_hwm_input_authority_assessment` verifies the selected-source
    # replay against generations that are pinned as artifact nodes, and only
    # the production specification carries those. The assessment therefore
    # stays PENDING_GOVERNED_HWM_RESULT here and becomes AVAILABLE in
    # `build_production_spec`, which has both the bindings and the sources.
    product_gates = _product_gate_rows(
        product_authority,
        product_path,
        input_contract_path=financial_input_contract_path,
    )
    manifest_gate_path = spec_path.with_name(
        "financial-epoch-manifest-input-authority.json"
    )
    manifest_gates = _financial_epoch_manifest_gate_rows(
        manifest_inputs,
        recipe.get("financial_epoch_manifest_proof"),
        manifest_gate_path,
    )
    semantic_bytes = canonical_json_bytes(
        _semantic_gate_payload(
            recipe,
            epoch_manifest_proof=recipe.get("financial_epoch_manifest_proof"),
        )
    )
    launcher = output_root / "stages" / "panel" / "index.html"
    spec = {
        **_spec_identity_fields(identity_scheme),
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
            *(
                [
                    {
                        "gate_id": "financial-mvp-bound-contract",
                        "path": str(bound_contract_path),
                        "sha256": hashlib.sha256(bound_contract_bytes).hexdigest(),
                    }
                ]
                if epoch_generation is not None
                else []
            ),
            *product_gates,
            *manifest_gates,
        ],
        "nodes": nodes,
        "pipeline_id": recipe["pipeline_id"],
        "roots": {
            ROOT_IDS[role]: str(output_root / "release-roots" / ROOT_IDS[role])
            for role in RELEASE_ROLES
        },
        "schema_version": 1,
        "state_root": str(retained_cache_root),
    }
    _write_exact(spec_path, canonical_json_bytes(spec))
    return {"contract": str(contract_path), "nodes": len(nodes), "spec": str(spec_path)}


def _production_lens_nodes(
    prepared_root: Path, contract: Mapping[str, Any]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Bind both current surfaces to immutable generations in the existing roots."""
    analytics = _load(
        prepared_root / "stage-results" / "analytics.json", "prepared analytics"
    )
    bindings = analytics.get("lens_generation_bindings")
    if analytics.get("schema_version") != 2 or not isinstance(bindings, list):
        raise MaterializationError("current lens generation bindings are absent")
    calculation = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "DECOMPOSITION"
    )
    plan = _load(CURRENT_STRATEGY_REGISTRY, "current decomposition plan")
    expected = {
        (mode, surface, source)
        for mode in calculation["evidence_modes"]
        for surface in calculation["surfaces"]
        for source in plan["sources"]
    }
    nodes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            raise MaterializationError("current lens generation binding differs")
        key = (
            _text(binding.get("evidence_mode"), "lens evidence_mode"),
            _text(binding.get("surface"), "lens surface"),
            _text(binding.get("source_key"), "lens source_key"),
        )
        if key not in expected or key in nodes:
            raise MaterializationError("current lens generation key set differs")
        mode, surface, source_key = key
        root_id = f"hybrid-{source_key}" if mode == "LIVE_PLUS_BACKTEST" else source_key
        if root_id not in PRODUCTION_ROOTS:
            raise MaterializationError("current lens production root differs")
        root = (
            prepared_root
            / "stages"
            / "analytics"
            / "decomposition-lenses"
            / mode
            / surface
            / source_key
        ).resolve()
        if _absolute(binding.get("root"), "lens root") != root:
            raise MaterializationError("current lens staging root differs")
        generation_id, source = _current_generation(root)
        if generation_id != binding.get("generation_id"):
            raise MaterializationError("current lens pointer binding differs")
        inventory = inventory_generation(source)
        if inventory["generation_id"] != generation_id:
            raise MaterializationError("current lens generation identity differs")
        target = surface == "STATIC"
        nodes[key] = {
            "depends_on": [],
            "files": inventory["files"],
            "generation_id": generation_id,
            "kind": "GENERATION",
            "manifest_sha256": inventory["manifest_sha256"],
            "node_id": f"publish-{root_id}" + ("" if target else "-dynamic-fls"),
            "release_target": target,
            "root_id": root_id,
            "source_dir": str(source),
        }
    if set(nodes) != expected:
        raise MaterializationError("current lens generation key set differs")
    return nodes


def _verify_panel_lens_nodes(
    panel: Mapping[str, Any],
    nodes: Mapping[tuple[str, str, str], Mapping[str, Any]],
    decomposition_generation_id: str,
) -> None:
    """Require the panel to select every bound surface and evidence mode exactly."""
    lenses = panel.get("decomposition_lenses")
    modes = lenses.get("modes") if isinstance(lenses, dict) else None
    selected: dict[tuple[object, object, object], object] = {}
    if not isinstance(modes, dict):
        raise MaterializationError("prepared panel lens authority differs")
    for mode, groups in modes.items():
        if not isinstance(groups, list):
            raise MaterializationError("prepared panel lens authority differs")
        for group in groups:
            surfaces = group.get("surfaces") if isinstance(group, dict) else None
            if not isinstance(surfaces, dict):
                # The dynamic style path is a lens in its own right rather than
                # a surface split of one, so it carries no `surfaces`. It is not
                # part of the current-surface binding and must not be compared
                # against it -- its generation is the decomposition authority
                # the caller has already verified against `meta`, which is what
                # makes skipping it here safe rather than merely lenient.
                if (
                    isinstance(group, dict)
                    and group.get("generation_id") == decomposition_generation_id
                ):
                    continue
                raise MaterializationError("prepared panel lens authority differs")
            for surface, value in surfaces.items():
                sources = value.get("sources") if isinstance(value, dict) else None
                if not isinstance(sources, list):
                    raise MaterializationError("prepared panel lens authority differs")
                for source in sources:
                    if not isinstance(source, dict):
                        raise MaterializationError(
                            "prepared panel lens authority differs"
                        )
                    key = (mode, surface, source.get("source_key"))
                    if key in selected:
                        raise MaterializationError(
                            "prepared panel lens authority differs"
                        )
                    selected[key] = source.get("generation_id")
    if selected != {key: node["generation_id"] for key, node in nodes.items()}:
        raise MaterializationError("prepared panel lens authority differs")


FINANCIAL_WORK_COUNTER_KEYS: Final = (
    "correlation_build_calls",
    "full_rederivation_count",
    "grid_search_calls",
    "model_fit_calls",
    "receipt_reuse_count",
)


def _financial_work_counters(proof: Mapping[str, Any]) -> dict[str, int]:
    """The five per-invocation counters of one verifier proof, exactly as reported.

    The key set is the one the QC receipt carries. A proof without it is refused here:
    an emitted set of counters that silently lost a key would read as "no work".
    """

    counters = proof.get("work_counters")
    if (
        not isinstance(counters, Mapping)
        or set(counters) != set(FINANCIAL_WORK_COUNTER_KEYS)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counters.values()
        )
    ):
        raise MaterializationError(
            "financial outcomes proof does not carry exactly the five work counters: "
            + ", ".join(FINANCIAL_WORK_COUNTER_KEYS)
        )
    return {key: int(counters[key]) for key in FINANCIAL_WORK_COUNTER_KEYS}


def _prepared_retained_cache_root(prepared_root: Path) -> Path:
    """The retained cache root a prepared attended run recorded in its run receipt."""

    receipt = _load(_attended_run_receipt(prepared_root), "attended run receipt")
    identity = receipt.get("request_identity")
    root = identity.get("retained_cache_root") if isinstance(identity, dict) else None
    if not isinstance(root, str) or not root.strip():
        raise MaterializationError(
            "attended run receipt does not name a retained cache root"
        )
    return Path(root).resolve()


def _provider_root_paths(portfolio_root: Path) -> dict[str, Path]:
    return {
        root_id: portfolio_root.parent / name
        for root_id, name in PROVIDER_ROOTS.items()
    }


def _ledger_provider_bindings(ledger_generation: Path) -> dict[str, dict[str, Any]]:
    """The provider generations the selected provider ledger binds, by provider."""

    for relative in ("provider-ledger.json", "normalized/provider-ledger.json"):
        if (ledger_generation / relative).is_file():
            catalog = _load(ledger_generation / relative, "provider ledger catalog")
            break
    else:
        raise MaterializationError("provider ledger catalog is missing")
    rows = catalog.get("provider_generations")
    if not isinstance(rows, list):
        raise MaterializationError("provider ledger binds no provider generations")
    bound: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("provider_id"), str):
            raise MaterializationError("provider ledger provider binding differs")
        if row["provider_id"] in bound:
            raise MaterializationError("provider ledger binds a provider twice")
        bound[row["provider_id"]] = row
    if set(bound) != set(LEDGER_PROVIDER_ROOTS):
        raise MaterializationError("provider ledger provider set differs")
    return bound


def _catalog_hash_bound(
    generation: Path,
    declared: Sequence[Mapping[str, Any]],
    catalog_sha256: object,
) -> bool:
    """Whether the ledger's catalog hash is this generation's own catalog.

    The catalog must be an artifact the generation's own manifest declares: a hash
    that merely matches some file in the directory binds nothing. A generation whose
    catalog is retained elsewhere (Portfolio123 keeps only its manifest here) is bound
    by that declaration alone; one whose file IS present at its declared location must
    carry exactly the declared bytes, so a swapped catalog is not bound by a manifest
    line its own bytes contradict.
    """

    if not isinstance(catalog_sha256, str):
        return False
    declaring = [row for row in declared if row.get("sha256") == catalog_sha256]
    for row in declaring:
        artifact_id = str(row.get("artifact_id"))
        for candidate in (
            generation / artifact_id,
            generation / Path(artifact_id).name,
        ):
            if candidate.is_file() and _sha(candidate)[0] != catalog_sha256:
                return False
    return bool(declaring)


def _provider_root_nodes(
    provider_generations_path: Path,
    ledger_generation: Path,
    recipe: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """GenerationNodes for every provider root, each bound to the ledger by hash.

    ``provider_generations_path`` lists, per provider root, the directory that
    holds the exact generation the release depends on. Nothing here trusts that
    list: each generation must equal, by id, manifest hash and catalog hash, what
    the selected ledger recorded, and the ledger's own qualification decides
    whether it is published as current authority or retained as evidence.
    """

    try:
        listing = json.loads(provider_generations_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError("provider generations list is unreadable") from exc
    if (
        not isinstance(listing, list)
        or any(
            not isinstance(row, dict) or set(row) != {"root_id", "source_dir"}
            for row in listing
        )
        or sorted(str(row["root_id"]) for row in listing) != sorted(PROVIDER_ROOTS)
    ):
        raise MaterializationError(
            "provider generations list must name every provider root exactly once"
        )
    bound = _ledger_provider_bindings(ledger_generation)
    epoch = recipe.get("epoch_manifest_generation")
    nodes: list[dict[str, Any]] = []
    for row in sorted(listing, key=lambda item: str(item["root_id"])):
        root_id = str(row["root_id"])
        source = _absolute(row["source_dir"], f"{root_id} source_dir")
        inventory = inventory_generation(source)
        generation_id = _text(inventory["generation_id"], f"{root_id} generation_id")
        if source.name != generation_id:
            raise MaterializationError(f"{root_id} source directory name differs")
        files = inventory.get("files")
        if not isinstance(files, list):
            raise MaterializationError(f"{root_id} generation inventory differs")
        if root_id == "epoch-manifest":
            if (
                not isinstance(epoch, dict)
                or epoch.get("generation_id") != generation_id
            ):
                raise MaterializationError(
                    "epoch-manifest generation differs from the recipe binding"
                )
            release_target = True
        else:
            binding = bound[root_id.removeprefix("provider-")]
            declared_rows = _load(source / "generation.json", f"{root_id} manifest")[
                "artifacts"
            ]
            if (
                binding.get("generation_id") != generation_id
                or binding.get("manifest_sha256") != inventory["manifest_sha256"]
                or not _catalog_hash_bound(
                    source, declared_rows, binding.get("catalog_sha256")
                )
            ):
                raise MaterializationError(
                    f"{root_id} generation differs from the provider ledger binding"
                )
            qualification = binding.get("qualification")
            if qualification in PROVIDER_CURRENT_QUALIFICATIONS:
                release_target = True
            elif qualification in PROVIDER_EVIDENCE_QUALIFICATIONS:
                if root_id not in EVIDENCE_ONLY_PROVIDER_ROOTS:
                    raise MaterializationError(
                        f"{root_id} is qualified PARTIAL but is not a declared "
                        "evidence-only root"
                    )
                release_target = False
            else:
                raise MaterializationError(
                    f"{root_id} qualification is not recognized: {qualification}"
                )
        nodes.append(
            {
                "depends_on": [],
                "files": files,
                "generation_id": generation_id,
                "kind": "GENERATION",
                "manifest_sha256": inventory["manifest_sha256"],
                "node_id": f"publish-{root_id}"
                if release_target
                else f"retain-{root_id}",
                "release_target": release_target,
                "root_id": root_id,
                "source_dir": str(source),
            }
        )
    return nodes


def _pointer_recovery_gate_payload(
    release_roots: Mapping[str, Path], release_generations: Mapping[str, str]
) -> dict[str, Any] | None:
    """Derive, from disk, which release roots carry a pointer no consumer can read."""

    rows: list[dict[str, str]] = []
    for root_id in sorted(release_generations):
        pointer = release_roots[root_id] / "CURRENT.json"
        if not pointer.exists():
            continue
        try:
            read_current(release_roots[root_id])
        except PointerIntegrityError as exc:
            if str(exc) not in RECOVERABLE_POINTER_FAILURES:
                raise MaterializationError(
                    f"{root_id} pointer is damaged, not recoverable: {exc}"
                ) from exc
            rows.append(
                {
                    "failure": str(exc),
                    "pointer_sha256": hashlib.sha256(pointer.read_bytes()).hexdigest(),
                    "proposed_generation_id": release_generations[root_id],
                    "root_id": root_id,
                }
            )
    if not rows:
        return None
    return {
        "disposition": "PROMOTE_BOUND_GENERATIONS",
        "schema_version": 1,
        "unusable_pointers": rows,
    }


def build_production_spec(
    recipe_path: Path,
    prepared_root: Path,
    semantic_path: Path,
    portfolio_root: Path,
    state_root: Path,
    spec_path: Path,
    hwm_governed_result_path: Path | None = None,
    hwm_governed_input_generation_path: Path | None = None,
    hwm_governed_calendar_authority_path: Path | None = None,
    hwm_governed_admission_receipt_path: Path | None = None,
    financial_artifacts_path: Path | None = None,
    financial_input_contract_path: Path | None = None,
    predecessor_repair_gate_path: Path | None = None,
    provider_generations_path: Path | None = None,
) -> dict[str, Any]:
    """Bind a verified private proof to the stable production root set."""

    recipe = _recipe(recipe_path)
    if financial_artifacts_path is None:
        raise MaterializationError(
            "actual financial outcome ArtifactNodes are required"
        )
    artifact_rows = json.loads(financial_artifacts_path.read_bytes())
    if (
        not isinstance(artifact_rows, list)
        or not artifact_rows
        or any(
            not isinstance(row, dict)
            or row.get("kind") not in {"ARTIFACT", "GENERATION"}
            or (
                row.get("kind") == "GENERATION"
                and (
                    row.get("release_target") is not False
                    or row.get("root_id") not in PRODUCTION_ROOTS
                    or row.get("source_dir") is None
                )
            )
            for row in artifact_rows
        )
    ):
        raise MaterializationError(
            "financial inventory requires ArtifactNodes or non-pointer "
            "GenerationNodes under existing production roots"
        )
    if recipe.get("schema_version") != 3:
        raise MaterializationError(
            "schema-2 recipe is recovery-only and cannot create a production specification"
        )
    repo = Path(__file__).resolve().parents[1]
    release_commit, identity_scheme = _release_identity(repo, recipe["release_commit"])
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise MaterializationError("repository must be clean at the recipe commit")
    prepared_root = prepared_root.resolve()
    semantic_path = semantic_path.resolve()
    portfolio_root = portfolio_root.resolve()
    state_root = state_root.resolve()
    # The calculation-scoped verification receipts live under the state root. They are
    # written during the cold verification of the candidate, so the only state root
    # that can later reuse them is the prepared run's own retained cache root.
    retained_cache_root = _prepared_retained_cache_root(prepared_root)
    if state_root != retained_cache_root:
        raise MaterializationError(
            "production state root must be the prepared run's retained cache root: "
            "calculation-scoped verification receipts are retained there "
            f"(state root {state_root}, retained cache root {retained_cache_root})"
        )
    if portfolio_root == repo or repo in portfolio_root.parents:
        raise MaterializationError("production portfolio root must be outside Git")

    sources: dict[str, Path] = {}
    generation_ids: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    analytics_result = prepared_root / "stage-results" / "analytics.json"
    vertical = _load(Path(recipe["vertical_recipe"]["path"]), "vertical recipe")
    current_lenses = (
        _production_lens_nodes(
            prepared_root,
            _load(
                Path(recipe["product_authority"]["contract"]["path"]),
                "product contract",
            ),
        )
        if vertical.get("schema_version") == 9
        or (
            analytics_result.is_file()
            and "lens_generation_bindings"
            in _load(analytics_result, "prepared analytics")
        )
        else {}
    )
    lens_targets = {
        row["root_id"]: row for row in current_lenses.values() if row["release_target"]
    }
    for root_id, relative in PRODUCTION_STAGE_ROOTS.items():
        if root_id in lens_targets:
            node = lens_targets[root_id]
            sources[root_id] = Path(node["source_dir"])
            generation_ids[root_id] = node["generation_id"]
            nodes.append(node)
            continue
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

    provider_root_paths: dict[str, Path] = {}
    if provider_generations_path is not None:
        provider_root_paths = _provider_root_paths(portfolio_root)
        for provider_node in _provider_root_nodes(
            provider_generations_path, sources["provider-ledger"], recipe
        ):
            nodes.append(provider_node)
            if provider_node["release_target"]:
                sources[provider_node["root_id"]] = Path(provider_node["source_dir"])
                generation_ids[provider_node["root_id"]] = provider_node[
                    "generation_id"
                ]

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
    if current_lenses:
        _verify_panel_lens_nodes(
            panel_payload,
            current_lenses,
            generation_ids["factor-decomposition"],
        )
        # Reuse financial inventory identities so its ArtifactNode dependencies
        # remain exact, while preventing a duplicate immutable-generation copy.
        for lens in current_lenses.values():
            matches = [
                row
                for row in artifact_rows
                if row.get("kind") == "GENERATION"
                and row.get("root_id") == lens["root_id"]
                and row.get("generation_id") == lens["generation_id"]
            ]
            if len(matches) > 1:
                raise MaterializationError("duplicate financial lens generation")
            if matches:
                supplied = matches[0]
                if any(
                    supplied.get(field) != lens[field]
                    for field in (
                        "source_dir",
                        "manifest_sha256",
                        "files",
                    )
                ):
                    raise MaterializationError(
                        "financial lens generation binding differs"
                    )
                if lens["release_target"]:
                    old_id = supplied["node_id"]
                    for row in artifact_rows:
                        row["depends_on"] = [
                            lens["node_id"] if item == old_id else item
                            for item in row.get("depends_on", [])
                        ]
                    artifact_rows.remove(supplied)
            elif not lens["release_target"]:
                nodes.append(lens)
    elif _panel_hybrid_generation_ids(panel_payload) != {
        root_id: generation_ids[root_id] for root_id in HYBRID_PANEL_ROOT_IDS.values()
    }:
        raise MaterializationError("prepared panel hybrid analytics authority differs")

    prepared_epoch_proof = _financial_epoch_manifest_proof(recipe, prepared_root)
    accepted_epoch_proof = recipe.get("financial_epoch_manifest_proof")
    if accepted_epoch_proof is None:
        if prepared_epoch_proof is not None:
            raise MaterializationError("prepared epoch-manifest proof differs")
    elif (
        not isinstance(accepted_epoch_proof, dict)
        or prepared_epoch_proof is None
        or canonical_json_bytes(prepared_epoch_proof)
        != canonical_json_bytes(accepted_epoch_proof)
    ):
        raise MaterializationError("prepared epoch-manifest proof differs")
    expected_semantic = canonical_json_bytes(
        _semantic_gate_payload(recipe, epoch_manifest_proof=prepared_epoch_proof)
    )
    if semantic_path.read_bytes() != expected_semantic:
        raise MaterializationError("prepared semantic gate differs")
    production_semantic = canonical_json_bytes(
        _semantic_gate_payload(
            recipe,
            generation_ids,
            epoch_manifest_proof=prepared_epoch_proof,
        )
    )
    production_semantic_path = spec_path.with_name(
        "production-post-build-semantic.json"
    )
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

    manifest_inputs = recipe.get("financial_epoch_manifest_inputs")
    contract = {
        "code_commit": release_commit,
        "generation_ids": generation_ids,
        "orchestration_contract_sha256": orchestration_sha,
        "pipeline_id": recipe["pipeline_id"],
        "prepared_release_commit": prepared_release_commit,
        "production_roots": {
            **{
                root_id: str(portfolio_root / relative)
                for root_id, relative in PRODUCTION_ROOTS.items()
            },
            **{root_id: str(path) for root_id, path in provider_root_paths.items()},
        },
        "schema_version": (
            3
            if manifest_inputs is not None
            else 2
            if recipe.get("schema_version") == 3
            else 1
        ),
        "semantic_gate_sha256": hashlib.sha256(expected_semantic).hexdigest(),
        "production_semantic_gate_sha256": hashlib.sha256(
            production_semantic
        ).hexdigest(),
        "source_generations": {key: str(value) for key, value in sources.items()},
    }
    if manifest_inputs is not None:
        if prepared_epoch_proof is None:
            raise MaterializationError("prepared epoch-manifest proof is missing")
        contract["financial_epoch_manifest_inputs_sha256"] = hashlib.sha256(
            canonical_json_bytes(manifest_inputs)
        ).hexdigest()
        contract["financial_epoch_manifest_proof_sha256"] = hashlib.sha256(
            canonical_json_bytes(prepared_epoch_proof)
        ).hexdigest()
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
    product_path = spec_path.with_name("financial-mvp-product-conformance.json")
    hwm_bindings = _hwm_binding_paths(
        hwm_governed_result_path,
        hwm_governed_input_generation_path,
        hwm_governed_calendar_authority_path,
        hwm_governed_admission_receipt_path,
    )
    # A generation an external agent rewrote after publication can never
    # reproduce the inventory its release sealed, which stops every successor
    # being prepared. The pipeline already knows how to admit exactly that,
    # under `predecessor-authority-repair`: it re-derives the drift itself and
    # refuses a gate that does not match, or that reuses a drifted generation.
    # This only carries the reviewed evidence through to the production spec.
    predecessor_repair_gates: list[dict[str, Any]] = []
    if predecessor_repair_gate_path is not None:
        repair_bytes = predecessor_repair_gate_path.read_bytes()
        predecessor_repair_gates.append(
            {
                "gate_id": "predecessor-authority-repair",
                "path": str(predecessor_repair_gate_path),
                "sha256": hashlib.sha256(repair_bytes).hexdigest(),
            }
        )
    # Provider roots whose own CURRENT pointer dangles or is schema v1 cannot be
    # recorded as a predecessor graph. The pipeline admits exactly those roots
    # under `predecessor-pointer-recovery` after re-deriving the failure from
    # disk; the evidence is derived here, never supplied.
    pointer_recovery_gates: list[dict[str, Any]] = []
    recovery_payload = (
        None
        if provider_generations_path is None
        else _pointer_recovery_gate_payload(
            {
                **{
                    root_id: portfolio_root / relative
                    for root_id, relative in PRODUCTION_ROOTS.items()
                },
                **provider_root_paths,
            },
            generation_ids,
        )
    )
    if recovery_payload is not None:
        recovery_path = spec_path.with_name("predecessor-pointer-recovery.json")
        recovery_bytes = canonical_json_bytes(recovery_payload)
        _write_exact(recovery_path, recovery_bytes)
        pointer_recovery_gates.append(
            {
                "gate_id": POINTER_RECOVERY_GATE_ID,
                "path": str(recovery_path),
                "sha256": hashlib.sha256(recovery_bytes).hexdigest(),
            }
        )
    product_gates = _product_gate_rows(
        production_product_authority,
        product_path,
        input_contract_path=financial_input_contract_path,
        hwm_bindings=hwm_bindings,
        hwm_source_paths=(
            _production_hwm_source_paths(
                sources,
                artifact_rows,
                manifest_inputs,
            )
            if hwm_bindings is not None
            else None
        ),
        hwm_sleeve_facts_paths=(
            _production_sleeve_facts_paths(artifact_rows)
            if hwm_bindings is not None
            else ()
        ),
    )
    manifest_gates = _financial_epoch_manifest_gate_rows(
        manifest_inputs,
        prepared_epoch_proof,
        spec_path.with_name("financial-epoch-manifest-input-authority.json"),
    )
    spec = {
        **_spec_identity_fields(identity_scheme),
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
        "coordination_root": str(portfolio_root / "releases-refresh-2026"),
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
            *manifest_gates,
            *predecessor_repair_gates,
            *pointer_recovery_gates,
        ],
        "nodes": [*nodes, *artifact_rows],
        "pipeline_id": f"{recipe['pipeline_id']}-production",
        "roots": {
            **{
                root_id: str(portfolio_root / relative)
                for root_id, relative in PRODUCTION_ROOTS.items()
            },
            **{root_id: str(path) for root_id, path in provider_root_paths.items()},
        },
        "schema_version": 1,
        "state_root": str(state_root),
    }
    # The initial create-only specification contains no guessed future hashes.
    # Only this final specification binds already built numerical artifacts.
    from quant_engine.core.pipeline import PipelineSpec
    from portfolio_financial_outcomes import (
        validate_financial_outcomes_with_deferred_ledger,
    )

    # The verifier only reads the financial-mvp contract/HWM gates (never
    # gates[0]/gates[1] below), so this pre-finalization spec is sufficient to
    # run the one real verification. But PipelineSpec.spec_sha256 hashes the
    # *entire* spec, including gates[0]/gates[1], whose final hashes depend on
    # this call's own `proof` -- so the ledger entry for that one call cannot
    # be named until the finalized spec exists. Run the verification now and
    # defer the ledger write until the finalized spec is built below, instead
    # of attributing the call to a spec identity the emitted file will never
    # match, or repeating the (expensive, numerically authoritative) call a
    # second time against the finalized spec.
    completed_spec = PipelineSpec.from_json_bytes(canonical_json_bytes(spec))
    proof, finalize_ledger = validate_financial_outcomes_with_deferred_ledger(
        completed_spec, caller="production-spec"
    )
    final_spec = completed_spec
    try:
        work_counters = _financial_work_counters(proof)
        production_semantic = canonical_json_bytes(
            _semantic_gate_payload(
                recipe,
                generation_ids,
                epoch_manifest_proof=prepared_epoch_proof,
                financial_outcomes=proof,
            )
        )
        contract["production_semantic_gate_sha256"] = hashlib.sha256(
            production_semantic
        ).hexdigest()
        contract_bytes = canonical_json_bytes(contract)
        spec["gates"][0]["sha256"] = hashlib.sha256(contract_bytes).hexdigest()
        spec["gates"][1]["sha256"] = hashlib.sha256(production_semantic).hexdigest()
        final_spec = PipelineSpec.from_json_bytes(canonical_json_bytes(spec))
    finally:
        # The ledger entry names whichever spec identity is the best available:
        # the finalized one on the ordinary path, or the pre-finalization one
        # if building the final spec itself fails after a real verification.
        finalize_ledger(final_spec)
    _write_exact(production_semantic_path, production_semantic)
    _write_exact(contract_path, contract_bytes)
    _write_exact(spec_path, canonical_json_bytes(spec))
    return {
        "contract": str(contract_path),
        "nodes": len(spec["nodes"]),
        "root_count": len(PRODUCTION_ROOTS) + len(provider_root_paths),
        "spec": str(spec_path),
        # Per-invocation work evidence. It is not part of the semantic gate, the
        # contract or the specification, so reporting it moves no pin.
        "work_counters": work_counters,
    }


def _attended_stage_root(output_root: Path, role: str) -> Path:
    roots = {
        "ledger": "ledger",
        "baseline": "baseline",
        "attribution": "attribution",
        "analytics": "analytics",
        "report": "report",
        "panel-candidate": "panel",
        "panel": "panel",
    }
    return output_root / "stages" / roots[role]


def _attended_completed_ledger(receipt: Mapping[str, Any], stage_root: Path) -> None:
    """Verify the stage-owned ledger generation before resume may skip it."""

    try:
        root = _absolute(receipt.get("root"), "attended completed ledger root")
        generation_id = _text(
            receipt.get("generation_id"), "attended completed ledger generation_id"
        )
        catalog_sha256 = _text(
            receipt.get("catalog_sha256"), "attended completed ledger catalog_sha256"
        )
        if len(generation_id) != 64 or len(catalog_sha256) != 64:
            raise ValueError("ledger identity length differs")
        if root != stage_root.resolve():
            raise ValueError("ledger root differs")
        import portfolio_release_builder as release

        verified_id, catalog = release._validated_reusable_provider_ledger(  # noqa: SLF001
            stage_root / "generations" / generation_id
        )
        if verified_id != generation_id or _sha(catalog)[0] != catalog_sha256:
            raise ValueError("ledger catalog identity differs")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "attended completed ledger artifact identity differs"
        ) from exc


def _attended_completed_baseline(receipt: Mapping[str, Any], stage_root: Path) -> None:
    """Verify the staged baseline through its existing generation reader."""

    try:
        root = _absolute(receipt.get("root"), "attended completed baseline root")
        generation_id = _text(
            receipt.get("generation_id"), "attended completed baseline generation_id"
        )
        catalog_sha256 = _text(
            receipt.get("catalog_sha256"), "attended completed baseline catalog_sha256"
        )
        if len(generation_id) != 64 or len(catalog_sha256) != 64:
            raise ValueError("baseline identity length differs")
        if root != stage_root.resolve():
            raise ValueError("baseline root differs")
        import portfolio_report

        _catalog, identity, _catalog_path = portfolio_report._baseline(  # noqa: SLF001
            stage_root, generation_id=generation_id
        )
        if identity.get("generation_id") != generation_id or (
            identity.get("catalog_sha256") != catalog_sha256
        ):
            raise ValueError("baseline catalog identity differs")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "attended completed baseline artifact identity differs"
        ) from exc


def _attended_completed_attribution(
    receipt: Mapping[str, Any], stage_root: Path
) -> None:
    """Re-read the immutable attribution bundle and its stage verification."""

    try:
        bundle_id = _text(receipt.get("bundle_id"), "attended attribution bundle_id")
        destination = _absolute(
            receipt.get("destination"), "attended attribution destination"
        )
        verification_path = _absolute(
            receipt.get("verification"), "attended attribution verification"
        )
        if destination.name != bundle_id or verification_path != (
            stage_root.resolve() / "attribution-verification.json"
        ):
            raise ValueError("attribution receipt identity differs")
        manifest_path = destination / "plan-manifest.json"
        manifest = _load(manifest_path, "attended attribution plan manifest")
        if manifest.get("bundle_id") != bundle_id:
            raise ValueError("attribution bundle identity differs")
        for section in ("inputs", "outputs"):
            rows = manifest.get(section)
            if not isinstance(rows, list) or not rows:
                raise ValueError("attribution artifact inventory differs")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("attribution artifact definition differs")
                path = destination / _text(row.get("path"), "attribution artifact")
                sha256, size_bytes = _sha(path)
                if sha256 != row.get("sha256") or size_bytes != row.get("size_bytes"):
                    raise ValueError("attribution artifact identity differs")
        verification = _load(verification_path, "attended attribution verification")
        if (
            verification.get("schema_version") != 1
            or verification.get("status") != "PASS"
            or verification.get("bundle_id") != bundle_id
            or verification.get("plan_manifest_sha256") != _sha(manifest_path)[0]
        ):
            raise ValueError("attribution verification identity differs")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "attended completed attribution artifact identity differs"
        ) from exc


def _attended_published_generation(
    root: Path,
    expected_generation_id: object,
    label: str,
    *,
    filename: str,
    expected_schema_version: int | Sequence[int],
) -> None:
    """Read one published analytics generation through its native reader."""

    import portfolio_control_panel as panel

    generation_id = _text(expected_generation_id, f"attended {label} generation_id")
    _value, identity, _path = panel._current_analytics(  # noqa: SLF001
        root,
        filename=filename,
        field_name=label,
        expected_schema_version=expected_schema_version,
    )
    if identity.get("generation_id") != generation_id:
        raise ValueError(f"{label} generation identity differs")


def _attended_completed_analytics(receipt: Mapping[str, Any], stage_root: Path) -> None:
    """Re-read every result bound by the current analytics producer."""

    if receipt.get("schema_version") != 2:
        raise ValueError("analytics result schema differs")
    roots = {
        "correlation": _absolute(
            receipt.get("correlation_root"), "attended analytics correlation_root"
        ),
        "decomposition": _absolute(
            receipt.get("decomposition_root"), "attended analytics decomposition_root"
        ),
        "lenses": _absolute(
            receipt.get("decomposition_lenses_root"),
            "attended analytics decomposition_lenses_root",
        ),
        "weekly": _absolute(
            receipt.get("weekly_root"), "attended analytics weekly_root"
        ),
    }
    if any(
        path.is_symlink() or not path.is_dir() or not path.is_relative_to(stage_root)
        for path in roots.values()
    ):
        raise ValueError("analytics output differs")
    _attended_published_generation(
        roots["correlation"],
        receipt.get("correlation_generation_id"),
        "analytics correlation",
        filename="correlation.json",
        expected_schema_version=(1, 2),
    )
    _attended_published_generation(
        roots["decomposition"],
        receipt.get("decomposition_generation_id"),
        "analytics decomposition",
        filename="decomposition.json",
        expected_schema_version=(1, 2, 3),
    )
    _attended_published_generation(
        roots["weekly"],
        receipt.get("weekly_generation_id"),
        "analytics weekly",
        filename="weekly-returns.json",
        expected_schema_version=2,
    )
    bindings = receipt.get("lens_generation_bindings")
    plan_bundles = receipt.get("plan_bundle_ids")
    if (
        not isinstance(bindings, list)
        or not bindings
        or not isinstance(plan_bundles, dict)
    ):
        raise ValueError("analytics lens bindings differ")
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError("analytics lens binding differs")
        evidence_mode = _text(binding.get("evidence_mode"), "analytics lens mode")
        spec_id = _text(binding.get("spec_id"), "analytics lens spec_id")
        source_key = _text(binding.get("source_key"), "analytics lens source_key")
        surface = _text(binding.get("surface"), "analytics lens surface")
        if (
            evidence_mode not in {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}
            or surface not in {"STATIC", "DYNAMIC_FLS"}
            or (evidence_mode, spec_id) in seen
            or binding.get("plan_bundle_id") != plan_bundles.get(evidence_mode)
            or spec_id != f"{source_key}:{surface}"
        ):
            raise ValueError("analytics lens binding differs")
        seen.add((evidence_mode, spec_id))
        lens_root = roots["lenses"] / evidence_mode / surface / source_key
        _attended_published_generation(
            lens_root,
            binding.get("generation_id"),
            "analytics lens",
            filename="decomposition.json",
            expected_schema_version=(1, 2, 3),
        )
    hybrid_generation = receipt.get("hybrid_dynamic_generation_id")
    if hybrid_generation is not None:
        _attended_published_generation(
            roots["lenses"] / "LIVE_PLUS_BACKTEST" / "dynamic-style",
            hybrid_generation,
            "analytics hybrid decomposition",
            filename="decomposition.json",
            expected_schema_version=(1, 2, 3),
        )


def _attended_completed_report(receipt: Mapping[str, Any], stage_root: Path) -> None:
    """Validate the report producer's canonical-data candidate for resume."""

    root = _absolute(receipt.get("root"), "attended completed report root")
    candidate = _absolute(receipt.get("candidate_dir"), "attended report candidate")
    if (
        root != stage_root.resolve()
        or candidate.is_symlink()
        or not candidate.is_dir()
        or not candidate.is_relative_to(stage_root)
    ):
        raise ValueError("report candidate differs")
    report_data = candidate / "portfolio-report-data.json"
    if report_data.is_symlink() or not report_data.is_file():
        raise ValueError("report candidate inventory differs")
    report_bytes = report_data.read_bytes()
    if (
        candidate.name != hashlib.sha256(report_bytes).hexdigest()
        or canonical_json_bytes(json.loads(report_bytes)) != report_bytes
    ):
        raise ValueError("report candidate content differs")
    generation_id = _text(receipt.get("generation_id"), "attended report generation_id")
    current = read_current(root)
    if current.generation_id != generation_id:
        raise ValueError("report current generation differs")
    # Read through the producer's own reader, as the ledger, baseline and
    # analytics gates already do. `read_generation_bundle` demands the detached
    # inventory and receipt that only `stage_generation_bundle` writes, and the
    # report producer uses the bare `stage_generation`, so it could never pass.
    # `_current_report` binds the report bytes to the manifest artifact's sha
    # AND size, and `read_current` pins the whole directory inventory.
    import portfolio_control_panel as panel

    _report, identity, _path = panel._current_report(root)  # noqa: SLF001
    if identity.get("generation_id") != generation_id:
        raise ValueError("report generation identity differs")
    generation_report = (
        root / "generations" / generation_id / "portfolio-report-data.json"
    )
    generation_bytes = generation_report.read_bytes()
    if (
        generation_bytes != report_bytes
        or canonical_json_bytes(json.loads(generation_bytes)) != generation_bytes
    ):
        raise ValueError("report generation content differs")


def _attended_completed_panel(receipt: Mapping[str, Any], stage_root: Path) -> None:
    """Verify the published panel generation and its retained producer QA."""

    root = _absolute(receipt.get("root"), "attended completed panel root")
    if root != stage_root.resolve():
        raise ValueError("panel root differs")
    generation_id = _text(receipt.get("generation_id"), "attended panel generation_id")
    current = read_current(root)
    if current.generation_id != generation_id:
        raise ValueError("panel current generation differs")
    # Same correction as the report gate. The panel producer also stages with
    # the bare `stage_generation`, and it writes QA under `qa/<name>` while
    # declaring the artifact id `evidence/<name>`, so a bundle read fails twice
    # over. `_current_panel_qa` binds each declared artifact to the bytes on
    # disk by sha and size, which a membership test on manifest ids would not.
    import portfolio_control_panel as panel

    identity = panel._current_panel_qa(  # noqa: SLF001
        root,
        required=("qa-validation.json", "semantic-validation.json"),
    )
    if identity.get("generation_id") != generation_id:
        raise ValueError("panel generation identity differs")


def _attended_completed_stage_root(
    receipt: Mapping[str, Any], stage_root: Path, role: str
) -> None:
    """Require a role result to bind its materialized root before resume."""

    try:
        if role == "analytics":
            _attended_completed_analytics(receipt, stage_root)
            return
        root = _absolute(receipt.get("root"), f"attended completed {role} root")
        if root != stage_root.resolve():
            raise ValueError("stage root differs")
        if role == "report":
            _attended_completed_report(receipt, stage_root)
        elif role == "panel":
            _attended_completed_panel(receipt, stage_root)
        else:
            import portfolio_release_builder as release

            generation_id, _generation = release._published_generation(stage_root)  # noqa: SLF001
            if receipt.get("generation_id") != generation_id:
                raise ValueError("published generation identity differs")
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            f"attended completed {role} artifact identity differs"
        ) from exc


def _attended_completed_panel_candidate(
    receipt: Mapping[str, Any], stage_root: Path
) -> None:
    """Validate the release builder's immutable panel-candidate checkpoint."""

    try:
        if (
            receipt.get("schema_version") != 1
            or receipt.get("role") != "panel-candidate"
        ):
            raise ValueError("panel candidate checkpoint differs")
        candidate = receipt.get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError("panel candidate identity differs")
        candidate_root = _absolute(candidate.get("path"), "panel candidate path")
        if not candidate_root.is_relative_to(stage_root.resolve()):
            raise ValueError("panel candidate root differs")
        _validate_directory_definition(candidate, "attended panel candidate")
        result = receipt.get("result")
        if (
            not isinstance(result, dict)
            or _absolute(result.get("candidate_dir"), "panel candidate result")
            != candidate_root
        ):
            raise ValueError("panel candidate result differs")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "attended completed panel-candidate artifact identity differs"
        ) from exc


def _attended_completed_stages(
    output_root: Path,
    required_stages: Sequence[str],
    *,
    failed_stage: str | None = None,
) -> list[str]:
    """Return only a contiguous, independently readable prefix of stage receipts."""

    completed: list[str] = []
    missing_seen = False
    for role in required_stages:
        receipt = output_root / "stage-results" / f"{role}.json"
        stage_root = _attended_stage_root(output_root, role)
        receipt_exists = receipt.exists()
        root_exists = stage_root.exists()
        # The panel candidate intentionally creates the panel staging root;
        # before browser QA the final panel receipt is absent while that shared
        # root is already a valid predecessor artifact.
        if role == "panel" and not receipt_exists and "panel-candidate" in completed:
            root_exists = False
        if not receipt_exists:
            if root_exists and (stage_root.is_symlink() or not stage_root.is_dir()):
                raise MaterializationError(
                    f"attended completed {role} stage root is invalid"
                )
            missing_seen = True
            continue
        if missing_seen or not root_exists:
            raise MaterializationError(
                f"attended completed {role} artifact set is incomplete"
            )
        if role == failed_stage:
            if any(
                (output_root / "stage-results" / f"{later}.json").exists()
                for later in required_stages[required_stages.index(role) + 1 :]
            ):
                raise MaterializationError(
                    "attended failed stage has a later completed receipt"
                )
            return completed
        result = _load(receipt, f"attended {role} stage result")
        if stage_root.is_symlink() or not stage_root.is_dir():
            raise MaterializationError(
                f"attended completed {role} stage root is invalid"
            )
        if role == "ledger":
            _attended_completed_ledger(result, stage_root)
        elif role == "baseline":
            _attended_completed_baseline(result, stage_root)
        elif role == "attribution":
            _attended_completed_attribution(result, stage_root)
        elif role == "panel-candidate":
            _attended_completed_panel_candidate(result, stage_root)
        else:
            _attended_completed_stage_root(result, stage_root, role)
        completed.append(role)
    return completed


ATTENDED_QA_STAGE: Final = "panel"
ATTENDED_QA_INSTRUCTIONS: Final = (
    "stage 'panel' consumes browser QA evidence produced from the panel-candidate "
    "output, which no earlier stage produces. Supply it with --qa-dir <directory> if "
    "it already exists; otherwise declare --qa-hold: the run builds every stage "
    "through panel-candidate and stops at panel, then run the browser QA against the "
    "panel-candidate output and continue with --execute --resume --qa-dir <directory>."
)


def _attended_qa_plan(
    required_stages: Sequence[str], qa_dir: Path | None
) -> dict[str, Any] | None:
    """What the plan needs for its QA stage, stated before anything runs."""

    if ATTENDED_QA_STAGE not in required_stages:
        return None
    return {
        "stage": ATTENDED_QA_STAGE,
        "required_flag": "--qa-dir",
        "hold_flag": "--qa-hold",
        "supplied": qa_dir is not None,
        "instructions": ATTENDED_QA_INSTRUCTIONS,
    }


def _attended_require_qa_plan(
    required_stages: Sequence[str], *, qa_dir: Path | None, qa_hold: bool
) -> None:
    """Refuse to start a run whose plan reaches panel with no QA and no declared hold."""

    if ATTENDED_QA_STAGE in required_stages and qa_dir is None and not qa_hold:
        raise MaterializationError(
            "attended plan includes the panel stage but neither --qa-dir nor "
            f"--qa-hold was given: {ATTENDED_QA_INSTRUCTIONS}"
        )


def _attended_view(
    output_root: Path,
    required_stages: Sequence[str],
    request_identity: Mapping[str, Any],
    verified_completed_stages: Sequence[str] | None = None,
    qa_dir: Path | None = None,
) -> dict[str, Any]:
    """Render selection facts without claiming later proof-bound material exists."""

    return {
        "qa": _attended_qa_plan(required_stages, qa_dir),
        "planned_stages": list(required_stages),
        "post_proof_pipeline_spec": None,
        "request_identity": _canonical_clone(request_identity),
        "verified_completed_stages": list(verified_completed_stages)
        if verified_completed_stages is not None
        else _attended_completed_stages(output_root, required_stages),
    }


def _attended_artifacts(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        raise MaterializationError("attended source artifacts differ")
    artifacts: dict[str, dict[str, object]] = {}
    for row in value:
        if not isinstance(row, dict) or "type" not in row:
            raise MaterializationError("attended source artifact differs")
        kind = row.get("type")
        definition = {key: item for key, item in row.items() if key != "type"}
        if kind == "FILE":
            path = _validate_file_definition(definition, "attended source artifact")
        elif kind == "DIRECTORY":
            path = _validate_directory_definition(
                definition, "attended source artifact"
            )
        else:
            raise MaterializationError("attended source artifact type differs")
        key = str(path)
        if key in artifacts:
            raise MaterializationError("attended source artifact is duplicated")
        artifacts[key] = definition
    return artifacts


def _attended_disjoint(left: Path, right: Path, label: str) -> None:
    try:
        left.relative_to(right)
    except ValueError:
        try:
            right.relative_to(left)
        except ValueError:
            return
    raise MaterializationError(f"attended {label} roots overlap")


def _attended_predecessor(definition: object, portfolio_root: Path) -> Path | None:
    """Validate the selected predecessor, rather than allowing a shadow pointer."""

    current = portfolio_root / "releases-refresh-2026" / "CURRENT_RELEASE.json"
    if definition is None:
        if current.exists():
            raise MaterializationError(
                "attended predecessor selection cannot hide a committed release"
            )
        return None
    path = _validate_file_definition(definition, "attended predecessor spec")
    try:
        from quant_engine.core.pipeline import PipelineSpec
        import portfolio_qc

        spec = PipelineSpec.from_json_bytes(path.read_bytes())
        manifest = portfolio_qc._read_committed_release(spec)  # noqa: SLF001
        portfolio_qc._verify_committed_manifest(spec, manifest)  # noqa: SLF001
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "attended predecessor is not a committed release"
        ) from exc
    return path


def _attended_request_identity(
    *,
    repo_root: Path,
    python: Path,
    workspace_root: Path,
    output_root: Path,
    retained_cache_root: Path,
    requested_as_of: str,
    evaluated_on: str | None,
    source_run: Path,
    financial_stage_inputs: Mapping[str, Path],
    source_phase: Mapping[str, Any],
    runtime: Mapping[str, Any],
    retained_cache_base: Path | None = None,
) -> dict[str, Any]:
    # `evaluated_on` is a separate clock from `requested_as_of`: plan bundles consume it as
    # `evaluation_date`, the dependence spec as its `as_of_date`, and the governed HWM result
    # records it and derives freshness from it.  Without it here, a resume under a different
    # evaluation date reused every checkpoint and returned the earlier date's calculations.
    # A shared read-only cache base is bound only when one is used, so the identity of a
    # run that reads its retained cache directly is unchanged and stays resumable.
    base = (
        {"retained_cache_base": str(retained_cache_base)}
        if retained_cache_base is not None
        else {}
    )
    return {
        **base,
        "evaluated_on": evaluated_on,
        "output_root": str(output_root),
        "financial_stage_inputs": {
            flag: _file_definition(path)
            for flag, path in sorted(financial_stage_inputs.items())
        },
        "python": _file_definition(python),
        "repo_root": str(repo_root),
        "requested_as_of": requested_as_of,
        "retained_cache_root": str(retained_cache_root),
        "runtime": _canonical_clone(runtime),
        "source_phase": _canonical_clone(source_phase),
        "source_run": _file_definition(source_run),
        "workspace_root": str(workspace_root),
    }


def _runtime_identity(python: Path, repo_root: Path) -> dict[str, object]:
    """Probe the exact configured interpreter before source preparation writes."""

    if python.is_symlink() or not python.is_file():
        raise MaterializationError("attended configured Python is unavailable")
    try:
        probe = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import json,sys; print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:3])},sort_keys=True))",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        value = json.loads(probe.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MaterializationError("attended configured Python probe failed") from exc
    if probe.returncode != 0 or not isinstance(value, dict):
        raise MaterializationError("attended configured Python probe failed")
    executable = value.get("executable")
    version = value.get("version")
    if (
        not isinstance(executable, str)
        or Path(executable).resolve() != python.resolve()
        or not isinstance(version, list)
        or len(version) != 3
        or not all(isinstance(item, int) for item in version)
    ):
        raise MaterializationError("attended configured Python identity differs")
    return {
        "environment_sha256": runtime_environment_sha256(python),
        "executable_path": str(python.resolve()),
        "executable_sha256": _sha(python)[0],
        "version": version,
    }


def _attended_stage_financial_inputs(
    args: argparse.Namespace, release: Any
) -> dict[str, Path]:
    """Validate the release builder's governed input families before a child."""

    supplied = {
        flag: getattr(args, attribute)
        for flag, attribute in {
            **ATTENDED_HWM_INPUTS,
            **ATTENDED_CAPITAL_INPUTS,
        }.items()
    }
    # The contract marks INVESTED_CAPITAL REQUIRED for every current strategy, so
    # an attended run without the governed capital family can only end in a
    # blocked validator.  HWM stays all-or-none and is not gated here.
    if any(supplied[flag] is None for flag in ATTENDED_CAPITAL_INPUTS):
        raise MaterializationError(
            "attended run requires the governed invested-capital result and input "
            "generation (--invested-capital-result and "
            "--invested-capital-input-generation); start it with --preparation-spec"
        )
    try:
        release._governed_hwm_arguments(  # noqa: SLF001
            result=supplied["--hwm-governed-result"],
            input_generation=supplied["--hwm-governed-input-generation"],
            calendar_authority=supplied["--hwm-governed-calendar-authority"],
            admission_receipt=supplied["--hwm-governed-admission-receipt"],
        )
        release._invested_capital_arguments(  # noqa: SLF001
            result=supplied["--invested-capital-result"],
            input_generation=supplied["--invested-capital-input-generation"],
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError("attended financial input family differs") from exc
    return {
        flag: _validate_file_definition(
            _file_definition(Path(path).resolve()), f"attended {attribute}"
        )
        for flag, attribute in {
            **ATTENDED_HWM_INPUTS,
            **ATTENDED_CAPITAL_INPUTS,
        }.items()
        if (path := supplied[flag]) is not None
    }


WINDOWS_PATH_LIMIT: Final = 260
_GENERATION_ID_LENGTH: Final = 64
_GENERATION: Final = "0" * _GENERATION_ID_LENGTH
# The deepest paths below each root are the ones the producers actually write, measured
# from the real attended runs (output trees under C:\Dev\q, 2026-10-04): at most 183
# characters below the output root and 102 below the retained cache root. Nothing here is
# a path a producer does not write; in particular the output tree holds no captured
# provider response (the ledger stage's longest file is admit-or-declare-receipt.json).
_DECOMPOSITION_MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
_DECOMPOSITION_VARIANTS: Final = ("", "STATIC", "DYNAMIC_FLS")
_STATIC_SNAPSHOT_ACCOUNT: Final = "account-" + "0" * 16
_STATIC_SNAPSHOT_WINDOW: Final = "2026-01-02-2026-09-17.json"
_PREPARATION_PROVENANCE_NAME: Final = "05-base-proxies-adjusted.json.manifest.json"
_CACHE_HASH_FILE: Final = f"{_GENERATION}.json"


def _backslash(*parts: str) -> str:
    return "\\".join(part for part in parts if part)


def _derived_only_strategy_ids() -> frozenset[str]:
    """Strategies whose every census epoch is derived from a nested-strategy tag map.

    A derived epoch sits inside its parent account's retrieval window and has no
    receipt of its own, so such a strategy writes no static-snapshot receipt.
    """

    try:
        census = json.loads(ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS.read_bytes())
        authority = _financial_mvp_module().DERIVED_SLEEVE_EPOCH_AUTHORITY
        derived: set[str] = set()
        other: set[str] = set()
        for sleeve in census["current_census"]["account_sleeves"]:
            for assignment in sleeve["assignments"]:
                (derived if assignment.get("authority") == authority else other).add(
                    str(assignment["strategy_id"])
                )
        return frozenset(derived - other)
    except (MaterializationError, OSError, ValueError, KeyError, TypeError):
        return frozenset()


def _registry_strategy_ids() -> list[str]:
    try:
        registry = json.loads(CURRENT_STRATEGY_REGISTRY.read_bytes())
        derived_only = _derived_only_strategy_ids()
        return [
            str(row["strategy_id"])
            for row in registry["strategies"]
            if isinstance(row, dict) and str(row["strategy_id"]) not in derived_only
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def generated_tails(qa_dir: Path | None = None) -> list[tuple[str, str]]:
    """Every deepest generated path below the output root, as (label, tail)."""

    generation = _backslash
    tails: list[tuple[str, str]] = []
    lens_names = sorted(
        {
            root.name
            for root in PRODUCTION_STAGE_ROOTS.values()
            if "decomposition-lenses" in root.parts
        }
    )
    for mode in _DECOMPOSITION_MODES:
        for variant in _DECOMPOSITION_VARIANTS:
            for lens in lens_names:
                tails.append(
                    (
                        f"{mode} {variant or 'default'} {lens}",
                        generation(
                            "stages",
                            "analytics",
                            "decomposition-lenses",
                            mode,
                            variant,
                            lens,
                            "generations",
                            _GENERATION,
                            "decomposition.json",
                        ),
                    )
                )
    for key, stage_root in PRODUCTION_STAGE_ROOTS.items():
        artifact = {
            "provider-ledger": "admit-or-declare-receipt.json",
            "portfolio-baseline": "portfolio-baseline.json",
            "portfolio-report": "input\\account-strategy-current-epoch-crosswalk.json",
            "weekly-returns": "weekly-returns.json",
        }.get(key, "decomposition.json")
        tails.append(
            (
                key,
                generation(*stage_root.parts, "generations", _GENERATION, artifact),
            )
        )
    tails.append(
        (
            "report candidate",
            generation(
                "stages",
                "report",
                "candidates",
                _GENERATION,
                "account-strategy-current-epoch-crosswalk.json",
            ),
        )
    )
    # Static-snapshot receipts are keyed by the registry's strategy ids.
    for strategy in _registry_strategy_ids() or ["strategy"]:
        tails.append(
            (
                f"epoch receipt {strategy}",
                generation(
                    "stages",
                    "epoch",
                    "generations",
                    _GENERATION,
                    "receipt",
                    "static-snapshot",
                    _STATIC_SNAPSHOT_ACCOUNT,
                    strategy,
                    _STATIC_SNAPSHOT_WINDOW,
                ),
            )
        )
    for mode in ("live_only", "live_plus_backtest"):
        tails.append(
            (
                f"preparation plan {mode}",
                generation(
                    "preparation",
                    "plans",
                    mode,
                    _GENERATION,
                    "inputs",
                    "base",
                    "provenance",
                    _PREPARATION_PROVENANCE_NAME,
                ),
            )
        )
    tails.append(
        (
            "panel qa",
            _backslash(
                "stages",
                "panel",
                "generations",
                _GENERATION,
                "qa",
                "desktop-attribution",
                "request-failures.json",
            ),
        )
    )
    if qa_dir is not None and qa_dir.is_dir():
        # The panel stage stages the supplied QA evidence under its generation, so its
        # file names are data: measure them rather than assume a bound.
        for found in qa_dir.rglob("*"):
            if found.is_file():
                relative = found.relative_to(qa_dir)
                tails.append(
                    (
                        f"panel qa file {relative}",
                        generation(
                            "stages",
                            "panel",
                            "generations",
                            _GENERATION,
                            "qa",
                            *relative.parts,
                        ),
                    )
                )
    return tails


def longest_generated_tail() -> tuple[str, str]:
    return max(generated_tails(), key=lambda item: len(item[1]))


# The production chain keeps its calculation-scoped verification receipts and its
# pipeline run state in the retained cache (the production specification's state root
# is that root). `pipelines/<pipeline_id>-<16 hex>/receipts/<node_id>.json` carries a
# recipe-chosen pipeline id; real recipes use 21 characters plus "-production", and this
# budget allows 48. ACCEPTED RESIDUAL: a longer pipeline id, or an operator-chosen
# financial-artifact node id longer than the longest `publish-<root>-dynamic-fls` node,
# is not measured here.
PRODUCTION_PIPELINE_ID_BUDGET: Final = 48
_PIPELINE_RUN_KEY_HASH_LENGTH: Final = 16


def _production_receipt_tail() -> str:
    node_id = max(
        (f"publish-{root_id}-dynamic-fls" for root_id in PRODUCTION_ROOTS), key=len
    )
    return _backslash(
        "pipelines",
        "p" * PRODUCTION_PIPELINE_ID_BUDGET + "-" + "0" * _PIPELINE_RUN_KEY_HASH_LENGTH,
        "receipts",
        f"{node_id}.json",
    )


def _cache_tails() -> list[tuple[str, str]]:
    """The layouts written under the retained cache.

    Besides the decomposition and correlation caches, the production chain writes its
    calculation-scoped replay receipts and its run-state receipts here.
    """

    return [
        (
            "financial replay receipt",
            _backslash("financial-replay-receipts", _CACHE_HASH_FILE),
        ),
        ("production run receipt", _production_receipt_tail()),
        (
            "training evidence",
            _backslash(".decomposition-training-evidence", _CACHE_HASH_FILE),
        ),
        ("training", _backslash(".decomposition-training", _CACHE_HASH_FILE)),
        ("decomposition index", _backslash(".decomposition-index", _CACHE_HASH_FILE)),
        ("correlation index", _backslash(".correlation-index", _CACHE_HASH_FILE)),
        (
            "candidate",
            _backslash("candidates", _GENERATION, "decomposition.json"),
        ),
        (
            "candidate staging",
            _backslash("candidates", ".stage-" + "x" * 8, "decomposition.json"),
        ),
    ]


def _attended_plan_provenance_tails(
    preparation_spec: Path | None,
) -> list[tuple[str, str]]:
    """Measure the provenance basenames the plan bundle will copy under the output root.

    ``portfolio_current_decomposition_plan`` writes
    ``inputs/base/provenance/<NN>-<basename>`` for every provenance source of each
    mode's base spec template, so the names are known at preflight from the
    preparation spec. An unreadable spec yields nothing: its own validation reports it.
    """

    if preparation_spec is None:
        return []
    try:
        config = json.loads(Path(preparation_spec).read_bytes())
        plans = config["plans"]
        tails: list[tuple[str, str]] = []
        for mode, definition in plans.items():
            template = json.loads(
                Path(definition["base_spec_template"]["path"]).read_bytes()
            )
            for index, source in enumerate(template.get("provenance_sources", [])):
                name = Path(str(source["path"])).name
                tails.append(
                    (
                        f"preparation plan {mode} provenance {name}",
                        _backslash(
                            "preparation",
                            "plans",
                            str(mode).lower(),
                            _GENERATION,
                            "inputs",
                            "base",
                            "provenance",
                            f"{index:02d}-{name}",
                        ),
                    )
                )
        return tails
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _attended_path_budget(
    output_root: Path,
    retained_cache_root: Path,
    *,
    qa_dir: Path | None = None,
    extra_tails: Sequence[tuple[str, str]] = (),
) -> None:
    """Refuse roots under which a generated path would reach the Windows limit.

    A run that crosses it fails hours in, inside a child, at whichever artifact is
    first to be written past 259 characters. The deepest names are known, so the
    arithmetic is done before anything is created.

    Names the producers copy from inputs are measured when preflight can see them: the
    QA evidence files and, for a preparation spec, the plan provenance basenames.
    ACCEPTED RESIDUAL: the remaining data-dependent names (static-snapshot strategy ids
    beyond the registry, and a source-run path whose recipe is not read here) use the
    sampled maxima measured on the real runs, 183 characters below the output root and
    102 below the cache; docs/attended-run-storage.md records them.
    """

    for label, root, tails in (
        ("output", output_root, [*generated_tails(qa_dir), *extra_tails]),
        ("retained cache", retained_cache_root, _cache_tails()),
    ):
        base = len(str(root))
        stage, tail = max(tails, key=lambda item: len(item[1]))
        reached = base + 1 + len(tail)
        if reached >= WINDOWS_PATH_LIMIT:
            raise MaterializationError(
                f"attended {label} root {root} would hold a generated path of "
                f"{reached} characters ({stage}: ...{tail[-60:]}); the Windows limit "
                f"is {WINDOWS_PATH_LIMIT}. Use a shorter {label} root"
            )


def _attended_basic_preflight(args: argparse.Namespace) -> dict[str, Any]:
    """Validate the runner environment before source admission can have effects."""

    repo_root = Path(args.repo_root).resolve()
    python = Path(args.python).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    output_root = Path(args.output_root).resolve()
    retained_cache_root = Path(args.retained_cache_root).resolve()
    base_argument = getattr(args, "retained_cache_base", None)
    retained_cache_base = (
        Path(base_argument).resolve() if base_argument is not None else None
    )
    # Location gate first: every attended run writes far more than 50 MB, and nothing
    # below may create, probe-write or hydrate anything before a synced root is refused.
    for label, root in (
        ("output", output_root),
        ("workspace", workspace_root),
        ("retained cache", retained_cache_root),
        ("retained cache base", retained_cache_base),
    ):
        if root is not None:
            run_storage.refuse_synced_root(root, label)
    if repo_root != REPOSITORY_ROOT or not (repo_root / "tools").is_dir():
        raise MaterializationError(
            "attended repository root differs from this entrypoint"
        )
    runtime = _runtime_identity(python, repo_root)
    if workspace_root.is_symlink() or not workspace_root.is_dir():
        raise MaterializationError("attended workspace root is unavailable")
    if retained_cache_base is None:
        if retained_cache_root.is_symlink() or not retained_cache_root.is_dir():
            raise MaterializationError("attended retained cache root is unavailable")
    else:
        # With a shared base the retained cache root is this run's overlay, which the
        # runner creates as hard links to the base on its first execution.
        if retained_cache_base.is_symlink() or not retained_cache_base.is_dir():
            raise MaterializationError("attended retained cache base is unavailable")
        try:
            # A promoted base (tools/release_cache_base.py) carries sha256 provenance; a
            # modified one must refuse here, not become the new baseline of its overlays.
            verify_if_promoted(retained_cache_base)
        except CacheBaseError as exc:
            raise MaterializationError(str(exc)) from exc
        if retained_cache_root.is_symlink() or (
            retained_cache_root.exists() and not retained_cache_root.is_dir()
        ):
            raise MaterializationError("attended retained cache root is unavailable")
        _attended_disjoint(
            output_root, retained_cache_base, "output and retained cache base"
        )
        _attended_disjoint(
            retained_cache_root,
            retained_cache_base,
            "retained cache and retained cache base",
        )
    portfolio_root = workspace_root / "dataPortfolio"
    if portfolio_root.is_symlink() or not portfolio_root.is_dir():
        raise MaterializationError("attended workspace dataPortfolio is unavailable")
    _attended_disjoint(output_root, portfolio_root, "output and portfolio")
    _attended_disjoint(output_root, retained_cache_root, "output and retained cache")
    qa_argument = getattr(args, "qa_dir", None)
    _attended_path_budget(
        output_root,
        retained_cache_root,
        qa_dir=Path(qa_argument).resolve() if qa_argument is not None else None,
        extra_tails=_attended_plan_provenance_tails(
            getattr(args, "preparation_spec", None)
        ),
    )
    try:
        requested_as_of = date.fromisoformat(args.requested_as_of).isoformat()
    except ValueError as exc:
        raise MaterializationError("attended requested as-of date is invalid") from exc
    return {
        "output_root": output_root,
        "portfolio_root": portfolio_root,
        "python": python,
        "repo_root": repo_root,
        "requested_as_of": requested_as_of,
        "retained_cache_base": retained_cache_base,
        "retained_cache_root": retained_cache_root,
        "runtime": runtime,
        "workspace_root": workspace_root,
    }


ORACLE_LEDGER_BASENAME: Final = "provider-ledger.json"


def _attended_oracle_ledger_identity(document: Path) -> dict[str, Any]:
    """The ledger identity an acceptance inputs MANIFEST (or a receipt embedding it) names.

    The identity is the sha256 of the ledger file's content: the oracle derivations
    (``acceptance/derive_a`` and ``derive_b``) select the one ``provider-ledger.json``
    entry of the manifest and pin its sha256, and a preserved copy under any folder
    name holds the same bytes. The parent folder counts as a generation id only when it
    is 64 hex characters; only then is it compared. A receipt such as
    ``parent-execution-receipt.json`` carries just ``manifest_sha256`` and is accepted
    only if it embeds the manifest under ``manifest``.
    """

    loaded = _load(document, "oracle inputs manifest")
    manifest = loaded
    if not isinstance(loaded.get("inputs"), list):
        embedded = loaded.get("manifest")
        manifest = embedded if isinstance(embedded, dict) else {}
    entries = manifest.get("inputs")
    ledgers: dict[str, str] = {}
    if isinstance(entries, list):
        for entry in entries:
            raw = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(raw, str):
                continue
            parts = raw.replace("\\", "/").split("/")
            if parts[-1] != ORACLE_LEDGER_BASENAME:
                continue
            sha256 = entry.get("sha256")
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise MaterializationError("oracle ledger input is not sha256-pinned")
            key = raw.replace("\\", "/")
            if ledgers.get(key, sha256) != sha256:
                raise MaterializationError(
                    f"oracle manifest names {raw} twice with different sha256: "
                    "conflicting duplicate ledger entry"
                )
            ledgers[key] = sha256
    if not ledgers:
        raise MaterializationError(
            f"oracle document {document} names no ledger input "
            f"({ORACLE_LEDGER_BASENAME}); --oracle-manifest takes the acceptance "
            "inputs MANIFEST file (a receipt is accepted only if it embeds that "
            "manifest; parent-execution-receipt.json holds only manifest_sha256 "
            "and is refused)"
        )
    if len(ledgers) != 1:
        raise MaterializationError("oracle document names more than one ledger input")
    raw, sha256 = next(iter(ledgers.items()))
    segments = raw.replace("\\", "/").split("/")
    # A root-relative "provider-ledger.json" has no folder: accept it on sha alone.
    folder = segments[-2] if len(segments) > 1 else ""
    is_generation = re.fullmatch(r"[0-9a-f]{64}", folder) is not None
    return {
        "generation_id": folder if is_generation else None,
        "generation_id_checked": is_generation,
        "sha256": sha256,
    }


def _attended_oracle_ledger_join(
    source_phase: Mapping[str, Any],
    oracle_document: Path | None,
    *,
    declares_acceptance: bool,
) -> dict[str, Any] | None:
    """Join the oracle's ledger identity to the ledger the run (and so report and panel) consumes.

    Report and panel read the provider ledger the source phase admitted, so the source
    phase's catalog identity is the ledger they consume. Without an oracle document a run
    that declares acceptance is reported NOT_CHECKED, loudly: it is not a pass.
    """

    if oracle_document is None:
        if not declares_acceptance:
            return None
        print(
            "attended oracle-to-run ledger join: NOT_CHECKED (no --oracle-manifest "
            "supplied for a run that declares acceptance)",
            file=sys.stderr,
        )
        return {"status": "NOT_CHECKED", "reason": "no --oracle-manifest supplied"}
    oracle = _attended_oracle_ledger_identity(Path(oracle_document).resolve())
    catalog = source_phase.get("provider_ledger_catalog")
    run_sha = catalog.get("sha256") if isinstance(catalog, dict) else None
    run_generation = source_phase.get("provider_ledger_generation_id")
    if oracle["sha256"] != run_sha or (
        oracle["generation_id_checked"] and oracle["generation_id"] != run_generation
    ):
        raise MaterializationError(
            "oracle ledger identity differs from the ledger the run consumed: "
            f"oracle generation {oracle['generation_id'] or '(not a generation folder)'} "
            f"(catalog sha256 {oracle['sha256']}) but run generation "
            f"{run_generation} (catalog sha256 {run_sha})"
        )
    return {"status": "MATCH", **oracle}


def _attended_pin_matrix(
    run_ledger_root: Path,
    *,
    allocation_root: Path | None = None,
    dependence_ledger_root: Path | None = None,
    baseline_root: Path | None = None,
    attribution_root: Path | None = None,
) -> list[dict[str, str]]:
    """Refuse a retained identity that binds a different provider ledger than the run."""

    import check_retained_authority_pins as authority_pins

    retained = {
        role: path
        for role, path in (
            ("allocation", allocation_root),
            ("baseline", baseline_root),
            ("attribution", attribution_root),
            ("dependence", dependence_ledger_root),
        )
        if path is not None
    }
    try:
        return authority_pins.check_ledger_pin_matrix(
            run_ledger_root, retained=retained
        )
    except authority_pins.LedgerPinMismatch as exc:
        raise MaterializationError(str(exc)) from exc


def _attended_preflight(args: argparse.Namespace) -> dict[str, Any]:
    """Bind schema-1 initial selection before a release-builder child may run."""

    basic = _attended_basic_preflight(args)
    repo_root = cast(Path, basic["repo_root"])
    python = cast(Path, basic["python"])
    workspace_root = cast(Path, basic["workspace_root"])
    output_root = cast(Path, basic["output_root"])
    retained_cache_root = cast(Path, basic["retained_cache_root"])
    retained_cache_base = cast(Path | None, basic.get("retained_cache_base"))
    runtime = cast(Mapping[str, Any], basic["runtime"])
    portfolio_root = cast(Path, basic["portfolio_root"])
    requested_as_of = cast(str, basic["requested_as_of"])
    source_run = Path(args.source_run).resolve()
    source = _load(source_run, "attended source run")
    if (
        set(source)
        != {
            "schema_version",
            "as_of_date",
            "release_commit",
            "artifacts",
            "attended",
        }
        or source.get("schema_version") != 1
    ):
        raise MaterializationError("attended source-run schema differs")
    source_as_of = _text(source.get("as_of_date"), "attended source as_of_date")
    try:
        date.fromisoformat(source_as_of)
    except ValueError as exc:
        raise MaterializationError("attended source as-of date is invalid") from exc
    if source_as_of != requested_as_of:
        raise MaterializationError(
            "attended requested as-of differs from source selection"
        )
    release_commit = _text(source.get("release_commit"), "attended release_commit")
    if len(release_commit) != 40:
        raise MaterializationError("attended release commit differs")
    try:
        _release_identity(repo_root, release_commit)
    except MaterializationError as exc:
        raise MaterializationError("attended release commit differs") from exc
    _attended_artifacts(source.get("artifacts"))
    attended = source.get("attended")
    expected_attended = {
        "schema_version",
        "release_recipe",
        "history_generation",
        "allocation_generation",
        "financial_input_contract",
        "required_stages",
        "predecessor_spec",
    }
    allowed_attended = (
        expected_attended,
        expected_attended | {"data_clock"},
        expected_attended | {"data_clock", "source_clock"},
    )
    if not isinstance(attended, dict) or set(attended) not in allowed_attended:
        raise MaterializationError("attended extension schema differs")
    if attended.get("schema_version") != 1:
        raise MaterializationError("attended extension version differs")
    data_clock = attended.get("data_clock")
    if data_clock is not None and (
        not isinstance(data_clock, dict)
        or set(data_clock)
        != {
            "latest_common_eod",
            "selected_financial_cutoff",
            "limiting_inputs",
            "source_data_as_of",
        }
        or data_clock.get("selected_financial_cutoff") != requested_as_of
        or not isinstance(data_clock.get("limiting_inputs"), list)
        or not data_clock["limiting_inputs"]
        or not isinstance(data_clock.get("source_data_as_of"), list)
    ):
        raise MaterializationError("attended data clock differs")
    if data_clock is not None:
        try:
            date.fromisoformat(
                _text(data_clock["latest_common_eod"], "attended latest common EOD")
            )
        except ValueError as exc:
            raise MaterializationError("attended data clock differs") from exc
    source_clock = attended.get("source_clock")
    if source_clock is not None:
        clock_path = _validate_file_definition(source_clock, "attended source clock")
        try:
            import portfolio_report as report

            if report._validated_source_clock(clock_path) != data_clock:  # noqa: SLF001
                raise ValueError("clock differs")
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise MaterializationError("attended source clock differs") from exc
    if getattr(args, "preparation_spec", None) is not None and data_clock is None:
        raise MaterializationError("attended preparation data clock is absent")
    required_stages = attended.get("required_stages")
    if required_stages != list(ATTENDED_STAGES):
        raise MaterializationError("attended required stage graph differs")
    recipe_path = _validate_file_definition(
        attended["release_recipe"], "attended release recipe"
    )
    history_root = _validate_directory_definition(
        attended["history_generation"], "attended history generation"
    )
    allocation_root = _validate_directory_definition(
        attended["allocation_generation"], "attended allocation generation"
    )
    financial_input_contract = _validate_file_definition(
        attended["financial_input_contract"], "attended financial input contract"
    )
    import portfolio_release_builder as release

    recipe = release._recipe(recipe_path)  # noqa: SLF001
    if (
        recipe["as_of_date"] != requested_as_of
        or recipe["release_commit"] != release_commit
    ):
        raise MaterializationError("attended release recipe identity differs")
    selected_definition = recipe.get("dependence_selected_inputs")
    if not isinstance(selected_definition, dict):
        raise MaterializationError("attended selected dependence inputs are missing")
    selected_path = _validate_file_definition(
        selected_definition, "attended selected dependence inputs"
    )
    selected = _load(selected_path, "attended selected dependence inputs")
    try:
        selected_history = _absolute(
            selected["history_generation_root"], "attended selected history root"
        )
        selected_allocation = _absolute(
            selected["allocation_generation_root"], "attended selected allocation root"
        )
        selected_contract = _absolute(
            selected["financial_input_contract_path"],
            "attended selected financial input contract",
        )
    except KeyError as exc:
        raise MaterializationError(
            "attended selected dependence inputs differ"
        ) from exc
    if (
        selected_history != history_root
        or selected_allocation != allocation_root
        or selected_contract != financial_input_contract
    ):
        raise MaterializationError("attended selected dependence authority differs")
    _epoch_manifest_generation_definition(recipe)
    provider_definition = recipe.get("provider_ledger_generation")
    if not isinstance(provider_definition, dict):
        raise MaterializationError("attended provider generation is absent")
    run_ledger_root = _absolute(
        provider_definition.get("path"), "attended provider generation"
    )
    # Cheap identity joins first: a retained input pinned to another ledger refuses here
    # in seconds, not after the provider admission replay or a stage.
    _attended_pin_matrix(
        run_ledger_root,
        allocation_root=allocation_root,
        dependence_ledger_root=(
            _absolute(
                selected["provider_ledger_generation_root"],
                "attended selected dependence ledger root",
            )
            if "provider_ledger_generation_root" in selected
            else None
        ),
    )
    source_phase = validate_source_phase(
        requested_as_of=requested_as_of,
        provider_ledger_generation_root=run_ledger_root,
        financial_input_contract_path=financial_input_contract,
    )
    try:
        # The provider admission replay is intentionally only one source gate.
        # Run the normal selected-history reader before a child can reach any
        # calculation, rather than deferring it to attribution or analytics.
        release._history_contract_path(recipe)  # noqa: SLF001
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MaterializationError(
            "attended selected history input QC differs"
        ) from exc
    predecessor = _attended_predecessor(attended["predecessor_spec"], portfolio_root)
    financial_inputs = release._financial_epoch_inputs(recipe)  # noqa: SLF001
    if financial_inputs is not None:
        try:
            release._validate_stage_financial_arguments(  # noqa: SLF001
                recipe,
                manifest_ledger=financial_inputs["ledger"],
                manifest_ledger_identity=financial_inputs["ledger_identity"],
                manifest_classified_flows=financial_inputs["classified_flows"],
                manifest_flow_coverage=financial_inputs["flow_coverage"],
                manifest_admission_policy=financial_inputs["admission_policy"],
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise MaterializationError(
                "attended selected public input QC differs"
            ) from exc
    financial_stage_inputs = _attended_stage_financial_inputs(args, release)
    qa_dir = (
        _validate_directory_definition(
            _directory_definition(Path(args.qa_dir).resolve()), "attended panel QA"
        )
        if args.qa_dir is not None
        else None
    )
    request_identity = _attended_request_identity(
        repo_root=repo_root,
        python=python,
        workspace_root=workspace_root,
        output_root=output_root,
        retained_cache_root=retained_cache_root,
        requested_as_of=requested_as_of,
        evaluated_on=args.evaluated_on,
        source_run=source_run,
        financial_stage_inputs=financial_stage_inputs,
        source_phase=source_phase,
        runtime=runtime,
        retained_cache_base=retained_cache_base,
    )
    return {
        "financial_inputs": financial_inputs or {},
        "financial_stage_inputs": financial_stage_inputs,
        "portfolio_root": portfolio_root,
        "predecessor_spec": predecessor,
        "python": python,
        "qa_dir": qa_dir,
        "recipe": recipe_path,
        "repo_root": repo_root,
        "request_identity": request_identity,
        "required_stages": ATTENDED_STAGES,
        "retained_cache_base": retained_cache_base,
        "retained_cache_root": retained_cache_root,
        "source_phase": source_phase,
        "source_run": source_run,
        "workspace_root": workspace_root,
    }


def _attended_run_receipt(output_root: Path) -> Path:
    return output_root / "attended-run.json"


def _attended_qa_receipt(output_root: Path) -> Path:
    return output_root / "attended-qa.json"


def _attended_source_phase_receipt(output_root: Path) -> Path:
    return output_root / "source-phase-qc.json"


def _attended_execution_observations_receipt(output_root: Path) -> Path:
    return output_root / "attended-execution-observations.json"


def _execution_timestamp() -> str:
    """Record the actual UTC instant without contributing to financial identity."""

    return datetime.now(UTC).isoformat()


ATTENDED_ATTEMPT_KEYS: Final = frozenset({"attempt", "pid", "started_at", "status"})
ATTENDED_ATTEMPT_OPTIONAL_KEYS: Final = frozenset({"ended_at", "reason", "storage"})


def _attended_legacy_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Map a schema-1/2 attempt onto explicit run states; it recorded no PID."""

    status = attempt.get("status")
    required = (
        {"attempt", "started_at", "status"}
        if status in {"RUNNING", "INCOMPLETE"}
        else {"attempt", "ended_at", "started_at", "status"}
    )
    if (
        set(attempt) != required
        or status not in run_storage.LEGACY_ATTEMPT_STATES
        or not isinstance(attempt.get("started_at"), str)
    ):
        raise MaterializationError("attended execution observations differ")
    mapped = {**attempt, "pid": None, "status": run_storage.attempt_state(status)}
    return dict(mapped)


def _attended_current_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one schema-3 attempt: an explicit run state and the runner's PID."""

    keys = set(attempt)
    status = attempt.get("status")
    pid = attempt.get("pid")
    if (
        not ATTENDED_ATTEMPT_KEYS <= keys
        or not keys <= ATTENDED_ATTEMPT_KEYS | ATTENDED_ATTEMPT_OPTIONAL_KEYS
        or status not in run_storage.ATTEMPT_STATES
        or not isinstance(attempt.get("started_at"), str)
        or (pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)))
        or (status == "RUNNING" and "ended_at" in attempt)
        or (status in {"PASSED", "FAILED"} and "ended_at" not in attempt)
        or ("reason" in attempt and not isinstance(attempt["reason"], str))
        or ("storage" in attempt and not isinstance(attempt["storage"], dict))
    ):
        raise MaterializationError("attended execution observations differ")
    return dict(attempt)


def _attended_execution_observations(
    output_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Read prior operational attempts, rejecting malformed timing evidence.

    Attempts carry the explicit run states RUNNING, CANCELLED, FAILED and PASSED.
    Receipts written before those states existed are read with COMPLETED mapped to
    PASSED and INCOMPLETE (an attempt its process abandoned) mapped to CANCELLED.
    """

    receipt = _attended_execution_observations_receipt(output_root)
    if not receipt.exists():
        return {"execution_attempts": [], "preparation_attempts": []}
    value = _load(receipt, "attended execution observations")
    schema = value.get("schema_version")
    values: dict[str, object]
    if schema == 1 and set(value) == {"attempts", "schema_version"}:
        values = {
            "execution_attempts": value.get("attempts"),
            "preparation_attempts": [],
        }
    elif schema in {2, 3} and set(value) == {
        "execution_attempts",
        "preparation_attempts",
        "schema_version",
    }:
        values = {
            "execution_attempts": value.get("execution_attempts"),
            "preparation_attempts": value.get("preparation_attempts"),
        }
    else:
        raise MaterializationError("attended execution observations differ")
    validated: dict[str, list[dict[str, Any]]] = {}
    for phase, attempts in values.items():
        if not isinstance(attempts, list):
            raise MaterializationError("attended execution observations differ")
        phase_attempts: list[dict[str, Any]] = []
        for expected_attempt, recorded in enumerate(attempts, start=1):
            if (
                not isinstance(recorded, dict)
                or recorded.get("attempt") != expected_attempt
            ):
                raise MaterializationError("attended execution observations differ")
            attempt = (
                _attended_current_attempt(recorded)
                if schema == 3
                else _attended_legacy_attempt(recorded)
            )
            try:
                started = datetime.fromisoformat(cast(str, attempt["started_at"]))
                ended = (
                    datetime.fromisoformat(cast(str, attempt["ended_at"]))
                    if "ended_at" in attempt
                    else None
                )
            except ValueError as exc:
                raise MaterializationError(
                    "attended execution observations differ"
                ) from exc
            if started.tzinfo is None or (ended is not None and ended.tzinfo is None):
                raise MaterializationError("attended execution observations differ")
            phase_attempts.append(dict(attempt))
        validated[phase] = phase_attempts
    return validated


def _attended_replace_execution_observations(
    output_root: Path, attempts: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    """Atomically preserve every operational attempt outside financial cache state."""

    receipt = _attended_execution_observations_receipt(output_root)
    if set(attempts) != {"execution_attempts", "preparation_attempts"}:
        raise MaterializationError("attended execution observations differ")
    value = {
        "execution_attempts": [
            dict(attempt) for attempt in attempts["execution_attempts"]
        ],
        "preparation_attempts": [
            dict(attempt) for attempt in attempts["preparation_attempts"]
        ],
        "schema_version": 3,
    }
    if not receipt.exists():
        _write_exact(receipt, canonical_json_bytes(value))
        return
    if receipt.is_symlink() or not receipt.is_file():
        raise MaterializationError(
            "attended execution observations are not a regular file"
        )
    temporary = receipt.with_name(f".{receipt.name}.update")
    _write_exact(temporary, canonical_json_bytes(value))
    try:
        os.replace(temporary, receipt)
    except OSError as exc:
        raise MaterializationError(
            "attended execution observations cannot be updated"
        ) from exc


def _attended_start_attempt(output_root: Path, phase: str) -> int:
    """Begin one operational phase attempt without altering financial identity."""

    if phase not in {"execution", "preparation"}:
        raise MaterializationError("attended execution phase differs")
    attempts = _attended_execution_observations(output_root)
    key = f"{phase}_attempts"
    phase_attempts = attempts[key]
    if phase_attempts and phase_attempts[-1]["status"] == "RUNNING":
        # Its process stopped without recording an outcome: it was interrupted, and
        # the true ending instant is unknown, so none is invented.
        phase_attempts[-1]["status"] = "CANCELLED"
    attempt = len(phase_attempts) + 1
    phase_attempts.append(
        {
            "attempt": attempt,
            "pid": os.getpid(),
            "started_at": _execution_timestamp(),
            "status": "RUNNING",
        }
    )
    _attended_replace_execution_observations(output_root, attempts)
    run_storage.write_run_state(output_root, "RUNNING", phase=phase, attempt=attempt)
    return attempt


def _attended_running_attempt(
    attempts: Mapping[str, list[dict[str, Any]]], phase: str, attempt: int
) -> dict[str, Any]:
    if phase not in {"execution", "preparation"}:
        raise MaterializationError("attended execution phase differs")
    phase_attempts = attempts[f"{phase}_attempts"]
    if (
        not phase_attempts
        or phase_attempts[-1].get("attempt") != attempt
        or phase_attempts[-1].get("status") != "RUNNING"
    ):
        raise MaterializationError("attended execution attempt differs")
    return phase_attempts[-1]


def _attended_finish_attempt(
    output_root: Path,
    phase: str,
    attempt: int,
    status: str,
    *,
    reason: str | None = None,
) -> None:
    """Terminal outcomes receive a real ending instant; interruption remains open."""

    if status not in run_storage.TERMINAL_STATES:
        raise MaterializationError("attended execution status differs")
    attempts = _attended_execution_observations(output_root)
    current = _attended_running_attempt(attempts, phase, attempt)
    current["ended_at"] = _execution_timestamp()
    current["status"] = status
    if reason is not None:
        current["reason"] = reason
    _attended_replace_execution_observations(output_root, attempts)
    # A passed preparation hands over to execution in the same process, so the run
    # stays RUNNING; every other terminal outcome ends the runner's work.
    if phase == "execution" or status != "PASSED":
        run_storage.write_run_state(
            output_root, status, phase=phase, attempt=attempt, reason=reason
        )


def _attended_record_attempt_storage(
    output_root: Path, phase: str, attempt: int, storage: Mapping[str, Any]
) -> None:
    """Record the bytes a running attempt has written so far, stage by stage."""

    attempts = _attended_execution_observations(output_root)
    _attended_running_attempt(attempts, phase, attempt)["storage"] = _canonical_clone(
        storage
    )
    _attended_replace_execution_observations(output_root, attempts)


def _attended_start_execution_attempt(output_root: Path) -> int:
    return _attended_start_attempt(output_root, "execution")


def _attended_finish_execution_attempt(
    output_root: Path, attempt: int, status: str, *, reason: str | None = None
) -> None:
    _attended_finish_attempt(output_root, "execution", attempt, status, reason=reason)


def _attended_start_preparation_attempt(output_root: Path) -> int:
    return _attended_start_attempt(output_root, "preparation")


def _attended_finish_preparation_attempt(
    output_root: Path, attempt: int, status: str, *, reason: str | None = None
) -> None:
    _attended_finish_attempt(output_root, "preparation", attempt, status, reason=reason)


def _attended_bind_qa(
    preflight: Mapping[str, Any], output_root: Path, completed: Sequence[str]
) -> None:
    """Bind QA when it is available, without rewriting an incomplete run's inputs."""

    qa_dir = cast(Path | None, preflight["qa_dir"])
    receipt = _attended_qa_receipt(output_root)
    expected = _directory_definition(qa_dir) if qa_dir is not None else None
    if receipt.exists():
        observed = _load(receipt, "attended QA receipt")
        if expected is None and "panel" in completed:
            # The completed panel consumed the recorded QA evidence; resuming a
            # finished run needs no --qa-dir, but the receipt must still verify.
            _validate_directory_definition(observed, "attended QA receipt")
            return
        if expected is None or canonical_json_bytes(observed) != canonical_json_bytes(
            expected
        ):
            raise MaterializationError("attended QA identity differs")
        _validate_directory_definition(observed, "attended QA receipt")
        return
    if expected is None:
        return
    if "panel" in completed:
        raise MaterializationError("attended QA identity cannot follow completed panel")
    _write_exact(receipt, canonical_json_bytes(expected))


def _attended_start_or_resume(
    preflight: Mapping[str, Any],
    output_root: Path,
    *,
    resume: bool,
    on_create: Callable[[Path], None] | None = None,
) -> str | None:
    request_identity = preflight["request_identity"]
    if not isinstance(request_identity, dict):
        raise MaterializationError("attended request identity differs")
    receipt = _attended_run_receipt(output_root)
    if resume:
        # Preparation owns the output root and may complete before any
        # downstream receipt exists.  Reuse the established create-only
        # handoff validation for that one first-execution transition, but only
        # before the execution observation ledger shows a downstream attempt.
        # A missing receipt after execution began remains fail-closed.
        if output_root.exists() and not receipt.exists() and not receipt.is_symlink():
            observations = _attended_execution_observations(output_root)
            if observations["execution_attempts"]:
                raise MaterializationError(
                    "attended run receipt is missing after execution started"
                )
            return _attended_start_or_resume(
                preflight, output_root, resume=False, on_create=on_create
            )
        value = _load(receipt, "attended run receipt")
        failed = value.get("failed_stage")
        if (
            set(value)
            not in (
                {"request_identity", "schema_version"},
                {"failed_stage", "request_identity", "schema_version"},
            )
            or value.get("schema_version") not in {1, 2}
            or canonical_json_bytes(value["request_identity"])
            != canonical_json_bytes(request_identity)
        ):
            raise MaterializationError("attended resume identity differs")
        failed_role: str | None = None
        if failed is not None:
            if (
                value.get("schema_version") != 2
                or not isinstance(failed, dict)
                or set(failed) != {"returncode", "role"}
                or failed.get("role") not in preflight["required_stages"]
                or isinstance(failed.get("returncode"), bool)
                or not isinstance(failed.get("returncode"), int)
                or failed["returncode"] == 0
            ):
                raise MaterializationError("attended failed-stage checkpoint differs")
            failed_role = cast(str, failed["role"])
        completed = _attended_completed_stages(
            output_root, preflight["required_stages"], failed_stage=failed_role
        )
        source_phase = preflight.get("source_phase")
        if not isinstance(source_phase, dict) or (
            _load(_attended_source_phase_receipt(output_root), "attended source phase")
            != source_phase
        ):
            raise MaterializationError("attended source phase identity differs")
        _attended_bind_qa(preflight, output_root, completed)
        return failed_role
    if output_root.exists():
        handoff = output_root / "preparation-handoff.json"
        if handoff.is_symlink() or not handoff.is_file():
            raise MaterializationError(
                "attended output root already exists; use --resume"
            )
        expected_stage = {
            flag: _file_definition(path)
            for flag, path in sorted(
                cast(
                    Mapping[str, Path], preflight.get("financial_stage_inputs", {})
                ).items()
            )
        }
        value = _load(handoff, "attended preparation handoff")
        source_run = cast(Path, preflight["source_run"])
        expected = {
            "schema_version": 1,
            "source_run": _file_definition(source_run),
            "financial_stage_inputs": expected_stage,
            "source_phase": preflight["source_phase"],
            "data_clock": _load(source_run, "attended prepared source run")["attended"][
                "data_clock"
            ],
        }
        if value != expected:
            raise MaterializationError("attended preparation handoff differs")
    else:
        output_root.mkdir(parents=True, exist_ok=False)
        if on_create is not None:
            on_create(output_root)
    _write_exact(
        receipt,
        canonical_json_bytes(
            {"schema_version": 1, "request_identity": request_identity}
        ),
    )
    source_phase = preflight.get("source_phase")
    if not isinstance(source_phase, dict) or source_phase.get("status") != "PASS":
        raise MaterializationError("attended source phase is not PASS")
    _write_exact(
        _attended_source_phase_receipt(output_root), canonical_json_bytes(source_phase)
    )
    _attended_bind_qa(preflight, output_root, ())
    return None


def _attended_record_failed_stage(
    output_root: Path,
    request_identity: Mapping[str, Any],
    role: str,
    returncode: int,
) -> None:
    """Preserve a failed child's partial bytes as pending retry evidence."""

    _attended_replace_checkpoint(
        output_root,
        {
            "failed_stage": {"returncode": returncode, "role": role},
            "request_identity": request_identity,
            "schema_version": 2,
        },
    )


def _attended_clear_failed_stage(
    output_root: Path, request_identity: Mapping[str, Any]
) -> None:
    """Return a checkpoint to its completed-only shape after native verification."""

    _attended_replace_checkpoint(
        output_root,
        {"request_identity": request_identity, "schema_version": 1},
    )


def _attended_replace_checkpoint(output_root: Path, value: Mapping[str, Any]) -> None:
    """Atomically update the one mutable attended-run checkpoint."""

    receipt = _attended_run_receipt(output_root)
    if receipt.is_symlink() or not receipt.is_file():
        raise MaterializationError("attended run receipt is not a regular file")
    temporary = receipt.with_name(f".{receipt.name}.update")
    _write_exact(temporary, canonical_json_bytes(dict(value)))
    try:
        os.replace(temporary, receipt)
    except OSError as exc:
        raise MaterializationError("attended run checkpoint cannot be updated") from exc


def _attended_shared_stage_roles(output_root: Path, role: str) -> tuple[str, ...]:
    """Return the other attended roles whose bytes live in this role's stage root.

    `panel-candidate` and `panel` share `stages/panel`, so moving that root aside for one of
    them also removes the other's outputs while its own receipt survives.  Derived from the
    mapping rather than naming the pair, so a future shared root cannot be missed.
    """

    stage_root = _attended_stage_root(output_root, role)
    return tuple(
        other
        for other in ATTENDED_STAGES
        if other != role and _attended_stage_root(output_root, other) == stage_root
    )


def _attended_preserve_failed_stage(output_root: Path, role: str) -> None:
    """Move failed-stage bytes aside before a create-only producer retries."""

    stage_root = _attended_stage_root(output_root, role)
    receipt = output_root / "stage-results" / f"{role}.json"
    shared_receipts = [
        output_root / "stage-results" / f"{other}.json"
        for other in _attended_shared_stage_roles(output_root, role)
    ]
    if (
        not stage_root.exists()
        and not receipt.exists()
        and not any(path.exists() for path in shared_receipts)
    ):
        return
    if stage_root.is_symlink() or (stage_root.exists() and not stage_root.is_dir()):
        raise MaterializationError("attended failed stage root is not a directory")
    if receipt.is_symlink() or (receipt.exists() and not receipt.is_file()):
        raise MaterializationError(
            "attended failed stage receipt is not a regular file"
        )

    evidence_root = output_root / "failed-stage-evidence" / role
    evidence_root.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (evidence_root / f"attempt-{attempt}").exists():
        attempt += 1
    destination = evidence_root / f"attempt-{attempt}"
    destination.mkdir()
    if stage_root.exists():
        os.replace(stage_root, destination / "stage-root")
        stage_root.mkdir(parents=True)
    if receipt.exists():
        os.replace(receipt, destination / "stage-result.json")
    # A receipt whose outputs just moved aside no longer describes anything on disk, so it
    # must not be able to satisfy the completed-stage validation on the retry.
    for path in shared_receipts:
        if path.is_symlink():
            raise MaterializationError(
                "attended shared stage receipt is not a regular file"
            )
        if path.exists():
            os.replace(path, destination / path.name)


def _attended_stage_command(
    preflight: Mapping[str, Any], output_root: Path, role: str
) -> list[str]:
    repo_root = cast(Path, preflight["repo_root"])
    command = [
        str(cast(Path, preflight["python"])),
        str(repo_root / "tools" / "portfolio_release_builder.py"),
        "stage",
        "--recipe",
        str(cast(Path, preflight["recipe"])),
        "--output-root",
        str(output_root),
        "--role",
        role,
        "--portfolio-root",
        str(cast(Path, preflight["portfolio_root"])),
        "--retained-cache-root",
        str(cast(Path, preflight["retained_cache_root"])),
    ]
    financial_inputs = cast(Mapping[str, Path], preflight["financial_inputs"])
    if set(financial_inputs) not in (set(), set(EPOCH_MANIFEST_INPUTS)):
        raise MaterializationError("attended financial input family differs")
    for key, flag in EPOCH_MANIFEST_INPUTS.items():
        if key in financial_inputs:
            command.extend((flag, str(financial_inputs[key])))
    financial_stage_inputs = cast(
        Mapping[str, Path], preflight.get("financial_stage_inputs", {})
    )
    hwm = [financial_stage_inputs.get(flag) for flag in ATTENDED_HWM_INPUTS]
    capital = [financial_stage_inputs.get(flag) for flag in ATTENDED_CAPITAL_INPUTS]
    if any(path is not None for path in hwm) and any(path is None for path in hwm):
        raise MaterializationError("attended financial input family differs")
    if any(path is None for path in capital):
        raise MaterializationError("attended financial input family differs")
    for flag in (*ATTENDED_HWM_INPUTS, *ATTENDED_CAPITAL_INPUTS):
        if (path := financial_stage_inputs.get(flag)) is not None:
            command.extend((flag, str(path)))
    if role == "panel":
        qa_dir = cast(Path | None, preflight["qa_dir"])
        if qa_dir is None:
            raise MaterializationError(
                "attended panel requires produced browser QA evidence: "
                f"{ATTENDED_QA_INSTRUCTIONS}"
            )
        command.extend(("--qa-dir", str(qa_dir)))
    return command


def _attended_execute(
    preflight: Mapping[str, Any],
    output_root: Path,
    *,
    resume: bool,
    storage: run_storage.AttendedStorage | None = None,
) -> tuple[int, dict[str, Any]]:
    failed_stage = _attended_start_or_resume(
        preflight,
        output_root,
        resume=resume,
        on_create=storage.mark_created if storage is not None else None,
    )
    attempt = _attended_start_execution_attempt(output_root)
    measured: dict[str, Any] = {}
    stage_bytes: list[dict[str, Any]] = []

    def record_stage_bytes(role: str) -> None:
        """Record what one child wrote, then refuse the run if it broke the budget."""

        if storage is None:
            return
        row, breach = storage.after_stage(role)
        stage_bytes.append(row)
        _attended_record_attempt_storage(
            output_root, "execution", attempt, {**measured, "stages": stage_bytes}
        )
        if breach is not None:
            raise breach

    try:
        if storage is not None:
            # The overlay is created (or its base re-verified) before any child can
            # read the cache, and the run is measured before any child can write.
            measured["cache"] = storage.open_cache()
            measured["start"] = storage.start()
            _attended_record_attempt_storage(
                output_root, "execution", attempt, {**measured, "stages": stage_bytes}
            )
        if failed_stage is not None:
            _attended_preserve_failed_stage(output_root, failed_stage)
        completed = _attended_completed_stages(
            output_root, preflight["required_stages"], failed_stage=failed_stage
        )
        # An interruption that killed a child before it returned leaves partial bytes with no
        # receipt and no failed_stage, and the create-only producer refuses its own partial
        # directory on the retry.  Three conditions keep this to exactly that case:
        #   - a later attempt, not `resume`, because a plain `--execute` can re-enter a
        #     prepared root the same way;
        #   - nothing sharing the root has completed -- `panel` legitimately runs into the
        #     root `panel-candidate` filled, which is the supported build -> QA -> resume path;
        #   - the root still HOLDS something.  The preserve above recreates an empty root, so
        #     without this an ordinary failed-stage resume would preserve twice and leave an
        #     empty second attempt directory, breaking the one-failure-one-attempt mapping the
        #     evidence directory exists to provide.
        preserved_partial = False
        if attempt > 1:
            for role in preflight["required_stages"]:
                if role in completed:
                    continue
                if any(
                    other in completed
                    for other in _attended_shared_stage_roles(output_root, role)
                ):
                    continue
                stage_root = _attended_stage_root(output_root, role)
                if stage_root.is_dir() and any(stage_root.iterdir()):
                    _attended_preserve_failed_stage(output_root, role)
                    preserved_partial = True
        if preserved_partial:
            completed = _attended_completed_stages(
                output_root, preflight["required_stages"], failed_stage=failed_stage
            )
        for role in preflight["required_stages"]:
            if role in completed:
                continue
            command = _attended_stage_command(preflight, output_root, role)
            result = subprocess.run(
                command,
                cwd=cast(Path, preflight["repo_root"]),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            if result.returncode != 0:
                _attended_record_failed_stage(
                    output_root, preflight["request_identity"], role, result.returncode
                )
                record_stage_bytes(role)
                _attended_finish_execution_attempt(
                    output_root,
                    attempt,
                    "FAILED",
                    reason=f"stage {role} exited {result.returncode}",
                )
                return result.returncode, _attended_view(
                    output_root,
                    preflight["required_stages"],
                    preflight["request_identity"],
                    completed,
                )
            record_stage_bytes(role)
            completed = _attended_completed_stages(
                output_root,
                preflight["required_stages"],
            )
            if role not in completed:
                raise MaterializationError(
                    f"attended {role} child returned success without completed artifacts"
                )
            if failed_stage == role:
                _attended_clear_failed_stage(output_root, preflight["request_identity"])
                failed_stage = None
        if storage is not None:
            end, breach = storage.finish()
            measured["end"] = end
            _attended_record_attempt_storage(
                output_root, "execution", attempt, {**measured, "stages": stage_bytes}
            )
            if breach is not None:
                raise breach
        _attended_finish_execution_attempt(output_root, attempt, "PASSED")
        return 0, _attended_view(
            output_root, preflight["required_stages"], preflight["request_identity"]
        )
    except KeyboardInterrupt:
        # The attempt stays open, as for any interruption: its successor records it
        # CANCELLED without inventing an ending instant. The run state says so now.
        run_storage.write_run_state(
            output_root,
            "CANCELLED",
            phase="execution",
            attempt=attempt,
            reason="interrupted",
        )
        raise
    except Exception as exc:
        _attended_finish_execution_attempt(
            output_root, attempt, "FAILED", reason=str(exc) or type(exc).__name__
        )
        raise


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
    attended_cmd = commands.add_parser(
        "attended",
        help="plan or run one source-bound attended release without publication",
    )
    attended_cmd.add_argument("--repo-root", type=Path, required=True)
    attended_cmd.add_argument("--python", type=Path, required=True)
    attended_cmd.add_argument("--workspace-root", type=Path, required=True)
    attended_cmd.add_argument("--output-root", type=Path, required=True)
    attended_cmd.add_argument("--retained-cache-root", type=Path, required=True)
    attended_cmd.add_argument("--requested-as-of", required=True)
    attended_cmd.add_argument("--evaluated-on")
    attended_cmd.add_argument(
        "--code-identity-scheme",
        choices=(COMMIT_SCHEME, TREE_SCHEME),
        default=COMMIT_SCHEME,
        help=(
            "release code identity: git-commit-v1 binds HEAD (default, legacy); "
            "git-tree-v2 binds HEAD^{tree} so a PR-head candidate can be promoted "
            "after merge behind a mandatory merge attestation"
        ),
    )
    attended_inputs = attended_cmd.add_mutually_exclusive_group(required=True)
    attended_inputs.add_argument("--source-run", type=Path)
    attended_inputs.add_argument("--preparation-spec", type=Path)
    attended_cmd.add_argument("--hwm-governed-result", type=Path)
    attended_cmd.add_argument("--hwm-governed-input-generation", type=Path)
    attended_cmd.add_argument("--hwm-governed-calendar-authority", type=Path)
    attended_cmd.add_argument("--hwm-governed-admission-receipt", type=Path)
    attended_cmd.add_argument("--invested-capital-result", type=Path)
    attended_cmd.add_argument("--invested-capital-input-generation", type=Path)
    attended_cmd.add_argument("--qa-dir", type=Path)
    attended_cmd.add_argument(
        "--oracle-manifest",
        type=Path,
        help=(
            "the acceptance inputs MANIFEST file (a receipt such as "
            "parent-execution-receipt.json is accepted only if it embeds that "
            "manifest); the run refuses when the manifest's ledger sha256 differs "
            "from the ledger the run consumes, and when its ledger folder is a "
            "64-hex generation id that differs from the run's generation"
        ),
    )
    attended_cmd.add_argument(
        "--declares-acceptance",
        action="store_true",
        help=(
            "this run feeds an acceptance receipt: without --oracle-manifest the "
            "oracle-to-run ledger join is reported NOT_CHECKED"
        ),
    )
    attended_cmd.add_argument(
        "--qa-hold",
        action="store_true",
        help=(
            "declare that --execute runs without QA evidence and will stop at the "
            "panel stage until browser QA exists (then --resume --qa-dir)"
        ),
    )
    attended_cmd.add_argument("--resume", action="store_true")
    attended_cmd.add_argument("--execute", action="store_true")
    attended_cmd.add_argument(
        "--retained-cache-base",
        type=Path,
        help=(
            "A shared retained cache the runner seals read-only. --retained-cache-root "
            "then names this run's overlay: the runner builds it from hard links to "
            "the base, so no cache byte is copied and every entry this run adds lands "
            "in the overlay alone. Omit it to use --retained-cache-root directly."
        ),
    )
    attended_cmd.add_argument(
        "--max-run-growth",
        default=run_storage.DEFAULT_MAX_RUN_GROWTH,
        help=(
            "Refuse the run once its output and retained-cache roots have grown by "
            "more than this since the run root was created (default "
            f"{run_storage.DEFAULT_MAX_RUN_GROWTH}; B, KB, MB, GB, KiB, MiB, GiB)."
        ),
    )
    attended_cmd.add_argument(
        "--max-scratch",
        default=run_storage.DEFAULT_MAX_SCRATCH,
        help=(
            "Refuse to start, or to finish, while the scratch root holds more than "
            f"this (default {run_storage.DEFAULT_MAX_SCRATCH})."
        ),
    )
    attended_cmd.add_argument(
        "--scratch-root",
        type=Path,
        help="The scratch root the --max-scratch budget measures (default: the "
        "output root's parent).",
    )
    retire_cmd = commands.add_parser(
        "retire",
        help=(
            "list, or with --apply remove, superseded attended run roots and overlays "
            "this runner created"
        ),
    )
    retire_cmd.add_argument("--runs-root", type=Path, required=True)
    retire_cmd.add_argument("--keep", type=int, default=run_storage.DEFAULT_RETIRE_KEEP)
    retire_cmd.add_argument(
        "--current",
        type=Path,
        help="The current candidate run root (default: the newest PASSED run root).",
    )
    retire_cmd.add_argument("--apply", action="store_true")
    wait_cmd = commands.add_parser(
        "wait",
        help="wait for an attended run's recorded runner process to exit",
    )
    wait_cmd.add_argument("run_root", type=Path)
    wait_cmd.add_argument("--timeout", type=float)
    wait_cmd.add_argument("--poll", type=float, default=5.0)
    semantic_cmd = commands.add_parser("semantic")
    semantic_cmd.add_argument("--recipe", type=Path, required=True)
    semantic_cmd.add_argument("--output", type=Path, required=True)
    semantic_cmd.add_argument("--financial-spec", type=Path)
    recipe_cmd = commands.add_parser("recipe")
    recipe_cmd.add_argument("--vertical-recipe", type=Path, required=True)
    recipe_cmd.add_argument("--proof-root", type=Path, required=True)
    recipe_cmd.add_argument("--qa-dir", type=Path, required=True)
    recipe_cmd.add_argument("--pipeline-id", required=True)
    recipe_cmd.add_argument("--financial-mvp-contract", type=Path, required=True)
    recipe_cmd.add_argument("--financial-mvp-source-matrix", type=Path, required=True)
    recipe_cmd.add_argument("--correction-registry", type=Path)
    recipe_cmd.add_argument("--rederive-receipt-registry", type=Path)
    recipe_cmd.add_argument("--financial-input-contract", type=Path)
    recipe_cmd.add_argument("--output", type=Path, required=True)
    spec_cmd = commands.add_parser("spec")
    spec_cmd.add_argument("--recipe", type=Path, required=True)
    spec_cmd.add_argument("--output-root", type=Path, required=True)
    spec_cmd.add_argument("--spec", type=Path, required=True)
    spec_cmd.add_argument("--hwm-governed-result", type=Path)
    spec_cmd.add_argument("--hwm-governed-input-generation", type=Path)
    spec_cmd.add_argument("--hwm-governed-calendar-authority", type=Path)
    spec_cmd.add_argument("--hwm-governed-admission-receipt", type=Path)
    spec_cmd.add_argument("--invested-capital-result", type=Path)
    spec_cmd.add_argument("--invested-capital-input-generation", type=Path)
    spec_cmd.add_argument(
        "--financial-input-contract",
        type=Path,
        help=(
            "The contract the provider ledger was ADMITTED under, when it differs from the product contract. Defaults to the product contract."
        ),
    )
    spec_cmd.add_argument(
        "--portfolio-root",
        type=Path,
        required=True,
        help=(
            "The production portfolio root. Every schema-3 recipe is financial "
            "and `build_spec` refuses without this root, so requiring it here "
            "refuses at parse time rather than after the recipe is read. The "
            "workbook and panel roles prove their stage output is disjoint "
            "from it before publishing governed HWM."
        ),
    )
    spec_cmd.add_argument("--retained-cache-root", type=Path)
    production_cmd = commands.add_parser("production-spec")
    production_cmd.add_argument("--recipe", type=Path, required=True)
    production_cmd.add_argument("--prepared-root", type=Path, required=True)
    production_cmd.add_argument("--semantic-gate", type=Path, required=True)
    production_cmd.add_argument("--portfolio-root", type=Path, required=True)
    production_cmd.add_argument("--state-root", type=Path, required=True)
    production_cmd.add_argument("--spec", type=Path, required=True)
    production_cmd.add_argument("--hwm-governed-result", type=Path)
    production_cmd.add_argument("--hwm-governed-input-generation", type=Path)
    production_cmd.add_argument("--hwm-governed-calendar-authority", type=Path)
    production_cmd.add_argument("--hwm-governed-admission-receipt", type=Path)
    production_cmd.add_argument(
        "--predecessor-repair-gate",
        type=Path,
        help=(
            "Reviewed predecessor-authority-repair gate admitting the artifact "
            "inventory drift of an already published generation that an external "
            "agent rewrote. The pipeline re-derives the drift and refuses any gate "
            "that does not match it exactly."
        ),
    )
    production_cmd.add_argument(
        "--provider-generations",
        type=Path,
        help=(
            "JSON list of {root_id, source_dir} naming the exact generation each "
            "provider root and the epoch-manifest root depends on. Each must equal "
            "the selected provider ledger's own binding by id and hash; the roots "
            "then join the release as targets (REL-PROVIDER-ROOTS-001)."
        ),
    )
    production_cmd.add_argument(
        "--financial-input-contract",
        type=Path,
        help=(
            "The contract the provider ledger was ADMITTED under, when it differs from the product contract. Defaults to the product contract."
        ),
    )
    production_cmd.add_argument(
        "--financial-artifacts",
        type=Path,
        required=True,
        help="JSON array of existing ArtifactNode definitions for actual outcomes and their producer inputs",
    )
    return parser


WAIT_EXIT_CODES: Final = {"PASSED": 0, "FAILED": 1, "CANCELLED": 2}
WAIT_TIMEOUT_EXIT_CODE: Final = 3


def _attended_storage(args: argparse.Namespace) -> run_storage.AttendedStorage:
    """The storage gates for one executing attended runner; nothing is written here."""

    output_root = Path(args.output_root).resolve()
    base = getattr(args, "retained_cache_base", None)
    scratch = getattr(args, "scratch_root", None)
    return run_storage.AttendedStorage(
        output_root=output_root,
        cache_root=Path(args.retained_cache_root).resolve(),
        base=Path(base).resolve() if base is not None else None,
        scratch_root=(
            Path(scratch).resolve() if scratch is not None else output_root.parent
        ),
        budget=run_storage.StorageBudget.parse(
            getattr(args, "max_run_growth", run_storage.DEFAULT_MAX_RUN_GROWTH),
            getattr(args, "max_scratch", run_storage.DEFAULT_MAX_SCRATCH),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    storage: run_storage.AttendedStorage | None = None
    try:
        exit_code = 0
        if args.command == "retire":
            result = run_storage.retire_run_roots(
                args.runs_root, keep=args.keep, current=args.current, apply=args.apply
            )
        elif args.command == "wait":
            result = run_storage.wait_for_run(
                args.run_root, timeout=args.timeout, poll=args.poll
            )
            exit_code = (
                WAIT_TIMEOUT_EXIT_CODE
                if result["timed_out"]
                else WAIT_EXIT_CODES.get(str(result["state"]), 1)
            )
        elif args.command == "attended":
            if args.resume and not args.execute:
                raise MaterializationError("attended --resume requires --execute")
            if (
                args.execute
                and args.declares_acceptance
                and args.oracle_manifest is None
            ):
                # NOT_CHECKED has no mechanism behind it, so an executing acceptance
                # run may not omit the oracle; plan-only still reports NOT_CHECKED.
                raise MaterializationError(
                    "attended --execute with --declares-acceptance requires "
                    "--oracle-manifest (the acceptance inputs manifest)"
                )

            def qa_gate() -> None:
                # The hold concerns the stages this run will still execute: a resume
                # whose panel stage is already complete needs no declaration.
                completed_stages: list[str] = []
                if args.resume:
                    try:
                        completed_stages = _attended_completed_stages(
                            Path(args.output_root).resolve(), ATTENDED_STAGES
                        )
                    except MaterializationError:
                        # An unreadable receipt is reported with its own message when
                        # the run resumes; here every stage still counts as remaining.
                        completed_stages = []
                _attended_require_qa_plan(
                    [
                        stage
                        for stage in ATTENDED_STAGES
                        if stage not in completed_stages
                    ],
                    qa_dir=args.qa_dir,
                    qa_hold=args.qa_hold,
                )

            # A resume reads stage receipts to know what remains, so its gate waits
            # for the location and environment admission; a cold run reads nothing.
            if args.execute and not args.resume:
                qa_gate()
            oracle_join: dict[str, Any] | None = None
            if args.execute:
                storage = _attended_storage(args)
            if args.preparation_spec is not None:
                if args.evaluated_on is None:
                    raise MaterializationError(
                        "attended --preparation-spec requires --evaluated-on"
                    )
                # Source planning/preparation can create private handoff bytes,
                # so retain the attended command's normal environment admission
                # ahead of either branch.
                _attended_basic_preflight(args)
                if args.execute and args.resume:
                    qa_gate()
                import portfolio_attended_preparation as preparation

                if not args.execute:
                    result = preparation.plan_attended_inputs(
                        preparation_spec_path=args.preparation_spec,
                        requested_as_of=args.requested_as_of,
                        evaluated_on=args.evaluated_on,
                    )
                    result = {
                        **result,
                        "qa": _attended_qa_plan(ATTENDED_STAGES, args.qa_dir),
                    }
                    plan_join = _attended_oracle_ledger_join(
                        result.get("source_phase") or {},
                        args.oracle_manifest,
                        declares_acceptance=args.declares_acceptance,
                    )
                    if plan_join is not None:
                        result["oracle_ledger_join"] = plan_join
                    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
                    return 0
                # The cheap declaration-only validation (pin matrix, source phase) and
                # the oracle join run BEFORE storage is admitted or any preparation
                # stage can create output, on every execute path.
                planned = preparation.plan_attended_inputs(
                    preparation_spec_path=args.preparation_spec,
                    requested_as_of=args.requested_as_of,
                    evaluated_on=args.evaluated_on,
                )
                oracle_join = _attended_oracle_ledger_join(
                    planned.get("source_phase") or {},
                    args.oracle_manifest,
                    declares_acceptance=args.declares_acceptance,
                )
                storage = storage or _attended_storage(args)
                storage.admit(resume=args.resume)
                prepared = preparation.prepare_attended_inputs(
                    preparation_spec_path=args.preparation_spec,
                    output_root=Path(args.output_root).resolve(),
                    requested_as_of=args.requested_as_of,
                    evaluated_on=args.evaluated_on,
                    release_commit=_git(
                        REPOSITORY_ROOT,
                        "rev-parse",
                        "HEAD^{tree}"
                        if getattr(args, "code_identity_scheme", COMMIT_SCHEME)
                        == TREE_SCHEME
                        else "HEAD",
                    ),
                    resume=args.resume,
                    on_create=storage.mark_created,
                )
                source_run = prepared.get("source_run")
                financial = prepared.get("financial_stage_inputs")
                if not isinstance(source_run, Path) or not isinstance(
                    financial, Mapping
                ):
                    raise MaterializationError("attended preparation handoff differs")
                args.source_run = source_run
                for flag, path in financial.items():
                    if flag not in {
                        *ATTENDED_HWM_INPUTS,
                        *ATTENDED_CAPITAL_INPUTS,
                    } or not isinstance(path, Path):
                        raise MaterializationError(
                            "attended preparation financial bindings differ"
                        )
                    setattr(args, flag[2:].replace("-", "_"), path)
            preflight = _attended_preflight(args)
            if args.execute and args.resume and args.preparation_spec is None:
                qa_gate()
            # Join against the FINAL admitted source phase, the ledger the run will
            # actually consume, even when a planned phase already matched: the planned
            # join is only the cheap pre-admission refusal.
            oracle_join = _attended_oracle_ledger_join(
                preflight.get("source_phase") or {},
                args.oracle_manifest,
                declares_acceptance=args.declares_acceptance,
            )
            if args.execute:
                storage = storage or _attended_storage(args)
                if args.preparation_spec is None:
                    storage.admit(resume=args.resume)
                exit_code, result = _attended_execute(
                    preflight,
                    Path(args.output_root).resolve(),
                    resume=args.resume,
                    storage=storage,
                )
            else:
                result = _attended_view(
                    Path(args.output_root).resolve(),
                    preflight["required_stages"],
                    preflight["request_identity"],
                    qa_dir=preflight["qa_dir"],
                )
            if oracle_join is not None:
                result["oracle_ledger_join"] = oracle_join
        elif args.command == "acquire":
            result = acquire(args.recipe, args.output_dir)
        elif args.command == "authority":
            result = authority(args.recipe, args.source_run, args.output_dir)
        elif args.command == "semantic":
            result = semantic(args.recipe, args.output, args.financial_spec)
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
                args.financial_input_contract,
            )
        elif args.command == "spec":
            result = build_spec(
                args.recipe,
                args.output_root,
                args.spec,
                args.hwm_governed_result,
                args.hwm_governed_input_generation,
                args.hwm_governed_calendar_authority,
                args.hwm_governed_admission_receipt,
                args.financial_input_contract,
                args.portfolio_root,
                args.invested_capital_result,
                args.invested_capital_input_generation,
                args.retained_cache_root,
            )
        else:
            result = build_production_spec(
                args.recipe,
                args.prepared_root,
                args.semantic_gate,
                args.portfolio_root,
                args.state_root,
                args.spec,
                args.hwm_governed_result,
                args.hwm_governed_input_generation,
                args.hwm_governed_calendar_authority,
                args.hwm_governed_admission_receipt,
                args.financial_artifacts,
                args.financial_input_contract,
                args.predecessor_repair_gate,
                args.provider_generations,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        if storage is not None:
            storage.fail_if_open(str(exc) or type(exc).__name__)
        print(f"portfolio-pipeline: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
