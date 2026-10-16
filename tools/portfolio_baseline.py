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
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import (
    Any,
    Callable,
    Collection,
    Final,
    Iterable,
    Mapping,
    NamedTuple,
    Sequence,
)
from zoneinfo import ZoneInfo

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
    AdmissionDisposition,
    CashFlow,
    EvidenceLayer,
    HwmInputsContractError,
    MappingStatus,
    NavObservation,
    ObservedAmount,
    ProviderAdmissionError,
    ProviderFactAdmission,
    Transaction,
    decimal_text,
    hwm_policy_exclusions,
    validate_hwm_inputs_envelope,
    validate_provider_authority,
)


SPEC_SCHEMA_VERSION: Final = 3
SUPPORTED_SPEC_SCHEMA_VERSIONS: Final = frozenset({1, 2, 3})
CATALOG_SCHEMA_VERSION: Final = 1
# A catalog that carries custody-derived cash publishes version 2: each cash line is dated
# by the coverage that proves it and names its last event and its coverage end.
CUSTODY_CASH_CATALOG_SCHEMA_VERSION: Final = 2
BUILDER_VERSION: Final = "1.3.0"
POLICY_VERSION: Final = "PORT-PERF-1"
# The provenance registry binds nine strategy identities (including the retained
# excluded ones).  It is a registry fact, never the count of current strategies:
# `current_capital` makes no count assumption.
RETAINED_REGISTRY_STRATEGY_COUNT: Final = 9
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PROVIDER_LEDGER_CATALOG_SCHEMA_VERSIONS: Final = frozenset({1, 2})
_SCHWAB_CATALOG_SCHEMA_VERSIONS: Final = frozenset({1, 2})
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


def _load_json_decimal(path: Path, field_name: str) -> dict[str, Any]:
    """Parse with float→Decimal for exact monetary reconciliation.

    Scoped to inputs whose money values feed Decimal-exact checks: a global
    parse would leak Decimal into verbatim-copied output sections, which
    canonical_json_bytes cannot serialize."""
    try:
        decoded = json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
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
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    except ValueError as exc:
        raise BaselineValidationError("Schwab provider manifest is invalid") from exc
    if (
        manifest.generation_id != generation_id
        or manifest.verification.value != "VERIFIED"
    ):
        raise BaselineValidationError("Schwab provider generation is not verified")
    catalog = _load_json(catalog_path, "Schwab provider catalog")
    catalog_schema_version = catalog.get("catalog_schema_version")
    if (
        type(catalog_schema_version) is not int
        or catalog_schema_version not in _SCHWAB_CATALOG_SCHEMA_VERSIONS
    ):
        raise BaselineValidationError("Schwab provider catalog schema is unsupported")
    catalog_artifacts = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "normalized/schwab-generation.json"
    ]
    if (
        len(catalog_artifacts) != 1
        or catalog_artifacts[0].sha256 != actual_catalog_hash
        or catalog_artifacts[0].size_bytes != catalog_size
        or catalog_artifacts[0].schema_name != "schwab-generation-catalog"
        or catalog_artifacts[0].schema_version != str(catalog_schema_version)
    ):
        raise BaselineValidationError(
            "Schwab provider catalog manifest binding differs"
        )
    if catalog_schema_version == 2:
        authorized_nav_artifact_ids = {
            artifact.artifact_id
            for artifact in manifest.artifacts
            if artifact.schema_name == "schwab-wch-historical-balance"
        }
        for item in _objects(catalog.get("nav_observations"), "Schwab provider NAV"):
            source_artifact_id = item.get("source_artifact_id")
            if (
                not isinstance(source_artifact_id, str)
                or not source_artifact_id.strip()
            ):
                raise BaselineValidationError(
                    "Schwab provider v2 NAV source artifact is invalid"
                )
            if source_artifact_id not in authorized_nav_artifact_ids:
                raise BaselineValidationError(
                    "Schwab provider v2 NAV source artifact is unauthorized"
                )
            if item.get("value_basis") != "ACCOUNT_VALUE":
                raise BaselineValidationError(
                    "Schwab provider v2 NAV value basis differs"
                )
    provider_as_of = _date(catalog.get("as_of_date"), "Schwab provider as_of_date")
    if (
        manifest.as_of_date != provider_as_of
        or catalog_artifacts[0].as_of_date != provider_as_of
    ):
        raise BaselineValidationError("Schwab provider catalog as-of binding differs")
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
            schema_version=str(catalog_schema_version),
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
    *,
    custody_authority: dict[str, Any] | None = None,
    fx_authority: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[SourceArtifact, ...], list[dict[str, Any]]]:
    ref = _object(value, "provider_ledger")
    _strict_keys(
        ref,
        {"catalog_sha256", "generation_id", "manifest_sha256", "root_id"}
        | ({"financial_mvp"} if "financial_mvp" in ref else set()),
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
    catalog_schema_version = catalog.get("catalog_schema_version")
    if (
        type(catalog_schema_version) is not int
        or catalog_schema_version not in _PROVIDER_LEDGER_CATALOG_SCHEMA_VERSIONS
    ):
        raise BaselineValidationError("provider-ledger catalog schema is unsupported")
    catalog_artifacts = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "normalized/provider-ledger.json"
    ]
    if (
        len(catalog_artifacts) != 1
        or catalog_artifacts[0].sha256 != actual_catalog_hash
        or catalog_artifacts[0].size_bytes != catalog_size
        or catalog_artifacts[0].schema_name != "provider-ledger-catalog"
        or catalog_artifacts[0].schema_version != str(catalog_schema_version)
    ):
        raise BaselineValidationError(
            "provider-ledger catalog manifest binding differs"
        )
    provider_as_of = _date(catalog.get("as_of_date"), "provider-ledger as_of_date")
    if catalog_artifacts[0].as_of_date != provider_as_of:
        raise BaselineValidationError("provider-ledger catalog as-of binding differs")
    if manifest.as_of_date != max(item.as_of_date for item in manifest.artifacts):
        raise BaselineValidationError(
            "provider-ledger generation date is not artifact-derived"
        )
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
    artifacts: tuple[SourceArtifact, ...] = (
        SourceArtifact(
            artifact_id="providers/provider-ledger/generation.json",
            sha256=actual_manifest_hash,
            size_bytes=manifest_size,
            as_of_date=manifest.as_of_date,
            schema_name="generation-manifest",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="providers/provider-ledger/provider-ledger.json",
            sha256=actual_catalog_hash,
            size_bytes=catalog_size,
            as_of_date=provider_as_of,
            schema_name="provider-ledger-catalog",
            schema_version=str(catalog_schema_version),
        ),
    )
    if "financial_mvp" in ref:
        authority, admission, policy = _selected_custody_authority(ref, generation_root)
        if fx_authority is not None:
            admitted_fx = _fx_admission(authority["contract"], catalog)
            if admitted_fx is not None:
                fx_authority.update(admitted_fx)
        approved_dates = [
            _date(row["approved_on"], "owner receipt approved_on")
            for row in policy["owner_decision_receipts"].values()
        ]
        authority_date = max(approved_dates)
        additions = []
        for name in ("contract", "registry", "schema"):
            path = Path(ref["financial_mvp"][f"{name}_path"])
            digest, size = hash_file(path)
            additions.append(
                SourceArtifact(
                    artifact_id=f"authority/financial-mvp/{name}.json",
                    sha256=digest,
                    size_bytes=size,
                    as_of_date=authority_date,
                    schema_name=f"financial-mvp-admitted-{name}",
                    schema_version="1",
                )
            )
        receipt_path = generation_root / "admit-or-declare-receipt.json"
        receipt_digest, receipt_size = hash_file(receipt_path)
        additions.append(
            SourceArtifact(
                artifact_id="authority/provider-admission-receipt.json",
                sha256=receipt_digest,
                size_bytes=receipt_size,
                as_of_date=provider_as_of,
                schema_name="provider-admission-receipt",
                schema_version="1",
            )
        )
        artifacts = (*artifacts, *additions)
        if "provider_custody_perimeter" in policy and custody_authority is not None:
            custody_authority.update(
                {
                    "ledger_generation_id": generation_id,
                    "ledger_catalog_sha256": actual_catalog_hash,
                    "ledger_manifest_sha256": actual_manifest_hash,
                    "admission_receipt_sha256": admission["receipt_sha256"],
                    "contract_raw_sha256": authority["contract_raw_sha256"],
                    "contract_semantic_sha256": authority["contract_semantic_sha256"],
                    "census_id": authority["census_id"],
                    "census_sha256": authority["census_sha256"],
                    "invested_capital_policy_sha256": hashlib.sha256(
                        canonical_json_bytes(policy)
                    ).hexdigest(),
                    # Providers whose one account funds several funds carry no
                    # derived cash line (owner decision, #536); the list is the
                    # approved policy's, bound by the policy digest above.
                    **(
                        {
                            "fund_scoped_providers": sorted(
                                policy["provider_custody_perimeter"][
                                    "fund_scoped_providers"
                                ]
                            )
                        }
                        if "fund_scoped_providers"
                        in policy["provider_custody_perimeter"]
                        else {}
                    ),
                }
            )
    links = [
        {
            "artifact_id": item.artifact_id,
            "as_of_date": item.as_of_date.isoformat(),
            "schema_name": item.schema_name,
            "schema_version": item.schema_version,
            "sha256": item.sha256,
            "source_key": (
                "selected-custody-authority"
                if item.artifact_id.startswith("authority/")
                else "provider-ledger-generation"
            ),
        }
        for item in artifacts
    ]
    return catalog, artifacts, links


_FX_SOURCE_ID: Final = "portfolio-fx-rates"


def _fx_admission(
    contract: Mapping[str, Any], provider_catalog: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The FX generation the admitted contract selects, when the ledger used it.

    The contract's one ``portfolio-fx-rates`` segment must be READY, hold a
    current generation and a dated observed interval, and the selected ledger must
    record that same generation as the one it converted with.  Anything else is
    no admitted FX generation (``None``), which keeps the FX fact FAILED.
    """

    segments = [
        item
        for item in contract.get("source_segments", ())
        if isinstance(item, Mapping) and item.get("source_id") == _FX_SOURCE_ID
    ]
    if len(segments) != 1:
        return None
    segment = segments[0]
    generation = segment.get("current_generation")
    interval = segment.get("interval")
    ledger_fx = provider_catalog.get("portfolio_fx_generation")
    if (
        segment.get("readiness") != "READY"
        or not isinstance(generation, Mapping)
        or not isinstance(interval, Mapping)
        or not isinstance(ledger_fx, Mapping)
    ):
        return None
    generation_id = generation.get("generation_id")
    if (
        not isinstance(generation_id, str)
        or _SHA256.fullmatch(generation_id) is None
        or ledger_fx.get("generation_id") != generation_id
    ):
        return None
    try:
        start = _date(interval.get("observed_start"), "fx observed_start")
        end = _date(interval.get("observed_end"), "fx observed_end")
    except BaselineValidationError:
        return None
    if end < start:
        return None
    return {"generation_id": generation_id, "start": start, "end": end}


def _selected_custody_authority(
    ref: Mapping[str, Any],
    generation_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Admit only the contract actually bound by the selected ledger receipt."""
    from provider_ledger import _financial_mvp_authority
    from portfolio_pipeline_materialize import _provider_admission_authority

    try:
        authority = _financial_mvp_authority({"financial_mvp": ref["financial_mvp"]})
        admission = _provider_admission_authority(
            generation_root,
            authority["contract"],
            contract_raw_sha256=authority["contract_raw_sha256"],
            contract_semantic_sha256=authority["contract_semantic_sha256"],
            census_sha256=authority["census_sha256"],
        )
        for key in ("generation_id", "catalog_sha256", "manifest_sha256"):
            if admission[key] != ref[key]:
                raise BaselineValidationError(
                    "selected custody admission identity differs"
                )
        outcomes = authority["contract"]["financial_outcomes"]
        policy = next(
            row["calculation_contract"]
            for row in outcomes
            if row["outcome_id"] == "INVESTED_CAPITAL"
        )
        decision = policy.get("provider_custody_perimeter")
        if decision is not None:
            from portfolio_financial_mvp_contract import OPTIONAL_OWNER_DECISION_ID

            treatment = (
                "PROVIDER_ACCOUNT_ONLY_EXTERNAL_CAPITAL_DEPOSITS_AND_WITHDRAWALS_ARE_"
                "THE_OWNER_EXTERNAL_FLOWS_AT_EVENT_TIME_FX; SLEEVE_CAPITAL_ANCHORS_ON_"
                "THAT_ACCOUNT_ARE_ALLOCATIONS_INSIDE_THE_STRATEGY_WITH_ZERO_"
                "INVESTED_CAPITAL_EFFECT; UNALLOCATED_ACCOUNT_CASH_IS_AN_EXPLICIT_"
                "STRATEGY_CASH_LINE_NOT_RESERVE_NOT_DROPPED; SLEEVE_NAV_AND_ALLOCATION_"
                "PATH_REMAIN_THE_RETURN_PATH; NEGATIVE_CASH_LINE_FAILS_CLOSED"
            )
            if (
                admission["admission_gate_status"] != "PASS"
                or admission["product_readiness"] != "READY_FOR_COMPOSITION"
            ):
                raise BaselineValidationError(
                    "selected custody source is not READY_FOR_COMPOSITION"
                )
            if policy["policy_status"] != "READY":
                raise BaselineValidationError("selected custody policy is not READY")
            if (
                decision["decision_status"] != "APPROVED"
                or decision["decision_authority"] != "OWNER_318"
                or decision["selected_treatment"] != treatment
                or OPTIONAL_OWNER_DECISION_ID not in policy["owner_decision_receipts"]
            ):
                raise BaselineValidationError(
                    "selected custody decision is not approved treatment"
                )
        return authority, admission, policy
    except (RuntimeError, ValueError, KeyError, StopIteration, OSError) as exc:
        raise BaselineValidationError(f"selected custody authority: {exc}") from exc


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
    if len(strategies) != RETAINED_REGISTRY_STRATEGY_COUNT:
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
        # The published transaction amount is part of the sealed catalog: a retained
        # baseline was written with the product rounded to 28 digits.
        with localcontext() as context:
            context.prec = _SNAPSHOT_PRECISION
            context.rounding = ROUND_HALF_EVEN
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
    position_market_value = _snapshot_sum(
        _decimal(
            _object(item.get("market_value"), "position.market_value").get("value"),
            "position.market_value.value",
        )
        or Decimal("0")
        for item in positions
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


_PROVIDER_ADMISSION_KEYS = {
    "account_id",
    "account_reconciliation_sha256",
    "disposition",
    "effective_end",
    "effective_start",
    "evidence_layer",
    "fact_type",
    "pool_id",
    "provider_id",
    "strategy_id",
}


def _validated_provider_admissions(
    catalog: Mapping[str, Any],
) -> tuple[ProviderFactAdmission, ...]:
    if catalog.get("catalog_schema_version") != 2:
        return ()
    parsed: list[ProviderFactAdmission] = []
    try:
        for item in _objects(catalog.get("admissions"), "provider-ledger.admissions"):
            _strict_keys(item, _PROVIDER_ADMISSION_KEYS, "provider-ledger admission")
            parsed.append(
                ProviderFactAdmission(
                    provider_id=_text(item.get("provider_id"), "admission.provider_id"),
                    fact_type=_text(item.get("fact_type"), "admission.fact_type"),
                    evidence_layer=EvidenceLayer(
                        _text(item.get("evidence_layer"), "admission.evidence_layer")
                    ),
                    disposition=AdmissionDisposition(
                        _text(item.get("disposition"), "admission.disposition")
                    ),
                    effective_start=_date(
                        item.get("effective_start"), "admission.effective_start"
                    ),
                    effective_end=_date(
                        item.get("effective_end"), "admission.effective_end"
                    ),
                    account_id=(
                        None
                        if item.get("account_id") is None
                        else _text(item.get("account_id"), "admission.account_id")
                    ),
                    pool_id=(
                        None
                        if item.get("pool_id") is None
                        else _text(item.get("pool_id"), "admission.pool_id")
                    ),
                    strategy_id=(
                        None
                        if item.get("strategy_id") is None
                        else _text(item.get("strategy_id"), "admission.strategy_id")
                    ),
                    account_reconciliation_sha256=(
                        None
                        if item.get("account_reconciliation_sha256") is None
                        else _text(
                            item.get("account_reconciliation_sha256"),
                            "admission.account_reconciliation_sha256",
                        )
                    ),
                )
            )
        return validate_provider_authority(parsed)
    except BaselineValidationError:
        raise
    except (ProviderAdmissionError, ValueError) as exc:
        raise BaselineValidationError("provider-ledger admission is invalid") from exc


def _admitted_strategy_returns(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _objects(catalog.get("strategy_returns"), "provider-ledger.strategy_returns")
    if catalog.get("catalog_schema_version") != 2:
        return rows

    authoritative: dict[tuple[str, str, str], list[tuple[date, date]]] = defaultdict(
        list
    )
    for admission in _validated_provider_admissions(catalog):
        if (
            admission.fact_type == "strategy_returns"
            and admission.disposition is AdmissionDisposition.AUTHORITATIVE
            and admission.evidence_layer is EvidenceLayer.ACCOUNT_LIVE
            and admission.account_id is not None
            and admission.pool_id is None
            and admission.strategy_id is not None
        ):
            authoritative[
                (admission.provider_id, admission.strategy_id, admission.account_id)
            ].append((admission.effective_start, admission.effective_end))

    accepted: list[dict[str, Any]] = []
    for item in rows:
        if item.get("evidence_layer") != EvidenceLayer.ACCOUNT_LIVE.value:
            continue
        provider_id = _text(item.get("provider_id"), "strategy_returns.provider_id")
        strategy_id = _text(item.get("strategy_id"), "strategy_returns.strategy_id")
        day = _date(item.get("date"), "strategy_returns.date")
        raw_account_ids = item.get("component_account_ids")
        if not isinstance(raw_account_ids, list) or not raw_account_ids:
            raise BaselineValidationError(
                "provider-ledger ACCOUNT_LIVE strategy return lacks component accounts"
            )
        account_ids = [
            _text(account_id, "strategy_returns.component_account_ids")
            for account_id in raw_account_ids
        ]
        if len(account_ids) != len(set(account_ids)):
            raise BaselineValidationError(
                "provider-ledger strategy return component accounts are duplicated"
            )

        def has_authority(account_id: str) -> bool:
            return any(
                start <= day <= end
                for start, end in authoritative.get(
                    (provider_id, strategy_id, account_id), ()
                )
            )

        if all(has_authority(account_id) for account_id in account_ids):
            accepted.append(item)
    return accepted


def _capital_anchor_flow(anchor: Mapping[str, Any]) -> dict[str, Any]:
    """Project one strategy-perimeter capital anchor into a flow fact (D2).

    A ``STRATEGY_CAPITAL_ALLOCATION`` anchor is the only admitted evidence
    that capital entered a Quantbase or Matrixport strategy's NAV perimeter:
    the linked-account deposit that funds it is booked separately, stays
    ``UNMAPPED`` (account-level, outside the strategy perimeter), and is
    never projected here.  The anchor amount is always positive (the ledger
    fails closed rather than admit a non-fee sale as an anchor), so it is a
    genuine external contribution establishing basis in the strategy, not an
    internal transfer paired against a same-day opposite leg.  The caller
    excludes ``OPENING_CAPITAL_ANCHOR`` rows: the opening anchor is the
    performance opening basis (R1), not a funding event, and is read
    directly from the ledger's own ``capital_anchors`` by the manifest
    engine (``parse_capital_anchors``), not through this projection.

    B7 / M9 fail-closed rule: a Matrixport allocation anchor is denominated
    in its native subscription currency (e.g. USDT) unless and until a
    contract-admitted FX conversion has replaced it with a USD amount; it is
    never assumed to be at par with USD.  The caller filters non-USD anchors
    out before calling this function and records an explicit NOT_READY
    finding instead; this function re-asserts the same invariant so no other
    caller can silently mint a mislabeled USD flow from a native-currency
    anchor.
    """

    amount = anchor["amount"]
    if not isinstance(amount, Mapping) or amount.get("currency") != "USD":
        raise BaselineValidationError(
            "capital-anchor flow projection requires a USD amount; a "
            "native-currency anchor (e.g. Matrixport USDT) must never be "
            "treated as USD -- admit it only after a contract-admitted FX "
            "conversion"
        )
    return {
        "account_id": anchor["account_id"],
        "amount": amount,
        "capital_disposition": "EXTERNAL_CAPITAL",
        "evidence_layer": anchor.get("evidence_layer"),
        "event_date": anchor["event_date"],
        "flow_type": "EXTERNAL_CONTRIBUTION",
        "generation_id": anchor.get("generation_id"),
        "mapping_basis": anchor.get("mapping_basis"),
        "mapping_status": anchor["mapping_status"],
        "provider": anchor.get("provider"),
        "provider_id": anchor.get("provider_id"),
        "record_id": f"capital-allocation-flow:{anchor['record_id']}",
        "source_artifact_id": anchor.get("source_artifact_id"),
        "source_key": anchor.get("source_key"),
        "source_record_id": anchor.get("source_record_id"),
        "strategy_id": anchor.get("strategy_id"),
    }


def _usd_nav_value(item: Mapping[str, Any]) -> Decimal | None:
    amount = item.get("nav")
    if (
        not isinstance(amount, Mapping)
        or amount.get("status") != "AVAILABLE"
        or amount.get("currency") != "USD"
    ):
        return None
    return _decimal(amount.get("value"), "nav.value")


def _last_admitted_observation_of_value(
    catalog: Mapping[str, Any],
    account_id: str,
    nav: Sequence[Mapping[str, Any]],
    authoritative_intervals: Mapping[tuple[str, str, str], Sequence[tuple[date, date]]],
    baseline_as_of: date,
) -> Mapping[str, Any] | None:
    """N59: the ledger's last admitted observation of an account's current NAV value.

    A retained-excluded account whose current NAV is a legacy CSV point is dated by
    the frozen ledger's own observations of that value: the ledger rows after the
    legacy date that carry the same USD value without a different value between them
    and fall in an AUTHORITATIVE account-live NAV admission window.  The ledger's
    rows are account-level and may be unmapped to a strategy (the assignment ended),
    so the strategy mapping is not required.  The value is the legacy value; only its
    date moves to the last such observation.  No qualifying row means no change.
    """

    points = [
        item
        for item in nav
        if item.get("account_id") == account_id
        and _usd_nav_value(item) is not None
        and _date(item["as_of_date"], "nav.as_of_date") <= baseline_as_of
    ]
    if not points:
        return None
    current = max(points, key=lambda item: _date(item["as_of_date"], "nav.as_of_date"))
    if current.get("source_key") == "provider-ledger":
        return None
    current_day = _date(current["as_of_date"], "nav.as_of_date")
    current_value = _usd_nav_value(current)
    by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for item in _objects(catalog.get("nav_observations"), "provider-ledger.nav"):
        if item.get("account_id") != account_id or _usd_nav_value(item) is None:
            continue
        day = _date(item.get("as_of_date"), "nav_observations.as_of_date")
        provider_id = _text(item.get("provider_id"), "nav_observations.provider_id")
        if not current_day < day <= baseline_as_of or not any(
            start <= day <= end
            for start, end in authoritative_intervals.get(
                (provider_id, "nav_observations", account_id), ()
            )
        ):
            continue
        by_day[day].append(item)
    carried: Mapping[str, Any] | None = None
    for day in sorted(by_day):
        values = {_usd_nav_value(item) for item in by_day[day]}
        if values != {current_value}:
            break
        carried = by_day[day][0]
    return carried


def _provider_ledger_facts(
    catalog: Mapping[str, Any],
    accounts: Sequence[Mapping[str, Any]],
    legacy_nav: Sequence[Mapping[str, Any]],
    legacy_flows: Sequence[Mapping[str, Any]],
    legacy_transactions: Sequence[Mapping[str, Any]],
    legacy_securities: Sequence[Mapping[str, Any]],
    baseline_as_of: date,
    retained_excluded_accounts: Collection[str] = (),
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
    authoritative_intervals: dict[tuple[str, str, str], list[tuple[date, date]]] = (
        defaultdict(list)
    )
    if catalog.get("catalog_schema_version") == 2:
        accepted_admissions = _validated_provider_admissions(catalog)
        for admission in accepted_admissions:
            if (
                admission.disposition is AdmissionDisposition.AUTHORITATIVE
                and admission.evidence_layer is EvidenceLayer.ACCOUNT_LIVE
                and admission.account_id is not None
                and admission.pool_id is None
                and admission.strategy_id is None
            ):
                authoritative_intervals[
                    (
                        admission.provider_id,
                        admission.fact_type,
                        admission.account_id,
                    )
                ].append((admission.effective_start, admission.effective_end))

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
            fact_date = _date(day_value, f"{name}.date")
            provider_id = _text(item.get("provider_id"), f"{name}.provider_id")
            if catalog.get("catalog_schema_version") == 2 and not any(
                start <= fact_date <= end
                for start, end in authoritative_intervals.get(
                    (provider_id, name, account_id), []
                )
            ):
                continue
            financial_fact_date = fact_date
            if (
                name == "positions"
                and provider_id == "schwab"
                and item.get("observed_at") is not None
            ):
                try:
                    observed_at = datetime.fromisoformat(
                        _text(item.get("observed_at"), "positions.observed_at")
                    )
                except ValueError as exc:
                    raise BaselineValidationError(
                        "Schwab current position clock is invalid"
                    ) from exc
                if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                    raise BaselineValidationError(
                        "Schwab current position clock must be timezone-aware"
                    )
                if observed_at.date() != fact_date:
                    raise BaselineValidationError(
                        "Schwab current position native date differs"
                    )
                # Authority remains bound to the native UTC row date above.  The
                # retained current-point evidence has already bound this clock;
                # only its financial-cutoff comparison uses Schwab's New York day.
                financial_fact_date = observed_at.astimezone(
                    ZoneInfo("America/New_York")
                ).date()
            if financial_fact_date > baseline_as_of:
                raise BaselineValidationError(
                    "provider-ledger fact is newer than baseline"
                )
            record = dict(item)
            record.update(
                {
                    "provider": _text(provider_id, f"{name}.provider_id").upper(),
                    "source_key": "provider-ledger",
                }
            )
            accepted.append(record)
        return accepted

    provider_nav = admitted("nav_observations")
    # capital_anchors postdates catalog_schema_version 1 (no ledger emits it
    # there); its absence means no anchors, not a malformed catalog.
    provider_capital_anchors = (
        admitted("capital_anchors") if "capital_anchors" in catalog else []
    )
    strategy_allocation_flows: list[dict[str, Any]] = []
    capital_anchor_currency_findings: list[dict[str, Any]] = []
    for item in provider_capital_anchors:
        if item.get("capital_type") != "STRATEGY_CAPITAL_ALLOCATION":
            continue
        anchor_currency = _object(item.get("amount"), "capital_anchors.amount").get(
            "currency"
        )
        if anchor_currency != "USD":
            # B7 / M9: a native-currency allocation (Matrixport USDT) has no
            # contract-admitted USD conversion at this layer -- exclude it
            # rather than assume par, and record why explicitly instead of
            # letting it vanish silently.  Admission resumes once the
            # producing ledger generation (T4) supplies a USD amount here.
            capital_anchor_currency_findings.append(
                {
                    "account_in_provenance": True,
                    "currency": anchor_currency,
                    "effective_date": item.get("event_date"),
                    "fact_type": "capital_anchors",
                    "provider_id": item.get("provider_id"),
                    "record_id": item.get("record_id"),
                    "status": "NOT_READY_AWAITING_CONTRACT_FX",
                }
            )
            continue
        strategy_allocation_flows.append(_capital_anchor_flow(item))
    provider_flows = [*admitted("flows"), *strategy_allocation_flows]
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
    for account_id in sorted(retained_excluded_accounts):
        carried = _last_admitted_observation_of_value(
            catalog,
            account_id,
            list(nav_by_key.values()),
            authoritative_intervals,
            baseline_as_of,
        )
        if carried is not None:
            record = dict(carried)
            record.update(
                {
                    "provider": _text(
                        record.get("provider_id"), "nav_observations.provider_id"
                    ).upper(),
                    "source_key": "provider-ledger",
                }
            )
            nav_by_key[(account_id, str(record["as_of_date"]))] = record
            provider_nav.append(record)

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
    quality_findings = [
        *_objects(catalog.get("quality_findings"), "provider-ledger.quality_findings"),
        *capital_anchor_currency_findings,
    ]
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
    if len(by_strategy) != RETAINED_REGISTRY_STRATEGY_COUNT:
        raise BaselineValidationError(
            "allocation snapshot must map exactly 9 strategies"
        )

    coverage: list[dict[str, Any]] = []
    nav_covered_values: list[Decimal] = []
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
            nav_covered_values.append(value)
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
    # The audit snapshot is sealed: these totals are compared with the audit's reported
    # total and with the materializer's own sum, so they are added as the audit added
    # them (see ``_snapshot_sum``), never exactly.
    systematic_total = _snapshot_sum(item[0] for item in by_strategy.values())
    nav_covered = _snapshot_sum(nav_covered_values)
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
    summary = _load_json_decimal(
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
    total = _snapshot_sum([systematic_total, reserve])

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


# ── Current capital (cluster 2, T1; design state/c2-design.md v3, M1/M2) ─────
#
# The allocation table and the audit replay above are one dated snapshot (the
# 2026-08-16 audit, the owner-approved dependence policy input).  The functions
# below publish the CURRENT capital view next to it, from facts only:
#
#   member value   = the member account's latest AVAILABLE USD NAV on or before
#                    the valuation date (each member keeps its own as-of date,
#                    so a provider boundary such as Matrixport's FX cutoff shows
#                    as an older member date, never as a repainted cutoff date)
#   strategy value = the sum of its current members (all members must be valued)
#   weight         = strategy value / the sum over the required strategies
#
# A component with no admitted source is NOT_READY_AT_CUTOFF with a null value.
# A dated audit value is never substituted for it.

_CURRENT_CAPITAL_BASIS: Final = "LATEST_ADMITTED_USD_NAV_ON_OR_BEFORE_VALUATION_DATE"
_CAPITAL_AVAILABLE: Final = "AVAILABLE"
_CAPITAL_NOT_READY: Final = "NOT_READY_AT_CUTOFF"
_SCOPE_SYSTEMATIC: Final = "SYSTEMATIC"
_SCOPE_RETAINED_EXCLUDED: Final = "RETAINED_EXCLUDED"
_RESERVE_POOL_ID: Final = "reserve-capital"
_WEIGHT_PRECISION: Final = 34
_EXACT_PRECISION: Final = 100
_CAPITAL_STRATEGY_CHECK_ID: Final = "strategy-capital-to-cutoff-account-values"
_CAPITAL_IDENTITY_CHECK_ID: Final = "capital-identity-at-cutoff"
_AGGREGATE_COMPONENTS: Final = ("reserve", "retained_excluded", "systematic")


class _Membership(NamedTuple):
    strategy_id: str
    account_id: str
    assignment_id: str
    start: date
    end: date | None


class _NavPoint(NamedTuple):
    day: date
    value: Decimal
    record_id: str | None
    source_key: str | None


def _capital_amount(value: Decimal | None) -> dict[str, Any]:
    if value is None:
        return {"currency": "USD", "status": "UNAVAILABLE", "value": None}
    return {"currency": "USD", "status": "AVAILABLE", "value": decimal_text(value)}


# The audit snapshot (the 2026-08-16 audit) is a sealed, retained artifact: its totals
# were fixed by summing the allocation table in the default 28-digit context, and every
# retained baseline generation replays and verifies against those figures.  A sum that is
# replayed against, or compared with, sealed bytes keeps that arithmetic.  Exact money
# arithmetic (``_exact_sum``) belongs to current capital, whose outputs are not compared
# with a previously sealed generation.
_SNAPSHOT_PRECISION: Final = 28


def _snapshot_sum(values: Iterable[Decimal]) -> Decimal:
    """Add as the sealed audit snapshot did: sequentially at 28 digits, half-even.

    The context is pinned, so the sum no longer follows the caller's precision, and for
    the default context it is the historical ``sum(values, Decimal(0))`` exactly.
    """

    with localcontext() as context:
        context.prec = _SNAPSHOT_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return sum(values, Decimal(0))


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Add money exactly: the default 28-digit context would round long sums."""

    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        return sum(values, Decimal(0))


def _in_window(day: date, start: date | None, end: date | None) -> bool:
    return (start is None or start <= day) and (end is None or day <= end)


def _optional_date(value: object, field_name: str) -> date | None:
    return None if value is None else _date(value, field_name)


def _mapped_memberships(
    assignments: Sequence[Mapping[str, Any]],
) -> list[_Membership]:
    memberships: list[_Membership] = []
    for item in assignments:
        if item.get("mapping_status") != "MAPPED":
            continue
        start_value = item.get("effective_start")
        if start_value is None:
            raise BaselineValidationError(
                "mapped assignment requires an explicit effective_start"
            )
        if item.get("strategy_id") is None:
            raise BaselineValidationError("mapped assignment requires a strategy_id")
        memberships.append(
            _Membership(
                strategy_id=_text(item.get("strategy_id"), "assignment.strategy_id"),
                account_id=_text(item.get("account_id"), "assignment.account_id"),
                assignment_id=_text(
                    item.get("assignment_id"), "assignment.assignment_id"
                ),
                start=_date(start_value, "assignment.effective_start"),
                end=_optional_date(
                    item.get("effective_end"), "assignment.effective_end"
                ),
            )
        )
    return memberships


def _latest_usd_navs(
    nav: Sequence[Mapping[str, Any]],
    valuation_date: date,
    *,
    admitted: Callable[[str, date], bool] | None = None,
) -> dict[str, _NavPoint]:
    """Each account's latest AVAILABLE USD NAV dated on or before the valuation date."""

    latest: dict[str, _NavPoint] = {}
    for item in nav:
        amount = item.get("nav")
        if (
            not isinstance(amount, Mapping)
            or amount.get("status") != "AVAILABLE"
            or amount.get("currency") != "USD"
        ):
            continue
        day = _date(item.get("as_of_date"), "nav.as_of_date")
        if day > valuation_date:
            continue
        account_id = _text(item.get("account_id"), "nav.account_id")
        if admitted is not None and not admitted(account_id, day):
            continue
        value = _decimal(amount.get("value"), "nav.value")
        assert value is not None
        record_id = item.get("record_id")
        source_key = item.get("source_key")
        point = _NavPoint(
            day,
            value,
            record_id if isinstance(record_id, str) else None,
            source_key if isinstance(source_key, str) else None,
        )
        prior = latest.get(account_id)
        if prior is None or day > prior.day:
            latest[account_id] = point
        elif day == prior.day and value != prior.value:
            raise BaselineValidationError("conflicting NAV observation")
    return latest


def _authoritative_nav_windows(
    provider_admissions: Sequence[Mapping[str, Any]],
) -> dict[str, list[tuple[date, date]]]:
    """Effective windows in which an account's NAV is AUTHORITATIVE account-live."""

    windows: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for item in provider_admissions:
        if (
            item.get("fact_type") != "nav_observations"
            or item.get("disposition") != "AUTHORITATIVE"
            or item.get("evidence_layer") != "ACCOUNT_LIVE"
            or item.get("account_id") is None
            or item.get("pool_id") is not None
            or item.get("strategy_id") is not None
        ):
            continue
        windows[_text(item.get("account_id"), "admission.account_id")].append(
            (
                _date(item.get("effective_start"), "admission.effective_start"),
                _date(item.get("effective_end"), "admission.effective_end"),
            )
        )
    return windows


def _reserve_accounts(
    assignments: Sequence[Mapping[str, Any]], valuation_date: date
) -> list[str]:
    accounts: set[str] = set()
    for item in assignments:
        if item.get("pool_id") != _RESERVE_POOL_ID:
            continue
        if _in_window(
            valuation_date,
            _optional_date(item.get("effective_start"), "assignment.effective_start"),
            _optional_date(item.get("effective_end"), "assignment.effective_end"),
        ):
            accounts.add(_text(item.get("account_id"), "assignment.account_id"))
    return sorted(accounts)


def _closed_weights(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Shares of the total at precision 34, with the residual on the last row.

    The last share is ``1 - sum(others)`` computed exactly, so the published
    weights sum to exactly one whatever the division rounded.
    """

    ids = sorted(values)
    total = _exact_sum(values.values())
    weights: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = _WEIGHT_PRECISION
        for strategy_id in ids[:-1]:
            weights[strategy_id] = values[strategy_id] / total
    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        weights[ids[-1]] = Decimal(1) - sum(weights.values(), Decimal(0))
    return weights


def _aggregate(
    status: str,
    value: Decimal | None,
    as_of: date | None,
    components: Sequence[str],
    reason: str | None,
) -> dict[str, Any]:
    return {
        "as_of_date": as_of.isoformat() if as_of is not None else None,
        "components": list(components),
        "current_value": _capital_amount(value),
        "reason": reason,
        "status": status,
    }


def _not_ready(components: Sequence[str], reason: str) -> dict[str, Any]:
    return _aggregate(_CAPITAL_NOT_READY, None, None, components, reason)


def _strategy_members(
    strategy_id: str,
    scope: str,
    valuation_date: date,
    memberships: Sequence[_Membership],
    current_owner: Mapping[str, str],
) -> dict[str, list[_Membership]]:
    """The accounts whose NAVs make up one strategy, with their assignments.

    A required strategy owns the accounts its MAPPED assignments put in effect on
    the valuation date.  A retained-excluded strategy keeps every account it was
    ever MAPPED to (the balance is retained even after the assignment ended), but
    never an account another strategy currently owns.
    """

    members: dict[str, list[_Membership]] = defaultdict(list)
    for item in memberships:
        if item.strategy_id != strategy_id:
            continue
        if scope == _SCOPE_SYSTEMATIC:
            if not _in_window(valuation_date, item.start, item.end):
                continue
        elif current_owner.get(item.account_id, strategy_id) != strategy_id:
            continue
        members[item.account_id].append(item)
    return members


def _contiguous_coverage_end(
    windows: Sequence[tuple[date, date]], earliest_event: date | None
) -> date:
    """End of the merged window segment that holds the earliest event of one fact type.

    Windows join when ``next.start <= previous.end + 1 day``; a day between two
    disjoint windows is attested by nothing, so the larger end is not bridged to.  A
    fact type with no event uses the earliest segment.
    """

    segments: list[list[date]] = []
    for start, end in sorted(windows):
        if segments and start <= segments[-1][1] + timedelta(days=1):
            segments[-1][1] = max(segments[-1][1], end)
        else:
            segments.append([start, end])
    if earliest_event is None:
        return segments[0][1]
    for start, end in segments:
        if start <= earliest_event <= end:
            return end
    raise BaselineValidationError("custody event lacks authoritative account admission")


def _derived_custody_cash(
    provider_catalog: Mapping[str, Any],
    valuation_date: date,
    *,
    legacy: bool = False,
    fund_scoped_providers: Sequence[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Derive separate cash components from the selected raw admitted events.

    A cash line is true through the earlier of the cutoff and the end of every
    AUTHORITATIVE account-live admission it is derived from: the flows it adds and the
    sleeve allocations it subtracts.  Its ``as_of_date`` is that proven-through date, not
    the date of its last event; ``last_event_date`` and the unclamped ``coverage_end`` are
    carried beside it.  ``legacy`` reproduces the version-1 label (the last event date)
    for a catalog sealed before the proof was recorded.
    """
    from portfolio_invested_capital import _source_effect_usd

    flows = _objects(provider_catalog.get("flows"), "custody flows")
    anchors = _objects(
        provider_catalog.get("capital_anchors"), "custody capital anchors"
    )
    identities = [
        _text(row.get("record_id"), "custody record_id") for row in [*flows, *anchors]
    ]
    if len(set(identities)) != len(identities):
        raise BaselineValidationError("custody duplicate source record ID")
    admissions = _validated_provider_admissions(provider_catalog)
    fund_scoped = {str(item).upper() for item in fund_scoped_providers}
    owner_flows = [
        row
        for row in flows
        if row.get("provider_account_scope") == "ACCOUNT_ONLY"
        and row.get("capital_disposition") == "EXTERNAL_CAPITAL"
        # One account funds several funds: its deposits are not the strategy's
        # capital and no derived cash line is built for it.
        and str(row.get("provider_id") or "").upper() not in fund_scoped
    ]
    if any(row.get("flow_type") != "DEPOSIT_OR_WITHDRAWAL" for row in owner_flows):
        raise BaselineValidationError(
            "custody external account flow type is unsupported"
        )
    accounts = sorted(
        {
            (str(row.get("provider_id")), str(row.get("account_id")))
            for row in owner_flows
        }
    )
    components: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for provider_id, account_id in accounts:
        held = [
            row
            for row in anchors
            if row.get("provider_id") == provider_id
            and row.get("account_id") == account_id
            and row.get("mapping_status") == "MAPPED"
            and _date(row.get("event_date"), "custody event date") <= valuation_date
        ]
        strategy_ids = {row.get("strategy_id") for row in held}
        if len(strategy_ids) != 1 or None in strategy_ids:
            raise BaselineValidationError("custody account strategy set is not unique")
        strategy_id = _text(next(iter(strategy_ids)), "custody strategy_id")
        selected = [
            (row, "flows", Decimal(1))
            for row in owner_flows
            if row.get("provider_id") == provider_id
            and row.get("account_id") == account_id
        ]
        selected.extend((row, "capital_anchors", Decimal(-1)) for row in held)
        if any(
            row.get("provider_id") == provider_id
            and row.get("account_id") == account_id
            and str(row.get("flow_type", "")).startswith("TRANSFER:INTERNAL")
            for row in flows
        ):
            raise BaselineValidationError(
                "custody ambiguous internal cash movement lacks admitted sleeve semantics"
            )
        movements: dict[date, list[Decimal]] = defaultdict(list)
        fact_days: dict[str, list[date]] = defaultdict(list)
        event_values: list[tuple[date, Decimal]] = []
        events: list[dict[str, Any]] = []
        for row, fact_type, sign in selected:
            day = _date(row.get("event_date"), "custody event date")
            if day > valuation_date:
                continue
            if row.get("evidence_layer") != "ACCOUNT_LIVE" or not any(
                item.provider_id == provider_id
                and item.account_id == account_id
                and item.fact_type == fact_type
                and item.disposition is AdmissionDisposition.AUTHORITATIVE
                and item.evidence_layer is EvidenceLayer.ACCOUNT_LIVE
                and item.effective_start <= day <= item.effective_end
                for item in admissions
            ):
                raise BaselineValidationError(
                    "custody event lacks authoritative account admission"
                )
            amount = _object(row.get("amount"), "custody amount")
            fx = row.get("event_time_fx")
            if fx is not None:
                fx = _object(fx, "custody event FX")
                if (
                    fx.get("currency") != amount.get("currency")
                    or fx.get("base_currency") != "USD"
                    or fx.get("date") != day.isoformat()
                    or not all(
                        isinstance(fx.get(key), str) and fx[key]
                        for key in (
                            "generation_id",
                            "source_artifact_id",
                            "provider",
                            "field",
                            "timing",
                        )
                    )
                    or (_decimal(fx.get("rate"), "custody FX rate") or Decimal(0)) <= 0
                    or _SHA256.fullmatch(str(fx.get("generation_id"))) is None
                    or (
                        amount.get("currency") == "USD"
                        and _decimal(fx.get("rate"), "USD FX") != 1
                    )
                ):
                    raise BaselineValidationError("custody event FX identity differs")
            try:
                with localcontext() as context:
                    context.prec = 100
                    value = _source_effect_usd(row) * sign
            except (RuntimeError, ValueError) as exc:
                raise BaselineValidationError(f"custody event FX: {exc}") from exc
            movements[day].append(value)
            fact_days[fact_type].append(day)
            event_values.append((day, value))
            events.append(
                {
                    "event_date": day.isoformat(),
                    "record_id": row["record_id"],
                    "event_sha256": hashlib.sha256(
                        canonical_json_bytes(row)
                    ).hexdigest(),
                    "native_currency": amount["currency"],
                    "native_value": amount["value"],
                    "event_time_fx": fx,
                    "signed_usd": decimal_text(value),
                }
            )
        balance = Decimal(0)
        for day in sorted(movements):
            balance = _exact_sum([balance, *movements[day]])
            if balance < 0:
                raise BaselineValidationError(
                    f"custody negative dated closing cash: {account_id} {day}"
                )
        last_event = max(movements)
        coverage_fields: dict[str, str] = {}
        proven_through = last_event
        if not legacy:
            window_ends = []
            for fact_type in ("flows", "capital_anchors"):
                windows = [
                    (item.effective_start, item.effective_end)
                    for item in admissions
                    if item.provider_id == provider_id
                    and item.account_id == account_id
                    and item.fact_type == fact_type
                    and item.disposition is AdmissionDisposition.AUTHORITATIVE
                    and item.evidence_layer is EvidenceLayer.ACCOUNT_LIVE
                ]
                if not windows:
                    raise BaselineValidationError(
                        "custody cash lacks an authoritative admission window"
                    )
                window_ends.append(
                    _contiguous_coverage_end(
                        windows, min(fact_days[fact_type], default=None)
                    )
                )
            coverage_end = min(window_ends)
            proven_through = min(valuation_date, coverage_end)
            # The value is the balance as it stood on the label: an event dated after
            # ``proven_through`` is not attested by every admission, so it is not counted.
            counted = [
                (day, value) for day, value in event_values if day <= proven_through
            ]
            if not counted:
                raise BaselineValidationError(
                    "custody cash has no event inside its proven coverage"
                )
            events = [
                row for row in events if row["event_date"] <= proven_through.isoformat()
            ]
            balance = _exact_sum(value for _, value in counted)
            last_event = max(day for day, _ in counted)
            coverage_fields = {
                "coverage_end": coverage_end.isoformat(),
                "last_event_date": last_event.isoformat(),
            }
        components[strategy_id].append(
            {
                "account_id": account_id,
                "provider_id": provider_id,
                "as_of_date": proven_through.isoformat(),
                "basis": "DERIVED_ADMITTED_EXTERNAL_FLOWS_MINUS_SLEEVE_ALLOCATIONS_PLUS_REDEMPTIONS",
                "status": _CAPITAL_AVAILABLE,
                "current_value": _capital_amount(balance),
                "events": sorted(
                    events, key=lambda row: (row["event_date"], row["record_id"])
                ),
                **coverage_fields,
            }
        )
    return dict(components)


_ACCOUNT_CASH_LINE_KINDS: Final = (
    (
        "DIVIDENDS_PAID_TO_ACCOUNT_CASH_NOT_IN_FUND_NAV",
        "DIVIDEND",
        None,
    ),
    (
        "FEES_PAID_FROM_ACCOUNT_CASH",
        "FEE_JOURNAL:PARTNER_SUB_FEE",
        "ACCOUNT_CASH",
    ),
    (
        "IN_FUND_FEE_FUNDED_SELLS_COST",
        "FEE_FUNDING_SELL",
        None,
    ),
)


def _fund_scoped_account_cash_lines(
    provider_catalog: Mapping[str, Any],
    valuation_date: date,
    fund_scoped_providers: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    """Labelled lines beside a fund-scoped strategy's fund NAV (owner decision, #536).

    Dividends paid to account cash are not in the fund NAV; fees paid from
    account cash and in-fund fee-funded sells are costs.  None of these is part
    of the strategy's current value or capital: they are disclosed so the
    economic result reads NAV plus dividends minus cash-paid fees.  Every line
    is an exact sum of admitted, mapped ledger transactions through the cutoff.
    """

    scoped = {str(item).upper() for item in fund_scoped_providers}
    if not scoped:
        return {}
    rows = [
        row
        for row in _objects(provider_catalog.get("transactions", []), "transactions")
        if str(row.get("provider_id") or "").upper() in scoped
        and row.get("mapping_status") == "MAPPED"
        and row.get("strategy_id") is not None
        and _date(row.get("event_date"), "account cash line date") <= valuation_date
    ]
    identities = [_text(row.get("record_id"), "account cash record_id") for row in rows]
    if len(set(identities)) != len(identities):
        raise BaselineValidationError("account cash duplicate source record ID")
    by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_strategy[str(row["strategy_id"])].append(row)
    result: dict[str, list[dict[str, Any]]] = {}
    for strategy_id, members in sorted(by_strategy.items()):
        lines: list[dict[str, Any]] = []
        for label, kind, funding in _ACCOUNT_CASH_LINE_KINDS:
            selected = [
                row
                for row in members
                if row.get("kind") == kind
                and (funding is None or row.get("funding") == funding)
            ]
            if not selected:
                continue
            amounts = []
            for row in selected:
                amount = _object(row.get("amount"), "account cash amount")
                if (
                    amount.get("status") != "AVAILABLE"
                    or amount.get("currency") != "USD"
                ):
                    raise BaselineValidationError("account cash amount is not USD")
                amounts.append(_decimal(amount.get("value"), "account cash amount"))
            ids = sorted(str(row["record_id"]) for row in selected)
            lines.append(
                {
                    "label": label,
                    "as_of_date": max(
                        _date(row["event_date"], "account cash date")
                        for row in selected
                    ).isoformat(),
                    "current_value": _capital_amount(_exact_sum(amounts)),
                    "event_count": len(selected),
                    "events_sha256": hashlib.sha256(
                        canonical_json_bytes(ids)
                    ).hexdigest(),
                }
            )
        if lines:
            result[strategy_id] = lines
    return result


def _build_current_capital(
    *,
    valuation_date: date,
    strategies: Sequence[Mapping[str, Any]],
    accounts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    provider_admissions: Sequence[Mapping[str, Any]],
    policy_exclusions: Mapping[str, date],
    provider_catalog: Mapping[str, Any] | None = None,
    custody_authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish the current capital view from NAV facts at ``valuation_date``."""

    strategy_ids = sorted(
        {_text(item.get("strategy_id"), "strategy.strategy_id") for item in strategies}
    )
    scopes = {
        strategy_id: (
            _SCOPE_RETAINED_EXCLUDED
            if strategy_id in policy_exclusions
            and policy_exclusions[strategy_id] <= valuation_date
            else _SCOPE_SYSTEMATIC
        )
        for strategy_id in strategy_ids
    }
    memberships = _mapped_memberships(assignments)
    if any(item.strategy_id not in scopes for item in memberships):
        raise BaselineValidationError("assignment references an unregistered strategy")
    current_owner: dict[str, str] = {}
    for item in memberships:
        if _in_window(valuation_date, item.start, item.end):
            prior = current_owner.setdefault(item.account_id, item.strategy_id)
            if prior != item.strategy_id:
                raise BaselineValidationError(
                    "account is a current member of more than one strategy"
                )
    latest = _latest_usd_navs(nav, valuation_date)
    vd = valuation_date.isoformat()

    cash = {}
    if custody_authority:
        if provider_catalog is None:
            raise BaselineValidationError("custody raw provider catalog is missing")
        cash = _derived_custody_cash(
            provider_catalog,
            valuation_date,
            fund_scoped_providers=custody_authority.get("fund_scoped_providers", ()),
        )
        if set(cash) - set(strategy_ids):
            raise BaselineValidationError(
                "custody cash references an unregistered strategy"
            )
        cash_lines = _fund_scoped_account_cash_lines(
            provider_catalog,
            valuation_date,
            custody_authority.get("fund_scoped_providers", ()),
        )
        if set(cash_lines) - set(strategy_ids):
            raise BaselineValidationError(
                "account cash lines reference an unregistered strategy"
            )
    else:
        cash_lines = {}
    rows: list[dict[str, Any]] = []
    claimed: dict[str, str] = {}
    for strategy_id in strategy_ids:
        scope = scopes[strategy_id]
        by_account = _strategy_members(
            strategy_id, scope, valuation_date, memberships, current_owner
        )
        members: list[dict[str, Any]] = []
        values: list[Decimal] = []
        days: list[date] = []
        missing: list[str] = []
        for account_id in sorted(by_account):
            if claimed.setdefault(account_id, strategy_id) != strategy_id:
                raise BaselineValidationError(
                    "account is a member of more than one strategy component"
                )
            held = by_account[account_id]
            point = latest.get(account_id)
            if point is None:
                missing.append(account_id)
            else:
                values.append(point.value)
                days.append(point.day)
            end_days = [item.end for item in held]
            members.append(
                {
                    "account_id": account_id,
                    "as_of_date": point.day.isoformat() if point else None,
                    "assignment_ids": sorted(item.assignment_id for item in held),
                    "effective_end": (
                        None
                        if any(day is None for day in end_days)
                        else max(day for day in end_days if day is not None).isoformat()
                    ),
                    "effective_start": min(item.start for item in held).isoformat(),
                    "nav_record_id": point.record_id if point else None,
                    "source_key": point.source_key if point else None,
                    "status": _CAPITAL_AVAILABLE if point else _CAPITAL_NOT_READY,
                    "value": _capital_amount(point.value if point else None),
                }
            )
        value: Decimal | None = None
        as_of: date | None = None
        reason: str | None = None
        if not members:
            reason = (
                f"no MAPPED account assignment is in effect on {vd}"
                if scope == _SCOPE_SYSTEMATIC
                else "no MAPPED account assignment is retained for this strategy"
            )
        elif missing:
            reason = (
                f"no admitted USD NAV on or before {vd} for member account(s): "
                + ", ".join(missing)
            )
        else:
            value = _exact_sum(
                [
                    *values,
                    *[
                        _decimal(item["current_value"]["value"], "custody cash")
                        or Decimal(0)
                        for item in cash.get(strategy_id, [])
                    ],
                ]
            )
            as_of = min(
                [
                    *days,
                    *[
                        _date(item["as_of_date"], "custody cash as_of_date")
                        for item in cash.get(strategy_id, [])
                    ],
                ]
            )
        rows.append(
            {
                "as_of_date": as_of.isoformat() if as_of is not None else None,
                "current_value": _capital_amount(value),
                "current_weight": None,
                "members": members,
                "reason": reason,
                "scope": scope,
                "status": (
                    _CAPITAL_AVAILABLE if value is not None else _CAPITAL_NOT_READY
                ),
                "strategy_id": strategy_id,
            }
        )
        if strategy_id in cash:
            rows[-1]["derived_cash"] = cash[strategy_id]
        if strategy_id in cash_lines:
            rows[-1]["account_cash_lines"] = cash_lines[strategy_id]

    def component(scope: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["scope"] == scope]

    def total_of(group: Sequence[Mapping[str, Any]]) -> Decimal:
        return _exact_sum(
            _decimal(row["current_value"]["value"], "current_value.value") or Decimal(0)
            for row in group
        )

    def oldest(group: Sequence[Mapping[str, Any]]) -> date | None:
        days = [
            _date(row["as_of_date"], "as_of_date")
            for row in group
            if row["as_of_date"] is not None
        ]
        return min(days) if days else None

    required = component(_SCOPE_SYSTEMATIC)
    required_ids = [row["strategy_id"] for row in required]
    not_ready_required = [
        row["strategy_id"] for row in required if row["status"] != _CAPITAL_AVAILABLE
    ]
    if not required:
        systematic = _not_ready([], "no required strategy is registered")
    elif not_ready_required:
        systematic = _not_ready(
            required_ids,
            "required strategies without a current value: "
            + ", ".join(not_ready_required),
        )
    elif total_of(required) <= 0:
        systematic = _not_ready(
            required_ids, "systematic total is not positive; weights are undefined"
        )
    else:
        systematic_total = total_of(required)
        systematic = _aggregate(
            _CAPITAL_AVAILABLE,
            systematic_total,
            oldest(required),
            required_ids,
            None,
        )
        weights = _closed_weights(
            {
                row["strategy_id"]: _decimal(
                    row["current_value"]["value"], "current_value.value"
                )
                or Decimal(0)
                for row in required
            }
        )
        for row in required:
            row["current_weight"] = decimal_text(weights[row["strategy_id"]])

    excluded = component(_SCOPE_RETAINED_EXCLUDED)
    excluded_ids = [row["strategy_id"] for row in excluded]
    not_ready_excluded = [
        row["strategy_id"] for row in excluded if row["status"] != _CAPITAL_AVAILABLE
    ]
    if not_ready_excluded:
        retained = _not_ready(
            excluded_ids,
            "retained excluded components without an admitted USD NAV on or "
            f"before {vd}: " + ", ".join(not_ready_excluded),
        )
    else:
        retained = _aggregate(
            _CAPITAL_AVAILABLE,
            total_of(excluded),
            oldest(excluded),
            excluded_ids,
            None,
        )

    reserve = _current_reserve(
        valuation_date=valuation_date,
        accounts=accounts,
        assignments=assignments,
        nav=nav,
        provider_admissions=provider_admissions,
        latest=latest,
    )

    parts = {
        "reserve": reserve,
        "retained_excluded": retained,
        "systematic": systematic,
    }
    waiting = sorted(
        name for name, part in parts.items() if part["status"] != _CAPITAL_AVAILABLE
    )
    if waiting:
        total_liquid = _not_ready(
            list(_AGGREGATE_COMPONENTS),
            "total liquid requires systematic, retained excluded and reserve "
            "capital to be current; not current: " + ", ".join(waiting),
        )
    else:
        total_liquid = _aggregate(
            _CAPITAL_AVAILABLE,
            _exact_sum(
                _decimal(part["current_value"]["value"], "current_value.value")
                or Decimal(0)
                for part in parts.values()
            ),
            min(
                _date(part["as_of_date"], "as_of_date")
                for part in parts.values()
                if part["as_of_date"] is not None
            ),
            list(_AGGREGATE_COMPONENTS),
            None,
        )

    return {
        **({"custody_authority": dict(custody_authority)} if custody_authority else {}),
        "basis": _CURRENT_CAPITAL_BASIS,
        "currency": "USD",
        "retained_excluded": retained,
        "reserve": reserve,
        "strategies": sorted(
            rows,
            key=lambda row: (
                row["scope"] != _SCOPE_SYSTEMATIC,
                str(row["strategy_id"]),
            ),
        ),
        "systematic": systematic,
        "total_liquid": total_liquid,
        "valuation_date": vd,
        "weight_basis": "CURRENT_CUTOFF",
    }


def _current_reserve(
    *,
    valuation_date: date,
    accounts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    provider_admissions: Sequence[Mapping[str, Any]],
    latest: Mapping[str, _NavPoint],
) -> dict[str, Any]:
    """Reserve capital, published only when every reserve account is admitted.

    A reserve sleeve is capital authority only through an AUTHORITATIVE
    account-live NAV admission.  Reserve NAVs admitted REFERENCE_ONLY (or present
    only as legacy rows) and accounts with no source at all leave the reserve
    NOT_READY: the dated audit reserve is never a stand-in.
    """

    vd = valuation_date.isoformat()
    reserve_accounts = _reserve_accounts(assignments, valuation_date)
    if not reserve_accounts:
        reserve = _not_ready([], f"no account is assigned to the reserve pool on {vd}")
        reserve["members"] = []
        return reserve
    windows = _authoritative_nav_windows(provider_admissions)

    def admitted(account_id: str, day: date) -> bool:
        return any(start <= day <= end for start, end in windows.get(account_id, ()))

    authoritative = _latest_usd_navs(nav, valuation_date, admitted=admitted)
    provider_by_account = {
        str(item.get("account_id")): item.get("provider") for item in accounts
    }
    members: list[dict[str, Any]] = []
    values: list[Decimal] = []
    days: list[date] = []
    no_source: list[str] = []
    not_authoritative: list[str] = []
    for account_id in reserve_accounts:
        point = authoritative.get(account_id)
        provider = provider_by_account.get(account_id)
        label = str(provider) if provider is not None else account_id
        if point is not None:
            authority = "AUTHORITATIVE"
            values.append(point.value)
            days.append(point.day)
        elif account_id in latest:
            authority = "NAV_NOT_AUTHORITATIVE"
            not_authoritative.append(label)
        else:
            authority = "NO_NAV"
            no_source.append(label)
        members.append(
            {
                "account_id": account_id,
                "as_of_date": point.day.isoformat() if point else None,
                "authority": authority,
                "nav_record_id": point.record_id if point else None,
                "provider": str(provider) if provider is not None else None,
                "status": _CAPITAL_AVAILABLE if point else _CAPITAL_NOT_READY,
                "value": _capital_amount(point.value if point else None),
            }
        )
    if no_source or not_authoritative:
        parts: list[str] = []
        if no_source:
            parts.append("no NAV source: " + ", ".join(sorted(no_source)))
        if not_authoritative:
            parts.append(
                "NAV not authoritative (REFERENCE_ONLY or unadmitted): "
                + ", ".join(sorted(not_authoritative))
            )
        reserve = _not_ready(
            reserve_accounts,
            "reserve accounts without an authoritative admitted NAV on or before "
            f"{vd}: " + "; ".join(parts),
        )
    else:
        reserve = _aggregate(
            _CAPITAL_AVAILABLE,
            _exact_sum(values),
            min(days),
            reserve_accounts,
            None,
        )
    reserve["members"] = members
    return reserve


def _recompute_strategy_capital(
    *,
    scopes: Mapping[str, str],
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    valuation_date: date,
) -> dict[str, tuple[Decimal | None, date | None, tuple[str, ...]]]:
    """Recompute each strategy's capital from ``nav`` x ``assignments`` alone.

    This is deliberately a second, account-first implementation: it never reads
    the published block, only the facts the block claims to sum.  The result per
    strategy is ``(value, oldest member date, member accounts)``, with ``None``
    value/date whenever any member has no admitted USD NAV.
    """

    best: dict[str, tuple[date, Decimal]] = {}
    for item in nav:
        amount = item.get("nav")
        if not (
            isinstance(amount, Mapping)
            and amount.get("status") == "AVAILABLE"
            and amount.get("currency") == "USD"
        ):
            continue
        day = _date(item.get("as_of_date"), "nav.as_of_date")
        if day > valuation_date:
            continue
        account_id = _text(item.get("account_id"), "nav.account_id")
        value = _decimal(amount.get("value"), "nav.value")
        assert value is not None
        if account_id not in best or day > best[account_id][0]:
            best[account_id] = (day, value)

    mapped = _mapped_memberships(assignments)
    owner: dict[str, str] = {}
    for member in mapped:
        if member.start <= valuation_date and (
            member.end is None or valuation_date <= member.end
        ):
            if owner.setdefault(member.account_id, member.strategy_id) != (
                member.strategy_id
            ):
                raise BaselineValidationError(
                    "account is a current member of more than one strategy"
                )
    accounts_of: dict[str, set[str]] = {strategy_id: set() for strategy_id in scopes}
    for member in mapped:
        scope = scopes.get(member.strategy_id)
        if scope is None:
            raise BaselineValidationError(
                "assignment references an unregistered strategy"
            )
        current = member.start <= valuation_date and (
            member.end is None or valuation_date <= member.end
        )
        if scope == _SCOPE_SYSTEMATIC and current:
            accounts_of[member.strategy_id].add(member.account_id)
        elif (
            scope == _SCOPE_RETAINED_EXCLUDED
            and owner.get(member.account_id, member.strategy_id) == member.strategy_id
        ):
            accounts_of[member.strategy_id].add(member.account_id)

    result: dict[str, tuple[Decimal | None, date | None, tuple[str, ...]]] = {}
    for strategy_id, members in accounts_of.items():
        ordered = tuple(sorted(members))
        if not ordered or any(account not in best for account in ordered):
            result[strategy_id] = (None, None, ordered)
            continue
        result[strategy_id] = (
            _exact_sum(best[account][1] for account in ordered),
            min(best[account][0] for account in ordered),
            ordered,
        )
    return result


def _recompute_reserve(
    *,
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    provider_admissions: Sequence[Mapping[str, Any]],
    valuation_date: date,
) -> tuple[Decimal | None, date | None, tuple[str, ...]]:
    accounts = tuple(_reserve_accounts(assignments, valuation_date))
    windows = _authoritative_nav_windows(provider_admissions)
    best: dict[str, tuple[date, Decimal]] = {}
    for item in nav:
        amount = item.get("nav")
        if not (
            isinstance(amount, Mapping)
            and amount.get("status") == "AVAILABLE"
            and amount.get("currency") == "USD"
        ):
            continue
        day = _date(item.get("as_of_date"), "nav.as_of_date")
        account_id = _text(item.get("account_id"), "nav.account_id")
        if day > valuation_date or not any(
            start <= day <= end for start, end in windows.get(account_id, ())
        ):
            continue
        value = _decimal(amount.get("value"), "nav.value")
        assert value is not None
        if account_id not in best or day > best[account_id][0]:
            best[account_id] = (day, value)
    if not accounts or any(account not in best for account in accounts):
        return None, None, accounts
    return (
        _exact_sum(best[account][1] for account in accounts),
        min(best[account][0] for account in accounts),
        accounts,
    )


def _published_amount(value: object, field_name: str) -> Decimal | None:
    """A published amount: ``None`` when UNAVAILABLE, else its exact Decimal."""

    amount = _object(value, field_name)
    if amount.get("currency") != "USD":
        raise BaselineValidationError(f"{field_name} must be USD")
    if amount.get("status") == "UNAVAILABLE":
        if amount.get("value") is not None:
            raise BaselineValidationError(f"{field_name} is unavailable with a value")
        return None
    if amount.get("status") != "AVAILABLE":
        raise BaselineValidationError(f"{field_name} status is invalid")
    return _decimal(amount.get("value"), f"{field_name}.value")


def _current_capital_checks(
    current_capital: Mapping[str, Any],
    *,
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    provider_admissions: Sequence[Mapping[str, Any]],
    provider_catalog: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Reconcile the published block with an independent recompute.

    The checks read the published block as the claim and ``nav`` x
    ``assignments`` (and the reserve admissions) as the evidence.  They can FAIL,
    and a NOT_READY component yields NOT_READY, never PASS.
    """

    valuation_date = _date(
        current_capital.get("valuation_date"), "current_capital.valuation_date"
    )
    try:
        return _reconcile_current_capital(
            current_capital,
            valuation_date,
            assignments,
            nav,
            provider_admissions,
            provider_catalog,
        )
    except BaselineValidationError as exc:
        return [
            {
                "check_id": check_id,
                "error": str(exc),
                "status": "FAIL",
                "valuation_date": valuation_date.isoformat(),
            }
            for check_id in (_CAPITAL_STRATEGY_CHECK_ID, _CAPITAL_IDENTITY_CHECK_ID)
        ]


def _reconcile_current_capital(
    current_capital: Mapping[str, Any],
    valuation_date: date,
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    provider_admissions: Sequence[Mapping[str, Any]],
    provider_catalog: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows = _objects(current_capital.get("strategies"), "current_capital.strategies")
    scopes = {
        _text(row.get("strategy_id"), "strategy_id"): _text(row.get("scope"), "scope")
        for row in rows
    }
    recomputed = _recompute_strategy_capital(
        scopes=scopes,
        assignments=assignments,
        nav=nav,
        valuation_date=valuation_date,
    )
    if current_capital.get("custody_authority"):
        if provider_catalog is None:
            raise BaselineValidationError(
                "custody reconciliation lacks raw provider catalog"
            )
        legacy_cash = any(
            "coverage_end" not in line
            for row in rows
            for line in _objects(row.get("derived_cash", []), "derived cash")
        )
        cash = _derived_custody_cash(
            provider_catalog,
            valuation_date,
            legacy=legacy_cash,
            fund_scoped_providers=current_capital["custody_authority"].get(
                "fund_scoped_providers", ()
            ),
        )
        cash_lines = _fund_scoped_account_cash_lines(
            provider_catalog,
            valuation_date,
            current_capital["custody_authority"].get("fund_scoped_providers", ()),
        )
        for row in rows:
            strategy_id = str(row["strategy_id"])
            expected = cash.get(strategy_id, [])
            if row.get("derived_cash", []) != expected:
                raise BaselineValidationError(
                    "custody cash differs from raw event recompute"
                )
            if row.get("account_cash_lines", []) != cash_lines.get(strategy_id, []):
                raise BaselineValidationError(
                    "account cash lines differ from raw event recompute"
                )
            value, day, accounts = recomputed[strategy_id]
            if value is not None:
                # _recompute_strategy_capital returns value and date together.
                assert day is not None
                value = _exact_sum(
                    [
                        value,
                        *[
                            _decimal(
                                item["current_value"]["value"],
                                "recomputed custody cash",
                            )
                            or Decimal(0)
                            for item in expected
                        ],
                    ]
                )
                day = min(
                    [
                        day,
                        *[
                            _date(item["as_of_date"], "custody date")
                            for item in expected
                        ],
                    ]
                )
                recomputed[strategy_id] = (value, day, accounts)
    elif any(row.get("derived_cash") or row.get("account_cash_lines") for row in rows):
        raise BaselineValidationError("custody cash lacks selected authority")
    capital_check = _strategy_capital_check(rows, scopes, recomputed, valuation_date)
    identity_check = _capital_identity_check(
        current_capital,
        rows,
        scopes,
        recomputed,
        _recompute_reserve(
            assignments=assignments,
            nav=nav,
            provider_admissions=provider_admissions,
            valuation_date=valuation_date,
        ),
        valuation_date,
    )
    return [capital_check, identity_check]


def _strategy_capital_check(
    rows: Sequence[Mapping[str, Any]],
    scopes: Mapping[str, str],
    recomputed: Mapping[str, tuple[Decimal | None, date | None, tuple[str, ...]]],
    valuation_date: date,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    not_ready_ids: list[str] = []
    for row in rows:
        strategy_id = str(row["strategy_id"])
        value, as_of, accounts = recomputed[strategy_id]
        published = _published_amount(row.get("current_value"), "current_value")
        published_as_of = _optional_date(row.get("as_of_date"), "as_of_date")
        published_accounts = tuple(
            sorted(
                _text(member.get("account_id"), "member.account_id")
                for member in _objects(row.get("members"), "members")
            )
        )
        if row.get("status") != (
            _CAPITAL_AVAILABLE if published is not None else _CAPITAL_NOT_READY
        ):
            mismatches.append({"reason": "status", "strategy_id": strategy_id})
        elif (
            published != value
            or published_as_of != as_of
            or published_accounts != accounts
        ):
            mismatches.append(
                {
                    "published_as_of_date": (
                        published_as_of.isoformat() if published_as_of else None
                    ),
                    "published_usd": (
                        decimal_text(published) if published is not None else None
                    ),
                    "reason": "value",
                    "recomputed_as_of_date": as_of.isoformat() if as_of else None,
                    "recomputed_usd": (
                        decimal_text(value) if value is not None else None
                    ),
                    "strategy_id": strategy_id,
                }
            )
        if scopes[strategy_id] == _SCOPE_SYSTEMATIC and (
            value is None
            or any(
                "coverage_end" in line
                and _date(line.get("as_of_date"), "derived_cash.as_of_date")
                < valuation_date
                for line in _objects(row.get("derived_cash", []), "derived cash")
            )
        ):
            # A cash line proven only to an earlier date states the row at that date, not
            # at the cutoff; this check recomputes with the producer's own logic, so it
            # must not echo the row as a cutoff-dated PASS (N56 rule 4: never passes).
            not_ready_ids.append(strategy_id)
    check: dict[str, Any] = {
        "check_id": _CAPITAL_STRATEGY_CHECK_ID,
        "matched_count": len(rows) - len(mismatches),
        "mismatches": mismatches,
        "not_ready_strategy_ids": sorted(not_ready_ids),
        "status": "FAIL" if mismatches else "NOT_READY" if not_ready_ids else "PASS",
        "strategy_count": len(rows),
        "valuation_date": valuation_date.isoformat(),
    }
    required = [
        recomputed[strategy_id][0]
        for strategy_id in recomputed
        if scopes[strategy_id] == _SCOPE_SYSTEMATIC
    ]
    if check["status"] == "PASS" and required:  # no row is dated before the cutoff
        check["systematic_total_usd"] = decimal_text(
            _exact_sum(value for value in required if value is not None)
        )
    return check


def _expected_aggregates(
    scopes: Mapping[str, str],
    recomputed: Mapping[str, tuple[Decimal | None, date | None, tuple[str, ...]]],
    reserve: tuple[Decimal | None, date | None, tuple[str, ...]],
) -> dict[str, tuple[Decimal | None, date | None]]:
    """What each aggregate must be, given the recomputed members."""

    def settled(
        scope: str,
    ) -> list[tuple[Decimal | None, date | None, tuple[str, ...]]]:
        return [recomputed[sid] for sid in sorted(recomputed) if scopes[sid] == scope]

    def total(
        group: Sequence[tuple[Decimal | None, date | None, tuple[str, ...]]],
    ) -> tuple[Decimal | None, date | None]:
        if any(value is None for value, _, _ in group):
            return None, None
        days = [day for _, day, _ in group if day is not None]
        return (
            _exact_sum(value for value, _, _ in group if value is not None),
            min(days) if days else None,
        )

    required = settled(_SCOPE_SYSTEMATIC)
    systematic = total(required) if required else (None, None)
    if systematic[0] is not None and systematic[0] <= 0:
        systematic = (None, None)
    expected: dict[str, tuple[Decimal | None, date | None]] = {
        "reserve": (reserve[0], reserve[1]),
        "retained_excluded": total(settled(_SCOPE_RETAINED_EXCLUDED)),
        "systematic": systematic,
    }
    if all(value is not None for value, _ in expected.values()):
        days = [day for _, day in expected.values() if day is not None]
        expected["total_liquid"] = (
            _exact_sum(value for value, _ in expected.values() if value is not None),
            min(days),
        )
    else:
        expected["total_liquid"] = (None, None)
    return expected


def _capital_identity_check(
    current_capital: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    scopes: Mapping[str, str],
    recomputed: Mapping[str, tuple[Decimal | None, date | None, tuple[str, ...]]],
    reserve: tuple[Decimal | None, date | None, tuple[str, ...]],
    valuation_date: date,
) -> dict[str, Any]:
    expected = _expected_aggregates(scopes, recomputed, reserve)
    mismatches: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    for name in (*_AGGREGATE_COMPONENTS, "total_liquid"):
        part = _object(current_capital.get(name), f"current_capital.{name}")
        published = _published_amount(
            part.get("current_value"), f"{name}.current_value"
        )
        published_day = _optional_date(part.get("as_of_date"), f"{name}.as_of_date")
        want, want_day = expected[name]
        statuses[name] = str(part.get("status"))
        if (
            part.get("status")
            != (_CAPITAL_AVAILABLE if published is not None else _CAPITAL_NOT_READY)
            or published != want
            or published_day != want_day
        ):
            mismatches.append(
                {
                    "component": name,
                    "published_as_of_date": (
                        published_day.isoformat() if published_day else None
                    ),
                    "published_usd": (
                        decimal_text(published) if published is not None else None
                    ),
                    "recomputed_as_of_date": (
                        want_day.isoformat() if want_day else None
                    ),
                    "recomputed_usd": decimal_text(want) if want is not None else None,
                }
            )
    # Shares: every required strategy's value over the systematic total, closing
    # to exactly one; none while the systematic total is not current.
    published_weights = {
        str(row["strategy_id"]): row.get("current_weight")
        for row in rows
        if scopes[str(row["strategy_id"])] == _SCOPE_SYSTEMATIC
    }
    wanted: dict[str, str | None] = {}
    if expected["systematic"][0] is not None:
        for strategy_id, share in _closed_weights(
            {
                strategy_id: value
                for strategy_id, (value, _, _) in recomputed.items()
                if scopes[strategy_id] == _SCOPE_SYSTEMATIC and value is not None
            }
        ).items():
            wanted[strategy_id] = decimal_text(share)
    else:
        wanted = dict.fromkeys(published_weights)
    weighted_outside = [
        str(row["strategy_id"])
        for row in rows
        if scopes[str(row["strategy_id"])] != _SCOPE_SYSTEMATIC
        and row.get("current_weight") is not None
    ]
    if published_weights != wanted or weighted_outside:
        mismatches.append({"component": "current_weight"})
    not_ready = sorted(
        name for name, status in statuses.items() if status != _CAPITAL_AVAILABLE
    )
    check: dict[str, Any] = {
        "check_id": _CAPITAL_IDENTITY_CHECK_ID,
        "components": dict(sorted(statuses.items())),
        "mismatches": mismatches,
        "not_ready_components": not_ready,
        "status": "FAIL" if mismatches else "NOT_READY" if not_ready else "PASS",
        "valuation_date": valuation_date.isoformat(),
    }
    total_liquid = expected["total_liquid"][0]
    if check["status"] == "PASS" and total_liquid is not None:
        check["total_liquid_usd"] = decimal_text(total_liquid)
    return check


def _build_audit_snapshot(
    *,
    as_of_date: date,
    replay: Mapping[str, Any],
    systematic_total: Decimal,
    reserve_value: Decimal,
    reserve_source_date: date,
    assignments: Sequence[Mapping[str, Any]],
    nav: Sequence[Mapping[str, Any]],
    reference_flows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The dated audit snapshot, labeled so no consumer reads it as current.

    It carries the audit replay and its two identity checks (moved out of the
    current reconciliations) and records the audit reserve with the dates its
    components were observed, plus the reserve-account flows booked after those
    dates, so the staleness of the audit reserve is visible instead of implied.
    """

    latest = _latest_usd_navs(nav, as_of_date)
    observations: list[dict[str, str]] = []
    without_observation: list[str] = []
    observed_days: dict[str, date] = {}
    for account_id in _reserve_accounts(assignments, as_of_date):
        point = latest.get(account_id)
        if point is None:
            without_observation.append(account_id)
            continue
        observed_days[account_id] = point.day
        observations.append(
            {
                "account_id": account_id,
                "as_of_date": point.day.isoformat(),
                "value_usd": decimal_text(point.value),
            }
        )
    observed_total = _exact_sum(Decimal(row["value_usd"]) for row in observations)
    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        difference = reserve_value - observed_total
    later_flows: list[dict[str, Any]] = []
    for flow in reference_flows:
        flow_account = flow.get("account_id")
        if not isinstance(flow_account, str) or flow_account not in observed_days:
            continue
        event_date = _date(flow.get("event_date"), "flow.event_date")
        if not observed_days[flow_account] < event_date <= as_of_date:
            continue
        amount = flow.get("amount")
        later_flows.append(
            {
                "account_id": flow_account,
                "amount": dict(amount) if isinstance(amount, Mapping) else None,
                "event_date": event_date.isoformat(),
                "flow_type": flow.get("flow_type"),
                "record_id": flow.get("record_id"),
            }
        )
    later_flows.sort(key=lambda row: (row["event_date"], str(row["record_id"] or "")))
    return {
        "allocation_source_keys": {
            "reserve": "current-portfolio-summary",
            "systematic": "component-decision-table",
        },
        "as_of_date": as_of_date.isoformat(),
        "checks": [
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
        ],
        "current_view": "current_capital",
        "replay": dict(replay),
        "reserve": {
            "account_observations": observations,
            "account_observations_total_usd": decimal_text(observed_total),
            "accounts_without_observation": without_observation,
            "post_observation_flows": later_flows,
            "source_effective_date": reserve_source_date.isoformat(),
            "source_key": "current-portfolio-summary",
            "unexplained_difference_usd": decimal_text(difference),
            "value_usd": decimal_text(reserve_value),
        },
        "status": "HISTORICAL_REFERENCE",
        "systematic_total_usd": decimal_text(systematic_total),
        "total_liquid_usd": replay["total_liquid_usd"],
    }


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
    fx_admission: Mapping[str, Any] | None = None,
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
    affected_text = (
        f"{len(non_usd_records)} non-USD transaction/flow records "
        f"({', '.join(non_usd_currencies) or 'no currencies'}) are FX-affected, "
        f"including {len(unavailable_non_usd)} with unavailable accepted USD amounts"
    )
    affected_counts = {
        "fx_affected_record_count": len(non_usd_records),
        "unavailable_usd_amount_count": len(unavailable_non_usd),
        "currencies": non_usd_currencies,
    }
    fx_dates = [
        _date(item.get("event_date"), "fx affected event_date")
        for item in non_usd_records
        if item.get("event_date") is not None
    ]
    admitted_start: date | None = fx_admission["start"] if fx_admission else None
    admitted_end: date | None = fx_admission["end"] if fx_admission else None
    window_start = min(fx_dates) if fx_dates else admitted_start
    # An admitted generation covers the window when it overlaps it; FX is FAILED
    # only when no admitted generation does.  The rest of the window, and every
    # record the generation has no series for, is a disclosed gap.
    if (
        fx_admission is not None
        and admitted_start is not None
        and admitted_end is not None
        and window_start is not None
        and admitted_start <= as_of_date
        and admitted_end >= window_start
    ):
        generation = str(fx_admission["generation_id"])[:8]
        fx_gaps: list[str] = []
        if non_usd_records:
            fx_gaps.append(
                "no independent FX series covers the unconverted records in the "
                f"admitted generation {generation}; {affected_text}; "
                "account NAV, current positions, allocations, and HWM current "
                "values remain provider-reported USD authority"
            )
        if window_start < admitted_start:
            fx_gaps.append(
                f"admitted FX generation {generation} starts "
                f"{admitted_start.isoformat()}; FX-affected records from "
                f"{window_start.isoformat()} precede it"
            )
        if admitted_end < as_of_date:
            fx_gaps.append(
                f"admitted FX generation {generation} ends "
                f"{admitted_end.isoformat()}; dates through "
                f"{as_of_date.isoformat()} after it carry no FX rate"
            )
        fx_claim["status"] = (
            "PARTIAL" if fx_gaps else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        )
        fx_claim["known_gaps"] = fx_gaps
        fx_claim["disposition"] = {
            "authority": "contract-admitted portfolio-fx-rates generation",
            "code": (
                "ADMITTED_FX_GENERATION_PARTIAL_COVERAGE"
                if fx_gaps
                else "ADMITTED_FX_GENERATION_COVERS_WINDOW"
            ),
            "effect": "Records the generation covers are converted at their own event date; unconverted records and dates outside the generation stay unavailable.",
            "generation_id": fx_admission["generation_id"],
            "admitted_start": admitted_start.isoformat(),
            "admitted_end": admitted_end.isoformat(),
            **affected_counts,
        }
    else:
        fx_claim["known_gaps"] = [
            f"no accepted independent FX observation source exists; {affected_text}; "
            "account NAV, current positions, allocations, and HWM current values "
            "remain provider-reported USD authority"
        ]
        fx_claim["disposition"] = {
            "authority": "provider-reported USD account facts; no independent FX series",
            "code": "RETAINED_NO_ACCEPTED_FX_SOURCE",
            "effect": "Independent non-USD transaction/flow recomputation is unavailable; provider-reported USD current values are not retranslated.",
            **affected_counts,
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


def _retained_excluded_accounts(
    assignments: Sequence[Mapping[str, Any]], valuation_date: date
) -> set[str]:
    """Accounts a retained-excluded strategy keeps, as ``_strategy_members`` selects."""

    excluded = {
        strategy_id
        for strategy_id, effective in _hwm_policy_exclusions().items()
        if effective <= valuation_date
    }
    memberships = _mapped_memberships(assignments)
    owned_elsewhere = {
        item.account_id
        for item in memberships
        if item.strategy_id not in excluded
        and _in_window(valuation_date, item.start, item.end)
    }
    return {
        item.account_id
        for item in memberships
        if item.strategy_id in excluded and item.account_id not in owned_elsewhere
    }


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
    # Every provider-ledger flow, admitted or not: used only to annotate the dated
    # audit reserve with what moved after it was observed, never as a capital fact.
    reference_flows: list[dict[str, Any]] = []
    custody_authority: dict[str, Any] = {}
    fx_authority: dict[str, Any] = {}
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
            spec["provider_ledger"],
            roots,
            as_of,
            custody_authority=custody_authority,
            fx_authority=fx_authority,
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
            retained_excluded_accounts=_retained_excluded_accounts(assignments, as_of),
        )
        provider_admissions = _objects(
            provider_catalog.get("admissions"), "provider-ledger.admissions"
        )
        reference_flows = _objects(
            provider_catalog.get("flows"), "provider-ledger.flows"
        )
        provider_coverage = _objects(
            provider_catalog.get("provider_coverage"),
            "provider-ledger.provider_coverage",
        )
        strategy_provider_evidence = _objects(
            provider_catalog.get("strategy_evidence"),
            "provider-ledger.strategy_evidence",
        )
        strategy_returns = _admitted_strategy_returns(provider_catalog)
        provider_strategy_returns = _objects(
            provider_catalog.get("provider_strategy_returns"),
            "provider-ledger.provider_strategy_returns",
        )
        grouped_unmapped: dict[tuple[str, str, str], int] = {}
        for item in raw_provider_unmapped:
            key = (
                _text(item.get("provider_id"), "quality provider_id"),
                _text(item.get("fact_type"), "quality fact_type"),
                # Most quality findings are a genuine account/strategy
                # mapping miss ("UNMAPPED"); a few (e.g. a native-currency
                # capital anchor awaiting contract FX, D2/B7) carry their own
                # more specific status and must not be relabeled UNMAPPED.
                _text(item.get("status") or "UNMAPPED", "quality status"),
            )
            grouped_unmapped[key] = grouped_unmapped.get(key, 0) + 1
        provider_unmapped = [
            {
                "fact_type": fact_type,
                "provider_id": provider_id,
                "record_count": count,
                "source_key": "provider-ledger",
                "status": status,
            }
            for (provider_id, fact_type, status), count in sorted(
                grouped_unmapped.items()
            )
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
        (
            hwm_inputs_envelope,
            hwm_inputs_artifact,
            hwm_inputs_link,
            hwm_acceptance_record,
        ) = hwm_inputs_result
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
    # The allocation table above is the dated audit snapshot and stays as it was
    # (dependence and attribution consume it unchanged).  The current capital view
    # is built from NAV facts, reconciled by an independent recompute, and a
    # failed reconciliation is never published.
    current_capital = _build_current_capital(
        valuation_date=as_of,
        strategies=strategies,
        accounts=accounts,
        assignments=assignments,
        nav=nav,
        provider_admissions=provider_admissions,
        policy_exclusions=_hwm_policy_exclusions(),
        provider_catalog=provider_catalog if custody_authority else None,
        custody_authority=custody_authority or None,
    )
    current_checks = _current_capital_checks(
        current_capital,
        assignments=assignments,
        nav=nav,
        provider_admissions=provider_admissions,
        provider_catalog=provider_catalog if custody_authority else None,
    )
    if any(check["status"] == "FAIL" for check in current_checks):
        raise BaselineValidationError(
            "current capital does not reconcile to cutoff account values"
        )
    audit_snapshot = _build_audit_snapshot(
        as_of_date=source_dates["component-decision-table"],
        replay=replay,
        systematic_total=systematic_total,
        reserve_value=reserve,
        reserve_source_date=source_dates["current-portfolio-summary"],
        assignments=assignments,
        nav=nav,
        reference_flows=reference_flows,
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
        fx_admission=fx_authority or None,
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
        "audit_snapshot": audit_snapshot,
        "baseline_schema_version": (
            CUSTODY_CASH_CATALOG_SCHEMA_VERSION
            if custody_authority
            else CATALOG_SCHEMA_VERSION
        ),
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
        "current_capital": current_capital,
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
            *current_checks,
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
    version_catalog = provenance.get("strategy_version_catalog")
    if version_catalog is not None:
        # The catalog's as_of_date is a certification date: the provenance producer requires
        # every strategy version to be in force at it, and downstream it decides hybrid
        # eligibility rather than labelling anything. Copying a catalog certified for an
        # earlier date into a later baseline therefore carries an assumption, not evidence.
        # The report refuses that, but three stages downstream -- so it is refused here,
        # where the baseline that would carry it is being built.
        certified_at = version_catalog.get("as_of_date")
        if certified_at != catalog.get("as_of_date"):
            raise BaselineValidationError(
                "strategy version catalog certifies "
                f"{certified_at} but the baseline is dated {catalog.get('as_of_date')}; "
                "re-certify the provenance generation at this cutoff"
            )
        catalog["strategy_version_catalog"] = version_catalog
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_artifact = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=as_of,
        schema_name="portfolio-baseline-catalog",
        schema_version=str(catalog["baseline_schema_version"]),
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
    selected_financial = semantic_spec.get("provider_ledger", {}).get("financial_mvp")
    if isinstance(selected_financial, dict):
        for name in ("contract", "registry", "schema"):
            selected_financial[f"{name}_path"] = f"authority-id:financial-mvp-{name}"
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
        as_of_date=max(
            item.as_of_date
            for item in (
                *source_artifacts,
                *([hwm_inputs_artifact] if hwm_inputs_artifact is not None else []),
                *provider_artifacts,
                provenance_artifact,
                spec_artifact,
                catalog_artifact,
            )
        ),
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
