"""Build and publish the first private, coverage-aware portfolio baseline.

The repository owns this deterministic builder and its schemas.  The private
specification, source records, normalized catalog, and CURRENT pointer remain
outside Git.  Every input is an explicit, hash-bound file; this tool never
walks a directory or calls a provider.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
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
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import (
    CashFlow,
    HwmInputsContractError,
    MappingStatus,
    NavObservation,
    ObservedAmount,
    Transaction,
    decimal_text,
    hwm_policy_exclusions,
    validate_hwm_inputs_envelope,
)


SPEC_SCHEMA_VERSION: Final = 3
SUPPORTED_SPEC_SCHEMA_VERSIONS: Final = frozenset({1, 2, 3})
CATALOG_SCHEMA_VERSION: Final = 1
BUILDER_VERSION: Final = "1.2.0"
POLICY_VERSION: Final = "PORT-PERF-1"
CURRENT_STRATEGY_COUNT: Final = 9
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SOURCE_KEYS: Final = {
    "alpaca-events",
    "alpaca-flows",
    "alpaca-nav",
    "attribution-diagnostic",
    "combined-control-status",
    "component-decision-table",
    "cost-economics",
    "current-portfolio-summary",
    "data-coverage-matrix",
    "evidence-layer-metrics",
    "hwm-status",
    "ibkr-ledger",
    "implementation-gap",
    "phase2-evidence-summary",
    "schwab-flows",
    "schwab-nav",
    "strategy-live-metrics",
}

# Optional governed HWM evidence sources.  Each key, when present, is admitted
# as a hash-bound source artifact whose schema_name MUST equal the key — the
# schema_name is the artifact role the hwm_inputs envelope resolves against.
_HWM_INPUTS_SOURCE_KEY: Final = "hwm-inputs"
_HWM_GOVERNED_SOURCE_KEYS: Final = frozenset(
    {
        "hwm-anchor-acceptance",
        "hwm-calendar-authority-account",
        "hwm-calendar-authority-provider",
        "hwm-calendar-authority-legacy",
        "hwm-flow-attestation",
        "hwm-fx-attestation",
    }
)
_HWM_ANCHOR_ACCEPTANCE_ROLE: Final = "hwm-anchor-acceptance"
_HWM_CALENDAR_AUTHORITY_ROLES: Final = {
    "ACCOUNT": "hwm-calendar-authority-account",
    "PROVIDER": "hwm-calendar-authority-provider",
    "LEGACY": "hwm-calendar-authority-legacy",
}
_HWM_VALID_LANES: Final = frozenset({"ACCOUNT", "PROVIDER", "LEGACY"})
_HWM_VALID_CALENDAR_KINDS: Final = frozenset(
    {"EXCHANGE", "ALWAYS_ON", "PROVIDER_PUBLICATION"}
)
_HWM_VALID_DISPOSITIONS: Final = frozenset(
    {
        "AVAILABLE",
        "UNAVAILABLE",
        "NOT_ADMITTED",
        "EXPECTED_NON_TRADING",
        "EXCLUDED_BY_POLICY",
    }
)
_HWM_VALID_FLOW_STATUSES: Final = frozenset({"OBSERVED", "CONFIRMED_ZERO", "MISSING"})
_HWM_VALID_FX_STATUSES: Final = frozenset(
    {"OBSERVED", "CONFIRMED_ONE", "NOT_REQUIRED", "MISSING"}
)
_HWM_PERIOD_REQUIRED_KEYS: Final = frozenset(
    {
        "strategy_id",
        "date",
        "lane",
        "disposition",
        "beginning_capital_usd",
        "return_source_record_id",
        "flow_status",
        "fx_status",
    }
)
_HWM_PERIOD_KEYS: Final = _HWM_PERIOD_REQUIRED_KEYS | {
    "component_account_ids",
    "flow_source_record_ids",
    "fx_source_record_ids",
}
_HWM_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[1] / "contracts" / "portfolio-hwm-policy-v3.json"
)

_CSV_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "alpaca-events": (
        "date",
        "type",
        "symbol",
        "qty",
        "price",
        "cash_after",
        "nav_after",
    ),
    "alpaca-flows": ("account", "date", "bucket", "amount", "note"),
    "alpaca-nav": ("date", "nav"),
    "attribution-diagnostic": ("institution", "account", "date", "nav", "note"),
    "component-decision-table": (
        "component",
        "current_value",
        "systematic_weight",
        "conceptual_family",
        "principal_overlap",
        "actual_or_nearest_live_cumulative_return",
        "actual_or_nearest_live_max_drawdown",
        "underwriting_stressed_component_max_drawdown",
        "preliminary_verdict",
        "required_gate",
    ),
    "cost-economics": (
        "component",
        "current_value",
        "systematic_weight",
        "annual_external_cost_dollars",
        "annual_external_cost_rate_at_current_size",
        "cost_status",
        "minimum_sleeve_for_1pct_fixed_cost",
        "retail_economics_flag",
    ),
    "data-coverage-matrix": (
        "component",
        "current_value",
        "systematic_weight",
        "account_specific_daily_series",
        "daily_start",
        "daily_end",
        "return_observations",
        "current_evidence_tier",
        "phase2_limitation",
    ),
    "evidence-layer-metrics": (
        "component",
        "layer",
        "provenance",
        "start_date",
        "end_date",
        "observations",
        "calendar_days",
        "cumulative_return",
        "descriptive_cagr",
        "annualized_volatility",
        "max_drawdown",
        "ulcer_index",
        "upi_vs_13w_treasury",
        "worst_period_return",
        "status",
    ),
    "ibkr-ledger": (
        "date",
        "account",
        "starting_value",
        "ending_value",
        "twr",
        "external_flow",
        "transfer_flow",
        "source_file",
    ),
    "implementation-gap": (
        "component",
        "provider_layer",
        "account_layer",
        "status",
        "overlap_start",
        "overlap_end",
        "overlap_observations",
        "provider_cumulative_return",
        "account_cumulative_return",
        "account_minus_provider_cumulative",
        "daily_return_correlation",
        "annualized_tracking_error",
        "mean_daily_account_minus_provider",
        "worst_daily_shortfall",
        "note",
    ),
    "schwab-flows": (
        "account",
        "date",
        "bucket",
        "amount",
        "action",
        "description",
        "counterparty_suffix",
    ),
    "schwab-nav": ("account", "date", "nav"),
    "strategy-live-metrics": (
        "first_date",
        "last_date",
        "source_return_observations",
        "return_observations",
        "calendar_days",
        "cumulative_return",
        "cagr",
        "annualized_volatility",
        "sharpe_period_rf",
        "max_drawdown",
        "ulcer_index",
        "upi_period_rf",
        "rf_13w_average",
        "rf_13w_latest",
        "worst_day",
        "best_day",
        "negative_day_share",
        "downside_deviation_daily",
        "historical_cvar_95_daily",
        "strategy",
        "source_reported_twr",
        "twr_reconstruction_difference",
        "spy_cumulative_return",
        "spy_cagr",
        "spy_max_drawdown",
        "spy_ulcer_index",
        "bil_cumulative_return",
        "bil_cagr",
        "bil_max_drawdown",
        "bil_ulcer_index",
        "cagr_minus_spy",
    ),
}


class BaselineValidationError(ValueError):
    """Private inputs or normalized facts violate the baseline contract."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BaselineValidationError(f"{field_name} must be non-empty text")
    return value.strip()


def _identifier(value: object, field_name: str) -> str:
    accepted = _text(value, field_name)
    if _IDENTIFIER.fullmatch(accepted) is None:
        raise BaselineValidationError(f"{field_name} is not a portable identifier")
    return accepted


def _strict_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BaselineValidationError(
            f"{name} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BaselineValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise BaselineValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _date(value: object, field_name: str) -> date:
    accepted = _text(value, field_name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise BaselineValidationError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != accepted:
        raise BaselineValidationError(f"{field_name} must use canonical ISO format")
    return parsed


def _decimal(
    value: object, field_name: str, *, allow_blank: bool = False
) -> Decimal | None:
    if not isinstance(value, str):
        raise BaselineValidationError(f"{field_name} must be decimal text")
    if not value.strip() and allow_blank:
        return None
    try:
        accepted = Decimal(value.strip())
    except InvalidOperation as exc:
        raise BaselineValidationError(f"{field_name} must be decimal text") from exc
    if not accepted.is_finite():
        raise BaselineValidationError(f"{field_name} must be finite")
    return accepted


def _portable_relative(value: object, field_name: str) -> str:
    accepted = _text(value, field_name)
    path = PurePosixPath(accepted)
    if (
        path.is_absolute()
        or "\\" in accepted
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BaselineValidationError(f"{field_name} must be a portable relative path")
    return accepted


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineValidationError(f"{field_name} is not readable JSON") from exc
    return _object(decoded, field_name)


def _read_csv(path: Path, source_key: str) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise BaselineValidationError(
            f"{source_key} is not readable UTF-8 CSV"
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    expected = _CSV_FIELDS[source_key]
    if tuple(reader.fieldnames or ()) != expected:
        raise BaselineValidationError(f"{source_key} header differs")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(item is None for item in row.values()):
            raise BaselineValidationError(f"{source_key} has a malformed row")
        rows.append({str(key): value or "" for key, value in row.items()})
    return rows


def _record_id(prefix: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _load_spec(spec_path: Path) -> dict[str, Any]:
    spec = _load_json(spec_path, "spec")
    version = spec.get("schema_version")
    if version not in SUPPORTED_SPEC_SCHEMA_VERSIONS:
        raise BaselineValidationError("spec schema version is unsupported")
    keys = {
        "as_of_date",
        "policy_version",
        "provenance",
        "replay",
        "roots",
        "schema_version",
        "sources",
    }
    if version == 2:
        keys.add("provider_generations")
    elif version == SPEC_SCHEMA_VERSION:
        keys.add("provider_ledger")
    _strict_keys(
        spec,
        keys,
        "spec",
    )
    if spec["policy_version"] != POLICY_VERSION:
        raise BaselineValidationError("spec policy version is unsupported")
    _date(spec["as_of_date"], "spec.as_of_date")
    return spec


def _schwab_generation(
    value: object,
    roots: Mapping[str, Path],
    accounts: Sequence[Mapping[str, Any]],
    baseline_as_of: date,
) -> tuple[
    dict[str, Any],
    tuple[SourceArtifact, ...],
    list[dict[str, Any]],
]:
    providers = _object(value, "provider_generations")
    _strict_keys(providers, {"schwab"}, "provider_generations")
    ref = _object(providers["schwab"], "provider_generations.schwab")
    _strict_keys(
        ref,
        {"catalog_sha256", "generation_id", "manifest_sha256", "root_id"},
        "provider_generations.schwab",
    )
    root_id = _identifier(ref["root_id"], "provider_generations.schwab.root_id")
    if root_id not in roots:
        raise BaselineValidationError("Schwab provider root is unknown")
    generation_id = _text(ref["generation_id"], "Schwab generation_id")
    catalog_sha256 = _text(ref["catalog_sha256"], "Schwab catalog_sha256")
    manifest_sha256 = _text(ref["manifest_sha256"], "Schwab manifest_sha256")
    if any(
        _SHA256.fullmatch(item) is None
        for item in (generation_id, catalog_sha256, manifest_sha256)
    ):
        raise BaselineValidationError("Schwab provider identity is invalid")
    root = roots[root_id]
    try:
        pointer = read_current(root)
    except (OSError, ValueError) as exc:
        raise BaselineValidationError(
            "Schwab provider pointer is not accepted"
        ) from exc
    if (
        pointer.generation_id != generation_id
        or pointer.manifest_sha256 != manifest_sha256
    ):
        raise BaselineValidationError("Schwab provider generation is not current")
    generation_root = root / "generations" / generation_id
    catalog_path = generation_root / "schwab-generation.json"
    manifest_path = generation_root / "generation.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (catalog_path, manifest_path)
    ):
        raise BaselineValidationError("Schwab provider generation is incomplete")
    actual_catalog_hash, catalog_size = hash_file(catalog_path)
    actual_manifest_hash, manifest_size = hash_file(manifest_path)
    if actual_catalog_hash != catalog_sha256 or actual_manifest_hash != manifest_sha256:
        raise BaselineValidationError("Schwab provider generation hash differs")
    catalog = _load_json(catalog_path, "Schwab provider catalog")
    if catalog.get("catalog_schema_version") != 1:
        raise BaselineValidationError("Schwab provider catalog schema is unsupported")
    provider_as_of = _date(catalog.get("as_of_date"), "Schwab provider as_of_date")
    if provider_as_of > baseline_as_of:
        raise BaselineValidationError(
            "Schwab provider generation is newer than baseline"
        )
    accepted_accounts = {
        str(item["account_id"]) for item in accounts if item.get("provider") == "SCHWAB"
    }
    provider_accounts = {
        _text(item.get("account_id"), "Schwab provider account_id")
        for item in _objects(catalog.get("accounts"), "Schwab provider accounts")
    }
    if provider_accounts != accepted_accounts:
        raise BaselineValidationError("Schwab provider account scope differs")
    artifacts = (
        SourceArtifact(
            artifact_id="providers/schwab/generation.json",
            sha256=actual_manifest_hash,
            size_bytes=manifest_size,
            as_of_date=provider_as_of,
            schema_name="generation-manifest",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="providers/schwab/schwab-generation.json",
            sha256=actual_catalog_hash,
            size_bytes=catalog_size,
            as_of_date=provider_as_of,
            schema_name="schwab-generation-catalog",
            schema_version="1",
        ),
    )
    links = [
        {
            "artifact_id": item.artifact_id,
            "as_of_date": provider_as_of.isoformat(),
            "schema_name": item.schema_name,
            "schema_version": item.schema_version,
            "sha256": item.sha256,
            "source_key": "schwab-provider-generation",
        }
        for item in artifacts
    ]
    return catalog, artifacts, links


def _provider_ledger(
    value: object,
    roots: Mapping[str, Path],
    baseline_as_of: date,
) -> tuple[dict[str, Any], tuple[SourceArtifact, ...], list[dict[str, Any]]]:
    ref = _object(value, "provider_ledger")
    _strict_keys(
        ref,
        {"catalog_sha256", "generation_id", "manifest_sha256", "root_id"},
        "provider_ledger",
    )
    root_id = _identifier(ref["root_id"], "provider_ledger.root_id")
    if root_id not in roots:
        raise BaselineValidationError("provider-ledger root is unknown")
    generation_id = _text(ref["generation_id"], "provider_ledger.generation_id")
    catalog_sha256 = _text(ref["catalog_sha256"], "provider_ledger.catalog_sha256")
    manifest_sha256 = _text(ref["manifest_sha256"], "provider_ledger.manifest_sha256")
    if any(
        _SHA256.fullmatch(item) is None
        for item in (generation_id, catalog_sha256, manifest_sha256)
    ):
        raise BaselineValidationError("provider-ledger identity is invalid")
    generation_root = roots[root_id] / "generations" / generation_id
    catalog_path = generation_root / "provider-ledger.json"
    manifest_path = generation_root / "generation.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (catalog_path, manifest_path)
    ):
        raise BaselineValidationError("provider-ledger generation is incomplete")
    actual_catalog_hash, catalog_size = hash_file(catalog_path)
    actual_manifest_hash, manifest_size = hash_file(manifest_path)
    if actual_catalog_hash != catalog_sha256 or actual_manifest_hash != manifest_sha256:
        raise BaselineValidationError("provider-ledger generation hash differs")
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    except ValueError as exc:
        raise BaselineValidationError("provider-ledger manifest is invalid") from exc
    if (
        manifest.generation_id != generation_id
        or manifest.verification.value != "VERIFIED"
    ):
        raise BaselineValidationError("provider-ledger generation is not verified")
    catalog = _load_json(catalog_path, "provider-ledger catalog")
    if catalog.get("catalog_schema_version") != 1:
        raise BaselineValidationError("provider-ledger catalog schema is unsupported")
    provider_as_of = _date(catalog.get("as_of_date"), "provider-ledger as_of_date")
    if provider_as_of > baseline_as_of:
        raise BaselineValidationError("provider-ledger is newer than baseline")
    provider_ids = {
        _text(item.get("provider_id"), "provider-ledger provider_id")
        for item in _objects(
            catalog.get("provider_generations"), "provider-ledger generations"
        )
    }
    if provider_ids != {
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    }:
        raise BaselineValidationError("provider-ledger provider scope differs")
    artifacts = (
        SourceArtifact(
            artifact_id="providers/provider-ledger/generation.json",
            sha256=actual_manifest_hash,
            size_bytes=manifest_size,
            as_of_date=provider_as_of,
            schema_name="generation-manifest",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="providers/provider-ledger/provider-ledger.json",
            sha256=actual_catalog_hash,
            size_bytes=catalog_size,
            as_of_date=provider_as_of,
            schema_name="provider-ledger-catalog",
            schema_version="1",
        ),
    )
    links = [
        {
            "artifact_id": item.artifact_id,
            "as_of_date": provider_as_of.isoformat(),
            "schema_name": item.schema_name,
            "schema_version": item.schema_version,
            "sha256": item.sha256,
            "source_key": "provider-ledger-generation",
        }
        for item in artifacts
    ]
    return catalog, artifacts, links


def _roots(value: object) -> dict[str, Path]:
    roots = _object(value, "roots")
    if not roots:
        raise BaselineValidationError("roots must not be empty")
    accepted: dict[str, Path] = {}
    for root_id, path_value in roots.items():
        identifier = _identifier(root_id, "root_id")
        path = Path(_text(path_value, f"roots.{root_id}"))
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise BaselineValidationError(
                f"root {root_id} is not a real absolute directory"
            )
        accepted[identifier] = path
    return accepted


def _source_path(root: Path, relative_path: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise BaselineValidationError("configured source is missing or linked")
    return candidate


def _sources(
    value: object, roots: Mapping[str, Path], generation_as_of: date
) -> tuple[dict[str, Path], tuple[SourceArtifact, ...], list[dict[str, Any]]]:
    values = _object(value, "sources")
    actual = set(values)
    if not _SOURCE_KEYS.issubset(actual):
        raise BaselineValidationError(
            f"source keys missing: {sorted(_SOURCE_KEYS - actual)}"
        )
    unknown = (
        actual - _SOURCE_KEYS - {_HWM_INPUTS_SOURCE_KEY} - _HWM_GOVERNED_SOURCE_KEYS
    )
    if unknown:
        raise BaselineValidationError(f"source keys unexpected: {sorted(unknown)}")
    paths: dict[str, Path] = {}
    artifacts: list[SourceArtifact] = []
    links: list[dict[str, Any]] = []
    for source_key in sorted(values):
        if source_key == _HWM_INPUTS_SOURCE_KEY:
            continue  # optional, processed separately in _load_hwm_inputs_envelope
        ref = _object(values[source_key], f"sources.{source_key}")
        _strict_keys(
            ref,
            {
                "as_of_date",
                "expected_sha256",
                "relative_path",
                "root_id",
                "schema_name",
                "schema_version",
            },
            f"sources.{source_key}",
        )
        root_id = _identifier(ref["root_id"], f"sources.{source_key}.root_id")
        if root_id not in roots:
            raise BaselineValidationError(f"source {source_key} has unknown root")
        relative_path = _portable_relative(
            ref["relative_path"], f"sources.{source_key}.relative_path"
        )
        expected_hash = _text(
            ref["expected_sha256"], f"sources.{source_key}.expected_sha256"
        )
        if _SHA256.fullmatch(expected_hash) is None:
            raise BaselineValidationError(f"source {source_key} hash is invalid")
        source_as_of = _date(ref["as_of_date"], f"sources.{source_key}.as_of_date")
        if source_as_of > generation_as_of:
            raise BaselineValidationError(
                f"source {source_key} is newer than the baseline"
            )
        path = _source_path(roots[root_id], relative_path)
        actual_hash, size = hash_file(path)
        if actual_hash != expected_hash:
            raise BaselineValidationError(f"source {source_key} hash differs")
        schema_name = _text(ref["schema_name"], f"sources.{source_key}.schema_name")
        schema_version = _text(
            ref["schema_version"], f"sources.{source_key}.schema_version"
        )
        if source_key in _HWM_GOVERNED_SOURCE_KEYS and schema_name != source_key:
            raise BaselineValidationError(
                f"source {source_key} schema_name must equal the governed role "
                f"{source_key!r}"
            )
        paths[source_key] = path
        artifact_id = f"sources/{source_key}/{hashlib.sha256(relative_path.encode()).hexdigest()[:16]}"
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=actual_hash,
                size_bytes=size,
                as_of_date=source_as_of,
                schema_name=schema_name,
                schema_version=schema_version,
            )
        )
        links.append(
            {
                "artifact_id": artifact_id,
                "as_of_date": source_as_of.isoformat(),
                "schema_name": schema_name,
                "schema_version": schema_version,
                "sha256": actual_hash,
                "source_key": source_key,
            }
        )
    return paths, tuple(artifacts), links


def _provenance(
    value: object,
    *,
    require_current: bool = True,
) -> tuple[dict[str, Any], SourceArtifact, dict[str, Any]]:
    ref = _object(value, "provenance")
    _strict_keys(ref, {"catalog_sha256", "generation_id", "root"}, "provenance")
    root = Path(_text(ref["root"], "provenance.root"))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise BaselineValidationError(
            "provenance root is not a real absolute directory"
        )
    generation_id = _text(ref["generation_id"], "provenance.generation_id")
    expected_hash = _text(ref["catalog_sha256"], "provenance.catalog_sha256")
    if (
        _SHA256.fullmatch(generation_id) is None
        or _SHA256.fullmatch(expected_hash) is None
    ):
        raise BaselineValidationError("provenance identity is invalid")
    if require_current:
        try:
            pointer = read_current(root)
        except (OSError, ValueError) as exc:
            raise BaselineValidationError("provenance pointer is not accepted") from exc
        if pointer.generation_id != generation_id:
            raise BaselineValidationError(
                "provenance generation is not current authority"
            )
    path = root / "generations" / generation_id / "portfolio-provenance.json"
    if path.is_symlink() or not path.is_file():
        raise BaselineValidationError("provenance catalog is missing or linked")
    actual_hash, size = hash_file(path)
    if actual_hash != expected_hash:
        raise BaselineValidationError("provenance catalog hash differs")
    catalog = _load_json(path, "provenance catalog")
    provenance_schema_version = catalog.get("catalog_schema_version")
    if provenance_schema_version not in {1, 2}:
        raise BaselineValidationError("provenance catalog schema is unsupported")
    if provenance_schema_version == 2 and not isinstance(
        catalog.get("strategy_version_catalog"), dict
    ):
        raise BaselineValidationError(
            "provenance v2 requires a strategy version catalog"
        )
    artifact = SourceArtifact(
        artifact_id="provenance/portfolio-provenance.json",
        sha256=actual_hash,
        size_bytes=size,
        as_of_date=_date(catalog.get("as_of_date"), "provenance.as_of_date"),
        schema_name="portfolio-provenance-catalog",
        schema_version=str(provenance_schema_version),
    )
    return (
        catalog,
        artifact,
        {
            "catalog_sha256": actual_hash,
            "generation_id": generation_id,
        },
    )


def _account_index(
    catalog: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    accounts = _objects(catalog.get("accounts"), "provenance.accounts")
    accepted: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    for account in accounts:
        account_id = _text(account.get("account_id"), "account.account_id")
        role = _text(account.get("role"), "account.role")
        account_aliases = account.get("aliases")
        if not isinstance(account_aliases, list) or not all(
            isinstance(item, str) and item.strip() for item in account_aliases
        ):
            raise BaselineValidationError("account aliases are invalid")
        if role == "RESERVE":
            provider = "INTERNAL"
        else:
            first = account_aliases[0].split(" | ", 1)[0].strip().upper()
            provider = "IBKR" if first == "IBRK" else first
        accepted.append(
            {
                "account_id": account_id,
                "base_currency": "USD",
                "provider": provider,
                "role": role,
            }
        )
        for alias in account_aliases:
            key = alias.strip().casefold()
            prior = aliases.get(key)
            if prior is not None and prior != account_id:
                raise BaselineValidationError("account alias is ambiguous")
            aliases[key] = account_id
    accepted.sort(key=lambda item: str(item["account_id"]))
    return accepted, aliases


def _resolve_account(aliases: Mapping[str, str], provider: str, external: str) -> str:
    legacy = "IBRK" if provider == "IBKR" else provider.title()
    external = external.strip()
    candidates = []
    if external and external.casefold() != legacy.casefold():
        candidates.append(f"{legacy} | {external}")
    candidates.append(legacy)
    for candidate in candidates:
        account_id = aliases.get(candidate.casefold())
        if account_id is not None:
            return account_id
    raise BaselineValidationError(f"account mapping failed for provider {provider}")


def _strategy_index(
    catalog: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    strategies = _objects(catalog.get("strategies"), "provenance.strategies")
    if len(strategies) != CURRENT_STRATEGY_COUNT:
        raise BaselineValidationError("provenance must contain exactly 9 strategies")
    aliases: dict[str, str] = {}
    for strategy in strategies:
        strategy_id = _text(strategy.get("strategy_id"), "strategy.strategy_id")
        names = [strategy.get("name"), *list(strategy.get("aliases", []))]
        for name in names:
            alias = _text(name, "strategy alias").casefold()
            prior = aliases.get(alias)
            if prior is not None and prior != strategy_id:
                raise BaselineValidationError("strategy alias is ambiguous")
            aliases[alias] = strategy_id
    return sorted(strategies, key=lambda item: str(item["strategy_id"])), aliases


def _resolve_strategy(aliases: Mapping[str, str], observed: str) -> str:
    strategy_id = aliases.get(observed.strip().casefold())
    if strategy_id is None:
        raise BaselineValidationError("current strategy mapping failed")
    return strategy_id


def _strategy_for_date(
    assignments: Sequence[Mapping[str, Any]], account_id: str, event_date: date
) -> tuple[MappingStatus, str | None]:
    matches: list[Mapping[str, Any]] = []
    for assignment in assignments:
        if assignment.get("account_id") != account_id:
            continue
        start_value = assignment.get("effective_start")
        end_value = assignment.get("effective_end")
        if assignment.get("mapping_status") == "MAPPED" and start_value is None:
            raise BaselineValidationError(
                "mapped assignment requires an explicit effective_start"
            )
        if start_value is not None and event_date < _date(
            start_value, "assignment.start"
        ):
            continue
        if end_value is not None and event_date > _date(end_value, "assignment.end"):
            continue
        matches.append(assignment)
    mapped = [item for item in matches if item.get("mapping_status") == "MAPPED"]
    if len(mapped) == 1:
        return MappingStatus.MAPPED, _text(mapped[0].get("strategy_id"), "strategy_id")
    if mapped:
        raise BaselineValidationError(
            "multiple mapped strategies overlap for an account date"
        )
    return MappingStatus.UNMAPPED, None


def _normalize_nav(
    paths: Mapping[str, Path], aliases: Mapping[str, str]
) -> tuple[list[dict[str, Any]], dict[str, set[date]], dict[str, str]]:
    records: dict[tuple[str, date], dict[str, Any]] = {}
    providers: dict[str, str] = {}

    def add(
        provider: str, external: str, day: str, value: str, source_key: str
    ) -> None:
        account_id = _resolve_account(aliases, provider, external)
        observed_date = _date(day, f"{source_key}.date")
        amount = _decimal(value, f"{source_key}.nav")
        assert amount is not None
        payload = NavObservation(
            record_id="pending",
            account_id=account_id,
            as_of_date=observed_date,
            nav=ObservedAmount.available(amount, "USD"),
        ).to_dict()
        payload["record_id"] = _record_id(
            "nav",
            {
                "account_id": account_id,
                "date": observed_date.isoformat(),
                "value": decimal_text(amount),
            },
        )
        payload["provider"] = provider
        payload["source_key"] = source_key
        key = (account_id, observed_date)
        prior = records.get(key)
        if prior is not None and prior["nav"] != payload["nav"]:
            raise BaselineValidationError("conflicting NAV observation")
        records[key] = payload
        providers[account_id] = provider

    for row in _read_csv(paths["schwab-nav"], "schwab-nav"):
        add("SCHWAB", row["account"], row["date"], row["nav"], "schwab-nav")
    for row in _read_csv(paths["ibkr-ledger"], "ibkr-ledger"):
        add("IBKR", row["account"], row["date"], row["ending_value"], "ibkr-ledger")
    for row in _read_csv(paths["alpaca-nav"], "alpaca-nav"):
        add("ALPACA", "Alpaca", row["date"], row["nav"], "alpaca-nav")

    provider_dates: dict[str, set[date]] = {}
    for record in records.values():
        provider_dates.setdefault(str(record["provider"]), set()).add(
            _date(record["as_of_date"], "nav.as_of_date")
        )
    return (
        sorted(
            records.values(),
            key=lambda item: (str(item["account_id"]), str(item["as_of_date"])),
        ),
        provider_dates,
        providers,
    )


def _reconcile_nav(
    path: Path,
    aliases: Mapping[str, str],
    normalized: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    raw = {
        (
            str(item["account_id"]),
            _date(item["as_of_date"], "nav.as_of_date"),
        ): _text(_object(item["nav"], "nav.amount").get("value"), "nav.value")
        for item in normalized
    }
    derived: dict[tuple[str, date], str] = {}
    for row in _read_csv(path, "attribution-diagnostic"):
        provider = (
            "IBKR"
            if row["institution"].strip().upper() == "IBRK"
            else row["institution"].strip().upper()
        )
        account_id = _resolve_account(aliases, provider, row["account"])
        day = _date(row["date"], "attribution.date")
        amount = _decimal(row["nav"], "attribution.nav")
        assert amount is not None
        derived[(account_id, day)] = decimal_text(amount)
    if raw != derived:
        raise BaselineValidationError(
            "raw NAV sources do not reconcile to tracker derivative"
        )
    return {
        "check_id": "raw-nav-to-attribution-diagnostic",
        "observed_count": len(raw),
        "status": "PASS",
    }


def _calendar_statuses(
    accounts: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    provider_dates: Mapping[str, set[date]],
) -> list[dict[str, Any]]:
    by_account: dict[str, set[date]] = {}
    for record in nav:
        by_account.setdefault(str(record["account_id"]), set()).add(
            _date(record["as_of_date"], "nav.as_of_date")
        )
    statuses: list[dict[str, Any]] = []
    for account in accounts:
        if account["role"] == "RESERVE":
            continue
        account_id = str(account["account_id"])
        provider = str(account["provider"])
        dates = provider_dates.get(provider, set())
        observed = by_account.get(account_id, set())
        if not observed:
            statuses.append(
                {
                    "account_id": account_id,
                    "date": None,
                    "provider": provider,
                    "status": "NO_ACCOUNT_NAV_OBSERVATIONS",
                }
            )
            continue
        current = min(observed)
        end = max(observed)
        while current <= end:
            if current in observed:
                status = "OBSERVED"
            elif current.weekday() >= 5:
                status = "NON_BUSINESS_WEEKEND"
            elif current in dates:
                status = "MISSING_ON_PROVIDER_OBSERVED_DATE"
            else:
                status = "UNRESOLVED_NO_CALENDAR_EVIDENCE"
            statuses.append(
                {
                    "account_id": account_id,
                    "date": current.isoformat(),
                    "provider": provider,
                    "status": status,
                }
            )
            current += timedelta(days=1)
    return sorted(
        statuses,
        key=lambda item: (str(item["account_id"]), str(item["date"] or "")),
    )


def _normalize_flows(
    paths: Mapping[str, Path],
    aliases: Mapping[str, str],
    assignments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = []

    def add(
        provider: str,
        external: str,
        day: str,
        flow_type: str,
        value: str,
        source_key: str,
        evidence_class: str,
        source_ordinal: int,
    ) -> None:
        account_id = _resolve_account(aliases, provider, external)
        event_date = _date(day, f"{source_key}.date")
        amount = _decimal(value, f"{source_key}.amount")
        assert amount is not None
        mapping_status, strategy_id = _strategy_for_date(
            assignments, account_id, event_date
        )
        identity = {
            "account_id": account_id,
            "amount": decimal_text(amount),
            "date": event_date.isoformat(),
            "flow_type": flow_type,
            "source_ordinal": source_ordinal,
            "source_key": source_key,
        }
        record = CashFlow(
            record_id=_record_id("flow", identity),
            account_id=account_id,
            event_date=event_date,
            flow_type=flow_type,
            amount=ObservedAmount.available(amount, "USD"),
            mapping_status=mapping_status,
            strategy_id=strategy_id,
        ).to_dict()
        record.update(
            {
                "evidence_class": evidence_class,
                "provider": provider,
                "source_key": source_key,
            }
        )
        flows.append(record)

    for source_ordinal, row in enumerate(
        _read_csv(paths["schwab-flows"], "schwab-flows"), start=1
    ):
        add(
            "SCHWAB",
            row["account"],
            row["date"],
            row["bucket"].upper(),
            row["amount"],
            "schwab-flows",
            "OBSERVED",
            source_ordinal,
        )
    for source_ordinal, row in enumerate(
        _read_csv(paths["alpaca-flows"], "alpaca-flows"), start=1
    ):
        evidence = "INFERRED" if "infer" in row["note"].casefold() else "OBSERVED"
        add(
            "ALPACA",
            row["account"],
            row["date"],
            row["bucket"].upper(),
            row["amount"],
            "alpaca-flows",
            evidence,
            source_ordinal,
        )
    for source_ordinal, row in enumerate(
        _read_csv(paths["ibkr-ledger"], "ibkr-ledger"), start=1
    ):
        for field, flow_type in (
            ("external_flow", "EXTERNAL_FLOW"),
            ("transfer_flow", "TRANSFER_FLOW"),
        ):
            amount = _decimal(row[field], f"ibkr-ledger.{field}")
            assert amount is not None
            if amount != 0:
                add(
                    "IBKR",
                    row["account"],
                    row["date"],
                    flow_type,
                    row[field],
                    "ibkr-ledger",
                    "OBSERVED",
                    source_ordinal,
                )
    identifiers = [str(item["record_id"]) for item in flows]
    if len(identifiers) != len(set(identifiers)):
        raise BaselineValidationError("flow identifiers are not unique")
    return sorted(
        flows, key=lambda item: (str(item["event_date"]), str(item["record_id"]))
    )


def _normalize_transactions(
    path: Path,
    aliases: Mapping[str, str],
    assignments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    account_id = _resolve_account(aliases, "ALPACA", "Alpaca")
    transactions: list[dict[str, Any]] = []
    securities: dict[str, dict[str, Any]] = {}
    for row in _read_csv(path, "alpaca-events"):
        if row["type"] not in {"buy_fill", "sell_fill"}:
            continue
        event_date = _date(row["date"], "alpaca-events.date")
        quantity = _decimal(row["qty"], "alpaca-events.qty")
        price = _decimal(row["price"], "alpaca-events.price")
        assert quantity is not None and price is not None
        symbol = _text(row["symbol"], "alpaca-events.symbol")
        security_id = f"security-{hashlib.sha256(symbol.encode()).hexdigest()[:16]}"
        securities[security_id] = {
            "currency": "USD",
            "security_id": security_id,
            "symbol": symbol,
        }
        signed_amount = (
            quantity
            * price
            * (Decimal("-1") if row["type"] == "buy_fill" else Decimal("1"))
        )
        mapping_status, strategy_id = _strategy_for_date(
            assignments, account_id, event_date
        )
        identity = {
            "account_id": account_id,
            "date": event_date.isoformat(),
            "kind": row["type"].upper(),
            "quantity": decimal_text(quantity),
            "security_id": security_id,
        }
        record = Transaction(
            record_id=_record_id("transaction", identity),
            account_id=account_id,
            event_date=event_date,
            kind=row["type"].upper(),
            amount=ObservedAmount.available(signed_amount, "USD"),
            mapping_status=mapping_status,
            strategy_id=strategy_id,
            security_id=security_id,
        ).to_dict()
        record.update(
            {
                "price": decimal_text(price),
                "provider": "ALPACA",
                "quantity": decimal_text(quantity),
                "source_key": "alpaca-events",
            }
        )
        transactions.append(record)
    identifiers = [str(item["record_id"]) for item in transactions]
    if len(identifiers) != len(set(identifiers)):
        raise BaselineValidationError("transaction identifiers are not unique")
    return (
        sorted(
            transactions,
            key=lambda item: (str(item["event_date"]), str(item["record_id"])),
        ),
        sorted(securities.values(), key=lambda item: str(item["security_id"])),
    )


def _schwab_facts(
    catalog: Mapping[str, Any],
    accounts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    baseline_nav: Sequence[Mapping[str, Any]],
    baseline_as_of: date,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    list[str],
]:
    accepted_accounts = {
        str(item["account_id"]) for item in accounts if item.get("provider") == "SCHWAB"
    }
    positions: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    securities: dict[str, dict[str, Any]] = {}
    for item in _objects(catalog.get("positions"), "Schwab positions"):
        account_id = _text(item.get("account_id"), "Schwab position account_id")
        if account_id not in accepted_accounts:
            raise BaselineValidationError("Schwab position account is outside scope")
        as_of = _date(item.get("as_of_date"), "Schwab position as_of_date")
        if as_of > baseline_as_of:
            raise BaselineValidationError("Schwab position is newer than baseline")
        if item.get("evidence_class") != "WCH_CURRENT":
            raise BaselineValidationError("Schwab position evidence class differs")
        symbol = _text(item.get("symbol"), "Schwab position symbol")
        security_id = _text(item.get("security_id"), "Schwab position security_id")
        securities[security_id] = {
            "currency": "USD",
            "security_id": security_id,
            "symbol": symbol,
        }
        record = dict(item)
        mapping_status, strategy_id = _strategy_for_date(assignments, account_id, as_of)
        record.update(
            {
                "mapping_status": mapping_status.value,
                "provider": "SCHWAB",
                "source_key": "schwab-generation",
                "strategy_id": strategy_id,
            }
        )
        positions.append(record)
    for item in _objects(catalog.get("transactions"), "Schwab transactions"):
        account_id = _text(item.get("account_id"), "Schwab transaction account_id")
        if account_id not in accepted_accounts:
            raise BaselineValidationError("Schwab transaction account is outside scope")
        event_date = _date(item.get("event_date"), "Schwab transaction event_date")
        if event_date > baseline_as_of:
            raise BaselineValidationError("Schwab transaction is newer than baseline")
        security_id_value = item.get("security_id")
        symbol_value = item.get("symbol")
        if security_id_value is not None:
            security_id = _text(security_id_value, "Schwab transaction security_id")
            symbol = _text(symbol_value, "Schwab transaction symbol")
            securities[security_id] = {
                "currency": "USD",
                "security_id": security_id,
                "symbol": symbol,
            }
        record = dict(item)
        record.update({"provider": "SCHWAB", "source_key": "schwab-generation"})
        transactions.append(record)
    position_ids = [str(item.get("record_id")) for item in positions]
    transaction_ids = [str(item.get("record_id")) for item in transactions]
    if len(position_ids) != len(set(position_ids)) or len(transaction_ids) != len(
        set(transaction_ids)
    ):
        raise BaselineValidationError(
            "Schwab provider record identifiers are not unique"
        )

    baseline_values = {
        (str(item["account_id"]), str(item["as_of_date"])): _text(
            _object(item.get("nav"), "baseline NAV").get("value"),
            "baseline NAV value",
        )
        for item in baseline_nav
        if str(item.get("account_id")) in accepted_accounts
    }
    provider_values = {
        (
            _text(item.get("account_id"), "Schwab NAV account_id"),
            _text(item.get("as_of_date"), "Schwab NAV as_of_date"),
        ): _text(
            _object(item.get("nav"), "Schwab NAV").get("value"),
            "Schwab NAV value",
        )
        for item in _objects(catalog.get("nav_observations"), "Schwab NAV")
    }
    if {key[0] for key in provider_values} != accepted_accounts:
        raise BaselineValidationError("Schwab NAV account scope differs")
    shared = set(baseline_values) & set(provider_values)
    reconciliation = {
        "baseline_only_count": len(set(baseline_values) - set(provider_values)),
        "check_id": "schwab-provider-nav-to-portfolio-nav",
        "different_shared_count": sum(
            baseline_values[key] != provider_values[key] for key in shared
        ),
        "provider_only_count": len(set(provider_values) - set(baseline_values)),
        "shared_count": len(shared),
        "status": "PASS_WITH_DECLARED_VARIANCE",
    }
    mapped_position_count = sum(
        item.get("mapping_status") == MappingStatus.MAPPED.value for item in positions
    )
    position_market_value = sum(
        (
            _decimal(
                _object(item.get("market_value"), "position.market_value").get("value"),
                "position.market_value.value",
            )
            or Decimal("0")
            for item in positions
        ),
        Decimal("0"),
    )
    position_reconciliation = {
        "check_id": "current-position-strategy-attribution",
        "mapped_count": mapped_position_count,
        "market_value_usd": decimal_text(position_market_value),
        "position_count": len(positions),
        "status": (
            "PASS"
            if mapped_position_count == len(positions)
            else "PASS_WITH_EXPLICIT_UNMAPPED"
        ),
        "unmapped_count": len(positions) - mapped_position_count,
    }
    coverage = _object(catalog.get("coverage"), "Schwab coverage")
    known_gaps = [
        _text(item, "Schwab coverage known_gap")
        for item in coverage.get("known_gaps", [])
    ]
    return (
        sorted(
            positions,
            key=lambda item: (str(item["account_id"]), str(item["security_id"])),
        ),
        sorted(
            transactions,
            key=lambda item: (str(item["event_date"]), str(item["record_id"])),
        ),
        sorted(securities.values(), key=lambda item: str(item["security_id"])),
        reconciliation,
        position_reconciliation,
        known_gaps,
    )


def _provider_ledger_facts(
    catalog: Mapping[str, Any],
    accounts: Sequence[Mapping[str, Any]],
    legacy_nav: Sequence[Mapping[str, Any]],
    legacy_flows: Sequence[Mapping[str, Any]],
    legacy_transactions: Sequence[Mapping[str, Any]],
    legacy_securities: Sequence[Mapping[str, Any]],
    baseline_as_of: date,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    list[str],
]:
    """Select admitted account facts while retaining legacy-only observations."""

    accepted_accounts = {str(item["account_id"]) for item in accounts}

    def admitted(name: str) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        for item in _objects(catalog.get(name), f"provider-ledger.{name}"):
            account_id = _text(item.get("account_id"), f"{name}.account_id")
            if (
                account_id not in accepted_accounts
                or item.get("mapping_status") != "MAPPED"
            ):
                continue
            day_value = item.get("as_of_date") or item.get("event_date")
            if _date(day_value, f"{name}.date") > baseline_as_of:
                raise BaselineValidationError(
                    "provider-ledger fact is newer than baseline"
                )
            record = dict(item)
            record.update(
                {
                    "provider": _text(
                        item.get("provider_id"), f"{name}.provider_id"
                    ).upper(),
                    "source_key": "provider-ledger",
                }
            )
            accepted.append(record)
        return accepted

    provider_nav = admitted("nav_observations")
    provider_flows = admitted("flows")
    provider_transactions = admitted("transactions")
    provider_positions = admitted("positions")

    nav_by_key = {
        (str(item["account_id"]), str(item["as_of_date"])): dict(item)
        for item in legacy_nav
    }
    conflicts = 0
    for item in provider_nav:
        key = (str(item["account_id"]), str(item["as_of_date"]))
        prior = nav_by_key.get(key)
        if prior is not None and prior.get("nav") != item.get("nav"):
            conflicts += 1
        nav_by_key[key] = item

    def replace_accounts(
        legacy: Sequence[Mapping[str, Any]], provider: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        provider_accounts = {str(item["account_id"]) for item in provider}
        combined = [
            dict(item)
            for item in legacy
            if str(item["account_id"]) not in provider_accounts
        ]
        combined.extend(dict(item) for item in provider)
        identifiers = [str(item["record_id"]) for item in combined]
        if len(identifiers) != len(set(identifiers)):
            raise BaselineValidationError("provider-ledger fact identifiers collide")
        return combined

    flows = replace_accounts(legacy_flows, provider_flows)
    transactions = replace_accounts(legacy_transactions, provider_transactions)

    latest_position: dict[tuple[str, str], dict[str, Any]] = {}
    for item in provider_positions:
        security_id = _text(item.get("security_id"), "position.security_id")
        key = (str(item["account_id"]), security_id)
        prior = latest_position.get(key)
        if prior is None or str(prior["as_of_date"]) < str(item["as_of_date"]):
            latest_position[key] = item

    used_securities = {
        str(item["security_id"])
        for item in (*transactions, *latest_position.values())
        if item.get("security_id") is not None
    }
    securities: dict[str, dict[str, Any]] = {
        str(item["security_id"]): dict(item) for item in legacy_securities
    }
    for item in _objects(catalog.get("securities"), "provider-ledger.securities"):
        security_id = str(item.get("security_id") or "")
        if security_id in used_securities and security_id not in securities:
            securities[security_id] = dict(item)

    known_gaps = sorted(
        {
            _text(gap, "provider coverage known_gap")
            for item in _objects(
                catalog.get("provider_coverage"), "provider-ledger.provider_coverage"
            )
            for gap in _object(item.get("coverage"), "provider coverage").get(
                "known_gaps", []
            )
        }
    )
    quality_findings = _objects(
        catalog.get("quality_findings"), "provider-ledger.quality_findings"
    )
    reconciliation = {
        "admitted_flow_count": len(provider_flows),
        "admitted_nav_count": len(provider_nav),
        "admitted_position_count": len(latest_position),
        "admitted_transaction_count": len(provider_transactions),
        "check_id": "provider-ledger-to-portfolio-baseline",
        "nav_conflict_count": conflicts,
        "quality_finding_count": len(quality_findings),
        "status": "PASS_WITH_EXPLICIT_RECONCILIATION",
    }
    return (
        sorted(
            nav_by_key.values(),
            key=lambda item: (str(item["account_id"]), str(item["as_of_date"])),
        ),
        sorted(
            flows, key=lambda item: (str(item["event_date"]), str(item["record_id"]))
        ),
        sorted(
            transactions,
            key=lambda item: (str(item["event_date"]), str(item["record_id"])),
        ),
        sorted(
            latest_position.values(),
            key=lambda item: (str(item["account_id"]), str(item["security_id"])),
        ),
        sorted(securities.values(), key=lambda item: str(item["security_id"])),
        quality_findings,
        reconciliation,
        known_gaps,
    )


def _allocations_and_coverage(
    paths: Mapping[str, Path],
    strategy_aliases: Mapping[str, str],
    source_effective_date: date,
    publication_date: date,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    Decimal,
    Decimal,
]:
    allocations: list[dict[str, Any]] = []
    by_strategy: dict[str, tuple[Decimal, Decimal]] = {}
    for row in _read_csv(paths["component-decision-table"], "component-decision-table"):
        strategy_id = _resolve_strategy(strategy_aliases, row["component"])
        value = _decimal(row["current_value"], "allocation.current_value")
        weight = _decimal(row["systematic_weight"], "allocation.systematic_weight")
        assert value is not None and weight is not None
        by_strategy[strategy_id] = (value, weight)
        payload = {
            "as_of_date": source_effective_date.isoformat(),
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": decimal_text(value),
            },
            "pool_id": "systematic-capital",
            "publication_date": publication_date.isoformat(),
            "scope": "SYSTEMATIC",
            "source_key": "component-decision-table",
            "source_effective_date": source_effective_date.isoformat(),
            "strategy_id": strategy_id,
            "systematic_weight": decimal_text(weight),
            "valuation_date": source_effective_date.isoformat(),
        }
        allocations.append(
            {"allocation_id": _record_id("allocation", payload), **payload}
        )
    if len(by_strategy) != CURRENT_STRATEGY_COUNT:
        raise BaselineValidationError(
            "allocation snapshot must map exactly 9 strategies"
        )

    coverage: list[dict[str, Any]] = []
    nav_covered = Decimal("0")
    for row in _read_csv(paths["data-coverage-matrix"], "data-coverage-matrix"):
        strategy_id = _resolve_strategy(strategy_aliases, row["component"])
        value = _decimal(row["current_value"], "coverage.current_value")
        assert value is not None
        if strategy_id not in by_strategy or by_strategy[strategy_id][0] != value:
            raise BaselineValidationError(
                "coverage and allocation current values differ"
            )
        daily = row["account_specific_daily_series"].strip().casefold()
        if daily not in {"yes", "no"}:
            raise BaselineValidationError("daily-series coverage must be yes or no")
        if daily == "yes":
            nav_covered += value
        coverage.append(
            {
                "current_value_usd": decimal_text(value),
                "daily_end": row["daily_end"].strip() or None,
                "daily_start": row["daily_start"].strip() or None,
                "known_gap": row["phase2_limitation"].strip() or None,
                "return_observations": row["return_observations"].strip() or None,
                "status": "AVAILABLE" if daily == "yes" else "UNAVAILABLE",
                "strategy_id": strategy_id,
            }
        )

    costs: list[dict[str, Any]] = []
    for row in _read_csv(paths["cost-economics"], "cost-economics"):
        strategy_id = _resolve_strategy(strategy_aliases, row["component"])
        cost = _decimal(
            row["annual_external_cost_dollars"], "cost.annual", allow_blank=True
        )
        costs.append(
            {
                "annual_external_cost_usd": decimal_text(cost)
                if cost is not None
                else None,
                "cost_status": _text(row["cost_status"], "cost.status"),
                "strategy_id": strategy_id,
            }
        )
    systematic_total = sum((item[0] for item in by_strategy.values()), Decimal("0"))
    return (
        sorted(allocations, key=lambda item: str(item["strategy_id"])),
        sorted(coverage, key=lambda item: str(item["strategy_id"])),
        sorted(costs, key=lambda item: str(item["strategy_id"])),
        systematic_total,
        nav_covered,
    )


def _normalize_strategy_evidence(
    paths: Mapping[str, Path], strategy_aliases: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    layer_metrics: list[dict[str, Any]] = []
    for row in _read_csv(paths["evidence-layer-metrics"], "evidence-layer-metrics"):
        strategy_id = _resolve_strategy(strategy_aliases, row["component"])
        layer_metrics.append(
            {
                "end_date": row["end_date"].strip() or None,
                "layer": _text(row["layer"], "evidence.layer"),
                "metrics": {
                    key: value or None
                    for key, value in row.items()
                    if key
                    not in {
                        "component",
                        "layer",
                        "provenance",
                        "start_date",
                        "end_date",
                        "status",
                    }
                },
                "provenance": _text(row["provenance"], "evidence.provenance"),
                "start_date": row["start_date"].strip() or None,
                "status": row["status"].strip() or "UNAVAILABLE_UNLABELED",
                "strategy_id": strategy_id,
            }
        )
    live_metrics: list[dict[str, Any]] = []
    for row in _read_csv(paths["strategy-live-metrics"], "strategy-live-metrics"):
        live_strategy_id: str | None
        try:
            live_strategy_id = _resolve_strategy(strategy_aliases, row["strategy"])
        except BaselineValidationError:
            # Aggregate/benchmark rows are preserved but cannot become Strategy identities.
            live_strategy_id = None
        live_metrics.append(
            {
                "evidence_layer": "ACCOUNT_LIVE"
                if live_strategy_id is not None
                else "AGGREGATE_OR_BENCHMARK",
                "metrics": {
                    key: value or None
                    for key, value in row.items()
                    if key != "strategy"
                },
                "observed_label": row["strategy"],
                "strategy_id": live_strategy_id,
            }
        )
    gaps: list[dict[str, Any]] = []
    for row in _read_csv(paths["implementation-gap"], "implementation-gap"):
        strategy_id = _resolve_strategy(strategy_aliases, row["component"])
        gaps.append(
            {
                "account_layer": row["account_layer"] or None,
                "metrics": {
                    key: value or None
                    for key, value in row.items()
                    if key
                    not in {
                        "component",
                        "provider_layer",
                        "account_layer",
                        "status",
                        "note",
                    }
                },
                "note": row["note"] or None,
                "provider_layer": row["provider_layer"] or None,
                "status": _text(row["status"], "implementation_gap.status"),
                "strategy_id": strategy_id,
            }
        )
    return (
        sorted(
            layer_metrics,
            key=lambda item: (
                str(item["strategy_id"]),
                str(item["layer"]),
                str(item["start_date"]),
            ),
        ),
        sorted(
            live_metrics,
            key=lambda item: (
                str(item["strategy_id"] or "~"),
                str(item["observed_label"]),
            ),
        ),
        sorted(gaps, key=lambda item: str(item["strategy_id"])),
    )


def _replay(
    spec_value: object,
    paths: Mapping[str, Path],
    systematic_total: Decimal,
    nav_covered: Decimal,
) -> tuple[dict[str, Any], Decimal]:
    spec = _object(spec_value, "replay")
    _strict_keys(
        spec,
        {
            "correlation_one_stress",
            "full_systematic_hwm_status",
            "nav_coverage",
            "portfolio_action",
            "reserve_total_usd",
            "systematic_total_usd",
            "total_liquid_usd",
        },
        "replay",
    )
    summary = _load_json(
        paths["current-portfolio-summary"], "current-portfolio-summary"
    )
    evidence = _load_json(paths["phase2-evidence-summary"], "phase2-evidence-summary")
    control = _load_json(paths["combined-control-status"], "combined-control-status")
    hwm = _load_json(paths["hwm-status"], "hwm-status")

    reserve = _decimal(str(summary.get("reserve_total")), "summary.reserve_total")
    reported_systematic = _decimal(
        str(summary.get("systematic_total")), "summary.systematic_total"
    )
    stress = _decimal(
        str(evidence.get("current_correlation_one_underwriting_loss")),
        "evidence.stress",
    )
    assert (
        reserve is not None and reported_systematic is not None and stress is not None
    )
    stress = abs(stress)
    if systematic_total != reported_systematic:
        raise BaselineValidationError(
            "allocation total does not reconcile to audit systematic total"
        )
    nav_ratio = nav_covered / systematic_total
    total = systematic_total + reserve

    actuals: dict[str, Decimal | str] = {
        "correlation_one_stress": stress,
        "full_systematic_hwm_status": _text(
            hwm.get("full_systematic_hwm_status"), "hwm.status"
        ),
        "nav_coverage": nav_ratio,
        "portfolio_action": _text(control.get("portfolio_action"), "control.action"),
        "reserve_total_usd": reserve,
        "systematic_total_usd": systematic_total,
        "total_liquid_usd": total,
    }
    rendered: dict[str, Any] = {}
    for key, actual in actuals.items():
        expected_value = spec[key]
        if isinstance(actual, Decimal):
            contract = _object(expected_value, f"replay.{key}")
            _strict_keys(contract, {"tolerance", "value"}, f"replay.{key}")
            expected = _decimal(contract["value"], f"replay.{key}.value")
            tolerance = _decimal(contract["tolerance"], f"replay.{key}.tolerance")
            assert expected is not None and tolerance is not None
            if tolerance < 0 or abs(actual - expected) > tolerance:
                raise BaselineValidationError(f"frozen replay differs for {key}")
            rendered[key] = decimal_text(actual)
        else:
            if actual != _text(expected_value, f"replay.{key}"):
                raise BaselineValidationError(f"frozen replay differs for {key}")
            rendered[key] = actual
    rendered["status"] = "PASS"
    return rendered, reserve


def _fact_coverage(
    accounts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    flows: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    schwab_known_gaps: Sequence[str],
    missing_strategy_nav_count: int,
    as_of_date: date,
    *,
    nav: Sequence[Mapping[str, Any]] = (),
    costs: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    investment_count = sum(1 for item in accounts if item["role"] == "INVESTMENT")
    unmapped = [
        item for item in assignments if item.get("mapping_status") == "UNMAPPED"
    ]
    attribution_unmapped = [
        item
        for item in unmapped
        if item.get("pool_id") != "reserve-capital"
        and item.get("strategy_mapping_disposition") != "RESERVE_NOT_APPLICABLE"
    ]
    current_unmapped: list[Mapping[str, Any]] = []
    historical_unmapped: list[Mapping[str, Any]] = []
    for item in attribution_unmapped:
        start = item.get("effective_start")
        end = item.get("effective_end")
        active = (
            start is None or _date(start, "assignment.effective_start") <= as_of_date
        ) and (end is None or _date(end, "assignment.effective_end") >= as_of_date)
        (current_unmapped if active else historical_unmapped).append(item)
    claims: list[dict[str, Any]] = [
        {
            "fact_type": "accounts",
            "known_gaps": [],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        {
            "fact_type": "current_strategies",
            "known_gaps": [],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        {
            "fact_type": "current_allocations",
            "known_gaps": [],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        {
            "fact_type": "assignments",
            "known_gaps": [],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        {
            "fact_type": "current_strategy_attribution",
            "known_gaps": (
                [
                    f"{len(current_unmapped)} current intervals are mapped to the systematic pool but not a strategy"
                ]
                if current_unmapped
                else []
            ),
            "status": "PARTIAL"
            if current_unmapped
            else "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "disposition": {
                "authority": "effective-dated account assignment registry",
                "code": (
                    "POOL_ONLY_UNALLOCATED_RETAINED" if current_unmapped else "COMPLETE"
                ),
                "effect": (
                    "Current pool-only intervals remain unassigned; no holding-based strategy inference is permitted."
                    if current_unmapped
                    else "Every current systematic interval has an accepted strategy assignment."
                ),
            },
            "record_count": len(current_unmapped),
        },
        {
            "fact_type": "historical_strategy_attribution",
            "known_gaps": (
                [f"{len(historical_unmapped)} historical intervals remain pool-only"]
                if historical_unmapped
                else []
            ),
            "status": "PARTIAL"
            if historical_unmapped
            else "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "disposition": {
                "authority": "effective-dated account assignment registry",
                "code": (
                    "POOL_ONLY_UNALLOCATED_RETAINED"
                    if historical_unmapped
                    else "COMPLETE"
                ),
                "effect": (
                    "Historical pool-only intervals remain visible and are excluded from strategy attribution."
                    if historical_unmapped
                    else "Every historical systematic interval has an accepted strategy assignment."
                ),
            },
            "record_count": len(historical_unmapped),
        },
        {
            "fact_type": "nav",
            "known_gaps": (
                [
                    f"{missing_strategy_nav_count} current strategies lack account-specific daily series"
                ]
                if missing_strategy_nav_count
                else []
            ),
            "status": (
                "PARTIAL"
                if missing_strategy_nav_count
                else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
            ),
            "disposition": {
                "authority": "accepted account NAV plus governed HWM qualification",
                "code": "RETAINED_UNQUALIFIED_HWM_NAV",
                "effect": "Automated HWM remains unavailable for every included sleeve without qualified fresh daily NAV evidence.",
            },
            "record_count": len(nav),
        },
        {
            "fact_type": "flows",
            "known_gaps": [
                (
                    f"{len(flows)} accepted flow records across "
                    f"{', '.join(sorted({str(item['provider']) for item in flows if item.get('provider')})) or 'no providers'}; "
                    "providers without an admitted flow path remain explicit in the account fact matrix"
                )
            ],
            "status": "PARTIAL",
            "disposition": {
                "authority": "accepted provider-ledger and legacy flow evidence",
                "code": "RETAINED_PROVIDER_INTERVAL_BOUNDARY",
                "effect": "MWR remains unavailable outside accounts and intervals with complete admitted external-flow evidence.",
            },
            "record_count": len(flows),
        },
        {
            "fact_type": "transactions",
            "known_gaps": [
                (
                    f"{len(transactions)} accepted transaction records across "
                    f"{', '.join(sorted({str(item['provider']) for item in transactions if item.get('provider')})) or 'no providers'}; "
                    "providers without an admitted transaction path remain explicit in the account fact matrix"
                )
            ],
            "status": "PARTIAL",
            "disposition": {
                "authority": "accepted provider-ledger and legacy transaction evidence",
                "code": "RETAINED_PROVIDER_INTERVAL_BOUNDARY",
                "effect": "Transaction analysis is limited to the named account/provider intervals; absence is never converted to zero activity.",
            },
            "record_count": len(transactions),
        },
        {
            "fact_type": "positions",
            "known_gaps": (
                list(schwab_known_gaps)
                if positions
                else ["no accepted position source exists for this baseline"]
            ),
            "status": "PARTIAL" if positions else "FAILED",
            "disposition": {
                "authority": "accepted current provider position snapshots",
                "code": (
                    "RETAINED_PROVIDER_SNAPSHOT_BOUNDARY"
                    if positions
                    else "RETAINED_NO_ACCEPTED_POSITION_SOURCE"
                ),
                "effect": "Only accepted current position rows are reported; accounts without reported rows remain unavailable.",
            },
            "record_count": len(positions),
        },
        {
            "fact_type": "fx",
            "known_gaps": [],
            "status": "FAILED",
            "disposition": {},
        },
        {
            "fact_type": "costs",
            "known_gaps": [
                "several current strategy costs remain unknown or conditional"
            ],
            "status": "PARTIAL",
            "disposition": {
                "authority": "strategy cost-economics register",
                "code": "RETAINED_UNKNOWN_OR_CONDITIONAL_COSTS",
                "effect": "Gross-to-net and after-tax conclusions remain unavailable where a sleeve cost is unknown or conditional.",
            },
            "record_count": len(costs),
        },
    ]
    non_usd_records = [
        item
        for item in (*transactions, *flows)
        if isinstance(item.get("amount"), Mapping)
        and item["amount"].get("currency") not in {None, "USD"}
    ]
    unavailable_non_usd = [
        item
        for item in non_usd_records
        if isinstance(item.get("amount"), Mapping)
        and item["amount"].get("status") != "AVAILABLE"
    ]
    non_usd_currencies = sorted(
        {
            str(item["amount"]["currency"])
            for item in non_usd_records
            if isinstance(item.get("amount"), Mapping)
        }
    )
    fx_claim = next(item for item in claims if item["fact_type"] == "fx")
    fx_claim["known_gaps"] = [
        "no accepted independent FX observation source exists; "
        f"{len(non_usd_records)} non-USD transaction/flow records "
        f"({', '.join(non_usd_currencies) or 'no currencies'}) are FX-affected, "
        f"including {len(unavailable_non_usd)} with unavailable accepted USD amounts; "
        "account NAV, current positions, allocations, and HWM current values remain provider-reported USD authority"
    ]
    fx_claim["disposition"] = {
        "authority": "provider-reported USD account facts; no independent FX series",
        "code": "RETAINED_NO_ACCEPTED_FX_SOURCE",
        "effect": "Independent non-USD transaction/flow recomputation is unavailable; provider-reported USD current values are not retranslated.",
        "fx_affected_record_count": len(non_usd_records),
        "unavailable_usd_amount_count": len(unavailable_non_usd),
        "currencies": non_usd_currencies,
    }
    fx_claim["record_count"] = 0
    exceptions: list[dict[str, Any]] = [
        {
            "exception_id": (
                "positions-partial" if positions else "positions-unavailable"
            ),
            "fact_type": "positions",
            "record_count": len(positions),
            "severity": (
                "DISCLOSED_GAP" if positions else "BLOCKING_FOR_POSITION_ANALYSIS"
            ),
            "status": "RETAINED_WITH_EXPLICIT_DISPOSITION",
            "authority": "accepted current provider position snapshots",
            "disposition": "REPORT_ACCEPTED_ROWS_ONLY_NO_INFERENCE",
        },
        {
            "exception_id": "full-systematic-hwm-unavailable",
            "fact_type": "nav",
            "severity": "BLOCKING_FOR_AUTOMATED_HWM",
            "status": "UNAVAILABLE",
            "authority": "governed HWM policy and qualified daily NAV evidence",
            "disposition": "FAIL_CLOSED_UNTIL_EVERY_INCLUDED_SLEEVE_IS_QUALIFIED",
        },
        {
            "exception_id": "current-strategy-attribution-partial",
            "fact_type": "current_strategy_attribution",
            "record_count": len(current_unmapped),
            "severity": "BLOCKING_FOR_FULL_ATTRIBUTION",
            "status": (
                "RETAINED_WITH_EXPLICIT_DISPOSITION" if current_unmapped else "PASS"
            ),
            "authority": "effective-dated account assignment registry",
            "disposition": "POOL_ONLY_UNALLOCATED",
        },
        {
            "exception_id": "historical-strategy-attribution-unresolved",
            "fact_type": "historical_strategy_attribution",
            "record_count": len(historical_unmapped),
            "severity": "DISCLOSED_HISTORICAL_GAP",
            "status": (
                "RETAINED_WITH_EXPLICIT_DISPOSITION" if historical_unmapped else "PASS"
            ),
            "authority": "effective-dated account assignment registry",
            "disposition": "POOL_ONLY_UNALLOCATED",
        },
        {
            "exception_id": "account-nav-coverage-partial",
            "fact_type": "nav",
            "investment_account_count": investment_count,
            "severity": "BLOCKING_FOR_FULL_ATTRIBUTION",
            "status": "RETAINED_WITH_EXPLICIT_DISPOSITION",
            "authority": "governed HWM qualification and account NAV coverage",
            "disposition": "HWM_REMAINS_FAIL_CLOSED_FOR_UNQUALIFIED_SLEEVES",
        },
        {
            "exception_id": "transaction-coverage-partial",
            "fact_type": "transactions",
            "record_count": len(transactions),
            "severity": "DISCLOSED_GAP",
            "status": "RETAINED_WITH_EXPLICIT_DISPOSITION",
            "authority": "account fact coverage matrix",
            "disposition": "ACCEPT_ONLY_NAMED_PROVIDER_ACCOUNT_INTERVALS",
        },
        {
            "exception_id": "flow-coverage-partial",
            "fact_type": "flows",
            "record_count": len(flows),
            "severity": "DISCLOSED_GAP",
            "status": "RETAINED_WITH_EXPLICIT_DISPOSITION",
            "authority": "account fact coverage matrix",
            "disposition": "MWR_UNAVAILABLE_OUTSIDE_COMPLETE_FLOW_INTERVALS",
        },
    ]
    return claims, exceptions


def _coverage_evidence(
    accounts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    provider_admissions: Sequence[Mapping[str, Any]],
    provider_coverage: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    flows: Sequence[Mapping[str, Any]],
    as_of_date: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build value-free account/fact evidence without inventing completeness."""

    fact_specs = {
        "nav": (nav, "as_of_date", "nav_observations", "nav", "DAILY"),
        "positions": (
            positions,
            "as_of_date",
            "positions",
            "market_value",
            "SNAPSHOT",
        ),
        "transactions": (
            transactions,
            "event_date",
            "transactions",
            "amount",
            "EVENT_STREAM",
        ),
        "flows": (flows, "event_date", "flows", "amount", "EVENT_STREAM"),
    }
    coverage_by_provider = {
        str(item.get("provider_id", "")).upper(): _object(
            item.get("coverage"), "provider coverage"
        )
        for item in provider_coverage
    }
    admissions: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in provider_admissions:
        account_id = item.get("account_id")
        fact_type = item.get("fact_type")
        if account_id is not None and fact_type is not None:
            admissions[(str(account_id), str(fact_type))].append(item)

    matrix: list[dict[str, Any]] = []
    observation_windows: list[dict[str, Any]] = []
    for provider_id, coverage in sorted(coverage_by_provider.items()):
        observation_windows.append(
            {
                "boundary": coverage.get("boundary"),
                "observed_end": coverage.get("observed_end"),
                "observed_start": coverage.get("observed_start"),
                "provider_id": provider_id,
                "status": coverage.get("status"),
                "window_type": "PROVIDER_SOURCE",
            }
        )

    for fact_type, (
        records,
        date_field,
        admission_type,
        amount_field,
        cadence,
    ) in fact_specs.items():
        fact_dates = sorted(
            str(item[date_field]) for item in records if item.get(date_field)
        )
        observation_windows.append(
            {
                "fact_type": fact_type,
                "boundary_basis": "OBSERVED_RECORDS_ONLY",
                "cadence": cadence,
                "declared_end": None,
                "declared_start": None,
                "observed_end": fact_dates[-1] if fact_dates else None,
                "observed_start": fact_dates[0] if fact_dates else None,
                "record_count": len(records),
                "status": (
                    "OBSERVED_NOT_COMPLETENESS_PROOF"
                    if fact_dates
                    else "NO_ACCEPTED_OBSERVATION"
                ),
                "window_type": "ACCEPTED_FACT",
            }
        )
        records_by_account: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in records:
            records_by_account[str(item.get("account_id"))].append(item)
        for account in sorted(accounts, key=lambda item: str(item.get("account_id"))):
            if account.get("role") != "INVESTMENT":
                continue
            account_id = _text(account.get("account_id"), "account.account_id")
            provider_id = _text(account.get("provider"), "account.provider").upper()
            accepted = records_by_account.get(account_id, [])
            accepted_dates = sorted(
                str(item[date_field]) for item in accepted if item.get(date_field)
            )
            admitted = admissions.get((account_id, admission_type), [])
            authoritative = [
                item for item in admitted if item.get("disposition") == "AUTHORITATIVE"
            ]
            authority_rows = authoritative or admitted
            authority_starts = sorted(
                str(item["effective_start"])
                for item in authority_rows
                if item.get("effective_start")
            )
            authority_ends = sorted(
                str(item["effective_end"])
                for item in authority_rows
                if item.get("effective_end")
            )
            unavailable_amount_count = sum(
                1
                for item in accepted
                if isinstance(item.get(amount_field), Mapping)
                and item[amount_field].get("status") != "AVAILABLE"
            )
            if accepted:
                status = (
                    "OBSERVED_WITH_UNAVAILABLE_AMOUNTS_NOT_COMPLETENESS_PROOF"
                    if unavailable_amount_count
                    else "OBSERVED_NOT_COMPLETENESS_PROOF"
                )
                disposition = "RETAINED_ACCEPTED_REPORTED_FACTS_ONLY"
                missing_reason = (
                    "fact-specific completeness boundary is not declared; "
                    "observed records establish observations, not complete intervals"
                )
                authority = (
                    "PROVIDER_ADMISSION_AND_ACCEPTED_FACTS"
                    if admitted
                    else "LEGACY_ACCEPTED_FACTS"
                )
                boundary_basis = "OBSERVED_RECORDS_ONLY"
            elif admitted:
                status = (
                    "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"
                    if cadence == "EVENT_STREAM"
                    else "NO_REPORTED_FACT_NOT_COMPLETENESS_PROOF"
                )
                disposition = (
                    "RETAINED_UNATTESTED_EVENT_INTERVAL"
                    if cadence == "EVENT_STREAM"
                    else "RETAINED_NO_REPORTED_FACT"
                )
                missing_reason = (
                    "an effective-dated provider admission exists, but it is not a "
                    "fact completeness declaration"
                )
                authority = "PROVIDER_ADMISSION"
                boundary_basis = "EFFECTIVE_DATED_ADMISSION_NOT_COMPLETENESS"
            else:
                status = "NO_ACCEPTED_FACT_PATH"
                disposition = "RETAINED_NO_ACCEPTED_PROVIDER_PATH"
                missing_reason = (
                    "no admitted provider/account path or fact-specific completeness "
                    "declaration exists for this fact"
                )
                authority = "ACCOUNT_REGISTRY_NO_PROVIDER_SOURCE"
                boundary_basis = "NO_ACCEPTED_SOURCE"
            matrix.append(
                {
                    "account_id": account_id,
                    "admission_effective_end": (
                        authority_ends[-1] if authority_ends else None
                    ),
                    "admission_effective_start": (
                        authority_starts[0] if authority_starts else None
                    ),
                    "authority": authority,
                    "boundary_basis": boundary_basis,
                    "cadence": cadence,
                    "declared_end": None,
                    "declared_start": None,
                    "disposition": disposition,
                    "fact_type": fact_type,
                    "missing_interval_end": None,
                    "missing_interval_reason": missing_reason,
                    "missing_interval_start": None,
                    "missing_intervals": [],
                    "observed_end": accepted_dates[-1] if accepted_dates else None,
                    "observed_start": accepted_dates[0] if accepted_dates else None,
                    "provider_id": provider_id,
                    "record_count": len(accepted),
                    "status": status,
                    "unavailable_amount_count": unavailable_amount_count,
                }
            )

    attribution: list[dict[str, Any]] = []
    provider_by_account = {
        str(item.get("account_id")): str(item.get("provider")) for item in accounts
    }
    for item in assignments:
        if (
            item.get("mapping_status") != "UNMAPPED"
            or item.get("pool_id") == "reserve-capital"
            or item.get("strategy_mapping_disposition") == "RESERVE_NOT_APPLICABLE"
        ):
            continue
        start = item.get("effective_start")
        end = item.get("effective_end")
        current = (
            start is None or _date(start, "assignment.effective_start") <= as_of_date
        ) and (end is None or _date(end, "assignment.effective_end") >= as_of_date)
        account_id = _text(item.get("account_id"), "assignment.account_id")
        attribution.append(
            {
                "account_id": account_id,
                "authority": item.get("source_id") or item.get("source_classification"),
                "disposition": "POOL_ONLY_UNALLOCATED",
                "effective_end": end,
                "effective_start": start,
                "period": "CURRENT" if current else "HISTORICAL",
                "provider_id": provider_by_account.get(account_id),
            }
        )
    return (
        sorted(
            matrix, key=lambda item: (str(item["fact_type"]), str(item["account_id"]))
        ),
        sorted(
            observation_windows,
            key=lambda item: (
                str(item["window_type"]),
                str(item.get("fact_type") or item.get("provider_id")),
            ),
        ),
        sorted(
            attribution,
            key=lambda item: (
                str(item["period"]),
                str(item.get("effective_start")),
                str(item["account_id"]),
            ),
        ),
    )


def _iso_datetime(value: object, field_name: str) -> str:
    accepted = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineValidationError(f"{field_name} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise BaselineValidationError(
            f"{field_name} must be an ISO datetime with a timezone offset"
        )
    return accepted


def _hwm_policy_exclusions() -> dict[str, date]:
    """Load the admitted PORT-HWM-3 perimeter exclusions (strategy → effective
    date) from the versioned repository contract, extracted by the shared
    ledger-package helper so admission and consumption share one rule."""
    policy = _load_json(_HWM_POLICY_PATH, "hwm policy contract")
    try:
        return hwm_policy_exclusions(policy)
    except HwmInputsContractError as exc:
        raise BaselineValidationError(str(exc)) from exc


def _hwm_expected_keyspace(
    allocations: Sequence[Mapping[str, Any]],
    anchor: date,
    as_of: date,
) -> set[tuple[str, str]]:
    """Derive the complete expected (strategy_id, date) keyspace.

    The expected strategy set is the governed perimeter: every strategy with
    a systematic allocation in the catalog.  Perimeter membership never
    depends on account assignments, so an allocated but unmapped (e.g.
    provider-only) sleeve cannot be silently omitted.  Every perimeter
    strategy is expected on every day from the anchor through the catalog
    as-of date; the envelope's period keys must equal this set exactly so
    omission cannot masquerade as exclusion.  Assignments are used only to
    validate account-lane evidence, never to bound membership.
    """
    strategy_ids = sorted(
        {
            _text(item["strategy_id"], "allocation.strategy_id")
            for item in allocations
            if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id") is not None
        }
    )
    expected: set[tuple[str, str]] = set()
    for strategy_id in strategy_ids:
        current = anchor
        while current <= as_of:
            expected.add((strategy_id, current.isoformat()))
            current += timedelta(days=1)
    return expected


def _hwm_strategy_account_windows(
    assignments: Sequence[Mapping[str, Any]],
) -> dict[str, list[tuple[str, date, date | None]]]:
    """Index MAPPED assignments as (account_id, start, end) per strategy."""
    windows: dict[str, list[tuple[str, date, date | None]]] = defaultdict(list)
    for assignment in assignments:
        if assignment.get("mapping_status") != "MAPPED":
            continue
        strategy_id = assignment.get("strategy_id")
        start_value = assignment.get("effective_start")
        if start_value is None:
            raise BaselineValidationError(
                "mapped assignment requires an explicit effective_start"
            )
        if strategy_id is None:
            raise BaselineValidationError("mapped assignment requires a strategy_id")
        end_value = assignment.get("effective_end")
        windows[_text(strategy_id, "assignment.strategy_id")].append(
            (
                _text(assignment.get("account_id"), "assignment.account_id"),
                _date(start_value, "assignment.effective_start"),
                _date(end_value, "assignment.effective_end")
                if end_value is not None
                else None,
            )
        )
    return windows


def _hwm_governed_source_doc(
    spec_sources: Mapping[str, Any],
    roots: Mapping[str, Path],
    source_key: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Open a governed HWM source record bound to an artifact identity.

    The artifact id must be exactly the one derived from the admitted
    ``source_key`` spec source (whose file hash the builder has already
    verified), so the parsed content is hash-bound.  Anything else fails
    closed.
    """
    if source_key not in spec_sources:
        raise BaselineValidationError(
            f"hwm_inputs reference {artifact_id!r} does not resolve to an "
            f"admitted {source_key} source"
        )
    source_ref = _object(spec_sources[source_key], f"sources.{source_key}")
    relative = _portable_relative(
        source_ref["relative_path"], f"sources.{source_key}.relative_path"
    )
    expected_artifact_id = (
        f"sources/{source_key}/{hashlib.sha256(relative.encode()).hexdigest()[:16]}"
    )
    if artifact_id != expected_artifact_id:
        raise BaselineValidationError(
            f"hwm_inputs reference {artifact_id!r} does not resolve to the "
            f"admitted {source_key} source artifact"
        )
    root_id = _identifier(source_ref["root_id"], f"sources.{source_key}.root_id")
    if root_id not in roots:
        raise BaselineValidationError(f"source {source_key} has unknown root")
    return _load_json(_source_path(roots[root_id], relative), f"{source_key} record")


def _load_hwm_inputs_envelope(
    spec_sources: Mapping[str, Any],
    roots: Mapping[str, Path],
    as_of: date,
    final_source_links: Sequence[Mapping[str, Any]],
    strategies: Sequence[Mapping[str, Any]],
    accounts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    strategy_returns: Sequence[Mapping[str, Any]],
    allocations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], SourceArtifact, dict[str, Any], dict[str, Any]] | None:
    """Load and semantically validate the optional hwm_inputs envelope.

    When the spec declares an ``hwm-inputs`` source, load the JSON file,
    validate every cross-reference and disposition rule against the complete
    catalog context, and return the validated envelope together with its
    source artifact and evidence link.  When absent the function returns
    ``None`` and the catalog omits the ``hwm_inputs`` key entirely.

    Enforcement is fail-closed: the expected (strategy, date) keyspace is
    derived from the governed perimeter (every systematic allocation in the
    catalog) over the anchor-to-as-of window and must equal the envelope's
    period keys exactly; the anchor must equal the admitted
    ``hwm-anchor-acceptance`` record on the governed fields; every artifact
    reference resolves against the complete final source-artifact set with a
    required schema role and a verified hash; attestations must resolve to
    hash-bound records whose content asserts the period's strategy, date,
    and value; ``EXCLUDED_BY_POLICY`` rows must be named in the admitted
    PORT-HWM-3 exclusions effective for the period date; ACCOUNT-lane
    capital is verified against the hash-bound NAV-rollforward rows in
    ``strategy_returns`` (never the allocation snapshot); PROVIDER- and
    LEGACY-lane AVAILABLE periods are rejected until hash-bound provider or
    legacy capital evidence is admitted; and the allocation endpoint check
    is recomputed independently.
    """
    if _HWM_INPUTS_SOURCE_KEY not in spec_sources:
        return None
    ref = _object(spec_sources[_HWM_INPUTS_SOURCE_KEY], "sources.hwm-inputs")
    _strict_keys(
        ref,
        {
            "as_of_date",
            "expected_sha256",
            "relative_path",
            "root_id",
            "schema_name",
            "schema_version",
        },
        "sources.hwm-inputs",
    )
    if _text(ref["schema_name"], "sources.hwm-inputs.schema_name") != (
        _HWM_INPUTS_SOURCE_KEY
    ):
        raise BaselineValidationError(
            "sources.hwm-inputs schema_name must equal the governed role "
            f"{_HWM_INPUTS_SOURCE_KEY!r}"
        )
    root_id = _identifier(ref["root_id"], "sources.hwm-inputs.root_id")
    if root_id not in roots:
        raise BaselineValidationError("hwm-inputs source has unknown root")
    relative_path = _portable_relative(
        ref["relative_path"], "sources.hwm-inputs.relative_path"
    )
    expected_hash = _text(ref["expected_sha256"], "sources.hwm-inputs.expected_sha256")
    if _SHA256.fullmatch(expected_hash) is None:
        raise BaselineValidationError("hwm-inputs source hash is invalid")
    source_as_of = _date(ref["as_of_date"], "sources.hwm-inputs.as_of_date")
    if source_as_of > as_of:
        raise BaselineValidationError("hwm-inputs source is newer than the baseline")
    path = _source_path(roots[root_id], relative_path)
    actual_hash, _size = hash_file(path)
    if actual_hash != expected_hash:
        raise BaselineValidationError("hwm-inputs source hash differs")
    envelope = _load_json(path, "hwm-inputs envelope")

    # --- typed registry over the COMPLETE final source-artifact set ---
    # Provider artifacts are admitted before this validation runs, so
    # references resolve against the final artifact set, not a partial one.
    artifact_registry: dict[str, tuple[str, str]] = {}
    for link in final_source_links:
        artifact_id = _text(link.get("artifact_id"), "source_artifact.artifact_id")
        artifact_registry[artifact_id] = (
            _text(link.get("schema_name"), "source_artifact.schema_name"),
            _text(link.get("sha256"), "source_artifact.sha256"),
        )

    def _resolve_typed_artifact(
        artifact_id: str, allowed_roles: set[str], field_name: str
    ) -> None:
        entry = artifact_registry.get(artifact_id)
        if entry is None:
            raise BaselineValidationError(
                f"{field_name} {artifact_id!r} does not resolve to an admitted "
                "source artifact"
            )
        if entry[0] not in allowed_roles:
            raise BaselineValidationError(
                f"{field_name} {artifact_id!r} has schema role {entry[0]!r}; "
                f"required one of {sorted(allowed_roles)}"
            )

    # --- contract_version ---
    _strict_keys(
        envelope,
        {
            "contract_version",
            "anchor",
            "calendar_authorities",
            "periods",
            "reconciliation",
        },
        "hwm_inputs",
    )
    if envelope["contract_version"] != 1:
        raise BaselineValidationError("hwm_inputs contract_version must be 1")

    # --- anchor ---
    anchor = _object(envelope["anchor"], "hwm_inputs.anchor")
    _strict_keys(
        anchor,
        {
            "anchor_date",
            "index_base_value",
            "hwm_policy_id",
            "acceptance_provenance",
        },
        "hwm_inputs.anchor",
    )
    anchor_date = _date(anchor["anchor_date"], "hwm_inputs.anchor.anchor_date")
    if anchor_date > as_of:
        raise BaselineValidationError(
            "hwm_inputs anchor_date must not be after catalog as_of_date"
        )
    if _text(anchor["index_base_value"], "hwm_inputs.anchor.index_base_value") != "1.0":
        raise BaselineValidationError("hwm_inputs anchor index_base_value must be 1.0")
    if (
        _text(anchor["hwm_policy_id"], "hwm_inputs.anchor.hwm_policy_id")
        != "PORT-HWM-3"
    ):
        raise BaselineValidationError(
            "hwm_inputs anchor hwm_policy_id must be PORT-HWM-3"
        )

    # --- acceptance_provenance: typed, hash-bound, ISO-typed timestamp ---
    prov = _object(
        anchor["acceptance_provenance"],
        "hwm_inputs.anchor.acceptance_provenance",
    )
    _strict_keys(
        prov,
        {
            "accepted_at",
            "accepted_by",
            "source_artifact_id",
            "source_artifact_sha256",
        },
        "hwm_inputs.anchor.acceptance_provenance",
    )
    _iso_datetime(prov["accepted_at"], "hwm_inputs.acceptance_provenance.accepted_at")
    _text(prov["accepted_by"], "hwm_inputs.acceptance_provenance.accepted_by")
    prov_artifact_id = _text(
        prov["source_artifact_id"],
        "hwm_inputs.acceptance_provenance.source_artifact_id",
    )
    prov_sha256 = _text(
        prov["source_artifact_sha256"],
        "hwm_inputs.acceptance_provenance.source_artifact_sha256",
    )
    if _SHA256.fullmatch(prov_sha256) is None:
        raise BaselineValidationError(
            "hwm_inputs acceptance_provenance source_artifact_sha256 is invalid"
        )
    _resolve_typed_artifact(
        prov_artifact_id,
        {_HWM_ANCHOR_ACCEPTANCE_ROLE},
        "hwm_inputs acceptance_provenance source_artifact_id",
    )
    if artifact_registry[prov_artifact_id][1] != prov_sha256:
        raise BaselineValidationError(
            "hwm_inputs acceptance_provenance sha256 does not match catalog "
            "source_artifact"
        )

    # --- anchor content cross-check: the envelope's anchor must equal the
    # admitted acceptance record byte-for-byte on the governed fields, so a
    # fabricated later anchor cannot truncate the derived keyspace.  The
    # acceptance record must be the admitted hwm-anchor-acceptance governed
    # source (the only role the builder can open and hash-verify). ---
    acceptance_doc = _hwm_governed_source_doc(
        spec_sources,
        roots,
        _HWM_ANCHOR_ACCEPTANCE_ROLE,
        prov_artifact_id,
    )
    for field in ("anchor_date", "index_base_value", "hwm_policy_id"):
        if acceptance_doc.get(field) != anchor[field]:
            raise BaselineValidationError(
                f"hwm_inputs anchor {field} disagrees with the admitted "
                "hwm-anchor-acceptance record"
            )

    # --- calendar_authorities: one typed admitted authority per lane ---
    cal_authorities = _objects(
        envelope["calendar_authorities"], "hwm_inputs.calendar_authorities"
    )
    if not cal_authorities:
        raise BaselineValidationError(
            "hwm_inputs calendar_authorities must have at least one entry"
        )
    authority_lanes: set[str] = set()
    for item in cal_authorities:
        _strict_keys(
            item,
            {
                "lane",
                "authority_id",
                "authority_version",
                "calendar_kind",
                "admitted_source_artifact_id",
            },
            "hwm_inputs.calendar_authorities[*]",
        )
        lane = _text(item["lane"], "calendar_authorities.lane")
        if lane not in _HWM_VALID_LANES:
            raise BaselineValidationError(
                f"calendar_authorities lane {lane!r} is invalid"
            )
        if lane in authority_lanes:
            raise BaselineValidationError(
                f"duplicate calendar_authorities lane {lane!r}"
            )
        authority_lanes.add(lane)
        _text(item["authority_id"], "calendar_authorities.authority_id")
        _text(
            item["authority_version"],
            "calendar_authorities.authority_version",
        )
        kind = _text(item["calendar_kind"], "calendar_authorities.calendar_kind")
        if kind not in _HWM_VALID_CALENDAR_KINDS:
            raise BaselineValidationError(
                f"calendar_authorities calendar_kind {kind!r} is invalid"
            )
        _resolve_typed_artifact(
            _text(
                item["admitted_source_artifact_id"],
                "calendar_authorities.admitted_source_artifact_id",
            ),
            {_HWM_CALENDAR_AUTHORITY_ROLES[lane]},
            "calendar_authorities admitted_source_artifact_id",
        )

    # --- catalog evidence indexes ---
    strategy_ids = {_text(s["strategy_id"], "strategy.strategy_id") for s in strategies}
    known_accounts = {_text(a["account_id"], "account.account_id") for a in accounts}
    account_windows = _hwm_strategy_account_windows(assignments)
    policy_exclusions = _hwm_policy_exclusions()

    def _mapped_account(strategy_id: str, account_id: str, day: date) -> bool:
        return any(
            candidate == account_id and start <= day and (end is None or day <= end)
            for candidate, start, end in account_windows.get(strategy_id, [])
        )

    # Provider lane: the current catalog shapes carry no hash-bound provider
    # capital evidence (provider-live rows have no beginning_capital, no
    # component account set, and no reconciliation hash), so every PROVIDER
    # AVAILABLE period is rejected fail-closed.  Deferred admission
    # requirement: a hash-bound provider rollforward/capital schedule must be
    # admitted before PROVIDER-lane AVAILABLE periods can validate.

    # Hash-bound NAV-rollforward rows (the daily capital authority for the
    # ACCOUNT lane; the allocation snapshot is never a capital source).
    rollforward: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in strategy_returns:
        if row.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        if row.get("beginning_capital") is None:
            continue
        key = (
            _text(row.get("strategy_id"), "strategy_returns.strategy_id"),
            _text(row.get("date"), "strategy_returns.date"),
        )
        rollforward[key] = row

    # --- shared admission/consumption validation ---
    # The ledger-package validator is the single authority for the hwm_inputs
    # contract.  Run it at admission (with the builder's full context) so
    # every check that both consumers enforce is also enforced here.
    perimeter = sorted(
        {
            _text(item["strategy_id"], "allocation.strategy_id")
            for item in allocations
            if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id")
        }
    )
    try:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=as_of,
            perimeter=perimeter,
            crosswalk=strategy_returns,
            artifact_roles=artifact_registry,
            policy_exclusions=policy_exclusions,
            allocations=allocations,
            acceptance_record=acceptance_doc,
        )
    except HwmInputsContractError as exc:
        raise BaselineValidationError(str(exc)) from exc

    # --- periods ---
    periods = _objects(envelope["periods"], "hwm_inputs.periods")
    if not periods:
        raise BaselineValidationError("hwm_inputs periods must have at least one row")
    seen_keys: set[tuple[str, str]] = set()
    for row in periods:
        actual_keys = set(row)
        if not _HWM_PERIOD_REQUIRED_KEYS.issubset(actual_keys):
            raise BaselineValidationError(
                f"periods row missing required keys: "
                f"{sorted(_HWM_PERIOD_REQUIRED_KEYS - actual_keys)}"
            )
        if actual_keys - _HWM_PERIOD_KEYS:
            raise BaselineValidationError(
                f"periods row has unexpected keys: "
                f"{sorted(actual_keys - _HWM_PERIOD_KEYS)}"
            )
        sid = _text(row["strategy_id"], "periods.strategy_id")
        if sid not in strategy_ids:
            raise BaselineValidationError(
                f"periods strategy_id {sid!r} is not a known strategy"
            )
        day = _date(row["date"], "periods.date")
        if day > as_of:
            raise BaselineValidationError(
                "periods date must not be after catalog as_of_date"
            )
        lane = _text(row["lane"], "periods.lane")
        if lane not in _HWM_VALID_LANES:
            raise BaselineValidationError(f"periods lane {lane!r} is invalid")
        if lane not in authority_lanes:
            raise BaselineValidationError(
                f"periods lane {lane!r} has no admitted calendar authority"
            )
        disp = _text(row["disposition"], "periods.disposition")
        if disp not in _HWM_VALID_DISPOSITIONS:
            raise BaselineValidationError(f"periods disposition {disp!r} is invalid")
        flow_status = _text(row["flow_status"], "periods.flow_status")
        if flow_status not in _HWM_VALID_FLOW_STATUSES:
            raise BaselineValidationError(
                f"periods flow_status {flow_status!r} is invalid"
            )
        fx_status = _text(row["fx_status"], "periods.fx_status")
        if fx_status not in _HWM_VALID_FX_STATUSES:
            raise BaselineValidationError(f"periods fx_status {fx_status!r} is invalid")

        key = (sid, day.isoformat())
        if key in seen_keys:
            raise BaselineValidationError(
                f"duplicate period for strategy {sid!r} on {day.isoformat()}"
            )
        seen_keys.add(key)

        if disp == "EXCLUDED_BY_POLICY":
            effective = policy_exclusions.get(sid)
            if effective is None or day < effective:
                raise BaselineValidationError(
                    f"EXCLUDED_BY_POLICY period for strategy {sid!r} on "
                    f"{day.isoformat()} is not named in the admitted PORT-HWM-3 "
                    "exclusions effective for that date"
                )

        capital_val = row["beginning_capital_usd"]
        return_ref = row["return_source_record_id"]
        component_ids = row.get("component_account_ids")
        if component_ids is not None:
            if not isinstance(component_ids, list) or any(
                not isinstance(item, str) or not item.strip() for item in component_ids
            ):
                raise BaselineValidationError(
                    "periods component_account_ids must be non-empty text"
                )
            for component_id in component_ids:
                if component_id not in known_accounts:
                    raise BaselineValidationError(
                        f"periods component account {component_id!r} is not a "
                        "known account"
                    )

        if disp == "AVAILABLE":
            if (
                capital_val is None
                or not isinstance(capital_val, str)
                or not capital_val.strip()
            ):
                raise BaselineValidationError(
                    "AVAILABLE period must have non-null beginning_capital_usd"
                )
            capital = _decimal(capital_val, "beginning_capital_usd")
            assert capital is not None
            if (
                return_ref is None
                or not isinstance(return_ref, str)
                or not return_ref.strip()
            ):
                raise BaselineValidationError(
                    "AVAILABLE period must have non-null return_source_record_id"
                )
            if flow_status == "MISSING":
                raise BaselineValidationError(
                    "AVAILABLE period must not have MISSING flow_status"
                )
            if fx_status == "MISSING":
                raise BaselineValidationError(
                    "AVAILABLE period must not have MISSING fx_status"
                )
            if lane == "ACCOUNT":
                if not component_ids:
                    raise BaselineValidationError(
                        "ACCOUNT lane AVAILABLE period requires non-empty "
                        "component_account_ids"
                    )
                for component_id in component_ids:
                    if not _mapped_account(sid, component_id, day):
                        raise BaselineValidationError(
                            f"component account {component_id!r} is not mapped to "
                            f"strategy {sid!r} on {day.isoformat()}"
                        )
                evidence = rollforward.get((sid, day.isoformat()))
                if evidence is None:
                    raise BaselineValidationError(
                        "ACCOUNT lane AVAILABLE period lacks hash-bound NAV "
                        f"rollforward evidence for strategy {sid!r} on "
                        f"{day.isoformat()}"
                    )
                evidence_capital = _decimal(
                    evidence.get("beginning_capital"),
                    "strategy_returns.beginning_capital",
                )
                if evidence_capital != capital:
                    raise BaselineValidationError(
                        "beginning_capital_usd does not match the hash-bound NAV "
                        f"rollforward evidence for strategy {sid!r} on "
                        f"{day.isoformat()}"
                    )
                if sorted(
                    str(item) for item in evidence.get("component_account_ids", [])
                ) != sorted(component_ids):
                    raise BaselineValidationError(
                        "component_account_ids differ from the NAV rollforward "
                        f"evidence for strategy {sid!r} on {day.isoformat()}"
                    )
                if (
                    _text(
                        evidence.get("account_reconciliation_sha256"),
                        "strategy_returns.account_reconciliation_sha256",
                    )
                    != return_ref
                ):
                    raise BaselineValidationError(
                        "return_source_record_id does not match the hash-bound "
                        f"NAV rollforward evidence for strategy {sid!r} on "
                        f"{day.isoformat()}"
                    )
            elif lane == "PROVIDER":
                raise BaselineValidationError(
                    "PROVIDER lane AVAILABLE period requires hash-bound provider "
                    "capital evidence; none is admitted by the current catalog "
                    "(deferred admission requirement) — the period must be "
                    "UNAVAILABLE"
                )
            else:
                raise BaselineValidationError(
                    "LEGACY lane has no admitted effective-dated capital "
                    "schedule; the period must be UNAVAILABLE"
                )
        else:
            # UNAVAILABLE, NOT_ADMITTED, EXPECTED_NON_TRADING, and
            # EXCLUDED_BY_POLICY all carry null capital and a null return
            # reference — an unavailable period must never carry capital.
            if capital_val is not None:
                raise BaselineValidationError(
                    f"{disp} period must have null beginning_capital_usd"
                )
            if return_ref is not None:
                raise BaselineValidationError(
                    f"{disp} period must have null return_source_record_id"
                )

        # Attestation: CONFIRMED_* statuses resolve every reference to an
        # admitted, hash-verified artifact of the matching attestation role
        # whose parsed CONTENT asserts exactly this (strategy, date, value).
        def _check_attestation(
            source_id: str,
            role_key: str,
            status_field: str,
            asserted: str,
            field_name: str,
        ) -> None:
            _resolve_typed_artifact(source_id, {role_key}, field_name)
            doc = _hwm_governed_source_doc(spec_sources, roots, role_key, source_id)
            _strict_keys(
                doc,
                {"strategy_id", "date", status_field},
                f"{role_key} record",
            )
            kind = status_field.split("_", 1)[0]
            if doc["strategy_id"] != sid:
                raise BaselineValidationError(
                    f"{kind} attestation strategy_id does not match the period"
                )
            if doc["date"] != day.isoformat():
                raise BaselineValidationError(
                    f"{kind} attestation date does not match the period"
                )
            if doc[status_field] != asserted:
                raise BaselineValidationError(
                    f"{kind} attestation must assert {status_field} {asserted}"
                )

        flow_src_ids = row.get("flow_source_record_ids", [])
        if not isinstance(flow_src_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in flow_src_ids
        ):
            raise BaselineValidationError("periods flow_source_record_ids must be text")
        if flow_status == "CONFIRMED_ZERO":
            if not flow_src_ids:
                raise BaselineValidationError(
                    "CONFIRMED_ZERO flow_status requires non-empty "
                    "flow_source_record_ids"
                )
            for source_id in flow_src_ids:
                _check_attestation(
                    source_id,
                    "hwm-flow-attestation",
                    "flow_status",
                    "CONFIRMED_ZERO",
                    "periods flow_source_record_ids",
                )
        fx_src_ids = row.get("fx_source_record_ids", [])
        if not isinstance(fx_src_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in fx_src_ids
        ):
            raise BaselineValidationError("periods fx_source_record_ids must be text")
        if fx_status == "CONFIRMED_ONE":
            if not fx_src_ids:
                raise BaselineValidationError(
                    "CONFIRMED_ONE fx_status requires non-empty fx_source_record_ids"
                )
            for source_id in fx_src_ids:
                _check_attestation(
                    source_id,
                    "hwm-fx-attestation",
                    "fx_status",
                    "CONFIRMED_ONE",
                    "periods fx_source_record_ids",
                )

    # --- expected-keyspace completeness ---
    expected_keys = _hwm_expected_keyspace(allocations, anchor_date, as_of)
    if seen_keys != expected_keys:
        raise BaselineValidationError(
            "hwm_inputs period keyspace differs from the expected perimeter "
            f"keyspace: missing={sorted(expected_keys - seen_keys)[:5]}, "
            f"unexpected={sorted(seen_keys - expected_keys)[:5]}"
        )

    # --- reconciliation: recompute the allocation endpoint check ---
    rec = _object(envelope["reconciliation"], "hwm_inputs.reconciliation")
    _strict_keys(
        rec,
        {"period_capital_method", "allocation_endpoint_check"},
        "hwm_inputs.reconciliation",
    )
    if (
        _text(rec["period_capital_method"], "reconciliation.period_capital_method")
        != "NAV_ROLLFORWARD"
    ):
        raise BaselineValidationError(
            "hwm_inputs reconciliation period_capital_method must be NAV_ROLLFORWARD"
        )
    endpoint = _object(
        rec["allocation_endpoint_check"],
        "hwm_inputs.reconciliation.allocation_endpoint_check",
    )
    _strict_keys(
        endpoint,
        {"matched", "delta_usd"},
        "hwm_inputs.reconciliation.allocation_endpoint_check",
    )
    if not isinstance(endpoint["matched"], bool):
        raise BaselineValidationError(
            "reconciliation.allocation_endpoint_check.matched must be boolean"
        )
    declared_delta = _decimal(
        endpoint["delta_usd"],
        "reconciliation.allocation_endpoint_check.delta_usd",
    )
    assert declared_delta is not None

    snapshot_dates = {
        _date(item["as_of_date"], "allocation.as_of_date")
        for item in allocations
        if item.get("scope") == "SYSTEMATIC" and item.get("strategy_id") is not None
    }
    if not snapshot_dates:
        raise BaselineValidationError(
            "no systematic allocation snapshot exists for endpoint reconciliation"
        )
    snapshot_date = max(snapshot_dates)
    verified_endpoints: dict[str, Decimal] = {}
    for row in periods:
        if row["disposition"] != "AVAILABLE" or row["lane"] != "ACCOUNT":
            continue
        day = _date(row["date"], "periods.date")
        if day != snapshot_date:
            continue
        evidence = rollforward[(str(row["strategy_id"]), day.isoformat())]
        capital = _decimal(row["beginning_capital_usd"], "beginning_capital_usd")
        period_return = _decimal(evidence.get("return"), "strategy_returns.return")
        assert capital is not None and period_return is not None
        verified_endpoints[str(row["strategy_id"])] = capital * (
            Decimal(1) + period_return
        )
    envelope_total = sum(verified_endpoints.values(), Decimal(0))
    allocation_total = Decimal(0)
    for allocation in allocations:
        if (
            allocation.get("scope") != "SYSTEMATIC"
            or allocation.get("strategy_id") not in verified_endpoints
            or _date(allocation["as_of_date"], "allocation.as_of_date") != snapshot_date
        ):
            continue
        allocation_value = _decimal(
            _object(allocation["current_value"], "allocation.current_value").get(
                "value"
            ),
            "allocation.current_value",
        )
        assert allocation_value is not None
        allocation_total += allocation_value
    recomputed_delta = envelope_total - allocation_total
    if recomputed_delta != 0:
        raise BaselineValidationError(
            "allocation endpoint check does not reconcile: recomputed delta "
            f"{decimal_text(recomputed_delta)} between rollforward endpoint "
            "capital and the systematic allocation snapshot"
        )
    if decimal_text(declared_delta) != decimal_text(recomputed_delta):
        raise BaselineValidationError(
            "reconciliation.allocation_endpoint_check.delta_usd differs from the "
            f"recomputed delta {decimal_text(recomputed_delta)}"
        )
    if endpoint["matched"] is not True:
        raise BaselineValidationError(
            "reconciliation.allocation_endpoint_check.matched must be true for a "
            "reconciled endpoint"
        )

    # Build source artifact and evidence link for the hwm-inputs source.
    schema_name = _text(ref["schema_name"], "sources.hwm-inputs.schema_name")
    schema_version = _text(ref["schema_version"], "sources.hwm-inputs.schema_version")
    artifact_id = (
        f"sources/hwm-inputs/{hashlib.sha256(relative_path.encode()).hexdigest()[:16]}"
    )
    hwm_artifact = SourceArtifact(
        artifact_id=artifact_id,
        sha256=actual_hash,
        size_bytes=_size,
        as_of_date=source_as_of,
        schema_name=schema_name,
        schema_version=schema_version,
    )
    hwm_link: dict[str, Any] = {
        "artifact_id": artifact_id,
        "as_of_date": source_as_of.isoformat(),
        "schema_name": schema_name,
        "schema_version": schema_version,
        "sha256": actual_hash,
        "source_key": "hwm-inputs",
    }
    return envelope, hwm_artifact, hwm_link, acceptance_doc


def build_catalog(
    spec_path: Path,
) -> tuple[dict[str, Any], GenerationManifest, bytes]:
    """Build and validate one deterministic baseline candidate."""

    spec = _load_spec(spec_path)
    as_of = _date(spec["as_of_date"], "spec.as_of_date")
    roots = _roots(spec["roots"])
    paths, source_artifacts, source_links = _sources(spec["sources"], roots, as_of)
    source_dates = {
        str(item["source_key"]): _date(item["as_of_date"], "source.as_of_date")
        for item in source_links
    }
    provenance, provenance_artifact, provenance_ref = _provenance(
        spec["provenance"], require_current=spec["schema_version"] < 3
    )
    accounts, account_aliases = _account_index(provenance)
    strategies, strategy_aliases = _strategy_index(provenance)
    assignments = _objects(provenance.get("assignments"), "provenance.assignments")
    nav, provider_dates, _ = _normalize_nav(paths, account_aliases)
    nav_reconciliation = _reconcile_nav(
        paths["attribution-diagnostic"], account_aliases, nav
    )
    calendar = _calendar_statuses(accounts, nav, provider_dates)
    flows = _normalize_flows(paths, account_aliases, assignments)
    transactions, securities = _normalize_transactions(
        paths["alpaca-events"], account_aliases, assignments
    )
    positions: list[dict[str, Any]] = []
    provider_known_gaps: list[str] = []
    schwab_reconciliation: dict[str, Any] | None = None
    position_reconciliation: dict[str, Any] | None = None
    provider_artifacts: tuple[SourceArtifact, ...] = ()
    provider_links: list[dict[str, Any]] = []
    provider_admissions: list[dict[str, Any]] = []
    provider_coverage: list[dict[str, Any]] = []
    provider_unmapped: list[dict[str, Any]] = []
    strategy_provider_evidence: list[dict[str, Any]] = []
    strategy_returns: list[dict[str, Any]] = []
    provider_strategy_returns: list[dict[str, Any]] = []
    if spec["schema_version"] == 2:
        schwab_catalog, provider_artifacts, provider_links = _schwab_generation(
            spec["provider_generations"], roots, accounts, as_of
        )
        (
            positions,
            schwab_transactions,
            schwab_securities,
            schwab_reconciliation,
            position_reconciliation,
            provider_known_gaps,
        ) = _schwab_facts(schwab_catalog, accounts, assignments, nav, as_of)
        transactions.extend(schwab_transactions)
        transactions.sort(
            key=lambda item: (str(item["event_date"]), str(item["record_id"]))
        )
        if len({str(item["record_id"]) for item in transactions}) != len(transactions):
            raise BaselineValidationError(
                "combined transaction identifiers are not unique"
            )
        by_security = {str(item["security_id"]): item for item in securities}
        for item in schwab_securities:
            security_id = str(item["security_id"])
            prior = by_security.get(security_id)
            if prior is not None and prior != item:
                raise BaselineValidationError("security identity conflicts")
            by_security[security_id] = item
        securities = sorted(
            by_security.values(), key=lambda item: str(item["security_id"])
        )
    elif spec["schema_version"] == SPEC_SCHEMA_VERSION:
        provider_catalog, provider_artifacts, provider_links = _provider_ledger(
            spec["provider_ledger"], roots, as_of
        )
        (
            nav,
            flows,
            transactions,
            positions,
            securities,
            raw_provider_unmapped,
            schwab_reconciliation,
            provider_known_gaps,
        ) = _provider_ledger_facts(
            provider_catalog,
            accounts,
            nav,
            flows,
            transactions,
            securities,
            as_of,
        )
        provider_admissions = _objects(
            provider_catalog.get("admissions"), "provider-ledger.admissions"
        )
        provider_coverage = _objects(
            provider_catalog.get("provider_coverage"),
            "provider-ledger.provider_coverage",
        )
        strategy_provider_evidence = _objects(
            provider_catalog.get("strategy_evidence"),
            "provider-ledger.strategy_evidence",
        )
        strategy_returns = _objects(
            provider_catalog.get("strategy_returns"),
            "provider-ledger.strategy_returns",
        )
        provider_strategy_returns = _objects(
            provider_catalog.get("provider_strategy_returns"),
            "provider-ledger.provider_strategy_returns",
        )
        grouped_unmapped: dict[tuple[str, str], int] = {}
        for item in raw_provider_unmapped:
            key = (
                _text(item.get("provider_id"), "quality provider_id"),
                _text(item.get("fact_type"), "quality fact_type"),
            )
            grouped_unmapped[key] = grouped_unmapped.get(key, 0) + 1
        provider_unmapped = [
            {
                "fact_type": fact_type,
                "provider_id": provider_id,
                "record_count": count,
                "source_key": "provider-ledger",
                "status": "UNMAPPED",
            }
            for (provider_id, fact_type), count in sorted(grouped_unmapped.items())
        ]
        for record in nav:
            provider_dates.setdefault(
                str(record.get("provider", "UNKNOWN")), set()
            ).add(_date(record["as_of_date"], "nav.as_of_date"))
        calendar = _calendar_statuses(accounts, nav, provider_dates)
    allocations, strategy_nav_coverage, costs, systematic_total, nav_covered = (
        _allocations_and_coverage(
            paths,
            strategy_aliases,
            source_dates["component-decision-table"],
            as_of,
        )
    )
    # The envelope resolves attestations and provenance against the COMPLETE
    # final artifact set, so it is validated only after provider artifacts and
    # the allocation snapshot exist.
    hwm_inputs_result = _load_hwm_inputs_envelope(
        spec["sources"],
        roots,
        as_of,
        [*source_links, *provider_links],
        strategies,
        accounts,
        assignments,
        strategy_returns,
        allocations,
    )
    hwm_inputs_envelope: dict[str, Any] | None = None
    hwm_inputs_artifact: SourceArtifact | None = None
    hwm_inputs_link: dict[str, Any] | None = None
    if hwm_inputs_result is not None:
        hwm_inputs_envelope, hwm_inputs_artifact, hwm_inputs_link, hwm_acceptance_record = hwm_inputs_result
    layer_metrics, live_metrics, implementation_gaps = _normalize_strategy_evidence(
        paths, strategy_aliases
    )
    replay, reserve = _replay(spec["replay"], paths, systematic_total, nav_covered)
    reserve_id = next(
        str(item["account_id"]) for item in accounts if item["role"] == "RESERVE"
    )
    reserve_payload = {
        "as_of_date": source_dates["current-portfolio-summary"].isoformat(),
        "current_value": {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": decimal_text(reserve),
        },
        "pool_id": "reserve-capital",
        "publication_date": as_of.isoformat(),
        "scope": "RESERVE",
        "source_key": "current-portfolio-summary",
        "source_effective_date": source_dates["current-portfolio-summary"].isoformat(),
        "strategy_id": None,
        "systematic_weight": None,
        "account_id": reserve_id,
        "valuation_date": source_dates["current-portfolio-summary"].isoformat(),
    }
    allocations.append(
        {"allocation_id": _record_id("allocation", reserve_payload), **reserve_payload}
    )
    allocations.sort(
        key=lambda item: (str(item["scope"]), str(item.get("strategy_id") or ""))
    )
    missing_strategy_nav_count = sum(
        1 for item in strategy_nav_coverage if item["status"] == "UNAVAILABLE"
    )
    fact_coverage, exceptions = _fact_coverage(
        accounts,
        assignments,
        transactions,
        flows,
        positions,
        provider_known_gaps,
        missing_strategy_nav_count,
        as_of,
        nav=nav,
        costs=costs,
    )
    fact_coverage_matrix, observation_windows, attribution_dispositions = (
        _coverage_evidence(
            accounts,
            assignments,
            provider_admissions,
            provider_coverage,
            nav,
            positions,
            transactions,
            flows,
            as_of,
        )
    )
    unmapped = [
        *sorted(
            [item for item in assignments if item.get("mapping_status") == "UNMAPPED"],
            key=lambda item: str(item.get("assignment_id")),
        ),
        *provider_unmapped,
    ]
    evidence_links = [
        {
            "evidence_layer": route["evidence_layer"],
            "purpose": route["purpose"],
            "source_id": route["source_id"],
            "strategy_id": strategy["strategy_id"],
        }
        for strategy in strategies
        for route in _objects(
            strategy.get("evidence_routes"), "strategy.evidence_routes"
        )
    ]
    catalog: dict[str, Any] = {
        "accounts": accounts,
        "allocations": allocations,
        "as_of_date": as_of.isoformat(),
        "assignments": sorted(
            assignments, key=lambda item: str(item.get("assignment_id"))
        ),
        "assumptions": provenance.get("assumptions", []),
        "baseline_schema_version": CATALOG_SCHEMA_VERSION,
        "calendar_coverage": calendar,
        "capital_pools": provenance.get("capital_pools")
        or [
            {
                "base_currency": "USD",
                "name": "Systematic capital",
                "pool_id": "systematic-capital",
                "role": "SYSTEMATIC",
            },
            {
                "base_currency": "USD",
                "name": "Reserve capital",
                "pool_id": "reserve-capital",
                "role": "RESERVE",
            },
        ],
        "corrections": provenance.get("corrections", []),
        "costs": costs,
        "evidence_layer_metrics": layer_metrics,
        "evidence_links": sorted(
            evidence_links,
            key=lambda item: (
                str(item["strategy_id"]),
                str(item["source_id"]),
                str(item["evidence_layer"]),
            ),
        ),
        "fact_coverage": fact_coverage,
        "fact_coverage_matrix": fact_coverage_matrix,
        "flows": flows,
        "implementation_gaps": implementation_gaps,
        "nav_observations": nav,
        "observation_windows": observation_windows,
        "policy_version": POLICY_VERSION,
        "positions": positions,
        "provider_admissions": provider_admissions,
        "provider_coverage": provider_coverage,
        "provider_strategy_returns": provider_strategy_returns,
        "provenance": provenance_ref,
        "reconciliation_checks": [
            nav_reconciliation,
            {
                "check_id": "allocation-to-audit-systematic-total",
                "status": "PASS",
                "value_usd": decimal_text(systematic_total),
            },
            {
                "check_id": "allocation-plus-reserve-to-liquid-total",
                "status": "PASS",
                "value_usd": replay["total_liquid_usd"],
            },
            *([schwab_reconciliation] if schwab_reconciliation is not None else []),
            *([position_reconciliation] if position_reconciliation is not None else []),
        ],
        "reconciliation_exceptions": exceptions,
        "replay": replay,
        "securities": securities,
        "source_artifacts": [
            *source_links,
            *([hwm_inputs_link] if hwm_inputs_link is not None else []),
            *provider_links,
        ],
        "strategies": strategies,
        "strategy_live_metrics": live_metrics,
        "strategy_nav_coverage": strategy_nav_coverage,
        "strategy_provider_evidence": strategy_provider_evidence,
        "strategy_returns": strategy_returns,
        "transactions": transactions,
        "unmapped_records": unmapped,
        "attribution_dispositions": attribution_dispositions,
    }
    if hwm_inputs_envelope is not None:
        catalog["hwm_inputs"] = hwm_inputs_envelope
        # Embed the hash-verified acceptance record so consumers can
        # perform the anchor-content cross-check without access to the
        # governed source files.
        catalog["hwm_acceptance_record"] = hwm_acceptance_record
    if provenance.get("strategy_version_catalog") is not None:
        catalog["strategy_version_catalog"] = provenance["strategy_version_catalog"]
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_artifact = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=as_of,
        schema_name="portfolio-baseline-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    # Physical root bindings are runtime locators, not financial evidence.  The
    # same hash-identified inputs must therefore produce the same generation
    # when a governed pipeline stages them under a different private root.
    # Retain every semantic field while replacing machine-specific locators
    # with their logical registry identities for the manifest artifact.
    semantic_spec = json.loads(canonical_json_bytes(spec))
    semantic_spec["roots"] = {
        root_id: f"root-id:{root_id}" for root_id in sorted(roots)
    }
    semantic_provenance = semantic_spec.get("provenance")
    if isinstance(semantic_provenance, dict) and "root" in semantic_provenance:
        semantic_provenance["root"] = "root-id:provenance"
    semantic_spec_bytes = canonical_json_bytes(semantic_spec)
    spec_artifact = SourceArtifact(
        artifact_id="config/portfolio-baseline-semantic-spec.json",
        sha256=hashlib.sha256(semantic_spec_bytes).hexdigest(),
        size_bytes=len(semantic_spec_bytes),
        as_of_date=as_of,
        schema_name="portfolio-baseline-semantic-spec",
        schema_version=str(spec["schema_version"]),
    )
    observed_dates = [_date(item["as_of_date"], "nav.as_of_date") for item in nav]
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-baseline-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="exact configured baseline inputs and explicit fact-level availability dispositions",
            fact_types=(
                "accounts",
                "allocations",
                "assignments",
                "coverage",
                "flows",
                "nav",
                "positions",
                "reconciliation",
                "strategies",
                "transactions",
            ),
            observed_start=min(observed_dates),
            observed_end=max(observed_dates),
        ),
        artifacts=(
            *source_artifacts,
            *([hwm_inputs_artifact] if hwm_inputs_artifact is not None else []),
            *provider_artifacts,
            provenance_artifact,
            spec_artifact,
            catalog_artifact,
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_baseline(spec_path: Path, output_root: Path) -> dict[str, Any]:
    catalog, manifest, catalog_bytes = build_catalog(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "portfolio-baseline.json"
    try:
        descriptor = os.open(
            catalog_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        if catalog_path.is_symlink() or catalog_path.read_bytes() != catalog_bytes:
            raise GenerationConflictError(
                "generation catalog already contains different evidence"
            )
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(catalog_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "account_count": len(catalog["accounts"]),
        "allocation_count": len(catalog["allocations"]),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "flow_count": len(catalog["flows"]),
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),
        "position_count": len(catalog["positions"]),
        "replay_status": catalog["replay"]["status"],
        "strategy_count": len(catalog["strategies"]),
        "transaction_count": len(catalog["transactions"]),
        "unmapped_count": len(catalog["unmapped_records"]),
    }


def publish_baseline(
    spec_path: Path, output_root: Path, generation_id: str
) -> dict[str, Any]:
    evidence = stage_baseline(spec_path, output_root)
    if evidence["generation_id"] != generation_id:
        raise BaselineValidationError(
            "requested generation differs from current inputs"
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
            result = stage_baseline(args.spec, args.output_root)
        else:
            result = publish_baseline(args.spec, args.output_root, args.generation_id)
    except (OSError, ValueError, GenerationConflictError) as exc:
        print(f"portfolio-baseline: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
