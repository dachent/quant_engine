"""Normalize provider-custodied strategy NAV into governed target returns.

The first vertical slice accepts an immutable Matrixport generation. The
roll-forward path also accepts published Quantbase and Portfolio123
generations. It adds the Quantbase-custodied Volatis account path, preserves
the separate Portfolio123 provider-model and IBKR account-live paths, and
emits explicit overlap reconciliation. No provider call or credential access
is performed.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
import hashlib
import importlib.util
import inspect
import io
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core.contracts import (
    GenerationManifest,
    ManifestValidationError,
    VerificationStatus,
    canonical_json_bytes,
)


TARGET_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
)
MATRIXPORT_NAV_FIELDS: Final = frozenset({"nav", "product_id", "timestamp_utc"})
MATRIXPORT_HOLDING_FIELDS: Final = frozenset({"product_id", "shares"})
MATRIXPORT_DAILY_HOLDING_FIELDS: Final = frozenset(
    {"nav", "nav_available", "product_id", "share", "utc_date"}
)
MATRIXPORT_SUBSCRIPTION_FIELDS: Final = frozenset(
    {
        "confirm_nav_time_utc",
        "product_id",
        "status",
        "subscription_share",
        "subscription_time_utc",
    }
)
QUANTBASE_HISTORY_FIELDS: Final = frozenset(
    {"time", "value", "cost_basis", "profit_loss"}
)
QUANTBASE_ACTIVITY_FIELDS: Final = frozenset(
    {"activity_type", "amount", "fund", "is_rebalance", "time", "type"}
)
CORRELATION_FIELDS: Final = ("series_id", "date", "return", "evidence_layer")
CORRELATION_FIELDS_V2: Final = (*CORRELATION_FIELDS, "strategy_version")
STRATEGY_HISTORY_MANIFEST_SCHEMA_VERSION: Final = 1
STRATEGY_HISTORY_BUILDER_VERSIONS: Final = frozenset({"1.2.0", "1.3.0", "1.4.0"})
MATRIXPORT_TARGET_BUILDER: Final = "portfolio-target-series/matrixport-4"
LAYERED_HISTORY_BUILDER: Final = "portfolio-target-series/layered-history-1"
LAYERED_HISTORY_BUILDER_VERSION: Final = "1.14.0"
LAYERED_HISTORY_SCHEMA_VERSION: Final = 1
LAYERED_HISTORY_ASSURANCE_PROFILES: Final = frozenset(
    {"DEVELOPMENT", "WORKSTATION_QC", "RELEASE_QC"}
)
LAYERED_HISTORY_MODE_POLICY: Final = {
    "LIVE_ONLY": "SELECTED_ACCOUNT_LIVE_ONLY",
    "LIVE_PLUS_BACKTEST": "EXACT_VERSION_BACKTEST_STRICTLY_BEFORE_ACCOUNT_LIVE_THEN_ACCOUNT_LIVE",
    "comparison_layers_in_modes": False,
    "post_inception_backtest_fill": False,
}
LAYERED_HISTORY_CORRECTION_RESOLUTION_IDS: Final = frozenset(
    {
        "echotrade-event-only-account-live-rejected",
        "p123-ai-backtest",
        "p123-eu-provider-live",
        "robuxio-account-live-after-funding",
        "volatis-account-live",
    }
)
MATRIXPORT_GAP_KINDS: Final = (
    "nav_missing_date",
    "fx_missing_date",
    "reconciliation_incomplete_date",
)
MATRIXPORT_RETURN_TRANSFORM_CAPABILITIES: Final = {
    ("matrixport-declared-gap-one-day-returns", "1"): frozenset(
        {
            "ACCOUNT_LIVE_DAILY_RETURN",
            "PROVIDER_LIVE_DAILY_RETURN",
            "DECLARED_GAP_COVERAGE",
        }
    ),
    ("matrixport-declared-gap-span-returns", "1"): frozenset(
        {
            "ACCOUNT_LIVE_DAILY_RETURN",
            "ACCOUNT_LIVE_HOLDING_PERIOD_RETURN",
            "DECLARED_GAP_COVERAGE",
            "PROVIDER_LIVE_DAILY_RETURN",
        }
    ),
}
# The retained Matrixport generation materialized account totals with Python's
# 28-significant-digit Decimal context.  Multiplication into account value and
# the later return/composite divisions can each contribute one rounding unit.
# Product-NAV returns therefore corroborate account returns within this strict
# bound, while the admitted provider-ledger account return remains canonical.
MATRIXPORT_ACCOUNT_RETURN_RECONCILIATION_MAX_ERROR: Final = Decimal("2e-27")
LAYERED_HISTORY_REPOSITORY_DEPENDENCIES: Final = (
    "docs/analytics/current-strategy-decomposition-plan.json",
    "packages/core/src/quant_engine/core/contracts.py",
    "schemas/portfolio/financial-mvp-source-contract.schema.json",
    "tools/portfolio_financial_mvp_contract.py",
)
CORE_GENERATION_ADAPTER_BY_SOURCE_SCHEMA: Final = {
    ("provider-ledger", "1"): "quant-engine-provider-ledger",
    ("matrixport-generation-catalog", "2"): "quant-engine-matrixport",
    ("quantbase-generation", "1"): "quant-engine-web-contract-harvester",
    ("portfolio123-generation", "1"): "quant-engine-portfolio123",
}


def _clean_git_environment() -> dict[str, str]:
    repository_selection_variables = frozenset(
        {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CEILING_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_INDEX_FILE",
            "GIT_NAMESPACE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_PREFIX",
            "GIT_WORK_TREE",
        }
    )
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in repository_selection_variables
    }


@lru_cache(maxsize=1)
def _trusted_layered_history_code_identity() -> tuple[str, str]:
    repository_root = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "rev-parse",
                "--show-toplevel",
                "HEAD^{commit}",
            ],
            capture_output=True,
            check=False,
            env=_clean_git_environment(),
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise TargetSeriesError(
            "layered history repository identity is unavailable"
        ) from exc
    identity_lines = completed.stdout.splitlines()
    repository_sha = identity_lines[1].strip() if len(identity_lines) == 2 else ""
    if (
        completed.returncode != 0
        or len(identity_lines) != 2
        or Path(identity_lines[0].strip()).resolve() != repository_root
        or len(repository_sha) != 40
        or any(ch not in "0123456789abcdef" for ch in repository_sha)
    ):
        raise TargetSeriesError("layered history repository identity is unavailable")
    tool_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return repository_sha, tool_sha256


@lru_cache(maxsize=1)
def _committed_layered_history_dependency_sha256s() -> dict[str, str]:
    repository_root = Path(__file__).resolve().parents[1]
    repository_sha, _tool_sha256 = _trusted_layered_history_code_identity()
    identities: dict[str, str] = {}
    for relative_path in LAYERED_HISTORY_REPOSITORY_DEPENDENCIES:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository_root),
                    "show",
                    f"{repository_sha}:{relative_path}",
                ],
                capture_output=True,
                check=False,
                env=_clean_git_environment(),
                timeout=10,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise TargetSeriesError(
                "layered history committed dependency is unavailable"
            ) from exc
        if completed.returncode != 0:
            raise TargetSeriesError(
                "layered history committed dependency is unavailable"
            )
        identities[relative_path] = hashlib.sha256(completed.stdout).hexdigest()
    return identities


def _require_trusted_layered_history_dependencies() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    expected = _committed_layered_history_dependency_sha256s()
    for relative_path, expected_sha256 in expected.items():
        path = repository_root.joinpath(*relative_path.split("/"))
        if (
            path.is_symlink()
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
        ):
            raise TargetSeriesError(
                f"layered history repository dependency differs: {relative_path}"
            )
    imported_core_path = inspect.getsourcefile(canonical_json_bytes)
    core_dependency = "packages/core/src/quant_engine/core/contracts.py"
    if (
        imported_core_path is None
        or hashlib.sha256(Path(imported_core_path).read_bytes()).hexdigest()
        != expected[core_dependency]
    ):
        raise TargetSeriesError("layered history imported core dependency differs")


LAYERED_HISTORY_SEGMENT_FIELDS: Final = (
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
LAYERED_HISTORY_OUTPUT_NAMES: Final = (
    "segments.csv",
    "excluded.csv",
    "returns.csv",
    "live-only.csv",
    "live-plus-backtest.csv",
    "coverage.json",
)
STRATEGY_HISTORY_STATUSES: Final = frozenset(
    {
        "ACCEPTED_EXACT_VERSION_HISTORY",
        "ACCEPTED_EXACT_VERSION_PLUS_GOVERNED_SIGNAL_PRICE_EXTENSION",
        "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
    }
)


class TargetSeriesError(ValueError):
    """A source or requested target-series build violates the contract."""


@dataclass(frozen=True)
class VerifiedArtifact:
    artifact_id: str
    sha256: str
    size_bytes: int


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TargetSeriesError(f"{name} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError(f"{name} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise TargetSeriesError(f"{name} must be an object")
    return value


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _manifest_artifacts(value: object) -> dict[str, VerifiedArtifact]:
    if not isinstance(value, list):
        raise TargetSeriesError("manifest artifacts must be an array")
    result: dict[str, VerifiedArtifact] = {}
    for item in value:
        if not isinstance(item, dict):
            raise TargetSeriesError("manifest artifact must be an object")
        artifact_id = str(item.get("artifact_id", ""))
        sha256 = str(item.get("sha256", ""))
        size = item.get("size_bytes")
        if (
            not artifact_id
            or len(sha256) != 64
            or not isinstance(size, int)
            or size < 0
            or artifact_id in result
        ):
            raise TargetSeriesError("manifest artifact identity is invalid")
        result[artifact_id] = VerifiedArtifact(artifact_id, sha256, size)
    return result


def _inventory_sha256(files: list[dict[str, object]]) -> str:
    files.sort(key=lambda item: str(item["relative_path"]))
    return hashlib.sha256(
        canonical_json_bytes({"files": files, "inventory_schema_version": 1})
    ).hexdigest()


def _source_generation_inventory_sha256(
    source_name: str,
    manifest_content: bytes,
    manifest: Mapping[str, Any],
) -> str:
    """Derive the published generation inventory from governed manifest bytes."""

    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    production_inventory_artifacts: dict[str, dict[str, str]] = {
        "provider-ledger": {
            "normalized/admit-or-declare-receipt.json": (
                "admit-or-declare-receipt.json"
            ),
            "normalized/provider-ledger.json": "provider-ledger.json",
        },
        "matrixport": {"normalized/generation-catalog.json": "catalog.json"},
        "quantbase": {
            "normalized/quantbase-generation.json": "quantbase-generation.json"
        },
        "portfolio123": {
            "normalized/portfolio123-generation.json": "portfolio123-generation.json"
        },
    }
    if source_name not in production_inventory_artifacts:
        raise TargetSeriesError("source generation inventory profile differs")
    selected: dict[str, str] = {}
    configured = production_inventory_artifacts[source_name]
    if set(configured) <= set(artifacts):
        selected.update(configured)
        if source_name == "portfolio123":
            selected.update(
                {
                    artifact_id: artifact_id
                    for artifact_id in artifacts
                    if artifact_id.startswith("tables/")
                }
            )
    else:
        # Portable synthetic fixtures do not stage provider envelope catalogs.
        # Their declared artifacts are the complete generation inventory.
        selected.update({artifact_id: artifact_id for artifact_id in artifacts})
    if not selected:
        raise TargetSeriesError("source generation inventory is empty")
    files: list[dict[str, object]] = [
        {
            "relative_path": "generation.json",
            "sha256": hashlib.sha256(manifest_content).hexdigest(),
            "size_bytes": len(manifest_content),
        }
    ]
    for artifact_id, relative_path in selected.items():
        artifact = artifacts[artifact_id]
        files.append(
            {
                "relative_path": relative_path,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
        )
    if len({str(item["relative_path"]) for item in files}) != len(files):
        raise TargetSeriesError("source generation inventory paths differ")
    return _inventory_sha256(files)


def _generation_directory_inventory_sha256(generation_dir: Path) -> str:
    if generation_dir.is_symlink() or not generation_dir.is_dir():
        raise TargetSeriesError("source generation directory is missing or linked")
    files: list[dict[str, object]] = []
    for directory_name, directory_names, file_names in os.walk(
        generation_dir, followlinks=False
    ):
        directory = Path(directory_name)
        if any((directory / name).is_symlink() for name in directory_names):
            raise TargetSeriesError("source generation inventory contains a link")
        for name in file_names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise TargetSeriesError(
                    "source generation inventory contains an irregular file"
                )
            digest, size = _sha256(path)
            files.append(
                {
                    "relative_path": path.relative_to(generation_dir).as_posix(),
                    "sha256": digest,
                    "size_bytes": size,
                }
            )
    if "generation.json" not in {str(item["relative_path"]) for item in files}:
        raise TargetSeriesError("source generation inventory lacks generation.json")
    return _inventory_sha256(files)


def _verify_artifact(
    path: Path, artifacts: Mapping[str, VerifiedArtifact], artifact_id: str
) -> VerifiedArtifact:
    if path.is_symlink() or not path.is_file():
        raise TargetSeriesError(f"{artifact_id} must be a regular file")
    expected = artifacts.get(artifact_id)
    if expected is None:
        raise TargetSeriesError(f"manifest does not declare {artifact_id}")
    actual_hash, actual_size = _sha256(path)
    if (actual_hash, actual_size) != (expected.sha256, expected.size_bytes):
        raise TargetSeriesError(f"{artifact_id} differs from the manifest")
    return expected


def _csv_rows(path: Path, required: frozenset[str], name: str) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise TargetSeriesError(f"{name} is not readable UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = tuple(reader.fieldnames or ())
    if not required.issubset(fields):
        raise TargetSeriesError(f"{name} lacks required columns")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise TargetSeriesError(f"{name} contains a malformed row")
        rows.append({str(key): str(value) for key, value in row.items()})
    return rows


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        accepted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TargetSeriesError(f"{name} must be decimal") from exc
    if not accepted.is_finite() or accepted <= 0:
        raise TargetSeriesError(f"{name} must be positive and finite")
    return accepted


def _decimal(value: object, name: str) -> Decimal:
    try:
        accepted = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise TargetSeriesError(f"{name} must be decimal") from exc
    if not accepted.is_finite():
        raise TargetSeriesError(f"{name} must be finite")
    return accepted


def _utc_day(value: str) -> date:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetSeriesError("Matrixport NAV timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetSeriesError("Matrixport NAV timestamp lacks a UTC offset")
    return parsed.date()


def _matrixport_declared_missing_dates(
    manifest: Mapping[str, Any],
    expected_policy: Mapping[str, Any] | None = None,
) -> frozenset[date]:
    """Return manifest-declared omissions, optionally bound to product authority."""

    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict) or (
        coverage.get("status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        or coverage.get("known_gaps") != []
    ):
        raise TargetSeriesError("Matrixport declared-boundary coverage is invalid")
    exclusions = coverage.get("declared_exclusions")
    if not isinstance(exclusions, list):
        raise TargetSeriesError("Matrixport declared exclusions are absent")
    dates_by_kind: dict[str, set[date]] = {kind: set() for kind in MATRIXPORT_GAP_KINDS}
    authorities: set[tuple[str, str]] = set()
    for raw in exclusions:
        if not isinstance(raw, dict):
            raise TargetSeriesError("Matrixport declared exclusion is invalid")
        item = raw.get("item")
        if not isinstance(item, str) or ":" not in item:
            continue
        kind, rendered_date = item.split(":", 1)
        if kind not in dates_by_kind:
            continue
        if set(raw) != {"item", "provenance", "reason"}:
            raise TargetSeriesError("Matrixport declared exclusion authority differs")
        provenance = raw.get("provenance")
        reason = raw.get("reason")
        if (
            not isinstance(provenance, str)
            or not provenance
            or not isinstance(reason, str)
            or not reason
        ):
            raise TargetSeriesError("Matrixport declared exclusion authority differs")
        authorities.add((provenance, reason))
        try:
            missing_date = date.fromisoformat(rendered_date)
        except ValueError as exc:
            raise TargetSeriesError(
                "Matrixport declared exclusion date is invalid"
            ) from exc
        if item != f"{kind}:{missing_date.isoformat()}":
            raise TargetSeriesError("Matrixport declared exclusion item is invalid")
        if missing_date in dates_by_kind[kind]:
            raise TargetSeriesError("Matrixport declared exclusion is duplicated")
        dates_by_kind[kind].add(missing_date)
    accepted = dates_by_kind[MATRIXPORT_GAP_KINDS[0]]
    if any(dates_by_kind[kind] != accepted for kind in MATRIXPORT_GAP_KINDS[1:]):
        raise TargetSeriesError("Matrixport declared exclusion date sets differ")
    if len(authorities) > 1:
        raise TargetSeriesError("Matrixport declared exclusion authority differs")
    if expected_policy is not None:
        raw_expected_dates = expected_policy.get("declared_missing_source_dates")
        if not isinstance(raw_expected_dates, list) or any(
            not isinstance(value, str) for value in raw_expected_dates
        ):
            raise TargetSeriesError("Matrixport product gap dates are invalid")
        try:
            expected_dates = {date.fromisoformat(value) for value in raw_expected_dates}
        except ValueError as exc:
            raise TargetSeriesError("Matrixport product gap dates are invalid") from exc
        expected_authority = (
            expected_policy.get("provenance"),
            expected_policy.get("reason"),
        )
        if accepted != expected_dates or authorities != {expected_authority}:
            raise TargetSeriesError(
                "Matrixport declared exclusions differ from product authority"
            )
    return frozenset(accepted)


def _matrixport_gap_endpoints(
    observed_dates: Sequence[date], declared_missing_dates: frozenset[date]
) -> frozenset[date]:
    """Validate all interior omissions and return non-comparable endpoints."""

    ordered = sorted(set(observed_dates))
    actual_missing: set[date] = set()
    omitted_return_dates: set[date] = set()
    for previous, current in zip(ordered, ordered[1:], strict=False):
        candidate = previous + timedelta(days=1)
        interval_missing: set[date] = set()
        while candidate < current:
            interval_missing.add(candidate)
            candidate += timedelta(days=1)
        if interval_missing:
            actual_missing.update(interval_missing)
            omitted_return_dates.add(current)
    if actual_missing != set(declared_missing_dates):
        raise TargetSeriesError("Matrixport NAV gaps differ from declared exclusions")
    accepted_endpoints = _matrixport_expected_gap_endpoints(declared_missing_dates)
    if omitted_return_dates != set(accepted_endpoints):
        raise TargetSeriesError("Matrixport NAV gap endpoints differ")
    return accepted_endpoints


def _matrixport_expected_gap_endpoints(
    declared_missing_dates: frozenset[date],
) -> frozenset[date]:
    """Return the first post-gap date whose multi-day return is unavailable."""

    return frozenset(
        item + timedelta(days=1)
        for item in declared_missing_dates
        if item + timedelta(days=1) not in declared_missing_dates
    )


def _matrixport_source_gap_policy(
    declared_missing_dates: frozenset[date],
    omitted_return_dates: frozenset[date],
    authority_policy: Mapping[str, Any] | None = None,
    span_return_receipts: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    unavailable = sorted(declared_missing_dates | omitted_return_dates)
    derived: dict[str, object] = {
        "declared_missing_source_dates": [
            item.isoformat() for item in sorted(declared_missing_dates)
        ],
        "omitted_return_dates": [
            item.isoformat() for item in sorted(omitted_return_dates)
        ],
        "unavailable_return_dates": [item.isoformat() for item in unavailable],
        "span_return_receipts": [dict(item) for item in span_return_receipts],
    }
    if authority_policy is None:
        return {**derived, "authority_bound": False}
    accepted = dict(authority_policy)
    required_keys = (
        "declared_missing_source_dates",
        "omitted_return_dates",
        "unavailable_return_dates",
    )
    if any(accepted.get(key) != derived[key] for key in required_keys):
        raise TargetSeriesError(
            "Matrixport source gap policy differs from observed omissions"
        )
    if span_return_receipts and (
        accepted.get("span_return_receipts") != derived["span_return_receipts"]
    ):
        raise TargetSeriesError(
            "Matrixport source span receipts differ from observed endpoints"
        )
    return accepted


def _matrixport_return_transform(policy: Mapping[str, Any]) -> None:
    """Prove that the contract-requested gap transform matches code capabilities."""

    raw = policy.get("return_transform")
    if not isinstance(raw, dict) or set(raw) != {"facts", "transform", "version"}:
        raise TargetSeriesError("Matrixport return transform is invalid")
    transform = raw.get("transform")
    version = raw.get("version")
    facts = raw.get("facts")
    if (
        not isinstance(transform, str)
        or not isinstance(version, str)
        or not isinstance(facts, list)
        or any(not isinstance(item, str) or not item for item in facts)
        or len(facts) != len(set(facts))
    ):
        raise TargetSeriesError("Matrixport return transform is invalid")
    supported = MATRIXPORT_RETURN_TRANSFORM_CAPABILITIES.get((transform, version))
    if supported is None or frozenset(facts) != supported:
        raise TargetSeriesError("Matrixport return transform capability differs")


def _matrixport_span_endpoint_identity(
    *, product_id: str, observed: date, nav: Decimal, shares: Decimal
) -> str:
    """Redact one exact span endpoint while retaining all numeric identity inputs."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "date": observed.isoformat(),
                "nav": format(nav.normalize(), "f"),
                "product_id": product_id,
                "shares": format(shares.normalize(), "f"),
            }
        )
    ).hexdigest()


def _matrixport_span_return_receipts(
    *,
    observations: Mapping[date, Decimal],
    declared_missing_dates: frozenset[date],
    product_id: str,
    endpoint_shares: Mapping[date, Decimal],
    funding_event_dates: Sequence[date],
) -> list[dict[str, object]]:
    """Derive only declared two-day, no-flow owner holding-period returns.

    These receipts never turn a missing date into a daily return.  They retain
    the exactly observed return from the preceding endpoint to the following
    endpoint so epoch compounding does not silently lose a measured interval.
    """

    receipts: list[dict[str, object]] = []
    for missing in sorted(declared_missing_dates):
        start = missing - timedelta(days=1)
        end = missing + timedelta(days=1)
        start_nav = observations.get(start)
        end_nav = observations.get(end)
        start_shares = endpoint_shares.get(start)
        end_shares = endpoint_shares.get(end)
        if (
            start_nav is None
            or end_nav is None
            or start_shares is None
            or end_shares is None
        ):
            raise TargetSeriesError("Matrixport gap span lacks exact NAV endpoints")
        if start_shares != end_shares:
            raise TargetSeriesError("Matrixport gap span endpoint shares differ")
        if any(start < observed <= end for observed in funding_event_dates):
            raise TargetSeriesError(
                "Matrixport gap span contains a subscription/funding event"
            )
        value = end_nav / start_nav - Decimal(1)
        if value <= -1:
            raise TargetSeriesError("Matrixport gap span return is invalid")
        receipts.append(
            {
                "end_endpoint_sha256": _matrixport_span_endpoint_identity(
                    product_id=product_id,
                    observed=end,
                    nav=end_nav,
                    shares=end_shares,
                ),
                "missing_source_date": missing.isoformat(),
                "return": format(value.normalize(), "f"),
                "span_days": (end - start).days,
                "span_end_date": end.isoformat(),
                "span_start_date": start.isoformat(),
                "start_endpoint_sha256": _matrixport_span_endpoint_identity(
                    product_id=product_id,
                    observed=start,
                    nav=start_nav,
                    shares=start_shares,
                ),
                "subscription_events_in_span": 0,
            }
        )
    return receipts


def _matrixport_epoch_hpr_receipt(
    *,
    returns: Sequence[Mapping[str, str]],
    span_return_receipts: Sequence[Mapping[str, object]],
    source_gap_policy: Mapping[str, object],
    admission_cutoff: date | None,
) -> dict[str, object]:
    """Bind daily-only analytics to the separately admitted epoch HPR path.

    A span is not a daily observation.  The receipt consequently hashes only
    the retained daily path while supplying the independently hash-bound
    holding-period links which cumulative/wealth consumers must compound.
    """

    if not returns:
        raise TargetSeriesError("Matrixport epoch HPR receipt lacks daily returns")
    daily_rows = [
        {
            "date": row["date"],
            "return": format(_decimal(row["return"], "Matrixport return").normalize(), "f"),
        }
        for row in returns
    ]
    daily_wealth = Decimal(1)
    for row in daily_rows:
        daily_wealth *= Decimal(1) + Decimal(row["return"])
    epoch_links = [
        (date.fromisoformat(row["date"]), Decimal(row["return"]))
        for row in daily_rows
    ] + [
        (
            date.fromisoformat(str(span.get("span_end_date"))),
            _decimal(span.get("return"), "Matrixport span return"),
        )
        for span in span_return_receipts
    ]
    epoch_links.sort(key=lambda item: item[0])
    if len({item[0] for item in epoch_links}) != len(epoch_links):
        raise TargetSeriesError("Matrixport epoch HPR links repeat an endpoint")
    epoch_wealth = Decimal(1)
    for _endpoint, value in epoch_links:
        epoch_wealth *= Decimal(1) + value
    return {
        "admission_cutoff": admission_cutoff.isoformat()
        if admission_cutoff is not None
        else None,
        "daily_cumulative_return": format((daily_wealth - Decimal(1)).normalize(), "f"),
        "daily_return_observations": len(daily_rows),
        "daily_return_series_schema": "ordered-date-return/1",
        "daily_return_series_sha256": hashlib.sha256(
            canonical_json_bytes(daily_rows)
        ).hexdigest(),
        "epoch_cumulative_return": format((epoch_wealth - Decimal(1)).normalize(), "f"),
        "epoch_holding_period_link_observations": len(daily_rows)
        + len(span_return_receipts),
        "observed_end": daily_rows[-1]["date"],
        "observed_start": daily_rows[0]["date"],
        "source_gap_policy_sha256": hashlib.sha256(
            canonical_json_bytes(source_gap_policy)
        ).hexdigest(),
        "span_return_receipts": [dict(item) for item in span_return_receipts],
    }


def _matrixport_daily_holding_shares(
    daily_holdings_path: Path,
    *,
    product_id: str,
    observations: Mapping[date, Decimal],
    declared_missing_dates: frozenset[date],
    account_live_start: date,
) -> dict[date, Decimal]:
    """Bind span endpoints to retained daily holdings, not a current snapshot."""

    daily: dict[date, tuple[Decimal, Decimal | None, bool]] = {}
    for row in _csv_rows(
        daily_holdings_path,
        MATRIXPORT_DAILY_HOLDING_FIELDS,
        "Matrixport daily holdings",
    ):
        if row["product_id"] != product_id:
            continue
        try:
            observed = date.fromisoformat(row["utc_date"])
        except ValueError as exc:
            raise TargetSeriesError("Matrixport daily holding date is invalid") from exc
        shares = _positive_decimal(row["share"], "Matrixport daily holding shares")
        nav_available = row["nav_available"].strip().lower()
        if nav_available not in {"true", "false"}:
            raise TargetSeriesError("Matrixport daily holding nav availability is invalid")
        nav = (
            _positive_decimal(row["nav"], "Matrixport daily holding NAV")
            if nav_available == "true"
            else None
        )
        candidate = (shares, nav, nav_available == "true")
        if observed in daily and daily[observed] != candidate:
            raise TargetSeriesError("Matrixport daily holdings have conflicting dates")
        daily[observed] = candidate

    required_dates = {account_live_start}
    for missing in declared_missing_dates:
        required_dates.update(
            {missing - timedelta(days=1), missing, missing + timedelta(days=1)}
        )
    endpoint_shares: dict[date, Decimal] = {}
    for observed in required_dates:
        candidate = daily.get(observed)
        if candidate is None:
            raise TargetSeriesError("Matrixport daily holdings lack required span date")
        shares, daily_nav, nav_available = candidate
        expected_nav = observations.get(observed)
        if observed in declared_missing_dates:
            if nav_available or daily_nav is not None or expected_nav is not None:
                raise TargetSeriesError("Matrixport daily holdings missing-date status differs")
        elif not nav_available or daily_nav != expected_nav:
            raise TargetSeriesError("Matrixport daily holdings NAV differs from endpoint")
        endpoint_shares[observed] = shares
    return endpoint_shares


def _base_rows(
    path: Path, series_id: str, *, replace_existing: bool = False
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows = _csv_rows(path, frozenset(TARGET_FIELDS), "base targets")
    if tuple(rows[0].keys()) != TARGET_FIELDS if rows else False:
        raise TargetSeriesError("base target header differs")
    existing = [row for row in rows if row["series_id"] == series_id]
    if existing and not replace_existing:
        raise TargetSeriesError("base targets already contain the requested series")
    if replace_existing and not existing:
        raise TargetSeriesError(
            "base targets lack the series requested for replacement"
        )
    retained = [row for row in rows if row["series_id"] != series_id]
    return (
        [{field: row[field] for field in TARGET_FIELDS} for row in retained],
        [{field: row[field] for field in TARGET_FIELDS} for row in existing],
    )


def _matrixport_returns(
    nav_path: Path,
    holdings_path: Path,
    subscription_path: Path,
    daily_holdings_path: Path | None,
    product_id: str,
    series_id: str,
    declared_missing_dates: frozenset[date],
    declared_gap_policy: Mapping[str, Any] | None = None,
    admission_cutoff: date | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    holdings = [
        row
        for row in _csv_rows(
            holdings_path, MATRIXPORT_HOLDING_FIELDS, "Matrixport holdings"
        )
        if row["product_id"] == product_id
    ]
    if len(holdings) != 1:
        raise TargetSeriesError(
            "Matrixport product must match exactly one live holding"
        )
    shares = _positive_decimal(holdings[0]["shares"], "Matrixport holding shares")

    funded: list[tuple[date, date, Decimal]] = []
    for row in _csv_rows(
        subscription_path,
        MATRIXPORT_SUBSCRIPTION_FIELDS,
        "Matrixport subscriptions",
    ):
        if row["product_id"] != product_id or row["status"] != "300":
            continue
        try:
            subscription_share = Decimal(row["subscription_share"])
        except (InvalidOperation, ValueError) as exc:
            raise TargetSeriesError(
                "Matrixport subscription share must be decimal"
            ) from exc
        if not subscription_share.is_finite() or subscription_share <= 0:
            continue
        funded.append(
            (
                _utc_day(row["subscription_time_utc"]),
                _utc_day(row["confirm_nav_time_utc"]),
                subscription_share,
            )
        )
    if not funded:
        raise TargetSeriesError(
            "Matrixport product lacks a successful positive subscription"
        )
    (
        subscription_initiated_date,
        funding_event_date,
        funded_shares,
    ) = min(funded, key=lambda item: item[1])
    if funding_event_date < subscription_initiated_date:
        raise TargetSeriesError(
            "Matrixport subscription confirmation precedes its initiation"
        )
    account_live_start = funding_event_date

    observations: dict[date, Decimal] = {}
    for row in _csv_rows(nav_path, MATRIXPORT_NAV_FIELDS, "Matrixport daily NAV"):
        if row["product_id"] != product_id:
            continue
        day = _utc_day(row["timestamp_utc"])
        if admission_cutoff is not None and day > admission_cutoff:
            continue
        nav = _positive_decimal(row["nav"], "Matrixport NAV")
        if day in observations and observations[day] != nav:
            raise TargetSeriesError(
                "Matrixport NAV contains conflicting duplicate dates"
            )
        observations[day] = nav
    if len(observations) < 2:
        raise TargetSeriesError("Matrixport product lacks a usable NAV history")

    ordered = sorted(observations.items())
    if account_live_start not in observations:
        raise TargetSeriesError(
            "Matrixport funding confirmation lacks an exact held NAV observation"
        )
    account_nav_dates = [day for day, _nav in ordered if day >= account_live_start]
    omitted_return_dates = _matrixport_gap_endpoints(
        account_nav_dates, declared_missing_dates
    )
    endpoint_shares: dict[date, Decimal]
    if declared_missing_dates:
        if daily_holdings_path is None:
            raise TargetSeriesError("Matrixport gap spans require daily holdings")
        endpoint_shares = _matrixport_daily_holding_shares(
            daily_holdings_path,
            product_id=product_id,
            observations=observations,
            declared_missing_dates=declared_missing_dates,
            account_live_start=account_live_start,
        )
        if funded_shares != endpoint_shares[account_live_start]:
            raise TargetSeriesError(
                "Matrixport funding shares differ from the first held daily observation"
            )
    elif funded_shares != shares:
        raise TargetSeriesError(
            "Matrixport funding shares differ from the exact live holding"
        )
    span_return_receipts = _matrixport_span_return_receipts(
        observations=observations,
        declared_missing_dates=declared_missing_dates,
        product_id=product_id,
        endpoint_shares=endpoint_shares if declared_missing_dates else {},
        funding_event_dates=[item[1] for item in funded],
    )
    returns: list[dict[str, str]] = []
    provider_return_observations = 0
    boundary_return_excluded = False
    epoch_wealth = Decimal(1)
    for (_previous_day, previous), (day, nav) in zip(
        ordered, ordered[1:], strict=False
    ):
        value = nav / previous - Decimal(1)
        if day < account_live_start:
            provider_return_observations += 1
            continue
        if day == account_live_start:
            boundary_return_excluded = True
            continue
        if day in omitted_return_dates:
            epoch_wealth *= Decimal(1) + value
            continue
        epoch_wealth *= Decimal(1) + value
        returns.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
    provider_nav_dates = [day for day, _nav in ordered if day < account_live_start]
    if len(account_nav_dates) < 2 or not returns:
        raise TargetSeriesError(
            "Matrixport funded product lacks a measurable account-live history"
        )
    source_gap_policy = _matrixport_source_gap_policy(
        declared_missing_dates,
        omitted_return_dates,
        declared_gap_policy,
        span_return_receipts,
    )
    epoch_hpr_receipt = _matrixport_epoch_hpr_receipt(
        returns=returns,
        span_return_receipts=span_return_receipts,
        source_gap_policy=source_gap_policy,
        admission_cutoff=admission_cutoff,
    )
    if (
        epoch_hpr_receipt["daily_cumulative_return"]
        != format((_cumulative(returns)).normalize(), "f")
        or epoch_hpr_receipt["epoch_cumulative_return"]
        != format((epoch_wealth - Decimal(1)).normalize(), "f")
    ):
        raise TargetSeriesError("Matrixport epoch HPR receipt differs from return path")
    return returns, {
        "account_live_start": account_live_start.isoformat(),
        "admission_cutoff": (
            admission_cutoff.isoformat() if admission_cutoff is not None else None
        ),
        "account_nav_observations": len(account_nav_dates),
        "boundary_return_excluded": boundary_return_excluded,
        "daily_cumulative_return": format(
            (_cumulative(returns)).normalize(), "f"
        ),
        "epoch_cumulative_return": format(
            (epoch_wealth - Decimal(1)).normalize(), "f"
        ),
        "epoch_holding_period_link_observations": len(returns)
        + len(span_return_receipts),
        "epoch_hpr_receipt": epoch_hpr_receipt,
        "funding_event_date": funding_event_date.isoformat(),
        "holding_shares": format(shares.normalize(), "f"),
        "nav_observations": len(ordered),
        "nav_observed_start": ordered[0][0].isoformat(),
        "nav_observed_end": ordered[-1][0].isoformat(),
        "observed_start": returns[0]["date"],
        "observed_end": returns[-1]["date"],
        "provider_live_observed_end": (
            provider_nav_dates[-1].isoformat() if provider_nav_dates else None
        ),
        "provider_live_observed_start": (
            provider_nav_dates[0].isoformat() if provider_nav_dates else None
        ),
        "provider_live_return_observations": provider_return_observations,
        "return_observations": len(returns),
        "source_gap_policy": source_gap_policy,
        "span_return_receipts": span_return_receipts,
        "subscription_initiated_date": subscription_initiated_date.isoformat(),
    }


def _target_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TARGET_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _target_rows(content: bytes) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TargetSeriesError("target content is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != TARGET_FIELDS:
        raise TargetSeriesError("target content header differs")
    rows = [{field: str(row[field]) for field in TARGET_FIELDS} for row in reader]
    if not rows:
        raise TargetSeriesError("target content is empty")
    return rows


def _path_statistics(rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    if not rows:
        raise TargetSeriesError("return path is empty")
    wealth = Decimal(1)
    peak = Decimal(1)
    maximum_drawdown = Decimal(0)
    for row in rows:
        value = _decimal(row["return"], "return")
        if value <= -1:
            raise TargetSeriesError("return must exceed negative one")
        wealth *= Decimal(1) + value
        peak = max(peak, wealth)
        maximum_drawdown = max(maximum_drawdown, Decimal(1) - wealth / peak)
    return {
        "cumulative_return": format((wealth - Decimal(1)).normalize(), "f"),
        "current_drawdown": format((Decimal(1) - wealth / peak).normalize(), "f"),
        "maximum_drawdown": format(maximum_drawdown.normalize(), "f"),
        "observed_end": rows[-1]["date"],
        "observed_start": rows[0]["date"],
        "return_observations": len(rows),
    }


def _quantbase_returns(
    history_path: Path,
    activity_path: Path,
    fund_id: str,
    series_id: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    history: list[tuple[date, Decimal, Decimal]] = []
    seen_days: set[date] = set()
    for row in _csv_rows(
        history_path, QUANTBASE_HISTORY_FIELDS, "Quantbase account price history"
    ):
        try:
            day = datetime.strptime(row["time"], "%m/%d/%Y").date()
        except ValueError as exc:
            raise TargetSeriesError("Quantbase account date is invalid") from exc
        if day in seen_days:
            raise TargetSeriesError("Quantbase account history has duplicate dates")
        seen_days.add(day)
        value = _decimal(row["value"], "Quantbase account value")
        cost_basis = _decimal(row["cost_basis"], "Quantbase cost basis")
        if value < 0 or cost_basis < 0:
            raise TargetSeriesError(
                "Quantbase value and cost basis must be nonnegative"
            )
        history.append((day, value, cost_basis))
    history.sort(key=lambda item: item[0])
    if len(history) < 2:
        raise TargetSeriesError("Quantbase account history is insufficient")

    contributions: list[tuple[datetime, Decimal]] = []
    for row in _csv_rows(
        activity_path, QUANTBASE_ACTIVITY_FIELDS, "Quantbase account activity"
    ):
        if (
            row["activity_type"] != "filled_txn"
            or row["fund"] != fund_id
            or row["is_rebalance"].lower() != "false"
        ):
            continue
        amount = _decimal(row["amount"], "Quantbase fund activity amount")
        if amount <= 0 or row["type"].upper() != "BUY":
            continue
        try:
            observed = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise TargetSeriesError("Quantbase activity timestamp is invalid") from exc
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise TargetSeriesError("Quantbase activity timestamp lacks an offset")
        contributions.append((observed, amount))
    contributions.sort(key=lambda item: item[0])
    if not contributions:
        raise TargetSeriesError("Quantbase fund has no identified capital additions")

    pending: list[tuple[datetime, Decimal]] = []
    contribution_index = 0
    previous_cost_basis = Decimal(0)
    booked_flows: dict[date, Decimal] = {}
    flow_matches: list[dict[str, object]] = []
    tolerance = Decimal("0.02")
    for day, _value, cost_basis in history:
        while (
            contribution_index < len(contributions)
            and contributions[contribution_index][0].date() <= day
        ):
            pending.append(contributions[contribution_index])
            contribution_index += 1
        delta = cost_basis - previous_cost_basis
        if delta > tolerance and pending:
            running = Decimal(0)
            matched_count = 0
            for index, (_timestamp, amount) in enumerate(pending):
                running += amount
                if abs(running - delta) <= tolerance:
                    matched_count = index + 1
                    break
                if running > delta + tolerance:
                    break
            if matched_count:
                booked_flows[day] = running
                flow_matches.append(
                    {
                        "booked_date": day.isoformat(),
                        "cost_basis_increase": format(delta.normalize(), "f"),
                        "flow_amount": format(running.normalize(), "f"),
                        "source_activity_count": matched_count,
                    }
                )
                pending = pending[matched_count:]
        previous_cost_basis = cost_basis
    if contribution_index != len(contributions) or pending:
        raise TargetSeriesError("Quantbase capital additions do not reconcile")

    result: list[dict[str, str]] = []
    previous_value: Decimal | None = None
    first_value_date: date | None = None
    for day, value, _cost_basis in history:
        if value <= 0:
            continue
        if first_value_date is None:
            first_value_date = day
        if previous_value is None:
            previous_value = value
            continue
        flow = booked_flows.get(day, Decimal(0))
        period_return = (value - flow) / previous_value - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Quantbase flow-adjusted return is invalid")
        result.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
        previous_value = value
    statistics_value = _path_statistics(result)
    statistics_value.update(
        {
            "flow_convention": "DATE_ONLY_END_OF_DAY_BOOKING",
            "flow_events": len(flow_matches),
            "fund_id": fund_id,
            "nav_observations": len(result) + 1,
            "source_first_positive_value_date": (
                first_value_date.isoformat() if first_value_date else None
            ),
        }
    )
    return result, {"coverage": statistics_value, "flow_matches": flow_matches}


def build_quantbase_targets(
    base_targets: Path,
    manifest_path: Path,
    history_path: Path,
    activity_path: Path,
    fund_id: str,
    series_id: str,
) -> tuple[bytes, bytes]:
    manifest = _load_json(manifest_path, "Quantbase generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Quantbase generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Quantbase generation identity is invalid")
    try:
        generation_as_of_date = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("Quantbase generation as-of date is invalid") from exc
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    history_artifact = _verify_artifact(
        history_path,
        artifacts,
        "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
    )
    activity_artifact = _verify_artifact(
        activity_path, artifacts, "source/csv/account_activity.csv"
    )
    base, _existing = _base_rows(base_targets, series_id)
    provider_rows, evidence = _quantbase_returns(
        history_path, activity_path, fund_id, series_id
    )
    combined = sorted(
        [*base, *provider_rows], key=lambda row: (row["series_id"], row["date"])
    )
    target_content = _target_bytes(combined)
    base_hash, base_size = _sha256(base_targets)
    proof: dict[str, object] = {
        "builder": "portfolio-target-series/quantbase-account-1",
        "generation_id": generation_id,
        "generation_as_of_date": generation_as_of_date.isoformat(),
        "provider_id": "quantbase",
        "series_id": series_id,
        "strategy_id": "volatis-strategy",
        "evidence_layer": "ACCOUNT_LIVE",
        "return_basis": "NET",
        **evidence,
        "sources": {
            "base_targets": {"sha256": base_hash, "size_bytes": base_size},
            "account_history": {
                "artifact_id": history_artifact.artifact_id,
                "sha256": history_artifact.sha256,
                "size_bytes": history_artifact.size_bytes,
            },
            "account_activity": {
                "artifact_id": activity_artifact.artifact_id,
                "sha256": activity_artifact.sha256,
                "size_bytes": activity_artifact.size_bytes,
            },
        },
        "target_sha256": hashlib.sha256(target_content).hexdigest(),
        "target_size_bytes": len(target_content),
    }
    return target_content, canonical_json_bytes(proof)


def build_matrixport_targets(
    base_targets: Path,
    manifest_path: Path,
    nav_path: Path,
    holdings_path: Path,
    subscription_path: Path,
    product_id: str,
    series_id: str,
    *,
    admission_cutoff: date | None = None,
    daily_holdings_path: Path | None = None,
    replace_existing: bool = False,
) -> tuple[bytes, bytes]:
    manifest = _load_json(manifest_path, "Matrixport generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Matrixport generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Matrixport generation identity is invalid")
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    nav_artifact = _verify_artifact(nav_path, artifacts, "csv/nav_daily.csv")
    holding_artifact = _verify_artifact(holdings_path, artifacts, "csv/holdings.csv")
    daily_holding_artifact = (
        _verify_artifact(daily_holdings_path, artifacts, "csv/daily_holdings.csv")
        if daily_holdings_path is not None
        else None
    )
    subscription_artifact = _verify_artifact(
        subscription_path, artifacts, "csv/subscription_details.csv"
    )
    declared_missing_dates = _matrixport_declared_missing_dates(manifest)

    base, existing = _base_rows(
        base_targets, series_id, replace_existing=replace_existing
    )
    provider_rows, coverage = _matrixport_returns(
        nav_path,
        holdings_path,
        subscription_path,
        daily_holdings_path,
        product_id,
        series_id,
        declared_missing_dates,
        admission_cutoff=admission_cutoff,
    )
    combined = sorted(
        [*base, *provider_rows], key=lambda row: (row["series_id"], row["date"])
    )
    target_content = _target_bytes(combined)
    base_hash, base_size = _sha256(base_targets)
    proof: dict[str, object] = {
        "builder": MATRIXPORT_TARGET_BUILDER,
        "generation_id": generation_id,
        "generation_as_of_date": str(manifest.get("as_of_date", "")),
        "series_id": series_id,
        "evidence_layer": "ACCOUNT_LIVE",
        "return_basis": "NET",
        "coverage": coverage,
        "sources": {
            "base_targets": {"sha256": base_hash, "size_bytes": base_size},
            "matrixport_nav": {
                "artifact_id": nav_artifact.artifact_id,
                "sha256": nav_artifact.sha256,
                "size_bytes": nav_artifact.size_bytes,
            },
            "matrixport_holding": {
                "artifact_id": holding_artifact.artifact_id,
                "sha256": holding_artifact.sha256,
                "size_bytes": holding_artifact.size_bytes,
            },
            **(
                {
                    "matrixport_daily_holding": {
                        "artifact_id": daily_holding_artifact.artifact_id,
                        "sha256": daily_holding_artifact.sha256,
                        "size_bytes": daily_holding_artifact.size_bytes,
                    }
                }
                if daily_holding_artifact is not None
                else {}
            ),
            "matrixport_subscription": {
                "artifact_id": subscription_artifact.artifact_id,
                "sha256": subscription_artifact.sha256,
                "size_bytes": subscription_artifact.size_bytes,
            },
        },
        "replacement": {
            "enabled": replace_existing,
            "prior_observed_end": existing[-1]["date"] if existing else None,
            "prior_observed_start": existing[0]["date"] if existing else None,
            "removed_observations": len(existing),
        },
        "target_sha256": hashlib.sha256(target_content).hexdigest(),
        "target_size_bytes": len(target_content),
    }
    return target_content, canonical_json_bytes(proof)


def build_matrixport_strategy_evidence(
    base_evidence_path: Path,
    matrixport_proof_content: bytes,
    strategy_id: str,
) -> bytes:
    """Replace one strategy-evidence row with generation-bound Matrixport proof."""

    evidence = _load_json(base_evidence_path, "base strategy evidence")
    if evidence.get("schema_version") != 1:
        raise TargetSeriesError("base strategy evidence schema differs")
    strategies = evidence.get("strategies")
    if not isinstance(strategies, list):
        raise TargetSeriesError("base strategy evidence strategies are invalid")
    try:
        proof = json.loads(matrixport_proof_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError("Matrixport source proof is invalid") from exc
    if not isinstance(proof, dict) or proof.get("builder") != MATRIXPORT_TARGET_BUILDER:
        raise TargetSeriesError("Matrixport source proof contract differs")
    generation_as_of = str(proof.get("generation_as_of_date", ""))
    try:
        date.fromisoformat(generation_as_of)
    except ValueError as exc:
        raise TargetSeriesError("Matrixport generation as-of date is invalid") from exc
    coverage = proof.get("coverage")
    sources = proof.get("sources")
    if not isinstance(coverage, dict) or not isinstance(sources, dict):
        raise TargetSeriesError("Matrixport source proof is incomplete")
    replacement = {
        "account": coverage,
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_as_of_date": generation_as_of,
        "generation_id": str(proof.get("generation_id", "")),
        "provider_id": "matrixport",
        "sources": sources,
        "strategy_id": strategy_id,
        "strategy_name": str(proof.get("series_id", "")),
    }
    if len(replacement["generation_id"]) != 64 or not replacement["strategy_name"]:
        raise TargetSeriesError("Matrixport strategy identity is invalid")
    retained = [
        item
        for item in strategies
        if isinstance(item, dict) and item.get("strategy_id") != strategy_id
    ]
    result = {
        "as_of_date": max(str(evidence.get("as_of_date", "")), generation_as_of),
        "schema_version": 1,
        "strategies": sorted(
            [*retained, replacement], key=lambda item: str(item.get("strategy_id", ""))
        ),
    }
    return canonical_json_bytes(result)


def _parquet_rows(
    path: Path, columns: Sequence[str], name: str
) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise TargetSeriesError(f"{name} must be a regular file")
    try:
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
    except ImportError as exc:
        raise TargetSeriesError("Portfolio123 Parquet support is unavailable") from exc
    try:
        table = pq.read_table(path, columns=list(columns))
    except Exception as exc:
        raise TargetSeriesError(f"{name} is not readable Parquet") from exc
    return [dict(row) for row in table.to_pylist()]


def _portfolio123_returns(equity_path: Path, entity_id: str) -> list[dict[str, str]]:
    selected: dict[date, tuple[Decimal, Decimal]] = {}
    for row in _parquet_rows(
        equity_path,
        ("_entity_id", "Date", "Total Equity", "Cash Added"),
        "Portfolio123 equity curve",
    ):
        if str(row.get("_entity_id") or "") != entity_id:
            continue
        raw_date = str(row.get("Date") or "")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise TargetSeriesError("Portfolio123 equity date is invalid") from exc
        equity = _decimal(row.get("Total Equity") or "", "Portfolio123 equity")
        cash_added = _decimal(row.get("Cash Added") or "0", "Portfolio123 cash added")
        if equity <= 0:
            raise TargetSeriesError("Portfolio123 equity must be positive")
        observation = (equity, cash_added)
        if day in selected and selected[day] != observation:
            raise TargetSeriesError("Portfolio123 equity has conflicting dates")
        selected[day] = observation
    ordered = sorted(selected.items())
    if len(ordered) < 2:
        raise TargetSeriesError("Portfolio123 entity lacks an equity history")
    result: list[dict[str, str]] = []
    previous = ordered[0][1][0]
    for day, (equity, cash_added) in ordered[1:]:
        period_return = (equity - cash_added) / previous - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Portfolio123 flow-adjusted return is invalid")
        result.append(
            {
                "series_id": entity_id,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "PROVIDER_LIVE",
                "return_basis": "OBSERVED",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
        previous = equity
    return result


def _cumulative(rows: Sequence[Mapping[str, str]]) -> Decimal:
    wealth = Decimal(1)
    for row in rows:
        wealth *= Decimal(1) + _decimal(row["return"], "return")
    return wealth - Decimal(1)


def _overlap_reconciliation(
    account_rows: Sequence[Mapping[str, str]],
    provider_rows: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    account = {
        row["date"]: _decimal(row["return"], "account return") for row in account_rows
    }
    provider = {
        row["date"]: _decimal(row["return"], "provider return") for row in provider_rows
    }
    common = sorted(set(account) & set(provider))
    if len(common) < 2:
        raise TargetSeriesError("Portfolio123 paths lack a usable exact-date overlap")
    account_values = [float(account[day]) for day in common]
    provider_values = [float(provider[day]) for day in common]
    if (
        statistics.pstdev(account_values) <= 1e-15
        or statistics.pstdev(provider_values) <= 1e-15
    ):
        correlation: float | None = None
    else:
        correlation = statistics.correlation(account_values, provider_values)
    differences = [left - right for left, right in zip(account_values, provider_values)]
    tracking_error = statistics.pstdev(differences) * math.sqrt(252)
    account_overlap = [
        {"return": format(account[day].normalize(), "f")} for day in common
    ]
    provider_overlap = [
        {"return": format(provider[day].normalize(), "f")} for day in common
    ]
    account_cumulative = _cumulative(account_overlap)
    provider_cumulative = _cumulative(provider_overlap)
    return {
        "account_cumulative_return": format(account_cumulative.normalize(), "f"),
        "account_minus_provider_cumulative": format(
            (account_cumulative - provider_cumulative).normalize(), "f"
        ),
        "annualized_tracking_error": format(tracking_error, ".12g"),
        "daily_return_correlation": (
            None if correlation is None else format(correlation, ".12g")
        ),
        "observations": len(common),
        "observed_end": common[-1],
        "observed_start": common[0],
        "provider_cumulative_return": format(provider_cumulative.normalize(), "f"),
        "status": "TESTED_EXACT_DATE_PATHS",
    }


def _portfolio123_snapshot(
    metadata_path: Path, account_entity_id: str
) -> dict[str, object]:
    matches = []
    for row in _parquet_rows(
        metadata_path,
        ("id", "lastFix", "netWorth", "netWorthPrevWithCashAdded", "isLive"),
        "Portfolio123 account metadata",
    ):
        if str(row.get("id") or "") == account_entity_id and row.get(
            "netWorth"
        ) not in (None, ""):
            matches.append(row)
    if len(matches) != 1:
        raise TargetSeriesError("Portfolio123 account snapshot is not unique")
    row = matches[0]
    try:
        milliseconds = int(str(row.get("lastFix") or ""))
    except ValueError as exc:
        raise TargetSeriesError(
            "Portfolio123 account snapshot time is invalid"
        ) from exc
    observed = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return {
        "account_entity_id": account_entity_id,
        "is_live": str(row.get("isLive") or "").lower() == "true",
        "net_worth": format(
            _decimal(
                row.get("netWorth") or "", "Portfolio123 account net worth"
            ).normalize(),
            "f",
        ),
        "net_worth_previous_with_cash_added": format(
            _decimal(
                row.get("netWorthPrevWithCashAdded") or "",
                "Portfolio123 previous net worth",
            ).normalize(),
            "f",
        ),
        "observed_at_utc": observed.isoformat().replace("+00:00", "Z"),
        "observed_date": observed.date().isoformat(),
    }


def build_portfolio123_evidence(
    target_content: bytes,
    manifest_path: Path,
    equity_path: Path,
    metadata_path: Path,
    provider_entity_id: str,
    account_entity_id: str,
    strategy_id: str,
    account_series_id: str,
) -> bytes:
    manifest = _load_json(manifest_path, "Portfolio123 generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Portfolio123 generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Portfolio123 generation identity is invalid")
    try:
        generation_as_of_date = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError(
            "Portfolio123 generation as-of date is invalid"
        ) from exc
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    equity_artifact = _verify_artifact(
        equity_path, artifacts, "tables/har_equity_curve.parquet"
    )
    metadata_artifact = _verify_artifact(
        metadata_path, artifacts, "tables/har_account_metadata.parquet"
    )
    account_rows = [
        row
        for row in _target_rows(target_content)
        if row["series_id"] == account_series_id
    ]
    if len(account_rows) < 2 or {row["evidence_layer"] for row in account_rows} != {
        "ACCOUNT_LIVE"
    }:
        raise TargetSeriesError("Portfolio123 account target is unavailable")
    provider_rows = _portfolio123_returns(equity_path, provider_entity_id)
    provider_statistics = _path_statistics(provider_rows)
    account_statistics = _path_statistics(account_rows)
    evidence = {
        "account": account_statistics,
        "current_snapshot": _portfolio123_snapshot(metadata_path, account_entity_id),
        "evidence_layer": "PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE",
        "generation_id": generation_id,
        "generation_as_of_date": generation_as_of_date.isoformat(),
        "overlap": _overlap_reconciliation(account_rows, provider_rows),
        "provider": {
            **provider_statistics,
            "entity_id": provider_entity_id,
        },
        "provider_id": "portfolio123",
        "sources": {
            "equity_curve": {
                "artifact_id": equity_artifact.artifact_id,
                "sha256": equity_artifact.sha256,
                "size_bytes": equity_artifact.size_bytes,
            },
            "account_metadata": {
                "artifact_id": metadata_artifact.artifact_id,
                "sha256": metadata_artifact.sha256,
                "size_bytes": metadata_artifact.size_bytes,
            },
        },
        "strategy_id": strategy_id,
        "strategy_name": account_series_id,
    }
    return canonical_json_bytes(evidence)


def build_strategy_evidence(
    quantbase_proof_content: bytes, portfolio123_evidence_content: bytes
) -> bytes:
    quantbase = json.loads(quantbase_proof_content)
    portfolio123 = json.loads(portfolio123_evidence_content)
    quantbase_strategy = {
        "account": quantbase["coverage"],
        "evidence_layer": quantbase["evidence_layer"],
        "generation_id": quantbase["generation_id"],
        "generation_as_of_date": quantbase["generation_as_of_date"],
        "provider_id": quantbase["provider_id"],
        "sources": quantbase["sources"],
        "strategy_id": quantbase["strategy_id"],
        "strategy_name": quantbase["series_id"],
    }
    as_of_dates = [
        str(quantbase_strategy["generation_as_of_date"]),
        str(portfolio123["generation_as_of_date"]),
    ]
    result = {
        "as_of_date": max(as_of_dates),
        "schema_version": 1,
        "strategies": [quantbase_strategy, portfolio123],
    }
    return canonical_json_bytes(result)


def _correlation_bytes(target_content: bytes) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CORRELATION_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in _target_rows(target_content):
        writer.writerow({field: row[field] for field in CORRELATION_FIELDS})
    return buffer.getvalue().encode("utf-8")


def build_provider_ledger_targets(
    manifest_path: Path,
    catalog_path: Path,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Export the accepted provider ledger as the sole live strategy source.

    This adapter deliberately performs no provider access.  It verifies the
    immutable provider-ledger generation, preserves the ratified evidence layer
    for each strategy, and emits both the target CSV and a compact series
    contract used by downstream parity gates.
    """

    manifest = _load_json(manifest_path, "provider-ledger generation manifest")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("provider-ledger generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("provider-ledger generation identity is invalid")
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    catalog_artifact = _verify_artifact(
        catalog_path, artifacts, "normalized/provider-ledger.json"
    )
    catalog = _load_json(catalog_path, "provider-ledger catalog")
    catalog_schema_version = catalog.get("catalog_schema_version")
    if catalog_schema_version not in {1, 2}:
        raise TargetSeriesError("provider-ledger catalog schema differs")
    if catalog_schema_version == 2 and not isinstance(
        catalog.get("provider_strategy_returns"), list
    ):
        raise TargetSeriesError(
            "provider-ledger provider strategy returns are unavailable"
        )
    as_of_date = str(catalog.get("as_of_date", ""))
    try:
        accepted_as_of = date.fromisoformat(as_of_date)
        manifest_as_of = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("provider-ledger as-of date is invalid") from exc
    if accepted_as_of != manifest_as_of:
        raise TargetSeriesError("provider-ledger manifest and catalog dates differ")

    raw_rows = catalog.get("strategy_returns")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise TargetSeriesError("provider-ledger strategy returns are unavailable")
    accepted_layers = {"ACCOUNT_LIVE", "PROVIDER_LIVE", "OFFICIAL_VEHICLE"}
    names_by_strategy: dict[str, str] = {}
    rows_by_series: dict[str, list[dict[str, str]]] = {}
    identities: set[tuple[str, str]] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise TargetSeriesError("provider-ledger strategy return is invalid")
        strategy_id = str(raw.get("strategy_id", "")).strip()
        series_id = str(raw.get("strategy_name", "")).strip()
        layer = str(raw.get("evidence_layer", "")).strip()
        if not strategy_id or not series_id or layer not in accepted_layers:
            raise TargetSeriesError("provider-ledger strategy return contract differs")
        prior_name = names_by_strategy.setdefault(strategy_id, series_id)
        if prior_name != series_id:
            raise TargetSeriesError("provider-ledger strategy name is inconsistent")
        try:
            observed = date.fromisoformat(str(raw.get("date", "")))
        except ValueError as exc:
            raise TargetSeriesError(
                "provider-ledger strategy return date is invalid"
            ) from exc
        if observed > accepted_as_of:
            raise TargetSeriesError("provider-ledger strategy return exceeds as-of")
        identity = (series_id, observed.isoformat())
        if identity in identities:
            raise TargetSeriesError("provider-ledger strategy return is duplicated")
        identities.add(identity)
        period_return = _decimal(raw.get("return"), "provider-ledger strategy return")
        if period_return <= -1:
            raise TargetSeriesError("provider-ledger strategy return is invalid")
        rows_by_series.setdefault(series_id, []).append(
            {
                "series_id": series_id,
                "date": observed.isoformat(),
                "return": format(period_return, "f"),
                "evidence_layer": layer,
                "return_basis": str(raw.get("return_basis") or "NET"),
                "return_type": str(raw.get("return_type") or "TOTAL"),
                "cost": "" if raw.get("cost") is None else str(raw["cost"]),
            }
        )

    evidence_rows = catalog.get("strategy_evidence")
    if not isinstance(evidence_rows, list):
        raise TargetSeriesError("provider-ledger strategy evidence is unavailable")
    evidence_by_name = {
        str(item.get("strategy_name")): item
        for item in evidence_rows
        if isinstance(item, dict) and item.get("strategy_name")
    }
    contracts: list[dict[str, object]] = []
    for series_id, series_rows in sorted(rows_by_series.items()):
        series_rows.sort(key=lambda item: item["date"])
        layers = {item["evidence_layer"] for item in series_rows}
        if len(layers) != 1:
            raise TargetSeriesError(
                "one canonical strategy has conflicting live layers"
            )
        evidence = evidence_by_name.get(series_id)
        if evidence is None:
            raise TargetSeriesError("provider-ledger strategy evidence is incomplete")
        layer = next(iter(layers))
        if evidence.get("evidence_layer") != layer:
            raise TargetSeriesError("provider-ledger return and evidence layers differ")
        selected = evidence.get("account" if layer == "ACCOUNT_LIVE" else "provider")
        if not isinstance(selected, dict):
            raise TargetSeriesError("provider-ledger selected evidence path is absent")
        expected = {
            "observed_start": series_rows[0]["date"],
            "observed_end": series_rows[-1]["date"],
            "return_observations": len(series_rows),
        }
        if any(selected.get(key) != value for key, value in expected.items()):
            raise TargetSeriesError("provider-ledger evidence statistics differ")
        contracts.append(
            {
                "evidence_layer": layer,
                "generation_id": evidence.get("generation_id"),
                "observed_end": expected["observed_end"],
                "observed_start": expected["observed_start"],
                "provider_id": evidence.get("provider_id"),
                "return_observations": expected["return_observations"],
                "series_id": series_id,
                "strategy_id": evidence.get("strategy_id"),
            }
        )

    ordered_rows = [
        row for series_id in sorted(rows_by_series) for row in rows_by_series[series_id]
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TARGET_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(ordered_rows)
    target_content = buffer.getvalue().encode("utf-8")
    evidence_content = canonical_json_bytes(
        {
            "as_of_date": as_of_date,
            "provider_ledger_generation_id": generation_id,
            "schema_version": 1,
            "strategies": evidence_rows,
        }
    )
    proof_content = canonical_json_bytes(
        {
            "as_of_date": as_of_date,
            "builder": "portfolio-target-series/provider-ledger-1",
            "provider_ledger": {
                "catalog_artifact_id": catalog_artifact.artifact_id,
                "catalog_sha256": catalog_artifact.sha256,
                "catalog_size_bytes": catalog_artifact.size_bytes,
                "generation_id": generation_id,
                "manifest_sha256": _sha256(manifest_path)[0],
            },
            "series_contracts": contracts,
            "target_sha256": hashlib.sha256(target_content).hexdigest(),
            "target_size_bytes": len(target_content),
        }
    )
    return target_content, evidence_content, proof_content, manifest_path.read_bytes()


def _strategy_version(namespace: str, value: Mapping[str, object]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"{namespace}:{digest}"


def _artifact_identity(
    *,
    input_id: str,
    artifact_role: str,
    path: Path,
    artifact_id: str | None = None,
    generation_id: str | None = None,
    as_of_date: str | None = None,
) -> dict[str, object]:
    digest, size = _sha256(path)
    return {
        "artifact_id": artifact_id,
        "artifact_role": artifact_role,
        "as_of_date": as_of_date,
        "generation_id": generation_id,
        "input_id": input_id,
        "sha256": digest,
        "size_bytes": size,
    }


def _history_contract(
    path: Path,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    _require_trusted_layered_history_dependencies()
    validator_path = Path(__file__).with_name("portfolio_financial_mvp_contract.py")
    spec = importlib.util.spec_from_file_location(
        "_portfolio_financial_mvp_contract_for_history", validator_path
    )
    if spec is None or spec.loader is None:
        raise TargetSeriesError("financial MVP contract validator is unavailable")
    validator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validator
    try:
        spec.loader.exec_module(validator)
        validator_canonical_json_bytes = getattr(
            validator, "canonical_json_bytes", None
        )
        validator_core_path = (
            inspect.getsourcefile(validator_canonical_json_bytes)
            if callable(validator_canonical_json_bytes)
            else None
        )
        committed_core_sha256 = _committed_layered_history_dependency_sha256s()[
            "packages/core/src/quant_engine/core/contracts.py"
        ]
        if (
            validator_core_path is None
            or hashlib.sha256(Path(validator_core_path).read_bytes()).hexdigest()
            != committed_core_sha256
        ):
            raise TargetSeriesError(
                "financial MVP validator imported core dependency differs"
            )
        contract = _load_json(path, "financial MVP source contract")
        schema = _load_json(
            validator_path.parents[1]
            / "schemas"
            / "portfolio"
            / "financial-mvp-source-contract.schema.json",
            "financial MVP source contract schema",
        )
        schema_failures = validator.schema_errors(schema, contract, schema)
        if schema_failures:
            raise TargetSeriesError("financial MVP contract schema differs")
        registry = _load_json(validator.DEFAULT_REGISTRY, "current strategy registry")
        contract = validator.validate_contract(contract, registry=registry)
    except (OSError, ValueError) as exc:
        raise TargetSeriesError("financial MVP contract is invalid") from exc
    product = contract.get("product")
    history = contract.get("history_corrections")
    raw_strategies = contract.get("strategies")
    raw_sources = contract.get("source_segments")
    if (
        not isinstance(product, dict)
        or product.get("product_id") != "FINANCIAL_MVP"
        or not isinstance(history, dict)
        or history.get("policy_id") != "PORT-HISTORY-TRUTH-1"
        or not isinstance(raw_strategies, list)
        or not isinstance(raw_sources, list)
    ):
        raise TargetSeriesError("financial MVP history contract differs")
    strategies: dict[str, dict[str, Any]] = {}
    for raw in raw_strategies:
        if not isinstance(raw, dict):
            raise TargetSeriesError("financial MVP strategy row is invalid")
        strategy_id = str(raw.get("strategy_id", "")).strip()
        if (
            not strategy_id
            or raw.get("product_requirement") not in {"REQUIRED", "EXCLUDED"}
            or strategy_id in strategies
        ):
            raise TargetSeriesError("financial MVP strategy identity differs")
        strategies[strategy_id] = dict(raw)
    if not strategies:
        raise TargetSeriesError("financial MVP strategy set is empty")
    sources: dict[str, dict[str, Any]] = {}
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise TargetSeriesError("financial MVP source row is invalid")
        source_id = str(raw.get("source_id", "")).strip()
        if not source_id or source_id in sources:
            raise TargetSeriesError("financial MVP source identity differs")
        sources[source_id] = dict(raw)
    corrections: dict[str, dict[str, Any]] = {}
    raw_corrections = history.get("corrections")
    if not isinstance(raw_corrections, list):
        raise TargetSeriesError("financial MVP history corrections are invalid")
    for raw in raw_corrections:
        if not isinstance(raw, dict):
            raise TargetSeriesError("financial MVP history correction is invalid")
        correction_id = str(raw.get("correction_id", "")).strip()
        if not correction_id or correction_id in corrections:
            raise TargetSeriesError("financial MVP correction identity differs")
        corrections[correction_id] = dict(raw)
    required_corrections = {
        "robuxio-provider-live-before-funding",
        "robuxio-account-live-after-funding",
        "p123-ai-backtest-candidate",
        "p123-eu-backtest-not-captured",
        "p123-eu-provider-live",
        "p123-eu-account-live",
        "volatis-backtest-not-proved",
        "volatis-provider-live",
        "volatis-account-live",
    }
    if not required_corrections <= set(corrections):
        raise TargetSeriesError("financial MVP history correction set is incomplete")
    return contract, strategies, sources, corrections


def _excluded_strategy_mode_reason(strategy: Mapping[str, Any]) -> str | None:
    requirement = strategy.get("product_requirement")
    lifecycle = strategy.get("lifecycle_status")
    if requirement == "REQUIRED":
        return None
    if requirement != "EXCLUDED":
        raise TargetSeriesError("financial MVP strategy requirement differs")
    if lifecycle == "DEPRECATED_HISTORY_ONLY":
        return "NOT_APPLICABLE_OWNER_APPROVED_DEPRECATED"
    if lifecycle == "IMMATERIAL_EXCLUDED":
        return "NOT_APPLICABLE_OWNER_APPROVED_IMMATERIAL"
    raise TargetSeriesError("excluded financial MVP strategy lifecycle differs")


def _verified_core_generation_envelope(
    manifest_content: bytes, source: Mapping[str, Any], error: str
) -> GenerationManifest:
    """Validate one retained provider generation against its contract schema."""

    expected_schema = source.get("schema")
    if (
        not isinstance(expected_schema, dict)
        or set(expected_schema) != {"name", "version"}
        or not isinstance(expected_schema["name"], str)
        or not isinstance(expected_schema["version"], str)
    ):
        raise TargetSeriesError(error)
    expected_adapter = CORE_GENERATION_ADAPTER_BY_SOURCE_SCHEMA.get(
        (expected_schema.get("name"), expected_schema.get("version"))
    )
    if expected_adapter is None:
        raise TargetSeriesError(error)
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_content)
    except ManifestValidationError as exc:
        raise TargetSeriesError(error) from exc
    if (
        manifest.adapter.name != expected_adapter
        or manifest.verification is not VerificationStatus.VERIFIED
    ):
        raise TargetSeriesError(error)
    return manifest


def _verified_current_generation(
    *,
    pointer_path: Path,
    manifest_path: Path,
    source: Mapping[str, Any],
    name: str,
    inventory_profile: str,
) -> tuple[dict[str, Any], str, str]:
    pointer_content = pointer_path.read_bytes()
    pointer = _load_json(pointer_path, f"{name} current pointer")
    manifest = _load_json(manifest_path, f"{name} generation manifest")
    manifest_content = manifest_path.read_bytes()
    current = source.get("current_generation")
    if not isinstance(current, dict):
        raise TargetSeriesError(f"{name} current generation is absent from contract")
    _verified_core_generation_envelope(
        manifest_content,
        source,
        f"{name} generation manifest differs from contract",
    )
    generation_id = str(manifest.get("generation_id", ""))
    if (
        manifest.get("verification") != "VERIFIED"
        or len(generation_id) != 64
        or pointer.get("generation_id") != generation_id
        or current.get("generation_id") != generation_id
    ):
        raise TargetSeriesError(f"{name} current generation identity differs")
    manifest_hash, _manifest_size = _sha256(manifest_path)
    if (
        current.get("sha256") != manifest_hash
        or pointer.get("manifest_sha256") != manifest_hash
        or pointer.get("state") != "PUBLISHED"
        or canonical_json_bytes(pointer) != pointer_content
    ):
        raise TargetSeriesError(f"{name} generation manifest differs from contract")
    pointer_schema_version = pointer.get("pointer_schema_version")
    expected_pointer_keys = {
        "artifact_inventory_sha256",
        "generation_id",
        "manifest_sha256",
        "pointer_schema_version",
        "state",
    }
    if set(pointer) != expected_pointer_keys or pointer_schema_version != 2:
        raise TargetSeriesError(f"{name} current pointer contract differs")
    inventory_sha256 = _source_generation_inventory_sha256(
        inventory_profile, manifest_content, manifest
    )
    if pointer.get("artifact_inventory_sha256") != inventory_sha256:
        raise TargetSeriesError(f"{name} current pointer inventory differs")
    generation_dir = pointer_path.parent / "generations" / generation_id
    if generation_dir.exists() and (
        _generation_directory_inventory_sha256(generation_dir) != inventory_sha256
    ):
        raise TargetSeriesError(f"{name} published generation inventory differs")
    as_of = str(manifest.get("as_of_date", ""))
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise TargetSeriesError(f"{name} generation as-of date is invalid") from exc
    if current.get("as_of") != as_of:
        raise TargetSeriesError(f"{name} generation as-of differs from contract")
    return manifest, generation_id, as_of


def _verified_immutable_generation(
    *,
    manifest_path: Path,
    source: Mapping[str, Any],
    name: str,
) -> tuple[dict[str, Any], str, str]:
    """Bind a retained generation directly to its contract pin, without a pointer."""

    manifest_content = manifest_path.read_bytes()
    manifest = _load_json(manifest_path, f"{name} generation manifest")
    current = source.get("current_generation")
    generation_id = str(manifest.get("generation_id", ""))
    manifest_hash = hashlib.sha256(manifest_content).hexdigest()
    as_of = str(manifest.get("as_of_date", ""))
    common_valid = (
        manifest_content == canonical_json_bytes(manifest)
        and isinstance(current, dict)
        and len(generation_id) == 64
        and current.get("generation_id") == generation_id
        and current.get("sha256") == manifest_hash
        and current.get("as_of") == as_of
    )
    _verified_core_generation_envelope(
        manifest_content,
        source,
        f"{name} immutable generation differs from contract",
    )
    if not common_valid:
        raise TargetSeriesError(f"{name} immutable generation differs from contract")
    try:
        observed_as_of = date.fromisoformat(as_of)
    except ValueError as exc:
        raise TargetSeriesError(f"{name} generation as-of date is invalid") from exc
    if observed_as_of.isoformat() != as_of:
        raise TargetSeriesError(f"{name} generation as-of date is invalid")
    return manifest, generation_id, as_of


def _matrixport_provider_returns(
    nav_path: Path,
    product_id: str,
    series_id: str,
    account_live_start: date,
) -> list[dict[str, str]]:
    observations: dict[date, Decimal] = {}
    for row in _csv_rows(nav_path, MATRIXPORT_NAV_FIELDS, "Matrixport daily NAV"):
        if row["product_id"] != product_id:
            continue
        day = _utc_day(row["timestamp_utc"])
        nav = _positive_decimal(row["nav"], "Matrixport NAV")
        if day in observations and observations[day] != nav:
            raise TargetSeriesError(
                "Matrixport NAV contains conflicting duplicate dates"
            )
        observations[day] = nav
    ordered = sorted(observations.items())
    rows: list[dict[str, str]] = []
    for (previous_day, previous), (day, nav) in zip(ordered, ordered[1:], strict=False):
        if previous_day >= account_live_start or day >= account_live_start:
            continue
        if (day - previous_day).days != 1:
            continue
        value = nav / previous - Decimal(1)
        if value <= -1:
            raise TargetSeriesError("Matrixport provider return is invalid")
        rows.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "PROVIDER_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
    if not rows:
        raise TargetSeriesError("Matrixport provider-live history is unavailable")
    return rows


def _matrixport_account_live_from_ledger(
    ledger_rows: Sequence[Mapping[str, str]],
    product_rows: Sequence[Mapping[str, str]],
    unavailable_return_dates: frozenset[date],
) -> list[dict[str, str]]:
    """Select canonical account returns after independent product-NAV proof."""

    if not product_rows:
        raise TargetSeriesError("Matrixport account-live history is unavailable")
    product_by_date = {row["date"]: row for row in product_rows}
    if len(product_by_date) != len(product_rows):
        raise TargetSeriesError("Matrixport product return date is duplicated")
    observed_start = product_rows[0]["date"]
    observed_end = product_rows[-1]["date"]
    ledger_by_date: dict[str, Mapping[str, str]] = {}
    for row in ledger_rows:
        observed = row["date"]
        if observed < observed_start or observed > observed_end:
            continue
        if observed in ledger_by_date:
            raise TargetSeriesError("Matrixport provider-ledger return date is duplicated")
        ledger_by_date[observed] = row

    product_dates = set(product_by_date)
    ledger_dates = set(ledger_by_date)
    unavailable = {item.isoformat() for item in unavailable_return_dates}
    if product_dates - ledger_dates:
        raise TargetSeriesError("Matrixport provider-ledger account return is missing")
    if ledger_dates - product_dates - unavailable:
        raise TargetSeriesError(
            "Matrixport provider-ledger return exceeds the declared gap boundary"
        )

    selected: list[dict[str, str]] = []
    with localcontext() as context:
        context.prec = 50
        for product_row in product_rows:
            ledger_row = ledger_by_date[product_row["date"]]
            difference = abs(
                Decimal(ledger_row["return"]) - Decimal(product_row["return"])
            )
            if difference > MATRIXPORT_ACCOUNT_RETURN_RECONCILIATION_MAX_ERROR:
                raise TargetSeriesError(
                    "Matrixport account-live return differs from provider ledger"
                )
            selected.append(dict(ledger_row))
    return selected


def _quantbase_account_live_from_ledger(
    ledger_rows: Sequence[Mapping[str, str]],
    recalculated_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Select ledger rows only after exact independent Quantbase recalculation."""

    ledger_by_date = {row["date"]: row for row in ledger_rows}
    recalculated_by_date = {row["date"]: row for row in recalculated_rows}
    if len(ledger_by_date) != len(ledger_rows) or len(recalculated_by_date) != len(
        recalculated_rows
    ):
        raise TargetSeriesError("Quantbase account-live return date is duplicated")
    if set(ledger_by_date) != set(recalculated_by_date):
        raise TargetSeriesError(
            "Quantbase provider-ledger account return date perimeter differs"
        )

    selected: list[dict[str, str]] = []
    for observed in sorted(recalculated_by_date):
        ledger_row = ledger_by_date[observed]
        recalculated_row = recalculated_by_date[observed]
        if Decimal(ledger_row["return"]) != Decimal(recalculated_row["return"]):
            raise TargetSeriesError(
                "Quantbase account-live return differs from provider ledger"
            )
        selected.append(dict(ledger_row))
    return selected


def _validate_robuxio_provider_interval(
    rows: Sequence[Mapping[str, str]], interval: Mapping[str, Any]
) -> None:
    if not rows:
        raise TargetSeriesError("Matrixport provider-live history is unavailable")
    try:
        provider_observed_start = date.fromisoformat(str(interval["observed_start"]))
        provider_observed_end = date.fromisoformat(str(interval["observed_end"]))
        first_return = date.fromisoformat(rows[0]["date"])
        last_return = date.fromisoformat(rows[-1]["date"])
    except (KeyError, ValueError) as exc:
        raise TargetSeriesError("Robuxio provider-live interval is invalid") from exc
    if first_return <= provider_observed_start or last_return != provider_observed_end:
        raise TargetSeriesError("Robuxio provider-live interval differs from contract")


def _weekday_dates(start: date, end: date) -> list[date]:
    if end < start:
        raise TargetSeriesError("provider performance interval is reversed")
    result: list[date] = []
    observed = start
    while observed <= end:
        if observed.weekday() < 5:
            result.append(observed)
        observed += timedelta(days=1)
    return result


def _portfolio123_exact_model(
    metadata_path: Path,
) -> tuple[set[str], str]:
    rows = _parquet_rows(
        metadata_path,
        (
            "assetName",
            "assetUid",
            "currency",
            "inceptionDt",
            "isAccountLive",
            "name",
            "portfolioUid",
            "posCnt",
            "strategyTotalHoldCount",
        ),
        "Portfolio123 managed metadata",
    )
    matched = [
        row
        for row in rows
        if "eu multifactor microcap" in str(row.get("name") or "").casefold()
        and str(row.get("isAccountLive") or "").casefold() == "true"
    ]
    signatures: dict[str, dict[str, object]] = {}
    for row in matched:
        signature = {
            field: row.get(field)
            for field in (
                "assetName",
                "assetUid",
                "currency",
                "inceptionDt",
                "isAccountLive",
                "name",
                "portfolioUid",
                "posCnt",
                "strategyTotalHoldCount",
            )
        }
        signatures[hashlib.sha256(canonical_json_bytes(signature)).hexdigest()] = row
    if len(signatures) != 1:
        raise TargetSeriesError("Portfolio123 exact EU model is not unique")
    exact = next(iter(signatures.values()))
    entity_ids = {
        str(exact.get(field) or "").strip()
        for field in ("assetUid", "portfolioUid")
        if str(exact.get(field) or "").strip()
    }
    if not entity_ids:
        raise TargetSeriesError("Portfolio123 exact EU model lacks identity")
    version = _strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": "__GENERATION_BOUND_BY_CALLER__",
            "provider_entity_ids": sorted(entity_ids),
            "strategy_name": str(exact.get("assetName") or ""),
        },
    )
    return entity_ids, version


def _portfolio123_provider_performance_returns(
    *,
    metadata_path: Path,
    performance_path: Path,
    equity_path: Path,
    generation_id: str,
    series_id: str,
    accepted_version: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    entity_ids, placeholder_version = _portfolio123_exact_model(metadata_path)
    # Recompute the exact accepted version with the current generation.  The
    # private provider identifiers participate only in this in-memory digest.
    metadata_rows = _parquet_rows(
        metadata_path,
        (
            "assetName",
            "assetUid",
            "currency",
            "inceptionDt",
            "isAccountLive",
            "name",
            "portfolioUid",
            "posCnt",
            "strategyTotalHoldCount",
        ),
        "Portfolio123 managed metadata",
    )
    exact = next(
        row
        for row in metadata_rows
        if "eu multifactor microcap" in str(row.get("name") or "").casefold()
        and str(row.get("isAccountLive") or "").casefold() == "true"
    )
    computed_version = _strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": generation_id,
            "provider_entity_ids": sorted(entity_ids),
            "strategy_name": str(exact.get("assetName") or ""),
        },
    )
    if placeholder_version == computed_version or computed_version != accepted_version:
        raise TargetSeriesError("Portfolio123 exact EU strategy version differs")

    performance_rows = [
        row
        for row in _parquet_rows(
            performance_path,
            ("_entity_id", "field", "value"),
            "Portfolio123 managed performance",
        )
        if str(row.get("_entity_id") or "") in entity_ids
    ]
    captures: dict[str, dict[str, str]] = {}
    by_entity: dict[str, dict[str, str]] = {}
    for row in performance_rows:
        entity_id = str(row.get("_entity_id") or "")
        field = str(row.get("field") or "")
        value = str(row.get("value") or "")
        fields = by_entity.setdefault(entity_id, {})
        prior = fields.get(field)
        if prior is not None and prior != value:
            raise TargetSeriesError(
                "Portfolio123 managed performance has conflicting captures"
            )
        fields[field] = value
    for fields in by_entity.values():
        if not {"data", "date", "dateLast", "panels"} <= set(fields):
            continue
        signature = hashlib.sha256(
            canonical_json_bytes(
                {key: fields[key] for key in ("data", "date", "dateLast", "panels")}
            )
        ).hexdigest()
        captures[signature] = fields
    if len(captures) != 1:
        raise TargetSeriesError("Portfolio123 exact EU performance is not unique")
    fields = next(iter(captures.values()))
    try:
        observed_start = date.fromisoformat(fields["date"])
        observed_end = date.fromisoformat(fields["dateLast"])
        data = json.loads(fields["data"])
        panels = json.loads(fields["panels"])
    except (ValueError, json.JSONDecodeError) as exc:
        raise TargetSeriesError("Portfolio123 exact EU performance is invalid") from exc
    if not isinstance(data, list) or not isinstance(panels, list):
        raise TargetSeriesError("Portfolio123 exact EU performance shape differs")
    graphs = [
        graph
        for panel in panels
        if isinstance(panel, dict)
        for graph in panel.get("graphs", [])
        if isinstance(graph, dict)
    ]
    if not any(
        graph.get("idx") == 0 and graph.get("s") == "Strategy" for graph in graphs
    ):
        raise TargetSeriesError("Portfolio123 strategy performance selector differs")
    if not any(
        graph.get("idx") == 1 and graph.get("s") == "Total Value" for graph in graphs
    ):
        raise TargetSeriesError("Portfolio123 total-value selector differs")
    calendar = _weekday_dates(observed_start, observed_end)
    if len(calendar) != len(data):
        raise TargetSeriesError("Portfolio123 performance calendar differs")
    parsed: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for day, raw in zip(calendar, data, strict=True):
        if not isinstance(raw, list) or len(raw) != 3:
            raise TargetSeriesError("Portfolio123 performance observation differs")
        strategy_level = _decimal(raw[0], "Portfolio123 strategy performance")
        total_equity = _positive_decimal(raw[1], "Portfolio123 total value")
        cash_added = _decimal(raw[2], "Portfolio123 cash added")
        wealth = Decimal(1) + strategy_level / Decimal(100)
        if wealth <= 0:
            raise TargetSeriesError("Portfolio123 strategy performance is invalid")
        parsed.append((day, wealth, total_equity, cash_added))

    equity_rows = [
        row
        for row in _parquet_rows(
            equity_path,
            ("_entity_id", "Date", "Total Equity", "Cash Added"),
            "Portfolio123 equity curve",
        )
        if str(row.get("_entity_id") or "") in entity_ids
        and row.get("Total Equity") not in (None, "")
    ]
    equity_by_day: dict[date, tuple[Decimal, Decimal]] = {}
    for row in equity_rows:
        try:
            day = date.fromisoformat(str(row.get("Date") or ""))
        except ValueError as exc:
            raise TargetSeriesError("Portfolio123 equity date is invalid") from exc
        equity_identity = (
            _positive_decimal(row.get("Total Equity"), "Portfolio123 total equity"),
            _decimal(row.get("Cash Added") or 0, "Portfolio123 cash added"),
        )
        if day in equity_by_day and equity_by_day[day] != equity_identity:
            raise TargetSeriesError("Portfolio123 equity curve conflicts")
        equity_by_day[day] = equity_identity
    overlap = 0
    for day, _wealth, total_equity, cash_added in parsed:
        if day not in equity_by_day:
            continue
        overlap += 1
        if equity_by_day[day] != (total_equity, cash_added):
            raise TargetSeriesError("Portfolio123 performance and equity paths differ")
    if overlap < 2:
        raise TargetSeriesError(
            "Portfolio123 performance/equity overlap is insufficient"
        )

    returns: list[dict[str, str]] = []
    for previous, current in zip(parsed, parsed[1:], strict=False):
        period_return = current[1] / previous[1] - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Portfolio123 provider return is invalid")
        returns.append(
            {
                "series_id": series_id,
                "date": current[0].isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "PROVIDER_LIVE",
                "return_basis": "PROVIDER_COMPUTED_STRATEGY_PERFORMANCE",
                "return_type": "TOTAL",
                "cost": "",
            }
        )
    return returns, {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": overlap,
        "observed_end": returns[-1]["date"],
        "observed_start": returns[0]["date"],
        "raw_level_observations": len(parsed),
        "return_observations": len(returns),
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }


def _correlation_view_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=CORRELATION_FIELDS_V2, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(
        {field: row[field] for field in CORRELATION_FIELDS_V2} for row in rows
    )
    return buffer.getvalue().encode("utf-8")


def _segment_view_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=LAYERED_HISTORY_SEGMENT_FIELDS, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(
        {field: row[field] for field in LAYERED_HISTORY_SEGMENT_FIELDS} for row in rows
    )
    return buffer.getvalue().encode("utf-8")


def _source_temporal(source: Mapping[str, Any]) -> tuple[str, str]:
    temporal = source.get("temporal")
    if not isinstance(temporal, dict):
        raise TargetSeriesError("history source temporal contract is absent")
    calendar = str(temporal.get("calendar", "")).strip()
    frequency = str(temporal.get("cadence", "")).strip()
    if not calendar or not frequency:
        raise TargetSeriesError("history source temporal contract is incomplete")
    return calendar, frequency


def _source_generation(
    source: Mapping[str, Any], name: str
) -> tuple[str | None, str | None]:
    current = source.get("current_generation")
    if not isinstance(current, dict):
        raise TargetSeriesError(f"{name} generation contract is absent")
    generation_id = current.get("generation_id")
    as_of = current.get("as_of")
    if generation_id is not None and (
        not isinstance(generation_id, str) or len(generation_id) != 64
    ):
        raise TargetSeriesError(f"{name} generation identity is invalid")
    if as_of is not None:
        try:
            date.fromisoformat(str(as_of))
        except ValueError as exc:
            raise TargetSeriesError(f"{name} generation as-of is invalid") from exc
    return generation_id, None if as_of is None else str(as_of)


def _contract_version(strategy: Mapping[str, Any]) -> tuple[str, str]:
    value = strategy.get("strategy_version")
    if not isinstance(value, dict):
        raise TargetSeriesError("strategy version contract is absent")
    status = str(value.get("status", "")).strip()
    version = value.get("version_ref")
    if status == "EVIDENCED":
        if not isinstance(version, str) or ":" not in version:
            raise TargetSeriesError("evidenced strategy version is invalid")
        return status, version
    if status == "NOT_APPLICABLE":
        if not isinstance(version, str) or ":" not in version:
            raise TargetSeriesError("not-applicable strategy version is invalid")
        return status, version
    if status != "UNRESOLVED" or version is not None:
        raise TargetSeriesError("strategy version status differs")
    return status, "UNRESOLVED"


def _segment_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    strategy_id: str,
    strategy_version: str,
    segment_id: str,
    source_id: str,
    source_generation_id: str | None,
    mode_admission: str,
    calendar: str,
    frequency: str,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for row in rows:
        result.append(
            {
                "calendar": calendar,
                "date": row["date"],
                "evidence_layer": row["evidence_layer"],
                "frequency": frequency,
                "mode_admission": mode_admission,
                "return": row["return"],
                "return_basis": row.get("return_basis", "OBSERVED"),
                "return_type": row.get("return_type", "TOTAL"),
                "segment_id": segment_id,
                "series_id": row["series_id"],
                "source_generation_id": source_generation_id or "NOT_CAPTURED",
                "source_id": source_id,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
            }
        )
    return result


def _row_identity(rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    if not rows:
        return {
            "observed_end": None,
            "observed_start": None,
            "return_observations": 0,
            "sha256": hashlib.sha256(_correlation_view_bytes([])).hexdigest(),
        }
    ordered = sorted(rows, key=lambda row: (row["series_id"], row["date"]))
    return {
        "observed_end": ordered[-1]["date"],
        "observed_start": ordered[0]["date"],
        "return_observations": len(ordered),
        "sha256": hashlib.sha256(_correlation_view_bytes(ordered)).hexdigest(),
    }


def _materialized_segment_identities(
    rows: Sequence[Mapping[str, str]],
    sources: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, object]]:
    """Bind each value-bearing segment to its current governed source receipt."""

    result: list[dict[str, object]] = []
    for segment_id in sorted({row["segment_id"] for row in rows}):
        selected = [row for row in rows if row["segment_id"] == segment_id]
        first = selected[0]
        static_fields = (
            "calendar",
            "evidence_layer",
            "frequency",
            "mode_admission",
            "return_basis",
            "return_type",
            "segment_id",
            "source_generation_id",
            "source_id",
            "strategy_id",
            "strategy_version",
        )
        if any(
            tuple(row[field] for field in static_fields)
            != tuple(first[field] for field in static_fields)
            for row in selected
        ):
            raise TargetSeriesError(
                f"layered history segment has mixed authority: {segment_id}"
            )
        source = sources.get(first["source_id"])
        generation = source.get("current_generation") if source is not None else None
        if not isinstance(generation, dict) or (
            generation.get("generation_id") != first["source_generation_id"]
            or not isinstance(generation.get("sha256"), str)
        ):
            raise TargetSeriesError(
                f"layered history segment source receipt differs: {segment_id}"
            )
        identity = _row_identity(selected)
        result.append(
            {
                "calendar": first["calendar"],
                "canonicalization": "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1",
                "evidence_layer": first["evidence_layer"],
                "frequency": first["frequency"],
                "mode_admission": first["mode_admission"],
                "observed_end": identity["observed_end"],
                "observed_start": identity["observed_start"],
                "return_basis": first["return_basis"],
                "return_observations": identity["return_observations"],
                "return_type": first["return_type"],
                "segment_id": segment_id,
                "sha256": identity["sha256"],
                "source_generation_id": first["source_generation_id"],
                "source_generation_sha256": generation["sha256"],
                "source_id": first["source_id"],
                "strategy_id": first["strategy_id"],
                "strategy_version": first["strategy_version"],
            }
        )
    return result


def _target_row_identity(rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    """Hash rejected target rows without inventing a strategy-version identity."""

    ordered = sorted(rows, key=lambda row: (row["series_id"], row["date"]))
    return {
        "observed_end": ordered[-1]["date"] if ordered else None,
        "observed_start": ordered[0]["date"] if ordered else None,
        "return_observations": len(ordered),
        "sha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "rows": [
                        {field: row[field] for field in TARGET_FIELDS}
                        for row in ordered
                    ]
                }
            )
        ).hexdigest(),
    }


def _gap_disposition(
    rows: Sequence[Mapping[str, str]],
    declared_unavailable_dates: frozenset[date] = frozenset(),
) -> str:
    """Classify observable continuity without inventing a market calendar."""

    dates = sorted({date.fromisoformat(row["date"]) for row in rows})
    if len(dates) < 2:
        return "REQUIRED_NOT_READY_INSUFFICIENT_CONTINUITY_EVIDENCE"
    if declared_unavailable_dates:
        actual_missing: set[date] = set()
        candidate = dates[0] + timedelta(days=1)
        observed = set(dates)
        while candidate < dates[-1]:
            if candidate not in observed:
                actual_missing.add(candidate)
            candidate += timedelta(days=1)
        expected_missing = {
            item for item in declared_unavailable_dates if dates[0] < item < dates[-1]
        }
        if actual_missing == expected_missing:
            return "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        return "REQUIRED_NOT_READY_UNEXPLAINED_INTERIOR_GAP"
    if all(
        (current - previous).days == 1 for previous, current in zip(dates, dates[1:])
    ):
        return "COMPLETE_CONSECUTIVE_CALENDAR_DAYS"
    if all(day.weekday() < 5 for day in dates) and dates == _weekday_dates(
        dates[0], dates[-1]
    ):
        return "COMPLETE_CONSECUTIVE_WEEKDAYS"
    return "REQUIRED_NOT_READY_UNEXPLAINED_INTERIOR_GAP"


def _p123_ai_backtest(
    *,
    evidence: Mapping[str, Any],
    returns_path: Path,
    relationships_path: Path,
    generation_id: str,
    relationships_sha256: str,
    strategy_evidence_sha256: str,
    series_id: str,
) -> tuple[list[dict[str, str]], str, dict[str, object]]:
    provider = evidence.get("provider")
    if not isinstance(provider, dict) or provider.get("provider_id") != "portfolio123":
        raise TargetSeriesError("Portfolio123 AI provider evidence is absent")
    provider_entity_id = str(provider.get("entity_id", "")).strip()
    if not provider_entity_id:
        raise TargetSeriesError("Portfolio123 AI provider identity is absent")
    twins: set[str] = set()
    for row in _parquet_rows(
        relationships_path,
        ("entity_ref_a", "entity_ref_b", "relationship_type", "notes"),
        "Portfolio123 strategy relationships",
    ):
        if row.get("relationship_type") != "live_sim_twin":
            continue
        left = str(row.get("entity_ref_a") or "")
        right = str(row.get("entity_ref_b") or "")
        if provider_entity_id == left and right:
            twins.add(right)
        if provider_entity_id == right and left:
            twins.add(left)
    if len(twins) != 1:
        raise TargetSeriesError("Portfolio123 AI live/simulation twin is not unique")
    simulation_entity_id = next(iter(twins))
    rows, coverage = _portfolio123_simulation_returns(
        returns_path,
        relationships_path,
        provider_entity_id,
        simulation_entity_id,
        series_id,
    )
    version = _strategy_version(
        "p123-live-sim-twin",
        {
            "generation_id": generation_id,
            "provider_entity_id": provider_entity_id,
            "relationship_sha256": relationships_sha256,
            "relationship_type": "live_sim_twin",
            "simulation_entity_id": simulation_entity_id,
            "strategy_evidence_sha256": strategy_evidence_sha256,
        },
    )
    return rows, version, coverage


def _history_output_identity(
    name: str, content: bytes, row_count: int
) -> dict[str, object]:
    schema_name = {
        "segments.csv": "portfolio-layered-history-segments",
        "excluded.csv": "portfolio-layered-history-excluded-targets",
        "returns.csv": "portfolio-layered-strategy-returns",
        "live-only.csv": "portfolio-layered-strategy-returns",
        "live-plus-backtest.csv": "portfolio-layered-strategy-returns",
        "coverage.json": "portfolio-layered-history-coverage",
    }[name]
    return {
        "artifact_id": f"layered-history/{name}",
        "row_count": row_count,
        "schema_name": schema_name,
        "schema_version": "2"
        if name in {"returns.csv", "live-only.csv", "live-plus-backtest.csv"}
        else "1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def build_layered_history_generation(
    *,
    contract_path: Path,
    repository_sha: str,
    as_of_date: str,
    provider_ledger_pointer_path: Path | None,
    provider_ledger_manifest_path: Path,
    provider_ledger_catalog_path: Path,
    matrixport_pointer_path: Path | None,
    matrixport_manifest_path: Path,
    matrixport_nav_path: Path,
    matrixport_holdings_path: Path,
    matrixport_daily_holdings_path: Path,
    matrixport_subscription_path: Path,
    matrixport_product_id: str,
    quantbase_pointer_path: Path | None,
    quantbase_manifest_path: Path,
    quantbase_history_path: Path,
    quantbase_activity_path: Path,
    quantbase_fund_id: str,
    portfolio123_pointer_path: Path | None,
    portfolio123_manifest_path: Path,
    portfolio123_metadata_path: Path,
    portfolio123_performance_path: Path,
    portfolio123_equity_path: Path,
    portfolio123_returns_path: Path,
    portfolio123_relationships_path: Path,
    strategy_history_manifest_path: Path | None = None,
    strategy_history_returns_path: Path | None = None,
    source_selection_mode: str = "CURRENT_POINTERS",
) -> tuple[dict[str, bytes], bytes]:
    """Build one deterministic, content-addressed current history generation.

    Private provider identifiers are used only in memory.  The emitted receipt
    contains hashes, redacted strategy identities, coverage states, and source
    generation identities; it never contains roots or provider/account IDs.
    """

    pointer_paths = {
        "provider ledger": provider_ledger_pointer_path,
        "Matrixport": matrixport_pointer_path,
        "Quantbase": quantbase_pointer_path,
        "Portfolio123": portfolio123_pointer_path,
    }
    if source_selection_mode not in {"CURRENT_POINTERS", "IMMUTABLE_GENERATIONS"}:
        raise TargetSeriesError("layered history source selection mode differs")
    if source_selection_mode == "IMMUTABLE_GENERATIONS" and any(
        path is not None for path in pointer_paths.values()
    ):
        raise TargetSeriesError("immutable generations do not accept current pointers")
    if source_selection_mode == "CURRENT_POINTERS" and any(
        path is None for path in pointer_paths.values()
    ):
        raise TargetSeriesError("current-pointer mode requires all current pointers")
    if source_selection_mode == "CURRENT_POINTERS":
        assert provider_ledger_pointer_path is not None
        assert matrixport_pointer_path is not None
        assert quantbase_pointer_path is not None
        assert portfolio123_pointer_path is not None
    if len(repository_sha) != 40 or any(
        ch not in "0123456789abcdef" for ch in repository_sha
    ):
        raise TargetSeriesError("repository SHA must be lowercase hexadecimal")
    trusted_repository_sha, _trusted_tool_sha256 = (
        _trusted_layered_history_code_identity()
    )
    if repository_sha != trusted_repository_sha:
        raise TargetSeriesError("repository SHA differs from the executing checkout")
    try:
        accepted_as_of = date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise TargetSeriesError("layered history as-of date is invalid") from exc
    contract, strategies, sources, corrections = _history_contract(contract_path)
    product = contract["product"]
    history_policy = contract["history_corrections"]

    if source_selection_mode == "CURRENT_POINTERS":
        ledger_manifest, ledger_generation, ledger_as_of = _verified_current_generation(
            pointer_path=provider_ledger_pointer_path,
            manifest_path=provider_ledger_manifest_path,
            source=sources["owner-account-live-history"],
            name="provider ledger",
            inventory_profile="provider-ledger",
        )
        matrixport_manifest, matrixport_generation, matrixport_as_of = (
            _verified_current_generation(
                pointer_path=matrixport_pointer_path,
                manifest_path=matrixport_manifest_path,
                source=sources["matrixport-account-generation"],
                name="Matrixport",
                inventory_profile="matrixport",
            )
        )
        quantbase_manifest, quantbase_generation, quantbase_as_of = (
            _verified_current_generation(
                pointer_path=quantbase_pointer_path,
                manifest_path=quantbase_manifest_path,
                source=sources["quantbase-account-generation"],
                name="Quantbase",
                inventory_profile="quantbase",
            )
        )
        p123_manifest, p123_generation, p123_as_of = _verified_current_generation(
            pointer_path=portfolio123_pointer_path,
            manifest_path=portfolio123_manifest_path,
            source=sources["portfolio123-model-generation"],
            name="Portfolio123",
            inventory_profile="portfolio123",
        )
    else:
        ledger_manifest, ledger_generation, ledger_as_of = _verified_immutable_generation(
            manifest_path=provider_ledger_manifest_path,
            source=sources["owner-account-live-history"],
            name="provider ledger",
        )
        matrixport_manifest, matrixport_generation, matrixport_as_of = (
            _verified_immutable_generation(
                manifest_path=matrixport_manifest_path,
                source=sources["matrixport-account-generation"],
                name="Matrixport",
            )
        )
        quantbase_manifest, quantbase_generation, quantbase_as_of = (
            _verified_immutable_generation(
                manifest_path=quantbase_manifest_path,
                source=sources["quantbase-account-generation"],
                name="Quantbase",
            )
        )
        p123_manifest, p123_generation, p123_as_of = _verified_immutable_generation(
            manifest_path=portfolio123_manifest_path,
            source=sources["portfolio123-model-generation"],
            name="Portfolio123",
        )
    if (
        max(
            map(
                date.fromisoformat,
                (ledger_as_of, matrixport_as_of, quantbase_as_of, p123_as_of),
            )
        )
        > accepted_as_of
    ):
        raise TargetSeriesError("source generation exceeds layered history as-of")

    targets, strategy_evidence_content, ledger_proof_content, _manifest_content = (
        build_provider_ledger_targets(
            provider_ledger_manifest_path, provider_ledger_catalog_path
        )
    )
    ledger_proof = json.loads(ledger_proof_content)
    contracts = ledger_proof.get("series_contracts")
    if not isinstance(contracts, list):
        raise TargetSeriesError("provider-ledger series contracts are absent")
    series_by_strategy: dict[str, str] = {}
    for item in contracts:
        if not isinstance(item, dict):
            raise TargetSeriesError("provider-ledger series contract is invalid")
        strategy_id = str(item.get("strategy_id", ""))
        series_id = str(item.get("series_id", ""))
        if not strategy_id or not series_id or strategy_id in series_by_strategy:
            raise TargetSeriesError("provider-ledger strategy mapping differs")
        series_by_strategy[strategy_id] = series_id
    unknown_ledger = set(series_by_strategy) - set(strategies)
    if unknown_ledger:
        raise TargetSeriesError("provider ledger contains non-product strategy")
    product_series_by_strategy: dict[str, str] = {}
    for strategy_id, strategy in strategies.items():
        history_series_id = strategy.get("history_series_id")
        if history_series_id is None:
            continue
        if not isinstance(history_series_id, str) or not history_series_id.strip():
            raise TargetSeriesError("product history series identity is invalid")
        product_series_by_strategy[strategy_id] = history_series_id
    if series_by_strategy != product_series_by_strategy:
        raise TargetSeriesError(
            "provider-ledger series mapping differs from product authority"
        )

    evidence_value = json.loads(strategy_evidence_content)
    evidence_rows = evidence_value.get("strategies")
    if not isinstance(evidence_rows, list):
        raise TargetSeriesError("provider-ledger strategy evidence is absent")
    evidence_by_strategy = {
        str(item.get("strategy_id")): item
        for item in evidence_rows
        if isinstance(item, dict) and item.get("strategy_id")
    }

    live_by_strategy: dict[str, list[dict[str, str]]] = {
        strategy_id: [] for strategy_id in strategies
    }
    series_to_strategy = {
        series_id: strategy_id for strategy_id, series_id in series_by_strategy.items()
    }
    for row in _target_rows(targets):
        mapped_strategy_id = series_to_strategy.get(row["series_id"])
        if mapped_strategy_id is None:
            raise TargetSeriesError("provider-ledger target lacks strategy mapping")
        if row["evidence_layer"] != "ACCOUNT_LIVE":
            raise TargetSeriesError(
                "provider-ledger selected target is not account-live"
            )
        if date.fromisoformat(row["date"]) > accepted_as_of:
            raise TargetSeriesError("provider-ledger return exceeds accepted as-of")
        live_by_strategy[mapped_strategy_id].append(dict(row))
    echo_correction = corrections.get("echotrade-event-only-account-live-rejected")
    if not isinstance(echo_correction, dict):
        raise TargetSeriesError("EchoTrade event-only disposition is absent")
    echo_candidate = sorted(
        live_by_strategy["echotrade-growth-pivots"], key=lambda row: row["date"]
    )
    echo_interval = echo_correction.get("interval")
    echo_capture = echo_correction.get("capture")
    echo_modes = echo_correction.get("mode_admission")
    if (
        not echo_candidate
        or not isinstance(echo_interval, dict)
        or not isinstance(echo_capture, dict)
        or not isinstance(echo_modes, dict)
        or echo_correction.get("source_id") != "owner-account-live-history"
        or echo_correction.get("evidence_layer") != "ACCOUNT_LIVE"
        or echo_correction.get("readiness") != "REQUIRED_NOT_READY"
        or echo_modes.get("LIVE_ONLY") != "EXCLUDE_EVENT_ONLY_RECONSTRUCTION"
        or echo_modes.get("LIVE_PLUS_BACKTEST") != "EXCLUDE_EVENT_ONLY_RECONSTRUCTION"
        or echo_capture.get("generation_id") != ledger_generation
        or (echo_candidate[0]["date"], echo_candidate[-1]["date"])
        != (echo_interval.get("observed_start"), echo_interval.get("observed_end"))
    ):
        raise TargetSeriesError("EchoTrade event-only disposition differs")
    echo_rejected_identity = _target_row_identity(echo_candidate)
    echo_expected_identity = echo_correction.get("rejected_evidence_identity")
    provider_ledger_catalog_sha = hashlib.sha256(
        provider_ledger_catalog_path.read_bytes()
    ).hexdigest()
    if (
        not isinstance(echo_expected_identity, dict)
        or set(echo_expected_identity)
        != {
            "canonicalization",
            "return_observations",
            "sha256",
            "source_receipt_sha256",
        }
        or echo_expected_identity.get("canonicalization")
        != "TARGET_FIELDS_CANONICAL_JSON_V1"
        or {
            "return_observations": echo_expected_identity.get("return_observations"),
            "sha256": echo_expected_identity.get("sha256"),
        }
        != {
            key: echo_rejected_identity[key]
            for key in ("return_observations", "sha256")
        }
        or echo_expected_identity.get("source_receipt_sha256")
        != provider_ledger_catalog_sha
    ):
        raise TargetSeriesError(
            "EchoTrade rejected evidence differs from product authority"
        )
    live_by_strategy["echotrade-growth-pivots"] = []
    matrixport_artifacts = _manifest_artifacts(matrixport_manifest.get("artifacts"))
    _verify_artifact(matrixport_nav_path, matrixport_artifacts, "csv/nav_daily.csv")
    _verify_artifact(matrixport_holdings_path, matrixport_artifacts, "csv/holdings.csv")
    _verify_artifact(
        matrixport_daily_holdings_path,
        matrixport_artifacts,
        "csv/daily_holdings.csv",
    )
    _verify_artifact(
        matrixport_subscription_path,
        matrixport_artifacts,
        "csv/subscription_details.csv",
    )
    matrixport_gap_policy = sources["matrixport-account-generation"].get(
        "declared_gap_policy"
    )
    if not isinstance(matrixport_gap_policy, dict):
        raise TargetSeriesError("Matrixport product gap policy is absent")
    _matrixport_return_transform(matrixport_gap_policy)
    matrixport_missing_dates = _matrixport_declared_missing_dates(
        matrixport_manifest, matrixport_gap_policy
    )
    robuxio_correction = corrections["robuxio-account-live-after-funding"]
    robuxio_interval = robuxio_correction.get("interval")
    if not isinstance(robuxio_interval, dict):
        raise TargetSeriesError("Robuxio account-live interval is absent from contract")
    try:
        matrixport_admission_cutoff = date.fromisoformat(
            str(robuxio_interval.get("observed_end", ""))
        )
    except ValueError as exc:
        raise TargetSeriesError("Robuxio account-live cutoff is invalid") from exc
    robuxio_series = series_by_strategy.get("robuxio-crypto")
    if robuxio_series is None:
        raise TargetSeriesError("provider ledger lacks Robuxio mapping")
    robuxio_live, robuxio_coverage = _matrixport_returns(
        matrixport_nav_path,
        matrixport_holdings_path,
        matrixport_subscription_path,
        matrixport_daily_holdings_path,
        matrixport_product_id,
        robuxio_series,
        matrixport_missing_dates,
        matrixport_gap_policy,
        admission_cutoff=matrixport_admission_cutoff,
    )
    raw_gap_policy = robuxio_coverage.get("source_gap_policy")
    if not isinstance(raw_gap_policy, dict):
        raise TargetSeriesError("Matrixport source gap policy is absent")
    if raw_gap_policy != sources["matrixport-account-generation"].get(
        "declared_gap_policy"
    ):
        raise TargetSeriesError(
            "Matrixport source gap policy differs from product authority"
        )
    raw_epoch_hpr_receipt = robuxio_coverage.get("epoch_hpr_receipt")
    if not isinstance(raw_epoch_hpr_receipt, dict):
        raise TargetSeriesError("Matrixport epoch HPR receipt is absent")
    robuxio_epoch_hpr_receipt = dict(raw_epoch_hpr_receipt)
    matrixport_unavailable_return_dates = frozenset(
        date.fromisoformat(str(item))
        for item in raw_gap_policy.get("unavailable_return_dates", [])
    )
    if (
        robuxio_live[0]["date"],
        robuxio_live[-1]["date"],
    ) != (
        robuxio_interval.get("observed_start"),
        robuxio_interval.get("observed_end"),
    ):
        raise TargetSeriesError("Robuxio account-live interval differs from contract")
    robuxio_provider = _matrixport_provider_returns(
        matrixport_nav_path,
        matrixport_product_id,
        robuxio_series,
        date.fromisoformat(str(robuxio_coverage["account_live_start"])),
    )
    robuxio_provider_interval = corrections["robuxio-provider-live-before-funding"].get(
        "interval"
    )
    if not isinstance(robuxio_provider_interval, dict):
        raise TargetSeriesError(
            "Robuxio provider-live interval is absent from contract"
        )
    _validate_robuxio_provider_interval(robuxio_provider, robuxio_provider_interval)
    live_by_strategy["robuxio-crypto"] = _matrixport_account_live_from_ledger(
        live_by_strategy["robuxio-crypto"],
        robuxio_live,
        matrixport_unavailable_return_dates,
    )

    quantbase_artifacts = _manifest_artifacts(quantbase_manifest.get("artifacts"))
    _verify_artifact(
        quantbase_history_path,
        quantbase_artifacts,
        "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
    )
    _verify_artifact(
        quantbase_activity_path,
        quantbase_artifacts,
        "source/csv/account_activity.csv",
    )
    volatis_series = series_by_strategy.get("volatis-strategy")
    if volatis_series is None:
        raise TargetSeriesError("provider ledger lacks Volatis mapping")
    volatis_live, volatis_proof = _quantbase_returns(
        quantbase_history_path,
        quantbase_activity_path,
        quantbase_fund_id,
        volatis_series,
    )
    volatis_coverage = volatis_proof.get("coverage")
    if not isinstance(volatis_coverage, dict):
        raise TargetSeriesError("Volatis coverage proof is invalid")
    volatis_interval = corrections["volatis-account-live"].get("interval")
    if not isinstance(volatis_interval, dict) or (
        volatis_live[0]["date"],
        volatis_live[-1]["date"],
    ) != (
        volatis_interval.get("observed_start"),
        volatis_interval.get("observed_end"),
    ):
        raise TargetSeriesError("Volatis account-live interval differs from contract")
    live_by_strategy["volatis-strategy"] = _quantbase_account_live_from_ledger(
        live_by_strategy["volatis-strategy"], volatis_live
    )

    p123_artifacts = _manifest_artifacts(p123_manifest.get("artifacts"))
    metadata_artifact = _verify_artifact(
        portfolio123_metadata_path,
        p123_artifacts,
        "tables/har_managed_metadata.parquet",
    )
    performance_artifact = _verify_artifact(
        portfolio123_performance_path,
        p123_artifacts,
        "tables/har_managed_performance.parquet",
    )
    equity_artifact = _verify_artifact(
        portfolio123_equity_path,
        p123_artifacts,
        "tables/har_equity_curve.parquet",
    )
    p123_returns_artifact = _verify_artifact(
        portfolio123_returns_path, p123_artifacts, "tables/returns_daily.parquet"
    )
    relationships_artifact = _verify_artifact(
        portfolio123_relationships_path,
        p123_artifacts,
        "tables/strategy_relationships.parquet",
    )
    p123_eu_series = series_by_strategy.get("p123-eu")
    if p123_eu_series is None:
        raise TargetSeriesError("provider ledger lacks Portfolio123 EU mapping")
    _eu_version_status, p123_eu_version = _contract_version(strategies["p123-eu"])
    p123_eu_candidate, p123_eu_proof = _portfolio123_provider_performance_returns(
        metadata_path=portfolio123_metadata_path,
        performance_path=portfolio123_performance_path,
        equity_path=portfolio123_equity_path,
        generation_id=p123_generation,
        series_id=p123_eu_series,
        accepted_version=p123_eu_version,
    )
    p123_eu_interval = corrections["p123-eu-provider-live"].get("interval")
    if not isinstance(p123_eu_interval, dict) or (
        p123_eu_candidate[0]["date"],
        p123_eu_candidate[-1]["date"],
    ) != (
        p123_eu_interval.get("observed_start"),
        p123_eu_interval.get("observed_end"),
    ):
        raise TargetSeriesError("Portfolio123 EU provider interval differs")
    p123_eu_live_interval = corrections["p123-eu-account-live"].get("interval")
    p123_eu_live = live_by_strategy["p123-eu"]
    if (
        not isinstance(p123_eu_live_interval, dict)
        or not p123_eu_live
        or (p123_eu_live[0]["date"], p123_eu_live[-1]["date"])
        != (
            p123_eu_live_interval.get("observed_start"),
            p123_eu_live_interval.get("observed_end"),
        )
    ):
        raise TargetSeriesError("Portfolio123 EU account-live interval differs")

    p123_ai_series = series_by_strategy.get("p123-ai-factor")
    p123_ai_evidence = evidence_by_strategy.get("p123-ai-factor")
    if p123_ai_series is None or not isinstance(p123_ai_evidence, dict):
        raise TargetSeriesError("Portfolio123 AI evidence is absent")
    p123_ai_backtest, p123_ai_version, p123_ai_proof = _p123_ai_backtest(
        evidence=p123_ai_evidence,
        returns_path=portfolio123_returns_path,
        relationships_path=portfolio123_relationships_path,
        generation_id=p123_generation,
        relationships_sha256=relationships_artifact.sha256,
        strategy_evidence_sha256=hashlib.sha256(strategy_evidence_content).hexdigest(),
        series_id=p123_ai_series,
    )

    history_rows: dict[str, list[dict[str, str]]] = {}
    history_definitions: dict[str, dict[str, object]] = {}
    history_dispositions: dict[str, dict[str, object]] = {}
    history_proof: dict[str, object] | None = None
    if (strategy_history_manifest_path is None) != (
        strategy_history_returns_path is None
    ):
        raise TargetSeriesError("strategy history inputs must be supplied together")
    if (
        strategy_history_manifest_path is not None
        and strategy_history_returns_path is not None
    ):
        (
            history_rows,
            history_definitions,
            history_dispositions,
            history_proof,
        ) = _strategy_history_returns(
            strategy_history_manifest_path, strategy_history_returns_path
        )

    first_live: dict[str, date] = {}
    for strategy_id, rows in live_by_strategy.items():
        rows.sort(key=lambda item: item["date"])
        seen_dates: set[str] = set()
        for row in rows:
            if row["date"] in seen_dates:
                raise TargetSeriesError(f"duplicate live return: {strategy_id}")
            seen_dates.add(row["date"])
        if rows:
            first_live[strategy_id] = date.fromisoformat(rows[0]["date"])
    version_by_strategy: dict[str, str] = {}
    version_status_by_strategy: dict[str, str] = {}
    for strategy_id, strategy in strategies.items():
        status, version = _contract_version(strategy)
        version_status_by_strategy[strategy_id] = status
        version_by_strategy[strategy_id] = version

    admitted_backtests: dict[str, list[dict[str, str]]] = {}
    unadmitted_backtests: dict[str, list[dict[str, str]]] = {}
    p123_ai_exact_version = (
        version_status_by_strategy["p123-ai-factor"] == "EVIDENCED"
        and version_by_strategy["p123-ai-factor"] == p123_ai_version
    )
    p123_ai_overlaps_live = (
        date.fromisoformat(p123_ai_backtest[-1]["date"]) >= first_live["p123-ai-factor"]
    )
    if not p123_ai_exact_version:
        p123_ai_disposition = "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    elif p123_ai_overlaps_live:
        unadmitted_backtests["p123-ai-factor"] = p123_ai_backtest
        p123_ai_disposition = "DERIVED_NOT_ADMITTED_OVERLAP"
    else:
        admitted_backtests["p123-ai-factor"] = p123_ai_backtest
        p123_ai_disposition = "ADMITTED_EXACT_VERSION_PREHISTORY"
    for series_id, rows in history_rows.items():
        mapped_strategy_id = series_to_strategy.get(series_id)
        if mapped_strategy_id is None:
            raise TargetSeriesError("strategy history series lacks product mapping")
        definition = history_definitions[series_id]
        definition_version = str(definition["strategy_version"])
        if (
            version_status_by_strategy[mapped_strategy_id] != "EVIDENCED"
            or version_by_strategy[mapped_strategy_id] != definition_version
        ):
            continue
        if (
            rows
            and date.fromisoformat(rows[-1]["date"]) >= first_live[mapped_strategy_id]
        ):
            raise TargetSeriesError(
                f"strategy history overlaps account-live: {mapped_strategy_id}"
            )
        admitted_backtests[mapped_strategy_id] = rows

    account_source = sources["owner-account-live-history"]
    account_calendar, account_frequency = _source_temporal(account_source)
    matrixport_calendar, matrixport_frequency = _source_temporal(
        sources["matrixport-account-generation"]
    )
    quantbase_calendar, quantbase_frequency = _source_temporal(
        sources["quantbase-account-generation"]
    )
    backtest_calendar, backtest_frequency = _source_temporal(
        sources["strategy-backtests"]
    )
    segment_rows: list[dict[str, str]] = []
    for strategy_id, rows in live_by_strategy.items():
        if strategy_id == "robuxio-crypto":
            source_id = "matrixport-account-generation"
            source_generation_id = matrixport_generation
            calendar, frequency = matrixport_calendar, matrixport_frequency
        elif strategy_id == "volatis-strategy":
            source_id = "quantbase-account-generation"
            source_generation_id = quantbase_generation
            calendar, frequency = quantbase_calendar, quantbase_frequency
        else:
            source_id = "owner-account-live-history"
            source_generation_id = ledger_generation
            calendar, frequency = account_calendar, account_frequency
        segment_rows.extend(
            _segment_rows(
                rows,
                strategy_id=strategy_id,
                strategy_version=version_by_strategy[strategy_id],
                segment_id=f"{strategy_id}:account-live",
                source_id=source_id,
                source_generation_id=source_generation_id,
                mode_admission="INCLUDE_SELECTED_LIVE",
                calendar=calendar,
                frequency=frequency,
            )
        )
    segment_rows.extend(
        _segment_rows(
            robuxio_provider,
            strategy_id="robuxio-crypto",
            strategy_version=version_by_strategy["robuxio-crypto"],
            segment_id="robuxio-crypto:provider-live-before-funding",
            source_id="matrixport-account-generation",
            source_generation_id=matrixport_generation,
            mode_admission="EXCLUDE_COMPARISON_ONLY",
            calendar=matrixport_calendar,
            frequency=matrixport_frequency,
        )
    )
    for strategy_id, rows in admitted_backtests.items():
        series_id = series_by_strategy[strategy_id]
        normalized = [
            {
                "series_id": series_id,
                "date": row["date"],
                "return": row["return"],
                "evidence_layer": "BACKTEST",
                "return_basis": "MODEL",
                "return_type": "TOTAL",
            }
            for row in rows
        ]
        source_generation_id = (
            p123_generation
            if strategy_id == "p123-ai-factor"
            else str((history_proof or {}).get("bundle_id") or "NOT_CAPTURED")
        )
        segment_rows.extend(
            _segment_rows(
                normalized,
                strategy_id=strategy_id,
                strategy_version=version_by_strategy[strategy_id],
                segment_id=f"{strategy_id}:backtest",
                source_id="strategy-backtests",
                source_generation_id=source_generation_id,
                mode_admission="INCLUDE_VERSION_COMPATIBLE_PREHISTORY",
                calendar=backtest_calendar,
                frequency=backtest_frequency,
            )
        )
    for strategy_id, rows in unadmitted_backtests.items():
        normalized = [
            {
                "series_id": series_by_strategy[strategy_id],
                "date": row["date"],
                "return": row["return"],
                "evidence_layer": "BACKTEST",
                "return_basis": "MODEL",
                "return_type": "TOTAL",
            }
            for row in rows
        ]
        segment_rows.extend(
            _segment_rows(
                normalized,
                strategy_id=strategy_id,
                strategy_version=version_by_strategy[strategy_id],
                segment_id=f"{strategy_id}:backtest-not-admitted",
                source_id="strategy-backtests",
                source_generation_id=p123_generation,
                mode_admission="REQUIRED_NOT_READY_OVERLAP",
                calendar=backtest_calendar,
                frequency=backtest_frequency,
            )
        )
    segment_rows.sort(
        key=lambda row: (
            row["strategy_id"],
            row["date"],
            row["evidence_layer"],
            row["segment_id"],
        )
    )
    segment_keys: set[tuple[str, str, str]] = set()
    for row in segment_rows:
        key = (row["strategy_id"], row["segment_id"], row["date"])
        if key in segment_keys:
            raise TargetSeriesError("layered history contains duplicate segment date")
        segment_keys.add(key)

    live_mode_rows = sorted(
        [
            row
            for row in segment_rows
            if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
        ],
        key=lambda row: (row["series_id"], row["date"]),
    )
    hybrid_rows = sorted(
        [
            row
            for row in segment_rows
            if row["mode_admission"]
            in {"INCLUDE_SELECTED_LIVE", "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"}
        ],
        key=lambda row: (
            row["series_id"],
            row["date"],
            0 if row["evidence_layer"] == "BACKTEST" else 1,
        ),
    )

    coverage_rows: list[dict[str, object]] = []
    live_not_ready: list[str] = []
    hybrid_not_ready: list[str] = []
    account_freshness = account_source.get("freshness")
    if not isinstance(account_freshness, dict) or not isinstance(
        account_freshness.get("max_age_days"), int
    ):
        raise TargetSeriesError("account-live freshness contract is invalid")
    default_max_age = int(account_freshness["max_age_days"])
    for strategy_id, strategy in strategies.items():
        mapped_series_id = series_by_strategy.get(strategy_id)
        live_rows = live_by_strategy[strategy_id]
        if strategy_id == "robuxio-crypto":
            freshness = sources["matrixport-account-generation"].get("freshness")
        elif strategy_id == "volatis-strategy":
            freshness = sources["quantbase-account-generation"].get("freshness")
        else:
            freshness = account_freshness
        freshness_max_age = (
            freshness.get("max_age_days") if isinstance(freshness, dict) else None
        )
        max_age = (
            freshness_max_age if isinstance(freshness_max_age, int) else default_max_age
        )
        live_data_age: dict[str, object] | None = None
        if live_rows:
            complete_through = date.fromisoformat(live_rows[-1]["date"])
            age_days = (accepted_as_of - complete_through).days
            if age_days < 0:
                raise TargetSeriesError(
                    f"account-live history exceeds accepted as-of: {strategy_id}"
                )
            live_data_age = {
                "age_days": age_days,
                "complete_through": complete_through.isoformat(),
                "evaluated_on": accepted_as_of.isoformat(),
                "status": (
                    "WARN_COMPLETE_DATA_AGE"
                    if age_days > max_age
                    else "CURRENT_WITHIN_AGE_THRESHOLD"
                ),
                "warning_after_days": max_age,
            }
        live_state = "AVAILABLE"
        live_reason: str | None = None
        declared_unavailable_dates = (
            matrixport_unavailable_return_dates
            if strategy_id == "robuxio-crypto"
            else frozenset()
        )
        gap_disposition = (
            _gap_disposition(live_rows, declared_unavailable_dates)
            if live_rows
            else None
        )
        exclusion_reason = _excluded_strategy_mode_reason(strategy)
        if exclusion_reason is not None:
            if live_rows:
                raise TargetSeriesError(
                    f"excluded strategy entered account-live history: {strategy_id}"
                )
            live_state = "NOT_APPLICABLE_OWNER_APPROVED"
            live_reason = exclusion_reason
            live_data_age = None
        elif not live_rows:
            live_state = "REQUIRED_NOT_READY"
            live_reason = (
                "CAPTURED_EVENT_ONLY_RECONSTRUCTION_NOT_ADMISSIBLE_DAILY_NAV"
                if strategy_id == "echotrade-growth-pivots"
                else "REQUIRED_ACCOUNT_LIVE_SEGMENT_ABSENT"
            )
        elif gap_disposition is not None and gap_disposition.startswith(
            "REQUIRED_NOT_READY"
        ):
            raise TargetSeriesError(f"unexplained account-live gap: {strategy_id}")
        elif live_data_age is not None and live_data_age["status"] == (
            "WARN_COMPLETE_DATA_AGE"
        ):
            live_reason = "WARN_ACCOUNT_LIVE_COMPLETE_DATA_AGE"
        if live_state == "REQUIRED_NOT_READY":
            live_not_ready.append(strategy_id)
        backtest_rows = admitted_backtests.get(strategy_id, [])
        if exclusion_reason is not None and backtest_rows:
            raise TargetSeriesError(
                f"excluded strategy entered backtest history: {strategy_id}"
            )
        hybrid_state = live_state
        hybrid_reason = live_reason
        if (
            exclusion_reason is None
            and hybrid_state == "AVAILABLE"
            and not backtest_rows
        ):
            hybrid_state = "REQUIRED_NOT_READY"
            if strategy_id in unadmitted_backtests:
                hybrid_reason = (
                    "REQUIRED_NOT_READY_BACKTEST_SOURCE_OVERLAPS_ACCOUNT_LIVE"
                )
            elif version_status_by_strategy[strategy_id] != "EVIDENCED":
                hybrid_reason = "REQUIRED_NOT_READY_VERSION_UNRESOLVED_OR_MISMATCHED"
            else:
                hybrid_reason = "REQUIRED_EXACT_VERSION_BACKTEST_NOT_ADMITTED"
        if hybrid_state == "REQUIRED_NOT_READY":
            hybrid_not_ready.append(strategy_id)
        strategy_segments = [
            row for row in segment_rows if row["strategy_id"] == strategy_id
        ]
        segment_summaries: list[dict[str, object]] = []
        for segment_id in sorted({row["segment_id"] for row in strategy_segments}):
            selected = [
                row for row in strategy_segments if row["segment_id"] == segment_id
            ]
            identity = _row_identity(selected)
            segment_unavailable_dates = (
                matrixport_unavailable_return_dates
                if strategy_id == "robuxio-crypto"
                and selected[0]["source_id"] == "matrixport-account-generation"
                else frozenset()
            )
            segment_summaries.append(
                {
                    "calendar": selected[0]["calendar"],
                    "evidence_layer": selected[0]["evidence_layer"],
                    "frequency": selected[0]["frequency"],
                    "gap_disposition": _gap_disposition(
                        selected, segment_unavailable_dates
                    ),
                    "mode_admission": selected[0]["mode_admission"],
                    "observed_end": identity["observed_end"],
                    "observed_start": identity["observed_start"],
                    "return_observations": identity["return_observations"],
                    "segment_id": segment_id,
                    "sha256": identity["sha256"],
                    "source_generation_id": selected[0]["source_generation_id"],
                    "source_id": selected[0]["source_id"],
                    "strategy_version": selected[0]["strategy_version"],
                }
            )
        coverage_rows.append(
            {
                "blockers": sorted(
                    {
                        int(item)
                        for item in strategy.get("blockers", [])
                        if isinstance(item, int)
                    }
                ),
                "funded_membership": strategy.get("funded_membership"),
                "data_age": live_data_age,
                "modes": {
                    "LIVE_ONLY": {"reason": live_reason, "status": live_state},
                    "LIVE_PLUS_BACKTEST": {
                        "reason": hybrid_reason,
                        "status": hybrid_state,
                    },
                },
                "product_readiness": strategy.get("readiness"),
                "segments": segment_summaries,
                "series_id": mapped_series_id,
                "strategy_id": strategy_id,
                "strategy_version": {
                    "history_generation_ref": version_by_strategy[strategy_id],
                    "product_status": version_status_by_strategy[strategy_id],
                    "product_version_ref": strategy.get("strategy_version", {}).get(
                        "version_ref"
                    )
                    if isinstance(strategy.get("strategy_version"), dict)
                    else None,
                },
            }
        )
    coverage = {
        "as_of_date": accepted_as_of.isoformat(),
        "builder": LAYERED_HISTORY_BUILDER,
        "builder_version": LAYERED_HISTORY_BUILDER_VERSION,
        "correction_resolutions": {
            "echotrade-event-only-account-live-rejected": {
                **echo_rejected_identity,
                "source_generation_id": ledger_generation,
                "status": "REJECTED_EVENT_ONLY_RECONSTRUCTION_NOT_DAILY_NAV",
            },
            "p123-ai-backtest": {
                **p123_ai_proof,
                "status": p123_ai_disposition,
            },
            "p123-eu-provider-live": {
                **p123_eu_proof,
                "status": "DERIVED_NOT_ADMITTED_PENDING_303",
            },
            "robuxio-account-live-after-funding": {
                "account_live_start": robuxio_coverage["account_live_start"],
                "observed_end": robuxio_live[-1]["date"],
                "observed_start": robuxio_live[0]["date"],
                "source_gap_policy": raw_gap_policy,
                "status": "RESOLVED_BY_PIPE_HISTORY_001",
            },
            "volatis-account-live": {
                "observed_end": volatis_live[-1]["date"],
                "observed_start": volatis_live[0]["date"],
                "source_first_positive_value_date": volatis_coverage[
                    "source_first_positive_value_date"
                ],
                "status": "RESOLVED_BY_PIPE_HISTORY_001",
            },
        },
        "epoch_hpr_receipts": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "receipt": robuxio_epoch_hpr_receipt,
                "source_generation_id": matrixport_generation,
                "source_id": "matrixport-account-generation",
                "strategy_id": "robuxio-crypto",
            }
        ],
        "mode_policy": dict(LAYERED_HISTORY_MODE_POLICY),
        "schema_version": LAYERED_HISTORY_SCHEMA_VERSION,
        "strategies": sorted(coverage_rows, key=lambda item: str(item["strategy_id"])),
        "verification": "VERIFIED_WITH_REQUIRED_NOT_READY_DISPOSITIONS",
    }
    artifacts = {
        "segments.csv": _segment_view_bytes(segment_rows),
        "excluded.csv": _target_bytes(echo_candidate),
        "live-only.csv": _correlation_view_bytes(live_mode_rows),
        "live-plus-backtest.csv": _correlation_view_bytes(hybrid_rows),
    }
    artifacts["returns.csv"] = artifacts["live-plus-backtest.csv"]
    artifacts["coverage.json"] = canonical_json_bytes(coverage)

    inputs = [
        _artifact_identity(
            input_id="financial-mvp-contract",
            artifact_role="PRODUCT_CONTRACT",
            path=contract_path,
        ),
        _artifact_identity(
            input_id="provider-ledger-manifest",
            artifact_role="GENERATION_MANIFEST",
            path=provider_ledger_manifest_path,
            generation_id=ledger_generation,
            as_of_date=ledger_as_of,
        ),
        _artifact_identity(
            input_id="provider-ledger-catalog",
            artifact_role="GENERATION_ARTIFACT",
            path=provider_ledger_catalog_path,
            artifact_id="normalized/provider-ledger.json",
            generation_id=ledger_generation,
            as_of_date=ledger_as_of,
        ),
        _artifact_identity(
            input_id="matrixport-manifest",
            artifact_role="GENERATION_MANIFEST",
            path=matrixport_manifest_path,
            generation_id=matrixport_generation,
            as_of_date=matrixport_as_of,
        ),
        _artifact_identity(
            input_id="matrixport-nav",
            artifact_role="GENERATION_ARTIFACT",
            path=matrixport_nav_path,
            artifact_id="csv/nav_daily.csv",
            generation_id=matrixport_generation,
            as_of_date=matrixport_as_of,
        ),
        _artifact_identity(
            input_id="matrixport-holdings",
            artifact_role="GENERATION_ARTIFACT",
            path=matrixport_holdings_path,
            artifact_id="csv/holdings.csv",
            generation_id=matrixport_generation,
            as_of_date=matrixport_as_of,
        ),
        _artifact_identity(
            input_id="matrixport-subscriptions",
            artifact_role="GENERATION_ARTIFACT",
            path=matrixport_subscription_path,
            artifact_id="csv/subscription_details.csv",
            generation_id=matrixport_generation,
            as_of_date=matrixport_as_of,
        ),
        _artifact_identity(
            input_id="quantbase-manifest",
            artifact_role="GENERATION_MANIFEST",
            path=quantbase_manifest_path,
            generation_id=quantbase_generation,
            as_of_date=quantbase_as_of,
        ),
        _artifact_identity(
            input_id="quantbase-history",
            artifact_role="GENERATION_ARTIFACT",
            path=quantbase_history_path,
            artifact_id="source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
            generation_id=quantbase_generation,
            as_of_date=quantbase_as_of,
        ),
        _artifact_identity(
            input_id="quantbase-activity",
            artifact_role="GENERATION_ARTIFACT",
            path=quantbase_activity_path,
            artifact_id="source/csv/account_activity.csv",
            generation_id=quantbase_generation,
            as_of_date=quantbase_as_of,
        ),
        _artifact_identity(
            input_id="portfolio123-manifest",
            artifact_role="GENERATION_MANIFEST",
            path=portfolio123_manifest_path,
            generation_id=p123_generation,
            as_of_date=p123_as_of,
        ),
        _artifact_identity(
            input_id="portfolio123-metadata",
            artifact_role="GENERATION_ARTIFACT",
            path=portfolio123_metadata_path,
            artifact_id=metadata_artifact.artifact_id,
            generation_id=p123_generation,
            as_of_date=p123_as_of,
        ),
        _artifact_identity(
            input_id="portfolio123-performance",
            artifact_role="GENERATION_ARTIFACT",
            path=portfolio123_performance_path,
            artifact_id=performance_artifact.artifact_id,
            generation_id=p123_generation,
            as_of_date=p123_as_of,
        ),
        _artifact_identity(
            input_id="portfolio123-equity",
            artifact_role="GENERATION_ARTIFACT",
            path=portfolio123_equity_path,
            artifact_id=equity_artifact.artifact_id,
            generation_id=p123_generation,
            as_of_date=p123_as_of,
        ),
        _artifact_identity(
            input_id="portfolio123-returns",
            artifact_role="GENERATION_ARTIFACT",
            path=portfolio123_returns_path,
            artifact_id=p123_returns_artifact.artifact_id,
            generation_id=p123_generation,
            as_of_date=p123_as_of,
        ),
        _artifact_identity(
            input_id="portfolio123-relationships",
            artifact_role="GENERATION_ARTIFACT",
            path=portfolio123_relationships_path,
            artifact_id=relationships_artifact.artifact_id,
            generation_id=p123_generation,
            as_of_date=p123_as_of,
        ),
    ]
    if source_selection_mode == "CURRENT_POINTERS":
        inputs.extend(
            [
                _artifact_identity(
                    input_id="provider-ledger-current",
                    artifact_role="CURRENT_POINTER",
                    path=provider_ledger_pointer_path,
                    generation_id=ledger_generation,
                    as_of_date=ledger_as_of,
                ),
                _artifact_identity(
                    input_id="matrixport-current",
                    artifact_role="CURRENT_POINTER",
                    path=matrixport_pointer_path,
                    generation_id=matrixport_generation,
                    as_of_date=matrixport_as_of,
                ),
                _artifact_identity(
                    input_id="quantbase-current",
                    artifact_role="CURRENT_POINTER",
                    path=quantbase_pointer_path,
                    generation_id=quantbase_generation,
                    as_of_date=quantbase_as_of,
                ),
                _artifact_identity(
                    input_id="portfolio123-current",
                    artifact_role="CURRENT_POINTER",
                    path=portfolio123_pointer_path,
                    generation_id=p123_generation,
                    as_of_date=p123_as_of,
                ),
            ]
        )
    if (
        strategy_history_manifest_path is not None
        and strategy_history_returns_path is not None
    ):
        bundle_id = str((history_proof or {}).get("bundle_id") or "")
        inputs.extend(
            [
                _artifact_identity(
                    input_id="strategy-history-manifest",
                    artifact_role="GENERATION_MANIFEST",
                    path=strategy_history_manifest_path,
                    generation_id=bundle_id or None,
                    as_of_date=str((history_proof or {}).get("as_of_date") or "")
                    or None,
                ),
                _artifact_identity(
                    input_id="strategy-history-returns",
                    artifact_role="GENERATION_ARTIFACT",
                    path=strategy_history_returns_path,
                    artifact_id="strategy-history/returns.csv",
                    generation_id=bundle_id or None,
                    as_of_date=str((history_proof or {}).get("as_of_date") or "")
                    or None,
                ),
            ]
        )
    inputs.sort(key=lambda item: str(item["input_id"]))
    output_identities = {
        name: _history_output_identity(
            name,
            artifacts[name],
            len(segment_rows)
            if name == "segments.csv"
            else len(echo_candidate)
            if name == "excluded.csv"
            else len(live_mode_rows)
            if name == "live-only.csv"
            else len(hybrid_rows)
            if name in {"returns.csv", "live-plus-backtest.csv"}
            else len(coverage_rows),
        )
        for name in LAYERED_HISTORY_OUTPUT_NAMES
    }
    source_manifest_contents = {
        "matrixport": matrixport_manifest_path.read_text(encoding="utf-8"),
        "portfolio123": portfolio123_manifest_path.read_text(encoding="utf-8"),
        "provider-ledger": provider_ledger_manifest_path.read_text(encoding="utf-8"),
        "quantbase": quantbase_manifest_path.read_text(encoding="utf-8"),
    }
    if strategy_history_manifest_path is not None:
        source_manifest_contents["strategy-history"] = (
            strategy_history_manifest_path.read_text(encoding="utf-8")
        )
    contract_sha = hashlib.sha256(canonical_json_bytes(contract)).hexdigest()
    manifest_without_id: dict[str, object] = {
        "$schema": "https://quant-engine.local/schemas/portfolio/layered-history-generation.schema.json",
        "as_of_date": accepted_as_of.isoformat(),
        "builder": LAYERED_HISTORY_BUILDER,
        "builder_version": LAYERED_HISTORY_BUILDER_VERSION,
        "inputs": inputs,
        "mode_status": {
            "LIVE_ONLY": {
                "required_not_ready_strategy_ids": sorted(live_not_ready),
                "status": "AVAILABLE" if not live_not_ready else "REQUIRED_NOT_READY",
            },
            "LIVE_PLUS_BACKTEST": {
                "required_not_ready_strategy_ids": sorted(hybrid_not_ready),
                "status": "AVAILABLE" if not hybrid_not_ready else "REQUIRED_NOT_READY",
            },
        },
        "outputs": output_identities,
        "product": {
            "contract_sha256": contract_sha,
            "history_policy_id": history_policy["policy_id"],
            "history_policy_version": history_policy["policy_version"],
            "product_id": product["product_id"],
            "product_version": product["product_version"],
        },
        "repository_sha": repository_sha,
        "schema_version": LAYERED_HISTORY_SCHEMA_VERSION,
        "source_manifests": source_manifest_contents,
        "source_selection_mode": source_selection_mode,
        "tool_sha256": _sha256(Path(__file__))[0],
        "verification": "VERIFIED",
    }
    if source_selection_mode == "CURRENT_POINTERS":
        manifest_without_id["source_pointers"] = {
            "matrixport": matrixport_pointer_path.read_text(encoding="utf-8"),
            "portfolio123": portfolio123_pointer_path.read_text(encoding="utf-8"),
            "provider-ledger": provider_ledger_pointer_path.read_text(encoding="utf-8"),
            "quantbase": quantbase_pointer_path.read_text(encoding="utf-8"),
        }

    generation_id = hashlib.sha256(
        canonical_json_bytes(manifest_without_id)
    ).hexdigest()
    manifest = {**manifest_without_id, "generation_id": generation_id}
    return artifacts, canonical_json_bytes(manifest)


def _csv_content_rows(
    content: bytes, fields: Sequence[str], name: str
) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TargetSeriesError(f"{name} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != tuple(fields):
        raise TargetSeriesError(f"{name} header differs")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise TargetSeriesError(f"{name} contains malformed row")
        rows.append({field: str(row[field]) for field in fields})
    return rows


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], name: str
) -> None:
    if set(value) != expected:
        raise TargetSeriesError(f"{name} keys differ")


def _layered_history_assurance_disposition(
    *,
    contract: Mapping[str, Any],
    receipt_as_of: date,
    assurance_profile: str,
    evaluated_on: date,
) -> dict[str, object]:
    if (
        not isinstance(assurance_profile, str)
        or assurance_profile not in LAYERED_HISTORY_ASSURANCE_PROFILES
    ):
        raise TargetSeriesError("layered history assurance profile is invalid")
    product = contract.get("product")
    as_of_policy = product.get("as_of_policy") if isinstance(product, dict) else None
    profiles = (
        as_of_policy.get("execution_profiles")
        if isinstance(as_of_policy, dict)
        else None
    )
    profile_policy = (
        profiles.get(assurance_profile) if isinstance(profiles, dict) else None
    )
    if not isinstance(profile_policy, str) or not profile_policy:
        raise TargetSeriesError("layered history assurance authority is absent")
    if receipt_as_of > evaluated_on:
        raise TargetSeriesError(
            "layered history receipt exceeds the assurance evaluation date"
        )
    receipt_age_days = (evaluated_on - receipt_as_of).days
    if assurance_profile == "RELEASE_QC" and receipt_age_days != 0:
        raise TargetSeriesError(
            "release QC requires a layered history receipt for the evaluation date"
        )
    if assurance_profile == "RELEASE_QC":
        status = "CURRENT_RELEASE_RECEIPT"
    elif receipt_age_days == 0:
        status = "CURRENT_IMMUTABLE_RECEIPT"
    else:
        status = "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE"
    return {
        "evaluated_on": evaluated_on.isoformat(),
        "profile": assurance_profile,
        "profile_policy": profile_policy,
        "receipt_age_days": receipt_age_days,
        "receipt_as_of": receipt_as_of.isoformat(),
        "status": status,
    }


def validate_layered_history_generation(
    *,
    artifacts: Mapping[str, bytes],
    manifest_content: bytes,
    contract_path: Path,
    require_mode: str | None = None,
    assurance_profile: str | None = None,
    evaluated_on: date | None = None,
) -> dict[str, object]:
    try:
        manifest = json.loads(manifest_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError(
            "layered history generation manifest is invalid"
        ) from exc
    if not isinstance(manifest, dict):
        raise TargetSeriesError("layered history generation manifest must be an object")
    if manifest_content != canonical_json_bytes(manifest):
        raise TargetSeriesError("layered history generation manifest is not canonical")
    source_selection_mode = manifest.get("source_selection_mode", "CURRENT_POINTERS")
    if source_selection_mode not in {"CURRENT_POINTERS", "IMMUTABLE_GENERATIONS"}:
        raise TargetSeriesError("layered history source selection mode differs")
    expected_manifest_keys = {
        "$schema",
        "as_of_date",
        "builder",
        "builder_version",
        "generation_id",
        "inputs",
        "mode_status",
        "outputs",
        "product",
        "repository_sha",
        "schema_version",
        "source_manifests",
        "tool_sha256",
        "verification",
    }
    if "source_selection_mode" in manifest:
        expected_manifest_keys.add("source_selection_mode")
    if source_selection_mode == "CURRENT_POINTERS":
        expected_manifest_keys.add("source_pointers")
    _require_exact_keys(
        manifest,
        frozenset(expected_manifest_keys),
        "layered history generation",
    )
    if (
        manifest.get("$schema")
        != "https://quant-engine.local/schemas/portfolio/layered-history-generation.schema.json"
        or manifest.get("schema_version") != LAYERED_HISTORY_SCHEMA_VERSION
        or manifest.get("builder") != LAYERED_HISTORY_BUILDER
        or manifest.get("builder_version") != LAYERED_HISTORY_BUILDER_VERSION
        or manifest.get("verification") != "VERIFIED"
    ):
        raise TargetSeriesError("layered history generation contract differs")
    generation_id = str(manifest.get("generation_id", ""))
    without_id = dict(manifest)
    without_id.pop("generation_id", None)
    if generation_id != hashlib.sha256(canonical_json_bytes(without_id)).hexdigest():
        raise TargetSeriesError("layered history generation identity differs")
    raw_as_of = str(manifest.get("as_of_date", ""))
    try:
        accepted_as_of = date.fromisoformat(raw_as_of)
    except ValueError as exc:
        raise TargetSeriesError("layered history generation as-of is invalid") from exc
    if accepted_as_of.isoformat() != raw_as_of:
        raise TargetSeriesError("layered history generation as-of is invalid")
    repository_sha = str(manifest.get("repository_sha", ""))
    tool_sha256 = str(manifest.get("tool_sha256", ""))
    if (
        len(repository_sha) != 40
        or any(ch not in "0123456789abcdef" for ch in repository_sha)
        or len(tool_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in tool_sha256)
    ):
        raise TargetSeriesError("layered history code identity differs")
    trusted_repository_sha, trusted_tool_sha256 = (
        _trusted_layered_history_code_identity()
    )
    if repository_sha != trusted_repository_sha or tool_sha256 != trusted_tool_sha256:
        raise TargetSeriesError("layered history code identity is not trusted")

    contract, strategies, sources, corrections = _history_contract(contract_path)
    product = manifest.get("product")
    if (
        not isinstance(product, dict)
        or product.get("contract_sha256")
        != hashlib.sha256(canonical_json_bytes(contract)).hexdigest()
    ):
        raise TargetSeriesError("layered history product contract identity differs")
    _require_exact_keys(
        product,
        frozenset(
            {
                "contract_sha256",
                "history_policy_id",
                "history_policy_version",
                "product_id",
                "product_version",
            }
        ),
        "layered history product",
    )
    if (
        product.get("product_id") != contract["product"]["product_id"]
        or product.get("product_version") != contract["product"]["product_version"]
        or product.get("history_policy_id")
        != contract["history_corrections"]["policy_id"]
        or product.get("history_policy_version")
        != contract["history_corrections"]["policy_version"]
    ):
        raise TargetSeriesError("layered history product authority differs")

    input_rows = manifest.get("inputs")
    if not isinstance(input_rows, list):
        raise TargetSeriesError("layered history input identities are absent")
    inputs: dict[str, dict[str, object]] = {}
    for raw in input_rows:
        if not isinstance(raw, dict):
            raise TargetSeriesError("layered history input identity is invalid")
        _require_exact_keys(
            raw,
            frozenset(
                {
                    "artifact_id",
                    "artifact_role",
                    "as_of_date",
                    "generation_id",
                    "input_id",
                    "sha256",
                    "size_bytes",
                }
            ),
            "layered history input identity",
        )
        input_id = str(raw.get("input_id", ""))
        sha256 = str(raw.get("sha256", ""))
        size = raw.get("size_bytes")
        if (
            not input_id
            or input_id in inputs
            or len(sha256) != 64
            or not isinstance(size, int)
            or size < 0
            or any(
                key in raw
                for key in ("path", "root", "provider_entity_id", "account_id")
            )
        ):
            raise TargetSeriesError("layered history input identity differs")
        inputs[input_id] = dict(raw)
    if input_rows != sorted(input_rows, key=lambda item: str(item["input_id"])):
        raise TargetSeriesError("layered history input ordering differs")

    expected_input_contracts: dict[str, tuple[str, str | None]] = {
        "financial-mvp-contract": ("PRODUCT_CONTRACT", None),
        "provider-ledger-manifest": ("GENERATION_MANIFEST", None),
        "provider-ledger-catalog": (
            "GENERATION_ARTIFACT",
            "normalized/provider-ledger.json",
        ),
        "matrixport-manifest": ("GENERATION_MANIFEST", None),
        "matrixport-nav": ("GENERATION_ARTIFACT", "csv/nav_daily.csv"),
        "matrixport-holdings": ("GENERATION_ARTIFACT", "csv/holdings.csv"),
        "matrixport-subscriptions": (
            "GENERATION_ARTIFACT",
            "csv/subscription_details.csv",
        ),
        "quantbase-manifest": ("GENERATION_MANIFEST", None),
        "quantbase-history": (
            "GENERATION_ARTIFACT",
            "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
        ),
        "quantbase-activity": (
            "GENERATION_ARTIFACT",
            "source/csv/account_activity.csv",
        ),
        "portfolio123-manifest": ("GENERATION_MANIFEST", None),
        "portfolio123-metadata": (
            "GENERATION_ARTIFACT",
            "tables/har_managed_metadata.parquet",
        ),
        "portfolio123-performance": (
            "GENERATION_ARTIFACT",
            "tables/har_managed_performance.parquet",
        ),
        "portfolio123-equity": (
            "GENERATION_ARTIFACT",
            "tables/har_equity_curve.parquet",
        ),
        "portfolio123-returns": (
            "GENERATION_ARTIFACT",
            "tables/returns_daily.parquet",
        ),
        "portfolio123-relationships": (
            "GENERATION_ARTIFACT",
            "tables/strategy_relationships.parquet",
        ),
    }
    if source_selection_mode == "CURRENT_POINTERS":
        expected_input_contracts.update(
            {
                "provider-ledger-current": ("CURRENT_POINTER", None),
                "matrixport-current": ("CURRENT_POINTER", None),
                "quantbase-current": ("CURRENT_POINTER", None),
                "portfolio123-current": ("CURRENT_POINTER", None),
            }
        )
    optional_input_ids = {"strategy-history-manifest", "strategy-history-returns"}
    if optional_input_ids & set(inputs):
        expected_input_contracts.update(
            {
                "strategy-history-manifest": ("GENERATION_MANIFEST", None),
                "strategy-history-returns": (
                    "GENERATION_ARTIFACT",
                    "strategy-history/returns.csv",
                ),
            }
        )
    if set(inputs) != set(expected_input_contracts):
        raise TargetSeriesError("layered history input set differs")
    for input_id, expected_contract in expected_input_contracts.items():
        actual_input = inputs[input_id]
        if (actual_input.get("artifact_role"), actual_input.get("artifact_id")) != (
            expected_contract
        ):
            raise TargetSeriesError(
                f"layered history input contract differs: {input_id}"
            )
    contract_input = inputs["financial-mvp-contract"]
    if (
        contract_input.get("as_of_date") is not None
        or contract_input.get("generation_id") is not None
    ):
        raise TargetSeriesError("layered history product input provenance differs")
    expected_manifests = {
        "provider-ledger-manifest": sources["owner-account-live-history"],
        "matrixport-manifest": sources["matrixport-account-generation"],
        "quantbase-manifest": sources["quantbase-account-generation"],
        "portfolio123-manifest": sources["portfolio123-model-generation"],
    }
    for input_id, source in expected_manifests.items():
        current = source.get("current_generation")
        actual = inputs.get(input_id)
        if (
            not isinstance(current, dict)
            or actual is None
            or (
                actual.get("artifact_role"),
                actual.get("generation_id"),
                actual.get("sha256"),
                actual.get("as_of_date"),
            )
            != (
                "GENERATION_MANIFEST",
                current.get("generation_id"),
                current.get("sha256"),
                current.get("as_of"),
            )
        ):
            raise TargetSeriesError(
                f"layered history current input differs: {input_id}"
            )
    if (
        contract_input.get("artifact_role"),
        contract_input.get("sha256"),
        contract_input.get("size_bytes"),
    ) != ("PRODUCT_CONTRACT", *_sha256(contract_path)):
        raise TargetSeriesError("layered history product input differs")
    raw_source_manifests = manifest.get("source_manifests")
    source_manifest_input_ids = {
        "provider-ledger": "provider-ledger-manifest",
        "matrixport": "matrixport-manifest",
        "quantbase": "quantbase-manifest",
        "portfolio123": "portfolio123-manifest",
    }
    if "strategy-history-manifest" in inputs:
        source_manifest_input_ids["strategy-history"] = "strategy-history-manifest"
    if not isinstance(raw_source_manifests, dict) or set(raw_source_manifests) != set(
        source_manifest_input_ids
    ):
        raise TargetSeriesError("layered history source manifests differ")
    source_manifests: dict[str, dict[str, Any]] = {}
    for source_name, input_id in source_manifest_input_ids.items():
        raw = raw_source_manifests.get(source_name)
        if not isinstance(raw, str):
            raise TargetSeriesError("layered history source manifest is invalid")
        if any(
            token in raw
            for token in ("C:\\\\", "/Users/", "provider_entity_id", "account_id")
        ):
            raise TargetSeriesError(
                "layered history source manifest exposes a private locator"
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TargetSeriesError(
                "layered history source manifest is invalid"
            ) from exc
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != raw.encode(
            "utf-8"
        ):
            raise TargetSeriesError("layered history source manifest is not canonical")
        source_input = inputs[input_id]
        source_generation_id = (
            parsed.get("bundle_id")
            if source_name == "strategy-history"
            else parsed.get("generation_id")
        )
        if source_name == "strategy-history":
            payload = dict(parsed)
            declared_bundle_id = payload.pop("bundle_id", None)
            if (
                not isinstance(declared_bundle_id, str)
                or len(declared_bundle_id) != 64
                or hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
                != declared_bundle_id
            ):
                raise TargetSeriesError(
                    "layered history strategy-history bundle identity differs"
                )
        if (
            source_input.get("artifact_role") != "GENERATION_MANIFEST"
            or hashlib.sha256(raw.encode("utf-8")).hexdigest() != source_input["sha256"]
            or len(raw.encode("utf-8")) != source_input.get("size_bytes")
            or source_generation_id != source_input.get("generation_id")
        ):
            raise TargetSeriesError(
                f"layered history source manifest identity differs: {source_name}"
            )
        source_manifests[source_name] = parsed
    input_source_names = {
        "provider-ledger-manifest": "provider-ledger",
        "provider-ledger-catalog": "provider-ledger",
        "matrixport-manifest": "matrixport",
        "matrixport-nav": "matrixport",
        "matrixport-holdings": "matrixport",
        "matrixport-subscriptions": "matrixport",
        "quantbase-manifest": "quantbase",
        "quantbase-history": "quantbase",
        "quantbase-activity": "quantbase",
        "portfolio123-manifest": "portfolio123",
        "portfolio123-metadata": "portfolio123",
        "portfolio123-performance": "portfolio123",
        "portfolio123-equity": "portfolio123",
        "portfolio123-returns": "portfolio123",
        "portfolio123-relationships": "portfolio123",
    }
    if source_selection_mode == "CURRENT_POINTERS":
        input_source_names.update(
            {
                "provider-ledger-current": "provider-ledger",
                "matrixport-current": "matrixport",
                "quantbase-current": "quantbase",
                "portfolio123-current": "portfolio123",
            }
        )
    if "strategy-history" in source_manifests:
        input_source_names.update(
            {
                "strategy-history-manifest": "strategy-history",
                "strategy-history-returns": "strategy-history",
            }
        )
    if set(input_source_names) != set(inputs) - {"financial-mvp-contract"}:
        raise TargetSeriesError("layered history input source mapping differs")
    for input_id, source_name in input_source_names.items():
        authority_manifest = source_manifests[source_name]
        generation_key = (
            "bundle_id" if source_name == "strategy-history" else "generation_id"
        )
        if (
            inputs[input_id].get("generation_id"),
            inputs[input_id].get("as_of_date"),
        ) != (
            authority_manifest.get(generation_key),
            authority_manifest.get("as_of_date"),
        ):
            raise TargetSeriesError(
                f"layered history input source authority differs: {input_id}"
            )
    matrixport_gap_policy = sources["matrixport-account-generation"].get(
        "declared_gap_policy"
    )
    if not isinstance(matrixport_gap_policy, dict):
        raise TargetSeriesError("Matrixport product gap policy is absent")
    _matrixport_return_transform(matrixport_gap_policy)
    matrixport_missing_dates = _matrixport_declared_missing_dates(
        source_manifests["matrixport"], matrixport_gap_policy
    )
    matrixport_omitted_return_dates = _matrixport_expected_gap_endpoints(
        matrixport_missing_dates
    )
    matrixport_unavailable_return_dates = frozenset(
        matrixport_missing_dates | matrixport_omitted_return_dates
    )
    expected_matrixport_gap_policy = _matrixport_source_gap_policy(
        matrixport_missing_dates,
        matrixport_omitted_return_dates,
        matrixport_gap_policy,
    )
    if expected_matrixport_gap_policy != sources["matrixport-account-generation"].get(
        "declared_gap_policy"
    ):
        raise TargetSeriesError(
            "Matrixport source gap policy differs from product authority"
        )
    if source_selection_mode == "CURRENT_POINTERS":
        source_pointer_input_ids = {
            "provider-ledger": "provider-ledger-current",
            "matrixport": "matrixport-current",
            "quantbase": "quantbase-current",
            "portfolio123": "portfolio123-current",
        }
        raw_source_pointers = manifest.get("source_pointers")
        if not isinstance(raw_source_pointers, dict) or set(raw_source_pointers) != set(
            source_pointer_input_ids
        ):
            raise TargetSeriesError("layered history source pointers differ")
        for source_name, pointer_input_id in source_pointer_input_ids.items():
            raw_pointer = raw_source_pointers.get(source_name)
            if not isinstance(raw_pointer, str):
                raise TargetSeriesError("layered history source pointer is invalid")
            try:
                pointer = json.loads(raw_pointer)
            except json.JSONDecodeError as exc:
                raise TargetSeriesError(
                    "layered history source pointer is invalid"
                ) from exc
            if not isinstance(pointer, dict) or canonical_json_bytes(
                pointer
            ) != raw_pointer.encode("utf-8"):
                raise TargetSeriesError(
                    "layered history source pointer is not canonical"
                )
            pointer_schema_version = pointer.get("pointer_schema_version")
            expected_pointer_keys = {
                "artifact_inventory_sha256",
                "generation_id",
                "manifest_sha256",
                "pointer_schema_version",
                "state",
            }
            if set(pointer) != expected_pointer_keys or pointer_schema_version != 2:
                raise TargetSeriesError(
                    "layered history source pointer contract differs"
                )
            pointer_input = inputs.get(pointer_input_id)
            manifest_input = inputs[source_manifest_input_ids[source_name]]
            if pointer_input is None or (
                pointer_input.get("artifact_role"),
                pointer_input.get("generation_id"),
                pointer_input.get("sha256"),
                pointer_input.get("size_bytes"),
                pointer.get("generation_id"),
                pointer.get("manifest_sha256"),
                pointer.get("state"),
            ) != (
                "CURRENT_POINTER",
                manifest_input.get("generation_id"),
                hashlib.sha256(raw_pointer.encode("utf-8")).hexdigest(),
                len(raw_pointer.encode("utf-8")),
                manifest_input.get("generation_id"),
                manifest_input.get("sha256"),
                "PUBLISHED",
            ):
                raise TargetSeriesError(
                    f"layered history current pointer differs: {source_name}"
                )
            inventory_sha = pointer.get("artifact_inventory_sha256")
            if (
                not isinstance(inventory_sha, str)
                or len(inventory_sha) != 64
                or any(ch not in "0123456789abcdef" for ch in inventory_sha)
                or inventory_sha
                != _source_generation_inventory_sha256(
                    source_name,
                    raw_source_manifests[source_name].encode("utf-8"),
                    source_manifests[source_name],
                )
            ):
                raise TargetSeriesError(
                    f"layered history current pointer inventory differs: {source_name}"
                )
    manifests_by_generation = {
        str(value.get("generation_id") or value.get("bundle_id")): value
        for value in source_manifests.values()
    }
    for input_id, artifact_input in inputs.items():
        if artifact_input.get("artifact_role") != "GENERATION_ARTIFACT":
            continue
        source_generation_id = str(artifact_input.get("generation_id") or "")
        source_manifest = manifests_by_generation.get(source_generation_id)
        if source_manifest is None:
            raise TargetSeriesError(
                f"layered history source generation is absent: {input_id}"
            )
        artifact_id = str(artifact_input.get("artifact_id") or "")
        if input_id == "strategy-history-returns":
            raw_artifact = source_manifest.get("returns_artifact")
            artifact_identity = (
                (
                    raw_artifact.get("artifact_id"),
                    raw_artifact.get("sha256"),
                    raw_artifact.get("size_bytes"),
                )
                if isinstance(raw_artifact, dict)
                else None
            )
        else:
            artifact = _manifest_artifacts(source_manifest.get("artifacts")).get(
                artifact_id
            )
            artifact_identity = (
                (artifact.artifact_id, artifact.sha256, artifact.size_bytes)
                if artifact is not None
                else None
            )
        if artifact_identity != (
            artifact_id,
            artifact_input.get("sha256"),
            artifact_input.get("size_bytes"),
        ):
            raise TargetSeriesError(
                f"layered history source artifact differs: {input_id}"
            )
    output_rows = manifest.get("outputs")
    if not isinstance(output_rows, dict) or set(output_rows) != set(
        LAYERED_HISTORY_OUTPUT_NAMES
    ):
        raise TargetSeriesError("layered history output identities differ")
    if set(artifacts) != set(LAYERED_HISTORY_OUTPUT_NAMES):
        raise TargetSeriesError("layered history output set differs")
    for name in LAYERED_HISTORY_OUTPUT_NAMES:
        expected = output_rows.get(name)
        content = artifacts[name]
        if isinstance(expected, dict):
            _require_exact_keys(
                expected,
                frozenset(
                    {
                        "artifact_id",
                        "row_count",
                        "schema_name",
                        "schema_version",
                        "sha256",
                        "size_bytes",
                    }
                ),
                f"layered history output identity {name}",
            )
        if not isinstance(expected, dict) or (
            expected.get("sha256"),
            expected.get("size_bytes"),
        ) != (hashlib.sha256(content).hexdigest(), len(content)):
            raise TargetSeriesError(f"layered history output differs: {name}")

    segments = _csv_content_rows(
        artifacts["segments.csv"],
        LAYERED_HISTORY_SEGMENT_FIELDS,
        "layered history segments",
    )
    if artifacts["segments.csv"] != _segment_view_bytes(segments):
        raise TargetSeriesError("layered history segments serialization differs")
    canonical_segments = sorted(
        segments,
        key=lambda row: (
            row["strategy_id"],
            row["date"],
            row["evidence_layer"],
            row["segment_id"],
        ),
    )
    if segments != canonical_segments:
        raise TargetSeriesError("layered history segment ordering differs")
    actual_segment_identities = _materialized_segment_identities(segments, sources)
    if actual_segment_identities != contract["history_corrections"].get(
        "materialized_segment_identities"
    ):
        raise TargetSeriesError(
            "layered history segment values differ from product authority"
        )
    excluded = _csv_content_rows(
        artifacts["excluded.csv"], TARGET_FIELDS, "excluded history evidence"
    )
    if artifacts["excluded.csv"] != _target_bytes(excluded):
        raise TargetSeriesError("excluded history evidence serialization differs")
    canonical_excluded = sorted(
        excluded, key=lambda row: (row["series_id"], row["date"])
    )
    if excluded != canonical_excluded:
        raise TargetSeriesError("excluded history evidence ordering differs")
    live_only = _csv_content_rows(
        artifacts["live-only.csv"], CORRELATION_FIELDS_V2, "LIVE_ONLY history"
    )
    if artifacts["live-only.csv"] != _correlation_view_bytes(live_only):
        raise TargetSeriesError("LIVE_ONLY history serialization differs")
    hybrid = _csv_content_rows(
        artifacts["live-plus-backtest.csv"],
        CORRELATION_FIELDS_V2,
        "LIVE_PLUS_BACKTEST history",
    )
    if artifacts["live-plus-backtest.csv"] != _correlation_view_bytes(hybrid):
        raise TargetSeriesError("LIVE_PLUS_BACKTEST history serialization differs")
    returns = _csv_content_rows(
        artifacts["returns.csv"], CORRELATION_FIELDS_V2, "correlation history"
    )
    if artifacts["returns.csv"] != _correlation_view_bytes(returns):
        raise TargetSeriesError("correlation history serialization differs")
    output_counts = {
        "segments.csv": len(segments),
        "excluded.csv": len(excluded),
        "returns.csv": len(returns),
        "live-only.csv": len(live_only),
        "live-plus-backtest.csv": len(hybrid),
    }
    expected_schema = {
        "segments.csv": ("portfolio-layered-history-segments", "1"),
        "excluded.csv": ("portfolio-layered-history-excluded-targets", "1"),
        "returns.csv": ("portfolio-layered-strategy-returns", "2"),
        "live-only.csv": ("portfolio-layered-strategy-returns", "2"),
        "live-plus-backtest.csv": ("portfolio-layered-strategy-returns", "2"),
        "coverage.json": ("portfolio-layered-history-coverage", "1"),
    }
    for name, count in output_counts.items():
        identity = output_rows[name]
        if (
            identity.get("artifact_id") != f"layered-history/{name}"
            or (identity.get("schema_name"), identity.get("schema_version"))
            != expected_schema[name]
            or identity.get("row_count") != count
        ):
            raise TargetSeriesError(f"layered history output metadata differs: {name}")
    if returns != hybrid:
        raise TargetSeriesError("correlation history differs from hybrid view")
    expected_live = [
        {field: row[field] for field in CORRELATION_FIELDS_V2}
        for row in segments
        if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    expected_live.sort(key=lambda row: (row["series_id"], row["date"]))
    expected_hybrid = [
        {field: row[field] for field in CORRELATION_FIELDS_V2}
        for row in segments
        if row["mode_admission"]
        in {"INCLUDE_SELECTED_LIVE", "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"}
    ]
    expected_hybrid.sort(
        key=lambda row: (
            row["series_id"],
            row["date"],
            0 if row["evidence_layer"] == "BACKTEST" else 1,
        )
    )
    if live_only != expected_live or hybrid != expected_hybrid:
        raise TargetSeriesError("layered history mode projection differs")
    segment_keys: set[tuple[str, str, str]] = set()
    rows_by_strategy: dict[str, list[dict[str, str]]] = {
        strategy_id: [] for strategy_id in strategies
    }
    for segment_row in segments:
        strategy_id = segment_row["strategy_id"]
        if strategy_id not in strategies:
            raise TargetSeriesError("layered history contains non-product strategy")
        key = (
            strategy_id,
            segment_row["segment_id"],
            segment_row["date"],
        )
        if key in segment_keys:
            raise TargetSeriesError("layered history contains duplicate segment date")
        segment_keys.add(key)
        try:
            observed = date.fromisoformat(segment_row["date"])
        except ValueError as exc:
            raise TargetSeriesError("layered history date is invalid") from exc
        if observed > accepted_as_of:
            raise TargetSeriesError("layered history exceeds accepted as-of")
        if _decimal(segment_row["return"], "layered history return") <= -1:
            raise TargetSeriesError("layered history return is invalid")
        if segment_row["evidence_layer"] in {
            "PROVIDER_LIVE",
            "PROVIDER_TRACK_RECORD",
        } and segment_row["mode_admission"] not in {
            "EXCLUDE_COMPARISON_ONLY",
            "REQUIRED_NOT_READY_OVERLAP",
        }:
            raise TargetSeriesError("comparison history entered a selected mode")
        rows_by_strategy[strategy_id].append(segment_row)

    for strategy_id, rows in rows_by_strategy.items():
        live = sorted(
            [row for row in rows if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"],
            key=lambda row: row["date"],
        )
        if len({row["date"] for row in live}) != len(live):
            raise TargetSeriesError("selected live history contains duplicate date")
        backtest = sorted(
            [
                row
                for row in rows
                if row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
            ],
            key=lambda row: row["date"],
        )
        if backtest:
            if not live:
                raise TargetSeriesError("admitted backtest lacks selected live path")
            if backtest[0]["strategy_version"] != live[0]["strategy_version"]:
                raise TargetSeriesError("admitted backtest version differs from live")
            if date.fromisoformat(backtest[-1]["date"]) >= date.fromisoformat(
                live[0]["date"]
            ):
                raise TargetSeriesError("admitted backtest overlaps account-live")

    try:
        coverage = json.loads(artifacts["coverage.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError("layered history coverage is invalid") from exc
    if not isinstance(coverage, dict):
        raise TargetSeriesError("layered history coverage contract differs")
    _require_exact_keys(
        coverage,
        frozenset(
            {
                "as_of_date",
                "builder",
                "builder_version",
                "correction_resolutions",
                "epoch_hpr_receipts",
                "mode_policy",
                "schema_version",
                "strategies",
                "verification",
            }
        ),
        "layered history coverage",
    )
    if canonical_json_bytes(coverage) != artifacts["coverage.json"]:
        raise TargetSeriesError("layered history coverage is not canonical")
    if coverage.get("verification") != (
        "VERIFIED_WITH_REQUIRED_NOT_READY_DISPOSITIONS"
    ):
        raise TargetSeriesError("layered history coverage contract differs")
    if (
        coverage.get("as_of_date"),
        coverage.get("builder"),
        coverage.get("builder_version"),
        coverage.get("schema_version"),
    ) != (
        accepted_as_of.isoformat(),
        LAYERED_HISTORY_BUILDER,
        LAYERED_HISTORY_BUILDER_VERSION,
        LAYERED_HISTORY_SCHEMA_VERSION,
    ):
        raise TargetSeriesError("layered history coverage metadata differs")
    mode_policy = coverage.get("mode_policy")
    if not isinstance(mode_policy, dict):
        raise TargetSeriesError("layered history mode policy differs")
    _require_exact_keys(
        mode_policy,
        frozenset(LAYERED_HISTORY_MODE_POLICY),
        "layered history mode policy",
    )
    if mode_policy != LAYERED_HISTORY_MODE_POLICY:
        raise TargetSeriesError("layered history mode policy differs")
    epoch_hpr_rows = coverage.get("epoch_hpr_receipts")
    if not isinstance(epoch_hpr_rows, list):
        raise TargetSeriesError("layered history epoch HPR receipts are absent")
    serialized_coverage = artifacts["coverage.json"].decode("utf-8")
    if any(
        token in serialized_coverage
        for token in ("C:\\\\", "/Users/", "provider_entity_id", "account_id")
    ):
        raise TargetSeriesError("layered history coverage exposes a private locator")
    coverage_rows = coverage.get("strategies")
    if not isinstance(coverage_rows, list):
        raise TargetSeriesError("layered history strategy coverage is absent")
    if any(not isinstance(raw, dict) for raw in coverage_rows) or [
        str(raw.get("strategy_id", "")) for raw in coverage_rows
    ] != sorted(str(raw.get("strategy_id", "")) for raw in coverage_rows):
        raise TargetSeriesError("layered history strategy coverage ordering differs")
    coverage_by_strategy: dict[str, dict[str, object]] = {}
    for raw in coverage_rows:
        if not isinstance(raw, dict):
            raise TargetSeriesError("layered history strategy coverage is invalid")
        strategy_id = str(raw.get("strategy_id", ""))
        if not strategy_id or strategy_id in coverage_by_strategy:
            raise TargetSeriesError("layered history strategy coverage is duplicated")
        coverage_by_strategy[strategy_id] = dict(raw)
    if set(coverage_by_strategy) != set(strategies):
        raise TargetSeriesError("layered history strategy coverage set differs")
    coverage_identity = output_rows["coverage.json"]
    if (
        coverage_identity.get("artifact_id") != "layered-history/coverage.json"
        or (
            coverage_identity.get("schema_name"),
            coverage_identity.get("schema_version"),
        )
        != expected_schema["coverage.json"]
        or coverage_identity.get("row_count") != len(coverage_by_strategy)
    ):
        raise TargetSeriesError(
            "layered history output metadata differs: coverage.json"
        )
    expected_modes_by_strategy: dict[str, dict[str, dict[str, str | None]]] = {}
    source_generation_inputs = {
        "owner-account-live-history": "provider-ledger-manifest",
        "matrixport-account-generation": "matrixport-manifest",
        "quantbase-account-generation": "quantbase-manifest",
        "portfolio123-model-generation": "portfolio123-manifest",
    }
    for strategy_id, raw in coverage_by_strategy.items():
        _require_exact_keys(
            raw,
            frozenset(
                {
                    "blockers",
                    "data_age",
                    "funded_membership",
                    "modes",
                    "product_readiness",
                    "segments",
                    "series_id",
                    "strategy_id",
                    "strategy_version",
                }
            ),
            "layered history strategy coverage",
        )
        strategy = strategies[strategy_id]
        if raw.get("series_id") != strategy.get("history_series_id"):
            raise TargetSeriesError(
                "layered history series identity differs from product authority"
            )
        expected_blockers = sorted(
            int(item) for item in strategy.get("blockers", []) if isinstance(item, int)
        )
        if (
            raw.get("blockers"),
            raw.get("funded_membership"),
            raw.get("product_readiness"),
        ) != (
            expected_blockers,
            strategy.get("funded_membership"),
            strategy.get("readiness"),
        ):
            raise TargetSeriesError("layered history strategy coverage differs")
        strategy_version = raw.get("strategy_version")
        if not isinstance(strategy_version, dict):
            raise TargetSeriesError("layered history strategy version is absent")
        product_version = strategies[strategy_id].get("strategy_version")
        product_version_ref = (
            product_version.get("version_ref")
            if isinstance(product_version, dict)
            else None
        )
        expected_version_status, expected_version_ref = _contract_version(
            strategies[strategy_id]
        )
        if strategy_version != {
            "history_generation_ref": expected_version_ref,
            "product_status": expected_version_status,
            "product_version_ref": product_version_ref,
        }:
            raise TargetSeriesError("layered history strategy version differs")
        summaries = raw.get("segments")
        if not isinstance(summaries, list):
            raise TargetSeriesError("layered history segment coverage is absent")
        summary_ids = [
            str(item.get("segment_id", ""))
            for item in summaries
            if isinstance(item, dict)
        ]
        if (
            len(summary_ids) != len(summaries)
            or len(set(summary_ids)) != len(summary_ids)
            or summary_ids != sorted(summary_ids)
        ):
            raise TargetSeriesError("layered history segment coverage ordering differs")
        actual_rows = rows_by_strategy[strategy_id]
        if strategy_id == "echotrade-growth-pivots" and actual_rows:
            raise TargetSeriesError("EchoTrade event-only history was admitted")
        actual_by_segment = {
            segment_id: [row for row in actual_rows if row["segment_id"] == segment_id]
            for segment_id in {row["segment_id"] for row in actual_rows}
        }
        if {
            str(item.get("segment_id")) for item in summaries if isinstance(item, dict)
        } != set(actual_by_segment):
            raise TargetSeriesError("layered history segment coverage set differs")
        for summary in summaries:
            if not isinstance(summary, dict):
                raise TargetSeriesError("layered history segment summary is invalid")
            _require_exact_keys(
                summary,
                frozenset(
                    {
                        "calendar",
                        "evidence_layer",
                        "frequency",
                        "gap_disposition",
                        "mode_admission",
                        "observed_end",
                        "observed_start",
                        "return_observations",
                        "segment_id",
                        "sha256",
                        "source_generation_id",
                        "source_id",
                        "strategy_version",
                    }
                ),
                "layered history segment summary",
            )
            selected = actual_by_segment[str(summary["segment_id"])]
            identity = _row_identity(selected)
            segment_unavailable_dates = (
                matrixport_unavailable_return_dates
                if strategy_id == "robuxio-crypto"
                and selected[0]["source_id"] == "matrixport-account-generation"
                else frozenset()
            )
            if (
                summary.get("sha256"),
                summary.get("return_observations"),
                summary.get("observed_start"),
                summary.get("observed_end"),
                summary.get("mode_admission"),
                summary.get("gap_disposition"),
            ) != (
                identity["sha256"],
                identity["return_observations"],
                identity["observed_start"],
                identity["observed_end"],
                selected[0]["mode_admission"],
                _gap_disposition(selected, segment_unavailable_dates),
            ):
                raise TargetSeriesError("layered history segment summary differs")
            if selected[0]["mode_admission"] == "INCLUDE_SELECTED_LIVE" and str(
                summary["gap_disposition"]
            ).startswith("REQUIRED_NOT_READY"):
                raise TargetSeriesError(
                    f"layered history has an unexplained live gap: {strategy_id}"
                )

            segment_id = str(summary["segment_id"])
            if segment_id == f"{strategy_id}:account-live":
                source_id = (
                    "matrixport-account-generation"
                    if strategy_id == "robuxio-crypto"
                    else "quantbase-account-generation"
                    if strategy_id == "volatis-strategy"
                    else "owner-account-live-history"
                )
                expected_layer = "ACCOUNT_LIVE"
                expected_admission = "INCLUDE_SELECTED_LIVE"
                expected_basis = "NET"
            elif (
                strategy_id == "robuxio-crypto"
                and segment_id == "robuxio-crypto:provider-live-before-funding"
            ):
                source_id = "matrixport-account-generation"
                expected_layer = "PROVIDER_LIVE"
                expected_admission = "EXCLUDE_COMPARISON_ONLY"
                expected_basis = "NET"
            elif segment_id in {
                f"{strategy_id}:backtest",
                f"{strategy_id}:backtest-not-admitted",
            }:
                source_id = "strategy-backtests"
                expected_layer = "BACKTEST"
                expected_admission = (
                    "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
                    if segment_id.endswith(":backtest")
                    else "REQUIRED_NOT_READY_OVERLAP"
                )
                expected_basis = "MODEL"
            else:
                raise TargetSeriesError("layered history segment is not authorized")
            calendar, frequency = _source_temporal(sources[source_id])
            if source_id == "strategy-backtests":
                generation_input_id = (
                    "portfolio123-manifest"
                    if strategy_id == "p123-ai-factor"
                    else "strategy-history-manifest"
                )
            else:
                generation_input_id = source_generation_inputs[source_id]
            generation_input = inputs.get(generation_input_id)
            expected_identity = (
                source_id,
                str((generation_input or {}).get("generation_id") or "NOT_CAPTURED"),
                expected_layer,
                expected_admission,
                expected_basis,
                "TOTAL",
                calendar,
                frequency,
                raw.get("series_id"),
                strategy_version.get("history_generation_ref"),
            )
            if any(
                (
                    row["source_id"],
                    row["source_generation_id"],
                    row["evidence_layer"],
                    row["mode_admission"],
                    row["return_basis"],
                    row["return_type"],
                    row["calendar"],
                    row["frequency"],
                    row["series_id"],
                    row["strategy_version"],
                )
                != expected_identity
                for row in selected
            ) or (
                summary.get("source_id"),
                summary.get("source_generation_id"),
                summary.get("evidence_layer"),
                summary.get("calendar"),
                summary.get("frequency"),
                summary.get("strategy_version"),
            ) != (
                source_id,
                expected_identity[1],
                expected_layer,
                calendar,
                frequency,
                expected_identity[9],
            ):
                raise TargetSeriesError("layered history segment authority differs")

        live_rows = sorted(
            [
                row
                for row in actual_rows
                if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
            ],
            key=lambda row: row["date"],
        )
        live_unavailable_dates = (
            matrixport_unavailable_return_dates
            if strategy_id == "robuxio-crypto"
            else frozenset()
        )
        expected_data_age: dict[str, object] | None = None
        live_status: str
        live_reason: str | None
        exclusion_reason = _excluded_strategy_mode_reason(strategies[strategy_id])
        if exclusion_reason is not None:
            if actual_rows:
                raise TargetSeriesError(
                    f"excluded strategy entered layered history: {strategy_id}"
                )
            live_status = "NOT_APPLICABLE_OWNER_APPROVED"
            live_reason = exclusion_reason
        elif not live_rows:
            live_status = "REQUIRED_NOT_READY"
            live_reason = (
                "CAPTURED_EVENT_ONLY_RECONSTRUCTION_NOT_ADMISSIBLE_DAILY_NAV"
                if strategy_id == "echotrade-growth-pivots"
                else "REQUIRED_ACCOUNT_LIVE_SEGMENT_ABSENT"
            )
        elif _gap_disposition(live_rows, live_unavailable_dates).startswith(
            "REQUIRED_NOT_READY"
        ):
            raise TargetSeriesError(
                f"layered history has an unexplained live gap: {strategy_id}"
            )
        else:
            live_source = sources[live_rows[0]["source_id"]]
            freshness = live_source.get("freshness")
            max_age = (
                freshness.get("max_age_days") if isinstance(freshness, dict) else None
            )
            if not isinstance(max_age, int):
                raise TargetSeriesError("layered history source freshness differs")
            complete_through = date.fromisoformat(live_rows[-1]["date"])
            age_days = (accepted_as_of - complete_through).days
            if age_days < 0:
                raise TargetSeriesError(
                    f"layered history exceeds accepted as-of: {strategy_id}"
                )
            expected_data_age = {
                "age_days": age_days,
                "complete_through": complete_through.isoformat(),
                "evaluated_on": accepted_as_of.isoformat(),
                "status": (
                    "WARN_COMPLETE_DATA_AGE"
                    if age_days > max_age
                    else "CURRENT_WITHIN_AGE_THRESHOLD"
                ),
                "warning_after_days": max_age,
            }
            live_status = "AVAILABLE"
            live_reason = (
                "WARN_ACCOUNT_LIVE_COMPLETE_DATA_AGE"
                if expected_data_age["status"] == "WARN_COMPLETE_DATA_AGE"
                else None
            )
        if raw.get("data_age") != expected_data_age:
            raise TargetSeriesError("layered history strategy data age differs")
        hybrid_status = live_status
        hybrid_reason = live_reason
        admitted_backtest = any(
            row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
            for row in actual_rows
        )
        if admitted_backtest and expected_version_status != "EVIDENCED":
            raise TargetSeriesError("unapproved backtest entered a selected mode")
        if (
            exclusion_reason is None
            and hybrid_status == "AVAILABLE"
            and not admitted_backtest
        ):
            hybrid_status = "REQUIRED_NOT_READY"
            if any(
                row["mode_admission"] == "REQUIRED_NOT_READY_OVERLAP"
                for row in actual_rows
            ):
                hybrid_reason = (
                    "REQUIRED_NOT_READY_BACKTEST_SOURCE_OVERLAPS_ACCOUNT_LIVE"
                )
            elif strategy_version.get("product_status") != "EVIDENCED":
                hybrid_reason = "REQUIRED_NOT_READY_VERSION_UNRESOLVED_OR_MISMATCHED"
            else:
                hybrid_reason = "REQUIRED_EXACT_VERSION_BACKTEST_NOT_ADMITTED"
        expected_modes: dict[str, dict[str, str | None]] = {
            "LIVE_ONLY": {"reason": live_reason, "status": live_status},
            "LIVE_PLUS_BACKTEST": {
                "reason": hybrid_reason,
                "status": hybrid_status,
            },
        }
        raw_modes = raw.get("modes")
        if not isinstance(raw_modes, dict) or set(raw_modes) != set(expected_modes):
            raise TargetSeriesError("layered history strategy modes differ")
        for mode_name, expected_mode in expected_modes.items():
            actual_mode = raw_modes.get(mode_name)
            if not isinstance(actual_mode, dict):
                raise TargetSeriesError("layered history strategy mode is invalid")
            _require_exact_keys(
                actual_mode,
                frozenset({"reason", "status"}),
                "layered history strategy mode",
            )
            if actual_mode != expected_mode:
                raise TargetSeriesError("layered history strategy mode reason differs")
        expected_modes_by_strategy[strategy_id] = expected_modes

    resolutions = coverage.get("correction_resolutions")
    if (
        not isinstance(resolutions, dict)
        or set(resolutions) != LAYERED_HISTORY_CORRECTION_RESOLUTION_IDS
    ):
        raise TargetSeriesError("layered history correction resolutions are absent")
    echo_resolution = resolutions.get("echotrade-event-only-account-live-rejected")
    echo_correction = corrections.get("echotrade-event-only-account-live-rejected")
    echo_interval = (
        echo_correction.get("interval") if isinstance(echo_correction, dict) else None
    )
    echo_capture = (
        echo_correction.get("capture") if isinstance(echo_correction, dict) else None
    )
    echo_contract_identity = (
        echo_correction.get("rejected_evidence_identity")
        if isinstance(echo_correction, dict)
        else None
    )
    provider_ledger_catalog_input = inputs.get("provider-ledger-catalog")
    echo_series_id = coverage_by_strategy.get("echotrade-growth-pivots", {}).get(
        "series_id"
    )
    if (
        not excluded
        or not isinstance(echo_series_id, str)
        or not echo_series_id
        or any(
            row["series_id"] != echo_series_id
            or row["evidence_layer"] != "ACCOUNT_LIVE"
            or date.fromisoformat(row["date"]) > accepted_as_of
            or _decimal(row["return"], "excluded return") <= -1
            for row in excluded
        )
        or len({row["date"] for row in excluded}) != len(excluded)
    ):
        raise TargetSeriesError("EchoTrade excluded evidence differs")
    expected_echo_identity = _target_row_identity(excluded)
    if (
        not isinstance(echo_contract_identity, dict)
        or echo_contract_identity.get("canonicalization")
        != "TARGET_FIELDS_CANONICAL_JSON_V1"
        or not isinstance(provider_ledger_catalog_input, dict)
        or echo_contract_identity.get("source_receipt_sha256")
        != provider_ledger_catalog_input.get("sha256")
        or {
            "return_observations": echo_contract_identity.get("return_observations"),
            "sha256": echo_contract_identity.get("sha256"),
        }
        != {
            key: expected_echo_identity[key]
            for key in ("return_observations", "sha256")
        }
    ):
        raise TargetSeriesError(
            "EchoTrade excluded evidence differs from product authority"
        )
    if (
        not isinstance(echo_resolution, dict)
        or set(echo_resolution)
        != {
            "observed_end",
            "observed_start",
            "return_observations",
            "sha256",
            "source_generation_id",
            "status",
        }
        or not isinstance(echo_interval, dict)
        or not isinstance(echo_capture, dict)
        or {
            key: echo_resolution.get(key)
            for key in (
                "observed_end",
                "observed_start",
                "return_observations",
                "sha256",
            )
        }
        != expected_echo_identity
        or (
            echo_resolution.get("observed_start"),
            echo_resolution.get("observed_end"),
            echo_resolution.get("source_generation_id"),
            echo_resolution.get("status"),
        )
        != (
            echo_interval.get("observed_start"),
            echo_interval.get("observed_end"),
            echo_capture.get("generation_id"),
            "REJECTED_EVENT_ONLY_RECONSTRUCTION_NOT_DAILY_NAV",
        )
        or not isinstance(echo_resolution.get("return_observations"), int)
        or echo_resolution["return_observations"] < 1
        or not isinstance(echo_resolution.get("sha256"), str)
        or len(echo_resolution["sha256"]) != 64
    ):
        raise TargetSeriesError("EchoTrade event-only resolution differs")
    p123_ai_resolution = resolutions.get("p123-ai-backtest")
    if not isinstance(p123_ai_resolution, dict):
        raise TargetSeriesError("Portfolio123 AI backtest disposition is absent")
    _require_exact_keys(
        p123_ai_resolution,
        frozenset(
            {
                "index_observations",
                "observed_end",
                "observed_start",
                "relationship_type",
                "return_observations",
                "source_labels",
                "status",
            }
        ),
        "Portfolio123 AI backtest disposition",
    )
    p123_ai_rows = [
        row
        for row in rows_by_strategy["p123-ai-factor"]
        if row["evidence_layer"] == "BACKTEST"
    ]
    p123_ai_coverage_version = coverage_by_strategy["p123-ai-factor"].get(
        "strategy_version"
    )
    if not isinstance(p123_ai_coverage_version, dict):
        raise TargetSeriesError("Portfolio123 AI strategy version is absent")
    p123_ai_product_status = p123_ai_coverage_version.get("product_status")
    if p123_ai_product_status != "EVIDENCED":
        expected_p123_ai_status = (
            "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
        )
        if p123_ai_rows:
            raise TargetSeriesError("unapproved Portfolio123 AI backtest was retained")
    elif any(
        row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        for row in p123_ai_rows
    ):
        expected_p123_ai_status = "ADMITTED_EXACT_VERSION_PREHISTORY"
    elif any(
        row["mode_admission"] == "REQUIRED_NOT_READY_OVERLAP" for row in p123_ai_rows
    ):
        expected_p123_ai_status = "DERIVED_NOT_ADMITTED_OVERLAP"
    else:
        expected_p123_ai_status = (
            "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
        )
    p123_ai_correction = corrections.get("p123-ai-backtest-candidate")
    p123_ai_identity = (
        p123_ai_correction.get("derived_backtest_identity")
        if isinstance(p123_ai_correction, dict)
        else None
    )
    if not isinstance(p123_ai_identity, dict) or p123_ai_resolution != {
        **p123_ai_identity,
        "status": expected_p123_ai_status,
    }:
        raise TargetSeriesError("Portfolio123 AI backtest disposition differs")
    robuxio_resolution = resolutions.get("robuxio-account-live-after-funding")
    if not isinstance(robuxio_resolution, dict):
        raise TargetSeriesError("Matrixport source gap resolution differs")
    _require_exact_keys(
        robuxio_resolution,
        frozenset(
            {
                "account_live_start",
                "observed_end",
                "observed_start",
                "source_gap_policy",
                "status",
            }
        ),
        "Matrixport source gap resolution",
    )
    if robuxio_resolution.get("source_gap_policy") != expected_matrixport_gap_policy:
        raise TargetSeriesError("Matrixport source gap resolution differs")
    p123_resolution = resolutions.get("p123-eu-provider-live")
    if not isinstance(p123_resolution, dict):
        raise TargetSeriesError("Portfolio123 EU return transform disposition differs")
    _require_exact_keys(
        p123_resolution,
        frozenset(
            {
                "calendar",
                "cash_added_treatment",
                "equity_overlap_observations",
                "observed_end",
                "observed_start",
                "raw_level_observations",
                "return_observations",
                "status",
                "transform_id",
            }
        ),
        "Portfolio123 EU return transform disposition",
    )
    p123_correction = corrections.get("p123-eu-provider-live")
    p123_evidence_identity = (
        p123_correction.get("derived_evidence_identity")
        if isinstance(p123_correction, dict)
        else None
    )
    if not isinstance(p123_evidence_identity, dict) or p123_resolution != {
        **p123_evidence_identity,
        "status": "DERIVED_NOT_ADMITTED_PENDING_303",
    }:
        raise TargetSeriesError("Portfolio123 EU return transform disposition differs")
    if any(
        row["strategy_id"] == "p123-eu" and row["evidence_layer"] == "PROVIDER_LIVE"
        for row in segments
    ):
        raise TargetSeriesError("Portfolio123 EU candidate transform was admitted")
    volatis_resolution = resolutions.get("volatis-account-live")
    if not isinstance(volatis_resolution, dict):
        raise TargetSeriesError("Volatis account-live resolution differs")
    _require_exact_keys(
        volatis_resolution,
        frozenset(
            {
                "observed_end",
                "observed_start",
                "source_first_positive_value_date",
                "status",
            }
        ),
        "Volatis account-live resolution",
    )
    volatis_correction = corrections.get("volatis-account-live")
    volatis_interval = (
        volatis_correction.get("interval")
        if isinstance(volatis_correction, dict)
        else None
    )
    volatis_funding = (
        volatis_correction.get("funding_event")
        if isinstance(volatis_correction, dict)
        else None
    )
    if (
        not isinstance(volatis_interval, dict)
        or not isinstance(volatis_funding, dict)
        or volatis_resolution
        != {
            "observed_end": volatis_interval.get("observed_end"),
            "observed_start": volatis_interval.get("observed_start"),
            "source_first_positive_value_date": volatis_funding.get("event_date"),
            "status": "RESOLVED_BY_PIPE_HISTORY_001",
        }
    ):
        raise TargetSeriesError("Volatis account-live resolution differs")

    def _correction_interval(correction_id: str) -> Mapping[str, Any]:
        value = corrections[correction_id].get("interval")
        if not isinstance(value, dict):
            raise TargetSeriesError(
                f"history correction interval is absent: {correction_id}"
            )
        return value

    robuxio_live = sorted(
        [
            row
            for row in rows_by_strategy["robuxio-crypto"]
            if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
        ],
        key=lambda row: row["date"],
    )
    robuxio_provider = sorted(
        [
            row
            for row in rows_by_strategy["robuxio-crypto"]
            if row["evidence_layer"] == "PROVIDER_LIVE"
        ],
        key=lambda row: row["date"],
    )
    robuxio_interval = _correction_interval("robuxio-account-live-after-funding")
    robuxio_provider_interval = _correction_interval(
        "robuxio-provider-live-before-funding"
    )
    funding_start = date.fromisoformat(str(robuxio_interval["effective_start"]))
    _validate_robuxio_provider_interval(robuxio_provider, robuxio_provider_interval)
    if (
        not robuxio_live
        or robuxio_live[0]["date"] != robuxio_interval["observed_start"]
        or robuxio_live[-1]["date"] != robuxio_interval["observed_end"]
        or any(date.fromisoformat(row["date"]) <= funding_start for row in robuxio_live)
        or any(
            date.fromisoformat(row["date"]) >= funding_start for row in robuxio_provider
        )
    ):
        raise TargetSeriesError("Robuxio funding correction differs")
    if robuxio_resolution != {
        "account_live_start": funding_start.isoformat(),
        "observed_end": robuxio_live[-1]["date"],
        "observed_start": robuxio_live[0]["date"],
        "source_gap_policy": expected_matrixport_gap_policy,
        "status": "RESOLVED_BY_PIPE_HISTORY_001",
    }:
        raise TargetSeriesError("Robuxio source gap coverage differs")
    if len(epoch_hpr_rows) != 1 or not isinstance(epoch_hpr_rows[0], dict):
        raise TargetSeriesError("layered history epoch HPR receipt set differs")
    epoch_hpr_row = epoch_hpr_rows[0]
    _require_exact_keys(
        epoch_hpr_row,
        frozenset(
            {
                "evidence_layer",
                "receipt",
                "source_generation_id",
                "source_id",
                "strategy_id",
            }
        ),
        "layered history epoch HPR receipt",
    )
    raw_span_receipts = expected_matrixport_gap_policy.get("span_return_receipts")
    if not isinstance(raw_span_receipts, list) or any(
        not isinstance(item, dict) for item in raw_span_receipts
    ):
        raise TargetSeriesError("Matrixport epoch HPR span receipts are invalid")
    expected_epoch_hpr_receipt = _matrixport_epoch_hpr_receipt(
        returns=[
            {
                "date": row["date"],
                "return": row["return"],
            }
            for row in robuxio_live
        ],
        span_return_receipts=raw_span_receipts,
        source_gap_policy=expected_matrixport_gap_policy,
        admission_cutoff=date.fromisoformat(str(robuxio_interval["observed_end"])),
    )
    if epoch_hpr_row != {
        "evidence_layer": "ACCOUNT_LIVE",
        "receipt": expected_epoch_hpr_receipt,
        "source_generation_id": source_manifests["matrixport"]["generation_id"],
        "source_id": "matrixport-account-generation",
        "strategy_id": "robuxio-crypto",
    }:
        raise TargetSeriesError("layered history epoch HPR receipt differs")
    for strategy_id, correction_id in (
        ("p123-eu", "p123-eu-account-live"),
        ("volatis-strategy", "volatis-account-live"),
    ):
        live = sorted(
            [
                row
                for row in rows_by_strategy[strategy_id]
                if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
            ],
            key=lambda row: row["date"],
        )
        interval = _correction_interval(correction_id)
        if not live or (live[0]["date"], live[-1]["date"]) != (
            interval["observed_start"],
            interval["observed_end"],
        ):
            raise TargetSeriesError(f"history correction differs: {strategy_id}")
    if any(
        row["evidence_layer"] == "BACKTEST"
        and row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        for row in rows_by_strategy["volatis-strategy"]
    ):
        raise TargetSeriesError("Volatis provider path was relabeled as backtest")

    mode_status = manifest.get("mode_status")
    if not isinstance(mode_status, dict):
        raise TargetSeriesError("layered history mode status is absent")
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        status = mode_status.get(mode)
        if not isinstance(status, dict):
            raise TargetSeriesError(f"layered history mode status differs: {mode}")
        _require_exact_keys(
            status,
            frozenset({"required_not_ready_strategy_ids", "status"}),
            f"layered history mode status {mode}",
        )
        not_ready: list[str] = []
        for strategy_id, expected_strategy_modes in expected_modes_by_strategy.items():
            if expected_strategy_modes[mode]["status"] == "REQUIRED_NOT_READY":
                not_ready.append(strategy_id)
        not_ready.sort()
        if status.get("required_not_ready_strategy_ids") != not_ready or status.get(
            "status"
        ) != ("AVAILABLE" if not not_ready else "REQUIRED_NOT_READY"):
            raise TargetSeriesError(f"layered history mode readiness differs: {mode}")
    if require_mode is not None:
        if require_mode not in {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
            raise TargetSeriesError("required history mode is invalid")
        required = mode_status[require_mode]
        if not isinstance(required, dict) or required.get("status") != "AVAILABLE":
            raise TargetSeriesError(
                f"required history mode is not ready: {require_mode}"
            )
    if (assurance_profile is None) != (evaluated_on is None):
        raise TargetSeriesError(
            "assurance profile and evaluation date must be supplied together"
        )
    if evaluated_on is not None and (
        not isinstance(evaluated_on, date) or isinstance(evaluated_on, datetime)
    ):
        raise TargetSeriesError("assurance evaluation date is invalid")
    result: dict[str, object] = {
        "generation_id": generation_id,
        "mode_status": mode_status,
        "output_sha256": {
            name: hashlib.sha256(artifacts[name]).hexdigest()
            for name in LAYERED_HISTORY_OUTPUT_NAMES
        },
        "strategy_count": len(coverage_by_strategy),
        "verification": "VALID",
    }
    if assurance_profile is not None and evaluated_on is not None:
        result["assurance"] = _layered_history_assurance_disposition(
            contract=contract,
            receipt_as_of=accepted_as_of,
            assurance_profile=assurance_profile,
            evaluated_on=evaluated_on,
        )
    return result


def write_layered_history_generation(
    output_root: Path,
    artifacts: Mapping[str, bytes],
    manifest_content: bytes,
) -> Path:
    try:
        manifest = json.loads(manifest_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError(
            "layered history generation manifest is invalid"
        ) from exc
    if not isinstance(manifest, dict):
        raise TargetSeriesError("layered history generation manifest must be an object")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("layered history generation identity is invalid")
    destination = output_root / generation_id
    for name in LAYERED_HISTORY_OUTPUT_NAMES:
        _write_exact(destination / name, artifacts[name])
    _write_exact(destination / "generation.json", manifest_content)
    return destination


def verify_layered_history_generation(
    generation_root: Path,
    contract_path: Path,
    *,
    require_mode: str | None = None,
    assurance_profile: str | None = None,
    evaluated_on: date | None = None,
) -> dict[str, object]:
    if generation_root.is_symlink() or not generation_root.is_dir():
        raise TargetSeriesError("layered history generation root must be a directory")

    expected_names = frozenset((*LAYERED_HISTORY_OUTPUT_NAMES, "generation.json"))
    try:
        entries = tuple(generation_root.iterdir())
    except OSError as exc:
        raise TargetSeriesError(
            "layered history generation inventory is unreadable"
        ) from exc
    if (
        len(entries) != len(expected_names)
        or {entry.name for entry in entries} != expected_names
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise TargetSeriesError("layered history generation inventory differs")

    def _read_generation_file(name: str) -> bytes:
        path = generation_root / name
        if path.is_symlink() or not path.is_file():
            raise TargetSeriesError(
                f"layered history generation file must be regular: {name}"
            )
        return path.read_bytes()

    artifacts = {
        name: _read_generation_file(name) for name in LAYERED_HISTORY_OUTPUT_NAMES
    }
    manifest_content = _read_generation_file("generation.json")
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=contract_path,
        require_mode=require_mode,
        assurance_profile=assurance_profile,
        evaluated_on=evaluated_on,
    )
    if generation_root.name != result["generation_id"]:
        raise TargetSeriesError("layered history generation directory differs")
    return result


def _portfolio123_simulation_returns(
    returns_path: Path,
    relationships_path: Path,
    provider_entity_id: str,
    simulation_entity_id: str,
    series_id: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    relationships = [
        row
        for row in _parquet_rows(
            relationships_path,
            ("entity_ref_a", "entity_ref_b", "relationship_type", "notes"),
            "Portfolio123 strategy relationships",
        )
        if row.get("relationship_type") == "live_sim_twin"
        and {str(row.get("entity_ref_a") or ""), str(row.get("entity_ref_b") or "")}
        == {provider_entity_id, simulation_entity_id}
    ]
    if len(relationships) != 1:
        raise TargetSeriesError(
            "Portfolio123 live/simulation version mapping is not unique"
        )

    indexed: dict[date, Decimal] = {}
    sources: set[str] = set()
    for row in _parquet_rows(
        returns_path,
        ("entity_id", "period_date", "model_return", "source"),
        "Portfolio123 simulation returns",
    ):
        if str(row.get("entity_id") or "") != simulation_entity_id:
            continue
        try:
            day = date.fromisoformat(str(row.get("period_date") or ""))
        except ValueError as exc:
            raise TargetSeriesError(
                "Portfolio123 simulation return date is invalid"
            ) from exc
        level = _positive_decimal(
            row.get("model_return") or "", "Portfolio123 simulation index"
        )
        if day in indexed and indexed[day] != level:
            raise TargetSeriesError(
                "Portfolio123 simulation returns have conflicting dates"
            )
        indexed[day] = level
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
    ordered = sorted(indexed.items())
    if len(ordered) < 2:
        raise TargetSeriesError("Portfolio123 simulation history is insufficient")

    result: list[dict[str, str]] = []
    previous = ordered[0][1]
    for day, level in ordered[1:]:
        period_return = level / previous - Decimal(1)
        if period_return <= -1:
            raise TargetSeriesError("Portfolio123 simulation return is invalid")
        result.append(
            {
                "series_id": series_id,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "BACKTEST",
            }
        )
        previous = level
    return result, {
        "index_observations": len(ordered),
        "observed_end": ordered[-1][0].isoformat(),
        "observed_start": ordered[0][0].isoformat(),
        "relationship_type": "live_sim_twin",
        "return_observations": len(result),
        "source_labels": sorted(sources),
    }


def _strategy_history_returns(
    manifest_path: Path,
    returns_path: Path,
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, object],
]:
    """Admit a hash-bound, multi-strategy history bundle.

    History compatibility and account implementation reconciliation are
    deliberately separate. A current, version-identified model history may be
    used for economic inference even when execution capture or account/model
    reconciliation remains incomplete. The latter status is retained in the
    evidence proof and must never erase the former history.
    """

    manifest = _load_json(manifest_path, "strategy history manifest")
    if manifest.get("schema_version") != STRATEGY_HISTORY_MANIFEST_SCHEMA_VERSION:
        raise TargetSeriesError("strategy history manifest schema is unsupported")
    if (
        manifest.get("builder") != "portfolio-strategy-history"
        or manifest.get("builder_version") not in STRATEGY_HISTORY_BUILDER_VERSIONS
    ):
        raise TargetSeriesError("strategy history builder version is unsupported")
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("strategy history bundle is not verified")
    try:
        history_as_of = date.fromisoformat(str(manifest.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("strategy history as-of date is invalid") from exc
    artifact = manifest.get("returns_artifact")
    if not isinstance(artifact, dict):
        raise TargetSeriesError("strategy history returns artifact is invalid")
    artifact_keys = frozenset(artifact)
    builder_version = str(manifest.get("builder_version"))
    if builder_version == "1.4.0":
        _require_exact_keys(
            artifact,
            frozenset({"artifact_id", "sha256", "size_bytes"}),
            "strategy history returns artifact",
        )
    elif artifact_keys not in {
        frozenset({"sha256", "size_bytes"}),
        frozenset({"artifact_id", "sha256", "size_bytes"}),
    }:
        raise TargetSeriesError("strategy history returns artifact keys differ")
    if "artifact_id" in artifact and artifact.get("artifact_id") != (
        "strategy-history/returns.csv"
    ):
        raise TargetSeriesError("strategy history returns artifact ID differs")
    expected_hash = str(artifact.get("sha256", ""))
    expected_size = artifact.get("size_bytes")
    actual_hash, actual_size = _sha256(returns_path)
    if (
        len(expected_hash) != 64
        or not isinstance(expected_size, int)
        or (actual_hash, actual_size) != (expected_hash, expected_size)
    ):
        raise TargetSeriesError("strategy history returns differ from manifest")

    rows = _csv_rows(
        returns_path,
        frozenset(CORRELATION_FIELDS_V2),
        "strategy history returns",
    )
    if rows and tuple(rows[0]) != CORRELATION_FIELDS_V2:
        raise TargetSeriesError("strategy history return header differs")
    raw_series = manifest.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise TargetSeriesError("strategy history manifest lacks series")
    definitions: dict[str, dict[str, object]] = {}
    for raw in raw_series:
        if not isinstance(raw, dict):
            raise TargetSeriesError("strategy history definition is invalid")
        series_id = str(raw.get("series_id", "")).strip()
        version = str(raw.get("strategy_version", "")).strip()
        status = str(raw.get("history_status", "")).strip()
        observations = raw.get("return_observations")
        if (
            not series_id
            or not version
            or status not in STRATEGY_HISTORY_STATUSES
            or not isinstance(observations, int)
            or observations <= 0
            or series_id in definitions
        ):
            raise TargetSeriesError("strategy history definition is invalid")
        if not isinstance(raw.get("required_for_analysis"), bool):
            raise TargetSeriesError("strategy history required flag is invalid")
        for field in (
            "compatibility_basis",
            "frequency",
            "implementation_reconciliation_status",
            "observed_end",
            "observed_start",
        ):
            if not str(raw.get(field, "")).strip():
                raise TargetSeriesError(
                    f"strategy history {field} is missing: {series_id}"
                )
        try:
            observed_start = date.fromisoformat(str(raw["observed_start"]))
            observed_end = date.fromisoformat(str(raw["observed_end"]))
        except ValueError as exc:
            raise TargetSeriesError(
                f"strategy history interval is invalid: {series_id}"
            ) from exc
        if observed_end < observed_start:
            raise TargetSeriesError(
                f"strategy history interval is reversed: {series_id}"
            )
        definitions[series_id] = dict(raw)

    dispositions: dict[str, dict[str, object]] = {}
    raw_dispositions = manifest.get("history_dispositions", [])
    if not isinstance(raw_dispositions, list):
        raise TargetSeriesError("strategy history dispositions are invalid")
    for raw in raw_dispositions:
        if not isinstance(raw, dict):
            raise TargetSeriesError("strategy history disposition is invalid")
        series_id = str(raw.get("series_id", "")).strip()
        status = str(raw.get("status", "")).strip()
        history_status = str(raw.get("history_status", "")).strip()
        version = str(raw.get("strategy_version", "")).strip()
        implementation = str(
            raw.get("implementation_reconciliation_status", "")
        ).strip()
        reason = str(raw.get("reason", "")).strip()
        if (
            not series_id
            or not status
            or not (
                history_status.startswith("UNAVAILABLE_")
                or history_status
                in {
                    "CAPTURED_EXACT_VERSION_PROVIDER_LIVE_RETURN_TRANSFORM_NOT_ACCEPTED",
                    "CAPTURED_EXACT_VERSION_PROVIDER_LIVE_RETURN_TRANSFORM_DERIVED_NOT_ADMITTED",
                }
            )
            or not version
            or not implementation
            or not reason
            or not isinstance(raw.get("required_for_analysis"), bool)
            or series_id in definitions
            or series_id in dispositions
        ):
            raise TargetSeriesError("strategy history disposition is invalid")
        dispositions[series_id] = dict(raw)

    grouped: dict[str, list[dict[str, str]]] = {
        series_id: [] for series_id in definitions
    }
    seen: set[tuple[str, str]] = set()
    for row in rows:
        series_id = row["series_id"]
        definition = definitions.get(series_id)
        if definition is None:
            raise TargetSeriesError(
                f"strategy history returns contain undeclared series: {series_id}"
            )
        if row["evidence_layer"] != "BACKTEST":
            raise TargetSeriesError("strategy history may contain only BACKTEST rows")
        if row["strategy_version"] != definition["strategy_version"]:
            raise TargetSeriesError(f"strategy history version differs: {series_id}")
        try:
            observed = date.fromisoformat(row["date"])
        except ValueError as exc:
            raise TargetSeriesError(
                f"strategy history date is invalid: {series_id}"
            ) from exc
        value = _decimal(row["return"], f"strategy history return {series_id}")
        if value <= -1:
            raise TargetSeriesError(f"strategy history return is invalid: {series_id}")
        if observed > history_as_of:
            raise TargetSeriesError(
                f"strategy history exceeds its as-of date: {series_id}"
            )
        key = (series_id, observed.isoformat())
        if key in seen:
            raise TargetSeriesError(
                f"strategy history contains duplicate dates: {series_id}"
            )
        seen.add(key)
        grouped[series_id].append(dict(row))

    for series_id, definition in definitions.items():
        accepted = sorted(grouped[series_id], key=lambda item: item["date"])
        if len(accepted) != definition["return_observations"]:
            raise TargetSeriesError(
                f"strategy history observation count differs: {series_id}"
            )
        if (
            not accepted
            or accepted[0]["date"] != definition["observed_start"]
            or accepted[-1]["date"] != definition["observed_end"]
        ):
            raise TargetSeriesError(
                f"strategy history observation bounds differ: {series_id}"
            )
        grouped[series_id] = accepted

    proof = {
        "as_of_date": history_as_of.isoformat(),
        "bundle_id": manifest.get("bundle_id"),
        "manifest_sha256": _sha256(manifest_path)[0],
        "returns_sha256": actual_hash,
        "returns_size_bytes": actual_size,
        "schema_version": manifest["schema_version"],
        "verification": manifest["verification"],
    }
    proof["disposition_count"] = len(dispositions)
    return grouped, definitions, dispositions, proof


def build_layered_correlation_inputs(
    base_spec_path: Path,
    base_returns_path: Path,
    target_content: bytes,
    portfolio123_manifest_path: Path,
    portfolio123_returns_path: Path,
    portfolio123_relationships_path: Path,
    portfolio123_provider_entity_id: str,
    portfolio123_simulation_entity_id: str,
    portfolio123_series_id: str,
    strategy_evidence_content: bytes,
    as_of_date: str,
    strategy_history_manifest_path: Path | None = None,
    strategy_history_returns_path: Path | None = None,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Build a hash-bound v2 live-plus-backtest correlation source.

    Account-live observations remain the selected authority. Portfolio123 is
    one admitted history adapter, not the sole history path. Additional
    strategies are admitted through one verified, hash-bound history bundle.
    The correlation builder later enforces the strict pre-live boundary.
    """

    spec = _load_json(base_spec_path, "base correlation spec")
    source = spec.get("return_source")
    if not isinstance(source, dict):
        raise TargetSeriesError("correlation return source is invalid")
    base_hash, base_size = _sha256(base_returns_path)
    if (
        source.get("expected_sha256") != base_hash
        or source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base correlation return identity differs")

    manifest = _load_json(
        portfolio123_manifest_path, "Portfolio123 generation manifest"
    )
    if manifest.get("verification") != "VERIFIED":
        raise TargetSeriesError("Portfolio123 generation is not verified")
    generation_id = str(manifest.get("generation_id", ""))
    if len(generation_id) != 64:
        raise TargetSeriesError("Portfolio123 generation identity is invalid")
    artifacts = _manifest_artifacts(manifest.get("artifacts"))
    returns_artifact = _verify_artifact(
        portfolio123_returns_path, artifacts, "tables/returns_daily.parquet"
    )
    relationships_artifact = _verify_artifact(
        portfolio123_relationships_path,
        artifacts,
        "tables/strategy_relationships.parquet",
    )
    simulation_rows, simulation_coverage = _portfolio123_simulation_returns(
        portfolio123_returns_path,
        portfolio123_relationships_path,
        portfolio123_provider_entity_id,
        portfolio123_simulation_entity_id,
        portfolio123_series_id,
    )
    try:
        strategy_evidence = json.loads(strategy_evidence_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetSeriesError("strategy evidence is not valid JSON") from exc
    if not isinstance(strategy_evidence, dict):
        raise TargetSeriesError("strategy evidence must be an object")
    strategies = strategy_evidence.get("strategies")
    if not isinstance(strategies, list):
        raise TargetSeriesError("strategy evidence lacks strategies")
    reconciled = [
        item
        for item in strategies
        if isinstance(item, dict)
        and item.get("strategy_name") == portfolio123_series_id
        and isinstance(item.get("provider"), dict)
        and item["provider"].get("entity_id") == portfolio123_provider_entity_id
        and item["provider"].get("provider_id") == "portfolio123"
        and item["provider"].get("generation_id") == generation_id
        and item.get("evidence_layer")
        in {"ACCOUNT_LIVE", "PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE"}
        and isinstance(item.get("account"), dict)
        and isinstance(item.get("overlap"), dict)
        and item["overlap"].get("status") == "TESTED_EXACT_DATE_PATHS"
        and isinstance(item["overlap"].get("observations"), int)
        and item["overlap"]["observations"] > 0
    ]
    if len(reconciled) != 1:
        raise TargetSeriesError(
            "Portfolio123 provider/live account reconciliation is not unique"
        )
    strategy_evidence_hash = hashlib.sha256(strategy_evidence_content).hexdigest()

    if (strategy_history_manifest_path is None) != (
        strategy_history_returns_path is None
    ):
        raise TargetSeriesError(
            "strategy history manifest and returns must be supplied together"
        )
    history_rows: dict[str, list[dict[str, str]]] = {}
    history_definitions: dict[str, dict[str, object]] = {}
    history_dispositions: dict[str, dict[str, object]] = {}
    history_proof: dict[str, object] | None = None
    if (
        strategy_history_manifest_path is not None
        and strategy_history_returns_path is not None
    ):
        (
            history_rows,
            history_definitions,
            history_dispositions,
            history_proof,
        ) = _strategy_history_returns(
            strategy_history_manifest_path, strategy_history_returns_path
        )

    target_rows = _target_rows(target_content)
    series_ids = spec.get("series_ids")
    if not isinstance(series_ids, list) or any(
        not isinstance(item, str) or not item for item in series_ids
    ):
        raise TargetSeriesError("correlation series IDs are invalid")
    requested = set(series_ids)
    if portfolio123_series_id not in requested:
        raise TargetSeriesError("Portfolio123 series is absent from correlation spec")
    unknown_history = (set(history_definitions) | set(history_dispositions)) - requested
    if unknown_history:
        raise TargetSeriesError(
            "strategy history contains series absent from correlation spec"
        )
    if portfolio123_series_id in history_definitions:
        raise TargetSeriesError(
            "Portfolio123 history is declared by more than one accepted adapter"
        )
    unknown = {row["series_id"] for row in target_rows} - requested
    if unknown:
        raise TargetSeriesError("target rows contain unrequested correlation series")

    rows_by_series: dict[str, list[dict[str, str]]] = {
        series_id: [] for series_id in series_ids
    }
    for row in target_rows:
        rows_by_series[row["series_id"]].append(row)
    version_by_series: dict[str, str] = {}
    live_version_by_series: dict[str, str] = {}
    live_layer_by_series: dict[str, str] = {}
    mapping_proof: list[dict[str, object]] = []
    for series_id in series_ids:
        rows = sorted(rows_by_series[series_id], key=lambda item: item["date"])
        layers = {row["evidence_layer"] for row in rows}
        if len(layers) > 1:
            raise TargetSeriesError("a live series has multiple evidence layers")
        live_layer = next(iter(layers), "ACCOUNT_LIVE")
        if live_layer not in {"ACCOUNT_LIVE", "PROVIDER_LIVE", "OFFICIAL_VEHICLE"}:
            raise TargetSeriesError("a correlation series lacks an accepted live layer")
        live_layer_by_series[series_id] = live_layer
        implementation_lineage: object | None = None
        if series_id == portfolio123_series_id:
            version = _strategy_version(
                "p123-live-sim-twin",
                {
                    "generation_id": generation_id,
                    "provider_entity_id": portfolio123_provider_entity_id,
                    "relationship_sha256": relationships_artifact.sha256,
                    "relationship_type": "live_sim_twin",
                    "simulation_entity_id": portfolio123_simulation_entity_id,
                    "strategy_evidence_sha256": strategy_evidence_hash,
                },
            )
            status = "ACCEPTED_GENERATION_BOUND_LIVE_SIM_TWIN"
            backtest_source = "portfolio123-live-sim-twin"
            backtest_observations = len(simulation_rows)
            backtest_observed_start = simulation_rows[0]["date"]
            backtest_observed_end = simulation_rows[-1]["date"]
            compatibility_basis = "EXACT_GENERATION_BOUND_LIVE_SIM_TWIN"
            history_status = "ACCEPTED_EXACT_VERSION_HISTORY"
            implementation_status = "TESTED_EXACT_DATE_PATHS"
            disposition_reason = None
        elif series_id in history_definitions:
            definition = history_definitions[series_id]
            version = str(definition["strategy_version"])
            status = str(definition["history_status"])
            backtest_source = "strategy-history-bundle"
            backtest_observations = int(str(definition["return_observations"]))
            backtest_observed_start = str(definition["observed_start"])
            backtest_observed_end = str(definition["observed_end"])
            compatibility_basis = str(definition["compatibility_basis"])
            history_status = str(definition["history_status"])
            implementation_status = str(
                definition["implementation_reconciliation_status"]
            )
            implementation_lineage = definition.get("implementation_lineage")
            disposition_reason = None
        elif series_id in history_dispositions:
            disposition = history_dispositions[series_id]
            version = str(disposition["strategy_version"])
            status = str(disposition["status"])
            backtest_source = None
            backtest_observations = 0
            backtest_observed_start = None
            backtest_observed_end = None
            compatibility_basis = None
            history_status = str(disposition["history_status"])
            implementation_status = str(
                disposition["implementation_reconciliation_status"]
            )
            disposition_reason = str(disposition["reason"])
        else:
            version = _strategy_version(
                "accepted-live-path",
                {"rows": rows, "series_id": series_id},
            )
            status = "NO_DECLARED_VERSIONED_HISTORY"
            backtest_source = None
            backtest_observations = 0
            backtest_observed_start = None
            backtest_observed_end = None
            compatibility_basis = None
            history_status = "UNAVAILABLE_NOT_DECLARED"
            implementation_status = "NOT_EVALUATED"
            disposition_reason = None
        version_by_series[series_id] = version
        # The live series identity is always the accepted live path, hash-bound
        # to the live rows, regardless of any declared versioned history or
        # generation-bound sim-twin: the baseline/provenance authority attests
        # this value, and the panel verifies parity against it.
        live_version = _strategy_version(
            "accepted-live-path",
            {"rows": rows, "series_id": series_id},
        )
        live_version_by_series[series_id] = live_version
        mapping_proof.append(
            {
                "backtest_observations": backtest_observations,
                "backtest_observed_end": backtest_observed_end,
                "backtest_observed_start": backtest_observed_start,
                "backtest_source": backtest_source,
                "compatibility_basis": compatibility_basis,
                "history_status": history_status,
                "history_disposition_reason": disposition_reason,
                "implementation_lineage": implementation_lineage,
                "implementation_reconciliation_status": implementation_status,
                "live_evidence_layer": live_layer,
                "live_observations": len(rows),
                "live_version_ref": live_version,
                "series_id": series_id,
                "status": status,
                "strategy_version": version,
            }
        )

    combined: list[dict[str, str]] = []
    for series_id in series_ids:
        for row in rows_by_series[series_id]:
            combined.append(
                {
                    "series_id": series_id,
                    "date": row["date"],
                    "return": row["return"],
                    "evidence_layer": row["evidence_layer"],
                    "strategy_version": version_by_series[series_id],
                }
            )
    for row in simulation_rows:
        combined.append(
            {
                **row,
                "strategy_version": version_by_series[portfolio123_series_id],
            }
        )
    for series_id, accepted_rows in history_rows.items():
        for row in accepted_rows:
            combined.append(dict(row))
    series_order = {series_id: index for index, series_id in enumerate(series_ids)}
    combined.sort(
        key=lambda row: (
            series_order[row["series_id"]],
            row["date"],
            0 if row["evidence_layer"] == "BACKTEST" else 1,
        )
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=CORRELATION_FIELDS_V2, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(combined)
    correlation_content = buffer.getvalue().encode("utf-8")

    spec["schema_version"] = 2
    spec["as_of_date"] = max(
        str(spec.get("as_of_date", "")),
        as_of_date,
        str((history_proof or {}).get("as_of_date", "")),
    )
    spec["evidence_mode_series"] = [
        {
            "live_evidence_layer": live_layer_by_series[series_id],
            "live_version_ref": live_version_by_series[series_id],
            "series_id": series_id,
            "strategy_version": version_by_series[series_id],
        }
        for series_id in series_ids
    ]
    source.update(
        {
            "artifact_id": "provider-rollforward/layered-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(correlation_content).hexdigest(),
            "expected_size_bytes": len(correlation_content),
            "relative_path": "returns.csv",
            "schema_name": "portfolio-layered-strategy-returns",
            "schema_version": "2",
        }
    )
    proof = {
        "as_of_date": spec["as_of_date"],
        "builder": "portfolio-target-series/layered-correlation-1",
        "evidence_mode_policy": {
            "default": "LIVE_ONLY",
            "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
            "post_inception_backtest_fill": False,
        },
        "portfolio123_generation_id": generation_id,
        "portfolio123_version_mapping": {
            "provider_entity_id": portfolio123_provider_entity_id,
            "relationship_type": "live_sim_twin",
            "simulation_entity_id": portfolio123_simulation_entity_id,
            "strategy_series_id": portfolio123_series_id,
            "strategy_version": version_by_series[portfolio123_series_id],
        },
        "series_mappings": mapping_proof,
        "simulation_coverage": simulation_coverage,
        "strategy_history_bundle": history_proof,
        "sources": {
            "accepted_target_rows": {
                "sha256": hashlib.sha256(target_content).hexdigest(),
                "size_bytes": len(target_content),
            },
            "account_provider_strategy_evidence": {
                "sha256": strategy_evidence_hash,
                "size_bytes": len(strategy_evidence_content),
            },
            "base_correlation_returns": {
                "sha256": base_hash,
                "size_bytes": base_size,
            },
            "base_correlation_spec": {
                "sha256": _sha256(base_spec_path)[0],
                "size_bytes": base_spec_path.stat().st_size,
            },
            "portfolio123_returns": {
                "artifact_id": returns_artifact.artifact_id,
                "sha256": returns_artifact.sha256,
                "size_bytes": returns_artifact.size_bytes,
            },
            "portfolio123_relationships": {
                "artifact_id": relationships_artifact.artifact_id,
                "sha256": relationships_artifact.sha256,
                "size_bytes": relationships_artifact.size_bytes,
            },
        },
        "target_sha256": hashlib.sha256(correlation_content).hexdigest(),
        "target_size_bytes": len(correlation_content),
    }
    return (
        canonical_json_bytes(spec),
        correlation_content,
        canonical_json_bytes(proof),
        portfolio123_manifest_path.read_bytes(),
    )


def build_correlation_inputs(
    base_spec_path: Path,
    base_returns_path: Path,
    target_content: bytes,
    as_of_date: str,
) -> tuple[bytes, bytes]:
    spec = _load_json(base_spec_path, "base correlation spec")
    source = spec.get("return_source")
    if not isinstance(source, dict):
        raise TargetSeriesError("correlation return source is invalid")
    base_hash, base_size = _sha256(base_returns_path)
    if (
        source.get("expected_sha256") != base_hash
        or source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base correlation return identity differs")
    content = _correlation_bytes(target_content)
    spec["as_of_date"] = max(str(spec.get("as_of_date", "")), as_of_date)
    source.update(
        {
            "artifact_id": "provider-rollforward/account-live-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "expected_size_bytes": len(content),
            "relative_path": "returns.csv",
            "schema_name": "portfolio-account-live-strategy-returns",
            "schema_version": "2",
        }
    )
    return canonical_json_bytes(spec), content


def build_decomposition_inputs(
    base_spec_path: Path,
    factor_path: Path,
    base_targets_path: Path,
    upstream_manifest_path: Path,
    target_content: bytes,
    proof_content: bytes,
) -> tuple[bytes, bytes, bytes]:
    """Rebind an existing decomposition spec to the normalized target artifact."""

    spec = _load_json(base_spec_path, "base decomposition spec")
    target_source = spec.get("target_source")
    factor_source = spec.get("factor_source")
    if not isinstance(target_source, dict) or not isinstance(factor_source, dict):
        raise TargetSeriesError("decomposition spec source contracts are invalid")
    base_hash, base_size = _sha256(base_targets_path)
    if (
        target_source.get("expected_sha256") != base_hash
        or target_source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base decomposition target identity differs")
    factor_hash, factor_size = _sha256(factor_path)
    if (
        factor_source.get("expected_sha256") != factor_hash
        or factor_source.get("expected_size_bytes") != factor_size
    ):
        raise TargetSeriesError("base decomposition factor identity differs")
    proof = json.loads(proof_content)
    coverage = proof.get("coverage")
    if not isinstance(coverage, dict):
        raise TargetSeriesError("target proof coverage is invalid")
    observed_end = str(coverage.get("observed_end", ""))
    try:
        original_as_of = date.fromisoformat(str(spec.get("as_of_date", "")))
        provider_as_of = date.fromisoformat(observed_end)
    except ValueError as exc:
        raise TargetSeriesError("decomposition as-of boundary is invalid") from exc
    spec["as_of_date"] = max(original_as_of, provider_as_of).isoformat()
    target_source.update(
        {
            "artifact_id": "provider-overlay/account-live-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(target_content).hexdigest(),
            "expected_size_bytes": len(target_content),
            "relative_path": "targets.csv",
            "schema_name": "portfolio-account-live-strategy-returns",
            "schema_version": "2",
        }
    )
    proof_hash = hashlib.sha256(proof_content).hexdigest()
    manifest_content = upstream_manifest_path.read_bytes()
    spec["provenance_sources"] = [
        {
            "artifact_id": "provider-overlay/source-proof.json",
            "expected_sha256": proof_hash,
            "expected_size_bytes": len(proof_content),
            "relative_path": "source-proof.json",
            "schema_name": "portfolio-target-source-proof",
            "schema_version": "1",
        },
        {
            "artifact_id": "provider-overlay/matrixport-generation.json",
            "expected_sha256": hashlib.sha256(manifest_content).hexdigest(),
            "expected_size_bytes": len(manifest_content),
            "relative_path": "matrixport-generation.json",
            "schema_name": "generation-manifest",
            "schema_version": "1",
        },
    ]
    return canonical_json_bytes(spec), factor_path.read_bytes(), manifest_content


def build_provider_rollforward_inputs(
    base_spec_path: Path,
    factor_path: Path,
    base_targets_path: Path,
    target_content: bytes,
    quantbase_proof_content: bytes,
    strategy_evidence_content: bytes,
    quantbase_manifest_path: Path,
    portfolio123_manifest_path: Path,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Rebind decomposition inputs to the two-provider roll-forward."""

    spec = _load_json(base_spec_path, "base decomposition spec")
    target_source = spec.get("target_source")
    factor_source = spec.get("factor_source")
    if not isinstance(target_source, dict) or not isinstance(factor_source, dict):
        raise TargetSeriesError("decomposition spec source contracts are invalid")
    base_hash, base_size = _sha256(base_targets_path)
    if (
        target_source.get("expected_sha256") != base_hash
        or target_source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base decomposition target identity differs")
    factor_hash, factor_size = _sha256(factor_path)
    if (
        factor_source.get("expected_sha256") != factor_hash
        or factor_source.get("expected_size_bytes") != factor_size
    ):
        raise TargetSeriesError("base decomposition factor identity differs")
    evidence = json.loads(strategy_evidence_content)
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        raise TargetSeriesError("strategy evidence is invalid")
    try:
        original_as_of = date.fromisoformat(str(spec.get("as_of_date", "")))
        evidence_as_of = date.fromisoformat(str(evidence.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError("provider roll-forward as-of date is invalid") from exc
    spec["as_of_date"] = max(original_as_of, evidence_as_of).isoformat()
    target_source.update(
        {
            "artifact_id": "provider-rollforward/account-live-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(target_content).hexdigest(),
            "expected_size_bytes": len(target_content),
            "relative_path": "targets.csv",
            "schema_name": "portfolio-account-live-strategy-returns",
            "schema_version": "3",
        }
    )
    provenance = spec.get("provenance_sources", [])
    if not isinstance(provenance, list):
        raise TargetSeriesError("decomposition provenance sources are invalid")
    additions = []
    for artifact_id, content, schema_name in (
        (
            "provider-rollforward/quantbase-source-proof.json",
            quantbase_proof_content,
            "portfolio-target-source-proof",
        ),
        (
            "provider-rollforward/strategy-evidence.json",
            strategy_evidence_content,
            "portfolio-strategy-provider-evidence",
        ),
        (
            "provider-rollforward/quantbase-generation.json",
            quantbase_manifest_path.read_bytes(),
            "generation-manifest",
        ),
        (
            "provider-rollforward/portfolio123-generation.json",
            portfolio123_manifest_path.read_bytes(),
            "generation-manifest",
        ),
    ):
        additions.append(
            {
                "artifact_id": artifact_id,
                "expected_sha256": hashlib.sha256(content).hexdigest(),
                "expected_size_bytes": len(content),
                "relative_path": artifact_id.rsplit("/", 1)[1],
                "schema_name": schema_name,
                "schema_version": "1",
            }
        )
    spec["provenance_sources"] = [*provenance, *additions]
    return (
        canonical_json_bytes(spec),
        factor_path.read_bytes(),
        quantbase_manifest_path.read_bytes(),
        portfolio123_manifest_path.read_bytes(),
    )


def build_provider_ledger_decomposition_inputs(
    base_spec_path: Path,
    factor_path: Path,
    base_targets_path: Path,
    target_content: bytes,
    evidence_content: bytes,
    proof_content: bytes,
    provider_ledger_manifest_path: Path,
    provider_ledger_catalog_path: Path,
) -> tuple[bytes, bytes, dict[str, bytes]]:
    """Rebind one decomposition lens to the accepted provider-ledger series."""

    spec = _load_json(base_spec_path, "base decomposition spec")
    target_source = spec.get("target_source")
    factor_source = spec.get("factor_source")
    if not isinstance(target_source, dict) or not isinstance(factor_source, dict):
        raise TargetSeriesError("decomposition spec source contracts are invalid")
    base_hash, base_size = _sha256(base_targets_path)
    if (
        target_source.get("expected_sha256") != base_hash
        or target_source.get("expected_size_bytes") != base_size
    ):
        raise TargetSeriesError("base decomposition target identity differs")
    factor_hash, factor_size = _sha256(factor_path)
    if (
        factor_source.get("expected_sha256") != factor_hash
        or factor_source.get("expected_size_bytes") != factor_size
    ):
        raise TargetSeriesError("base decomposition factor identity differs")
    proof = json.loads(proof_content)
    if not isinstance(proof, dict) or not isinstance(
        proof.get("series_contracts"), list
    ):
        raise TargetSeriesError("provider-ledger target proof is invalid")
    try:
        original_as_of = date.fromisoformat(str(spec.get("as_of_date", "")))
        ledger_as_of = date.fromisoformat(str(proof.get("as_of_date", "")))
    except ValueError as exc:
        raise TargetSeriesError(
            "provider-ledger decomposition date is invalid"
        ) from exc
    spec["as_of_date"] = max(original_as_of, ledger_as_of).isoformat()
    target_source.update(
        {
            "artifact_id": "provider-ledger/canonical-strategy-returns.csv",
            "expected_sha256": hashlib.sha256(target_content).hexdigest(),
            "expected_size_bytes": len(target_content),
            "relative_path": "targets.csv",
            "schema_name": "portfolio-canonical-strategy-returns",
            "schema_version": "1",
        }
    )
    artifacts = {
        "provider-ledger-generation.json": provider_ledger_manifest_path.read_bytes(),
        "provider-ledger.json": provider_ledger_catalog_path.read_bytes(),
        "provider-ledger-source-proof.json": proof_content,
        "strategy-evidence.json": evidence_content,
    }
    target_prefixes = ("provider-overlay/", "provider-rollforward/", "provider-ledger/")
    retained = []
    for source in spec.get("provenance_sources", []):
        if not isinstance(source, dict):
            raise TargetSeriesError("decomposition provenance source is invalid")
        if not str(source.get("artifact_id", "")).startswith(target_prefixes):
            retained.append(source)
    additions = []
    schemas = {
        "provider-ledger-generation.json": "generation-manifest",
        "provider-ledger.json": "provider-ledger-catalog",
        "provider-ledger-source-proof.json": "portfolio-canonical-series-proof",
        "strategy-evidence.json": "portfolio-strategy-provider-evidence",
    }
    for relative_path, content in artifacts.items():
        additions.append(
            {
                "artifact_id": f"provider-ledger/{relative_path}",
                "expected_sha256": hashlib.sha256(content).hexdigest(),
                "expected_size_bytes": len(content),
                "relative_path": relative_path,
                "schema_name": schemas[relative_path],
                "schema_version": "1",
            }
        )
    spec["provenance_sources"] = [*retained, *additions]
    return canonical_json_bytes(spec), factor_path.read_bytes(), artifacts


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise TargetSeriesError(f"existing output differs: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-targets", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--nav", type=Path)
    parser.add_argument("--holdings", type=Path)
    parser.add_argument("--daily-holdings", type=Path)
    parser.add_argument("--subscriptions", type=Path)
    parser.add_argument("--product-id")
    parser.add_argument("--series-id")
    parser.add_argument("--replace-series", action="store_true")
    parser.add_argument("--base-strategy-evidence", type=Path)
    parser.add_argument("--strategy-id")
    parser.add_argument("--base-decomposition-spec", type=Path)
    parser.add_argument("--factors", type=Path)
    parser.add_argument("--base-correlation-spec", type=Path)
    parser.add_argument("--base-correlation-returns", type=Path)
    parser.add_argument("--quantbase-manifest", type=Path)
    parser.add_argument("--quantbase-history", type=Path)
    parser.add_argument("--quantbase-activity", type=Path)
    parser.add_argument("--quantbase-fund-id")
    parser.add_argument("--quantbase-series-id")
    parser.add_argument("--portfolio123-manifest", type=Path)
    parser.add_argument("--portfolio123-equity", type=Path)
    parser.add_argument("--portfolio123-metadata", type=Path)
    parser.add_argument("--portfolio123-performance", type=Path)
    parser.add_argument("--portfolio123-returns", type=Path)
    parser.add_argument("--portfolio123-relationships", type=Path)
    parser.add_argument("--portfolio123-provider-entity-id")
    parser.add_argument("--portfolio123-simulation-entity-id")
    parser.add_argument("--portfolio123-strategy-evidence", type=Path)
    parser.add_argument("--portfolio123-account-entity-id")
    parser.add_argument("--portfolio123-strategy-id")
    parser.add_argument("--portfolio123-account-series-id")
    parser.add_argument("--strategy-history-manifest", type=Path)
    parser.add_argument("--strategy-history-returns", type=Path)
    parser.add_argument("--provider-ledger-manifest", type=Path)
    parser.add_argument("--provider-ledger-catalog", type=Path)
    parser.add_argument("--financial-mvp-contract", type=Path)
    parser.add_argument("--repository-sha")
    parser.add_argument("--as-of-date")
    parser.add_argument("--provider-ledger-current", type=Path)
    parser.add_argument("--matrixport-current", type=Path)
    parser.add_argument("--quantbase-current", type=Path)
    parser.add_argument("--portfolio123-current", type=Path)
    parser.add_argument("--layered-history-generation", action="store_true")
    parser.add_argument("--immutable-generations", action="store_true")
    parser.add_argument("--verify-layered-history", type=Path)
    parser.add_argument("--require-mode", choices=("LIVE_ONLY", "LIVE_PLUS_BACKTEST"))
    parser.add_argument(
        "--assurance-profile", choices=tuple(sorted(LAYERED_HISTORY_ASSURANCE_PROFILES))
    )
    parser.add_argument("--evaluated-on")
    parser.add_argument("--layered-correlation-only", action="store_true")
    parser.add_argument("--output-root", type=Path)
    return parser


def _required(args: argparse.Namespace, names: Sequence[str], mode: str) -> None:
    missing = [name for name in names if getattr(args, name) is None]
    if missing:
        rendered = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise TargetSeriesError(f"{mode} requires {rendered}")


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.layered_history_generation and args.verify_layered_history is not None:
            raise TargetSeriesError(
                "layered history build and verification modes are mutually exclusive"
            )
        if (args.assurance_profile is None) != (args.evaluated_on is None):
            raise TargetSeriesError(
                "assurance profile and evaluation date must be supplied together"
            )
        assurance_evaluated_on: date | None = None
        if args.evaluated_on is not None:
            try:
                assurance_evaluated_on = date.fromisoformat(args.evaluated_on)
            except ValueError as exc:
                raise TargetSeriesError("assurance evaluation date is invalid") from exc
            if assurance_evaluated_on.isoformat() != args.evaluated_on:
                raise TargetSeriesError("assurance evaluation date is invalid")
        if args.assurance_profile is not None and args.verify_layered_history is None:
            raise TargetSeriesError(
                "assurance profile evaluation requires layered history verification"
            )
        if args.verify_layered_history is not None:
            _required(args, ("financial_mvp_contract",), "layered history verification")
            result = verify_layered_history_generation(
                args.verify_layered_history,
                args.financial_mvp_contract,
                require_mode=args.require_mode,
                assurance_profile=args.assurance_profile,
                evaluated_on=assurance_evaluated_on,
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0
        if args.layered_history_generation:
            pointer_names = (
                "provider_ledger_current",
                "matrixport_current",
                "quantbase_current",
                "portfolio123_current",
            )
            if args.immutable_generations and any(
                getattr(args, name) is not None for name in pointer_names
            ):
                raise TargetSeriesError(
                    "immutable generations do not accept current pointers"
                )
            required_names = (
                "financial_mvp_contract",
                "repository_sha",
                "as_of_date",
                "provider_ledger_manifest",
                "provider_ledger_catalog",
                "manifest",
                "nav",
                "holdings",
                "daily_holdings",
                "subscriptions",
                "product_id",
                "quantbase_manifest",
                "quantbase_history",
                "quantbase_activity",
                "quantbase_fund_id",
                "portfolio123_manifest",
                "portfolio123_metadata",
                "portfolio123_performance",
                "portfolio123_equity",
                "portfolio123_returns",
                "portfolio123_relationships",
                "output_root",
            ) + (() if args.immutable_generations else pointer_names)
            _required(args, required_names, "layered history generation")
            artifacts, manifest_content = build_layered_history_generation(
                contract_path=args.financial_mvp_contract,
                repository_sha=args.repository_sha,
                as_of_date=args.as_of_date,
                provider_ledger_pointer_path=args.provider_ledger_current,
                provider_ledger_manifest_path=args.provider_ledger_manifest,
                provider_ledger_catalog_path=args.provider_ledger_catalog,
                matrixport_pointer_path=args.matrixport_current,
                matrixport_manifest_path=args.manifest,
                matrixport_nav_path=args.nav,
                matrixport_holdings_path=args.holdings,
                matrixport_daily_holdings_path=args.daily_holdings,
                matrixport_subscription_path=args.subscriptions,
                matrixport_product_id=args.product_id,
                quantbase_pointer_path=args.quantbase_current,
                quantbase_manifest_path=args.quantbase_manifest,
                quantbase_history_path=args.quantbase_history,
                quantbase_activity_path=args.quantbase_activity,
                quantbase_fund_id=args.quantbase_fund_id,
                portfolio123_pointer_path=args.portfolio123_current,
                portfolio123_manifest_path=args.portfolio123_manifest,
                portfolio123_metadata_path=args.portfolio123_metadata,
                portfolio123_performance_path=args.portfolio123_performance,
                portfolio123_equity_path=args.portfolio123_equity,
                portfolio123_returns_path=args.portfolio123_returns,
                portfolio123_relationships_path=args.portfolio123_relationships,
                strategy_history_manifest_path=args.strategy_history_manifest,
                strategy_history_returns_path=args.strategy_history_returns,
                source_selection_mode=(
                    "IMMUTABLE_GENERATIONS"
                    if args.immutable_generations
                    else "CURRENT_POINTERS"
                ),
            )
            result = validate_layered_history_generation(
                artifacts=artifacts,
                manifest_content=manifest_content,
                contract_path=args.financial_mvp_contract,
                require_mode=args.require_mode,
            )
            write_layered_history_generation(
                args.output_root, artifacts, manifest_content
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0
        _required(args, ("base_targets", "output_root"), "target-series")
        if args.provider_ledger_manifest is not None:
            _required(
                args,
                (
                    "provider_ledger_catalog",
                    "base_decomposition_spec",
                    "factors",
                    "base_correlation_spec",
                    "base_correlation_returns",
                    "portfolio123_manifest",
                    "portfolio123_returns",
                    "portfolio123_relationships",
                    "portfolio123_provider_entity_id",
                    "portfolio123_simulation_entity_id",
                    "portfolio123_account_series_id",
                ),
                "provider-ledger roll-forward",
            )
            targets, strategy_evidence, proof, ledger_manifest = (
                build_provider_ledger_targets(
                    args.provider_ledger_manifest, args.provider_ledger_catalog
                )
            )
            spec, factors, ledger_artifacts = (
                build_provider_ledger_decomposition_inputs(
                    args.base_decomposition_spec,
                    args.factors,
                    args.base_targets,
                    targets,
                    strategy_evidence,
                    proof,
                    args.provider_ledger_manifest,
                    args.provider_ledger_catalog,
                )
            )
            decomposition_root = args.output_root / "decomposition"
            _write_exact(decomposition_root / "targets.csv", targets)
            _write_exact(decomposition_root / "factors.csv", factors)
            _write_exact(decomposition_root / "spec.json", spec)
            for relative_path, content in ledger_artifacts.items():
                _write_exact(decomposition_root / relative_path, content)
            rebound = json.loads(spec)
            for source in rebound.get("provenance_sources", []):
                if not isinstance(source, dict):
                    raise TargetSeriesError("rebound provenance source is invalid")
                relative = str(source.get("relative_path", ""))
                destination = decomposition_root / relative
                if destination.exists():
                    continue
                _write_exact(
                    destination,
                    (args.base_decomposition_spec.parent / relative).read_bytes(),
                )
            evidence_value = json.loads(strategy_evidence)
            (
                correlation_spec,
                correlation_returns,
                correlation_proof,
                correlation_p123_manifest,
            ) = build_layered_correlation_inputs(
                args.base_correlation_spec,
                args.base_correlation_returns,
                targets,
                args.portfolio123_manifest,
                args.portfolio123_returns,
                args.portfolio123_relationships,
                args.portfolio123_provider_entity_id,
                args.portfolio123_simulation_entity_id,
                args.portfolio123_account_series_id,
                strategy_evidence,
                str(evidence_value["as_of_date"]),
                args.strategy_history_manifest,
                args.strategy_history_returns,
            )
            correlation_root = args.output_root / "correlation"
            _write_exact(correlation_root / "spec.json", correlation_spec)
            _write_exact(correlation_root / "returns.csv", correlation_returns)
            _write_exact(correlation_root / "evidence-proof.json", correlation_proof)
            _write_exact(
                correlation_root / "portfolio123-generation.json",
                correlation_p123_manifest,
            )
            _write_exact(args.output_root / "strategy-evidence.json", strategy_evidence)
            _write_exact(args.output_root / "provider-ledger-source-proof.json", proof)
            _write_exact(
                args.output_root / "provider-ledger-generation.json", ledger_manifest
            )
        elif args.layered_correlation_only:
            _required(
                args,
                (
                    "base_correlation_spec",
                    "base_correlation_returns",
                    "portfolio123_manifest",
                    "portfolio123_returns",
                    "portfolio123_relationships",
                    "portfolio123_provider_entity_id",
                    "portfolio123_simulation_entity_id",
                    "portfolio123_strategy_evidence",
                    "portfolio123_account_series_id",
                ),
                "layered correlation",
            )
            manifest_value = _load_json(
                args.portfolio123_manifest, "Portfolio123 generation manifest"
            )
            as_of_date = str(manifest_value.get("as_of_date", ""))
            try:
                date.fromisoformat(as_of_date)
            except ValueError as exc:
                raise TargetSeriesError(
                    "Portfolio123 generation as-of date is invalid"
                ) from exc
            specification, returns, proof, manifest = build_layered_correlation_inputs(
                args.base_correlation_spec,
                args.base_correlation_returns,
                args.base_targets.read_bytes(),
                args.portfolio123_manifest,
                args.portfolio123_returns,
                args.portfolio123_relationships,
                args.portfolio123_provider_entity_id,
                args.portfolio123_simulation_entity_id,
                args.portfolio123_account_series_id,
                args.portfolio123_strategy_evidence.read_bytes(),
                as_of_date,
                args.strategy_history_manifest,
                args.strategy_history_returns,
            )
            _write_exact(args.output_root / "spec.json", specification)
            _write_exact(args.output_root / "returns.csv", returns)
            _write_exact(args.output_root / "evidence-proof.json", proof)
            _write_exact(args.output_root / "portfolio123-generation.json", manifest)
        elif args.quantbase_manifest is not None:
            _required(
                args,
                (
                    "base_decomposition_spec",
                    "factors",
                    "base_correlation_spec",
                    "base_correlation_returns",
                    "quantbase_history",
                    "quantbase_activity",
                    "quantbase_fund_id",
                    "quantbase_series_id",
                    "portfolio123_manifest",
                    "portfolio123_equity",
                    "portfolio123_metadata",
                    "portfolio123_provider_entity_id",
                    "portfolio123_account_entity_id",
                    "portfolio123_strategy_id",
                    "portfolio123_account_series_id",
                ),
                "provider roll-forward",
            )
            targets, quantbase_proof = build_quantbase_targets(
                args.base_targets,
                args.quantbase_manifest,
                args.quantbase_history,
                args.quantbase_activity,
                args.quantbase_fund_id,
                args.quantbase_series_id,
            )
            p123_evidence = build_portfolio123_evidence(
                targets,
                args.portfolio123_manifest,
                args.portfolio123_equity,
                args.portfolio123_metadata,
                args.portfolio123_provider_entity_id,
                args.portfolio123_account_entity_id,
                args.portfolio123_strategy_id,
                args.portfolio123_account_series_id,
            )
            strategy_evidence = build_strategy_evidence(quantbase_proof, p123_evidence)
            spec, factors, quantbase_manifest, p123_manifest = (
                build_provider_rollforward_inputs(
                    args.base_decomposition_spec,
                    args.factors,
                    args.base_targets,
                    targets,
                    quantbase_proof,
                    strategy_evidence,
                    args.quantbase_manifest,
                    args.portfolio123_manifest,
                )
            )
            decomposition_root = args.output_root / "decomposition"
            _write_exact(decomposition_root / "targets.csv", targets)
            _write_exact(
                decomposition_root / "quantbase-source-proof.json",
                quantbase_proof,
            )
            _write_exact(
                decomposition_root / "strategy-evidence.json", strategy_evidence
            )
            _write_exact(decomposition_root / "spec.json", spec)
            _write_exact(decomposition_root / "factors.csv", factors)
            _write_exact(
                decomposition_root / "quantbase-generation.json", quantbase_manifest
            )
            _write_exact(
                decomposition_root / "portfolio123-generation.json", p123_manifest
            )
            base_spec = _load_json(
                args.base_decomposition_spec, "base decomposition spec"
            )
            for source in base_spec.get("provenance_sources", []):
                if not isinstance(source, dict):
                    raise TargetSeriesError("base provenance source is invalid")
                relative = str(source.get("relative_path", ""))
                if not relative:
                    raise TargetSeriesError("base provenance source path is invalid")
                _write_exact(
                    decomposition_root / relative,
                    (args.base_decomposition_spec.parent / relative).read_bytes(),
                )
            evidence_value = json.loads(strategy_evidence)
            layered_arguments = (
                args.portfolio123_returns,
                args.portfolio123_relationships,
                args.portfolio123_simulation_entity_id,
            )
            if any(item is not None for item in layered_arguments) and not all(
                item is not None for item in layered_arguments
            ):
                raise TargetSeriesError(
                    "layered correlation requires Portfolio123 returns, "
                    "relationships, and simulation entity ID together"
                )
            correlation_root = args.output_root / "correlation"
            if all(item is not None for item in layered_arguments):
                (
                    correlation_spec,
                    correlation_returns,
                    correlation_proof,
                    correlation_p123_manifest,
                ) = build_layered_correlation_inputs(
                    args.base_correlation_spec,
                    args.base_correlation_returns,
                    targets,
                    args.portfolio123_manifest,
                    args.portfolio123_returns,
                    args.portfolio123_relationships,
                    args.portfolio123_provider_entity_id,
                    args.portfolio123_simulation_entity_id,
                    args.portfolio123_account_series_id,
                    strategy_evidence,
                    str(evidence_value["as_of_date"]),
                    args.strategy_history_manifest,
                    args.strategy_history_returns,
                )
                _write_exact(
                    correlation_root / "evidence-proof.json", correlation_proof
                )
                _write_exact(
                    correlation_root / "portfolio123-generation.json",
                    correlation_p123_manifest,
                )
            else:
                correlation_spec, correlation_returns = build_correlation_inputs(
                    args.base_correlation_spec,
                    args.base_correlation_returns,
                    targets,
                    str(evidence_value["as_of_date"]),
                )
            _write_exact(correlation_root / "spec.json", correlation_spec)
            _write_exact(correlation_root / "returns.csv", correlation_returns)
            _write_exact(args.output_root / "strategy-evidence.json", strategy_evidence)
        else:
            _required(
                args,
                (
                    "manifest",
                    "nav",
                    "holdings",
                    "daily_holdings",
                    "subscriptions",
                    "product_id",
                    "series_id",
                ),
                "Matrixport normalization",
            )
            targets, proof = build_matrixport_targets(
                args.base_targets,
                args.manifest,
                args.nav,
                args.holdings,
                args.subscriptions,
                args.product_id,
                args.series_id,
                daily_holdings_path=args.daily_holdings,
                replace_existing=args.replace_series,
            )
            _write_exact(args.output_root / "targets.csv", targets)
            _write_exact(args.output_root / "source-proof.json", proof)
            if (args.base_strategy_evidence is None) != (args.strategy_id is None):
                raise TargetSeriesError(
                    "base strategy evidence and strategy ID must be supplied together"
                )
            if args.base_strategy_evidence is not None and args.strategy_id is not None:
                _write_exact(
                    args.output_root / "strategy-evidence.json",
                    build_matrixport_strategy_evidence(
                        args.base_strategy_evidence, proof, args.strategy_id
                    ),
                )
            if (args.base_decomposition_spec is None) != (args.factors is None):
                raise TargetSeriesError(
                    "base decomposition spec and factors must be supplied together"
                )
            if args.base_decomposition_spec is not None and args.factors is not None:
                spec, factors, upstream_manifest = build_decomposition_inputs(
                    args.base_decomposition_spec,
                    args.factors,
                    args.base_targets,
                    args.manifest,
                    targets,
                    proof,
                )
                _write_exact(args.output_root / "spec.json", spec)
                _write_exact(args.output_root / "factors.csv", factors)
                _write_exact(
                    args.output_root / "matrixport-generation.json",
                    upstream_manifest,
                )
    except (OSError, TargetSeriesError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 2
    print(
        json.dumps(
            {
                "output_root": str(args.output_root),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
