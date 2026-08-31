#!/usr/bin/env python3
"""Build and publish a private, hash-anchored portfolio provenance catalog.

The repository contains this validator/builder, never the real specification or
catalog. Every source file is explicit; this tool never walks a source tree.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    stage_generation,
)


SPEC_SCHEMA_VERSION: Final = 2
CATALOG_SCHEMA_VERSION: Final = 2
STRATEGY_VERSION_CATALOG_SCHEMA_VERSION: Final = 2
BUILDER_VERSION: Final = "2.0.1"
POLICY_VERSION: Final = "PORT-PERF-1"
RETAINED_STRATEGY_COUNT: Final = 9
SYSTEMATIC_POOL_ID: Final = "systematic-capital"
RESERVE_POOL_ID: Final = "reserve-capital"

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_LAYERS = frozenset(
    {
        "ACCOUNT_LIVE",
        "PROVIDER_LIVE",
        "OFFICIAL_VEHICLE",
        "PREDECESSOR",
        "SHADOW_OOS",
        "BACKTEST",
        "CONCEPTUAL_STRESS",
    }
)
_DISPOSITIONS = frozenset(
    {
        "AUTHORITATIVE_TRACKER_EVIDENCE",
        "GENERATED_DERIVATIVE",
        "HISTORICAL_REFERENCE",
        "PRODUCING_CODE",
        "PROVENANCE_ANCHOR",
        "SEMANTIC_REFERENCE",
        "STATIC_LINEAGE_MAP",
    }
)
_COST_STATUSES = frozenset({"KNOWN", "PARTIAL", "UNAVAILABLE"})
_COVERAGE_STATUSES = frozenset(
    {"COMPLETE_WITHIN_DECLARED_BOUNDARY", "PARTIAL", "FAILED"}
)
_ASSUMPTION_STATUSES = frozenset({"ACCEPTED", "INFERRED", "UNRESOLVED"})
_VERSION_DISPOSITIONS = frozenset({"EVIDENCED", "REFERENCE_ONLY", "UNRESOLVED"})
_SLOT_STATUSES = frozenset(
    {"EVIDENCED", "REFERENCE_ONLY", "UNAVAILABLE", "NOT_APPLICABLE"}
)
_LAYER_STATUSES = frozenset({"EVIDENCED", "REFERENCE_ONLY", "UNAVAILABLE"})
_COMPATIBILITY_STATUSES = frozenset(
    {"PROVEN_COMPATIBLE", "VERSION_MISMATCH", "UNRESOLVED", "NOT_APPLICABLE"}
)
_VERSION_SLOT_NAMES = (
    "backtest",
    "benchmark",
    "configuration",
    "costs",
    "design",
    "live_boundary",
    "source_route",
)

_ASSIGNMENT_FIELDS = (
    "institution",
    "account",
    "strategy",
    "period_start",
    "period_end",
    "source",
)
_CORRECTION_FIELDS = (
    "institution",
    "account",
    "strategy",
    "field",
    "old_value",
    "new_value",
    "evidence",
)
_OPEN_GAP_FIELDS = (
    "institution",
    "account",
    "last_known_strategy",
    "control_file_end_date",
    "raw_data_last_active_date",
    "note",
)


class ProvenanceValidationError(ValueError):
    """The private specification or a referenced artifact is invalid."""


def _strict_keys(
    value: Mapping[str, Any], expected: set[str], object_name: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ProvenanceValidationError(
            f"{object_name} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _identifier(value: object, field_name: str) -> str:
    parsed = _text(value, field_name)
    if _IDENTIFIER.fullmatch(parsed) is None:
        raise ProvenanceValidationError(f"{field_name} is not a portable identifier")
    return parsed


def _date(value: object, field_name: str) -> date:
    parsed_text = _text(value, field_name)
    try:
        parsed = date.fromisoformat(parsed_text)
    except ValueError as exc:
        raise ProvenanceValidationError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != parsed_text:
        raise ProvenanceValidationError(f"{field_name} must use canonical ISO format")
    return parsed


def _optional_text(value: object, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name)


def _optional_date(value: object, field_name: str) -> date | None:
    return None if value is None else _date(value, field_name)


def _optional_hash(value: object, field_name: str) -> str | None:
    parsed = _optional_text(value, field_name)
    if parsed is not None and _SHA256.fullmatch(parsed) is None:
        raise ProvenanceValidationError(f"{field_name} must be lower-case SHA-256")
    return parsed


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProvenanceValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProvenanceValidationError(f"{field_name} must be an array")
    return [_object(item, f"{field_name}[]") for item in value]


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProvenanceValidationError(f"{field_name} must be an array")
    parsed = tuple(sorted({_text(item, f"{field_name}[]") for item in value}))
    return parsed


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceValidationError("spec is not readable UTF-8 JSON") from exc
    spec = _object(parsed, "spec")
    _strict_keys(
        spec,
        {
            "acquisition_date",
            "as_of_date",
            "assumptions",
            "policy_version",
            "reserve_account",
            "roots",
            "schema_version",
            "source_groups",
            "strategies",
            "strategy_version_catalog",
            "tracker_imports",
        } | ({"financial_mvp"} if "financial_mvp" in spec else set()),
        "spec",
    )
    if spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise ProvenanceValidationError("spec schema_version is unsupported")
    if spec["policy_version"] != POLICY_VERSION:
        raise ProvenanceValidationError("spec policy_version is unsupported")
    _date(spec["as_of_date"], "spec.as_of_date")
    _date(spec["acquisition_date"], "spec.acquisition_date")
    return spec


def _financial_mvp_memberships(value: object | None) -> dict[str, str]:
    """Load only hash-pinned funded-membership dates from the static contract."""

    if value is None:
        return {}
    authority = _object(value, "financial_mvp")
    _strict_keys(
        authority,
        {
            "contract_path",
            "contract_sha256",
            "registry_path",
            "registry_sha256",
            "schema_path",
            "schema_sha256",
        },
        "financial_mvp",
    )
    paths: dict[str, Path] = {}
    for name in ("contract", "registry", "schema"):
        candidate = Path(_text(authority[f"{name}_path"], f"financial_mvp.{name}_path"))
        if not candidate.is_absolute():
            raise ProvenanceValidationError(
                f"financial MVP {name} path must be absolute"
            )
        try:
            resolved = candidate.resolve(strict=True)
            actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError as exc:
            raise ProvenanceValidationError(
                f"financial MVP {name} is unavailable"
            ) from exc
        expected_hash = _optional_hash(
            authority[f"{name}_sha256"], f"financial_mvp.{name}_sha256"
        )
        if expected_hash is None:
            raise ProvenanceValidationError(
                f"financial MVP {name} hash is required"
            )
        if actual_hash != expected_hash:
            raise ProvenanceValidationError(
                f"financial MVP {name} hash differs"
            )
        paths[name] = resolved

    module_path = Path(__file__).with_name("portfolio_financial_mvp_contract.py")
    module_spec = importlib.util.spec_from_file_location(
        "portfolio_provenance_financial_mvp_contract", module_path
    )
    if module_spec is None or module_spec.loader is None:
        raise ProvenanceValidationError("financial MVP contract validator is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    try:
        contract = module.load_contract(
            paths["contract"], paths["registry"], paths["schema"]
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ProvenanceValidationError(
            "financial MVP contract public validation failed"
        ) from exc

    memberships: dict[str, str] = {}
    for strategy in _objects(contract.get("strategies"), "financial MVP strategies"):
        strategy_id = _identifier(
            strategy.get("strategy_id"), "financial MVP strategy_id"
        )
        membership = _object(
            strategy.get("funded_membership"), "financial MVP funded_membership"
        )
        if (
            membership.get("status") != "EVIDENCED"
            or strategy.get("product_requirement") == "EXCLUDED"
        ):
            continue
        memberships[strategy_id] = _date(
            membership.get("effective_start"),
            "financial MVP funded_membership.effective_start",
        ).isoformat()
    return memberships


def _portable_relative(value: object, field_name: str) -> str:
    parsed = _text(value, field_name)
    path = PurePosixPath(parsed)
    if (
        path.is_absolute()
        or "\\" in parsed
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProvenanceValidationError(
            f"{field_name} must be a portable relative path"
        )
    return parsed


def _source_path(root: Path, relative_path: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProvenanceValidationError(
            f"source is missing or unavailable: {relative_path}"
        ) from exc
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ProvenanceValidationError(
            f"source escapes its declared root: {relative_path}"
        ) from exc
    if not resolved_candidate.is_file():
        raise ProvenanceValidationError(f"source is not a file: {relative_path}")
    return resolved_candidate


def _validate_roots(value: object) -> dict[str, Path]:
    roots = _object(value, "roots")
    if not roots:
        raise ProvenanceValidationError("roots must not be empty")
    accepted: dict[str, Path] = {}
    for raw_identifier, raw_path in roots.items():
        identifier = _identifier(raw_identifier, "roots key")
        path = Path(_text(raw_path, f"roots.{identifier}"))
        if not path.is_absolute():
            raise ProvenanceValidationError(f"root {identifier} must be absolute")
        if not path.is_dir():
            raise ProvenanceValidationError(f"root {identifier} is not a directory")
        accepted[identifier] = path
    return accepted


def _source_groups(
    value: object, roots: Mapping[str, Path], generation_as_of: date
) -> tuple[
    list[dict[str, Any]], tuple[SourceArtifact, ...], dict[tuple[str, str], str]
]:
    groups = _objects(value, "source_groups")
    if not groups:
        raise ProvenanceValidationError("source_groups must not be empty")
    group_ids: set[str] = set()
    physical_paths: set[str] = set()
    catalog_records: list[dict[str, Any]] = []
    artifacts: list[SourceArtifact] = []
    membership: dict[tuple[str, str], str] = {}
    for group in groups:
        _strict_keys(
            group,
            {
                "as_of_date",
                "disposition",
                "expected_hashes",
                "relative_paths",
                "root_id",
                "source_id",
            },
            "source_group",
        )
        source_id = _identifier(group["source_id"], "source_group.source_id")
        if source_id in group_ids:
            raise ProvenanceValidationError(f"duplicate source_id: {source_id}")
        group_ids.add(source_id)
        root_id = _identifier(group["root_id"], "source_group.root_id")
        try:
            root = roots[root_id]
        except KeyError as exc:
            raise ProvenanceValidationError(
                f"source group references unknown root: {root_id}"
            ) from exc
        source_as_of = _date(group["as_of_date"], "source_group.as_of_date")
        if source_as_of > generation_as_of:
            raise ProvenanceValidationError("source as_of_date exceeds generation")
        disposition = _text(group["disposition"], "source_group.disposition")
        if disposition not in _DISPOSITIONS:
            raise ProvenanceValidationError(
                f"unknown source disposition: {disposition}"
            )
        relative_paths = _strings(group["relative_paths"], "relative_paths")
        if not relative_paths:
            raise ProvenanceValidationError(
                f"source group {source_id} has no explicit files"
            )
        relative_paths = tuple(
            _portable_relative(item, "source_group.relative_path")
            for item in relative_paths
        )
        expected_hashes_raw = _object(
            group["expected_hashes"], "source_group.expected_hashes"
        )
        expected_hashes: dict[str, str] = {}
        for raw_path, raw_sha256 in expected_hashes_raw.items():
            relative_path = _portable_relative(raw_path, "expected_hashes key")
            sha256 = _text(raw_sha256, f"expected_hashes.{relative_path}")
            if _SHA256.fullmatch(sha256) is None:
                raise ProvenanceValidationError(
                    "expected hash must be lower-case SHA-256"
                )
            expected_hashes[relative_path] = sha256
        if not set(expected_hashes) <= set(relative_paths):
            raise ProvenanceValidationError(
                f"source group {source_id} has expected hashes for undeclared files"
            )

        files: list[dict[str, Any]] = []
        for index, relative_path in enumerate(relative_paths, start=1):
            source_path = _source_path(root, relative_path)
            physical_key = os.path.normcase(str(source_path))
            if physical_key in physical_paths:
                raise ProvenanceValidationError(
                    f"physical source is declared more than once: {relative_path}"
                )
            physical_paths.add(physical_key)
            sha256, size_bytes = hash_file(source_path)
            expected = expected_hashes.get(relative_path)
            if expected is not None and expected != sha256:
                raise ProvenanceValidationError(
                    f"source hash differs from expected value: {source_id}/{relative_path}"
                )
            files.append(
                {
                    "relative_path": relative_path,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                }
            )
            artifacts.append(
                SourceArtifact(
                    artifact_id=f"sources/{source_id}/{index:04d}",
                    sha256=sha256,
                    size_bytes=size_bytes,
                    as_of_date=source_as_of,
                    schema_name="portfolio-provenance-source",
                    schema_version="1",
                )
            )
            membership[(root_id, relative_path)] = source_id
        catalog_records.append(
            {
                "as_of_date": source_as_of.isoformat(),
                "disposition": disposition,
                "files": files,
                "root_id": root_id,
                "source_id": source_id,
            }
        )
    catalog_records.sort(key=lambda item: str(item["source_id"]))
    return catalog_records, tuple(artifacts), membership


def _validate_strategies(
    value: object, source_ids: set[str]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    strategies = _objects(value, "strategies")
    if len(strategies) != RETAINED_STRATEGY_COUNT:
        raise ProvenanceValidationError(
            "first baseline requires exactly "
            f"{RETAINED_STRATEGY_COUNT} retained strategy registry records"
        )
    accepted: list[dict[str, Any]] = []
    strategy_ids: set[str] = set()
    alias_map: dict[str, str] = {}
    for strategy in strategies:
        _strict_keys(
            strategy,
            {
                "aliases",
                "cost_status",
                "coverage_status",
                "economic_family",
                "evidence_routes",
                "gaps",
                "name",
                "strategy_id",
                "version_ref",
            },
            "strategy",
        )
        strategy_id = _identifier(strategy["strategy_id"], "strategy.strategy_id")
        if strategy_id in strategy_ids:
            raise ProvenanceValidationError(f"duplicate strategy_id: {strategy_id}")
        strategy_ids.add(strategy_id)
        name = _text(strategy["name"], "strategy.name")
        aliases = _strings(strategy["aliases"], "strategy.aliases")
        if not aliases:
            raise ProvenanceValidationError(f"strategy {strategy_id} has no aliases")
        for alias in (name, *aliases):
            key = alias.casefold()
            prior = alias_map.get(key)
            if prior is not None and prior != strategy_id:
                raise ProvenanceValidationError(f"strategy alias is ambiguous: {alias}")
            alias_map[key] = strategy_id
        cost_status = _text(strategy["cost_status"], "strategy.cost_status")
        if cost_status not in _COST_STATUSES:
            raise ProvenanceValidationError(f"unknown cost status: {cost_status}")
        coverage_status = _text(strategy["coverage_status"], "strategy.coverage_status")
        if coverage_status not in _COVERAGE_STATUSES:
            raise ProvenanceValidationError(
                f"unknown strategy coverage status: {coverage_status}"
            )
        gaps = _strings(strategy["gaps"], "strategy.gaps")
        if coverage_status != "COMPLETE_WITHIN_DECLARED_BOUNDARY" and not gaps:
            raise ProvenanceValidationError(
                f"strategy {strategy_id} requires an explicit coverage gap"
            )
        routes: list[dict[str, str]] = []
        for route in _objects(strategy["evidence_routes"], "evidence_routes"):
            _strict_keys(route, {"evidence_layer", "purpose", "source_id"}, "route")
            source_id = _identifier(route["source_id"], "route.source_id")
            if source_id not in source_ids:
                raise ProvenanceValidationError(
                    f"strategy route references unknown source: {source_id}"
                )
            evidence_layer = _text(route["evidence_layer"], "route.evidence_layer")
            if evidence_layer not in _EVIDENCE_LAYERS:
                raise ProvenanceValidationError(
                    f"unknown evidence layer: {evidence_layer}"
                )
            routes.append(
                {
                    "evidence_layer": evidence_layer,
                    "purpose": _text(route["purpose"], "route.purpose"),
                    "source_id": source_id,
                }
            )
        if not routes:
            raise ProvenanceValidationError(
                f"strategy {strategy_id} has no source route"
            )
        version_ref = strategy["version_ref"]
        if version_ref is not None:
            version_ref = _text(version_ref, "strategy.version_ref")
        version_disposition = (
            "REFERENCE_ONLY" if version_ref is not None else "UNRESOLVED"
        )
        accepted.append(
            {
                "aliases": list(aliases),
                "cost_status": cost_status,
                "coverage_status": coverage_status,
                "economic_family": _text(
                    strategy["economic_family"], "strategy.economic_family"
                ),
                "evidence_routes": sorted(
                    routes,
                    key=lambda item: (
                        item["source_id"],
                        item["evidence_layer"],
                        item["purpose"],
                    ),
                ),
                "gaps": list(gaps),
                "name": name,
                "strategy_id": strategy_id,
                "version_disposition": version_disposition,
                "version_ref": version_ref,
            }
        )
    accepted.sort(key=lambda item: str(item["strategy_id"]))
    return accepted, alias_map


def _validate_strategy_version_catalog(
    value: object,
    *,
    generation_as_of: date,
    source_ids: set[str],
    source_hashes: set[str],
    strategies: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate one as-of-effective version disposition per retained record."""

    catalog = _object(value, "strategy_version_catalog")
    _strict_keys(
        catalog,
        {"as_of_date", "catalog_schema_version", "strategies"},
        "strategy_version_catalog",
    )
    if catalog["catalog_schema_version"] != STRATEGY_VERSION_CATALOG_SCHEMA_VERSION:
        raise ProvenanceValidationError(
            "strategy version catalog schema_version is unsupported"
        )
    catalog_as_of = _date(catalog["as_of_date"], "strategy_version_catalog.as_of_date")
    if catalog_as_of != generation_as_of:
        raise ProvenanceValidationError(
            "strategy version catalog as_of_date differs from provenance"
        )
    expected_ids = {str(item["strategy_id"]) for item in strategies}
    spec_versions = {
        str(item["strategy_id"]): item.get("version_ref") for item in strategies
    }
    rows = _objects(catalog["strategies"], "strategy_version_catalog.strategies")
    if len(rows) != RETAINED_STRATEGY_COUNT:
        raise ProvenanceValidationError(
            "strategy version catalog requires exactly "
            f"{RETAINED_STRATEGY_COUNT} retained strategy registry rows"
        )

    def slot(value: object, field_name: str) -> dict[str, Any]:
        accepted = _object(value, field_name)
        _strict_keys(
            accepted,
            {
                "artifact_sha256",
                "disposition",
                "evidence_layer",
                "observed_end",
                "observed_start",
                "source_ids",
                "status",
                "version_ref",
            },
            field_name,
        )
        status = _text(accepted["status"], f"{field_name}.status")
        if status not in _SLOT_STATUSES:
            raise ProvenanceValidationError(f"unknown strategy evidence slot: {status}")
        version_ref = _optional_text(
            accepted["version_ref"], f"{field_name}.version_ref"
        )
        artifact_sha256 = _optional_hash(
            accepted["artifact_sha256"], f"{field_name}.artifact_sha256"
        )
        if artifact_sha256 is not None and artifact_sha256 not in source_hashes:
            raise ProvenanceValidationError(
                f"{field_name}.artifact_sha256 is not an admitted source artifact"
            )
        routes = _strings(accepted["source_ids"], f"{field_name}.source_ids")
        if not set(routes) <= source_ids:
            raise ProvenanceValidationError(f"{field_name} references unknown source")
        evidence_layer = _optional_text(
            accepted["evidence_layer"], f"{field_name}.evidence_layer"
        )
        if evidence_layer is not None and evidence_layer not in _EVIDENCE_LAYERS:
            raise ProvenanceValidationError(
                f"{field_name} has unknown evidence layer: {evidence_layer}"
            )
        observed_start = _optional_date(
            accepted["observed_start"], f"{field_name}.observed_start"
        )
        observed_end = _optional_date(
            accepted["observed_end"], f"{field_name}.observed_end"
        )
        if (observed_start is None) != (observed_end is None):
            raise ProvenanceValidationError(
                f"{field_name} must declare both observation bounds or neither"
            )
        if observed_start is not None and observed_end is not None:
            if observed_end < observed_start or observed_end > generation_as_of:
                raise ProvenanceValidationError(
                    f"{field_name} observation interval is invalid"
                )
        if status == "EVIDENCED" and artifact_sha256 is None and version_ref is None:
            raise ProvenanceValidationError(
                f"{field_name} EVIDENCED requires an artifact hash or version"
            )
        if status == "REFERENCE_ONLY" and not routes:
            raise ProvenanceValidationError(
                f"{field_name} REFERENCE_ONLY requires a source route"
            )
        if status in {"UNAVAILABLE", "NOT_APPLICABLE"} and any(
            item is not None
            for item in (artifact_sha256, version_ref, observed_start, observed_end)
        ):
            raise ProvenanceValidationError(
                f"{field_name} {status} cannot carry asserted evidence"
            )
        return {
            "artifact_sha256": artifact_sha256,
            "disposition": _text(accepted["disposition"], f"{field_name}.disposition"),
            "evidence_layer": evidence_layer,
            "observed_end": observed_end.isoformat() if observed_end else None,
            "observed_start": observed_start.isoformat() if observed_start else None,
            "source_ids": list(routes),
            "status": status,
            "version_ref": version_ref,
        }

    accepted_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        _strict_keys(
            row,
            {
                *_VERSION_SLOT_NAMES,
                "compatibility_reason",
                "compatibility_status",
                "disposition",
                "disposition_reason",
                "effective_end",
                "effective_start",
                "evidence_layers",
                "gaps",
                "strategy_id",
                "version_ref",
            },
            "strategy_version",
        )
        strategy_id = _identifier(row["strategy_id"], "strategy_version.strategy_id")
        if strategy_id in seen_ids:
            raise ProvenanceValidationError(
                f"duplicate strategy version row: {strategy_id}"
            )
        seen_ids.add(strategy_id)
        if strategy_id not in expected_ids:
            raise ProvenanceValidationError(
                f"strategy version references unknown strategy: {strategy_id}"
            )
        effective_start = _date(
            row["effective_start"], "strategy_version.effective_start"
        )
        effective_end = _optional_date(
            row["effective_end"], "strategy_version.effective_end"
        )
        if effective_start > generation_as_of or (
            effective_end is not None
            and (effective_end < effective_start or effective_end < generation_as_of)
        ):
            raise ProvenanceValidationError(
                f"strategy version is not effective at as_of_date: {strategy_id}"
            )
        disposition = _text(row["disposition"], "strategy_version.disposition")
        if disposition not in _VERSION_DISPOSITIONS:
            raise ProvenanceValidationError(
                f"unknown strategy version disposition: {disposition}"
            )
        version_ref = _optional_text(row["version_ref"], "strategy_version.version_ref")
        if spec_versions[strategy_id] != version_ref:
            raise ProvenanceValidationError(
                f"strategy version_ref differs from strategy registry: {strategy_id}"
            )
        gaps = _strings(row["gaps"], "strategy_version.gaps")
        if disposition == "EVIDENCED" and version_ref is None:
            raise ProvenanceValidationError(
                f"EVIDENCED strategy version requires a version_ref: {strategy_id}"
            )
        if disposition == "REFERENCE_ONLY" and (version_ref is None or not gaps):
            raise ProvenanceValidationError(
                f"REFERENCE_ONLY strategy version requires a reference and gaps: {strategy_id}"
            )
        if disposition == "UNRESOLVED" and (version_ref is not None or not gaps):
            raise ProvenanceValidationError(
                f"UNRESOLVED strategy version requires a null reference and gaps: {strategy_id}"
            )
        accepted_slots = {
            name: slot(row[name], f"strategy_version.{strategy_id}.{name}")
            for name in _VERSION_SLOT_NAMES
        }
        if (
            accepted_slots["source_route"]["status"]
            not in {
                "EVIDENCED",
                "REFERENCE_ONLY",
            }
            or not accepted_slots["source_route"]["source_ids"]
        ):
            raise ProvenanceValidationError(
                f"strategy version requires an admitted source route: {strategy_id}"
            )
        compatibility = _text(
            row["compatibility_status"], "strategy_version.compatibility_status"
        )
        if compatibility not in _COMPATIBILITY_STATUSES:
            raise ProvenanceValidationError(
                f"unknown compatibility status: {compatibility}"
            )
        if disposition == "UNRESOLVED" and compatibility == "PROVEN_COMPATIBLE":
            raise ProvenanceValidationError(
                f"unresolved strategy cannot assert compatibility: {strategy_id}"
            )
        layers: list[dict[str, Any]] = []
        layer_keys: set[tuple[str, str]] = set()
        for layer in _objects(
            row["evidence_layers"], f"strategy_version.{strategy_id}.evidence_layers"
        ):
            _strict_keys(
                layer,
                {
                    "artifact_sha256",
                    "disposition",
                    "evidence_layer",
                    "observed_end",
                    "observed_start",
                    "source_id",
                    "status",
                    "version_ref",
                },
                "strategy evidence layer",
            )
            evidence_layer = _text(
                layer["evidence_layer"], "strategy evidence layer.evidence_layer"
            )
            if evidence_layer not in _EVIDENCE_LAYERS:
                raise ProvenanceValidationError(
                    f"unknown strategy evidence layer: {evidence_layer}"
                )
            source_id = _identifier(
                layer["source_id"], "strategy evidence layer.source_id"
            )
            if source_id not in source_ids:
                raise ProvenanceValidationError(
                    f"strategy evidence layer references unknown source: {source_id}"
                )
            key = (evidence_layer, source_id)
            if key in layer_keys:
                raise ProvenanceValidationError(
                    f"duplicate strategy evidence layer route: {strategy_id}/{evidence_layer}/{source_id}"
                )
            layer_keys.add(key)
            status = _text(layer["status"], "strategy evidence layer.status")
            if status not in _LAYER_STATUSES:
                raise ProvenanceValidationError(
                    f"unknown strategy evidence layer status: {status}"
                )
            layer_hash = _optional_hash(
                layer["artifact_sha256"], "strategy evidence layer.artifact_sha256"
            )
            if layer_hash is not None and layer_hash not in source_hashes:
                raise ProvenanceValidationError(
                    "strategy evidence layer hash is not an admitted source artifact"
                )
            layer_version = _optional_text(
                layer["version_ref"], "strategy evidence layer.version_ref"
            )
            if status == "EVIDENCED" and layer_hash is None and layer_version is None:
                raise ProvenanceValidationError(
                    "EVIDENCED strategy evidence layer requires an artifact hash or version"
                )
            observed_start = _optional_date(
                layer["observed_start"], "strategy evidence layer.observed_start"
            )
            observed_end = _optional_date(
                layer["observed_end"], "strategy evidence layer.observed_end"
            )
            if (observed_start is None) != (observed_end is None):
                raise ProvenanceValidationError(
                    "strategy evidence layer requires both observation bounds or neither"
                )
            if (
                observed_start
                and observed_end
                and (observed_end < observed_start or observed_end > generation_as_of)
            ):
                raise ProvenanceValidationError(
                    "strategy evidence layer observation interval is invalid"
                )
            if status == "UNAVAILABLE" and any(
                item is not None
                for item in (layer_hash, layer_version, observed_start, observed_end)
            ):
                raise ProvenanceValidationError(
                    "UNAVAILABLE strategy evidence layer cannot assert evidence"
                )
            layers.append(
                {
                    "artifact_sha256": layer_hash,
                    "disposition": _text(
                        layer["disposition"], "strategy evidence layer.disposition"
                    ),
                    "evidence_layer": evidence_layer,
                    "observed_end": observed_end.isoformat() if observed_end else None,
                    "observed_start": (
                        observed_start.isoformat() if observed_start else None
                    ),
                    "source_id": source_id,
                    "status": status,
                    "version_ref": layer_version,
                }
            )
        if not layers:
            raise ProvenanceValidationError(
                f"strategy version has no evidence-layer inventory: {strategy_id}"
            )
        accepted_rows.append(
            {
                **accepted_slots,
                "compatibility_reason": _text(
                    row["compatibility_reason"],
                    "strategy_version.compatibility_reason",
                ),
                "compatibility_status": compatibility,
                "disposition": disposition,
                "disposition_reason": _text(
                    row["disposition_reason"], "strategy_version.disposition_reason"
                ),
                "effective_end": effective_end.isoformat() if effective_end else None,
                "effective_start": effective_start.isoformat(),
                "evidence_layers": sorted(
                    layers,
                    key=lambda item: (
                        str(item["evidence_layer"]),
                        str(item["source_id"]),
                    ),
                ),
                "gaps": list(gaps),
                "strategy_id": strategy_id,
                "version_ref": version_ref,
            }
        )
    if seen_ids != expected_ids:
        raise ProvenanceValidationError(
            "strategy version catalog membership differs from retained strategy registry"
        )
    return {
        "as_of_date": catalog_as_of.isoformat(),
        "catalog_schema_version": STRATEGY_VERSION_CATALOG_SCHEMA_VERSION,
        "strategies": sorted(accepted_rows, key=lambda item: str(item["strategy_id"])),
    }


def _import_ref(
    value: object,
    field_name: str,
    roots: Mapping[str, Path],
    membership: Mapping[tuple[str, str], str],
) -> tuple[Path, str]:
    ref = _object(value, field_name)
    _strict_keys(ref, {"relative_path", "root_id"}, field_name)
    root_id = _identifier(ref["root_id"], f"{field_name}.root_id")
    relative_path = _portable_relative(
        ref["relative_path"], f"{field_name}.relative_path"
    )
    try:
        root = roots[root_id]
    except KeyError as exc:
        raise ProvenanceValidationError(
            f"{field_name} references unknown root: {root_id}"
        ) from exc
    try:
        source_id = membership[(root_id, relative_path)]
    except KeyError as exc:
        raise ProvenanceValidationError(
            f"{field_name} is not present in a source group"
        ) from exc
    return _source_path(root, relative_path), source_id


def _read_csv(
    path: Path, expected_fields: Sequence[str], object_name: str
) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvenanceValidationError(
            f"{object_name} is not readable UTF-8 CSV"
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != tuple(expected_fields):
        raise ProvenanceValidationError(f"{object_name} header differs")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ProvenanceValidationError(f"{object_name} has a malformed row")
        rows.append({key: value or "" for key, value in row.items() if key is not None})
    return rows


def _record_id(prefix: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _import_tracker(
    value: object,
    *,
    roots: Mapping[str, Path],
    membership: Mapping[tuple[str, str], str],
    strategies: Sequence[Mapping[str, Any]],
    alias_map: Mapping[str, str],
    funded_memberships: Mapping[str, str],
    reserve_value: object,
    generation_as_of: date,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    imports = _object(value, "tracker_imports")
    _strict_keys(
        imports,
        {"assignments", "corrections", "open_gaps", "unmapped_labels"},
        "tracker_imports",
    )
    assignment_path, assignment_source = _import_ref(
        imports["assignments"], "tracker_imports.assignments", roots, membership
    )
    correction_path, correction_source = _import_ref(
        imports["corrections"], "tracker_imports.corrections", roots, membership
    )
    gap_path, gap_source = _import_ref(
        imports["open_gaps"], "tracker_imports.open_gaps", roots, membership
    )
    unmapped_labels = {
        item.casefold()
        for item in _strings(imports["unmapped_labels"], "unmapped_labels")
    }

    reserve = _object(reserve_value, "reserve_account")
    _strict_keys(
        reserve, {"account_id", "aliases", "name", "source_routes"}, "reserve_account"
    )
    reserve_id = _identifier(reserve["account_id"], "reserve_account.account_id")
    reserve_routes = _strings(reserve["source_routes"], "reserve_account.source_routes")
    known_source_ids = set(membership.values())
    if not reserve_routes or not set(reserve_routes) <= known_source_ids:
        raise ProvenanceValidationError("reserve source routes are missing or unknown")

    assignment_rows = _read_csv(assignment_path, _ASSIGNMENT_FIELDS, "assignments")
    accounts_by_alias: dict[str, dict[str, Any]] = {}
    assignments: list[dict[str, Any]] = []
    funded_replaced_starts: dict[int, str] = {}
    strategy_ids = {str(item["strategy_id"]) for item in strategies}
    for row in assignment_rows:
        institution = _text(row["institution"], "assignment.institution")
        account_label = row["account"].strip()
        source_alias = (
            institution if not account_label else f"{institution} | {account_label}"
        )
        account_id = (
            f"account-{hashlib.sha256(source_alias.encode('utf-8')).hexdigest()[:16]}"
        )
        accounts_by_alias[source_alias] = {
            "account_id": account_id,
            "aliases": [source_alias],
            "current_pool_id": SYSTEMATIC_POOL_ID,
            "name": source_alias,
            "role": "INVESTMENT",
            "source_routes": [assignment_source],
        }
        observed_label = _text(row["strategy"], "assignment.strategy")
        strategy_id = alias_map.get(observed_label.casefold())
        if strategy_id is None and observed_label.casefold() not in unmapped_labels:
            raise ProvenanceValidationError(
                f"tracker strategy label is neither mapped nor approved unmapped: {observed_label}"
            )
        if strategy_id is not None and strategy_id not in strategy_ids:
            raise ProvenanceValidationError("tracker alias maps to an unknown strategy")
        period_start = row["period_start"].strip()
        period_end = row["period_end"].strip()
        if strategy_id is not None and not period_start:
            raise ProvenanceValidationError(
                f"mapped tracker assignment requires period_start: {observed_label}"
            )
        if period_start:
            _date(period_start, "assignment.period_start")
        if period_end:
            _date(period_end, "assignment.period_end")
        if (
            period_start
            and period_end
            and date.fromisoformat(period_end) < date.fromisoformat(period_start)
        ):
            raise ProvenanceValidationError("assignment interval is reversed")
        source_classification = _text(row["source"], "assignment.source")
        original_period_start = period_start
        if strategy_id is not None and source_classification == "provider_account_live_start":
            try:
                period_start = funded_memberships[strategy_id]
            except KeyError as exc:
                raise ProvenanceValidationError(
                    "provider account-live start requires financial MVP authority"
                ) from exc
            source_classification = "financial_mvp_funded_membership_reconciled"
        is_current = (
            not period_start or date.fromisoformat(period_start) <= generation_as_of
        ) and (not period_end or date.fromisoformat(period_end) >= generation_as_of)
        strategy_disposition = (
            "STRATEGY_MAPPED"
            if strategy_id is not None
            else ("CURRENT_POOL_ONLY" if is_current else "HISTORICAL_POOL_ONLY")
        )
        payload: dict[str, Any] = {
            "account_id": account_id,
            "effective_end": period_end or None,
            "effective_start": period_start or None,
            "mapping_status": "MAPPED" if strategy_id is not None else "UNMAPPED",
            "observed_label": observed_label,
            "pool_id": SYSTEMATIC_POOL_ID,
            "pool_mapping_status": "MAPPED",
            "source_classification": source_classification,
            "source_id": assignment_source,
            "strategy_id": strategy_id,
            "strategy_mapping_disposition": strategy_disposition,
        }
        assignment = {"assignment_id": _record_id("assignment", payload), **payload}
        assignments.append(assignment)
        if original_period_start != period_start:
            funded_replaced_starts[id(assignment)] = original_period_start

    current_mapped_by_strategy: dict[str, list[dict[str, Any]]] = {}
    for assignment in assignments:
        if assignment["mapping_status"] != "MAPPED":
            continue
        strategy_id = str(assignment["strategy_id"])
        start_value = assignment.get("effective_start")
        end_value = assignment.get("effective_end")
        if (
            start_value is not None
            and date.fromisoformat(str(start_value)) > generation_as_of
        ):
            continue
        if (
            end_value is not None
            and date.fromisoformat(str(end_value)) < generation_as_of
        ):
            continue
        current_mapped_by_strategy.setdefault(strategy_id, []).append(assignment)
    for strategy_id, current_assignments in current_mapped_by_strategy.items():
        funded_start = funded_memberships.get(strategy_id)
        if funded_start is None or len(current_assignments) != 1:
            continue
        current_assignment = current_assignments[0]
        original_start = funded_replaced_starts.get(
            id(current_assignment), current_assignment["effective_start"]
        )
        current_assignment["effective_start"] = funded_start
        current_assignment["source_classification"] = (
            "financial_mvp_funded_membership_reconciled"
        )
        if original_start is None or funded_start <= original_start:
            continue
        original_start_date = date.fromisoformat(str(original_start))
        preceding_ends = {
            original_start_date.isoformat(),
            (original_start_date - timedelta(days=1)).isoformat(),
        }
        funded_predecessor_end = (
            date.fromisoformat(funded_start) - timedelta(days=1)
        ).isoformat()
        for assignment in assignments:
            if (
                assignment["account_id"] == current_assignment["account_id"]
                and assignment["mapping_status"] == "UNMAPPED"
                and str(assignment["observed_label"]).casefold() in unmapped_labels
                and assignment["effective_end"] in preceding_ends
            ):
                assignment["effective_end"] = funded_predecessor_end
                break

    # A current legacy ``General`` row is the account-level implementation of
    # the reserve pool, not a missing systematic strategy.  Keep historical
    # General intervals on systematic accounts explicitly unmapped, but route
    # accounts whose current state is only General to reserve capital.
    active_by_account: dict[str, list[dict[str, Any]]] = {}
    for assignment in assignments:
        start_value = assignment.get("effective_start")
        end_value = assignment.get("effective_end")
        if (
            start_value is not None
            and date.fromisoformat(str(start_value)) > generation_as_of
        ):
            continue
        if (
            end_value is not None
            and date.fromisoformat(str(end_value)) < generation_as_of
        ):
            continue
        active_by_account.setdefault(str(assignment["account_id"]), []).append(
            assignment
        )
    for account_id, active in active_by_account.items():
        if not active or any(item["mapping_status"] == "MAPPED" for item in active):
            continue
        if not all(
            str(item["observed_label"]).casefold() in unmapped_labels for item in active
        ):
            continue
        account_row = next(
            item
            for item in accounts_by_alias.values()
            if item["account_id"] == account_id
        )
        account_row["current_pool_id"] = RESERVE_POOL_ID
        for assignment in active:
            assignment["pool_id"] = RESERVE_POOL_ID
            assignment["strategy_mapping_disposition"] = "RESERVE_NOT_APPLICABLE"
    for assignment in assignments:
        payload = {
            key: value for key, value in assignment.items() if key != "assignment_id"
        }
        assignment["assignment_id"] = _record_id("assignment", payload)

    accounts = sorted(
        accounts_by_alias.values(), key=lambda item: str(item["account_id"])
    )
    accounts.append(
        {
            "account_id": reserve_id,
            "aliases": list(_strings(reserve["aliases"], "reserve_account.aliases")),
            "current_pool_id": RESERVE_POOL_ID,
            "name": _text(reserve["name"], "reserve_account.name"),
            "role": "RESERVE",
            "source_routes": list(reserve_routes),
        }
    )
    accounts.sort(key=lambda item: str(item["account_id"]))

    corrections: list[dict[str, Any]] = []
    for row in _read_csv(correction_path, _CORRECTION_FIELDS, "corrections"):
        payload = {"fields": row, "source_id": correction_source}
        corrections.append(
            {"correction_id": _record_id("correction", payload), **payload}
        )
    corrections.sort(key=lambda item: str(item["correction_id"]))

    open_gaps: list[dict[str, Any]] = []
    for row in _read_csv(gap_path, _OPEN_GAP_FIELDS, "open_gaps"):
        payload = {"fields": row, "source_id": gap_source}
        open_gaps.append({"gap_id": _record_id("gap", payload), **payload})
    open_gaps.sort(key=lambda item: str(item["gap_id"]))
    assignments.sort(key=lambda item: str(item["assignment_id"]))
    return accounts, assignments, corrections, open_gaps


def _validate_assumptions(value: object, source_ids: set[str]) -> list[dict[str, Any]]:
    assumptions: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for assumption in _objects(value, "assumptions"):
        _strict_keys(
            assumption,
            {"assumption_id", "description", "source_routes", "status"},
            "assumption",
        )
        assumption_id = _identifier(
            assumption["assumption_id"], "assumption.assumption_id"
        )
        if assumption_id in identifiers:
            raise ProvenanceValidationError(f"duplicate assumption_id: {assumption_id}")
        identifiers.add(assumption_id)
        status = _text(assumption["status"], "assumption.status")
        if status not in _ASSUMPTION_STATUSES:
            raise ProvenanceValidationError(f"unknown assumption status: {status}")
        routes = _strings(assumption["source_routes"], "assumption.source_routes")
        if not routes or not set(routes) <= source_ids:
            raise ProvenanceValidationError(
                f"assumption {assumption_id} has missing or unknown source routes"
            )
        assumptions.append(
            {
                "assumption_id": assumption_id,
                "description": _text(
                    assumption["description"], "assumption.description"
                ),
                "source_routes": list(routes),
                "status": status,
            }
        )
    assumptions.sort(key=lambda item: str(item["assumption_id"]))
    return assumptions


def build_catalog(
    spec_path: Path,
) -> tuple[dict[str, Any], GenerationManifest, bytes]:
    """Validate the private spec and return catalog, manifest, and bytes."""

    spec = _load_spec(spec_path)
    generation_as_of = _date(spec["as_of_date"], "spec.as_of_date")
    roots = _validate_roots(spec["roots"])
    source_records, source_artifacts, membership = _source_groups(
        spec["source_groups"], roots, generation_as_of
    )
    source_ids = {str(item["source_id"]) for item in source_records}
    source_hashes = {
        str(file["sha256"])
        for source in source_records
        for file in _objects(source.get("files"), "source_artifacts.files")
    }
    strategies, alias_map = _validate_strategies(spec["strategies"], source_ids)
    funded_memberships = _financial_mvp_memberships(spec.get("financial_mvp"))
    strategy_version_catalog = _validate_strategy_version_catalog(
        spec["strategy_version_catalog"],
        generation_as_of=generation_as_of,
        source_ids=source_ids,
        source_hashes=source_hashes,
        strategies=strategies,
    )
    versions_by_strategy = {
        str(item["strategy_id"]): item
        for item in _objects(
            strategy_version_catalog["strategies"], "strategy version catalog"
        )
    }
    for strategy in strategies:
        version = versions_by_strategy[str(strategy["strategy_id"])]
        strategy["version_disposition"] = version["disposition"]
        strategy["version_ref"] = version["version_ref"]
    accounts, assignments, corrections, open_gaps = _import_tracker(
        spec["tracker_imports"],
        roots=roots,
        membership=membership,
        strategies=strategies,
        alias_map=alias_map,
        funded_memberships=funded_memberships,
        reserve_value=spec["reserve_account"],
        generation_as_of=generation_as_of,
    )
    assumptions = _validate_assumptions(spec["assumptions"], source_ids)
    catalog: dict[str, Any] = {
        "accounts": accounts,
        "acquisition_date": _date(
            spec["acquisition_date"], "spec.acquisition_date"
        ).isoformat(),
        "as_of_date": generation_as_of.isoformat(),
        "assignments": assignments,
        "assumptions": assumptions,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "capital_pools": [
            {
                "base_currency": "USD",
                "name": "Systematic capital",
                "pool_id": SYSTEMATIC_POOL_ID,
                "role": "SYSTEMATIC",
            },
            {
                "base_currency": "USD",
                "name": "Reserve capital",
                "pool_id": RESERVE_POOL_ID,
                "role": "RESERVE",
            },
        ],
        "corrections": corrections,
        "open_gaps": open_gaps,
        "policy_version": POLICY_VERSION,
        "source_artifacts": source_records,
        "strategies": strategies,
        "strategy_version_catalog": strategy_version_catalog,
    }
    catalog_bytes = _canonical_bytes(catalog)
    catalog_artifact = SourceArtifact(
        artifact_id="normalized/portfolio-provenance.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=generation_as_of,
        schema_name="portfolio-provenance-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("portfolio-provenance-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "explicitly configured portfolio provenance sources, accounts, "
                "and nine retained strategy registry records (seven REQUIRED active "
                "current, Echo DEPRECATED_HISTORY_ONLY, and Composer "
                "IMMATERIAL_EXCLUDED); data completeness remains in catalog gaps"
            ),
            fact_types=(
                "accounts",
                "assignments",
                "assumptions",
                "corrections",
                "source artifacts",
                "strategies",
                "strategy versions",
            ),
            observed_start=generation_as_of,
            observed_end=generation_as_of,
        ),
        artifacts=(*source_artifacts, catalog_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_catalog(spec_path: Path, output_root: Path) -> dict[str, Any]:
    catalog, manifest, catalog_bytes = build_catalog(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "portfolio-provenance.json"
    try:
        descriptor = os.open(
            catalog_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        if catalog_path.read_bytes() != catalog_bytes:
            raise GenerationConflictError(
                "generation catalog already exists with different bytes"
            )
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(catalog_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "account_count": len(catalog["accounts"]),
        "assignment_count": len(catalog["assignments"]),
        "assumption_count": len(catalog["assumptions"]),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "correction_count": len(catalog["corrections"]),
        "generation_id": manifest.generation_id,
        "open_gap_count": len(catalog["open_gaps"]),
        "source_file_count": sum(
            len(item["files"]) for item in catalog["source_artifacts"]
        ),
        "strategy_count": len(catalog["strategies"]),
    }


def publish_catalog(
    spec_path: Path, output_root: Path, generation_id: str
) -> dict[str, Any]:
    evidence = stage_catalog(spec_path, output_root)
    if evidence["generation_id"] != generation_id:
        raise ProvenanceValidationError(
            "requested generation differs from the current spec and source content"
        )
    pointer = publish_generation(output_root, generation_id)
    return {**evidence, "publication_state": pointer.state.value}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("stage", "publish"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--spec", type=Path, required=True)
        command_parser.add_argument("--output-root", type=Path, required=True)
        if command == "publish":
            command_parser.add_argument("--generation-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            result = stage_catalog(args.spec, args.output_root)
        else:
            result = publish_catalog(args.spec, args.output_root, args.generation_id)
    except (OSError, ProvenanceValidationError, GenerationConflictError) as exc:
        print(f"portfolio-provenance: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
