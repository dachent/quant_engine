"""Build a current-perimeter dependence source from accepted immutable history."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Final

from quant_engine.core.contracts import GenerationManifest, canonical_json_bytes


BUILDER: Final = "portfolio-current-dependence-source"
BUILDER_VERSION: Final = "1.2.0"
SCHEMA_VERSION: Final = 1
POLICY_ID: Final = "PORT-DEPENDENCE-1"
LAYERED_HISTORY_BUILDER: Final = "portfolio-target-series/layered-history-1"
# The dependence policy pins the accepted 31030553... receipt produced by 1.14.0.
# This consumer validates that exact historical authority; it does not follow
# the version of whichever layered-history producer happens to be installed.
LAYERED_HISTORY_BUILDER_VERSION: Final = "1.14.0"
REQUIRED_HISTORY_FILES: Final = frozenset(
    {
        "coverage.json",
        "excluded.csv",
        "generation.json",
        "live-only.csv",
        "live-plus-backtest.csv",
        "returns.csv",
        "segments.csv",
    }
)
SEGMENT_FIELDS: Final = (
    "strategy_id",
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
    "segment_id",
    "source_id",
    "source_generation_id",
    "mode_admission",
    "return_basis",
    "return_type",
    "calendar",
    "frequency",
)
RETURN_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
)


class CurrentDependenceError(ValueError):
    """The current dependence source is not mechanically admissible."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _regular_file(path: Path, name: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CurrentDependenceError(f"{name} must be a regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CurrentDependenceError(f"{name} is not readable") from exc


def _json_bytes(content: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentDependenceError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CurrentDependenceError(f"{name} root must be an object")
    return value


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CurrentDependenceError(f"{name} must be an object")
    return dict(value)


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CurrentDependenceError(f"{name} must be an array of objects")
    return [dict(item) for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CurrentDependenceError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: object, name: str) -> str:
    accepted = _text(value, name)
    if len(accepted) != 64 or any(ch not in "0123456789abcdef" for ch in accepted):
        raise CurrentDependenceError(f"{name} must be lowercase SHA-256")
    return accepted


def _day(value: object, name: str) -> date:
    accepted = _text(value, name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise CurrentDependenceError(f"{name} must be a canonical date") from exc
    if parsed.isoformat() != accepted:
        raise CurrentDependenceError(f"{name} must be a canonical date")
    return parsed


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CurrentDependenceError(f"{name} is invalid")
    return value


def _financial_module() -> Any:
    module_path = (
        Path(__file__).resolve().with_name("portfolio_financial_mvp_contract.py")
    )
    module_name = "portfolio_financial_mvp_contract_for_current_dependence"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CurrentDependenceError("financial MVP validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise CurrentDependenceError(
            "financial MVP validator cannot be loaded"
        ) from exc
    return module


def _load_authorities(
    contract_path: Path, policy_path: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    module = _financial_module()
    try:
        contract = module.load_contract(contract_path=contract_path)
    except (OSError, ValueError) as exc:
        raise CurrentDependenceError(
            f"financial MVP contract is invalid: {exc}"
        ) from exc
    policy_content = _regular_file(policy_path, "dependence policy")
    policy = _json_bytes(policy_content, "dependence policy")
    schema_locator = policy.get("$schema")
    if schema_locator != "../schemas/portfolio/dependence-policy.schema.json":
        raise CurrentDependenceError("dependence policy schema locator differs")
    schema_path = (policy_path.parent / str(schema_locator)).resolve()
    schema = _json_bytes(
        _regular_file(schema_path, "dependence policy schema"),
        "dependence policy schema",
    )
    errors = module.schema_errors(schema, policy, schema)
    if errors:
        raise CurrentDependenceError(
            "dependence policy schema validation failed: " + "; ".join(errors[:10])
        )
    if (
        policy.get("schema_version") != SCHEMA_VERSION
        or policy.get("policy_id") != POLICY_ID
        or policy.get("owner_issue") != 336
    ):
        raise CurrentDependenceError("dependence policy identity differs")
    contract_content = _regular_file(contract_path, "financial MVP contract")
    identities = {
        "contract_raw_sha256": _sha256(contract_content),
        "contract_semantic_digest": module.contract_digest(contract),
        "census_sha256": module.census_digest(contract["current_census"]),
        "policy_sha256": _sha256(policy_content),
    }
    return contract, policy, identities


def _csv_rows(
    content: bytes, fields: tuple[str, ...], name: str
) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if tuple(reader.fieldnames or ()) != fields:
            raise CurrentDependenceError(f"{name} headers differ")
        rows = [dict(row) for row in reader]
    except (UnicodeDecodeError, csv.Error) as exc:
        raise CurrentDependenceError(f"{name} is not readable CSV") from exc
    if any(set(row) != set(fields) for row in rows):
        raise CurrentDependenceError(f"{name} row schema differs")
    return rows


def _csv_content(fields: tuple[str, ...], rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({field: row[field] for field in fields} for row in rows)
    return buffer.getvalue().encode("utf-8")


def _return_identity(rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    ordered = sorted(rows, key=lambda row: (row["series_id"], row["date"]))
    return {
        "observed_start": ordered[0]["date"] if ordered else None,
        "observed_end": ordered[-1]["date"] if ordered else None,
        "return_observations": len(ordered),
        "sha256": _sha256(_csv_content(RETURN_FIELDS, ordered)),
    }


def _verified_history(
    generation_root: Path,
    policy: Mapping[str, Any],
    contract: Mapping[str, Any],
    evaluated_on: date,
) -> tuple[list[dict[str, str]], list[dict[str, object]], date, dict[str, Any], bytes]:
    root = generation_root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise CurrentDependenceError("layered history generation must be a directory")
    names = {path.name for path in root.iterdir()}
    if names != REQUIRED_HISTORY_FILES:
        raise CurrentDependenceError("layered history generation inventory differs")
    authority = _object(policy.get("history_authority"), "history authority")
    manifest_content = _regular_file(
        root / "generation.json", "layered history receipt"
    )
    if _sha256(manifest_content) != _hash(
        authority.get("receipt_sha256"), "history receipt_sha256"
    ):
        raise CurrentDependenceError("layered history receipt identity differs")
    manifest = _json_bytes(manifest_content, "layered history receipt")
    if canonical_json_bytes(manifest) != manifest_content:
        raise CurrentDependenceError("layered history receipt is not canonical")
    generation_id = _hash(manifest.get("generation_id"), "history generation_id")
    if generation_id != authority.get("generation_id") or generation_id != root.name:
        raise CurrentDependenceError("layered history generation identity differs")
    without_id = dict(manifest)
    without_id.pop("generation_id")
    if _sha256(canonical_json_bytes(without_id)) != generation_id:
        raise CurrentDependenceError("layered history receipt content identity differs")
    if (
        manifest.get("builder") != LAYERED_HISTORY_BUILDER
        or manifest.get("builder_version") != LAYERED_HISTORY_BUILDER_VERSION
        or manifest.get("schema_version") != 1
        or manifest.get("verification") != "VERIFIED"
    ):
        raise CurrentDependenceError("layered history receipt contract differs")
    output_authority = _object(authority.get("outputs"), "history outputs")
    receipt_outputs = _object(manifest.get("outputs"), "history receipt outputs")
    verified_output_content: dict[str, bytes] = {}
    for name in sorted(REQUIRED_HISTORY_FILES - {"generation.json"}):
        expected = _object(output_authority.get(name), f"history authority {name}")
        receipt = _object(receipt_outputs.get(name), f"history receipt {name}")
        if (expected.get("sha256"), expected.get("size_bytes")) != (
            receipt.get("sha256"),
            receipt.get("size_bytes"),
        ):
            raise CurrentDependenceError(
                f"layered history receipt authority differs: {name}"
            )
        # Excluded evidence is intentionally not consumed by dependence. Its
        # exact identity remains content-addressed by the accepted #320 receipt;
        # downstream verification needs only prove that the declared regular
        # file and byte size are still present. This also avoids needlessly
        # hydrating rejected private evidence on a different workstation.
        if name == "excluded.csv":
            path = root / name
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != expected.get("size_bytes")
            ):
                raise CurrentDependenceError(
                    "layered history excluded evidence identity differs"
                )
            continue
        content = _regular_file(root / name, f"layered history {name}")
        actual = (_sha256(content), len(content))
        if actual != (expected.get("sha256"), expected.get("size_bytes")):
            raise CurrentDependenceError(f"layered history authority differs: {name}")
        if actual != (receipt.get("sha256"), receipt.get("size_bytes")):
            raise CurrentDependenceError(
                f"layered history receipt output differs: {name}"
            )
        verified_output_content[name] = content

    segment_content = verified_output_content["segments.csv"]
    segments = _csv_rows(segment_content, SEGMENT_FIELDS, "layered history segments")
    if segment_content != _csv_content(SEGMENT_FIELDS, segments):
        raise CurrentDependenceError("layered history segments are not canonical")
    expected_segments = _objects(
        _object(contract.get("history_corrections"), "history corrections").get(
            "materialized_segment_identities"
        ),
        "materialized segment identities",
    )
    actual_by_id: dict[str, list[dict[str, str]]] = {}
    for row in segments:
        actual_by_id.setdefault(row["segment_id"], []).append(row)
    for expected in expected_segments:
        segment_id = _text(expected.get("segment_id"), "materialized segment_id")
        selected = actual_by_id.get(segment_id)
        if not selected:
            raise CurrentDependenceError(
                f"accepted history segment is absent: {segment_id}"
            )
        first = selected[0]
        static = {
            "calendar": first["calendar"],
            "evidence_layer": first["evidence_layer"],
            "frequency": first["frequency"],
            "mode_admission": first["mode_admission"],
            "return_basis": first["return_basis"],
            "return_type": first["return_type"],
            "segment_id": first["segment_id"],
            "source_generation_id": first["source_generation_id"],
            "source_id": first["source_id"],
            "strategy_id": first["strategy_id"],
            "strategy_version": first["strategy_version"],
        }
        if any(
            any(row[key] != value for key, value in static.items()) for row in selected
        ):
            raise CurrentDependenceError(
                f"accepted history segment metadata varies: {segment_id}"
            )
        identity = _return_identity(selected)
        comparable = {
            **static,
            **identity,
            "canonicalization": "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1",
            "source_generation_sha256": expected.get("source_generation_sha256"),
        }
        if comparable != expected:
            raise CurrentDependenceError(
                f"accepted history segment differs: {segment_id}"
            )

    required_strategies = [
        row
        for row in _objects(contract.get("strategies"), "financial MVP strategies")
        if row.get("product_requirement") == "REQUIRED"
        and _object(row.get("outcome_dispositions"), "strategy outcomes").get(
            "DEPENDENCE"
        )
        == "REQUIRED"
    ]
    required_ids = [str(row["strategy_id"]) for row in required_strategies]
    policy_series = _objects(policy.get("series"), "dependence policy series")
    if [row.get("strategy_id") for row in policy_series] != required_ids:
        raise CurrentDependenceError(
            "dependence policy perimeter differs from the product"
        )
    live_by_strategy: dict[str, list[dict[str, str]]] = {}
    selected_identities: list[dict[str, object]] = []
    expected_by_id = {str(row["segment_id"]): row for row in expected_segments}
    prehistory_identities = _selected_prehistory_identities(contract, required_ids)
    prehistory_by_strategy: dict[str, list[dict[str, str]]] = {}
    for identity in prehistory_identities:
        strategy_id = _text(identity.get("strategy_id"), "history strategy_id")
        segment_id = _text(identity.get("segment_id"), "materialized segment_id")
        rows = actual_by_id.get(segment_id)
        if not rows:
            raise CurrentDependenceError(
                f"admitted prehistory segment is absent: {segment_id}"
            )
        prehistory_by_strategy[strategy_id] = rows
    for strategy in required_strategies:
        strategy_id = str(strategy["strategy_id"])
        live_identities = [
            row
            for row in expected_segments
            if row.get("strategy_id") == strategy_id
            and row.get("mode_admission") == "INCLUDE_SELECTED_LIVE"
        ]
        if len(live_identities) != 1:
            raise CurrentDependenceError(
                f"required dependence strategy lacks one selected live segment: {strategy_id}"
            )
        identity = live_identities[0]
        selected = actual_by_id[str(identity["segment_id"])]
        if any(
            _day(row["date"], "history observation date") > evaluated_on
            for row in selected
        ):
            raise CurrentDependenceError("layered history exceeds the evaluation date")
        live_by_strategy[strategy_id] = selected
        selected_identities.append(dict(expected_by_id[str(identity["segment_id"])]))
    # The bundle packages the segments it projects, and the verifier expects
    # exactly that set, so the admitted prehistory travels with the live
    # selection rather than only reaching returns.csv.
    selected_identities.extend(
        dict(expected_by_id[str(identity["segment_id"])])
        for identity in prehistory_identities
    )
    common_dates = set.intersection(
        *(
            {row["date"] for row in live_by_strategy[strategy_id]}
            for strategy_id in required_ids
        )
    )
    if not common_dates:
        raise CurrentDependenceError("required histories have no common complete date")
    complete_through = _day(max(common_dates), "complete_through")
    projected: list[dict[str, str]] = []
    for policy_row in policy_series:
        strategy_id = str(policy_row["strategy_id"])
        series_id = _text(policy_row.get("series_id"), "dependence series_id")
        strategy = next(
            row for row in required_strategies if row["strategy_id"] == strategy_id
        )
        if strategy.get("history_series_id") != series_id:
            raise CurrentDependenceError(
                f"dependence series differs from product authority: {strategy_id}"
            )
        for row in (
            *prehistory_by_strategy.get(strategy_id, ()),
            *live_by_strategy[strategy_id],
        ):
            if _day(row["date"], "history observation date") <= complete_through:
                projected.append({field: row[field] for field in RETURN_FIELDS})
    projected.sort(
        key=lambda row: (
            required_ids.index(
                next(
                    item["strategy_id"]
                    for item in policy_series
                    if item["series_id"] == row["series_id"]
                )
            ),
            row["date"],
        )
    )
    if {row["series_id"] for row in projected} != {
        str(row["series_id"]) for row in policy_series
    }:
        raise CurrentDependenceError("dependence projection lacks a required series")
    coverage_content = verified_output_content["coverage.json"]
    return projected, selected_identities, complete_through, manifest, coverage_content


def _verified_allocations(
    generation_root: Path,
    policy: Mapping[str, Any],
    required_ids: Sequence[str],
    evaluated_on: date,
) -> tuple[dict[str, str], dict[str, Any]]:
    root = generation_root.resolve()
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.name != policy["allocation_authority"]["generation_id"]
    ):
        raise CurrentDependenceError("allocation generation identity differs")
    manifest_content = _regular_file(root / "generation.json", "allocation manifest")
    catalog_content = _regular_file(
        root / "portfolio-baseline.json", "allocation catalog"
    )
    authority = _object(policy.get("allocation_authority"), "allocation authority")
    if _sha256(manifest_content) != authority.get("manifest_sha256"):
        raise CurrentDependenceError("allocation manifest identity differs")
    if _sha256(catalog_content) != authority.get("catalog_sha256"):
        raise CurrentDependenceError("allocation catalog identity differs")
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_content)
    except ValueError as exc:
        raise CurrentDependenceError(f"allocation manifest is invalid: {exc}") from exc
    if (
        manifest.generation_id != authority.get("generation_id")
        or manifest.verification.value != "VERIFIED"
        or manifest.as_of_date.isoformat() != authority.get("as_of_date")
    ):
        raise CurrentDependenceError("allocation manifest authority differs")
    if manifest.as_of_date > evaluated_on:
        raise CurrentDependenceError("allocation manifest exceeds the evaluation date")
    artifacts = {item.artifact_id: item for item in manifest.artifacts}
    catalog_artifact = artifacts.get("normalized/portfolio-baseline.json")
    if catalog_artifact is None or (
        catalog_artifact.sha256,
        catalog_artifact.size_bytes,
    ) != (_sha256(catalog_content), len(catalog_content)):
        raise CurrentDependenceError("allocation catalog is not manifest-bound")
    catalog = _json_bytes(catalog_content, "allocation catalog")
    allocations = _objects(catalog.get("allocations"), "allocation catalog allocations")
    weights: dict[str, str] = {}
    for row in allocations:
        strategy_id = row.get("strategy_id")
        if row.get("scope") != "SYSTEMATIC" or strategy_id not in required_ids:
            continue
        if strategy_id in weights:
            raise CurrentDependenceError("allocation strategy is duplicated")
        weight = _text(row.get("systematic_weight"), "systematic allocation weight")
        parsed = Decimal(weight)
        if parsed < 0 or parsed > 1:
            raise CurrentDependenceError("systematic allocation weight is invalid")
        weights[str(strategy_id)] = weight
    if set(weights) != set(required_ids):
        raise CurrentDependenceError("allocation catalog lacks the required perimeter")
    return weights, {
        "as_of_date": manifest.as_of_date.isoformat(),
        "catalog_sha256": _sha256(catalog_content),
        "generation_id": manifest.generation_id,
        "manifest_sha256": _sha256(manifest_content),
    }


def _age_evidence(
    *,
    contract: Mapping[str, Any],
    policy_series: Sequence[Mapping[str, Any]],
    selected_identities: Sequence[Mapping[str, Any]],
    complete_through: date,
    evaluated_on: date,
) -> dict[str, Any]:
    source_by_id: dict[str, Mapping[str, Any]] = {}
    for row in _objects(contract.get("source_segments"), "financial MVP sources"):
        source_id = _text(row.get("source_id"), "financial MVP source_id")
        if source_id in source_by_id:
            raise CurrentDependenceError("financial MVP source_id is duplicated")
        source_by_id[source_id] = row
    series_by_strategy = {
        _text(row.get("strategy_id"), "dependence strategy_id"): _text(
            row.get("series_id"), "dependence series_id"
        )
        for row in policy_series
    }
    limiting_inputs: list[dict[str, Any]] = []
    for identity in selected_identities:
        # Age describes the live path. Admitted prehistory travels in the same
        # selection so it can be packaged, but it ends before the live segment
        # starts and never limits freshness -- except in a fixture where the
        # two share dates, which would make the producer and the verifier
        # disagree depending on which list each was handed.
        if identity.get("mode_admission") != "INCLUDE_SELECTED_LIVE":
            continue
        if (
            _day(identity.get("observed_end"), "history observed_end")
            != complete_through
        ):
            continue
        strategy_id = _text(identity.get("strategy_id"), "history strategy_id")
        source_id = _text(identity.get("source_id"), "history source_id")
        source = source_by_id.get(source_id)
        if source is None:
            raise CurrentDependenceError("history source lacks product authority")
        freshness = _object(source.get("freshness"), "history source freshness")
        warning_after_days = _nonnegative_int(
            freshness.get("max_age_days"), "history source max_age_days"
        )
        publication_lag_days = _nonnegative_int(
            freshness.get("publication_lag_days"),
            "history source publication_lag_days",
        )
        limiting_inputs.append(
            {
                "observed_end": complete_through.isoformat(),
                "publication_lag_days": publication_lag_days,
                "segment_id": _text(identity.get("segment_id"), "history segment_id"),
                "series_id": series_by_strategy[strategy_id],
                "source_generation_id": _hash(
                    identity.get("source_generation_id"),
                    "history source_generation_id",
                ),
                "source_id": source_id,
                "strategy_id": strategy_id,
                "warning_after_days": warning_after_days,
            }
        )
    if not limiting_inputs:
        raise CurrentDependenceError(
            "latest common complete date lacks an exact limiting input"
        )
    strategy_order = list(series_by_strategy)
    limiting_inputs.sort(key=lambda row: strategy_order.index(row["strategy_id"]))
    warning_after_days = min(int(row["warning_after_days"]) for row in limiting_inputs)
    calculation_age_days = (evaluated_on - complete_through).days
    if calculation_age_days < 0:
        raise CurrentDependenceError("dependence complete date exceeds evaluation date")
    return {
        "calculation_age_days": calculation_age_days,
        "complete_through": complete_through.isoformat(),
        "evaluated_on": evaluated_on.isoformat(),
        "limiting_inputs": limiting_inputs,
        "status": (
            "WARN_COMPLETE_DATA_AGE"
            if calculation_age_days > warning_after_days
            else "CURRENT_WITHIN_AGE_THRESHOLD"
        ),
        "warning_after_days": warning_after_days,
    }


def _provider_mappings(
    contract: Mapping[str, Any], policy_series: Sequence[Mapping[str, Any]]
) -> list[dict[str, object]]:
    census = _object(contract.get("current_census"), "current census")
    sleeves = _objects(census.get("account_sleeves"), "current census sleeves")
    providers: dict[str, set[str]] = {
        str(row["strategy_id"]): set() for row in policy_series
    }
    for sleeve in sleeves:
        strategy_id = sleeve.get("current_strategy_id")
        if strategy_id in providers and sleeve.get("role") == "ACTIVE_STRATEGY":
            providers[str(strategy_id)].add(
                _text(sleeve.get("provider_id"), "provider_id")
            )
    if any(not values for values in providers.values()):
        raise CurrentDependenceError("required strategy lacks an active provider lane")
    return [
        {
            "instruments": [],
            "providers": sorted(providers[str(row["strategy_id"])]),
            "scope": "CENSUS_PROVIDER_LANE_ONLY",
            "series_id": str(row["series_id"]),
            "venues": [],
        }
        for row in policy_series
    ]


def _required_dependence_strategies(
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        row
        for row in _objects(contract.get("strategies"), "financial MVP strategies")
        if row.get("product_requirement") == "REQUIRED"
        and _object(row.get("outcome_dispositions"), "strategy outcomes").get(
            "DEPENDENCE"
        )
        == "REQUIRED"
    ]


def _selected_live_identities(
    contract: Mapping[str, Any], required_ids: Sequence[str]
) -> list[dict[str, Any]]:
    identities = _objects(
        _object(contract.get("history_corrections"), "history corrections").get(
            "materialized_segment_identities"
        ),
        "materialized segment identities",
    )
    selected: list[dict[str, Any]] = []
    for strategy_id in required_ids:
        matches = [
            row
            for row in identities
            if row.get("strategy_id") == strategy_id
            and row.get("mode_admission") == "INCLUDE_SELECTED_LIVE"
        ]
        if len(matches) != 1:
            raise CurrentDependenceError(
                "required dependence strategy lacks one selected live segment: "
                f"{strategy_id}"
            )
        selected.append(matches[0])
    return selected


def _hybrid_disposition(prehistory_identities: Sequence[Mapping[str, Any]]) -> str:
    """State what the hybrid mode actually rests on.

    A mode that carries no admitted prehistory is live-only however it is
    labelled, which is what this field said unconditionally until the source
    began projecting the prehistory segments. Where prehistory is admitted the
    mode genuinely blends it with the live path; which individual series
    contributed is recorded per series by the correlation this source feeds.
    """

    if not prehistory_identities:
        return "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
    return "AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE"


def _selected_prehistory_identities(
    contract: Mapping[str, Any], required_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """The version-compatible prehistory segment per strategy, where one exists.

    A strategy whose backtest was never admitted contributes nothing, which is
    not an error: the layered history records that decision per strategy, and
    the evidence-mode split downstream degrades that series alone rather than
    the whole mode.
    """

    identities = _objects(
        _object(contract.get("history_corrections"), "history corrections").get(
            "materialized_segment_identities"
        ),
        "materialized segment identities",
    )
    selected: list[dict[str, Any]] = []
    for strategy_id in required_ids:
        matches = [
            row
            for row in identities
            if row.get("strategy_id") == strategy_id
            and row.get("mode_admission") == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        ]
        if len(matches) > 1:
            raise CurrentDependenceError(
                "required dependence strategy has more than one admitted "
                f"prehistory segment: {strategy_id}"
            )
        selected.extend(matches)
    return selected


def _packaged_projection(
    *,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    segment_content: bytes,
    evaluated_on: date,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], date]:
    segments = _csv_rows(segment_content, SEGMENT_FIELDS, "dependence segments")
    if segment_content != _csv_content(SEGMENT_FIELDS, segments):
        raise CurrentDependenceError("dependence segments are not canonical")
    required = _required_dependence_strategies(contract)
    required_ids = [str(row["strategy_id"]) for row in required]
    policy_series = _objects(policy.get("series"), "dependence policy series")
    if [row.get("strategy_id") for row in policy_series] != required_ids:
        raise CurrentDependenceError(
            "dependence policy perimeter differs from the product"
        )
    selected_identities = _selected_live_identities(contract, required_ids)
    prehistory_identities = _selected_prehistory_identities(contract, required_ids)
    expected_by_id = {
        _text(row.get("segment_id"), "materialized segment_id"): row
        for row in (*selected_identities, *prehistory_identities)
    }
    actual_by_id: dict[str, list[dict[str, str]]] = {}
    for row in segments:
        if row["segment_id"] not in expected_by_id:
            raise CurrentDependenceError(
                "dependence segments contain an unaccepted row"
            )
        actual_by_id.setdefault(row["segment_id"], []).append(row)
    if set(actual_by_id) != set(expected_by_id):
        raise CurrentDependenceError("dependence selected segment set differs")

    live_by_strategy: dict[str, list[dict[str, str]]] = {}
    for strategy_id, identity in zip(required_ids, selected_identities, strict=True):
        segment_id = _text(identity.get("segment_id"), "materialized segment_id")
        rows = actual_by_id[segment_id]
        first = rows[0]
        static = {
            "calendar": first["calendar"],
            "evidence_layer": first["evidence_layer"],
            "frequency": first["frequency"],
            "mode_admission": first["mode_admission"],
            "return_basis": first["return_basis"],
            "return_type": first["return_type"],
            "segment_id": first["segment_id"],
            "source_generation_id": first["source_generation_id"],
            "source_id": first["source_id"],
            "strategy_id": first["strategy_id"],
            "strategy_version": first["strategy_version"],
        }
        if any(any(row[key] != value for key, value in static.items()) for row in rows):
            raise CurrentDependenceError(
                f"dependence segment metadata varies: {segment_id}"
            )
        if static["strategy_id"] != strategy_id:
            raise CurrentDependenceError("dependence segment strategy differs")
        dates = [_day(row["date"], "dependence observation date") for row in rows]
        if len(dates) != len(set(dates)) or dates != sorted(dates):
            raise CurrentDependenceError(
                "dependence segment dates are not unique and sorted"
            )
        if any(item > evaluated_on for item in dates):
            raise CurrentDependenceError(
                "dependence segment exceeds the evaluation date"
            )
        comparable = {
            **static,
            **_return_identity(rows),
            "canonicalization": "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1",
            "source_generation_sha256": identity.get("source_generation_sha256"),
        }
        if comparable != identity:
            raise CurrentDependenceError(
                f"dependence accepted segment differs: {segment_id}"
            )
        live_by_strategy[strategy_id] = rows

    prehistory_by_strategy: dict[str, list[dict[str, str]]] = {}
    for identity in prehistory_identities:
        strategy_id = _text(identity.get("strategy_id"), "history strategy_id")
        segment_id = _text(identity.get("segment_id"), "materialized segment_id")
        rows = actual_by_id.get(segment_id)
        if not rows:
            raise CurrentDependenceError(
                f"admitted prehistory segment is absent: {segment_id}"
            )
        prehistory_by_strategy[strategy_id] = rows

    common_dates = set.intersection(
        *({row["date"] for row in live_by_strategy[item]} for item in required_ids)
    )
    if not common_dates:
        raise CurrentDependenceError("required histories have no common complete date")
    complete_through = _day(max(common_dates), "complete_through")
    projected: list[dict[str, str]] = []
    for policy_row in policy_series:
        strategy_id = str(policy_row["strategy_id"])
        series_id = _text(policy_row.get("series_id"), "dependence series_id")
        strategy = next(row for row in required if row["strategy_id"] == strategy_id)
        if strategy.get("history_series_id") != series_id:
            raise CurrentDependenceError(
                f"dependence series differs from product authority: {strategy_id}"
            )
        for row in (
            *prehistory_by_strategy.get(strategy_id, ()),
            *live_by_strategy[strategy_id],
        ):
            if _day(row["date"], "dependence observation date") <= complete_through:
                projected.append({field: row[field] for field in RETURN_FIELDS})
    series_order = {
        str(row["series_id"]): index for index, row in enumerate(policy_series)
    }
    projected.sort(key=lambda row: (series_order[row["series_id"]], row["date"]))
    return projected, selected_identities, complete_through


def _policy_calculation(
    policy_series: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], Decimal, Decimal]:
    raw_weights = {
        str(row["strategy_id"]): _text(
            row.get("allocation_weight"), "policy allocation_weight"
        )
        for row in policy_series
    }
    with localcontext() as context:
        context.prec = 50
        required_weight = sum(
            (Decimal(value) for value in raw_weights.values()), Decimal("0")
        )
        if required_weight <= 0 or required_weight > 1:
            raise CurrentDependenceError(
                "required dependence allocation perimeter is invalid"
            )
        normalized = {
            item: format(Decimal(value) / required_weight, "f")
            for item, value in raw_weights.items()
        }
        expected_loss = sum(
            (
                Decimal(normalized[str(row["strategy_id"])])
                * Decimal(str(row["adverse_loss_fraction"]))
                for row in policy_series
            ),
            Decimal("0"),
        )
    return normalized, required_weight, expected_loss


def _expected_spec(
    *,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    selected_identities: Sequence[Mapping[str, Any]],
    returns_content: bytes,
    evaluated_on: date,
) -> tuple[dict[str, Any], Decimal]:
    policy_series = _objects(policy.get("series"), "dependence policy series")
    normalized_weights, required_weight, expected_loss = _policy_calculation(
        policy_series
    )
    series_names = {
        str(row["strategy_id"]): str(row["series_id"]) for row in policy_series
    }
    priors = _objects(policy.get("conceptual_priors"), "conceptual priors")
    required_ids = [str(row["strategy_id"]) for row in policy_series]
    expected_pairs = {
        tuple(sorted((left, right)))
        for index, left in enumerate(required_ids)
        for right in required_ids[index + 1 :]
    }
    actual_pairs = {
        tuple(sorted((str(row["left_strategy_id"]), str(row["right_strategy_id"]))))
        for row in priors
    }
    if actual_pairs != expected_pairs or len(priors) != len(expected_pairs):
        raise CurrentDependenceError("conceptual prior pair set differs")
    parameters = _object(policy.get("calculation_parameters"), "calculation parameters")
    spec = {
        "as_of_date": evaluated_on.isoformat(),
        "conceptual_priors": [
            {
                "evidence_note": str(row["evidence_note"]),
                "left": min(
                    series_names[str(row["left_strategy_id"])],
                    series_names[str(row["right_strategy_id"])],
                ),
                "right": max(
                    series_names[str(row["left_strategy_id"])],
                    series_names[str(row["right_strategy_id"])],
                ),
                "value": str(row["value"]),
            }
            for row in priors
        ],
        "correlation_one": {
            "components": [
                {
                    "adverse_loss_fraction": str(row["adverse_loss_fraction"]),
                    "allocation_weight": normalized_weights[str(row["strategy_id"])],
                    "series_id": str(row["series_id"]),
                }
                for row in policy_series
            ],
            "expected_loss": format(expected_loss, "f"),
            "tolerance": str(parameters["correlation_one_tolerance"]),
        },
        "eigenvalue_floor": str(parameters["eigenvalue_floor"]),
        "evidence_mode_series": [
            {
                "live_evidence_layer": "ACCOUNT_LIVE",
                "live_version_ref": _text(
                    next(
                        row["live_implementation_identity"]["version_ref"]
                        for row in contract["strategies"]
                        if row["strategy_id"] == policy_row["strategy_id"]
                    ),
                    "live implementation version",
                ),
                "series_id": str(policy_row["series_id"]),
                "strategy_version": _text(
                    next(
                        row["strategy_version"]
                        for row in selected_identities
                        if row["strategy_id"] == policy_row["strategy_id"]
                    ),
                    "history strategy version",
                ),
            }
            for policy_row in policy_series
        ],
        "full_observed_weight_observations": parameters[
            "full_observed_weight_observations"
        ],
        "implementation_mappings": _provider_mappings(contract, policy_series),
        "maximum_observed_weight": str(parameters["maximum_observed_weight"]),
        "minimum_downside_observations": parameters["minimum_downside_observations"],
        "minimum_observations": parameters["minimum_observations"],
        "minimum_rolling_windows": parameters["minimum_rolling_windows"],
        "return_source": {
            "artifact_id": "current-dependence/returns.csv",
            "expected_sha256": _sha256(returns_content),
            "expected_size_bytes": len(returns_content),
            "relative_path": "returns.csv",
            "schema_name": "portfolio-layered-strategy-returns",
            "schema_version": "2",
        },
        "rolling_window": parameters["rolling_window"],
        "schema_version": 2,
        "series_ids": [str(row["series_id"]) for row in policy_series],
        "stress_cap": str(parameters["stress_cap"]),
        "stress_uplift": str(parameters["stress_uplift"]),
        "tail_quantile": str(parameters["tail_quantile"]),
    }
    return spec, required_weight


def build_source(
    *,
    contract_path: Path,
    policy_path: Path,
    history_generation_root: Path,
    allocation_generation_root: Path,
    evaluated_on: date,
) -> dict[str, bytes | dict[str, Any]]:
    contract, policy, identities = _load_authorities(contract_path, policy_path)
    (
        projection,
        segment_identities,
        complete_through,
        history_manifest,
        history_coverage_content,
    ) = _verified_history(history_generation_root, policy, contract, evaluated_on)
    policy_series = _objects(policy.get("series"), "dependence policy series")
    required_ids = [str(row["strategy_id"]) for row in policy_series]
    weights, allocation_identity = _verified_allocations(
        allocation_generation_root, policy, required_ids, evaluated_on
    )
    policy_weights = {
        str(row["strategy_id"]): str(row["allocation_weight"]) for row in policy_series
    }
    if weights != policy_weights:
        raise CurrentDependenceError(
            "dependence policy allocation weights differ from the governed snapshot"
        )
    series_names = {
        str(row["strategy_id"]): str(row["series_id"]) for row in policy_series
    }
    priors = _objects(policy.get("conceptual_priors"), "conceptual priors")
    expected_pairs = {
        tuple(sorted((left, right)))
        for index, left in enumerate(required_ids)
        for right in required_ids[index + 1 :]
    }
    actual_pairs = {
        tuple(sorted((str(row["left_strategy_id"]), str(row["right_strategy_id"]))))
        for row in priors
    }
    if actual_pairs != expected_pairs or len(priors) != len(expected_pairs):
        raise CurrentDependenceError("conceptual prior pair set differs")
    adverse = {
        str(row["strategy_id"]): Decimal(str(row["adverse_loss_fraction"]))
        for row in policy_series
    }
    with localcontext() as context:
        context.prec = 50
        required_weight = sum(Decimal(weights[item]) for item in required_ids)
        if required_weight <= 0 or required_weight > 1:
            raise CurrentDependenceError(
                "required dependence allocation perimeter is invalid"
            )
        normalized_weights = {
            item: format(Decimal(weights[item]) / required_weight, "f")
            for item in required_ids
        }
        expected_loss = sum(
            Decimal(normalized_weights[item]) * adverse[item] for item in required_ids
        )
    returns_content = _csv_content(RETURN_FIELDS, projection)
    parameters = _object(policy.get("calculation_parameters"), "calculation parameters")
    spec = {
        "as_of_date": evaluated_on.isoformat(),
        "conceptual_priors": [
            {
                "evidence_note": str(row["evidence_note"]),
                "left": sorted(
                    (
                        series_names[str(row["left_strategy_id"])],
                        series_names[str(row["right_strategy_id"])],
                    )
                )[0],
                "right": sorted(
                    (
                        series_names[str(row["left_strategy_id"])],
                        series_names[str(row["right_strategy_id"])],
                    )
                )[1],
                "value": str(row["value"]),
            }
            for row in priors
        ],
        "correlation_one": {
            "components": [
                {
                    "adverse_loss_fraction": str(row["adverse_loss_fraction"]),
                    "allocation_weight": normalized_weights[str(row["strategy_id"])],
                    "series_id": str(row["series_id"]),
                }
                for row in policy_series
            ],
            "expected_loss": format(expected_loss, "f"),
            "tolerance": str(parameters["correlation_one_tolerance"]),
        },
        "eigenvalue_floor": str(parameters["eigenvalue_floor"]),
        "evidence_mode_series": [
            {
                "live_evidence_layer": "ACCOUNT_LIVE",
                "live_version_ref": _text(
                    next(
                        row["live_implementation_identity"]["version_ref"]
                        for row in contract["strategies"]
                        if row["strategy_id"] == policy_row["strategy_id"]
                    ),
                    "live implementation version",
                ),
                "series_id": str(policy_row["series_id"]),
                "strategy_version": _text(
                    next(
                        row["strategy_version"]
                        for row in segment_identities
                        if row["strategy_id"] == policy_row["strategy_id"]
                    ),
                    "history strategy version",
                ),
            }
            for policy_row in policy_series
        ],
        "full_observed_weight_observations": parameters[
            "full_observed_weight_observations"
        ],
        "implementation_mappings": _provider_mappings(contract, policy_series),
        "maximum_observed_weight": str(parameters["maximum_observed_weight"]),
        "minimum_downside_observations": parameters["minimum_downside_observations"],
        "minimum_observations": parameters["minimum_observations"],
        "minimum_rolling_windows": parameters["minimum_rolling_windows"],
        "return_source": {
            "artifact_id": "current-dependence/returns.csv",
            "expected_sha256": _sha256(returns_content),
            "expected_size_bytes": len(returns_content),
            "relative_path": "returns.csv",
            "schema_name": "portfolio-layered-strategy-returns",
            "schema_version": "2",
        },
        "rolling_window": parameters["rolling_window"],
        "schema_version": 2,
        "series_ids": [str(row["series_id"]) for row in policy_series],
        "stress_cap": str(parameters["stress_cap"]),
        "stress_uplift": str(parameters["stress_uplift"]),
        "tail_quantile": str(parameters["tail_quantile"]),
    }
    spec_content = canonical_json_bytes(spec)
    selected_segments = [
        row
        for row in _csv_rows(
            _regular_file(
                history_generation_root / "segments.csv", "layered history segments"
            ),
            SEGMENT_FIELDS,
            "layered history segments",
        )
        if row["segment_id"]
        in {str(identity["segment_id"]) for identity in segment_identities}
    ]
    selected_segments.sort(
        key=lambda row: (
            required_ids.index(row["strategy_id"]),
            row["date"],
            row["segment_id"],
        )
    )
    segments_content = _csv_content(SEGMENT_FIELDS, selected_segments)
    age_evidence = _age_evidence(
        contract=contract,
        policy_series=policy_series,
        selected_identities=segment_identities,
        complete_through=complete_through,
        evaluated_on=evaluated_on,
    )
    source_identity = {
        "age_evidence": age_evidence,
        "allocation_authority": allocation_identity,
        "allocation_scope": {
            "excluded_weight": format(Decimal("1") - required_weight, "f"),
            "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
            "required_weight_before_normalization": format(required_weight, "f"),
        },
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "calculation_age_days": age_evidence["calculation_age_days"],
        "census_sha256": identities["census_sha256"],
        "complete_through": complete_through.isoformat(),
        "contract_raw_sha256": identities["contract_raw_sha256"],
        "contract_semantic_digest": identities["contract_semantic_digest"],
        "evaluated_on": evaluated_on.isoformat(),
        "excluded_product_strategy_ids": sorted(
            str(row["strategy_id"])
            for row in contract["strategies"]
            if row["product_requirement"] == "EXCLUDED"
        ),
        "history_authority": {
            "coverage": {
                "sha256": _sha256(history_coverage_content),
                "size_bytes": len(history_coverage_content),
            },
            "generation_id": history_manifest["generation_id"],
            "receipt_sha256": policy["history_authority"]["receipt_sha256"],
            "selected_segment_identities": sorted(
                segment_identities,
                key=lambda row: (str(row["strategy_id"]), str(row["segment_id"])),
            ),
        },
        "mode_dispositions": {
            "LIVE_ONLY": "AVAILABLE_ACCOUNT_LIVE",
            "LIVE_PLUS_BACKTEST": _hybrid_disposition(
                _selected_prehistory_identities(contract, required_ids)
            ),
        },
        "pair_count": len(expected_pairs),
        "pair_mode_count": len(expected_pairs) * 2,
        "policy_id": POLICY_ID,
        "policy_sha256": identities["policy_sha256"],
        "required_strategy_ids": required_ids,
        "schema_version": SCHEMA_VERSION,
        "spec_sha256": _sha256(spec_content),
        "spec_size_bytes": len(spec_content),
        "returns_sha256": _sha256(returns_content),
        "returns_size_bytes": len(returns_content),
        "segments_sha256": _sha256(segments_content),
        "segments_size_bytes": len(segments_content),
        "source_age_status": age_evidence["status"],
        "warning_after_days": age_evidence["warning_after_days"],
    }
    bundle_id = _sha256(canonical_json_bytes(source_identity))
    receipt = {"bundle_id": bundle_id, **source_identity}
    return {
        "dependence-source.json": canonical_json_bytes(receipt),
        "history-coverage.json": history_coverage_content,
        "returns.csv": returns_content,
        "segments.csv": segments_content,
        "spec.json": spec_content,
        "receipt": receipt,
    }


def write_source(
    output_root: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    built = build_source(**kwargs)
    receipt = _object(built["receipt"], "dependence source receipt")
    bundle_id = _hash(receipt.get("bundle_id"), "dependence bundle_id")
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / bundle_id
    contents = {
        name: value
        for name, value in built.items()
        if name != "receipt" and isinstance(value, bytes)
    }
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise CurrentDependenceError("dependence destination is not a directory")
        if {path.name for path in destination.iterdir()} != set(contents):
            raise CurrentDependenceError("dependence destination inventory differs")
        for name, content in contents.items():
            if _regular_file(destination / name, f"existing {name}") != content:
                raise CurrentDependenceError(
                    "dependence destination differs from exact rebuild"
                )
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".dependence-", dir=root))
        try:
            for name, content in contents.items():
                path = temporary / name
                path.write_bytes(content)
            os.replace(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return {
        "bundle_id": bundle_id,
        "complete_through": receipt["complete_through"],
        "destination": str(destination),
        "pair_count": receipt["pair_count"],
        "pair_mode_count": receipt["pair_mode_count"],
        "source_age_status": receipt["source_age_status"],
    }


def verify_source(source_root: Path, **kwargs: Any) -> dict[str, Any]:
    built = build_source(**kwargs)
    receipt = _object(built["receipt"], "dependence source receipt")
    if source_root.resolve().name != receipt["bundle_id"]:
        raise CurrentDependenceError("dependence source directory identity differs")
    expected_files = {
        "dependence-source.json",
        "history-coverage.json",
        "returns.csv",
        "segments.csv",
        "spec.json",
    }
    if (
        source_root.is_symlink()
        or not source_root.is_dir()
        or {path.name for path in source_root.iterdir()} != expected_files
    ):
        raise CurrentDependenceError("dependence source inventory differs")
    for name in sorted(expected_files):
        expected = built[name]
        assert isinstance(expected, bytes)
        if _regular_file(source_root / name, f"dependence source {name}") != expected:
            raise CurrentDependenceError(f"dependence source differs: {name}")
    return {
        "bundle_id": receipt["bundle_id"],
        "complete_through": receipt["complete_through"],
        "pair_count": receipt["pair_count"],
        "pair_mode_count": receipt["pair_mode_count"],
        "verification": "VALID",
    }


def verify_packaged_source(
    source_root: Path,
    *,
    contract_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    root = source_root.resolve()
    expected_files = {
        "dependence-source.json",
        "history-coverage.json",
        "returns.csv",
        "segments.csv",
        "spec.json",
    }
    if (
        root.is_symlink()
        or not root.is_dir()
        or {path.name for path in root.iterdir()} != expected_files
    ):
        raise CurrentDependenceError("dependence source inventory differs")
    contents = {
        name: _regular_file(root / name, f"dependence source {name}")
        for name in sorted(expected_files)
    }
    receipt_content = contents["dependence-source.json"]
    receipt = _json_bytes(receipt_content, "dependence source receipt")
    if canonical_json_bytes(receipt) != receipt_content:
        raise CurrentDependenceError("dependence source receipt is not canonical")
    bundle_id = _hash(receipt.get("bundle_id"), "dependence bundle_id")
    source_identity = dict(receipt)
    source_identity.pop("bundle_id")
    if (
        _sha256(canonical_json_bytes(source_identity)) != bundle_id
        or root.name != bundle_id
    ):
        raise CurrentDependenceError("dependence bundle identity differs")
    history_coverage = _json_bytes(
        contents["history-coverage.json"], "dependence history coverage"
    )
    if canonical_json_bytes(history_coverage) != contents["history-coverage.json"]:
        raise CurrentDependenceError("dependence history coverage is not canonical")
    receipt_history_authority = _object(
        receipt.get("history_authority"), "dependence history authority"
    )
    receipt_coverage = _object(
        receipt_history_authority.get("coverage"), "dependence history coverage receipt"
    )
    if receipt_coverage != {
        "sha256": _sha256(contents["history-coverage.json"]),
        "size_bytes": len(contents["history-coverage.json"]),
    }:
        raise CurrentDependenceError("dependence history coverage differs")
    epoch_hpr_receipts = history_coverage.get("epoch_hpr_receipts", [])
    if not isinstance(epoch_hpr_receipts, list):
        raise CurrentDependenceError("dependence history epoch HPR receipts differ")

    contract, policy, identities = _load_authorities(contract_path, policy_path)
    evaluated_on = _day(receipt.get("evaluated_on"), "dependence evaluated_on")
    projection, selected_identities, complete_through = _packaged_projection(
        contract=contract,
        policy=policy,
        segment_content=contents["segments.csv"],
        evaluated_on=evaluated_on,
    )
    returns_content = _csv_content(RETURN_FIELDS, projection)
    if contents["returns.csv"] != returns_content:
        raise CurrentDependenceError("dependence projected returns differ")
    expected_spec, required_weight = _expected_spec(
        contract=contract,
        policy=policy,
        selected_identities=selected_identities,
        returns_content=returns_content,
        evaluated_on=evaluated_on,
    )
    spec_content = contents["spec.json"]
    spec = _json_bytes(spec_content, "dependence spec")
    if canonical_json_bytes(spec) != spec_content or spec != expected_spec:
        raise CurrentDependenceError("dependence spec authority differs")

    policy_series = _objects(policy.get("series"), "dependence policy series")
    required_ids = [str(row["strategy_id"]) for row in policy_series]
    allocation_authority = _object(
        policy.get("allocation_authority"), "allocation authority"
    )
    if (
        _day(allocation_authority.get("as_of_date"), "allocation as_of_date")
        > evaluated_on
    ):
        raise CurrentDependenceError("allocation manifest exceeds the evaluation date")
    pair_count = len(required_ids) * (len(required_ids) - 1) // 2
    age_evidence = _age_evidence(
        contract=contract,
        policy_series=policy_series,
        selected_identities=selected_identities,
        complete_through=complete_through,
        evaluated_on=evaluated_on,
    )
    expected_identity = {
        "age_evidence": age_evidence,
        "allocation_authority": allocation_authority,
        "allocation_scope": {
            "excluded_weight": format(Decimal("1") - required_weight, "f"),
            "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
            "required_weight_before_normalization": format(required_weight, "f"),
        },
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "calculation_age_days": age_evidence["calculation_age_days"],
        "census_sha256": identities["census_sha256"],
        "complete_through": complete_through.isoformat(),
        "contract_raw_sha256": identities["contract_raw_sha256"],
        "contract_semantic_digest": identities["contract_semantic_digest"],
        "evaluated_on": evaluated_on.isoformat(),
        "excluded_product_strategy_ids": sorted(
            str(row["strategy_id"])
            for row in contract["strategies"]
            if row["product_requirement"] == "EXCLUDED"
        ),
        "history_authority": {
            "coverage": {
                "sha256": _sha256(contents["history-coverage.json"]),
                "size_bytes": len(contents["history-coverage.json"]),
            },
            "generation_id": policy["history_authority"]["generation_id"],
            "receipt_sha256": policy["history_authority"]["receipt_sha256"],
            "selected_segment_identities": sorted(
                (
                    *selected_identities,
                    *_selected_prehistory_identities(contract, required_ids),
                ),
                key=lambda row: (str(row["strategy_id"]), str(row["segment_id"])),
            ),
        },
        "mode_dispositions": {
            "LIVE_ONLY": "AVAILABLE_ACCOUNT_LIVE",
            "LIVE_PLUS_BACKTEST": _hybrid_disposition(
                _selected_prehistory_identities(contract, required_ids)
            ),
        },
        "pair_count": pair_count,
        "pair_mode_count": pair_count * 2,
        "policy_id": POLICY_ID,
        "policy_sha256": identities["policy_sha256"],
        "required_strategy_ids": required_ids,
        "returns_sha256": _sha256(returns_content),
        "returns_size_bytes": len(returns_content),
        "schema_version": SCHEMA_VERSION,
        "segments_sha256": _sha256(contents["segments.csv"]),
        "segments_size_bytes": len(contents["segments.csv"]),
        "source_age_status": age_evidence["status"],
        "spec_sha256": _sha256(spec_content),
        "spec_size_bytes": len(spec_content),
        "warning_after_days": age_evidence["warning_after_days"],
    }
    if source_identity != expected_identity:
        # Name the fields that moved. Which part of the receipt disagrees is
        # the whole diagnostic, and recovering it by bisection is expensive.
        differing = sorted(
            key
            for key in set(source_identity) | set(expected_identity)
            if source_identity.get(key) != expected_identity.get(key)
        )
        raise CurrentDependenceError(
            "dependence source receipt authority differs: " + ", ".join(differing)
        )
    return {
        "bundle_id": bundle_id,
        "complete_through": complete_through.isoformat(),
        "epoch_hpr_receipts": epoch_hpr_receipts,
        "pair_count": pair_count,
        "pair_mode_count": pair_count * 2,
        "verification": "VALID",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "verify"):
        item = subparsers.add_parser(command)
        item.add_argument("--contract", required=True, type=Path)
        item.add_argument("--policy", required=True, type=Path)
        item.add_argument("--history-generation", required=True, type=Path)
        item.add_argument("--allocation-generation", required=True, type=Path)
        item.add_argument("--evaluated-on", required=True, type=date.fromisoformat)
        item.add_argument("--output-root", required=True, type=Path)
        if command == "verify":
            item.add_argument("--source-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kwargs = {
        "contract_path": args.contract,
        "policy_path": args.policy,
        "history_generation_root": args.history_generation,
        "allocation_generation_root": args.allocation_generation,
        "evaluated_on": args.evaluated_on,
    }
    if args.command == "build":
        result = write_source(args.output_root, **kwargs)
    else:
        result = verify_source(args.source_root, **kwargs)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
