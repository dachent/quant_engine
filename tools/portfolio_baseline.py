"""Build and publish the first private, coverage-aware portfolio baseline.

The repository owns this deterministic builder and its schemas.  The private
specification, source records, normalized catalog, and CURRENT pointer remain
outside Git.  Every input is an explicit, hash-bound file; this tool never
walks a directory or calls a provider.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
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
    MappingStatus,
    NavObservation,
    ObservedAmount,
    Transaction,
    decimal_text,
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
    if set(values) != _SOURCE_KEYS:
        raise BaselineValidationError(
            f"source keys differ: missing={sorted(_SOURCE_KEYS - set(values))}, "
            f"unexpected={sorted(set(values) - _SOURCE_KEYS)}"
        )
    paths: dict[str, Path] = {}
    artifacts: list[SourceArtifact] = []
    links: list[dict[str, Any]] = []
    for source_key in sorted(values):
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
    if catalog.get("catalog_schema_version") != 1:
        raise BaselineValidationError("provenance catalog schema is unsupported")
    artifact = SourceArtifact(
        artifact_id="provenance/portfolio-provenance.json",
        sha256=actual_hash,
        size_bytes=size,
        as_of_date=_date(catalog.get("as_of_date"), "provenance.as_of_date"),
        schema_name="portfolio-provenance-catalog",
        schema_version="1",
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
        },
        {
            "fact_type": "nav",
            "known_gaps": [
                f"{missing_strategy_nav_count} current strategies lack account-specific daily series"
            ],
            "status": "PARTIAL",
        },
        {
            "fact_type": "flows",
            "known_gaps": [
                "coverage is limited to accepted Schwab, IBKR, and Alpaca evidence"
            ],
            "status": "PARTIAL",
        },
        {
            "fact_type": "transactions",
            "known_gaps": [
                "coverage is limited to accepted Alpaca fills and Schwab history"
            ],
            "status": "PARTIAL",
        },
        {
            "fact_type": "positions",
            "known_gaps": (
                list(schwab_known_gaps)
                if positions
                else ["no accepted position source exists for this baseline"]
            ),
            "status": "PARTIAL" if positions else "FAILED",
        },
        {
            "fact_type": "fx",
            "known_gaps": [
                "no accepted FX observation source exists for this baseline"
            ],
            "status": "FAILED",
        },
        {
            "fact_type": "costs",
            "known_gaps": [
                "several current strategy costs remain unknown or conditional"
            ],
            "status": "PARTIAL",
        },
    ]
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
            "status": "PARTIAL" if positions else "UNAVAILABLE",
        },
        {
            "exception_id": "full-systematic-hwm-unavailable",
            "fact_type": "nav",
            "severity": "BLOCKING_FOR_AUTOMATED_HWM",
            "status": "UNAVAILABLE",
        },
        {
            "exception_id": "current-strategy-attribution-partial",
            "fact_type": "current_strategy_attribution",
            "record_count": len(current_unmapped),
            "severity": "BLOCKING_FOR_FULL_ATTRIBUTION",
            "status": "PARTIAL" if current_unmapped else "PASS",
        },
        {
            "exception_id": "historical-strategy-attribution-unresolved",
            "fact_type": "historical_strategy_attribution",
            "record_count": len(historical_unmapped),
            "severity": "DISCLOSED_HISTORICAL_GAP",
            "status": "PARTIAL" if historical_unmapped else "PASS",
        },
        {
            "exception_id": "account-nav-coverage-partial",
            "fact_type": "nav",
            "investment_account_count": investment_count,
            "severity": "BLOCKING_FOR_FULL_ATTRIBUTION",
            "status": "PARTIAL",
        },
        {
            "exception_id": "transaction-coverage-partial",
            "fact_type": "transactions",
            "record_count": len(transactions),
            "severity": "DISCLOSED_GAP",
            "status": "PARTIAL",
        },
        {
            "exception_id": "flow-coverage-partial",
            "fact_type": "flows",
            "record_count": len(flows),
            "severity": "DISCLOSED_GAP",
            "status": "PARTIAL",
        },
    ]
    return claims, exceptions


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
        "flows": flows,
        "implementation_gaps": implementation_gaps,
        "nav_observations": nav,
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
        "source_artifacts": [*source_links, *provider_links],
        "strategies": strategies,
        "strategy_live_metrics": live_metrics,
        "strategy_nav_coverage": strategy_nav_coverage,
        "strategy_provider_evidence": strategy_provider_evidence,
        "strategy_returns": strategy_returns,
        "transactions": transactions,
        "unmapped_records": unmapped,
    }
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
